"""One independent process per string; browsing never selects/stops an analysis reader."""
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import hashlib
import fcntl
import json
import multiprocessing as mp
import queue
import signal
import threading
import time
import urllib.parse
import urllib.request

from acquisition import discover
from analysis import Config,dumps


def rotational(path):
    import os
    device=os.stat(path).st_dev
    node=Path('/sys/dev/block')/f'{os.major(device)}:{os.minor(device)}'
    for base in (node.resolve(),node.resolve().parent):
        try:return (base/'queue/rotational').read_text().strip()=='1'
        except OSError:pass
    return False


def canonical_sources(sources,replay):
    grouped={}
    for source in sources:
        group=source.get('string_group') or source['key'].split('_run')[0]
        grouped.setdefault(group,[]).append(source)
    preferred='root' if replay else 'dat'
    chosen=[next((s for s in values if s['kind']==preferred),values[0]) for values in grouped.values()]
    return sorted(chosen,key=lambda s:(s['kind']==preferred,s.get('stamp',''),max(f['mtime_ns'] for f in s['files'])),reverse=True)


def worker(data_dir,state_dir,config,source,follow,poll,replay,read_gate,build_gate,ready,stop,pressure):
    signal.signal(signal.SIGINT,signal.SIG_IGN)
    import os
    os.nice(10 if replay or source['kind']=='root' else 5)
    from runtime_resources import role
    role('SeaAna-'+str(source.get('string_group','FEE')))
    from http.server import ThreadingHTTPServer
    from server import Monitor,make_handler
    from replay_cache import ReplayCache
    monitor=None;server=None
    try:
        if replay and source['kind']=='root' and source.get('board_id') is not None:
            monitor=ReplayCache(data_dir,state_dir,Config(**config),source,read_gate,build_gate)
        else:monitor=Monitor(data_dir,state_dir,Config(**config),source['key'],follow,poll,replay=replay,read_gate=read_gate,pressure=pressure)
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(monitor))
        server.daemon_threads=True
        ready.put(dict(port=server.server_port));monitor.start()
        import os
        parent=os.getppid()
        def shutdown_when_parent_stops():
            while not stop.wait(.5):
                if os.getppid()!=parent:stop.set()
            server.shutdown()
        threading.Thread(target=shutdown_when_parent_stops,daemon=True).start()
        server.serve_forever(poll_interval=.1)
    except Exception as error:ready.put(dict(error=str(error)))
    finally:
        if server:server.server_close()
        if monitor:monitor.close()


