"""Read-only DAQ .dat volume discovery, sealed-volume detection, and single-pass frame scanning."""
from dataclasses import asdict, dataclass, field
import hashlib
from pathlib import Path
import re
import struct
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import OrganizerConfig

DAT_VOLUME_RE = re.compile(r"^data_fee_(\d+)_run(\d+)_idx(\d+)_(\d{14})\.dat$", re.IGNORECASE)
TRIGGER_SIGNAL_RE = re.compile(r"^trigger_signal_fee_(\d+)_run(\d+)_idx(\d+)_(\d{14})\.dat$", re.IGNORECASE)
GZIP_MAGIC = b"\x1f\x8b\x08"
CHUNK_BYTES = 4 * 1024 * 1024


def discover_trigger_signals(raw_dir: Path, board_id: int, run_str: str) -> List[Path]:
    result = []
    for path in Path(raw_dir).iterdir():
        match = TRIGGER_SIGNAL_RE.match(path.name)
        if path.is_file() and match and int(match.group(1)) == board_id and match.group(2) == run_str:
            result.append(path)
    return sorted(result)


@dataclass
class VolumeInfo:
    path: Path
    name: str
    board_id: int
    run_str: str
    run_id: int
    index: int
    stamp: str
    size: int
    mtime_ns: int
    sealed: bool = False
    seal_reason: str = ""


@dataclass
class RunGroup:
    key: str
    board_id: int
    string_group: str
    run_str: str
    run_id: int
    start_stamp: str
    volumes: List[VolumeInfo] = field(default_factory=list)
    missing_indices: List[int] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(v.size for v in self.volumes)

    @property
    def all_sealed(self) -> bool:
        return bool(self.volumes) and all(v.sealed for v in self.volumes)


@dataclass
class VolumeScan:
    name: str
    size: int
    sha256: str
    is_gzip: bool
    triggers: int
    empty_triggers: int
    wave_events: int
    waves: int
    bad_bytes: int
    resyncs: int
    orphans: int
    pending_bytes: int
    first_trigger: Optional[Tuple[int, int, int]]
    last_trigger: Optional[Tuple[int, int, int]]
    first_wave_trigger: Optional[Tuple[int, int, int]]
    last_wave_trigger: Optional[Tuple[int, int, int]]
    channels: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def clean(self) -> bool:
        return not self.is_gzip and self.bad_bytes == 0 and self.orphans == 0 and self.pending_bytes == 0


def discover_runs(
    raw_dir: Path,
    config: OrganizerConfig,
    finalize: bool = False,
    now_ns: Optional[int] = None,
) -> List[RunGroup]:
    """Scan raw_dir (flat or nested) for DAQ .dat volumes and determine sealed status."""
    raw_root = Path(raw_dir).expanduser().resolve()
    if not raw_root.is_dir():
        return []
    current_ns = time.time_ns() if now_ns is None else int(now_ns)
    settle_ns = int(max(0.0, float(config.settle_seconds)) * 1_000_000_000)

    grouped: Dict[Tuple[int, str], List[VolumeInfo]] = {}
    for entry in sorted(raw_root.iterdir(), key=lambda p: p.name):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        match = DAT_VOLUME_RE.match(entry.name)
        if not match:
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        board_id = int(match.group(1))
        run_str = match.group(2)
        run_id = int(run_str)
        idx = int(match.group(3))
        stamp = match.group(4)
        vol = VolumeInfo(
            path=entry.resolve(),
            name=entry.name,
            board_id=board_id,
            run_str=run_str,
            run_id=run_id,
            index=idx,
            stamp=stamp,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
        )
        grouped.setdefault((board_id, run_str), []).append(vol)

    runs_by_board: Dict[int, List[RunGroup]] = {}
    all_runs: List[RunGroup] = []
    for (board_id, run_str), vols in grouped.items():
        vols.sort(key=lambda v: (v.index, v.stamp, v.name))
        start_stamp = vols[0].stamp
        key = f"data_fee_{board_id}_run{run_str}_{start_stamp}"
        max_idx = max(v.index for v in vols)
        present = {v.index for v in vols}
        missing = [i for i in range(1, max_idx + 1) if i not in present]
        run = RunGroup(
            key=key,
            board_id=board_id,
            string_group=config.string_for_board(board_id),
            run_str=run_str,
            run_id=int(run_str),
            start_stamp=start_stamp,
            volumes=vols,
            missing_indices=missing,
        )
        runs_by_board.setdefault(board_id, []).append(run)
        all_runs.append(run)

    for board_id, board_runs in runs_by_board.items():
        board_runs.sort(key=lambda r: (r.start_stamp, r.run_id, r.key))
        for r_pos, run in enumerate(board_runs):
            has_newer_run = r_pos + 1 < len(board_runs)
            max_idx = max(v.index for v in run.volumes)
            for vol in run.volumes:
                quiet = (current_ns - vol.mtime_ns) >= settle_ns
                if vol.index < max_idx:
                    vol.sealed = quiet
                    vol.seal_reason = "next_volume" if quiet else "settling_next_volume"
                elif has_newer_run:
                    vol.sealed = quiet
                    vol.seal_reason = "newer_run" if quiet else "settling_newer_run"
                elif finalize:
                    vol.sealed = quiet
                    vol.seal_reason = "finalize_tail" if quiet else "active_tail_settling"
                else:
                    vol.sealed = False
                    vol.seal_reason = "active_tail"

    all_runs.sort(key=lambda r: (r.board_id, r.start_stamp, r.run_id))
    return all_runs


