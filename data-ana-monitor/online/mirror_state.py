"""IPC-only receive commits. Readers fail closed and never consume an uncommitted tail."""
import json
import os
from pathlib import Path
import sqlite3


def sync_directory(path):
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def committed_files(directory):
    metadata = Path(directory) / ".receiver"
    if not metadata.exists():
        return None
    try:
        db=sqlite3.connect((metadata/"receive.sqlite3").resolve().as_uri()+"?mode=ro",uri=True,timeout=5)
        try:
            row=db.execute("SELECT value FROM receive_state WHERE id=1").fetchone()
        finally:
            db.close()
        value=json.loads(row[0])
        if value["version"] != 1 or not isinstance(value["files"], dict):
            raise ValueError("invalid schema")
        for name, info in value["files"].items():
            if (Path(name).name != name or name in (".", "..") or not name.endswith(".dat") or
                    type(info["committed"]) is not int or info["committed"] < 0):
                raise ValueError("invalid commit entry")
        return {name:info for name,info in value["files"].items() if not info.get('pending_create')}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError) as error:
        raise ValueError("接收清单缺失或损坏；停止读取，不能把未确认尾部当作数据") from error
