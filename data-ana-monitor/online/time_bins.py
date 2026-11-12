"""Explicit, user-selected temporal means; raw observations remain separately queryable."""
import gzip
import hashlib
import json
import math
from pathlib import Path
from raw_trends import settings,root_page

CPP=r'''
#include <TFile.h>
#include <TTreeReader.h>
#include <TTreeReaderValue.h>
#include <map>
#include <unordered_map>
#include <algorithm>
#include <array>
#include <vector>
#include <cmath>
#include <limits>
struct SeaMeanBin {std::array<double,7> sum{},mean{},m2{};std::array<long long,7> n{};};
struct SeaTimeBins {std::vector<double> rows,hist,stats,bin_statistics;long long observations=0;};
struct SeaDistribution {
 long long n=0,missing=0,under=0,over=0;double mean=0,m2=0,low=INFINITY,high=-INFINITY;
 std::unordered_map<long long,long long> counts;std::vector<long long> dense;std::vector<double> intervals;
 void add(double v,int field,double lo,double hi,int bins){
  if(!std::isfinite(v)){++missing;return;}if(bins&&v<lo){++under;return;}if(bins&&v>hi){++over;return;}
  ++n;double delta=v-mean;mean+=delta/n;m2+=delta*(v-mean);low=std::min(low,v);high=std::max(high,v);
  if(!bins&&field>=5){intervals.push_back(v);return;}
  long long k=bins?std::min((long long)bins-1,(long long)std::floor((v-lo)*bins/(hi-lo))):(long long)std::floor(v);
  if(bins){if(dense.empty())dense.resize(bins);++dense[k];}else ++counts[k];
 }
};
SeaTimeBins seastar_time_means(const std::string& path,int segment,double lo,double hi,double width,long long max_seq,bool good_only,const std::vector<double>& lows,const std::vector<double>& highs,const std::vector<int>& nbins,bool histograms) {
 TFile f(path.c_str(),"READ");TTreeReader r("metrics",&f);
 TTreeReaderValue<Int_t> ch(r,"channel"),board(r,"board"),seg(r,"segment"),good(r,"good");
 TTreeReaderValue<Long64_t> raw(r,"raw_ns"),tr(r,"trigger_ns"),ev(r,"event");
 TTreeReaderValue<double> time(r,"t"),baseline(r,"baseline"),noise(r,"noise"),amp(r,"amplitude"),area(r,"area");
 std::map<std::pair<int,long long>,SeaMeanBin> bins;
 SeaTimeBins out;std::array<std::array<SeaDistribution,7>,9> distributions;
 std::array<int,9> codes;codes.fill(-2);codes[8]=-1;
 std::array<long long,9> keys;keys.fill(std::numeric_limits<long long>::min());std::array<SeaMeanBin*,9> pointers{};
 auto add=[&](int slot,int code,double t,const std::array<double,7>& values){
  if(t<lo||t>hi)return;long long k=(long long)std::floor(t/width);codes[slot]=code;
  if(keys[slot]!=k){keys[slot]=k;pointers[slot]=&bins[{code,k}];}auto& b=*pointers[slot];
  for(int field=0;field<7;++field){
   if(field>=5)continue;
   if(slot!=8&&field!=4&&good_only&&values[4]==0)continue;
   double v=values[field];if(histograms)distributions[slot][field].add(v,field,lows[field],highs[field],nbins[field]);
   if(std::isfinite(v)){b.sum[field]+=v;++b.n[field];double delta=v-b.mean[field];b.mean[field]+=delta/b.n[field];b.m2[field]+=delta*(v-b.mean[field]);}
  }
 };
 while(r.Next()) {
  if(r.GetCurrentEntry()+*ev+2>max_seq||*seg>segment)break;
  int code=*board*256+*ch;
  if(*seg!=segment)continue;
  long long origin=*tr-std::llround(*time*1e9);double t=double(*raw-origin)*1e-9;
  if(t>=lo&&t<=hi){++out.observations;add(*ch,code,t,{*baseline,*noise,*amp,*area,double(*good),NAN,NAN});}
 }
 for(auto& item:bins){int code=item.first.first;long long k=item.first.second;auto& b=item.second;
  double center=(std::max(lo,k*width)+std::min(hi,(k+1)*width))/2;
  out.rows.insert(out.rows.end(),{double(k),double(code),center,center});
  for(int field=0;field<7;++field)out.rows.push_back(b.n[field]?b.sum[field]/b.n[field]:NAN);
  out.bin_statistics.insert(out.bin_statistics.end(),{double(k),double(code)});
  for(int field=0;field<7;++field)out.bin_statistics.insert(out.bin_statistics.end(),{double(b.n[field]),b.n[field]>1?std::sqrt(std::max(0.,b.m2[field]/(b.n[field]*(b.n[field]-1.)))):NAN});
 }
 std::array<double,7> global_low,global_high;global_low.fill(INFINITY);global_high.fill(-INFINITY);
 for(int slot=0;slot<9;++slot)for(int k=0;k<7;++k){auto &d=distributions[slot][k];if(d.n){global_low[k]=std::min(global_low[k],d.low);global_high[k]=std::max(global_high[k],d.high);}}
 global_low[5]=global_low[6]=std::min(global_low[5],global_low[6]);global_high[5]=global_high[6]=std::max(global_high[5],global_high[6]);
 for(int slot=0;slot<9;++slot)for(int field=0;field<7;++field){auto &d=distributions[slot][field];int code=codes[slot];if(code==-2||(!d.n&&!d.missing&&!d.under&&!d.over))continue;
  double low=lows[field],high=highs[field];long long count=nbins[field];
  if(!count){
   if(field>=5){low=std::isfinite(global_low[field])?global_low[field]:0;high=std::isfinite(global_high[field])?global_high[field]:1;if(low==high)high=low+1;count=256;d.dense.resize(count);
    for(double v:d.intervals)++d.dense[std::min(count-1,(long long)std::floor((v-low)*count/(high-low)))];
   }else{low=std::isfinite(global_low[field])?std::floor(global_low[field]):0;high=std::isfinite(global_high[field])?std::floor(global_high[field])+1:1;count=(long long)(high-low);}
  }
  double binwidth=(high-low)/count;
  if(!d.dense.empty()){for(size_t k=0;k<d.dense.size();++k)if(d.dense[k])out.hist.insert(out.hist.end(),{double(code),double(field),low+(k+.5)*binwidth,binwidth,double(d.dense[k])});}
  else{std::vector<std::pair<long long,long long>> sorted(d.counts.begin(),d.counts.end());std::sort(sorted.begin(),sorted.end());
   for(auto kv:sorted)out.hist.insert(out.hist.end(),{double(code),double(field),kv.first+.5,binwidth,double(kv.second)});}
  out.stats.insert(out.stats.end(),{double(code),double(field),double(d.n),double(d.missing),double(d.under),double(d.over),d.n?d.mean:NAN,d.n?std::sqrt(std::max(0.,d.m2/d.n)):NAN,low,high,double(count)});
 }
 return out;
}
'''


