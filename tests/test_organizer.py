"""Comprehensive automated test suite for SeaStar v2 organizer (G0..G6 multi-string, watch/finalize, hadd, CSV, real-data)."""
import ast
from datetime import datetime, timedelta, timezone
import gzip
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import sys
import tempfile
import unittest

import numpy as np

V2_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = V2_ROOT.parent
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from organizer.cli import main as cli_main
from organizer.config import OrganizerConfig, load_organizer_config
from organizer.decoder_runner import _inspect_root_with_cli, inspect_root_file
from organizer.discovery import discover_runs, scan_dat_volume
from organizer.packager import finalize_session, verify_dataset, watch_once
from organizer.state import StateStore

BEIJING_TZ = timezone(timedelta(hours=8))


def make_trigger(number: int, ns: int) -> bytes:
    sec, nano = divmod(int(ns), 1_000_000_000)
    return b"\xff\x12" + struct.pack(">HIII", int(number) & 0xFFFF, 0, sec, nano)


def make_wave(board: int, channel: int, wave_id: int, ns: int, points: int = 64, peak: int = 600) -> bytes:
    sec, nano = divmod(int(ns), 1_000_000_000)
    x = np.arange(points)
    vals = np.round(200 + peak * np.exp(-0.5 * ((x - 24) / 3.0) ** 2)).astype(">u2")
    header = bytes([0xFE, ((board & 0x0F) << 4) | (channel & 0x0F), wave_id & 0xFF, 0]) + struct.pack(
        ">III", sec, nano, points
    )
    return header + vals.tobytes()


