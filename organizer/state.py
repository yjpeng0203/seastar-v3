"""Crash-safe SQLite checkpoint store for incremental per-volume decoding and session finalization."""
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional


@contextmanager
def decode_lock(work_dir: Path):
    directory = Path(work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "decode.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"解码目录已被另一个任务占用，请先停止旧的 organizer watch/finalize：{directory}")
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS decoded_parts (
                    run_key TEXT NOT NULL,
                    volume_name TEXT NOT NULL,
                    volume_index INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    triggers INTEGER NOT NULL,
                    empty_triggers INTEGER NOT NULL,
                    wave_events INTEGER NOT NULL,
                    waves INTEGER NOT NULL,
                    first_trig_json TEXT,
                    last_trig_json TEXT,
                    first_wave_trig_json TEXT,
                    last_wave_trig_json TEXT,
                    channels_json TEXT,
                    part_root TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    decoded_at TEXT NOT NULL,
                    PRIMARY KEY (run_key, volume_name)
                );

                CREATE TABLE IF NOT EXISTS finalized_runs (
                    run_key TEXT PRIMARY KEY,
                    session_dir TEXT NOT NULL,
                    string_group TEXT NOT NULL,
                    board_id INTEGER NOT NULL,
                    merged_root TEXT NOT NULL,
                    csv_path TEXT,
                    entries INTEGER NOT NULL,
                    waves INTEGER NOT NULL,
                    volume_count INTEGER NOT NULL,
                    finalized_at TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                );
                """
            )

    def get_valid_part(self, run_key: str, volume_name: str, expected_size: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM decoded_parts WHERE run_key = ? AND volume_name = ?",
            (run_key, volume_name),
        ).fetchone()
        if row is None:
            return None
        if int(row["size"]) != int(expected_size):
            return None
        part_path = Path(row["part_root"])
        if not part_path.is_file() or part_path.stat().st_size <= 0:
            return None
        result = dict(row)
        for field_name in ("first_trig_json", "last_trig_json", "first_wave_trig_json", "last_wave_trig_json", "channels_json"):
            raw = result.get(field_name)
            result[field_name[:-5]] = json.loads(raw) if raw else None
        return result

    def save_decoded_part(
        self,
        run_key: str,
        volume_name: str,
        volume_index: int,
        size: int,
        mtime_ns: int,
        scan_dict: Dict[str, Any],
        part_root: Path,
        backend: str,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO decoded_parts (
                    run_key, volume_name, volume_index, size, mtime_ns, sha256,
                    triggers, empty_triggers, wave_events, waves,
                    first_trig_json, last_trig_json, first_wave_trig_json, last_wave_trig_json,
                    channels_json, part_root, backend, decoded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_key, volume_name) DO UPDATE SET
                    volume_index=excluded.volume_index,
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    sha256=excluded.sha256,
                    triggers=excluded.triggers,
                    empty_triggers=excluded.empty_triggers,
                    wave_events=excluded.wave_events,
                    waves=excluded.waves,
                    first_trig_json=excluded.first_trig_json,
                    last_trig_json=excluded.last_trig_json,
                    first_wave_trig_json=excluded.first_wave_trig_json,
                    last_wave_trig_json=excluded.last_wave_trig_json,
                    channels_json=excluded.channels_json,
                    part_root=excluded.part_root,
                    backend=excluded.backend,
                    decoded_at=excluded.decoded_at
                """,
                (
                    run_key,
                    volume_name,
                    int(volume_index),
                    int(size),
                    int(mtime_ns),
                    str(scan_dict["sha256"]),
                    int(scan_dict["triggers"]),
                    int(scan_dict["empty_triggers"]),
                    int(scan_dict["wave_events"]),
                    int(scan_dict["waves"]),
                    json.dumps(scan_dict.get("first_trigger")),
                    json.dumps(scan_dict.get("last_trigger")),
                    json.dumps(scan_dict.get("first_wave_trigger")),
                    json.dumps(scan_dict.get("last_wave_trigger")),
                    json.dumps(scan_dict.get("channels", [])),
                    str(Path(part_root).resolve()),
                    backend,
                    _utc_now(),
                ),
            )

    def save_finalized_run(
        self,
        run_key: str,
        session_dir: Path,
        string_group: str,
        board_id: int,
        merged_root: Path,
        csv_path: Optional[Path],
        entries: int,
        waves: int,
        volume_count: int,
        summary: Dict[str, Any],
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO finalized_runs (
                    run_key, session_dir, string_group, board_id, merged_root, csv_path,
                    entries, waves, volume_count, finalized_at, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_key) DO UPDATE SET
                    session_dir=excluded.session_dir,
                    string_group=excluded.string_group,
                    board_id=excluded.board_id,
                    merged_root=excluded.merged_root,
                    csv_path=excluded.csv_path,
                    entries=excluded.entries,
                    waves=excluded.waves,
                    volume_count=excluded.volume_count,
                    finalized_at=excluded.finalized_at,
                    summary_json=excluded.summary_json
                """,
                (
                    run_key,
                    str(Path(session_dir).resolve()),
                    string_group,
                    int(board_id),
                    str(Path(merged_root).resolve()),
                    str(Path(csv_path).resolve()) if csv_path else None,
                    int(entries),
                    int(waves),
                    int(volume_count),
                    _utc_now(),
                    json.dumps(summary, ensure_ascii=False),
                ),
            )

    def get_finalized_run(self, run_key: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM finalized_runs WHERE run_key = ?",
            (run_key,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["summary"] = json.loads(data["summary_json"])
        return data

    def list_finalized_runs(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM finalized_runs ORDER BY finalized_at DESC").fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["summary"] = json.loads(item["summary_json"])
            results.append(item)
        return results

    def close(self) -> None:
        self.conn.close()