def root_time_bins(path,cache_dir,request,snapshot):
    import ROOT as R
    from raw_trends import result
    lo,hi,segment,_,_,seq=settings(request,snapshot)
    width=request.get('time_bin_s',100)
    if type(width) not in (int,float) or not math.isfinite(width) or width<=0 or (hi-lo)/width>100000:
        raise ValueError('时间箱宽须为正数，每个时段最多显示 100000 个时间箱')
    if not isinstance(request.get('histogram_settings',{}),dict):raise ValueError('数值直方图设置无效')
    if request.get('population','all') not in ('all','good'):raise ValueError('样本类型无效')
    signature=[hashlib.sha256(CPP.encode()).hexdigest(),str(Path(path).resolve()),Path(path).stat().st_size,Path(path).stat().st_mtime_ns,lo,hi,segment,seq,width,request.get('population','all'),request.get('histogram_settings',{}),request.get('include_distributions',True)]
    key=hashlib.sha256(json.dumps(signature).encode()).hexdigest()
    target=Path(cache_dir)/'time-bins'/f'{key}.json.gz'
    if target.exists():
        with gzip.open(target,'rt') as stream:return json.load(stream)
    if not hasattr(R,'seastar_time_means') and not R.gInterpreter.Declare(CPP):raise RuntimeError('不能初始化时间分箱')
    lows=[0.]*7;highs=[1.]*7;nbins=[0]*7
    lows[4],highs[4],nbins[4]=-.5,1.5,2
    for field,name in enumerate(('baseline','noise','amplitude','area','quality','interval','event_interval')):
        spec=request.get('histogram_settings',{}).get('interval' if field==6 else name)
        if spec:
            low,high,bins=spec.get('min'),spec.get('max'),spec.get('bins')
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in (low,high)) or not low<high or type(bins) is not int or not 1<=bins<=4096:raise ValueError('数值直方图设置无效')
            lows[field],highs[field],nbins[field]=float(low),float(high),bins
    R.seastar_time_means.__release_gil__=True
    value=R.seastar_time_means(str(path),segment,lo,hi,float(width),seq,request.get('population')=='good',lows,highs,nbins,bool(request.get('include_distributions',True)))
    answer=result(list(value.rows),None,True,request)
    import numpy as np
    names=('baseline','noise','amplitude','area','quality','interval','event_interval');hist={};stats={}
    for code,field,x,w,n in np.asarray(list(value.hist)).reshape(-1,5):hist.setdefault((int(code),int(field)),[]).append([float(x),int(n)])
    for code,field,n,missing,under,over,mean,sigma,low,high,bins in np.asarray(list(value.stats)).reshape(-1,11):
        stats.setdefault(names[int(field)],[]).append(dict(code=int(code),n=int(n),missing=int(missing),underflow=int(under),overflow=int(over),mean=float(mean) if math.isfinite(mean) else None,sigma=float(sigma) if math.isfinite(sigma) else None,min=float(low),max=float(high),bins=int(bins),bin_width=(high-low)/bins,hist=hist.get((int(code),int(field)),[])))
    answer['distributions']=stats
    answer['bin_statistics']=[[float(x) if math.isfinite(x) else None for x in row] for row in np.asarray(list(value.bin_statistics)).reshape(-1,16)]
    answer['mean_error_model']='sample standard error; N<2 unavailable; independent-observation assumption'
    answer.update(raw=False,aggregation='time_mean',time_bin_s=width,observations=int(value.observations),min_s=lo,max_s=hi,distributions_deferred=not request.get('include_distributions',True))
    target.parent.mkdir(parents=True,exist_ok=True);temporary=target.with_suffix('.tmp.gz')
    with gzip.open(temporary,'wt') as stream:json.dump(answer,stream,ensure_ascii=False,allow_nan=False)
    temporary.replace(target);return answer