class OrganizerTest(unittest.TestCase):
    def test_python38_compatible_syntax(self) -> None:
        targets = list((V2_ROOT / "organizer").glob("*.py")) + [V2_ROOT / "scripts" / "build_release.py"]
        for path in targets:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=8)

    def test_board_mapping_g0_to_g6_and_override(self) -> None:
        cfg = OrganizerConfig()
        for board in range(7):
            self.assertEqual(cfg.string_for_board(board), f"G{board}")
        custom = load_organizer_config(overrides={"board_map": {"7": "G4", "8": "G6"}})
        self.assertEqual(custom.string_for_board(0), "G0")
        self.assertEqual(custom.string_for_board(6), "G6")
        self.assertEqual(custom.string_for_board(7), "G4")
        self.assertEqual(custom.string_for_board(8), "G6")

    def test_ipc_decoder_watch_incremental_and_multi_string_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            # Simulate exact IPC layout (cwd = .../disk/debug/data/both):
            #   ../../build/decoder     -> <root>/disk/debug/build/decoder
            #   ../../cfg/config.json   -> <root>/disk/debug/cfg/config.json
            #   raw_data_dir            -> <root>/disk/debug/data/both/*.dat
            build_dir = root / "disk" / "debug" / "build"
            cfg_dir = root / "disk" / "debug" / "cfg"
            both_dir = root / "disk" / "debug" / "data" / "both"
            out_dir = root / "disk" / "debug" / "data" / "organized"
            mirror_root = root / "mirror" / "source_pi01"
            for d in (build_dir, cfg_dir, both_dir, out_dir, mirror_root / ".sync"):
                d.mkdir(parents=True, exist_ok=True)

            cfg_file = cfg_dir / "config.json"
            cfg_file.write_text('{"channels": 8}\n', encoding="utf-8")

            invocations_log = root / "decoder_calls.jsonl"
            fake_decoder = build_dir / "decoder"
            fake_decoder.write_text(
                f"""#!{sys.executable}
import json, os, sys
sys.path.insert(0, {repr(str(V2_ROOT))})
from organizer.decoder_runner import _ensure_pyroot_decoder
in_dat, out_root = sys.argv[1], sys.argv[2]
assert sys.argv[3] == "--config", sys.argv
assert os.path.isfile(sys.argv[4]), sys.argv
with open({repr(str(invocations_log))}, "a", encoding="utf-8") as log:
    log.write(json.dumps({{"cwd": os.getcwd(), "in": in_dat, "cfg": sys.argv[4]}}) + "\\n")
ROOT = _ensure_pyroot_decoder({repr(str(out_dir / ".organizer_work"))})
rc = int(ROOT.seastar_v2_jit.decode_dat_file(in_dat, out_root))
sys.exit(0 if rc >= 0 else 2)
""",
                encoding="utf-8",
            )
            fake_decoder.chmod(0o755)

            # Create multi-string runs across G0 (fee_0), G3 (fee_3, 3 volumes), and G5 (fee_5, 2 volumes)
            # Crossing midnight: 2026-11-01 23:59:50 -> 2026-11-02 00:00:20 => session folder 20261101_1102
            start_dt = datetime(2026, 11, 1, 23, 59, 50, tzinfo=BEIJING_TZ)
            stamp = start_dt.strftime("%Y%m%d%H%M%S")
            base_s = 10_000

            # G3: 3 volumes (idx001, idx002 sealed; idx003 active during watch)
            g3_v1 = (
                make_trigger(1, (base_s + 0) * 10**9)
                + make_wave(3, 0, 1, (base_s + 0) * 10**9 + 100)
                + make_trigger(2, (base_s + 5) * 10**9)  # empty trigger (must not enter ROOT)
                + make_trigger(3, (base_s + 10) * 10**9)
                + make_wave(3, 1, 2, (base_s + 10) * 10**9 + 200)
            )
            g3_v2 = (
                make_trigger(4, (base_s + 20) * 10**9)
                + make_wave(3, 0, 3, (base_s + 20) * 10**9 + 100)
                + make_wave(3, 2, 4, (base_s + 20) * 10**9 + 120)
            )
            g3_v3 = (
                make_trigger(5, (base_s + 30) * 10**9)
                + make_wave(3, 7, 5, (base_s + 30) * 10**9 + 300)
            )
            for idx, payload in ((1, g3_v1), (2, g3_v2), (3, g3_v3)):
                p = both_dir / f"data_fee_3_run010158_idx{idx:03d}_{stamp}.dat"
                p.write_bytes(payload)

            # G0: 1 volume; G5: 2 volumes
            (both_dir / f"data_fee_0_run010158_idx001_{stamp}.dat").write_bytes(
                make_trigger(10, (base_s + 2) * 10**9) + make_wave(0, 0, 1, (base_s + 2) * 10**9 + 50)
            )
            (both_dir / f"data_fee_5_run010158_idx001_{stamp}.dat").write_bytes(
                make_trigger(20, (base_s + 4) * 10**9) + make_wave(5, 3, 1, (base_s + 4) * 10**9 + 80)
            )
            (both_dir / f"data_fee_5_run010158_idx002_{stamp}.dat").write_bytes(
                make_trigger(21, (base_s + 25) * 10**9) + make_wave(5, 4, 2, (base_s + 25) * 10**9 + 90)
            )

            # Age mtimes by 10 seconds so settle check passes immediately
            old_ts = (datetime.now().timestamp() - 10.0)
            for p in both_dir.glob("*.dat"):
                os.utime(p, (old_ts, old_ts))

            # Create slow-control CSVs in fee-monitor mirror with header + out-of-window + in-window + incomplete tail
            csv_header = (
                "timestamp_ms,hit_rate_ch0,baseline_ch0,fpga_temperature_c,"
                "fpga_timestamp_seconds,fpga_timestamp_nanoseconds,valid\n"
            )
            t0_ms = int(start_dt.timestamp() * 1000)
            progress_files = {}
            for fee, fsec in ((0, base_s + 2), (3, base_s + 10), (5, base_s + 15)):
                csv_name = f"readback_fee_{fee}.csv"
                csv_body = (
                    csv_header
                    + f"{t0_ms - 300_000},10,197,41.2,{fsec - 300},0,1\n"
                    + f"{t0_ms + 5_000},12,198,41.5,{fsec},0,1\n"
                    + f"{t0_ms + 20_000},13,198,41.6,{fsec + 15},0,1\n"
                    + f"{t0_ms + 25_000},99,999"  # Incomplete trailing line without '\n' — must be ignored!
                ).encode("utf-8")
                cpath = mirror_root / csv_name
                cpath.write_bytes(csv_body)
                progress_files[csv_name] = {"size": len(csv_body), "offset": len(csv_body), "missing": False}
            (mirror_root / ".sync" / "progress.json").write_text(
                json.dumps({"initialized": True, "files": progress_files}),
                encoding="utf-8",
            )

            cfg = OrganizerConfig(
                raw_data_dir=str(both_dir),
                decoder_bin="../../build/decoder",
                decoder_config="../../cfg/config.json",
                backend="ipc",
                csv_mirror_dir=str(mirror_root.parent),
                output_root=str(out_dir),
                layout="group",
                settle_seconds=1.0,
                csv_sync_wait_seconds=0.5,
                hadd_batch_size=2,  # Force tree-batched hadd on G3's 3 volumes!
            )
            state = StateStore(cfg.resolve_work_dir() / "state.sqlite3")
            try:
                # 1. Watch pass 1: decodes sealed idx001 & idx002 of G3, idx001 of G5; leaves 3 active tail volumes
                w1 = watch_once(cfg, state)
                self.assertEqual(w1["errors"], [])
                self.assertEqual(len(w1["decoded_now"]), 3)
                self.assertEqual(w1["active_unsealed"], 3)

                # 2. Watch pass 2: zero re-decoding thanks to SQLite checkpoint!
                w2 = watch_once(cfg, state)
                self.assertEqual(w2["decoded_now"], [])
                self.assertEqual(w2["cached_sealed"], 3)

                # Verify IPC decoder was invoked with cwd == both_dir
                calls = [json.loads(line) for line in invocations_log.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(calls), 3)
                for call in calls:
                    self.assertEqual(Path(call["cwd"]).resolve(), both_dir.resolve())
                    self.assertEqual(Path(call["cfg"]).resolve(), cfg_file.resolve())

                # 3. Stop DAQ -> Finalize session: decodes only the 3 remaining tail volumes, merges via hadd, slices CSV
                fin = finalize_session(cfg, state)
                self.assertEqual(fin["status"], "ok")
                calls_after = [json.loads(line) for line in invocations_log.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(calls_after), 6)  # Exactly 3 + 3 = 6 total single-volume decode calls!

                session_dir = Path(fin["session_dir"])
                self.assertEqual(session_dir.name, "20261101_1102")
                for grp_name, expected_entries, expected_vols in (("G0", 1, 1), ("G3", 4, 3), ("G5", 2, 2)):
                    self.assertTrue((session_dir / grp_name / "raw_data").is_dir())
                    self.assertTrue((session_dir / grp_name / "rec_data").is_dir())
                    self.assertTrue((session_dir / grp_name / "csv").is_dir())
                    self.assertEqual(len(list((session_dir / grp_name / "raw_data").glob("*.dat"))), expected_vols)
                    roots = list((session_dir / grp_name / "rec_data").glob("*.root"))
                    self.assertEqual(len(roots), 1)
                    insp = inspect_root_file(roots[0])
                    self.assertTrue(insp.valid)
                    self.assertEqual(insp.entries, expected_entries)
                    # Check 0444 read-only mode on merged ROOT & sliced CSV
                    self.assertEqual(stat.S_IMODE(roots[0].stat().st_mode), 0o444)
                    csvs = list((session_dir / grp_name / "csv").glob("*.csv"))
                    self.assertEqual(len(csvs), 1)
                    self.assertEqual(stat.S_IMODE(csvs[0].stat().st_mode), 0o444)
                    lines = csvs[0].read_text(encoding="utf-8").splitlines()
                    self.assertEqual(len(lines), 3)  # header + 2 in-window complete rows (partial tail & old row excluded)

                # Verify CLI ROOT inspector matches PyROOT inspector on merged G3 ROOT
                g3_root = next((session_dir / "G3" / "rec_data").glob("*.root"))
                cli_insp = _inspect_root_with_cli(g3_root, work_dir=cfg.resolve_work_dir())
                py_insp = inspect_root_file(g3_root)
                self.assertEqual(cli_insp.valid, True)
                self.assertEqual(cli_insp.entries, py_insp.entries)
                self.assertEqual(cli_insp.first_trigger, py_insp.first_trigger)
                self.assertEqual(cli_insp.last_trigger, py_insp.last_trigger)

                # Verify read-only dataset audit passes with 0 errors and 0 warnings
                audit = verify_dataset(session_dir)
                self.assertTrue(audit["ok"], audit)
                self.assertEqual(audit["layout"], "group")
                self.assertEqual(audit["errors"], [])
                self.assertEqual(audit["warnings"], [])

                # 4. Also test legacy `--layout flat` into a separate output directory
                flat_cfg = OrganizerConfig(
                    raw_data_dir=str(both_dir),
                    decoder_bin="../../build/decoder",
                    decoder_config="../../cfg/config.json",
                    backend="ipc",
                    csv_mirror_dir=str(mirror_root.parent),
                    output_root=str(root / "flat_organized"),
                    work_dir=str(cfg.resolve_work_dir()),
                    layout="flat",
                    link_mode="copy",
                    settle_seconds=1.0,
                )
                fin_flat = finalize_session(flat_cfg, state, include_already_finalized=True)
                flat_dir = Path(fin_flat["session_dir"])
                self.assertEqual(flat_dir.name, "data_1101_1102")
                self.assertEqual(len(list(flat_dir.glob("*.dat"))), 6)
                self.assertEqual(len(list(flat_dir.glob("*.root"))), 3)
                self.assertEqual(len(list(flat_dir.glob("*.csv"))), 3)
                flat_audit = verify_dataset(flat_dir)
                self.assertTrue(flat_audit["ok"], flat_audit)
                self.assertEqual(flat_audit["layout"], "flat")
            finally:
                state.close()

    def test_gzip_corrupted_dat_and_missing_volume_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            raw_dir = root / "raw"
            raw_dir.mkdir()
            stamp = "20261026171940"
            bad_dat = raw_dir / f"data_fee_3_run010108_idx001_{stamp}.dat"
            bad_dat.write_bytes(gzip.compress(b"accidentally-tarred-over-idx001"))
            scan = scan_dat_volume(bad_dat)
            self.assertTrue(scan.is_gzip)
            self.assertFalse(scan.clean)

            # Missing idx002 between idx001 and idx003 must block finalize unless allow_partial=True
            clean_payload = make_trigger(1, 10**9) + make_wave(3, 0, 1, 10**9 + 100)
            bad_dat.write_bytes(clean_payload)
            (raw_dir / f"data_fee_3_run010108_idx003_{stamp}.dat").write_bytes(clean_payload)
            old_ts = datetime.now().timestamp() - 10.0
            for p in raw_dir.glob("*.dat"):
                os.utime(p, (old_ts, old_ts))

            cfg = OrganizerConfig(
                raw_data_dir=str(raw_dir),
                output_root=str(root / "out"),
                backend="pyroot",
                settle_seconds=0.5,
            )
            runs = discover_runs(raw_dir, cfg, finalize=True)
            self.assertEqual(runs[0].missing_indices, [2])
            state = StateStore(cfg.resolve_work_dir() / "state.sqlite3")
            try:
                with self.assertRaisesRegex(RuntimeError, "idx002"):
                    finalize_session(cfg, state)
            finally:
                state.close()

    def test_real_baseline_single_volume_decode_and_csv_slice(self) -> None:
        baseline_dat = WORKSPACE_ROOT / "data-ana-monitor" / "data_fee_3_run010100_idx001_20261023211634.dat"
        baseline_root = WORKSPACE_ROOT / "data-ana-monitor" / "data_fee_3_run010100_idx001_20261023211634.root"
        baseline_csv = WORKSPACE_ROOT / "readback_fee_3_1023-1026.csv"
        if not (baseline_dat.is_file() and baseline_root.is_file() and baseline_csv.is_file()):
            self.skipTest("基线样例文件不存在")

        before_dat_stat = baseline_dat.stat()
        before_csv_stat = baseline_csv.stat()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            raw_dir = root / "raw"
            raw_dir.mkdir()
            copied_dat = raw_dir / baseline_dat.name
            shutil.copy2(baseline_dat, copied_dat)
            old_ts = datetime.now().timestamp() - 10.0
            os.utime(copied_dat, (old_ts, old_ts))

            import contextlib
            import io
            buf_out = io.StringIO()
            with contextlib.redirect_stdout(buf_out):
                rc = cli_main(
                    [
                        "finalize",
                        "--raw-dir",
                        str(raw_dir),
                        "--csv-dir",
                        str(baseline_csv),
                        "--output-root",
                        str(root / "organized"),
                        "--backend",
                        "pyroot",
                        "--link-mode",
                        "copy",
                        "--settle-seconds",
                        "0",
                        "--json",
                    ]
                )
            self.assertEqual(rc, 0, buf_out.getvalue())
            session_dir = root / "organized" / "20261023_1023"
            self.assertTrue(session_dir.is_dir())
            produced_root = next((session_dir / "G3" / "rec_data").glob("*.root"))
            produced_csv = next((session_dir / "G3" / "csv").glob("*.csv"))

            expected_insp = inspect_root_file(baseline_root)
            actual_insp = inspect_root_file(produced_root)
            self.assertEqual(actual_insp.entries, expected_insp.entries)
            self.assertEqual(actual_insp.entries, 19562)
            self.assertEqual(actual_insp.first_trigger, expected_insp.first_trigger)
            self.assertEqual(actual_insp.last_trigger, expected_insp.last_trigger)
            self.assertGreater(len(produced_csv.read_text(encoding="utf-8").splitlines()), 50)

        # Verify baseline files were 100% untouched (even mtime_ns and mode)
        self.assertEqual(baseline_dat.stat().st_mtime_ns, before_dat_stat.st_mtime_ns)
        self.assertEqual(baseline_dat.stat().st_mode, before_dat_stat.st_mode)
        self.assertEqual(baseline_csv.stat().st_mtime_ns, before_csv_stat.st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
