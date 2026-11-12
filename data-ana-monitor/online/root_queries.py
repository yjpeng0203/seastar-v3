"""Exact range queries on saved scalar ROOT metrics; no waveform reanalysis."""
import csv
import io
import math
from storage import FIELDS

CPP=r'''
#include <TFile.h>
#include <TTreeReader.h>
#include <TTreeReaderValue.h>
#include <cmath>
#include <algorithm>
#include <memory>
#include <map>
#include <vector>
#include <string>
std::vector<double> seastar_range(const std::string& path,const std::string& metric,
  const std::vector<int>& channels,int board,bool good,double lo,double hi,int bins,long long max_seq) {
 TFile f(path.c_str(),"READ");TTreeReader r("metrics",&f);
 TTreeReaderValue<int> ch(r,"channel"),bd(r,"board"),ok(r,"good"),seg(r,"segment");
 TTreeReaderValue<Long64_t> event(r,"event"),raw(r,"raw_ns"),trigger(r,"trigger_ns");
 TTreeReaderValue<double> cfd(r,"cfd_ns");
 bool interval=metric=="interval_log10_s",triggers=metric=="trigger_interval_log10_s",pair=metric=="pair_dt_ns";
 bool special=interval||triggers||pair,integer=metric=="pulses";
 std::unique_ptr<TTreeReaderValue<double>> v;
 std::unique_ptr<TTreeReaderValue<int>> vi;
 if(!special) {if(integer) vi.reset(new TTreeReaderValue<int>(r,metric.c_str()));else v.reset(new TTreeReaderValue<double>(r,metric.c_str()));}
 int groups=triggers?1:pair?channels.size()*(channels.size()-1)/2:channels.size(),stride=bins+7;
 std::vector<double> out(groups*stride);
 auto add=[&](int k,double x) {double* a=out.data()+k*stride;
  if(!std::isfinite(x)){++a[bins+4];return;}++a[bins+1];
  if(x<lo){++a[bins+2];return;}if(x>hi){++a[bins+3];return;}
  int i=std::min(bins-1,int((x-lo)*bins/(hi-lo)));++a[i];
  double n=++a[bins],delta=x-a[bins+5];a[bins+5]+=delta/n;a[bins+6]+=delta*(x-a[bins+5]);};
 std::map<std::pair<int,int>,long long> previous;
 long long prior_event=-1,last_trigger=-1;int last_segment=-1;
 std::map<int,double> event_channels;
 auto finish=[&](){int k=0;for(size_t a=0;a<channels.size();++a)for(size_t b=a+1;b<channels.size();++b,++k)
  if(event_channels.count(channels[a])&&event_channels.count(channels[b]))add(k,event_channels[channels[b]]-event_channels[channels[a]]);};
 while(r.Next()) {
  if(r.GetCurrentEntry()+*event+2>max_seq)break;
  if(*bd!=board)continue;
  auto it=std::find(channels.begin(),channels.end(),*ch);int k=it-channels.begin();
  if(pair&&*event!=prior_event){finish();event_channels.clear();prior_event=*event;}
  if(triggers) {if(*event!=prior_event){if(last_segment==*seg&&*trigger>last_trigger)add(0,std::log10((*trigger-last_trigger)*1e-9));
    last_segment=*seg;last_trigger=*trigger;prior_event=*event;}continue;}
  if(it==channels.end())continue;
  if(interval) {auto key=std::make_pair(*seg,*ch);auto p=previous.find(key);if(p!=previous.end()&&*raw>p->second)add(k,std::log10((*raw-p->second)*1e-9));previous[key]=*raw;}
  else if(pair) {if(*ok&&std::isfinite(*cfd))event_channels.emplace(*ch,*cfd);}
  else if(!good||*ok)add(k,integer?double(**vi):**v);
 }
 if(pair)finish();return out;
}
'''


