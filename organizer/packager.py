"""Incremental watch orchestrator, post-run session packager (group G0..G6 & flat layouts), and read-only dataset verifier."""
from datetime import datetime, timedelta, timezone
from collections import Counter
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import VERSION
from .config import OrganizerConfig
from .csv_slicer import CsvSliceResult, slice_csv_for_run
from .decoder_runner import (
    RootInspection,
    decode_volume,
    inspect_root_file,
    merge_part_roots,
)
from .discovery import DAT_VOLUME_RE, TRIGGER_SIGNAL_RE, RunGroup, VolumeInfo, VolumeScan, discover_runs, discover_trigger_signals, scan_dat_volume
from .state import StateStore

BEIJING_TZ = timezone(timedelta(hours=8))
STRING_DIR_RE = re.compile(r"^g(\d+)$", re.IGNORECASE)
ROOT_FEE_RE = re.compile(r"^data_fee_(\d+)_.*\.root$", re.IGNORECASE)
CSV_FEE_RE = re.compile(r"^readback_fee_(\d+)(?:_.*)?\.csv$", re.IGNORECASE)


def parse_stamp_beijing(stamp_14: str) -> datetime:
    dt = datetime.strptime(stamp_14, "%Y%m%d%H%M%S")
    return dt.replace(tzinfo=BEIJING_TZ)


def trigger_tuple_to_ns(trig: Optional[Tuple[int, int, int]]) -> Optional[int]:
    if trig is None:
        return None
    return int(trig[1]) * 1_000_000_000 + int(trig[2])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _unlock_if_exists(path: Path) -> None:
    if path.exists():
        try:
            current_mode = stat.S_IMODE(path.stat().st_mode)
            if not (current_mode & stat.S_IWUSR):
                path.chmod(current_mode | stat.S_IWUSR)
        except OSError:
            pass


def _protect_readonly(path: Path, enabled: bool) -> None:
    if not enabled or not path.exists():
        return
    try:
        path.chmod(0o444)
    except OSError:
        pass


