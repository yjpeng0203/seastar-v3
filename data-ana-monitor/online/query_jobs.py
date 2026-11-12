"""One cancellable, memory-budgeted heavy query for the whole public monitor."""
from collections import OrderedDict
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import threading
import time
from runtime_resources import HARD_BYTES,SOFT_BYTES,process_tree,record,role


def execute(target,route,request,output):
    role('SeaStar-query')
    os.nice(10)
    if os.path.exists('/proc/self/status'):
        import resource
        # RSS is watched by the parent; cap additional address space before a huge allocation.
        fields=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines())
        virtual=int(fields.get('VmSize','0 kB').split()[0])*1024
        resource.setrlimit(resource.RLIMIT_AS,(virtual+2*1024**3,virtual+2*1024**3))
    try:
        path=Path(target['path']);snap=target['snapshot'];state=target['state_dir']
        if target['kind']=='sqlite':
            if route=='/api/time-trend':
                from time_bins import sqlite_time_bins
                result=sqlite_time_bins(path,request,snap)
            elif route=='/api/raw-trend':
                from raw_trends import sqlite_page
                result=sqlite_page(path,request,snap)
            elif route=='/api/histogram':
                from histograms import query_histogram
                result=query_histogram(path,request)
            elif route=='/api/rates':
                from rates import sqlite_rates
                result=sqlite_rates(path,request,snap);result.update(live=False,analysis_limited=True)
            else:raise ValueError('未知查询')
        else:
            if route=='/api/time-trend':
                from time_bins import root_time_bins
                result=root_time_bins(path,state,request,snap)
            elif route=='/api/raw-trend':
                from raw_trends import root_page
                result=root_page(path,state,request,snap)
            elif route=='/api/histogram':
                from replay_cache import query_saved_histogram
                result=query_saved_histogram(target,request)
            elif route=='/api/rates':
                from rates import root_rates
                result=root_rates(path,request,snap)
            else:raise ValueError('未知查询')
        payload={'result':result}
    except Exception as error:
        payload={'error':str(error) or type(error).__name__}
    # Stream encoding avoids a second complete JSON string beside a large result.
    temporary=Path(output).with_suffix('.tmp')
    try:
        with temporary.open('w') as stream:
            for chunk in json.JSONEncoder(ensure_ascii=False,allow_nan=False).iterencode(payload):
                stream.write(chunk)
                if stream.tell()>32*1024**2:raise ValueError('查询结果超过 32 MiB，请缩小详情范围或使用导出')
        temporary.replace(output)
    except Exception as error:
        temporary.unlink(missing_ok=True)
        Path(output).write_text(json.dumps({'error':str(error)}))