NATIVE_CPP=r'''
#include <algorithm>
#include <TFile.h>
#include <TTreeReader.h>
#include <TTreeReaderValue.h>
#include <map>
#include <vector>
#include <cmath>
#include <memory>
#include <limits>
struct SeaNativeQuery {std::vector<double> rows,stats;double low=0,high=1;};
SeaNativeQuery seastar_native_range(const std::string& path,const std::string& metric,
 const std::vector<int>& channels,int board,bool good,long long max_seq,int points){
 TFile f(path.c_str(),"READ");TTreeReader r("metrics",&f);
 TTreeReaderValue<int> ch(r,"channel"),bd(r,"board"),ok(r,"good");TTreeReaderValue<Long64_t> event(r,"event");
 std::unique_ptr<TTreeReaderValue<double>> v;
 std::unique_ptr<TTreeReaderValue<int>> vi;
 if(metric=="pulses")vi.reset(new TTreeReaderValue<int>(r,metric.c_str()));else v.reset(new TTreeReaderValue<double>(r,metric.c_str()));
 struct Stats{long long n=0,missing=0;double mean=0,m2=0;std::map<long long,long long> counts;};
 std::vector<Stats> values(channels.size());long long allocated=0;
 while(r.Next()){
  if(r.GetCurrentEntry()+*event+2>max_seq)break;if(*bd!=board||(good&&!*ok))continue;
  auto found=std::find(channels.begin(),channels.end(),*ch);if(found==channels.end())continue;
  auto &s=values[found-channels.begin()];double x=vi?double(**vi):**v;
  if(!std::isfinite(x)){++s.missing;continue;}
  ++s.n;double delta=x-s.mean;s.mean+=delta/s.n;s.m2+=delta*(x-s.mean);
  long long key=(long long)std::floor(x);auto it=s.counts.emplace(key,0);
  if(it.second&&++allocated>1000000)throw std::runtime_error("Sparse ROOT query exceeds occupied-bin memory budget; select fewer channels or an explicit range");
  ++it.first->second;
 }
 SeaNativeQuery out;double lo=INFINITY,hi=-INFINITY;
 for(auto&s:values)if(!s.counts.empty()){lo=std::min(lo,double(s.counts.begin()->first));hi=std::max(hi,double(s.counts.rbegin()->first)+1);}
 if(std::isfinite(lo)){out.low=lo;out.high=hi;}
 for(size_t group=0;group<values.size();++group){auto&s=values[group];
  out.stats.insert(out.stats.end(),{double(s.n),double(s.missing),s.n?s.mean:NAN,s.n?std::sqrt(std::max(0.,s.m2/s.n)):NAN});
  if(points){std::map<int,std::pair<long long,long long>> preview;
   for(auto p:s.counts){int k=std::min(points-1,int((p.first+.5-out.low)*points/(out.high-out.low)));
    auto it=preview.find(k);if(it==preview.end()||p.second>it->second.second)preview[k]=p;}
   for(auto p:preview)out.rows.insert(out.rows.end(),{double(group),p.second.first+.5,double(p.second.second)});
  }else for(auto p:s.counts)out.rows.insert(out.rows.end(),{double(group),p.first+.5,double(p.second)});
 }
 return out;
}
'''


def native_histogram(path, request, snapshot):
    import ROOT as R
    from histograms import SCALARS
    metric=request.get('metric');seq=request.get('max_seq',snapshot['seq'])
    if metric not in SCALARS and metric not in ('window_ns','snr'):raise ValueError('原始分箱指标无效')
    if request.get('run_id')!=snapshot.get('run_id'):raise ValueError('分析会话已变化，请刷新')
    if type(seq) is not int or seq<0:raise ValueError('记录水位无效')
    keys=request.get('channels',[]);allowed={v['key'] for v in snapshot.get('channels',[])}
    if not isinstance(keys,list) or any(k not in allowed for k in keys):raise ValueError('通道无效')
    keys=sorted(set(keys),key=lambda k:tuple(map(int,k.split(':'))))
    population=request.get('population','good')
    if population not in ('all','good'):raise ValueError('样本类型无效')
    points=request.get('preview_points');points=points if type(points) is int and 32<=points<=4096 else 0
    if not hasattr(R,'seastar_native_range') and not R.gInterpreter.Declare(NATIVE_CPP):raise RuntimeError('不能初始化稀疏原始分箱查询')
    R.seastar_native_range.__release_gil__=True
    result=R.seastar_native_range(str(path),metric,[int(k.split(':')[1]) for k in keys],snapshot['source']['board_id'],population=='good',seq,points)
    series=[]
    for i,key in enumerate(keys):
        n,missing,mean,sigma=(float(result.stats[i*4+j]) for j in range(4))
        series.append(dict(key=key,hist=[],n=int(n),total=int(n),underflow=0,overflow=0,missing=int(missing),mean=mean if n else None,sigma=sigma if n else None))
    for i in range(0,len(result.rows),3):series[int(result.rows[i])]['hist'].append([float(result.rows[i+1]),int(result.rows[i+2])])
    return dict(metric=metric,run_id=request['run_id'],max_seq=min(seq,snapshot['seq']),min=float(result.low),max=float(result.high),bins=int(result.high-result.low),bin_width=1,
                population=population,series=series,binning='native',preview=bool(points),index_source='streaming sparse ROOT query')