class FleetMonitor:
    def __init__(self,data_dir,state_dir,config=None,source=None,follow=False,poll=.5,receiver=None,replay=False,workers=3):
        self.data_dir=Path(data_dir).resolve();self.state_dir=Path(state_dir).resolve();self.state_dir.mkdir(parents=True,exist_ok=True)
        self.lock_file=(self.state_dir/'fleet.lock').open('a+b')
        try:fcntl.flock(self.lock_file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close();raise RuntimeError('状态目录已由另一个三串服务使用，请使用不同的 --state-dir')
        self.config=config or Config();self.receiver=receiver;self.follow=follow;self.poll=poll;self.replay=replay
        self.limit=workers;self.sources=canonical_sources(discover(data_dir),replay)
        if source:
            explicit=next((s for s in discover(data_dir) if s['key']==source or any(f['name']==source for f in s['files'])),None)
            if explicit:
                group=explicit.get('string_group')
                self.sources=[explicit if s.get('string_group')==group else s for s in self.sources]
        self.active=next((s['key'] for s in self.sources if s['key']==source or any(f['name']==source for f in s['files'])),self.sources[0]['key'] if self.sources else '')
        self.ctx=mp.get_context('spawn');self.read_gate=self.ctx.Lock();self.build_gate=self.ctx.Lock();self.pressure=self.ctx.Event()
        self.resources={};self.resource_checked=0
        self.stop=threading.Event();self.entries={};self.guard=threading.RLock();self.commands=self
        self.store=SimpleNamespace(close=self.close)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.thread=threading.Thread(target=self.watch,daemon=True)

    def start(self):self.thread.start()
    def qsize(self):return 0
    def busy(self,entry):
        return entry['process'].is_alive() and not entry.get('error') and (entry.get('pending_start') is not None or
            entry['snapshot'].get('status') not in ('complete','incomplete','error','paused'))
    def launch(self,source,config=None):
        key=source['key'];directory=self.state_dir/'strings'/(source.get('string_group') or hashlib.sha256(key.encode()).hexdigest()[:12])
        entry=dict(source=source,directory=directory,port=None,snapshot={},error=None)
        gate=self.read_gate if rotational(self.data_dir/source['files'][0]['name']) else None
        ready=self.ctx.Queue();stop=self.ctx.Event()
        process=self.ctx.Process(target=worker,args=(str(self.data_dir),str(directory),asdict(config or self.config),source,self.follow,self.poll,self.replay,gate,self.build_gate,ready,stop,self.pressure),name='SeaStar-'+(source.get('string_group') or key))
        entry.update(process=process,ready=ready,stop=stop);self.entries[key]=entry;process.start()
        from runtime_resources import record
        record(self.state_dir/'processes.jsonl','analysis_started',role=source.get('string_group'),source=key,worker_pid=process.pid)

    def watch(self):
        while not self.stop.is_set():
            if time.monotonic()-self.resource_checked>1:
                from runtime_resources import snapshot,SOFT_BYTES
                self.resources=snapshot();self.resource_checked=time.monotonic()
                if self.resources['rss_bytes']>=SOFT_BYTES:self.pressure.set()
                elif self.resources['rss_bytes']<SOFT_BYTES*.8:self.pressure.clear()
                if time.monotonic()-getattr(self,'last_resource_log',0)>10:
                    from runtime_resources import record
                    record(self.state_dir/'resources.jsonl','resources',**self.resources)
                    self.last_resource_log=time.monotonic()
            with self.guard:
                active=sum(self.busy(e) for e in self.entries.values())
                for source in self.sources:
                    if source['kind']=='root' and not self.replay and source['key']!=self.active:continue
                    if source['key'] not in self.entries and active<self.limit:self.launch(source);active+=1
                entries=list(self.entries.values())
            for entry in entries:
                if not entry['port']:
                    try:
                        ready=entry['ready'].get_nowait();entry.update(ready)
                    except queue.Empty:pass
                if entry['port']:
                    try:
                        with self.opener.open(f'http://127.0.0.1:{entry["port"]}/api/snapshot',timeout=2) as response:
                            entry['snapshot']=json.load(response)
                        pending=entry.get('pending_start');snap=entry['snapshot']
                        if pending and (snap.get('status')=='error' or
                            (pending['action']=='resume' and snap.get('status')!='paused') or
                            (pending['action']!='resume' and snap.get('run_id')!=pending['run_id'])):
                            entry.pop('pending_start',None)
                        current=entry['snapshot'].get('source')
                        old=entry['source']['key']
                        if current and current['key']!=old:
                            with self.guard:
                                entry['source']=current
                                self.entries.pop(old,None);self.entries[current['key']]=entry
                                self.sources=[current if s['key']==old else s for s in self.sources]
                                if self.active==old:self.active=current['key']
                    except (OSError,ValueError):pass
                if not entry['process'].is_alive() and not entry.get('error'):entry['error']='分析子进程已停止，已提交断点保留'
            if time.monotonic()-getattr(self,'last_scan',0)>5:
                discovered=canonical_sources(discover(self.data_dir),self.replay)
                for source in discovered:
                    if not any(s.get('string_group')==source.get('string_group') for s in self.sources):self.sources.append(source)
                if not self.active and self.sources:self.active=self.sources[0]["key"]
                self.last_scan=time.monotonic()
            busy=any(e['snapshot'].get('status') not in ('complete','error','paused','incomplete') for e in entries)
            self.stop.wait(.5 if busy else 2.)

    def snapshot(self):
        with self.guard:
            entries=list(self.entries.values());entry=self.entries.get(self.active,{})
            selected=next((s for s in self.sources if s['key']==self.active),None)
            snap=dict(entry.get('snapshot') or dict(status='starting' if entry else 'queued',channels=[],config=asdict(self.config),sources=self.sources,
                       source=entry.get('source',selected),waves=0,triggers=0,span_s=0,seq=0,follow=self.follow,paused=False,alerts=[]))
            tasks=[]
            for source in self.sources:
                e=self.entries.get(source['key'],{});s=e.get('snapshot',{})
                tasks.append(dict(key=source['key'],group=source.get('string_group') or source['key'],status='error' if e.get('error') else s.get('status','starting' if e else 'queued'),
                  waves=s.get('waves',0),backlog=s.get('backlog'),backlog_unit=s.get('backlog_unit'),rate=s.get('analysis_waves_per_s'),cache_mode=s.get('cache_mode'),eta_seconds=s.get('eta_seconds'),error=e.get('error') or s.get('error')))
            snap.update(sources=self.sources,tasks=tasks,workers=self.limit,active_source=self.active,data_dir=str(self.data_dir),input_mode='replay' if self.replay else 'remote' if self.receiver else 'local',resources=self.resources,resource_paused=self.pressure.is_set())
            if hasattr(self,'query_jobs'):snap['query_tasks']=self.query_jobs.snapshot()
            if self.receiver:snap['transfer']=self.receiver.snapshot()
            if entry.get('error'):snap.update(status='error',error=entry['error'])
            return snap

    def put(self,command):
        action=command['action']
        if action=='select':
            if command['source'] not in [s['key'] for s in self.sources]:raise ValueError('数据源不存在')
            self.active=command['source'];return
        if action in ('receive-pause','receive-resume'):
            if self.receiver is None:raise ValueError('当前不是接收模式')
            self.receiver.pause() if action=='receive-pause' else self.receiver.resume();return
        entry=self.entries.get(self.active)
        if not entry:raise ValueError('该串等待计算资源；请暂停其它分析任务释放名额，或调整 --workers')
        if not entry['port']:raise ValueError('该串正在启动，请稍候')
        if action in ('resume','configure','reset'):
            with self.guard:
                if not self.busy(entry):
                    if sum(self.busy(e) for e in self.entries.values())>=self.limit:
                        raise ValueError(f'并行分析名额已满（{self.limit}）；请先暂停另一个任务，再继续当前串')
                    entry['pending_start']=dict(action=action,run_id=entry['snapshot'].get('run_id'))
        if action in ('configure','reset') and self.replay and entry['source']['kind']=='root':
            config=Config(**command['config']) if action=='configure' else Config(**entry['snapshot']['config'])
            entry['stop'].set();entry['process'].join(timeout=30)
            if entry['process'].is_alive():raise ValueError('正在保存上一会话，请稍后重试')
            if action=='reset':
                snapshot=entry['snapshot']
                path=Path(snapshot.get('state_dir',entry['directory']))/'snapshot.json'
                if path.exists():path.unlink()
            with self.guard:self.launch(entry['source'],config)
            return
        try:self.request(entry,'/api/control',command)
        except Exception:
            entry.pop('pending_start',None)
            raise

    def request(self,entry,route,body=None):
        request=urllib.request.Request(f'http://127.0.0.1:{entry["port"]}'+route,
            data=dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
        try:
            with self.opener.open(request,timeout=90) as response:return json.load(response)
        except urllib.error.HTTPError as error:raise ValueError(json.load(error).get('error','子任务请求失败')) from error

    def owner(self,run):
        entry=next((e for e in self.entries.values() if e['snapshot'].get('run_id')==run),None)
        if not entry or not entry['port']:raise ValueError('分析会话未就绪或已经切换，请刷新')
        return entry
    def query_target(self,request):
        entry=self.owner(request.get('run_id'));snap=entry['snapshot']
        if snap.get('cache_mode'):
            if not snap.get('analysis_path') or snap.get('status')!='complete':raise ValueError('历史分析尚未完成，请稍后重试')
            return dict(kind='root',path=str(Path(snap['analysis_path'])/'root/analysis.root'),state_dir=snap['state_dir'],snapshot=snap)
        return dict(kind='sqlite',path=str(entry['directory']/'analysis.sqlite3'),state_dir=str(entry['directory']),snapshot=snap)
    def rates(self,request):
        entry=self.owner(request.get('run_id'))
        if request.get('live') and request.get('population','all')=='all' and entry['source']['kind']=='dat' and not self.replay:
            from live_rates import query
            return query(entry['directory'],entry['source']['key'],request,entry['snapshot'])
        return self.request(entry,'/api/rates',request)
    def time_trend(self,request):return self.request(self.owner(request.get('run_id')),'/api/time-trend',request)
    def raw_trend(self,request):return self.request(self.owner(request.get('run_id')),'/api/raw-trend',request)
    def histogram(self,request):return self.request(self.owner(request.get('run_id')),'/api/histogram',request)
    def export_csv(self,run,channel=None,max_seq=None):
        query=dict(run_id=run)
        if channel is not None:query['channel']=channel
        if max_seq is not None:query['max_seq']=max_seq
        entry=self.owner(run)
        with self.opener.open(f'http://127.0.0.1:{entry["port"]}/api/metrics.csv?'+urllib.parse.urlencode(query),timeout=90) as response:
            while data:=response.read(65536):yield data
    def close(self):
        self.stop.set()
        if hasattr(self,'query_jobs'):self.query_jobs.close()
        if self.thread.ident:self.thread.join(timeout=5)
        for entry in self.entries.values():entry['stop'].set()
        for entry in self.entries.values():
            entry['process'].join(timeout=30)
            if entry['process'].is_alive():entry['process'].terminate();entry['process'].join(timeout=5)
        self.lock_file.close()
