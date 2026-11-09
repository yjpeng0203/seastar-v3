"""Recorded counts / observed time exposure, never inverse mean spacing."""
import csv
import math
from pathlib import Path
import sqlite3
from raw_trends import settings
from stat_errors import poisson_interval

CPP=r'''
#include <TFile.h>
#include <TTreeReader.h>
#include <TTreeReaderValue.h>
#include <map>
#include <vector>
#include <cmath>
std::vector<double> seastar_rate_counts(const std::string& path,int segment,double lo,double hi,double width,long long max_seq,bool good_only,bool close_hi) {
 TFile f(path.c_str(),"READ");TTreeReader r("metrics",&f);
 TTreeReaderValue<int> ch(r,"channel"),board(r,"board"),seg(r,"segment"),good(r,"good");
 TTreeReaderValue<Long64_t> event(r,"event");TTreeReaderValue<double> time(r,"t");
 std::map<std::pair<int,long long>,long long> counts;long long previous=-1;
 while(r.Next()){
  if(r.GetCurrentEntry()+*event+2>max_seq||*seg>segment)break;
  if(*seg!=segment||*time<lo||*time>hi||(!close_hi&&*time==hi))continue;
  long long last=(long long)std::ceil(hi/width)-1,k=std::min(last,(long long)std::floor(*time/width));
  if(!good_only||*good)++counts[{*board*256+*ch,k}];
  if(*event!=previous){++counts[{-1,k}];previous=*event;}
 }
 std::vector<double> out;for(auto item:counts)out.insert(out.end(),{double(item.first.first),double(item.first.second),double(item.second)});return out;
}
'''


def finish(counts,channels,lo,hi,width,run,seq,method):
    series=[]
    if hi<=lo:return dict(run_id=run,series=[],time_bin_s=width,unit='Hz',method=method,min_s=lo,max_s=hi,max_seq=seq)
    first,last=math.floor(lo/width),math.ceil(hi/width)-1
    for code in list(channels)+[-1]:
        data=[]
        for k in range(first,last+1):
            low=max(lo,k*width);high=min(hi,(k+1)*width);exposure=high-low
            if exposure<=0:continue
            n=int(counts.get((code,k),0));lower,upper=poisson_interval(n)
            data.append([(low+high)/2,n/exposure,n,exposure,lower/exposure,upper/exposure,low,high])
        series.append(dict(code=code,data=data))
    return dict(run_id=run,series=series,time_bin_s=width,unit='Hz',method=method,min_s=lo,max_s=hi,max_seq=seq,
      uncertainty='68.27% Poisson interval; exact for N<25, Wilson-Hilferty approximation otherwise; independent-count assumption')


def parameters(request,snapshot,end):
    lo,hi,segment,_,_,seq=settings(request,snapshot);width=request.get('time_bin_s',100)
    if type(width) not in (int,float) or not math.isfinite(width) or width<=0 or (hi-lo)/width>100000:raise ValueError('计数率时间箱宽无效')
    if request.get('population','all') not in ('all','good'):raise ValueError('统计样本无效')
    return max(0.,lo),min(hi,float(end)),segment,seq,float(width)


def root_rates(path,request,snapshot):
    import json
    meta=json.loads((Path(path).parent.parent/'analysis.json').read_text())
    segment=request.get('segment',snapshot.get('segment',0));spans=meta.get('segment_spans_s',[snapshot.get('relative_s',0)])
    end=spans[segment] if type(segment) is int and 0<=segment<len(spans) else 0
    lo,hi,segment,seq,width=parameters(request,snapshot,end)
    channels=[int(c['key'].split(':')[0])*256+int(c['key'].split(':')[1]) for c in snapshot['channels']]
    if seq<snapshot['seq']:
        from replay_cache import ROOT_LOCK
        with ROOT_LOCK:
            return _root_frozen_rates(path,request,snapshot,meta,channels)
    counts={};directory=Path(path).parent.parent/'tables' 
    # Reuse exact recorded integer counts, not arithmetic averages of stored rates.
    aligned=abs(width/100-round(width/100))<1e-10 and width>=100 and abs(lo/100-round(lo/100))<1e-10 and (hi==end or abs(hi/100-round(hi/100))<1e-10)
    if hi>lo and aligned and seq>=snapshot['seq'] and end%width!=0:
        for row in csv.DictReader((directory/'trends.csv').read_text().splitlines()):
            a,b=float(row['t_start_s']),float(row['t_end_s'])
            if int(row['segment'])!=segment or a<lo or b>hi:continue
            code=int(row['board'])*256+int(row['channel']);k=math.floor(a/width)
            counts[(code,k)]=counts.get((code,k),0)+int(row['good' if request.get('population')=='good' else 'waves'])
        for row in csv.DictReader((directory/'trigger_rates.csv').read_text().splitlines()):
            a,b=float(row['t_start_s']),float(row['t_end_s'])
            if int(row['segment'])==segment and a>=lo and b<=hi:
                k=math.floor(a/width);counts[(-1,k)]=counts.get((-1,k),0)+int(row['wave_events'])
        method='saved integer counts / observed exposure'
    elif hi>lo:
        from replay_cache import ROOT_LOCK
        with ROOT_LOCK:
            import ROOT as R
            if not hasattr(R,'seastar_rate_counts') and not R.gInterpreter.Declare(CPP):raise RuntimeError('不能初始化计数率读取器')
            R.seastar_rate_counts.__release_gil__=True
            a=R.seastar_rate_counts(str(path),segment,lo,hi,width,seq,request.get('population')=='good',hi==end)
            for i in range(0,len(a),3):counts[(int(a[i]),int(a[i+1]))]=int(a[i+2])
            method='raw recorded counts / observed exposure'
    else:method='insufficient observed exposure'
    return finish(counts,channels,lo,hi,width,request['run_id'],seq,method)


