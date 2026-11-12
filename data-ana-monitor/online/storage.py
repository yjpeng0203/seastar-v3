"""WAL metrics and checkpoints are committed in one transaction: replay is exactly-once."""
from dataclasses import asdict
from collections import Counter
import fcntl
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid

from analysis import dumps, METRICS

FIELDS = ("seq", "position", "board", "channel", "wave_id", "raw_ns", "trigger_number",
          "t", "segment", "points", "baseline", "noise", "amplitude", "area", "tail_negative",
          "peak_ns", "cfd_ns", "threshold_ns", "fwhm_ns", "rise_ns", "fall_ns", "wave_dt_ns",
          "start", "end", "polarity", "pulses", "baseline_points", "good", "flags")

TIME_COLUMNS = ('seq','board','channel','segment','t','raw_ns','wave_dt_ns','trigger_number',
                'baseline','noise','amplitude','area','good')
TIME_UPSERT = ('INSERT INTO time_bins100 VALUES (?,?,?,?,?,?,?,?,?,?) '
    'ON CONFLICT(run,segment,code,bin,population,field) DO UPDATE SET '
    'n=n+excluded.n,total=total+excluded.total,'
    'mean=mean+(excluded.mean-mean)*excluded.n/(n+excluded.n),'
    'm2=m2+excluded.m2+(excluded.mean-mean)*(excluded.mean-mean)*n*excluded.n/(n+excluded.n)')


def time_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS time_bins100 (run TEXT,segment INTEGER,code INTEGER,bin INTEGER,population TEXT,field INTEGER,n INTEGER,total REAL,mean REAL,m2 REAL,PRIMARY KEY(run,segment,code,bin,population,field)) WITHOUT ROWID')
    db.execute('CREATE TABLE IF NOT EXISTS time_index (run TEXT PRIMARY KEY,max_seq INTEGER,state TEXT)')


def index_time(db,run,metrics):
    """Caller owns the write transaction; counts and predecessor watermark commit together."""
    previous=db.execute('SELECT max_seq,state FROM time_index WHERE run=?',(run,)).fetchone()
    state=json.loads(previous[1]) if previous else {'origins':{},'channels':{},'event':None,'seq':0}
    grouped={};through=previous[0] if previous else 0
    def add(segment,code,t,field,value,pop):
        if value is None or not math.isfinite(value):return
        key=(segment,code,math.floor(t/100),pop,field);item=grouped.get(key)
        if item is None:item=[0,0.,0.,0.];grouped[key]=item
        item[0]+=1;item[1]+=value;delta=value-item[2];item[2]+=delta/item[0];item[3]+=delta*(value-item[2])
    for m in metrics:
        q=m['seq']
        if q<=through:continue
        segment=m['segment'];code=m['board']*256+m['channel'];raw=m['raw_ns']
        trigger=raw-int(m['wave_dt_ns']);origin=state['origins'].setdefault(str(segment),trigger-round(m['t']*1e9))
        t=(raw-origin)*1e-9;tt=(trigger-origin)*1e-9;key=f'{segment}:{code}'
        last=state['channels'].get(key);interval=raw-last if last is not None else None
        event=[segment,m['trigger_number'],trigger]
        new=event!=state['event'] or q-state['seq']>1
        delta=trigger-state['event'][2] if new and state['event'] and state['event'][0]==segment else None
        for pop in ('all','good'):
            for field,name in enumerate(('baseline','noise','amplitude','area')):
                if pop=='all' or m['good']:add(segment,code,t,field,m[name],pop)
            add(segment,code,t,4,m['good'],pop)
            if pop=='all' or m['good']:add(segment,code,t,5,interval,pop)
            if delta is not None:add(segment,-1,tt,6,delta,pop)
        state['channels'][key]=raw;state['event']=event;state['seq']=q;through=q
    db.executemany(TIME_UPSERT,[(run,*key,*value) for key,value in grouped.items()])
    db.execute('INSERT OR REPLACE INTO time_index VALUES (?,?,?)',(run,through,json.dumps(state)))
    return through


def ensure_time_index(path,run,through):
    """A cancellable query backfills old databases in bounded atomic chunks."""
    db=sqlite3.connect(path,timeout=20)
    try:
        time_schema(db);db.commit()
        while True:
            with db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT max_seq FROM time_index WHERE run=?',(run,)).fetchone()
                after=row[0] if row else 0
                if after>=through:return after
                rows=db.execute('SELECT '+','.join(TIME_COLUMNS)+' FROM metrics WHERE run=? AND seq>? AND seq<=? ORDER BY seq LIMIT 8192',(run,after,through)).fetchall()
                if not rows:return after
                index_time(db,run,(dict(zip(TIME_COLUMNS,r)) for r in rows))
    finally:db.close()


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
        time_schema(self.db)
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

    def index_native(self,run_id,after,through,config,metrics=None):
        # One bounded read per batch instead of rescanning it separately for every metric.
        cursor=None if metrics is not None else self.db.execute('SELECT '+','.join('"'+f+'"' for f in FIELDS)+' FROM metrics WHERE run=? AND seq>? AND seq<=? ORDER BY seq',(run_id,after,through))
        offset=0
        while True:
            batch=metrics[offset:offset+4096] if metrics is not None else [dict(zip(FIELDS,r)) for r in cursor.fetchmany(4096)]
            if not batch:break
            counts=Counter()
            for m in batch:
                if not after<m['seq']<=through:continue
                for name in METRICS:
                    if name=='window_ns':value=(m['end']-m['start'])*config['sample_ns']
                    elif name=='snr':value=(m['amplitude']/m['noise'] if m['amplitude'] is not None else None) if m['noise'] and m['noise']>0 else 0
                    else:value=m[name]
                    if value is None or not math.isfinite(value):continue
                    k=math.floor(value);counts[(name,'all',m['board'],m['channel'],k)]+=1
                    if m['good']:counts[(name,'good',m['board'],m['channel'],k)]+=1
            self.db.executemany('INSERT INTO native_bins VALUES (?,?,?,?,?,?,?) ON CONFLICT(run,metric,population,board,channel,bin) DO UPDATE SET n=n+excluded.n',[(run_id,*key,n) for key,n in counts.items()])
            offset+=len(batch)
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
            prior_seq=self.db.execute('SELECT seq FROM metrics WHERE run=? ORDER BY seq DESC LIMIT 1',(run_id,)).fetchone()
            time_seq=self.db.execute('SELECT max_seq FROM time_index WHERE run=?',(run_id,)).fetchone()
            if metrics:
                names = ",".join('"' + f + '"' for f in FIELDS)
                self.db.executemany("INSERT INTO metrics(run," + names + ") VALUES (" +
                                    ",".join("?" for _ in range(len(FIELDS) + 1)) + ")",
                                    [(run_id,) + tuple(m[f] for f in FIELDS) for m in metrics])
            if metrics and state.get('engine'):
                through=max(m['seq'] for m in metrics)
                after=previous[0] if previous else 0
                self.index_native(run_id,after,through,state['engine']['config'],metrics if after==(prior_seq[0] if prior_seq else 0) else None)
            if metrics and (time_seq[0] if time_seq else 0)==(prior_seq[0] if prior_seq else 0):
                index_time(self.db,run_id,metrics)
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
