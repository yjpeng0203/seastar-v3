"""Unaveraged waveform observations. Pagination never merges or samples records."""
import json
import math
from pathlib import Path
import sqlite3
import numpy as np

FIELDS=('point_id','channel_code','wave_s','trigger_s','baseline','noise','amplitude','area','good','wave_interval_ns','event_interval_ns')
ROOT_CPP=r'''
#include <TFile.h>
#include <TTreeReader.h>
#include <TTreeReaderValue.h>
#include <array>
#include <vector>
#include <cmath>
#include <limits>
struct SeaRawPage {std::vector<double> rows;Long64_t next=-1;bool done=true;};
std::vector<Long64_t> seastar_observation_index(const std::string& path) {
 TFile f(path.c_str(),"READ");TTreeReader r("metrics",&f);
 TTreeReaderValue<Int_t> ch(r,"channel"),seg(r,"segment");
 TTreeReaderValue<Long64_t> raw(r,"raw_ns"),tr(r,"trigger_ns"),ev(r,"event");TTreeReaderValue<double> time(r,"t");
 std::array<Long64_t,8> prev;prev.fill(-1);Long64_t event=-1,trigger=-1;int prior_seg=-1;
 std::vector<Long64_t> out;
 while(r.Next()) {
  auto i=r.GetCurrentEntry();
  if(i%65536==0||*seg!=prior_seg) {
   out.insert(out.end(),{i,*seg,Long64_t(std::llround(*time*1e9)),*tr-Long64_t(std::llround(*time*1e9)),prior_seg,event,trigger});
   out.insert(out.end(),prev.begin(),prev.end());
  }
  if(*seg!=prior_seg){prev.fill(-1);event=-1;trigger=-1;prior_seg=*seg;}
  if(*ev!=event){event=*ev;trigger=*tr;}prev[*ch]=*raw;
 }
 return out;
}
SeaRawPage seastar_observation_page(const std::string& path,const std::vector<Long64_t>& index,int segment,
 double lo,double hi,Long64_t cursor,int limit,Long64_t max_seq,double margin) {
 TFile f(path.c_str(),"READ");TTreeReader r("metrics",&f);r.SetEntriesRange(index[0],-1);
 TTreeReaderValue<Int_t> ch(r,"channel"),board(r,"board"),seg(r,"segment"),good(r,"good");
 TTreeReaderValue<Long64_t> raw(r,"raw_ns"),tr(r,"trigger_ns"),ev(r,"event");
 TTreeReaderValue<double> time(r,"t"),baseline(r,"baseline"),noise(r,"noise"),amp(r,"amplitude"),area(r,"area");
 std::array<Long64_t,8> prev;for(int i=0;i<8;++i)prev[i]=index[7+i];
 int prior_seg=index[4];Long64_t event=index[5],trigger=index[6],page_event=-1;
 SeaRawPage out;out.rows.reserve((limit+32)*11);
 while(r.Next()) {
  auto i=r.GetCurrentEntry();if(i+*ev+2>max_seq||*seg>segment)break;
  if(*seg!=prior_seg){prev.fill(-1);event=-1;trigger=-1;prior_seg=*seg;}
  const bool new_event=*ev!=event;
  double wave_delta=prev[*ch]>=0?double(*raw-prev[*ch]):NAN;
  double event_delta=new_event&&trigger>=0?double(*tr-trigger):NAN;
  if(new_event){event=*ev;trigger=*tr;}prev[*ch]=*raw;
  if(*seg!=segment)continue;
  const auto origin=*tr-Long64_t(std::llround(*time*1e9));
  double t=double(*raw-origin)*1e-9,tt=double(*tr-origin)*1e-9;
  if(t>hi&&*time>hi+margin)break;
  if(i<cursor||t<lo||t>hi)continue;
  if(out.rows.size()/11>=size_t(limit)&&*ev!=page_event){out.next=i;out.done=false;break;}
  out.rows.insert(out.rows.end(),{double(i+1),double(*board*256+*ch),t,tt,*baseline,*noise,*amp,*area,double(*good),wave_delta,event_delta});
  page_event=*ev;
 }
 return out;
}
'''


def settings(request,snapshot):
    if not isinstance(request,dict) or request.get('run_id')!=snapshot.get('run_id'):raise ValueError('分析会话已变化，请刷新')
    lo,hi=request.get('min_s',0),request.get('max_s',snapshot.get('relative_s',0)+.001)
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (lo,hi)) or not lo<hi or max(abs(lo),abs(hi))>9e9:raise ValueError('采集时间范围无效')
    segment=request.get('segment',snapshot.get('segment',0));cursor=request.get('cursor',0);limit=request.get('limit',50000);seq=request.get('max_seq',snapshot.get('seq',0))
    if type(segment) is not int or not 0<=segment<=2**31-1 or type(cursor) is not int or not 0<=cursor<=2**63-1 or type(limit) is not int or not 1<=limit<=100000 or type(seq) is not int or not 0<=seq<=2**63-1:raise ValueError('分页或时间段参数无效')
    return float(lo),float(hi),segment,cursor,limit,seq


def result(rows,next_cursor,done,request):
    # JSON null means missing predecessor/metric, never a synthetic zero.
    a=np.asarray(rows,dtype=np.float64).reshape(-1,len(FIELDS))
    data=[[float(v) if math.isfinite(v) else None for v in row] for row in a]
    return dict(fields=FIELDS,rows=data,next_cursor=next_cursor,done=done,run_id=request['run_id'],
                raw=True,aggregation='none',sampling='none',count=len(data),max_seq=request.get('max_seq'))


