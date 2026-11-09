"""Slow-control CSV discovery, fee-monitor sync watermark check, time-window slicing, and FPGA clock cross-check."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CsvSliceResult:
    board_id: int
    source_files: List[str]
    output_path: Optional[str]
    rows_written: int
    bytes_written: int
    sha256: str
    window_start_ms: int
    window_end_ms: int
    first_timestamp_ms: Optional[int]
    last_timestamp_ms: Optional[int]
    first_fpga_seconds: Optional[int]
    last_fpga_seconds: Optional[int]
    sync_pending_bytes: int
    incomplete_tail_lines: int
    fpga_clock_consistent: Optional[bool]
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _fee_csv_pattern(board_id: int) -> re.Pattern:
    return re.compile(rf"^readback_fee_{int(board_id)}(?:_.*)?\.csv$", re.IGNORECASE)


def _read_sync_pending_for_board(csv_root: Path, board_id: int) -> int:
    pattern = _fee_csv_pattern(board_id)
    progress_files: List[Path] = []
    direct = csv_root / ".sync" / "progress.json"
    if direct.is_file():
        progress_files.append(direct)
    if csv_root.is_dir():
        for child in csv_root.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                candidate = child / ".sync" / "progress.json"
                if candidate.is_file():
                    progress_files.append(candidate)
    pending = 0
    for pfile in progress_files:
        try:
            state = json.loads(pfile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for name, record in (state.get("files") or {}).items():
            if record.get("missing"):
                continue
            if pattern.match(Path(name).name):
                size = int(record.get("size", 0))
                offset = int(record.get("offset", 0))
                pending += max(0, size - offset)
    return pending


def find_fee_csv_sources(
    csv_dir: Path,
    board_id: int,
    wait_seconds: float = 0.0,
) -> Tuple[List[Path], int]:
    """Find mirrored or local readback_fee_<board>.csv files, waiting briefly if fee-monitor is catching up."""
    root = Path(csv_dir).expanduser().resolve()
    if root.is_file():
        if _fee_csv_pattern(board_id).match(root.name):
            return [root], 0
        return [], 0
    if not root.is_dir():
        return [], 0

    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    pending = _read_sync_pending_for_board(root, board_id)
    while pending > 0 and time.monotonic() < deadline:
        time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
        pending = _read_sync_pending_for_board(root, board_id)

    pattern = _fee_csv_pattern(board_id)
    matches: List[Path] = []
    for path in root.rglob("*.csv"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if pattern.match(path.name):
            matches.append(path.resolve())
    matches.sort(key=lambda p: (p.stat().st_mtime_ns if p.exists() else 0, str(p)))
    return matches, pending


def slice_csv_for_run(
    csv_dir: Path,
    board_id: int,
    start_ms: int,
    end_ms: int,
    output_csv_path: Path,
    margin_seconds: float = 60.0,
    wait_seconds: float = 0.0,
    dat_first_trig_sec: Optional[int] = None,
    dat_last_trig_sec: Optional[int] = None,
) -> CsvSliceResult:
    """Slice rows within [start_ms - margin, end_ms + margin] cleanly without partial trailing lines."""
    margin_ms = int(round(max(0.0, float(margin_seconds)) * 1000.0))
    win_lo = int(start_ms) - margin_ms
    win_hi = int(end_ms) + margin_ms

    sources, pending_bytes = find_fee_csv_sources(csv_dir, board_id, wait_seconds=wait_seconds)
    if not sources:
        return CsvSliceResult(
            board_id=board_id,
            source_files=[],
            output_path=None,
            rows_written=0,
            bytes_written=0,
            sha256="",
            window_start_ms=win_lo,
            window_end_ms=win_hi,
            first_timestamp_ms=None,
            last_timestamp_ms=None,
            first_fpga_seconds=None,
            last_fpga_seconds=None,
            sync_pending_bytes=pending_bytes,
            incomplete_tail_lines=0,
            fpga_clock_consistent=None,
            note=f"在 {csv_dir} 中未找到 fee_{board_id} 的慢控制 CSV 文件",
        )

    header_line: Optional[str] = None
    ts_col = 0
    fpga_sec_col: Optional[int] = None
    collected: List[Tuple[int, str, Optional[int]]] = []
    seen_rows = set()
    incomplete_tails = 0

    for src in sources:
        try:
            with src.open("rb") as stream:
                first_nonempty = True
                for raw_line in stream:
                    if not raw_line.endswith(b"\n"):
                        incomplete_tails += 1
                        continue
                    text = raw_line.decode("utf-8", errors="replace").strip("\r\n")
                    if not text.strip():
                        continue
                    if first_nonempty:
                        first_nonempty = False
                        cols = [c.strip() for c in text.split(",")]
                        if "timestamp_ms" in cols or not cols[0].lstrip("-").isdigit():
                            if header_line is None:
                                header_line = text
                                if "timestamp_ms" in cols:
                                    ts_col = cols.index("timestamp_ms")
                                if "fpga_timestamp_seconds" in cols:
                                    fpga_sec_col = cols.index("fpga_timestamp_seconds")
                            continue
                    parts = text.split(",")
                    if len(parts) <= ts_col:
                        continue
                    try:
                        ts_val = int(float(parts[ts_col]))
                    except ValueError:
                        continue
                    if ts_val < win_lo or ts_val > win_hi:
                        continue
                    dedup_key = (ts_val, text)
                    if dedup_key in seen_rows:
                        continue
                    seen_rows.add(dedup_key)
                    fpga_sec: Optional[int] = None
                    if fpga_sec_col is not None and fpga_sec_col < len(parts):
                        try:
                            fpga_sec = int(float(parts[fpga_sec_col]))
                        except ValueError:
                            fpga_sec = None
                    collected.append((ts_val, text, fpga_sec))
        except OSError:
            continue

    collected.sort(key=lambda item: item[0])
    if not collected:
        return CsvSliceResult(
            board_id=board_id,
            source_files=[str(p) for p in sources],
            output_path=None,
            rows_written=0,
            bytes_written=0,
            sha256="",
            window_start_ms=win_lo,
            window_end_ms=win_hi,
            first_timestamp_ms=None,
            last_timestamp_ms=None,
            first_fpga_seconds=None,
            last_fpga_seconds=None,
            sync_pending_bytes=pending_bytes,
            incomplete_tail_lines=incomplete_tails,
            fpga_clock_consistent=None,
            note="已扫描慢控制 CSV，但没有落在当前采集时间窗内的记录",
        )

    out_path = Path(output_csv_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f".{out_path.stem}.{os.getpid()}.tmp.csv")
    digest = hashlib.sha256()
    bytes_written = 0

    try:
        with tmp_path.open("wb") as out_stream:
            if header_line is not None:
                head_bytes = (header_line + "\n").encode("utf-8")
                out_stream.write(head_bytes)
                digest.update(head_bytes)
                bytes_written += len(head_bytes)
            for _ts, line_text, _fsec in collected:
                row_bytes = (line_text + "\n").encode("utf-8")
                out_stream.write(row_bytes)
                digest.update(row_bytes)
                bytes_written += len(row_bytes)
            out_stream.flush()
            os.fsync(out_stream.fileno())
        os.replace(tmp_path, out_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    fpga_vals = [item[2] for item in collected if item[2] is not None]
    first_fpga = fpga_vals[0] if fpga_vals else None
    last_fpga = fpga_vals[-1] if fpga_vals else None
    clock_ok: Optional[bool] = None
    note = ""
    if (
        first_fpga is not None
        and last_fpga is not None
        and dat_first_trig_sec is not None
        and dat_last_trig_sec is not None
    ):
        tol = max(120, int(margin_seconds) * 2)
        lo_a, hi_a = min(first_fpga, last_fpga), max(first_fpga, last_fpga)
        lo_b, hi_b = min(dat_first_trig_sec, dat_last_trig_sec), max(dat_first_trig_sec, dat_last_trig_sec)
        clock_ok = (lo_a <= hi_b + tol) and (lo_b <= hi_a + tol)
        if not clock_ok:
            note = (
                f"注意：CSV fpga_timestamp_seconds ({first_fpga}..{last_fpga}) "
                f"与 DAT trigger_second ({dat_first_trig_sec}..{dat_last_trig_sec}) 区间不一致"
            )
    if pending_bytes > 0:
        extra = f"fee-monitor 镜像仍有 {pending_bytes} B 待同步"
        note = f"{note}；{extra}" if note else extra

    return CsvSliceResult(
        board_id=board_id,
        source_files=[str(p) for p in sources],
        output_path=str(out_path),
        rows_written=len(collected),
        bytes_written=bytes_written,
        sha256=digest.hexdigest(),
        window_start_ms=win_lo,
        window_end_ms=win_hi,
        first_timestamp_ms=collected[0][0],
        last_timestamp_ms=collected[-1][0],
        first_fpga_seconds=first_fpga,
        last_fpga_seconds=last_fpga,
        sync_pending_bytes=pending_bytes,
        incomplete_tail_lines=incomplete_tails,
        fpga_clock_consistent=clock_ok,
        note=note,
    )