def _root_frozen_rates(path,request,snapshot,meta,channels):
    import ROOT as R
    lo,hi,segment,_,_,seq=settings(request,snapshot);width=float(request.get('time_bin_s',100))
    spans=meta.get('segment_spans_s',[snapshot.get('relative_s',0)]);end=spans[segment] if 0<=segment<len(spans) else 0
    f=R.TFile.Open(str(path));tree=f.Get('metrics');tree.SetBranchStatus('*',0)
    for name in ('event','segment','t'):tree.SetBranchStatus(name,1)
    left,right=0,int(tree.GetEntries())
    while left<right:
        middle=(left+right)//2;tree.GetEntry(middle)
        if middle+int(tree.event)+2<=seq:left=middle+1
        else:right=middle
    if left:
        tree.GetEntry(left-1);last_segment=int(tree.segment)
        if last_segment==segment:end=float(tree.t)
        elif last_segment<segment:end=0
    else:end=0
    f.Close();lo,hi,segment,seq,width=parameters(request,snapshot,end)
    counts={}
    if hi>lo:
        if not hasattr(R,'seastar_rate_counts') and not R.gInterpreter.Declare(CPP):raise RuntimeError('不能初始化计数率读取器')
        a=R.seastar_rate_counts(str(path),segment,lo,hi,width,seq,request.get('population')=='good',hi==end)
        for i in range(0,len(a),3):counts[(int(a[i]),int(a[i+1]))]=int(a[i+2])
    return finish(counts,channels,lo,hi,width,request['run_id'],seq,'raw recorded counts / frozen observed exposure')


def sqlite_rates(path,request,snapshot):
    lo0,hi0,segment,_,_,seq=settings(request,snapshot)
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('BEGIN');run=request['run_id'];channels=[];starts=[];end=0.
        for c in snapshot.get('channels',[]):
            board,ch=map(int,c['key'].split(':'));channels.append(board*256+ch)
            row=db.execute('SELECT MAX(t) FROM metrics WHERE run=? AND board=? AND channel=? AND segment=? AND seq<=?',(run,board,ch,segment,seq)).fetchone()
            if row[0] is not None:end=max(end,row[0])
            row=db.execute('SELECT MIN(seq) FROM metrics WHERE run=? AND board=? AND channel=? AND segment=? AND t>=? AND t<=? AND seq<=?',(run,board,ch,segment,lo0,hi0,seq)).fetchone()
            if row[0] is not None:starts.append(row[0])
        lo,hi,segment,seq,width=parameters(request,snapshot,end);counts={};event=None;previous_seq=None
        if starts and hi>lo:
            for q,b,c,t,good,number,tr in db.execute('SELECT seq,board,channel,t,good,trigger_number,raw_ns-CAST(wave_dt_ns AS INTEGER) FROM metrics WHERE run=? AND segment=? AND seq>=? AND seq<=? AND t>=? AND t<=? ORDER BY seq',(run,segment,min(starts),seq,lo,hi)):
                if t==hi and hi<end:continue
                k=min(math.ceil(hi/width)-1,math.floor(t/width));code=b*256+c
                if request.get('population')!='good' or good:counts[(code,k)]=counts.get((code,k),0)+1
                current=(number,tr)
                if current!=event or (previous_seq is not None and q-previous_seq>1):counts[(-1,k)]=counts.get((-1,k),0)+1
                event=current;previous_seq=q
        return finish(counts,channels,lo,hi,width,run,seq,'raw recorded counts / observed exposure')
    finally:db.close()