def histogram(path,request,snapshot,max_bins=4096):
    import ROOT as R
    from histograms import SCALARS,SPECIAL
    if request.get('binning')=='native':return native_histogram(path,request,snapshot)
    metric=request.get('metric');lo=request.get('min');hi=request.get('max');bins=request.get('bins');seq=request.get('max_seq',snapshot['seq'])
    if metric not in SCALARS and metric not in SPECIAL:raise ValueError('指标无效')
    if any(type(v) not in (float,int) or not math.isfinite(v) for v in (lo,hi)) or not lo<hi:raise ValueError('横轴范围无效')
    if type(bins) is not int or not 1<=bins<=max_bins:raise ValueError('自定义 bin 数必须为 1–4096')
    if not math.isfinite(hi-lo) or (hi-lo)/bins<=0:raise ValueError('横轴范围过小或溢出')
    if type(seq) is not int or seq<0:raise ValueError('记录水位无效')
    channels=sorted(set(request.get('channels',[])),key=lambda k:tuple(map(int,k.split(':'))))
    if not hasattr(R,'seastar_range') and not R.gInterpreter.Declare(CPP):raise RuntimeError('不能编译标量查询')
    board=snapshot['source']['board_id'];ids=[int(k.split(':')[1]) for k in channels]
    groups=1 if metric=='trigger_interval_log10_s' else len(ids)*(len(ids)-1)//2 if metric=='pair_dt_ns' else len(ids)
    if groups*(bins+7)*8>64*1024*1024:raise ValueError('直方图稠密数组超过 64 MiB 预算，请选择原始稀疏分箱或更小的指定范围')
    result=R.seastar_range(str(path),metric,ids,board,request.get('population')=='good',lo,hi,bins,seq)
    keys=channels
    if metric=='trigger_interval_log10_s':keys=['triggers']
    elif metric=='pair_dt_ns':keys=[a+'/'+b for i,a in enumerate(channels) for b in channels[i+1:]]
    series=[]
    for i,key in enumerate(keys):
        a=list(result[i*(bins+7):(i+1)*(bins+7)]);n=int(a[bins])
        series.append(dict(key=key,counts=[int(v) for v in a[:bins]],n=n,total=int(a[bins+1]),underflow=int(a[bins+2]),
          overflow=int(a[bins+3]),missing=int(a[bins+4]),mean=a[bins+5] if n else None,sigma=math.sqrt(max(0,a[bins+6]/n)) if n else None))
    return dict(metric=metric,run_id=request['run_id'],max_seq=min(seq,snapshot['seq']),min=lo,max=hi,bins=bins,bin_width=(hi-lo)/bins,
      population=request.get('population','good'),series=series,binning='custom')


def export_csv(path,run,config,kernel,channel,max_seq):
    import ROOT as R
    f=R.TFile.Open(str(path));tree=f.Get('metrics')
    output=io.StringIO();writer=csv.writer(output)
    writer.writerow(('run_id',)+FIELDS+('sample_ns','adc_mv','resistance_ohm','kernel_sha256','analysis_sha256'))
    yield ('\ufeff'+output.getvalue()).encode();output.seek(0);output.truncate()
    tree.SetBranchStatus('*',0)
    needed=(set(FIELDS)-{'seq','position','polarity','baseline_points'})|{'event'}
    for name in needed:tree.SetBranchStatus(name,1)
    try:
        for i in range(tree.GetEntries()):
            tree.GetEntry(i);seq=i+int(tree.event)+2
            if max_seq is not None and seq>max_seq:break
            if channel and channel!=f'{int(tree.board)}:{int(tree.channel)}':continue
            values=[]
            for name in FIELDS:
                value=seq if name=='seq' else int(tree.event) if name=='position' else None if name in ('polarity','baseline_points') else getattr(tree,name)
                if isinstance(value,float) and not math.isfinite(value):value=None
                values.append(value)
            writer.writerow([run]+values+[config['sample_ns'],config['adc_mv'],config['resistance'],kernel,kernel])
            if output.tell()>65536:
                yield output.getvalue().encode();output.seek(0);output.truncate()
        if output.tell():yield output.getvalue().encode()
    finally:f.Close()