def _place_raw_dat(
    src: Path,
    dst: Path,
    link_mode: str,
    expected_sha256: str,
    readonly_protect: bool,
) -> Tuple[str, str]:
    """Place .dat into archive without mutating source inode attributes."""
    src = Path(src).resolve()
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists():
        src_stat = src.stat()
        dst_stat = dst.stat()
        if (src_stat.st_dev, src_stat.st_ino) == (dst_stat.st_dev, dst_stat.st_ino):
            return "hardlink_existing", expected_sha256 or sha256_file(dst)
        if dst_stat.st_size == src_stat.st_size:
            actual_hash = sha256_file(dst)
            if not expected_sha256 or actual_hash == expected_sha256:
                return "existing_verified", actual_hash
        _unlock_if_exists(dst)
        dst.unlink()

    tmp_dst = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    if tmp_dst.exists():
        _unlock_if_exists(tmp_dst)
        tmp_dst.unlink()

    if link_mode == "hardlink":
        try:
            os.link(src, tmp_dst)
            os.replace(tmp_dst, dst)
            # Do NOT chmod hardlinked file so raw_data_dir source inode ctime/mode stays 100% untouched.
            return "hardlink", expected_sha256 or sha256_file(dst)
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP):
                raise

    digest = hashlib.sha256()
    try:
        with src.open("rb") as reader, tmp_dst.open("wb") as writer:
            while True:
                chunk = reader.read(4 * 1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
                digest.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        actual_sha = digest.hexdigest()
        if expected_sha256 and actual_sha != expected_sha256:
            raise RuntimeError(f"复制分卷 SHA-256 校验不一致 ({src.name}): 实际={actual_sha}, 预期={expected_sha256}")
        os.replace(tmp_dst, dst)
        _protect_readonly(dst, readonly_protect)
        return "copy", actual_sha
    finally:
        if tmp_dst.exists():
            try:
                tmp_dst.unlink()
            except OSError:
                pass


def _ensure_volume_decoded(
    run: RunGroup,
    vol: VolumeInfo,
    config: OrganizerConfig,
    state: StateStore,
    use_nice: bool,
) -> Tuple[Dict[str, Any], bool]:
    """Return cached or freshly decoded part record + (was_cached)."""
    cached = state.get_valid_part(run.key, vol.name, vol.size)
    if cached is not None:
        return cached, True

    scan = scan_dat_volume(vol.path, compute_sha256=True)
    parts_dir = config.resolve_work_dir() / "parts" / run.key
    part_root = parts_dir / f"part_idx{vol.index:03d}_{vol.path.stem}.root"
    backend, _inspection = decode_volume(
        vol_path=vol.path,
        part_root_path=part_root,
        scan=scan,
        config=config,
        use_nice=use_nice,
    )
    state.save_decoded_part(
        run_key=run.key,
        volume_name=vol.name,
        volume_index=vol.index,
        size=vol.size,
        mtime_ns=vol.mtime_ns,
        scan_dict=scan.to_dict(),
        part_root=part_root,
        backend=backend,
    )
    saved = state.get_valid_part(run.key, vol.name, vol.size)
    assert saved is not None
    return saved, False


def watch_once(config: OrganizerConfig, state: StateStore) -> Dict[str, Any]:
    """Scan raw_data_dir once and incrementally decode any newly sealed .dat volumes."""
    raw_dir = config.resolve_raw_dir()
    runs = discover_runs(raw_dir, config=config, finalize=False)
    decoded_now: List[str] = []
    cached_count = 0
    active_count = 0
    errors: List[Dict[str, str]] = []

    for run in runs:
        for vol in run.volumes:
            if not vol.sealed:
                active_count += 1
                continue
            try:
                _record, was_cached = _ensure_volume_decoded(
                    run=run,
                    vol=vol,
                    config=config,
                    state=state,
                    use_nice=True,
                )
                if was_cached:
                    cached_count += 1
                else:
                    decoded_now.append(vol.name)
            except Exception as exc:
                errors.append({"run": run.key, "volume": vol.name, "error": str(exc)})

    return {
        "runs_found": len(runs),
        "decoded_now": decoded_now,
        "cached_sealed": cached_count,
        "active_unsealed": active_count,
        "errors": errors,
    }


def _select_session_dir(
    output_root: Path,
    layout: str,
    min_start_dt: datetime,
    max_end_dt: datetime,
    run_keys: Sequence[str],
    explicit_name: Optional[str],
) -> Path:
    if explicit_name:
        return (output_root / explicit_name).resolve()
    if layout == "group":
        base_name = f"{min_start_dt:%Y%m%d}_{max_end_dt:%m%d}"
    else:
        base_name = f"data_{min_start_dt:%m%d}_{max_end_dt:%m%d}"

    candidate = (output_root / base_name).resolve()
    manifest_file = candidate / "manifest.json"
    if manifest_file.is_file():
        try:
            existing = json.loads(manifest_file.read_text(encoding="utf-8"))
            existing_keys = sorted(r.get("run_key", "") for r in existing.get("runs", []))
            if existing_keys and existing_keys != sorted(run_keys):
                key_hash = hashlib.sha256("|".join(sorted(run_keys)).encode()).hexdigest()[:8]
                alt_name = f"{base_name}_{min_start_dt:%H%M%S}_{key_hash}"
                return (output_root / alt_name).resolve()
        except (OSError, ValueError):
            pass
    return candidate


def finalize_session(
    config: OrganizerConfig,
    state: StateStore,
    session_name: Optional[str] = None,
    run_filter: Optional[Sequence[str]] = None,
    board_filter: Optional[Sequence[int]] = None,
    since_stamp: Optional[str] = None,
    allow_partial: bool = False,
    include_already_finalized: bool = False,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Decode remaining tail volumes, merge per-run ROOTs via hadd, slice slow-control CSVs, and assemble archive."""
    raw_dir = config.resolve_raw_dir()
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"原始数据目录不存在: {raw_dir}")

    runs = discover_runs(raw_dir, config=config, finalize=True)
    if any(not r.all_sealed for r in runs) and config.settle_seconds > 0:
        time.sleep(min(float(config.settle_seconds) + 0.2, 5.0))
        runs = discover_runs(raw_dir, config=config, finalize=True)

    selected_runs: List[RunGroup] = []
    run_filter_set = set(run_filter or [])
    board_filter_set = {int(b) for b in (board_filter or [])}

    for run in runs:
        if board_filter_set and run.board_id not in board_filter_set:
            continue
        if since_stamp and run.start_stamp < since_stamp:
            continue
        if run_filter_set and (run.key not in run_filter_set and f"run{run.run_str}" not in run_filter_set and run.run_str not in run_filter_set):
            continue
        if not include_already_finalized and not run_filter_set and not session_name:
            prior = state.get_finalized_run(run.key)
            if (
                prior is not None
                and int(prior["volume_count"]) == len(run.volumes)
                and Path(prior["merged_root"]).is_file()
            ):
                continue
        selected_runs.append(run)

    if not selected_runs:
        return {
            "status": "nothing_to_finalize",
            "session_dir": None,
            "runs": [],
            "message": "没有待归档的新增采集 Run（若需重新归档已完成 Run，可加 --reprocess 或指定 --session-name）",
        }

    # Pre-check volume completeness and decode all volumes first before creating final output directories.
    prepared_runs: List[Dict[str, Any]] = []
    for run in selected_runs:
        if run.missing_indices and not allow_partial:
            missing_str = ", ".join(f"idx{i:03d}" for i in run.missing_indices)
            raise RuntimeError(
                f"Run {run.key} 缺失分卷 [{missing_str}]；已中止自动归档（确认缺失可加 --allow-partial）"
            )
        unsealed = [v.name for v in run.volumes if not v.sealed]
        if unsealed:
            raise RuntimeError(
                f"Run {run.key} 仍有正在写入的活跃分卷 {unsealed}；请确认 DAQ 已停采后再执行 finalize"
            )
        signals = discover_trigger_signals(raw_dir, run.board_id, run.run_str)
        if any(time.time_ns() - path.stat().st_mtime_ns < config.settle_seconds * 1e9 for path in signals):
            raise RuntimeError(f"Run {run.key} 的 trigger_signal 文件尚未静默，请停采后重试")

        part_records: List[Dict[str, Any]] = []
        cached_parts = 0
        fresh_parts = 0
        for vol in run.volumes:
            if on_progress:on_progress(dict(stage="decode",message=f"解码并校验 {run.string_group} · {vol.name}",group=run.string_group,volume=vol.name))
            rec, was_cached = _ensure_volume_decoded(
                run=run,
                vol=vol,
                config=config,
                state=state,
                use_nice=False,
            )
            part_records.append(rec)
            if was_cached:
                cached_parts += 1
            else:
                fresh_parts += 1

        total_triggers = sum(int(r["triggers"]) for r in part_records)
        total_empty = sum(int(r["empty_triggers"]) for r in part_records)
        total_wave_events = sum(int(r["wave_events"]) for r in part_records)
        total_waves = sum(int(r["waves"]) for r in part_records)

        first_trig: Optional[Tuple[int, int, int]] = None
        last_trig: Optional[Tuple[int, int, int]] = None
        first_wave_trig: Optional[Tuple[int, int, int]] = None
        last_wave_trig: Optional[Tuple[int, int, int]] = None
        channels_union = set()

        for rec in part_records:
            if rec.get("first_trig") and first_trig is None:
                first_trig = tuple(rec["first_trig"])  # type: ignore[assignment]
            if rec.get("last_trig"):
                last_trig = tuple(rec["last_trig"])  # type: ignore[assignment]
            if rec.get("first_wave_trig") and first_wave_trig is None:
                first_wave_trig = tuple(rec["first_wave_trig"])  # type: ignore[assignment]
            if rec.get("last_wave_trig"):
                last_wave_trig = tuple(rec["last_wave_trig"])  # type: ignore[assignment]
            for ch in rec.get("channels") or []:
                channels_union.add(ch)

        start_dt = parse_stamp_beijing(run.start_stamp)
        t0_ns = trigger_tuple_to_ns(first_wave_trig) if first_wave_trig else trigger_tuple_to_ns(first_trig)
        t1_ns = trigger_tuple_to_ns(last_wave_trig) if last_wave_trig else trigger_tuple_to_ns(last_trig)
        span_ns = max(0, (t1_ns - t0_ns)) if (t0_ns is not None and t1_ns is not None) else 0
        end_dt = start_dt + timedelta(microseconds=span_ns / 1000.0)
        range_tag = f"{start_dt:%m%d%H%M}-{end_dt:%m%d%H%M}"

        prepared_runs.append(
            {
                "run": run,
                "part_records": part_records,
                "cached_parts": cached_parts,
                "fresh_parts": fresh_parts,
                "total_triggers": total_triggers,
                "total_empty_triggers": total_empty,
                "total_wave_events": total_wave_events,
                "total_waves": total_waves,
                "first_trig": first_trig,
                "last_trig": last_trig,
                "first_wave_trig": first_wave_trig,
                "last_wave_trig": last_wave_trig,
                "channels": sorted(channels_union, key=lambda c: tuple(int(x) for x in c.split(":"))),
                "start_dt": start_dt,
                "end_dt": end_dt,
                "span_seconds": round(span_ns / 1e9, 6),
                "range_tag": range_tag,
                "trigger_signals": signals,
            }
        )

    min_start_dt = min(item["start_dt"] for item in prepared_runs)
    max_end_dt = max(item["end_dt"] for item in prepared_runs)
    output_root = config.resolve_output_root()
    output_root.mkdir(parents=True, exist_ok=True)
    session_dir = _select_session_dir(
        output_root=output_root,
        layout=config.layout,
        min_start_dt=min_start_dt,
        max_end_dt=max_end_dt,
        run_keys=[item["run"].key for item in prepared_runs],
        explicit_name=session_name,
    )
    session_dir.mkdir(parents=True, exist_ok=True)

    work_merge_dir = config.resolve_work_dir() / "merge"
    work_merge_dir.mkdir(parents=True, exist_ok=True)

    checksum_entries: List[Tuple[str, str]] = []
    manifest_runs: List[Dict[str, Any]] = []
    groups_summary: Dict[str, Dict[str, Any]] = {}
    name_counts = Counter((item["run"].board_id, item["range_tag"]) for item in prepared_runs)

    for item in prepared_runs:
        run: RunGroup = item["run"]
        part_records = item["part_records"]
        range_tag = item["range_tag"]
        file_tag = range_tag + (f"_run{run.run_str}" if name_counts[(run.board_id, range_tag)] > 1 else "")

        if config.layout == "group":
            group_root = session_dir / run.string_group
            raw_target_dir = group_root / "raw_data"
            rec_target_dir = group_root / "rec_data"
            csv_target_dir = group_root / "csv"
            for subdir in (raw_target_dir, rec_target_dir, csv_target_dir):
                subdir.mkdir(parents=True, exist_ok=True)
        else:
            raw_target_dir = session_dir
            rec_target_dir = session_dir
            csv_target_dir = session_dir

        placed_raw: List[Dict[str, Any]] = []
        for vol, rec in zip(run.volumes, part_records):
            if on_progress:on_progress(dict(stage="copy",message=f"复制并核对原始卷 {run.string_group} · {vol.name}",group=run.string_group,volume=vol.name))
            dst_dat = raw_target_dir / vol.name
            mode_used, actual_sha = _place_raw_dat(
                src=vol.path,
                dst=dst_dat,
                link_mode=config.link_mode,
                expected_sha256=str(rec["sha256"]),
                readonly_protect=config.readonly_protect,
            )
            rel_dat = dst_dat.relative_to(session_dir).as_posix()
            checksum_entries.append((rel_dat, actual_sha))
            placed_raw.append(
                {
                    "name": vol.name,
                    "relative_path": rel_dat,
                    "index": vol.index,
                    "size": vol.size,
                    "sha256": actual_sha,
                    "placement": mode_used,
                    "triggers": int(rec["triggers"]),
                    "wave_events": int(rec["wave_events"]),
                    "waves": int(rec["waves"]),
                }
            )

        merged_root_name = f"data_fee_{run.board_id}_{file_tag}.root"
        placed_signals = []
        for signal_path in item["trigger_signals"]:
            if on_progress:on_progress(dict(stage="copy",message=f"保存触发记录 {run.string_group} · {signal_path.name}",group=run.string_group))
            expected_sha = sha256_file(signal_path)
            destination = raw_target_dir / signal_path.name
            mode_used, actual_sha = _place_raw_dat(signal_path, destination, "copy", expected_sha, config.readonly_protect)
            relative = destination.relative_to(session_dir).as_posix()
            checksum_entries.append((relative, actual_sha))
            placed_signals.append(dict(name=signal_path.name,relative_path=relative,size=destination.stat().st_size,sha256=actual_sha,placement=mode_used))
        merged_root_path = rec_target_dir / merged_root_name
        _unlock_if_exists(merged_root_path)
        part_paths = [Path(r["part_root"]) for r in part_records]
        if on_progress:on_progress(dict(stage="merge",message=f"合并 {run.string_group} ROOT 并核对事件数",group=run.string_group))
        root_inspection: RootInspection = merge_part_roots(
            part_roots=part_paths,
            output_root_path=merged_root_path,
            expected_entries=item["total_wave_events"],
            expected_first_trigger=item["first_wave_trig"],
            expected_last_trigger=item["last_wave_trig"],
            config=config,
            temp_dir=work_merge_dir,
            use_nice=False,
        )
        root_sha = sha256_file(merged_root_path)
        _protect_readonly(merged_root_path, config.readonly_protect)
        rel_root = merged_root_path.relative_to(session_dir).as_posix()
        checksum_entries.append((rel_root, root_sha))

        csv_name = f"readback_fee_{run.board_id}_{file_tag}.csv"
        csv_target_path = csv_target_dir / csv_name
        _unlock_if_exists(csv_target_path)
        start_ms = int(round(item["start_dt"].timestamp() * 1000.0))
        end_ms = int(round(item["end_dt"].timestamp() * 1000.0))
        first_trig_sec = item["first_trig"][1] if item["first_trig"] else None
        last_trig_sec = item["last_trig"][1] if item["last_trig"] else None

        if on_progress:on_progress(dict(stage="csv",message=f"匹配并保存 {run.string_group} 慢控制 CSV",group=run.string_group))
        csv_result: CsvSliceResult = slice_csv_for_run(
            csv_dir=config.resolve_csv_dir(),
            board_id=run.board_id,
            start_ms=start_ms,
            end_ms=end_ms,
            output_csv_path=csv_target_path,
            margin_seconds=config.csv_margin_seconds,
            wait_seconds=config.csv_sync_wait_seconds,
            dat_first_trig_sec=first_trig_sec,
            dat_last_trig_sec=last_trig_sec,
        )
        rel_csv: Optional[str] = None
        if csv_result.output_path:
            out_csv = Path(csv_result.output_path)
            _protect_readonly(out_csv, config.readonly_protect)
            rel_csv = out_csv.relative_to(session_dir).as_posix()
            checksum_entries.append((rel_csv, csv_result.sha256))

        run_summary = {
            "run_key": run.key,
            "string_group": run.string_group,
            "board_id": run.board_id,
            "run_str": run.run_str,
            "start_beijing": item["start_dt"].isoformat(),
            "end_beijing": item["end_dt"].isoformat(),
            "span_seconds": item["span_seconds"],
            "range_tag": range_tag,
            "volume_count": len(run.volumes),
            "missing_indices": run.missing_indices,
            "cached_volumes_reused": item["cached_parts"],
            "freshly_decoded_volumes": item["fresh_parts"],
            "raw_bytes": run.total_bytes,
            "triggers": item["total_triggers"],
            "empty_triggers": item["total_empty_triggers"],
            "wave_events": item["total_wave_events"],
            "waves": item["total_waves"],
            "channels": item["channels"],
            "first_wave_trigger": list(item["first_wave_trig"]) if item["first_wave_trig"] else None,
            "last_wave_trigger": list(item["last_wave_trig"]) if item["last_wave_trig"] else None,
            "rec_root": {
                "relative_path": rel_root,
                "size": merged_root_path.stat().st_size,
                "sha256": root_sha,
                "entries": root_inspection.entries,
            },
            "csv": {
                "relative_path": rel_csv,
                "rows": csv_result.rows_written,
                "size": csv_result.bytes_written,
                "sha256": csv_result.sha256,
                "first_timestamp_ms": csv_result.first_timestamp_ms,
                "last_timestamp_ms": csv_result.last_timestamp_ms,
                "first_fpga_seconds": csv_result.first_fpga_seconds,
                "last_fpga_seconds": csv_result.last_fpga_seconds,
                "fpga_clock_consistent": csv_result.fpga_clock_consistent,
                "sync_pending_bytes": csv_result.sync_pending_bytes,
                "note": csv_result.note,
            },
            "raw_volumes": placed_raw,
            "trigger_signal_files": placed_signals,
        }
        manifest_runs.append(run_summary)

        grp = groups_summary.setdefault(
            run.string_group,
            {
                "string_group": run.string_group,
                "boards": [],
                "runs": 0,
                "volumes": 0,
                "wave_events": 0,
                "waves": 0,
                "csv_rows": 0,
            },
        )
        if run.board_id not in grp["boards"]:
            grp["boards"].append(run.board_id)
        grp["runs"] += 1
        grp["volumes"] += len(run.volumes)
        grp["wave_events"] += item["total_wave_events"]
        grp["waves"] += item["total_waves"]
        grp["csv_rows"] += csv_result.rows_written

        state.save_finalized_run(
            run_key=run.key,
            session_dir=session_dir,
            string_group=run.string_group,
            board_id=run.board_id,
            merged_root=merged_root_path,
            csv_path=Path(csv_result.output_path) if csv_result.output_path else None,
            entries=root_inspection.entries,
            waves=item["total_waves"],
            volume_count=len(run.volumes),
            summary=run_summary,
        )

    checksum_entries.sort(key=lambda pair: pair[0])
    if on_progress:on_progress(dict(stage="manifest",message="生成批次清单与 SHA-256 文件"))
    sha_file = session_dir / "SHA256SUMS.txt"
    _unlock_if_exists(sha_file)
    sha_tmp = session_dir / f".SHA256SUMS.{os.getpid()}.tmp"
    sha_text = "".join(f"{digest}  {rel_path}\n" for rel_path, digest in checksum_entries)
    sha_tmp.write_text(sha_text, encoding="utf-8")
    os.replace(sha_tmp, sha_file)
    _protect_readonly(sha_file, config.readonly_protect)

    manifest = {
        "schema_version": "seastar-archive-v2",
        "organizer_version": VERSION,
        "generated_at_beijing": datetime.now(BEIJING_TZ).isoformat(),
        "session_name": session_dir.name,
        "layout": config.layout,
        "board_map": config.board_map,
        "groups": [groups_summary[k] for k in sorted(groups_summary.keys(), key=lambda g: (not g.startswith("G"), g))],
        "runs": manifest_runs,
    }
    manifest_file = session_dir / "manifest.json"
    _unlock_if_exists(manifest_file)
    manifest_tmp = session_dir / f".manifest.{os.getpid()}.tmp.json"
    manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(manifest_tmp, manifest_file)
    _protect_readonly(manifest_file, config.readonly_protect)

    return {
        "status": "ok",
        "session_dir": str(session_dir),
        "layout": config.layout,
        "groups": manifest["groups"],
        "runs": manifest_runs,
    }


def _audit_csv_file(path: Path) -> Dict[str, Any]:
    rows = 0
    header: Optional[str] = None
    first_ts: Optional[int] = None
    last_ts: Optional[int] = None
    incomplete_tail = 0
    ts_col = 0
    with path.open("rb") as stream:
        first_line = True
        for raw_line in stream:
            if not raw_line.endswith(b"\n"):
                incomplete_tail += 1
                continue
            text = raw_line.decode("utf-8", errors="replace").strip("\r\n")
            if not text:
                continue
            if first_line:
                first_line = False
                cols = [c.strip() for c in text.split(",")]
                if "timestamp_ms" in cols or not cols[0].lstrip("-").isdigit():
                    header = text
                    if "timestamp_ms" in cols:
                        ts_col = cols.index("timestamp_ms")
                    continue
            rows += 1
            parts = text.split(",")
            if len(parts) > ts_col:
                try:
                    ts = int(float(parts[ts_col]))
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                except ValueError:
                    pass
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "rows": rows,
        "has_header": header is not None,
        "first_timestamp_ms": first_ts,
        "last_timestamp_ms": last_ts,
        "incomplete_tail_lines": incomplete_tail,
    }


def verify_dataset(
    dataset_dir: Path,
    root_bin: str = "root",
    board_map: Optional[Dict[str, str]] = None,
    check_sha256: bool = True,
    work_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read-only integrity audit of any organized dataset directory (group G0..G6 or legacy flat)."""
    target = Path(dataset_dir).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"数据集目录不存在: {target}")

    mapping = {str(i): f"G{i}" for i in range(7)}
    if board_map:
        mapping.update({str(k): str(v) for k, v in board_map.items()})

    group_subdirs = sorted(
        [p for p in target.iterdir() if p.is_dir() and STRING_DIR_RE.match(p.name)],
        key=lambda p: int(STRING_DIR_RE.match(p.name).group(1)),  # type: ignore[union-attr]
    )
    layout = "group" if group_subdirs else "flat"

    errors: List[str] = []
    warnings: List[str] = []
    units: Dict[str, Dict[str, Any]] = {}

    def unit_for(group_name: str, board_id: Optional[int]) -> Dict[str, Any]:
        label = group_name or (mapping.get(str(board_id), f"FEE{board_id}") if board_id is not None else "UNKNOWN")
        return units.setdefault(
            label,
            {
                "group": label,
                "board_ids": [],
                "dat_files": [],
                "trigger_signal_files": [],
                "root_files": [],
                "csv_files": [],
                "clean_dat_wave_events": 0,
                "clean_dat_waves": 0,
                "corrupted_dat_files": [],
                "root_total_entries": 0,
            },
        )

    # ROOT launched through a sandbox/container may not share Python's /tmp.
    scratch_parent = Path(work_dir).resolve() if work_dir else target.parent
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="seastar_verify_", dir=str(scratch_parent)) as scratch_tmp:
        scratch_path = Path(scratch_tmp)
        for path in sorted(target.rglob("*"), key=lambda p: p.as_posix()):
            if not path.is_file():
                continue
            rel = path.relative_to(target)
            if any(part.startswith(".") or part == "results" for part in rel.parts):
                continue

            explicit_grp = ""
            for part in rel.parts[:-1]:
                m_grp = STRING_DIR_RE.match(part)
                if m_grp:
                    explicit_grp = f"G{int(m_grp.group(1))}"
                    break

            suffix = path.suffix.lower()
            signal_match = TRIGGER_SIGNAL_RE.match(path.name)
            if signal_match:
                board_id = int(signal_match.group(1))
                unit_for(explicit_grp, board_id)["trigger_signal_files"].append(dict(relative_path=rel.as_posix(),size=path.stat().st_size))
                continue  # Opaque trigger sidecars use SHA-256 validation, not waveform-frame parsing.
            if suffix == ".dat":
                m_dat = DAT_VOLUME_RE.match(path.name)
                b_id = int(m_dat.group(1)) if m_dat else None
                u = unit_for(explicit_grp, b_id)
                if b_id is not None and b_id not in u["board_ids"]:
                    u["board_ids"].append(b_id)
                scan = scan_dat_volume(path, compute_sha256=check_sha256)
                entry = scan.to_dict()
                entry["relative_path"] = rel.as_posix()
                entry["index"] = int(m_dat.group(3)) if m_dat else None
                u["dat_files"].append(entry)
                if scan.is_gzip:
                    msg = f"{rel.as_posix()}: 文件头为 gzip (1f8b08)，原始 .dat 已被压缩包误覆盖"
                    errors.append(msg)
                    u["corrupted_dat_files"].append(rel.as_posix())
                elif not scan.clean:
                    msg = (
                        f"{rel.as_posix()}: 存在帧异常 "
                        f"(bad_bytes={scan.bad_bytes}, orphans={scan.orphans}, pending_bytes={scan.pending_bytes})"
                    )
                    errors.append(msg)
                    u["corrupted_dat_files"].append(rel.as_posix())
                else:
                    u["clean_dat_wave_events"] += scan.wave_events
                    u["clean_dat_waves"] += scan.waves

            elif suffix == ".root":
                m_root = ROOT_FEE_RE.match(path.name)
                b_id = int(m_root.group(1)) if m_root else None
                u = unit_for(explicit_grp, b_id)
                if b_id is not None and b_id not in u["board_ids"]:
                    u["board_ids"].append(b_id)
                insp = inspect_root_file(path, root_bin=root_bin, work_dir=scratch_path)
                r_dict = insp.to_dict()
                r_dict["relative_path"] = rel.as_posix()
                r_dict["size"] = path.stat().st_size
                u["root_files"].append(r_dict)
                if not insp.valid:
                    errors.append(f"{rel.as_posix()}: ROOT 校验未通过 ({insp.error})")
                else:
                    u["root_total_entries"] += insp.entries

            elif suffix == ".csv":
                m_csv = CSV_FEE_RE.match(path.name)
                b_id = int(m_csv.group(1)) if m_csv else None
                u = unit_for(explicit_grp, b_id)
                if b_id is not None and b_id not in u["board_ids"]:
                    u["board_ids"].append(b_id)
                c_dict = _audit_csv_file(path)
                c_dict["relative_path"] = rel.as_posix()
                u["csv_files"].append(c_dict)
                if c_dict["incomplete_tail_lines"] > 0:
                    warnings.append(f"{rel.as_posix()}: 末尾有 {c_dict['incomplete_tail_lines']} 行未换行残缺记录")
                if c_dict["rows"] == 0:
                    warnings.append(f"{rel.as_posix()}: CSV 有效数据行数为 0")

    for label, u in sorted(units.items()):
        indices = sorted(d["index"] for d in u["dat_files"] if d.get("index") is not None)
        if indices:
            expected_indices = list(range(1, max(indices) + 1))
            missing = sorted(set(expected_indices) - set(indices))
            if missing:
                errors.append(f"{label}: 缺失 .dat 分卷序号 {[f'idx{i:03d}' for i in missing]}")
        if not u["root_files"]:
            warnings.append(f"{label}: 未找到 decode 后的 .root 文件")
        elif u["dat_files"]:
            if any(not r["valid"] for r in u["root_files"]):
                warnings.append(f"{label}: ROOT 未全部读取，暂不比较事件总数；不能将未读取计为 0 事件")
            elif not u["corrupted_dat_files"]:
                if u["root_total_entries"] != u["clean_dat_wave_events"]:
                    errors.append(
                        f"{label}: ROOT 总事件数 ({u['root_total_entries']}) != .dat 带波形触发总数 ({u['clean_dat_wave_events']})"
                    )
            else:
                diff = u["root_total_entries"] - u["clean_dat_wave_events"]
                warnings.append(
                    f"{label}: 完好分卷共 {u['clean_dat_wave_events']} 事件，ROOT 共 {u['root_total_entries']} 事件 "
                    f"（差值 {diff} 事件来自已损坏分卷 {u['corrupted_dat_files']} 损坏前的完整转换）"
                )
        if not u["csv_files"]:
            warnings.append(f"{label}: 目录内未包含慢控制 .csv 文件")

    sha_sums = target / "SHA256SUMS.txt"
    if check_sha256 and sha_sums.is_file():
        for line in sha_sums.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("  ", 1)
            if len(parts) != 2:
                continue
            expected_hash, rel_str = parts
            check_target = (target / rel_str).resolve()
            if not check_target.is_file():
                errors.append(f"SHA256SUMS.txt 记录的文件缺失: {rel_str}")
            else:
                actual_hash = sha256_file(check_target)
                if actual_hash != expected_hash:
                    errors.append(f"SHA256 校验失败: {rel_str} (预期 {expected_hash[:12]}..., 实际 {actual_hash[:12]}...)")

    return {
        "ok": len(errors) == 0,
        "dataset_dir": str(target),
        "layout": layout,
        "has_manifest": (target / "manifest.json").is_file(),
        "has_sha256sums": sha_sums.is_file(),
        "errors": errors,
        "warnings": warnings,
        "groups": [units[k] for k in sorted(units.keys())],
    }