def sqlite_time_bins(path,request,snapshot):
    import sqlite3
    lo,hi,segment,_,_,seq=settings(request,snapshot)
    width=request.get('time_bin_s',100)
    if type(width) not in (int,float) or not math.isfinite(width) or width<=0 or (hi-lo)/width>100000:raise ValueError('时间箱宽无效')
    if request.get('population','all') not in ('all','good'):raise ValueError('统计样本类型无效')
    if width!=100:return _sqlite_time_bins_scan(path,request,snapshot)
    from storage import ensure_time_index
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        target=db.execute('SELECT COALESCE(MAX(seq),0) FROM metrics WHERE run=?',(request['run_id'],)).fetchone()[0]
    finally:db.close()
    ensure_time_index(path,request['run_id'],target if request.get('allow_latest') else min(seq,target))
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('BEGIN')
        row=db.execute('SELECT max_seq FROM time_index WHERE run=?',(request['run_id'],)).fetchone()
        actual=row[0] if row else 0
        if actual>seq and not request.get('allow_latest'):
            return _sqlite_time_bins_scan(path,request,snapshot)
        # Keep the boundary bins exact, including wave/trigger offsets across a box edge.
        margin=max([.001]+[abs(v)*1e-9 for c in snapshot.get('channels',[]) for v in (c.get('stats',{}).get('all',{}).get('wave_dt_ns',{}).get('min'),c.get('stats',{}).get('all',{}).get('wave_dt_ns',{}).get('max')) if isinstance(v,(float,int)) and math.isfinite(v)])
        first=math.ceil((lo+margin)/100);stop=math.floor((hi-margin)/100)
        bins={}
        if stop>first:
            for code,k,field,n,total,mean,m2 in db.execute('SELECT code,bin,field,n,total,mean,m2 FROM time_bins100 WHERE run=? AND segment=? AND population=? AND bin>=? AND bin<?',(request['run_id'],segment,request.get('population','all'),first,stop)):
                key=(code,k)
                if key not in bins:bins[key]=[[0.,0,0.,0.] for _ in range(7)]
                bins[key][field]=[total,n,m2,mean]
    finally:db.close()
    from raw_trends import sqlite_page,FIELDS
    query=dict(request,max_seq=actual,cursor=0,limit=100000)
    spans=[(lo,hi)] if stop<=first else [(lo,min(hi,first*100+margin)),(max(lo,stop*100-margin),hi)]
    for low,high in spans:
        cursor=0
        while True:
            page=sqlite_page(path,dict(query,min_s=low,max_s=high,cursor=cursor),snapshot)
            for row in page['rows']:
                t=row[2];tt=row[3];code=int(row[1])
                if not lo<=t<=hi:continue
                outside=stop<=first or not first*100<=t<stop*100
                if outside:
                    b=bins.setdefault((code,math.floor(t/100)),[[0.,0,0.,0.] for _ in range(7)])
                    for field,index in enumerate((4,5,6,7,8,9)):
                        if field!=4 and request.get('population')=='good' and not row[8]:continue
                        value=row[index]
                        if value is not None:_add_moment(b[field],value)
                if row[10] is not None and lo<=tt<=hi and (stop<=first or not first*100<=tt<stop*100):
                    e=bins.setdefault((-1,math.floor(tt/100)),[[0.,0,0.,0.] for _ in range(7)])
                    e[6][0]+=row[10];e[6][1]+=1
            if page['done']:break
            cursor=page['next_cursor']
    rows=[];statistics=[]
    for (code,k),items in sorted(bins.items()):
        t=(max(lo,k*100)+min(hi,(k+1)*100))/2
        rows.append([k,code,t,t]+[v[0]/v[1] if v[1] else None for v in items])
        statistics.append([k,code]+[v for field,item in enumerate(items) for v in (item[1],(0. if field==6 else math.sqrt(max(0.,item[2]/(item[1]*(item[1]-1))))) if item[1]>1 else None)])
    observations=sum(items[4][1] for (code,k),items in bins.items() if code!=-1)
    return dict(bin_statistics=statistics,mean_error_model='sample standard error; N<2 unavailable',distributions=sqlite_distributions(path,query,snapshot) if request.get('include_distributions',True) else {},distributions_deferred=not request.get('include_distributions',True),fields=FIELDS,rows=rows,run_id=request['run_id'],raw=False,aggregation='time_mean',time_bin_s=100,observations=observations,count=len(rows),done=True,next_cursor=None,min_s=lo,max_s=hi,max_seq=actual,requested_max_seq=seq,snapshot_advanced=actual>seq,index_source='persistent 100 s moments')