class QueryRunner:
    def __init__(self,state_dir,budget_bytes=512*1024**2,timeout=90):
        self.directory=Path(state_dir)/'queries';self.directory.mkdir(parents=True,exist_ok=True)
        self.budget_bytes=budget_bytes;self.timeout=timeout;self.jobs=OrderedDict();self.tokens={}
        self.condition=threading.Condition();self.stop=threading.Event();self.active=None;self.last=None
        self.thread=threading.Thread(target=self.work,daemon=True,name='seastar-query-dispatch')
        self.thread.start()

    def run(self,target,route,request,disconnected=lambda:False):
        request=dict(request);token=request.pop('_query_id',None)
        if token is not None and (not isinstance(token,str) or not 1<=len(token)<=160):raise ValueError('查询标识无效')
        token=token or os.urandom(16).hex()
        identity=dict(kind=target['kind'],path=target['path'],route=route,request=request)
        key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
        with self.condition:
            if len(self.tokens)>=16:raise ValueError('重统计请求过多，请稍后重试；计数率继续更新')
            job=self.jobs.get(key)
            if not job:
                if len(self.jobs)>=8:raise ValueError('重统计队列已满，请稍后重试；计数率继续更新')
                job=dict(key=key,target=target,route=route,request=request,listeners={},done=False,error=None,path=self.directory/(key+'.json'))
                self.jobs[key]=job
            job['listeners'][token]=disconnected;self.tokens[token]=job
            self.condition.notify_all()
            try:
                while not job['done']:
                    if token not in job['listeners'] or disconnected():
                        job['listeners'].pop(token,None);self.condition.notify_all()
                        raise ValueError('查询已取消')
                    self.condition.wait(.1)
                if job['error']:raise ValueError(job['error'])
                with job['path'].open() as stream:payload=json.load(stream)
                if payload.get('error'):raise ValueError(payload['error'])
                return payload['result']
            finally:
                self.tokens.pop(token,None);job['listeners'].pop(token,None)
                if job['done'] and not job['listeners']:job['path'].unlink(missing_ok=True)

    def cancel(self,token):
        with self.condition:
            job=self.tokens.get(token)
            if job:
                job['listeners'].pop(token,None)
                self.condition.notify_all()
            return dict(cancelled=bool(job))

    def work(self):
        while not self.stop.is_set():
            with self.condition:
                if not self.jobs:
                    self.condition.wait(.2);continue
                key,job=next(iter(self.jobs.items()));self.active=job
            process=None;started=time.monotonic();peak=0;error=None
            try:
                if not job['listeners']:raise ValueError('查询已取消')
                total=sum(p['rss_bytes'] for p in process_tree())
                if total>=SOFT_BYTES:raise ValueError('监控内存达到保护预算，重统计暂缓；计数率继续更新')
                job['path'].unlink(missing_ok=True)
                process=mp.get_context('spawn').Process(target=execute,args=(job['target'],job['route'],job['request'],str(job['path'])),name='SeaStar-query')
                process.start();job.update(pid=process.pid,started=time.time(),rss_bytes=0)
                record(self.directory/'tasks.jsonl','started',query=key,route=job['route'],worker_pid=process.pid,run=job['request'].get('run_id'))
                while process.is_alive():
                    with self.condition:
                        for token,check in list(job['listeners'].items()):
                            if check():job['listeners'].pop(token,None)
                        if not job['listeners']:raise ValueError('查询已取消')
                    if self.stop.is_set():raise ValueError('监控服务正在停止')
                    if time.monotonic()-started>self.timeout:raise ValueError('重统计超过时间预算，请缩小范围或稍后重试')
                    rss=sum(p['rss_bytes'] for p in process_tree(process.pid));peak=max(peak,rss);job['rss_bytes']=rss
                    if rss>(max(self.budget_bytes,768*1024**2) if job['target']['kind']=='root' else self.budget_bytes):raise ValueError('重统计超过内存预算，请分段查询；计数率继续更新')
                    if sum(p['rss_bytes'] for p in process_tree())>HARD_BYTES:raise ValueError('监控触发总内存保护，已停止重统计')
                    process.join(timeout=.1)
                if process.exitcode or not job['path'].exists():raise ValueError('重统计进程未正常完成，计数和分析断点保留')
            except Exception as failure:error=str(failure)
            finally:
                if process and process.is_alive():process.terminate();process.join(timeout=5)
                if process and process.is_alive():process.kill();process.join(timeout=2)
                with self.condition:
                    job.update(done=True,error=error)
                    self.last=dict(route=job['route'],pid=job.get('pid'),seconds=round(time.monotonic()-started,3),peak_rss_bytes=peak,error=error)
                    self.active=None;self.jobs.pop(key,None);self.condition.notify_all()
                record(self.directory/'tasks.jsonl','finished',query=key,**self.last)
                if not job['listeners']:job['path'].unlink(missing_ok=True)
                job['path'].with_suffix('.tmp').unlink(missing_ok=True)

    def snapshot(self):
        with self.condition:
            active=self.active
            return dict(queued=len(self.jobs),budget_bytes=self.budget_bytes,active={k:active.get(k) for k in ('route','pid','started','rss_bytes')} if active else None,last=self.last)

    def close(self):
        self.stop.set()
        with self.condition:
            for job in self.jobs.values():
                job.update(done=True,error='监控服务正在停止');job['listeners'].clear()
            self.condition.notify_all()
        self.thread.join(timeout=7)
