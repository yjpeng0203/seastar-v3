"""WAL metrics and checkpoints are committed in one transaction: replay is exactly-once."""
from dataclasses import asdict
import fcntl
import json
from pathlib import Path
import sqlite3
import time
import uuid

from analysis import dumps, METRICS

FIELDS = ("seq", "position", "board", "channel", "wave_id", "raw_ns", "trigger_number",
          "t", "segment", "points", "baseline", "noise", "amplitude", "area", "tail_negative",
          "peak_ns", "cfd_ns", "threshold_ns", "fwhm_ns", "rise_ns", "fall_ns", "wave_dt_ns",
          "start", "end", "polarity", "pulses", "baseline_points", "good", "flags")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Path(str(self.path) + ".lock").open("a+b")
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.lock.close()
            raise RuntimeError("状态目录已被另一个分析进程使用；请使用不同的 --state-dir") from error
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=20)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_size_limit=16777216")
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, source TEXT NOT NULL, "
                        "config TEXT NOT NULL, kernel TEXT NOT NULL, state TEXT, created REAL, updated REAL)")
        columns = ",".join('"' + f + '" ' + ("REAL" if f in FIELDS[7:8] + FIELDS[10:22] else "INTEGER") for f in FIELDS)
        self.db.execute("CREATE TABLE IF NOT EXISTS metrics (run TEXT NOT NULL REFERENCES runs(id), " +
                        columns + ", PRIMARY KEY(run,seq))")
        self.db.execute("CREATE INDEX IF NOT EXISTS metrics_channel ON metrics(run,board,channel,segment,t)")
        self.db.execute("CREATE TABLE IF NOT EXISTS native_bins (run TEXT,metric TEXT,population TEXT,board INTEGER,channel INTEGER,bin INTEGER,n INTEGER,PRIMARY KEY(run,metric,population,board,channel,bin)) WITHOUT ROWID")
        self.db.execute("CREATE TABLE IF NOT EXISTS native_index (run TEXT PRIMARY KEY,max_seq INTEGER)")
        self.db.commit()

    def create(self, source, config, kernel):
        run_id = uuid.uuid4().hex
        now = time.time()
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                            (run_id, source, dumps(asdict(config)), kernel, None, now, now))
        return run_id

    def restore(self, source, config, kernel):
        row = self.db.execute("SELECT id,state FROM runs WHERE source=? AND config=? AND kernel=? "
                              "ORDER BY updated DESC LIMIT 1", (source, dumps(asdict(config)), kernel)).fetchone()
        return (row[0], json.loads(row[1]) if row[1] else None) if row else (None, None)

    def index_native(self,run_id,after,through,config):
        for name in METRICS:
            expression='"'+name+'"'
            if name=='window_ns':expression=f'("end"-"start")*{float(config["sample_ns"])}'
            if name=='snr':expression='CASE WHEN noise>0 THEN amplitude/noise ELSE 0 END'
            floor='(CAST('+expression+' AS INTEGER)-('+expression+'<CAST('+expression+' AS INTEGER)))'
            self.db.execute('INSERT INTO native_bins SELECT run,?,p,board,channel,'+floor+',COUNT(*) '
                'FROM metrics CROSS JOIN (SELECT "all" AS p UNION ALL SELECT "good") '
                'WHERE run=? AND seq>? AND seq<=? AND (p="all" OR good=1) AND '+expression+' IS NOT NULL '
                'GROUP BY board,channel,p,'+floor+' '
                'ON CONFLICT(run,metric,population,board,channel,bin) DO UPDATE SET n=n+excluded.n',
                (name,run_id,after,through))
        self.db.execute('INSERT OR REPLACE INTO native_index VALUES (?,?)',(run_id,through))

    def rebuild_native(self,run_id,config):
        watermark=self.db.execute('SELECT COALESCE(MAX(seq),0) FROM metrics WHERE run=?',(run_id,)).fetchone()[0]
        row=self.db.execute('SELECT max_seq FROM native_index WHERE run=?',(run_id,)).fetchone()
        if row and row[0]>=watermark:return
        with self.db:self.index_native(run_id,row[0] if row else 0,watermark,config)

    def save(self, run_id, state, metrics):
        # Any failure rolls back BOTH offsets/aggregates and individual metrics.
        with self.db:
            previous=self.db.execute('SELECT max_seq FROM native_index WHERE run=?',(run_id,)).fetchone()
            if metrics:
                names = ",".join('"' + f + '"' for f in FIELDS)
                self.db.executemany("INSERT INTO metrics(run," + names + ") VALUES (" +
                                    ",".join("?" for _ in range(len(FIELDS) + 1)) + ")",
                                    [(run_id,) + tuple(m[f] for f in FIELDS) for m in metrics])
            if metrics and state.get('engine'):
                through=max(m['seq'] for m in metrics)
                self.index_native(run_id,previous[0] if previous else 0,through,state['engine']['config'])
            self.db.execute("UPDATE runs SET state=?,updated=? WHERE id=?",
                            (dumps(state), time.time(), run_id))

    def close(self):
        self.db.close()
        self.lock.close()


def read_run(path, run_id):
    db = sqlite3.connect("file:" + Path(path).resolve().as_posix() + "?mode=ro", uri=True, timeout=20)
    try:
        row = db.execute("SELECT config,kernel,state FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("分析会话不存在")
        return json.loads(row[0]), row[1], json.loads(row[2]) if row[2] else {}
    finally:
        db.close()


def export_rows(path, run_id, channel=None, max_seq=None):
    db = sqlite3.connect("file:" + Path(path).resolve().as_posix() + "?mode=ro", uri=True, timeout=20)
    try:
        sql = "SELECT " + ",".join('"' + f + '"' for f in FIELDS) + " FROM metrics WHERE run=?"
        args = [run_id]
        if max_seq is not None:
            sql += " AND seq<=?"
            args.append(max_seq)
        if channel:
            board, ch = (int(v) for v in channel.split(":"))
            sql += " AND board=? AND channel=?"
            args += [board, ch]
        cursor = db.execute(sql + " ORDER BY seq", args)
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            yield rows
    finally:
        db.close()