def _add_moment(item,value):
    item[0]+=value;item[1]+=1;delta=value-item[3];item[3]+=delta/item[1];item[2]+=delta*(value-item[3])


def _sqlite_time_bins_scan(path,request,snapshot):
    from raw_trends import sqlite_page
    width=request.get('time_bin_s',100)
    lo,hi,_,_,_,_=settings(request,snapshot)
    if type(width) not in (int,float) or not math.isfinite(width) or width<=0 or (hi-lo)/width>100000:raise ValueError('时间箱宽无效')
    bins={};cursor=0;observations=0
    while True:
        page=sqlite_page(path,dict(request,cursor=cursor,limit=100000),snapshot);observations+=page['count']
        for row in page['rows']:
            code=int(row[1]);t=row[2];k=math.floor(t/width)
            b=bins.setdefault((code,k),[[0.,0,0.,0.] for _ in range(7)])
            for field,index in enumerate((4,5,6,7,8,9)):
                if field!=4 and request.get('population')=='good' and not row[8]:continue
                v=row[index]
                if v is not None:
                    item=b[field];item[0]+=v;item[1]+=1;delta=v-item[3];item[3]+=delta/item[1];item[2]+=delta*(v-item[3])
            if row[10] is not None and lo<=row[3]<=hi:
                e=bins.setdefault((-1,math.floor(row[3]/width)),[[0.,0,0.,0.] for _ in range(7)])
                e[6][0]+=row[10];e[6][1]+=1
        if page['done']:break
        cursor=page['next_cursor']
    rows=[]
    for (code,k),items in sorted(bins.items()):
        t=(max(lo,k*width)+min(hi,(k+1)*width))/2
        rows.append([k,code,t,t]+[v[0]/v[1] if v[1] else None for v in items])
    bin_statistics=[[k,code]+[v for item in items for v in (item[1],math.sqrt(max(0.,item[2]/(item[1]*(item[1]-1)))) if item[1]>1 else None)] for (code,k),items in sorted(bins.items())]
    return dict(bin_statistics=bin_statistics,mean_error_model='sample standard error; N<2 unavailable',distributions=sqlite_distributions(path,request,snapshot) if request.get('include_distributions',True) else {},distributions_deferred=not request.get('include_distributions',True),fields=page['fields'],rows=rows,run_id=request['run_id'],raw=False,aggregation='time_mean',time_bin_s=width,
                observations=observations,count=len(rows),done=True,next_cursor=None,min_s=lo,max_s=hi)