def root_page(path,cache_dir,request,snapshot):
    import ROOT as R
    lo,hi,segment,cursor,limit,seq=settings(request,snapshot)
    margin=max([.001]+[abs(v)*1e-9 for c in snapshot.get('channels',[]) for v in (c.get('stats',{}).get('all',{}).get('wave_dt_ns',{}).get('min'),c.get('stats',{}).get('all',{}).get('wave_dt_ns',{}).get('max')) if isinstance(v,(float,int)) and math.isfinite(v)])
    if not hasattr(R,'seastar_observation_page') and not R.gInterpreter.Declare(ROOT_CPP):raise RuntimeError('不能初始化原始观测读取器')
    st=Path(path).stat();signature=[st.st_size,st.st_mtime_ns];cache=Path(cache_dir)/'raw-observation-index.npz'
    index=None
    if cache.exists():
        with np.load(cache) as saved:
            if saved['signature'].tolist()==signature:index=saved['index']
    if index is None:
        index=np.asarray(list(R.seastar_observation_index(str(path))),dtype=np.int64).reshape(-1,15)
        temporary=cache.with_suffix('.tmp.npz');np.savez_compressed(temporary,index=index,signature=np.array(signature,dtype=np.int64));temporary.replace(cache)
    candidates=index[index[:,1]==segment]
    if not len(candidates):return result([],None,True,request)
    if cursor:
        candidates=candidates[candidates[:,0]<=cursor]
    else:candidates=candidates[candidates[:,2]<=math.floor((lo-margin)*1e9)]
    anchor=candidates[-1] if len(candidates) else index[index[:,1]==segment][0]
    page=R.seastar_observation_page(str(path),[int(v) for v in anchor],segment,lo,hi,cursor,limit,seq,margin)
    answer=result(list(page.rows),int(page.next) if not page.done else None,bool(page.done),request)
    answer['origin_ns']=str(int(anchor[3]));return answer


def sqlite_page(path,request,snapshot):
    lo,hi,segment,cursor,limit,seq=settings(request,snapshot)
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('BEGIN');run=request['run_id']
        origin=db.execute('SELECT raw_ns-CAST(wave_dt_ns AS INTEGER)-CAST(round(t*1e9) AS INTEGER) FROM metrics WHERE run=? AND segment=? ORDER BY seq LIMIT 1',(run,segment)).fetchone()
        if not origin:return result([],None,True,request)
        origin=origin[0]
        margin=max([.001]+[abs(v)*1e-9 for c in snapshot.get('channels',[]) for v in (c.get('stats',{}).get('all',{}).get('wave_dt_ns',{}).get('min'),c.get('stats',{}).get('all',{}).get('wave_dt_ns',{}).get('max')) if isinstance(v,(float,int)) and math.isfinite(v)])
        # t is the original trigger-relative time; query a margin, then use exact waveform time.
        where='run=? AND segment=? AND seq<=? AND t>=? AND t<=?'
        args=[run,segment,seq,lo-margin,hi+margin]
        first=db.execute('SELECT seq,t FROM metrics WHERE '+where+' AND seq>=? ORDER BY seq LIMIT 1',args+[cursor]).fetchone()
        if not first:return result([],None,True,request)
        before=first[0];first_t=first[1];previous={}
        for b,c in db.execute('SELECT DISTINCT board,channel FROM metrics WHERE run=?',(run,)):
            row=db.execute('SELECT raw_ns FROM metrics WHERE run=? AND board=? AND channel=? AND segment=? AND t<=? AND seq<? ORDER BY t DESC,seq DESC LIMIT 1',(run,b,c,segment,first_t,before)).fetchone()
            if row:previous[(b,c)]=row[0]
        prior=db.execute('SELECT seq,trigger_number,raw_ns-CAST(wave_dt_ns AS INTEGER) FROM metrics WHERE run=? AND segment=? AND seq<? ORDER BY seq DESC LIMIT 1',(run,segment,before)).fetchone()
        event=tuple(prior[1:]) if prior else None;trigger=prior[2] if prior else None;previous_seq=prior[0] if prior else None
        rows=[];next_cursor=None;page_event=None
        fields='seq,board,channel,raw_ns,CAST(wave_dt_ns AS INTEGER),trigger_number,baseline,noise,amplitude,area,good'
        for q,b,c,raw,dt,number,baseline,noise,amp,area,good in db.execute('SELECT '+fields+' FROM metrics WHERE '+where+' AND seq>=? ORDER BY seq',args+[before]):
            tr=raw-dt;current=(number,tr);new=current!=event or (previous_seq is not None and q-previous_seq>1);wave_delta=raw-previous[(b,c)] if (b,c) in previous else math.nan;event_delta=tr-trigger if new and trigger is not None else math.nan
            event=current;trigger=tr;previous_seq=q;previous[(b,c)]=raw;t=(raw-origin)*1e-9
            if t<lo or t>hi:continue
            if len(rows)>=limit and current!=page_event:next_cursor=q;break
            rows.append([q,b*256+c,t,(tr-origin)*1e-9,baseline,noise,amp,area,good,wave_delta,event_delta]);page_event=current
        answer=result(rows,next_cursor,next_cursor is None,request);answer['origin_ns']=str(origin);return answer
    finally:db.close()
