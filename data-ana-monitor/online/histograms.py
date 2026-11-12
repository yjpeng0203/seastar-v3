"""Exact, read-only rebinning of persisted waveform metrics; never split coarse bins."""
from collections import OrderedDict
import copy
import threading
import itertools
import json
import weakref
import math
from pathlib import Path
import re
import sqlite3
import sys
import time

import numpy as np

SCALARS = {name: '"'+name+'"' for name in (
    'baseline','noise','amplitude','area','tail_negative','peak_ns','cfd_ns',
    'threshold_ns','fwhm_ns','rise_ns','fall_ns','wave_dt_ns','pulses')}
SPECIAL = {'window_ns','snr','interval_log10_s','trigger_interval_log10_s','pair_dt_ns'}
# Bounded incremental range caches; unrelated charts never wait on a shared query lock.
_NATIVE_CACHE = OrderedDict()
_QUERY_LOCKS = weakref.WeakValueDictionary()
_LOCK_GUARD = threading.Lock()
_CACHE_BYTES = 32*1024*1024


def cache_bytes(accum):
    return sum(sys.getsizeof(v.counts)+(sum(sys.getsizeof(k)+sys.getsizeof(n) for k,n in v.counts.items()) if isinstance(v.counts,dict) else 0) for v in accum.values())


class Counts:
    def __init__(self,lo,hi,bins):
        self.lo,self.hi,self.bins=lo,hi,bins
        self.counts={} if bins is None else np.zeros(bins,dtype=np.int64)
        self.total=self.n=self.under=self.over=self.missing=0
        self.mean=self.m2=0.

    def add(self,values):
        a=np.array([math.nan if v is None else v for v in values],dtype=float)
        valid=np.isfinite(a);self.missing+=int((~valid).sum());a=a[valid]
        self.total+=len(a)
        if self.bins is not None:
            self.under+=int((a<self.lo).sum());self.over+=int((a>self.hi).sum())
            a=a[(a>=self.lo)&(a<=self.hi)]
        if not len(a):return
        if self.bins is None:
            indices,counts=np.unique(np.floor(a),return_counts=True)
            for index,count in zip(indices,counts):
                i=int(index);self.counts[i]=self.counts.get(i,0)+int(count)
        else:self.counts+=np.histogram(a,bins=self.bins,range=(self.lo,self.hi))[0]
        n=len(a);mean=float(a.mean());m2=float(((a-mean)**2).sum())
        delta=mean-self.mean;combined=self.n+n
        self.m2+=m2+delta*delta*self.n*n/combined
        self.mean+=delta*n/combined;self.n=combined

    def result(self):
        hist=({'hist':[[i+.5,n] for i,n in sorted(self.counts.items())]} if self.bins is None
              else {'counts':self.counts.tolist()})
        return {**hist,'n':self.n,'total':self.total,'underflow':self.under,
                'overflow':self.over,'missing':self.missing,'mean':self.mean if self.n else None,
                'sigma':math.sqrt(max(0.,self.m2/self.n)) if self.n else None}


def query_histogram(path,request):
    if isinstance(request,dict):
        identity={k:v for k,v in request.items() if k not in ('max_seq','preview_points')}
        key=(str(Path(path).resolve()),json.dumps(identity,sort_keys=True))
        with _LOCK_GUARD:
            lock=_QUERY_LOCKS.setdefault(key,threading.Lock())
        with lock:return _query_histogram(path,request)
    return _query_histogram(path,request)