def sqlite_distributions(path,request,snapshot):
    """Independent value histograms over the raw records, not over temporal means."""
    from raw_trends import sqlite_page
    from histograms import Counts
    import numpy as np
    names=('baseline','noise','amplitude','area','quality','interval','event_interval')
    accum={};delayed=set();low=math.inf;high=-math.inf;cursor=0
    while True:
        page=sqlite_page(path,dict(request,cursor=cursor,limit=100000),snapshot);grouped={}
        for row in page['rows']:
            code=int(row[1])
            for field,index in enumerate((4,5,6,7,8,9)):
                if field!=4 and request.get('population')=='good' and not row[8]:continue
                grouped.setdefault((code,field),[]).append(row[index])
            if row[10] is not None:grouped.setdefault((-1,6),[]).append(row[10])
        for key,values in grouped.items():
            field=key[1];spec=request.get('histogram_settings',{}).get('interval' if field==6 else names[field])
            if field>=5 and not spec:
                delayed.add(key)
                for v in values:
                    if v is not None and math.isfinite(v):low=min(low,v);high=max(high,v)
                continue
            if key not in accum:
                if spec:accum[key]=Counts(spec['min'],spec['max'],spec['bins'])
                elif field==4:accum[key]=Counts(-.5,1.5,2)
                else:accum[key]=Counts(0,1,None)
            accum[key].add(values)
        if page['done']:break
        cursor=page['next_cursor']
    # The first pass finds exact limits; a second bounded pass fills intervals.
    # No array/list proportional to the full run is retained.
    if not math.isfinite(low):low,high=0,1
    elif low==high:high=low+1
    for key in delayed:accum[key]=Counts(low,high,256)
    cursor=0
    while delayed:
        page=sqlite_page(path,dict(request,cursor=cursor,limit=100000),snapshot);grouped={}
        for row in page['rows']:
            key=(int(row[1]),5)
            if key in delayed and (request.get('population')!='good' or row[8]):grouped.setdefault(key,[]).append(row[9])
            if (-1,6) in delayed and row[10] is not None:grouped.setdefault((-1,6),[]).append(row[10])
        for key,values in grouped.items():accum[key].add(values)
        if page['done']:break
        cursor=page['next_cursor']
    output={}
    native_bounds={}
    for (code,field),value in accum.items():
        if value.bins is None and value.counts:
            bounds=native_bounds.setdefault(field,[min(value.counts),max(value.counts)+1]);bounds[0]=min(bounds[0],min(value.counts));bounds[1]=max(bounds[1],max(value.counts)+1)
    for (code,field),value in accum.items():
        data=value.result()
        if value.bins is None:low,high=native_bounds.get(field,[0,1]);bins=high-low
        else:
            low,high,bins=value.lo,value.hi,value.bins
            data['hist']=[[low+(i+.5)*(high-low)/bins,int(n)] for i,n in enumerate(data.pop('counts')) if n]
        data.update(code=code,min=low,max=high,bins=bins,bin_width=(high-low)/bins)
        output.setdefault(names[field],[]).append(data)
    return output
