"""Fast closed-ROOT replay: reuse validated analysis, build a display cache once."""
from dataclasses import asdict
from pathlib import Path
import csv
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

import numpy as np
from analysis import FLAG_NAMES, METRICS, dumps
from histograms import preview_result

ROOT_LOCK=threading.Lock()
PROJECT=Path(__file__).resolve().parents[2]


def matching_analysis(source,config,kernel):
    path=Path(source)
    for meta in sorted((path.parent.parent/'results').glob('analysis_*/analysis.json'),reverse=True):
        try:
            m=json.loads(meta.read_text())
            recorded=next(v for k,v in m['input_stats'].items() if Path(k).name==path.name)
            st=path.stat()
            if (m.get('status')=='complete' and not m.get('sampled') and m.get('config')==config and
                m.get('kernel_sha256')==kernel and recorded==dict(size=st.st_size,mtime_ns=st.st_mtime_ns) and
                (meta.parent/'root/analysis.root').exists()):return meta.parent,m
        except (OSError,ValueError,KeyError,StopIteration):continue
    return None,None


class ReplayCache:
    def __init__(self,data_dir,state_dir,config,source,read_gate=None,build_gate=None):
        self.data_dir,self.state_dir=Path(data_dir),Path(state_dir)
        self.state_dir.mkdir(parents=True,exist_ok=True)
        self.config=config;self.source=source;self.sources=[source]
        self.input=self.data_dir/source['files'][0]['name']
        self.signature=(self.input.stat().st_size,self.input.stat().st_mtime_ns)
        self.kernel=hashlib.sha256((Path(__file__).parent/'adaptive_waveform.h').read_bytes()).hexdigest()
        self.identity=hashlib.sha256(b''.join((Path(__file__).parent/name).read_bytes() for name in ('server.py','fleet.py','replay_cache.py','root_queries.py','rates.py','stat_errors.py','time_bins.py','raw_trends.py','histograms.py','time_reference.py'))).hexdigest()
        self.run_id=hashlib.sha256((str(self.input)+str(self.signature)+dumps(asdict(config))+self.kernel).encode()).hexdigest()[:32]
        self.state_dir=self.state_dir/"closed"/self.run_id
        self.state_dir.mkdir(parents=True,exist_ok=True)
        self.commands=queue.Queue();self.stop=threading.Event();self.paused=False
        self.read_gate,self.build_gate=read_gate,build_gate
        self.process=None;self.analysis=None
        self.cached={'run_id':self.run_id,'status':'starting','channels':[],'sources':[source],'source':source,
          'config':asdict(config),'input_mode':'replay','data_dir':str(self.data_dir),'state_dir':str(self.state_dir),
          'time_basis':'独立串的相对电子学时间；北京时间仅作为分钟级参考。','alerts':[],
          'follow':False,'paused':False,'waves':0,'triggers':0,'seq':0,'backlog':0,'backlog_unit':'events',
          'file':source['files'][0]['name'],'transfer':None,'kernel_sha256':self.kernel,'analysis_sha256':self.kernel,
          'server_sha256':self.identity,'cache_mode':'building','segment':0,'segments':1,'span_s':0}
        self.thread=threading.Thread(target=self.work,daemon=True)

    def start(self):
        self.thread.start()
        threading.Thread(target=self.control_loop,daemon=True).start()
    def control_loop(self):
        import signal
        while not self.stop.is_set():
            try:command=self.commands.get(timeout=.2)
            except queue.Empty:continue
            if command.get('action') in ('pause','resume'):
                self.paused=command['action']=='pause'
                if self.process and self.process.poll() is None:
                    self.process.send_signal(signal.SIGSTOP if self.paused else signal.SIGCONT)
    def snapshot(self):
        from time_reference import reference
        st=self.input.stat()
        if (st.st_size,st.st_mtime_ns)!=self.signature:
            return dict(self.cached,status='error',error='ROOT 文件已经变化；请重建当前串显示缓存，旧结果不再作为当前数据。')
        return dict(self.cached,paused=self.paused,status='paused' if self.paused else self.cached['status'],beijing_reference=reference(self.data_dir,self.source,self.cached.get('origin_ns')))
    def close(self):
        self.stop.set()
        if self.process and self.process.poll() is None:
            import signal
            if self.paused:self.process.send_signal(signal.SIGCONT)
            self.process.terminate()
        if self.thread.ident:self.thread.join(timeout=30)

    def work(self):
        try:
            snapshot_path=self.state_dir/'snapshot.json'
            if snapshot_path.exists():
                saved=json.loads(snapshot_path.read_text())
                if saved.get('display_schema') in (2,3) and saved.get('run_id')==self.run_id and Path(saved.get('analysis_path','')) .joinpath('root/analysis.root').exists() and all((self.state_dir/(m+'.npz')).exists() or (self.state_dir/'native-bin-cache'/m/'bounds.json').exists() for m in METRICS):
                    self.analysis=Path(saved['analysis_path'])
                    metadata=json.loads((self.analysis/'analysis.json').read_text())
                    self.cached=dict(saved,state_dir=str(self.state_dir),analysis_sha256=metadata.get('backend_sha256',self.kernel),server_sha256=self.identity)
            if self.analysis is None:
                directory,metadata=matching_analysis(self.input,asdict(self.config),self.kernel)
                if directory is None:
                    local=self.state_dir/'analysis'
                    try:
                        candidate=json.loads((local/'analysis.json').read_text())
                        if candidate.get('status')=='complete' and candidate.get('config')==asdict(self.config) and (local/'root/analysis.root').exists():
                            directory,metadata=local,candidate
                    except (OSError,ValueError):pass
                if directory is None:directory,metadata=self.build()
                if self.stop.is_set():return
                self.analysis=directory
                with ROOT_LOCK:self.cached=self.load(directory,metadata)
                if (self.input.stat().st_size,self.input.stat().st_mtime_ns)!=self.signature:raise ValueError('读取期间 ROOT 文件发生变化，缓存未发布')
                snapshot_path.with_suffix('.tmp').write_text(dumps(self.cached))
                snapshot_path.with_suffix('.tmp').replace(snapshot_path)
            for channel in self.cached.get('channels',[]):
                channel.pop('trend',None);channel.pop('recent',None)
            for name in ('trigger_trend','trigger_recent','trend_bin_note'):self.cached.pop(name,None)
            self.cached['trend_mode']='raw_observations'
            self.stop.wait()
        except Exception as error:
            if self.stop.is_set():return
            self.cached=dict(self.cached,status='error',error=str(error),alerts=[{'level':'error','text':str(error)}])

    def build(self):
        sys.path.insert(0,str(PROJECT/'organizer'))
        from batch_analysis import build_analyzer,CONFIG_FIELDS
        from contextlib import nullcontext
        with self.build_gate if self.build_gate else nullcontext():exe,backend=build_analyzer()
        directory=self.state_dir/'analysis';(directory/'root').mkdir(parents=True,exist_ok=True);(directory/'tables').mkdir(exist_ok=True)
        cfg=asdict(self.config);config_file=directory/'config.txt'
        config_file.write_text(' '.join(str(cfg[k]) for k in CONFIG_FIELDS))
        (directory/'analysis.json').write_text(dumps({'status':'running','config':cfg}))
        output=directory/'root/analysis.root'
        if output.exists():output.unlink() # Only our incomplete, deterministic runtime cache.
        args=[str(exe),str(self.input),str(output),str(directory/'tables'),str(config_file),str(self.source['board_id']),'0',str(directory/'counts.json')]
        self.cached=dict(self.cached,status='replaying',cache_mode='native C++ batch')
        # Each subprocess is single-threaded; the shared read gate is used by incremental DAT readers.
        env=os.environ.copy()
        env.setdefault('OMP_NUM_THREADS','1');env.setdefault('OPENBLAS_NUM_THREADS','1')
        if self.read_gate:
            env['SEASTAR_READ_GATE']=str(self.state_dir.parents[3]/'rotational-read.lock')
        self.process=subprocess.Popen(args,env=env,start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
        if self.paused:
            import signal
            self.process.send_signal(signal.SIGSTOP)
        with (directory/'analysis.log').open('w') as log:
            for line in self.process.stdout:
                log.write(line);log.flush()
                match=re.search(r'events=(\d+)/(\d+) waves=(\d+) good=(\d+)',line)
                if match:
                    events,total,waves,good=map(int,match.groups())
                    speed=re.search(r'waves/s=([0-9.eE+\-]+)',line)
                    rate=float(speed[1]) if speed else 0
                    self.cached=dict(self.cached,triggers=events,wave_events=events,waves=waves,good=good,seq=events+waves,backlog=total-events,analysis_waves_per_s=rate,eta_seconds=(total-events)*waves/events/rate if rate and events else None)
                if self.stop.is_set():self.process.terminate();break
        self.process.stdout.close()
        if self.process.wait()!=0:raise RuntimeError('C++ 历史分析未完成，详见 '+str(directory/'analysis.log'))
        m=json.loads((directory/'counts.json').read_text())
        m.update(status='complete',backend_sha256=backend,config=cfg,kernel_sha256=self.kernel,group=self.source['string_group'],board=self.source['board_id'],
          source_root=str(self.input),source_csv=[],input_stats={str(self.input):dict(size=self.signature[0],mtime_ns=self.signature[1])})
        (directory/'analysis.json').write_text(dumps(m))
        return directory,m

    def load(self,directory,m):
        import ROOT as R
        R.gROOT.SetBatch(True)
        sys.path.insert(0,str(PROJECT/'organizer'))
        from figure_data import export_plot_data,read_histogram,native_rows,native_axis
        # Scientific figure revisions already contain exact full-range native bins.
        figures=[]
        for meta in (self.input.parent.parent/'results').glob('figures_*/analysis.json'):
            try:
                fm=json.loads(meta.read_text())
                if (fm.get('histogram_storage_schema')=='native-bins-bounded-v2' and fm.get('config')==asdict(self.config) and fm.get('waves')==m['waves'] and fm.get('kernel_sha256')==self.kernel and
                    any(Path(k).name==self.input.name and v==dict(size=self.signature[0],mtime_ns=self.signature[1]) for k,v in fm.get('input_stats',{}).items())):
                    focused=meta.parent/'root/focused.root'
                    if focused.exists():figures.append(focused)
            except (OSError,ValueError):pass
        if figures:focused=sorted(figures)[-1]
        else:
            sys.path.insert(0,str(PROJECT/'organizer'))
            from figure_data import export_plot_data
            target=self.state_dir/'native-figures';export_plot_data(directory,target,include_readback=False)
            focused=target/'root/focused.root'
        def table(name):
            with (directory/'tables'/name).open() as stream:yield from csv.DictReader(stream)
        f=R.TFile.Open(str(directory/'root/analysis.root'))
        native=R.TFile.Open(str(focused));channels={}
        def graph(name):
            g=f.Get(name)
            return [[float(g.GetPointX(i)),float(g.GetPointY(i))] for i in range(g.GetN())] if g else []
        for row in table('channel_statistics.csv'):
            ch=int(row['channel']);key=str(m['board'])+':'+str(ch)
            c=channels.setdefault(ch,dict(key=key,n=0,good=0,flags={},stats={'all':{},'good':{}},examples=[],latest=None,
                drift_adc=None,zero_intervals=0,negative_intervals=0,scatter=[],aligned=[],density=[],density_count=0,density_outside=0,shape_changes=0,polarities={}))
            name=row['metric']
            if name not in METRICS:continue
            c['stats'][row['population']][name]={k:(int(row[k]) if k=='n' else float(row[k]) if row[k] else None) for k in ('n','mean','sigma','min','max')}
        for name in METRICS:
            cache_dir=self.state_dir/'native-bin-cache'/name;cache_dir.mkdir(parents=True,exist_ok=True)
            bounds=None
            for ch,c in channels.items():
                for pop in ('all','good'):
                    h=read_histogram(native,f'full_{"accepted" if pop=="good" else pop}_{name}_ch{ch}')
                    axis=native_axis(h)
                    hist=native_rows(h)
                    d=c['stats'][pop][name];d.update(bin_width=1,bin_origin=axis.GetXmin())
                    d['hist']=native_rows(h,256).tolist();d['preview']=True
                    bounds=[axis.GetXmin(),axis.GetXmax(),axis.GetNbins()]
                    if int(hist[:,1].sum())!=d['n'] or int(h.GetEntries())!=d['n']:raise ValueError('完整分箱计数不守恒：'+name)
                    temporary=cache_dir/(pop+'_'+str(ch)+'.tmp.npy')
                    np.save(temporary,hist,allow_pickle=False);temporary.replace(cache_dir/(pop+'_'+str(ch)+'.npy'))
                    del hist,h
            temporary=cache_dir/'bounds.tmp';temporary.write_text(json.dumps(bounds));temporary.replace(cache_dir/'bounds.json')
        for row in table('quality.csv'):
            c=channels[int(row['channel'])];c['n']=int(row['waves']);c['good']=int(row['good']);c['density_count']=int(row['density_waveforms'])
            c['flags']={name:int(row['flag_'+str(bit)]) for bit,name in FLAG_NAMES.items()}
            c['zero_intervals']=int(row['zero_intervals']);c['negative_intervals']=int(row['negative_intervals'])
        # Time plots are fetched from individual metrics, never from profile tables.
        segment=int(m.get('segments',1))-1
        def hist_snapshot(h):
            return dict(n=int(h.GetEntries()),mean=h.GetMean() if h.GetEntries() else None,sigma=h.GetStdDev() if h.GetEntries() else None,
              min=h.GetXaxis().GetXmin(),max=h.GetXaxis().GetXmax(),bin_width=h.GetBinWidth(1),bin_origin=h.GetXaxis().GetXmin(),
              hist=[[h.GetBinCenter(i),int(h.GetBinContent(i))] for i in range(1,h.GetNbinsX()+1) if h.GetBinContent(i)])
        for ch,c in channels.items():
            c['intervals']=hist_snapshot(f.Get('interval_ch'+str(ch)))
            c['aligned']=graph('aligned_ch'+str(ch))
            averages=[k.GetName() for k in f.GetListOfKeys() if k.GetName().startswith('average_ch'+str(ch)+'_n')]
            avg=graph(averages[0]) if averages else [];c['average']=[v for _,v in avg];c['length']=len(avg);c['wave_count']=c['good'] if len(averages)==1 else None
            h=f.Get('density_ch'+str(ch));c['density']=[[i,j,int(sum(h.GetBinContent(i+1,k+1) for k in range(j*2,j*2+2)))] for i in range(128) for j in range(64)]
            c['density']=[v for v in c['density'] if v[2]]
            from analysis import Analyzer
            analyzer=Analyzer(self.config,self.state_dir/'build')
            for i in range(8):
                points=graph(f'example_ch{ch}_{i}')
                if not points:break
                samples=np.array([round(y) for _,y in points],dtype=np.uint16);pulse=analyzer(samples)
                c['examples'].append(dict(pulse,samples=samples.tolist(),t=None,wave_id=0,example_time_unknown=True))
            c['latest']=c['examples'][-1] if c['examples'] else None
        pair_dt={};pairs={};hits={};co=f.Get('coincidence')
        for a in channels:
            hits[f'{m["board"]}:{a}']=int(co.GetBinContent(a+1,a+1))
            for b in channels:
                if a>=b:continue
                key=f'{m["board"]}:{a}/{m["board"]}:{b}';pairs[key]=int(co.GetBinContent(a+1,b+1))
                h=f.Get(f'pair_{a}_{b}')
                if h:pair_dt[key]=hist_snapshot(h)
        multi=f.Get('multiplicity');multiplicity={str(round(multi.GetBinCenter(i))):int(multi.GetBinContent(i)) for i in range(1,multi.GetNbinsX()+1) if multi.GetBinContent(i)}
        if not hasattr(R,'seastar_scatter'):
            R.gInterpreter.Declare(r"""
#include <deque>
#include <array>
#include <algorithm>
#include <vector>
#include <map>
#include <TTreeReader.h>
#include <TTreeReaderValue.h>
std::vector<double> seastar_scatter(TTree* t) {
 TTreeReader r(t);r.SetEntriesRange(std::max(0LL,t->GetEntries()-200000),t->GetEntries());
 TTreeReaderValue<int> ch(r,"channel"),good(r,"good");TTreeReaderValue<double>a(r,"amplitude"),q(r,"area"),time(r,"t");
 std::map<int,std::deque<std::array<double,3>>> out;
 while(r.Next())if(*good && r.GetCurrentEntry()%10==0) {auto &v=out[*ch];v.push_back({*a,*q,*time});if(v.size()>1000)v.pop_front();}
 std::vector<double> flat;for(auto &v:out)for(auto &p:v.second){flat.push_back(v.first);flat.insert(flat.end(),p.begin(),p.end());}return flat;
}
""")
        tree=f.Get('metrics');scatter=R.seastar_scatter(tree)
        for i in range(0,len(scatter),4):channels[int(scatter[i])]['scatter'].append([float(v) for v in scatter[i+1:i+4]])
        tree.GetEntry(0);origin=int(tree.trigger_ns);tree.GetEntry(tree.GetEntries()-1);last=int(tree.trigger_ns)
        snapshot=dict(self.cached,display_schema=3,analysis_sha256=m.get('backend_sha256',self.kernel),status='complete',cache_mode='validated analysis cache',channels=list(channels.values()),
          triggers=m['events'],wave_events=m['events']-m.get('empty_saved_events',0),waves=m['waves'],seq=m['waves']+m['events'],
          good=m['good'],good_fraction=m['good']/m['waves'],backlog=0,span_s=sum(m.get('segment_spans_s',[])),relative_s=m.get('segment_spans_s',[0])[-1],version='adaptive-1.2',segment=segment,
          segments=m.get('segments',1),origin_ns=str(last-int(float(tree.t)*1e9)),last_ns=str(last),
          trigger_intervals=hist_snapshot(f.Get('trigger_interval')),
          pair_dt=pair_dt,pairs=pairs,event_hits=hits,multiplicity=multiplicity,last_event_provisional=False,
          empty_triggers=m.get('empty_saved_events',0),number_gaps=None,header_backsteps=0,clock_backsteps=segment,
          bad_bytes=0,pending_bytes=0,orphan_waves=0,analysis_waves_per_s=m['waves']/m.get('seconds',1),
          database_bytes=(directory/'root/analysis.root').stat().st_size,published_host_unix_s=time.time(),analysis_path=str(directory),
          trend_mode='raw_observations')
        f.Close();native.Close();return snapshot

    def rates(self,request):
        if self.analysis is None:raise ValueError('标量分析尚未完成')
        from rates import root_rates
        return root_rates(self.analysis/'root/analysis.root',request,self.cached)

    def time_trend(self,request):
        if self.analysis is None:raise ValueError('标量分析尚未完成')
        from time_bins import root_time_bins
        with ROOT_LOCK:return root_time_bins(self.analysis/'root/analysis.root',self.state_dir,request,self.cached)

    def raw_trend(self,request):
        if self.analysis is None:raise ValueError('标量分析尚未完成')
        from raw_trends import root_page
        with ROOT_LOCK:return root_page(self.analysis/'root/analysis.root',self.state_dir,request,self.cached)

    def query_target(self,request):
        if self.analysis is None or self.cached.get('status')!='complete':raise ValueError('首次分析缓存尚未完成，进度见串任务卡片')
        if request.get('run_id')!=self.run_id:raise ValueError('分析会话已变化，请刷新')
        return dict(kind='root',path=str(self.analysis/'root/analysis.root'),state_dir=str(self.state_dir),snapshot=self.snapshot())

    def histogram(self,request):
        if self.snapshot().get('status')=='error':raise ValueError(self.snapshot()['error'])
        with ROOT_LOCK:return query_saved_histogram(self.query_target(request),request)

    def custom_histogram(self,request,max_bins=4096):
        from root_queries import histogram
        if self.analysis is None:raise ValueError('首次分析尚未完成，进度见串任务卡片')
        with ROOT_LOCK:return preview_result(histogram(self.analysis/'root/analysis.root',request,self.cached,max_bins=max_bins),request.get('preview_points'))

    def export_csv(self,run_id,channel=None,max_seq=None):
        if run_id!=self.run_id:raise ValueError('分析会话不存在')
        from root_queries import export_csv
        yield from export_csv(self.analysis/'root/analysis.root',run_id,asdict(self.config),self.kernel,channel,max_seq)


def _preview_rows(hist, low, high, points):
    """Bounded two-pass envelope selection on mmap/NumPy rows, before creating Python lists."""
    if type(points) is not int or not 32<=points<=4096 or len(hist)<=points:return hist.tolist()
    maxima=np.full(points,-np.inf);best=np.full(points,-1,dtype=np.int64)
    scale=points/(high-low)
    for start in range(0,len(hist),65536):
        chunk=hist[start:start+65536];buckets=np.clip(((chunk[:,0]-low)*scale).astype(np.int64),0,points-1)
        np.maximum.at(maxima,buckets,chunk[:,1])
    for start in range(0,len(hist),65536):
        chunk=hist[start:start+65536];buckets=np.clip(((chunk[:,0]-low)*scale).astype(np.int64),0,points-1)
        winners=np.flatnonzero(chunk[:,1]==maxima[buckets])
        kinds,first=np.unique(buckets[winners],return_index=True)
        unseen=best[kinds]<0;best[kinds[unseen]]=start+winners[first[unseen]]
    return hist[best[best>=0]].tolist()


def query_saved_histogram(target,request):
    """Standalone ROOT query entry point for an isolated, cancellable query process."""
    from root_queries import histogram,native_histogram
    snapshot=target['snapshot'];metric=request.get('metric');keys=request.get('channels',[]);pop=request.get('population','good')
    if request.get('run_id')!=snapshot.get('run_id'):raise ValueError('分析会话已变化，请刷新')
    if metric not in METRICS and metric not in ('interval_log10_s','trigger_interval_log10_s','pair_dt_ns'):raise ValueError('不支持的指标')
    allowed={c['key'] for c in snapshot.get('channels',[])}
    if not isinstance(keys,list) or any(k not in allowed for k in keys):raise ValueError('通道无效')
    if pop not in ('all','good'):raise ValueError('样本类型无效')
    seq=request.get('max_seq',snapshot['seq'])
    if type(seq) is not int or seq<0:raise ValueError('记录水位无效')
    if metric not in METRICS or request.get('binning')!='native':return preview_result(histogram(target['path'],request,snapshot),request.get('preview_points'))
    if seq<snapshot['seq']:return native_histogram(target['path'],request,snapshot)
    directory=Path(target['state_dir']);folder=directory/'native-bin-cache'/metric
    legacy=None
    if (folder/'bounds.json').is_file():lo,hi,bins=json.loads((folder/'bounds.json').read_text())
    elif (directory/(metric+'.npz')).is_file():
        legacy=np.load(directory/(metric+'.npz'));lo,hi,bins=legacy['bounds']
    else:return native_histogram(target['path'],request,snapshot)
    series=[]
    try:
        for c in snapshot['channels']:
            if c['key'] not in keys:continue
            key=pop+'_'+c['key'].split(':')[1]
            hist=legacy[key] if legacy is not None else np.load(folder/(key+'.npy'),mmap_mode='r',allow_pickle=False)
            d=c['stats'][pop][metric]
            series.append(dict(key=c['key'],hist=_preview_rows(hist,float(lo),float(hi),request.get('preview_points')),n=d['n'],total=d['n'],underflow=0,overflow=0,
                               missing=c['n' if pop=='all' else 'good']-d['n'],mean=d['mean'],sigma=d['sigma']))
            del hist
    finally:
        if legacy is not None:legacy.close()
    return dict(metric=metric,run_id=snapshot['run_id'],max_seq=snapshot['seq'],min=float(lo),max=float(hi),bins=int(bins),bin_width=1,population=pop,series=series,
                binning='native',preview=type(request.get('preview_points')) is int and 32<=request['preview_points']<=4096,index_source='persistent ROOT mmap cache')
