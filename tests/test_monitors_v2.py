"""Integration tests for SeaStar v2 monitors (data-ana-monitor v2 + fee-monitor v2) on multi-string & baseline data."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import numpy as np

V2_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = V2_ROOT.parent

ANA_DIR = V2_ROOT / "data-ana-monitor" / "online"
FEE_DIR = V2_ROOT / "fee-monitor"

for p in (str(V2_ROOT), str(FEE_DIR), str(ANA_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from acquisition import RootReader, discover
from analysis import Config
from sync_service import stream_label
from organizer.packager import verify_dataset


def _load_module(alias: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(alias, str(file_path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ana_server = _load_module("ana_server_v2", ANA_DIR / "server.py")
fee_server = _load_module("fee_server_v2", FEE_DIR / "server.py")
Monitor = ana_server.Monitor


class _InMemorySocket:
    def __init__(self, request_bytes: bytes) -> None:
        self.rfile = io.BytesIO(request_bytes)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args, **kwargs):
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)

    def close(self) -> None:
        pass


def call_fee_handler(data_dir: Path, path_query: str) -> bytes:
    handler_cls = fee_server.make_handler(data_dir, sync=None)
    sock = _InMemorySocket(f"GET {path_query} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode("utf-8"))
    handler_cls(sock, ("127.0.0.1", 50000), None)
    raw = sock.wfile.getvalue()
    return raw.split(b"\r\n\r\n", 1)[1]


class MonitorsV2Test(unittest.TestCase):
    def test_fee_monitor_v2_groups_hidden_exclusion_and_local_launcher(self) -> None:
        for i in range(7):
            self.assertEqual(stream_label(f"fee_{i}"), f"G{i} · FEE {i}")
        self.assertEqual(stream_label("fee_8"), "FEE 8")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for grp, fee in (("G0", 0), ("G3", 3), ("G5", 5), ("G6", 6)):
                cdir = root / grp / "csv"
                cdir.mkdir(parents=True)
                (cdir / f"readback_fee_{fee}_11012050-11021034.csv").write_text(
                    "timestamp_ms,baseline_ch0\n1000,198\n",
                    encoding="utf-8",
                )
            # Hidden directories (.archive, .sync, .organizer_work) must NOT appear in /api/files
            for hidden in (".archive", ".sync", ".organizer_work", "G3/results/analysis_sample/tables"):
                hdir = root / hidden
                hdir.mkdir(parents=True)
                (hdir / "readback_fee_3.csv").write_text("timestamp_ms,baseline_ch0\n1,1\n", encoding="utf-8")

            payload = json.loads(call_fee_handler(root, "/api/files").decode("utf-8"))
            files = payload["files"]
            self.assertEqual(len(files), 4)
            self.assertEqual([f["group"] for f in files], ["G0", "G3", "G5", "G6"])

            # Verify fee-monitor/start.sh --local --help works without invoking SSH
            res = subprocess.run(
                ["sh", str(FEE_DIR / "start.sh"), "--local", "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("--local", res.stdout)

    def test_data_ana_monitor_v2_discovers_g0_to_g6_and_skips_organizer_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp).resolve() / "20261101_1102"
            stamp = "20261101205133"
            for board in (0, 1, 2, 3, 4, 5, 6):
                raw_dir = session / f"G{board}" / "raw_data"
                rec_dir = session / f"G{board}" / "rec_data"
                csv_dir = session / f"G{board}" / "csv"
                for d in (raw_dir, rec_dir, csv_dir):
                    d.mkdir(parents=True)
                (raw_dir / f"data_fee_{board}_run010158_idx001_{stamp}.dat").write_bytes(b"\x00" * 32)
                (rec_dir / f"data_fee_{board}_11012051-11021034.root").write_bytes(b"root")
                (csv_dir / f"readback_fee_{board}_11012051-11021034.csv").write_text("timestamp_ms\n", encoding="utf-8")

            work = session / ".organizer_work" / "parts"
            work.mkdir(parents=True)
            (work / "data_fee_3_temp.root").write_bytes(b"temp")

            sources = discover(session)
            self.assertEqual(len(sources), 14)  # 7 DAT runs + 7 merged ROOTs
            groups_seen = sorted({s["string_group"] for s in sources})
            self.assertEqual(groups_seen, ["G0", "G1", "G2", "G3", "G4", "G5", "G6"])
            self.assertFalse(any(".organizer_work" in s["key"] for s in sources))

    def test_real_data_1026_1027_audit_and_root_replay(self) -> None:
        dataset_1026 = WORKSPACE_ROOT / "data_1026_1027"
        if not dataset_1026.is_dir():
            self.skipTest("data_1026_1027 不存在")

        # 1. Verify read-only dataset audit accurately diagnoses idx001 gzip overwrite & validates ROOT
        report = verify_dataset(dataset_1026, check_sha256=False)
        self.assertEqual(report["layout"], "flat")
        self.assertFalse(report["ok"])
        self.assertTrue(any("idx001" in err and "gzip" in err for err in report["errors"]), report["errors"])
        grp = report["groups"][0]
        self.assertEqual(grp["group"], "G3")
        self.assertEqual(grp["clean_dat_wave_events"], 1_099_489)
        self.assertEqual(grp["root_total_entries"], 1_182_113)

        # 2. Verify data-ana-monitor v2 RootReader reads data_fee_3_10261719-10270930.root cleanly
        sources = discover(dataset_1026)
        root_source = next(s for s in sources if s["kind"] == "root")
        self.assertEqual(root_source["string_group"], "G3")
        reader = RootReader(dataset_1026, root_source)
        try:
            self.assertEqual(reader.entries, 1_182_113)
            batch = reader.read(root_source, max_events=64)
            waves = [r for r in batch if r["type"] == "wave"]
            self.assertGreater(len(waves), 64)
            self.assertEqual(waves[0]["board"], 3)
            self.assertEqual(len(waves[0]["samples"]), 504)
        finally:
            reader.close()

        # 3. Verify Monitor full replay on baseline single-volume ROOT inside isolated state dir
        single_dir = WORKSPACE_ROOT / "data-ana-monitor"
        single_sources = [s for s in discover(single_dir) if s["kind"] == "root"]
        if single_sources:
            with tempfile.TemporaryDirectory() as state_tmp:
                mon = Monitor(
                    single_dir,
                    Path(state_tmp),
                    Config(display_stride=20),
                    source=single_sources[0]["key"],
                    replay=True,
                    poll=0.05,
                )
                mon.start()
                try:
                    deadline = time.monotonic() + 20.0
                    while time.monotonic() < deadline:
                        snap = mon.snapshot()
                        if snap.get("status") in ("complete", "error", "incomplete"):
                            break
                        time.sleep(0.05)
                    self.assertEqual(snap["status"], "complete", snap.get("error"))
                    self.assertEqual(snap["wave_events"], 19_562)
                    self.assertEqual(snap["waves"], 20_543)
                    self.assertEqual(snap["source"]["string_group"], "G3")
                finally:
                    mon.close()


if __name__ == "__main__":
    unittest.main()
