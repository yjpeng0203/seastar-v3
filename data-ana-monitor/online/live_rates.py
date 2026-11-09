"""Independent DAT header counting; committed offsets and counts advance atomically."""
from collections import Counter
import fcntl
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import signal
import sqlite3
import time

from acquisition import DatParser, DatReader, discover
from rates import finish, parameters

WIDTH_NS = 100_000_000_000


def directory(state_dir, source_key):
    return Path(state_dir) / 'live-rates' / hashlib.sha256(source_key.encode()).hexdigest()[:24]


class CounterStore:
    def __init__(self, data_dir, state_dir, source_key):
        self.data_dir, self.source_key = Path(data_dir), source_key
        self.directory = directory(state_dir, source_key)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.directory / 'writer.lock').open('a+b')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.db = sqlite3.connect(self.directory / 'counts.sqlite3')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY,state TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS records (seq INTEGER PRIMARY KEY,segment INTEGER,t_ns INTEGER,code INTEGER)')
        self.db.execute('CREATE INDEX IF NOT EXISTS record_time ON records(segment,t_ns,code)')
        self.db.execute('CREATE TABLE IF NOT EXISTS bins (segment INTEGER,k INTEGER,code INTEGER,n INTEGER,PRIMARY KEY(segment,k,code)) WITHOUT ROWID')
        saved = self.db.execute('SELECT state FROM checkpoint WHERE id=1').fetchone()
        self.state = json.loads(saved[0]) if saved else dict(seq=0,segment=0,origin_ns=None,last_ns=None,pending=None,channels=[],spans={},waves=0,events=0)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO checkpoint VALUES (1,?)',(json.dumps(self.state),))
        self.reader = DatReader(self.data_dir, self.state.get('reader'))
        self.parser = DatParser(self.state.get('parser'))

    def step(self, source):
        incoming = self.reader.read(source, max_bytes=1024*1024)
        if not incoming:
            return False
        rows = []; counts = Counter(); s = self.state
        channels = set(s['channels'])
        for r in self.parser.feed(incoming, headers_only=True):
            s['seq'] += 1
            if r['type'] == 'trigger':
                s['pending'] = dict(ns=r['ns'],seen=False)
                continue
            event = s['pending']
            if event is None:
                raise ValueError('计数波形前缺少触发头')
            if not event['seen']:
                now = event['ns']
                if s['origin_ns'] is None:
                    s['origin_ns'] = now
                elif now < s['last_ns']:
                    s['segment'] += 1; s['origin_ns'] = now
                s['last_ns'] = now
                s['spans'][str(s['segment'])] = now-s['origin_ns']
                event['seen'] = True; s['events'] += 1
                t = now-s['origin_ns']
                # seq*2 separates the event and its first waveform without rounding time.
                rows.append((s['seq']*2,s['segment'],t,-1))
                counts[(s['segment'],t//WIDTH_NS,-1)] += 1
            t = event['ns']-s['origin_ns']; code = r['board']*256+r['channel']
            rows.append((s['seq']*2+1,s['segment'],t,code))
            counts[(s['segment'],t//WIDTH_NS,code)] += 1
            channels.add(code); s['waves'] += 1
        s.update(channels=sorted(channels),reader=self.reader.state(),parser=self.parser.state(),published_host_unix_s=time.time())
        with self.db:
            self.db.executemany('INSERT INTO records VALUES (?,?,?,?)', rows)
            self.db.executemany('INSERT INTO bins VALUES (?,?,?,?) ON CONFLICT(segment,k,code) DO UPDATE SET n=n+excluded.n',
                                [(*key,n) for key,n in counts.items()])
            self.db.execute('INSERT OR REPLACE INTO checkpoint VALUES (1,?)',(json.dumps(s),))
        return True

    def publish(self, source, error=None):
        value = {k:self.state[k] for k in ('seq','segment','channels','waves','events','spans')}
        value.update(source=self.source_key,checked_host_unix_s=time.time(),error=error,
                     published_host_unix_s=self.state.get('published_host_unix_s'),
                     backlog=sum(max(0,f['size']-self.reader.offsets.get(f['name'],0)) for f in source['files']),
                     pending_bytes=len(self.parser.buffer),waiting_for_volume=self.reader.waiting_for_volume,
                     bad_bytes=self.parser.bad_bytes,orphan_waves=self.parser.orphans,first_index=self.reader.first_index)
        path = self.directory / 'status.json'; temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value)); temporary.replace(path)

    def close(self):
        self.db.close(); self.lock.close()


def work(data_dir, state_dir, source_key, stop, follow, parent):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    store = None; source = None
    try:
        store = CounterStore(data_dir,state_dir,source_key)
        last_scan = last_publish = 0; sources=[]
        while not stop.is_set() and os.getppid() == parent:
            now = time.monotonic()
            if now-last_scan >= 1:
                sources=discover(data_dir)
                source = next((s for s in sources if s['key']==store.source_key),None)
                last_scan = now
            if source is None:
                raise ValueError('计数数据源已消失')
            read = store.step(source)
            if now-last_publish >= .5:
                store.publish(source); last_publish = now
                pointer=directory(state_dir,source_key)/'active.json';temporary=pointer.with_suffix('.tmp')
                temporary.write_text(json.dumps(dict(source=store.source_key)));temporary.replace(pointer)
            if not read:
                if follow.is_set() and not store.parser.buffer and not store.reader.waiting_for_volume:
                    newer=[s for s in sources if s['kind']=='dat' and s['key'].split('_run',1)[0]==store.source_key.split('_run',1)[0] and
                           (s['stamp'],s['key'])>(source['stamp'],source['key'])]
                    if newer:
                        store.close();store=None
                        store=CounterStore(data_dir,state_dir,newer[0]['key']);source=newer[0]
                        last_publish=0
                        continue
                stop.wait(.25)
    except Exception as error:
        if store and source:
            store.publish(source,str(error))
    finally:
        if store: store.close()


class LiveRates:
    def __init__(self, data_dir, state_dir, source_key, follow=False):
        self.source_key = source_key
        context = mp.get_context('spawn'); self.stop = context.Event();self.follow=context.Event()
        if follow:self.follow.set()
        self.process = context.Process(target=work,args=(str(data_dir),str(state_dir),source_key,self.stop,self.follow,os.getpid()),name='SeaStar-counter')
        self.process.start()

    def close(self):
        self.stop.set(); self.process.join(timeout=5)
        if self.process.is_alive():
            self.process.terminate(); self.process.join(timeout=5)


def status(state_dir, source_key):
    try:
        return json.loads((directory(state_dir,source_key)/'status.json').read_text())
    except (OSError,ValueError):
        return None


def query(state_dir, source_key, request, snapshot):
    requested_source=source_key
    # The counter keeps its current Run when following is switched off; it never rewinds.
    try:source_key=json.loads((directory(state_dir,source_key)/'active.json').read_text())['source']
    except (OSError,ValueError,KeyError):pass
    health = status(state_dir,source_key)
    if not health: raise ValueError('轻量计数通道正在初始化')
    if health.get('error'): raise ValueError('轻量计数已停止：'+health['error'])
    if time.time()-health['checked_host_unix_s'] > 5: raise ValueError('轻量计数通道超过 5 秒未报告状态')
    path = directory(state_dir,source_key)/'counts.sqlite3'
    db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)
    try:
        db.execute('BEGIN')
        s = json.loads(db.execute('SELECT state FROM checkpoint WHERE id=1').fetchone()[0])
        segment = request.get('segment',s['segment'])
        if request.get('follow_segment') and segment==snapshot.get('segment',0):segment=s['segment']
        end = s['spans'].get(str(segment),0)*1e-9
        request=dict(request,segment=segment)
        if request.get('scope') in ('recent','all','first'):
            request = dict(request,min_s=max(0,end-900) if request['scope']=='recent' else 0,
                           max_s=max(.000001,min(60,end) if request['scope']=='first' else end))
        lo,hi,segment,_,width = parameters(request,snapshot,end)
        counts = Counter()
        if hi>lo:
            first,last = math.floor(lo/width),math.ceil(hi/width)-1
            width_ns=width*1e9
            # Only partial edge bins touch individual timestamps for the default 100 s view.
            if width == 100:
                for code,k,n in db.execute('SELECT code,k,n FROM bins WHERE segment=? AND k>? AND k<?',(segment,first,last)):
                    counts[(code,k)] += n
                ranges = [(lo,min(hi,(first+1)*width))]
                if last>first:ranges.append((last*width,hi))
            else:
                ranges = [(lo,hi)]
            for a,b in ranges:
                closed = b==hi and hi==end
                sql = 'SELECT code,t_ns FROM records WHERE segment=? AND t_ns>=? AND t_ns'+('<=?' if closed else '<?')
                for code,ns in db.execute(sql,(segment,math.ceil(a*1e9),math.floor(b*1e9) if closed else math.ceil(b*1e9))):
                    k=ns//int(width_ns) if width_ns>=1 and width_ns.is_integer() else math.floor(ns*1e-9/width)
                    counts[(code,min(last,k))] += 1
        result = finish(counts,s['channels'],lo,hi,width,request['run_id'],s['seq'],'independent DAT header counts / observed exposure')
        result.update(live=True,counter_seq=s['seq'],counter_waves=s['waves'],counter_events=s['events'],
                      counter_source=source_key,analysis_source=requested_source,
                      checked_host_unix_s=health['checked_host_unix_s'],published_host_unix_s=s.get('published_host_unix_s'),
                      no_new_data_s=max(0,time.time()-s['published_host_unix_s']) if s.get('published_host_unix_s') else None,
                      backlog=health['backlog'],pending_bytes=health['pending_bytes'],waiting_for_volume=health['waiting_for_volume'],
                      bad_bytes=health['bad_bytes'],orphan_waves=health['orphan_waves'],first_index=health['first_index'],
                      segment=s['segment'],relative_s=s['spans'].get(str(s['segment']),0)*1e-9,analysis_seq=snapshot.get('seq'))
        return result
    finally:
        db.close()