def _query_histogram(path,request):
    if not isinstance(request,dict):raise ValueError('需要 JSON 参数')
    metric=request.get('metric')
    if metric not in SCALARS and metric not in SPECIAL:raise ValueError('不支持的分布量')
    run=request.get('run_id')
    if not isinstance(run,str) or not re.fullmatch('[0-9a-f]{32}',run):raise ValueError('无效分析会话')
    native=request.get('binning')=='native'
    if native and metric not in SCALARS and metric not in ('window_ns','snr'):
        raise ValueError('该分布不使用原始单位分箱')
    lo,hi=(0,1) if native else (request.get('min'),request.get('max'))
    bins=1 if native else request.get('bins');seq=request.get('max_seq')
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (lo,hi)):
        raise ValueError('横轴上下限必须是有限数字')
    if not lo<hi or not math.isfinite(hi-lo) or not 0<(hi-lo)/max(1,bins if type(bins) is int else 1):
        raise ValueError('横轴下限必须小于上限，范围不能过小或溢出')
    if type(bins) is not int or not 1<=bins<=4096:raise ValueError('bin 数必须为 1–4096 的整数')
    if type(seq) is not int or seq<0:raise ValueError('无效记录水位')
    population=request.get('population','good')
    if population not in ('all','good'):raise ValueError('无效统计样本类型')
    channels=request.get('channels',[])
    if not isinstance(channels,list) or len(channels)>64:raise ValueError('最多同时统计 64 个通道')
    if metric=='pair_dt_ns' and len(channels)>16:raise ValueError('符合时间差最多同时统计 16 个通道')
    parsed=[]
    for key in channels:
        if not isinstance(key,str) or not re.fullmatch(r'\d{1,3}:\d{1,3}',key):raise ValueError('通道格式无效')
        pair=tuple(int(v) for v in key.split(':'))
        if any(v>255 for v in pair) or key!=str(pair[0])+':'+str(pair[1]):raise ValueError('通道编号无效')
        if pair not in parsed:parsed.append(pair)
    parsed.sort();keys=[str(a)+':'+str(b) for a,b in parsed]
    interval=metric in ('interval_log10_s','trigger_interval_log10_s')
    if interval:population='all'
    if metric=='pair_dt_ns':population='good'
    accum={key:Counts(lo,hi,None if native else bins) for key in keys}
    if metric=='pair_dt_ns':accum={a+'/'+b:Counts(lo,hi,bins) for a,b in itertools.combinations(keys,2)}
    if metric=='trigger_interval_log10_s':accum={'triggers':Counts(lo,hi,bins)}
    cacheable=metric in SCALARS or metric in ('window_ns','snr')
    cache_key=(str(Path(path).resolve()),run,metric,population,tuple(keys),None if native else (lo,hi,bins))
    timeout=60 if native else 15
    deadline=time.monotonic()+timeout
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    try:
        db.execute('BEGIN')
        row=db.execute('SELECT config,state FROM runs WHERE id=?',(run,)).fetchone()
        if not row:raise ValueError('分析会话不存在')
        import json
        config=json.loads(row[0])
        saved=json.loads(row[1]) if row[1] else {}
        if native and saved.get('engine'):
            indexed=db.execute('SELECT max_seq FROM native_index WHERE run=?',(run,)).fetchone()
            engine=saved['engine']
            if indexed and (seq>=engine.get('seq',0) or request.get('allow_latest')):
                preview=request.get('preview_points');preview=preview if type(preview) is int and 32<=preview<=4096 else None
                bounds=[]
                if preview:
                    for key in accum:
                        board,ch=map(int,key.split(':'));params=(run,metric,population,board,ch)
                        low=db.execute('SELECT bin FROM native_bins WHERE run=? AND metric=? AND population=? AND board=? AND channel=? ORDER BY bin LIMIT 1',params).fetchone()
                        high=db.execute('SELECT bin FROM native_bins WHERE run=? AND metric=? AND population=? AND board=? AND channel=? ORDER BY bin DESC LIMIT 1',params).fetchone()
                        if low:bounds.append((low[0],high[0]+1))
                low=min((v[0] for v in bounds),default=0);high=max((v[1] for v in bounds),default=1)
                for key,value in accum.items():
                    board,ch=map(int,key.split(':'))
                    rows=db.execute('SELECT bin,n FROM native_bins WHERE run=? AND metric=? AND population=? AND board=? AND channel=? ORDER BY bin',(run,metric,population,board,ch))
                    if preview:
                        buckets={}
                        for k,n in rows:
                            bucket=min(preview-1,int((k+.5-low)*preview/(high-low)))
                            if bucket not in buckets or n>buckets[bucket][1]:buckets[bucket]=(k,n)
                        value.counts=dict(buckets.values())
                    else:value.counts=dict(rows)
                    channel=engine.get('channels',{}).get(key,{})
                    stats=channel.get('stats',{}).get(population,{}).get(metric,{})
                    value.n=value.total=stats.get('n',0);value.mean=stats.get('mean',0);value.m2=stats.get('m2',0)
                    value.missing=channel.get('n' if population=='all' else 'good',0)-value.n
                actual=engine.get('seq',indexed[0])
                result=_result(metric,run,actual,lo,hi,bins,population,accum,native)
                if preview:result.update(min=low,max=high,bins=high-low,bin_width=1)
                result['index_source']='persistent'
                result.update(requested_max_seq=seq,snapshot_advanced=actual>seq)
                return preview_result(result,request.get('preview_points'))

        db.set_progress_handler(lambda: int(time.monotonic()>deadline),100000)
        where='run=? AND seq<=?';params=[run,seq]
        if cacheable:
            watermark=db.execute('SELECT COALESCE(MAX(seq),0) FROM metrics WHERE run=? AND seq<=?',(run,seq)).fetchone()[0]
            prior=_NATIVE_CACHE.get(cache_key)
            if prior and prior[0]<=watermark:
                after,accum=prior[0],copy.deepcopy(prior[1])
                where+=' AND seq>?';params.append(after)
        if metric!='trigger_interval_log10_s':
            if not parsed:
                return _result(metric,run,seq,lo,hi,bins,population,accum,native)
            where+=' AND ('+' OR '.join('(board=? AND channel=?)' for _ in parsed)+')'
            params.extend(v for pair in parsed for v in pair)
        if metric in SCALARS or metric in ('window_ns','snr'):
            expression=SCALARS.get(metric)
            if metric=='window_ns':expression='("end"-"start")*?';params=[config['sample_ns']]+params
            if metric=='snr':expression='CASE WHEN noise>0 THEN amplitude/noise ELSE 0 END'
            if population=='good':where+=' AND good=1'
            cursor=db.execute('SELECT board,channel,'+expression+' FROM metrics WHERE '+where,params)
            while True:
                rows=cursor.fetchmany(8192)
                if not rows:break
                grouped={}
                for board,ch,value in rows:grouped.setdefault(str(board)+':'+str(ch),[]).append(value)
                for key,values in grouped.items():accum[key].add(values)
                if time.monotonic()>deadline:raise ValueError(f'统计超过 {timeout} 秒，请减少通道或稍后重试')
        else:
            cursor=db.execute('SELECT board,channel,trigger_number,raw_ns,CAST(wave_dt_ns AS INTEGER),segment,cfd_ns,good '
                              'FROM metrics WHERE '+where+' ORDER BY seq',params)
            previous={};event=None;event_channels={};previous_trigger=None
            pending={key:[] for key in accum}
            def flush():
                for key,values in pending.items():
                    if values:accum[key].add(values);values.clear()
            def finish_event():
                for a,b in itertools.combinations(sorted(event_channels,key=lambda k:tuple(map(int,k.split(':')))),2):
                    ta,tb=event_channels[a],event_channels[b]
                    if ta is not None and tb is not None:pending[a+'/'+b].append(tb-ta)
            for index,(board,ch,number,ns,wave_dt,segment,cfd,good) in enumerate(cursor):
                key=str(board)+':'+str(ch);trigger_ns=ns-wave_dt
                current=(segment,number,trigger_ns)
                if metric=='interval_log10_s':
                    prior=previous.get((segment,key));previous[(segment,key)]=ns
                    if prior is not None and ns>prior:pending[key].append(math.log10((ns-prior)*1e-9))
                elif metric=='trigger_interval_log10_s':
                    if current!=event:
                        if previous_trigger is not None and previous_trigger[0]==segment and trigger_ns>previous_trigger[1]:
                            pending['triggers'].append(math.log10((trigger_ns-previous_trigger[1])*1e-9))
                        previous_trigger=(segment,trigger_ns);event=current
                else:
                    if current!=event:
                        finish_event();event=current;event_channels={}
                    event_channels.setdefault(key,cfd if good else None)
                if index%8192==0:
                    flush()
                    if time.monotonic()>deadline:raise ValueError(f'统计超过 {timeout} 秒，请减少通道或稍后重试')
            if metric=='pair_dt_ns':finish_event()
            flush()
        if cacheable and (not prior or prior[0]<=watermark) and cache_bytes(accum)<=_CACHE_BYTES:
            _NATIVE_CACHE[cache_key]=(watermark,accum)
            _NATIVE_CACHE.move_to_end(cache_key)
            while len(_NATIVE_CACHE)>16 or sum(cache_bytes(v[1]) for v in _NATIVE_CACHE.values())>_CACHE_BYTES:_NATIVE_CACHE.popitem(last=False)
        elif cacheable and cache_bytes(accum)>_CACHE_BYTES:_NATIVE_CACHE.pop(cache_key,None)
        return preview_result(_result(metric,run,seq,lo,hi,bins,population,accum,native),request.get('preview_points'))
    except sqlite3.DatabaseError as error:
        raise ValueError('分布查询未完成：'+str(error)) from error
    finally:
        db.close()