def scan_dat_volume(path: Path, compute_sha256: bool = True) -> VolumeScan:
    """Single-pass streaming binary scanner matching DAQ framing and ROOT seastar event rules."""
    file_path = Path(path)
    stat_size = file_path.stat().st_size
    digest = hashlib.sha256() if compute_sha256 else None

    is_gzip = False
    checked_magic = False
    triggers = 0
    empty_triggers = 0
    wave_events = 0
    waves = 0
    bad_bytes = 0
    resyncs = 0
    orphans = 0

    first_trigger: Optional[Tuple[int, int, int]] = None
    last_trigger: Optional[Tuple[int, int, int]] = None
    first_wave_trigger: Optional[Tuple[int, int, int]] = None
    last_wave_trigger: Optional[Tuple[int, int, int]] = None
    channels: Set[str] = set()

    open_trigger: Optional[Tuple[int, int, int]] = None
    open_waves = 0
    recovering = False
    buf = bytearray()

    def flush_trigger() -> None:
        nonlocal open_trigger, open_waves, wave_events, empty_triggers, first_wave_trigger, last_wave_trigger
        if open_trigger is None:
            return
        if open_waves > 0:
            wave_events += 1
            if first_wave_trigger is None:
                first_wave_trigger = open_trigger
            last_wave_trigger = open_trigger
        else:
            empty_triggers += 1
        open_trigger = None
        open_waves = 0

    with file_path.open("rb") as stream:
        while True:
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            if digest is not None:
                digest.update(chunk)
            if not checked_magic:
                checked_magic = True
                if chunk[:3] == GZIP_MAGIC:
                    is_gzip = True
            if is_gzip:
                continue

            buf.extend(chunk)
            offset = 0
            length = len(buf)
            while length - offset >= 16:
                b0 = buf[offset]
                if b0 == 0xFF and buf[offset + 1] == 0x12 and buf[offset + 4:offset + 8] == b"\x00\x00\x00\x00":
                    nano = struct.unpack_from(">I", buf, offset + 12)[0]
                    if nano < 1_000_000_000:
                        flush_trigger()
                        number = struct.unpack_from(">H", buf, offset + 2)[0]
                        second = struct.unpack_from(">I", buf, offset + 8)[0]
                        trig = (int(number), int(second), int(nano))
                        triggers += 1
                        if first_trigger is None:
                            first_trigger = trig
                        last_trigger = trig
                        open_trigger = trig
                        open_waves = 0
                        recovering = False
                        offset += 16
                        continue

                if b0 == 0xFE and not recovering and buf[offset + 3] == 0:
                    second, nano, points = struct.unpack_from(">III", buf, offset + 4)
                    if nano < 1_000_000_000 and 1 <= points <= 8192:
                        frame_size = 16 + 2 * points
                        if length - offset < frame_size:
                            break
                        b1 = buf[offset + 1]
                        channels.add(f"{b1 >> 4}:{b1 & 0x0F}")
                        if open_trigger is not None:
                            open_waves += 1
                            waves += 1
                        else:
                            orphans += 1
                        offset += frame_size
                        continue

                if not recovering:
                    resyncs += 1
                    recovering = True
                next_pos = buf.find(b"\xff\x12", offset + 1)
                if next_pos == -1:
                    safe_skip = max(offset + 1, length - 15)
                    bad_bytes += safe_skip - offset
                    offset = safe_skip
                    break
                bad_bytes += next_pos - offset
                offset = next_pos

            if offset > 0:
                del buf[:offset]

    if is_gzip:
        return VolumeScan(
            name=file_path.name,
            size=stat_size,
            sha256=digest.hexdigest() if digest is not None else "",
            is_gzip=True,
            triggers=0,
            empty_triggers=0,
            wave_events=0,
            waves=0,
            bad_bytes=stat_size,
            resyncs=0,
            orphans=0,
            pending_bytes=0,
            first_trigger=None,
            last_trigger=None,
            first_wave_trigger=None,
            last_wave_trigger=None,
            channels=[],
        )

    flush_trigger()
    trailing = len(buf)
    if recovering:
        bad_bytes += trailing
        pending_bytes = 0
    else:
        pending_bytes = trailing

    sorted_channels = sorted(
        channels,
        key=lambda item: tuple(int(part) for part in item.split(":")),
    )
    return VolumeScan(
        name=file_path.name,
        size=stat_size,
        sha256=digest.hexdigest() if digest is not None else "",
        is_gzip=False,
        triggers=triggers,
        empty_triggers=empty_triggers,
        wave_events=wave_events,
        waves=waves,
        bad_bytes=bad_bytes,
        resyncs=resyncs,
        orphans=orphans,
        pending_bytes=pending_bytes,
        first_trigger=first_trigger,
        last_trigger=last_trigger,
        first_wave_trigger=first_wave_trigger,
        last_wave_trigger=last_wave_trigger,
        channels=sorted_channels,
    )