def _result(metric,run,seq,lo,hi,bins,population,accum,native=False):
    if native:
        lo=min((min(value.counts) for value in accum.values() if value.counts),default=0)
        hi=max((max(value.counts)+1 for value in accum.values() if value.counts),default=1)
        bins=hi-lo
    return {'metric':metric,'run_id':run,'max_seq':seq,'min':lo,'max':hi,'bins':bins,
            'bin_width':(hi-lo)/bins,'population':population,'binning':'native' if native else 'custom',
            'series':[dict(key=key,**value.result()) for key,value in accum.items()]}


def preview_result(result,points=None):
    """Display envelope retains original bin coordinates/counts; never sums adjacent bins."""
    if type(points) is not int or not 32<=points<=4096:return result
    for item in result['series']:
        hist=item.get('hist')
        if hist is None:
            hist=[[result['min']+(i+.5)*result['bin_width'],n] for i,n in enumerate(item['counts']) if n]
        if len(hist)>points:
            buckets={};scale=points/(result['max']-result['min'])
            for x,n in hist:
                k=min(points-1,int((x-result['min'])*scale))
                if k not in buckets or n>buckets[k][1]:buckets[k]=[x,n]
            hist=list(buckets.values())
        item['hist']=hist;item.pop('counts',None)
    result['preview']=True
    return result
