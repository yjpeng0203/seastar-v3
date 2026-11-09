"use strict";
const rawTrends=(()=>{
 let key="",page=null,busy=false,generation=0,controller=null,cursors=[0],pageNumber=0,loadedPage=0,lastSeq=-1,lastAttempt=0,customBounds=[0,60],loadingDescription="",initialScope=false;
 const get=id=>document.getElementById(id);
 let rateKey="",ratePage=null,rateController=null,rateGeneration=0,rateBusy=false,rateAttempt=-Infinity;
 function valueHistogramSettings(){const result={...(dashboard.histogramSettings?dashboard.histogramSettings():{})};delete result.rate;delete result.interval;return result;}
 function wantsDistributions(){return ["baselineTrend","noiseTrend","amplitudeTrend","chargeTrend","qualityTrend"].some(id=>dashboard.isExpanded?.(id));}
 function rateView(){
  if(!state?.run_id||frozen)return;
  const [lo,hi]=interval(),scope=get("trend-range").value,body={run_id:state.run_id,min_s:lo,max_s:hi,segment:Number(get("raw-segment").value||state.segment||0),follow_segment:scope==="recent"||scope==="all",time_bin_s:timeWidth,population:get("population").value,max_seq:state.seq||0,live:state.input_mode!=="replay",scope};
  const next=JSON.stringify([body.run_id,scope,body.segment,timeWidth,body.population,...(scope==="custom"?customBounds:[])]);
  const changed=next!==rateKey;
  if(changed){rateKey=next;ratePage=null;if(rateController)rateController.abort();rateBusy=false;}
  if(rateBusy){const note=get("rate-status");if(note&&performance.now()-rateAttempt>=2000)note.textContent="计数率请求已等待 "+fmt((performance.now()-rateAttempt)/1000,1)+" s；保留上次结果，不重复排队。";return;}
  if(changed||(!rateBusy&&state.input_mode!=="replay"&&performance.now()-rateAttempt>=1000)){
   rateBusy=true;rateAttempt=performance.now();rateController=new AbortController();const current=++rateGeneration;
   fetch("api/rates",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),signal:rateController.signal}).then(async response=>{const r=await response.json();if(!response.ok)throw Error(r.error||"计数率统计失败");return r;}).then(result=>{if(current!==rateGeneration)return;ratePage=result;paintRates();}).catch(error=>{if(current===rateGeneration&&error.name!=="AbortError"){const note=get("rate-status");if(note)note.textContent="计数率暂未更新："+error.message+"；保留上次结果。";}}).finally(()=>{if(current===rateGeneration)rateBusy=false;});return;
  }
  if(ratePage)paintRates();
 }
 function paintRates(){
  if(!ratePage||ratePage.run_id!==state.run_id||frozen)return;
  const channels=state.channels?.length?selectedChannels():ratePage.series.filter(s=>s.code>=0).map(s=>({key:Math.floor(s.code/256)+":"+(s.code%256)}));
  const allowed=new Map(channels.map((c,i)=>[Number(c.key.split(":")[0])*256+Number(c.key.split(":")[1]),{name:channelName(c.key),color:i}]));
  const list=ratePage.series.filter(s=>s.code===-1||allowed.has(s.code)).map(s=>{
   const item=allowed.get(s.code),name=item?item.name:"带波形事件率",color=item?palette[item.color%palette.length]:"#203a50";
   return {id:name,name,raw_code:s.code,type:"line",data:s.data.map(v=>[v[0],v[1],v[2],v[3]]),uncertainty:s.data.map(v=>[v[0],v[4],v[5],v[6],v[7],v[2]]),showSymbol:true,symbolSize:3,connectNulls:false,lineStyle:{width:1,color},tooltip:{trigger:"item",formatter:p=>{
     const v=s.data[p.dataIndex];return esc(name)+"<br>计数率："+fmt(v[1],6)+" Hz<br>N="+fmt(v[2],0)+"，观测箱长="+fmt(v[3],6)+" s<br>68.27% 统计区间：["+fmt(v[4],6)+", "+fmt(v[5],6)+"] Hz";
    }},itemStyle:{color},emphasis:{disabled:true}};
  });
  dashboard.trend("rates",list,"计数率 / Hz",1,{bounds:[ratePage.min_s,ratePage.max_s],beijing_reference:ratePage.live&&(ratePage.counter_source!==ratePage.analysis_source||ratePage.segment!==(state.segment||0))?null:state.beijing_reference,note:"计数率 = 箱内记录数 / 实际观测箱长（Hz）；黑线为带波形事件率。右侧为同一时段的时间箱计数率分布，不是单条间隔分布。灰带为 68.27% Poisson 统计区间（小计数精确，其余近似），假设计数独立，不含系统误差。"});
  updateTitle("rates","波形/带波形事件计数率（"+ratePage.time_bin_s+"s 时间箱）","按计数与观测时长计算 Hz；不使用平均间隔取倒数");
  const note=get("rate-status");if(note){const checked=ratePage.checked_host_unix_s||ratePage.published_host_unix_s;
   note.textContent=(ratePage.live?"独立轻量计数 · 1 s 请求周期，目标显示更新 ≤2 s":"基于已完成波形分析的计数率 · 进度受分析速度影响")+
    (checked?" · 状态时间 "+new Date(checked*1000).toLocaleTimeString("zh-CN",{hour12:false}):"")+
    (ratePage.live?" · 计数时间段 "+(ratePage.segment+1)+" · 已计数 "+fmt(ratePage.counter_waves,0)+" 波形 · 未读 "+fmt(ratePage.backlog,0)+" B"+(ratePage.counter_source!==ratePage.analysis_source?" · 计数已跟随新 Run："+ratePage.counter_source+"；完整分析继续处理原 Run":"")+(ratePage.waiting_for_volume?" · 等待分卷 "+ratePage.waiting_for_volume:"")+(ratePage.bad_bytes||ratePage.orphan_waves||ratePage.first_index>1?" · 数据完整性提示：异常字节 "+fmt(ratePage.bad_bytes,0)+"，孤立波形 "+fmt(ratePage.orphan_waves,0)+"，起始卷 "+ratePage.first_index:"")+(ratePage.no_new_data_s>=5?" · "+fmt(ratePage.no_new_data_s,0)+" s 无新增数据，保留最后观测结果":""):"");}
 }
 function updateTitle(id,title,description){const plot=get(id);if(!plot?.closest)return;const h=plot.closest("article").querySelector("h3");h.firstChild.textContent=title+" ";const small=h.querySelector("small");if(small)small.textContent=description;plot.setAttribute("aria-label",title+"，点击放大详情");if(dashboard.isExpanded?.(id))get("detail-title").textContent=title;}

 let timeEnabled=!!get("time-bin-enabled").checked,timeWidth=Number(get("time-bin-seconds").value||100);
 function interval(){
  const deltas=(state.channels||[]).flatMap(c=>[c.stats?.all?.wave_dt_ns?.min,c.stats?.all?.wave_dt_ns?.max]).filter(Number.isFinite);
  const first=Math.min(0,...deltas.map(v=>v*1e-9)),last=Math.max(1,(state.relative_s||0)+Math.max(0,...deltas)*1e-9+.000001),mode=get("trend-range").value;
  if(mode==="recent")return [Math.max(first,last-900),last];
  if(mode==="first")return [first,Math.min(last,first+60)];
  if(mode==="custom")return customBounds;
  return [first,last];
 }
 function status(text){get("raw-status").textContent=text;const detail=get("detail-raw-status");if(detail)detail.textContent=text;}
 function load(reset=true){
  if(!state?.run_id)return;
  const [lo,hi]=interval();if(!Number.isFinite(lo)||!Number.isFinite(hi)||lo>=hi){status("请输入有效采集时间范围（秒）。");return;}
  if(reset){cursors=[0];pageNumber=0;}
  if(controller)controller.abort();controller=new AbortController();const current=++generation;
  busy=true;for(const id of ["raw-prev","raw-next","detail-raw-prev","detail-raw-next"]){const b=get(id);if(b)b.disabled=true;}lastAttempt=performance.now();lastSeq=state.seq||0;status(timeEnabled?"正在按指定时间箱统计完整所选时段，原始指标保持不变…":"正在读取逐条原值；首次建立检索索引可能需要数秒，不计算时间箱均值。");
  const binned=timeEnabled;
  loadingDescription=binned?"正在计算 "+timeWidth+" s 时间箱，源记录 "+fmt(state.waves,0)+" 条":"正在读取逐条原值";
  const body={time_bin_s:timeWidth,population:get("population").value,histogram_settings:valueHistogramSettings(),include_distributions:wantsDistributions(),run_id:state.run_id,min_s:lo,max_s:hi,segment:Number(get("raw-segment").value||state.segment||0),cursor:cursors[pageNumber]||0,limit:50000,max_seq:state.seq||0};
  fetch(binned?"api/time-trend":"api/raw-trend",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),signal:controller.signal})
   .then(async response=>{const result=await response.json();if(!response.ok)throw Error(result.error||"原值读取失败");return result;})
   .then(result=>{if(current!==generation)return;page=result;loadedPage=pageNumber;busy=false;paint();})
   .catch(error=>{if(current===generation&&error.name!=="AbortError"){busy=false;status("读取失败："+error.message);}});
 }
 function paint(){
  if(!page||page.run_id!==state.run_id)return;
  const allowed=new Map(selectedChannels().map((c,i)=>[Number(c.key.split(":")[0])*256+Number(c.key.split(":")[1]),{name:channelName(c.key),color:i}]));
  const grouped=new Map([...allowed].map(([code,c])=>[code,{...c,code,baseline:[],noise:[],amplitude:[],area:[],quality:[],interval:[]}]));
  const binned=page.aggregation==="time_mean";
  const statistics=new Map((page.bin_statistics||[]).map(v=>[v[1]+":"+v[0],v]));
  const errors=Object.fromEntries([...allowed].map(([code])=>[code,{baseline:[],noise:[],amplitude:[],area:[],quality:[]}]));
  let lo=Infinity,hi=-Infinity;
  for(const row of page.rows){
   lo=Math.min(lo,row[2],row[3]);hi=Math.max(hi,row[2],row[3]);
   const c=grouped.get(row[1]);if(!c)continue;
   c.quality.push([row[2],row[8],row[0]]);
   const stats=statistics.get(row[1]+":"+row[0]);
   if(binned&&stats){
    for(const [field,index,factor,slot] of [["baseline",4,1,0],["noise",5,1,1],["amplitude",6,scale("amplitude"),2],["area",7,scale("area"),3],["quality",8,1,4]]){
     const n=stats[2+slot*2],se=stats[3+slot*2],mean=row[index];let low=null,high=null;
     if(field==="quality"&&n>0&&Number.isFinite(mean)){const center=(mean+.5/n)/(1+1/n),half=Math.sqrt(Math.max(0,mean*(1-mean)/n+.25/(n*n)))/(1+1/n);low=Math.max(0,center-half);high=Math.min(1,center+half);}
     else if(Number.isFinite(se)&&Number.isFinite(mean)){low=(mean-se)*factor;high=(mean+se)*factor;}
     const a=Math.max(page.min_s,row[0]*page.time_bin_s),b=Math.min(page.max_s,(row[0]+1)*page.time_bin_s);
     errors[row[1]][field].push([row[2],low,high,a,b,n]);
    }
   }
   if(get("population").value==="good"&&!row[8]){if(binned)for(const name of ["baseline","noise","amplitude","area","interval"])c[name].push([row[2],null,row[0]]);continue;}
   for(const [field,index,factor] of [["baseline",4,1],["noise",5,1],["amplitude",6,scale("amplitude")],["area",7,scale("area")],["interval",9,1e-6]]){
    c[field].push([row[2],row[index]===null?null:row[index]*factor,row[0]]);
   }
  }
  if(!Number.isFinite(lo)){[lo,hi]=interval();}if(lo===hi)hi=lo+1e-6;
  if(page.origin_ns&&state.beijing_reference){state.beijing_reference={...state.beijing_reference,origin_unix_ms:Number(page.origin_ns)/1e6+state.beijing_reference.offset*1000};}
  state.raw_page_bounds=binned?[page.min_s,page.max_s]:[lo,hi];state.raw_value_distributions=binned?page.distributions:null;state.raw_time_bin_s=binned?page.time_bin_s:null;state.raw_trend_note=binned?"左侧每 "+page.time_bin_s+" s 箱内均值，相邻有效点连线；合格标记为箱内比例。右侧统计完整所选时段的原始值，不统计均值点；灰带为均值标准误，比例为 Wilson 68.27% 区间，假设记录独立，不含系统误差；N<2 不估计均值误差。图内缩放不重算时间箱，需改时段请应用采集秒范围。":"逐条原始观测：未平均、未抽点、未合并，相邻有效点连线。右侧统计当前原值页，可前后翻页查看全部记录。";
  state.raw_distributions_deferred=!!page.distributions_deferred;
  const series=field=>[...grouped.values()].map(c=>({uncertainty:binned?errors[c.code]?.[field]:null,id:c.name,name:c.name,raw_code:c.code,type:"line",data:c[field],showSymbol:binned,symbolSize:3,connectNulls:false,lineStyle:{width:1},sampling:"none",
    tooltip:{trigger:"item",formatter:p=>{
     const v=c[field][p.dataIndex],e=errors[c.code]?.[field]?.[p.dataIndex];let text=esc(c.name)+"<br>采集秒："+fmt(v[0],9)+"<br>"+(binned?"箱内均值：":"原始值：")+fmt(v[1],6);
     if(binned&&e)text+="<br>N="+fmt(e[5],0)+(e[1]===null?"；样本不足，标准误差不可估计":"<br>统计范围：["+fmt(e[1],6)+", "+fmt(e[2],6)+"]");return text;
    }},dimensions:["采集秒",binned?"箱内均值":"原始值",binned?"时间箱编号":"原始记录ID"],encode:{x:0,y:1},itemStyle:{color:palette[c.color%palette.length],opacity:.65},emphasis:{disabled:true}}));
  dashboard.trend("baselineTrend",series("baseline"),"ADC",1);
  dashboard.trend("noiseTrend",series("noise"),"ADC",1);
  dashboard.trend("amplitudeTrend",series("amplitude"),unit("amplitude"),scale("amplitude"));
  dashboard.trend("chargeTrend",series("area"),unit("area"),scale("area"));
  dashboard.trend("qualityTrend",series("quality"),binned?"箱内合格比例（0–1）":"合格标记（0 / 1）",1);
  for(const [id,title] of [["baselineTrend","基线"],["noiseTrend","噪声"],["amplitudeTrend","幅度"],["chargeTrend","主脉冲积分"],["qualityTrend",binned?"合格比例":"合格标记（0/1）"]]){
    const plot=get(id);if(plot?.closest){const caption=title+(binned?"（"+page.time_bin_s+"s 箱内均值）":"（逐条原值）");
     const heading=plot.closest("article").querySelector("h3");heading.firstChild.textContent=caption+" ";
     const small=heading.querySelector("small");if(small)small.textContent=binned?"箱内原始指标均值；右侧仍按原始观测数值分布":"逐条原始值，不平均、不抽点，相邻有效点连线";
     plot.setAttribute("aria-label",caption+"，点击放大详情");if(dashboard.isExpanded?.(id))get("detail-title").textContent=caption;
    }
  }
  for(const id of ["raw-prev","detail-raw-prev"]){const b=get(id);if(b)b.disabled=busy||pageNumber===0;}for(const id of ["raw-next","detail-raw-next"]){const b=get(id);if(b)b.disabled=busy||page.done;}
  if(binned){status("时间箱宽 "+page.time_bin_s+" s · 完整时段 "+fmt(page.observations,0)+" 条原始记录 → "+fmt(page.count,0)+" 组通道/事件箱统计；各图按通道连线，原值可切换查看");return;}
  status("逐条原值第 "+(loadedPage+1)+" 页 · "+fmt(page.count,0)+" 条完整记录 · 本页采集秒 "+fmt(lo,9)+"–"+fmt(hi,9)+(page.done?" · 所选范围已读完":" · 尚有后续页（没有抽点或合并）"));
 }
 function render(){
  if(!initialScope&&state.run_id){if(state.input_mode!=="replay")get("trend-range").value="recent";initialScope=true;}
  const segment=get("raw-segment");const count=state.segments||((state.segment||0)+1);
  if(segment.options.length!==count){segment.replaceChildren(...Array.from({length:count},(_,i)=>{const o=document.createElement("option");o.value=i;o.textContent="独立时间段 "+(i+1);return o;}));segment.value=state.segment||0;}
  rateView();
  const next=JSON.stringify([state.run_id,get("trend-range").value,segment.value,timeEnabled,timeWidth,get("population").value,wantsDistributions(),JSON.stringify(valueHistogramSettings()),...(get("trend-range").value==="custom"?[...customBounds]:[])]);
  if(next!==key){key=next;page=null;load(true);return;}
  if(!busy&&!frozen&&state.input_mode!=="replay"&&get("trend-range").value==="recent"&&lastSeq!==state.seq&&performance.now()-lastAttempt>=10000){load(true);return;}
  if(page)paint();
  else if(!busy)load(true);
 }
 get("time-bin-apply").onclick=()=>{const width=Number(get("time-bin-seconds").value);if(!Number.isFinite(width)||width<=0){status("时间箱宽必须是正数秒。");return;}timeWidth=width;timeEnabled=get("time-bin-enabled").checked;key="";render();};
 get("time-bin-enabled").onchange=()=>{timeEnabled=get("time-bin-enabled").checked;key="";render();};
 get("raw-apply").onclick=()=>{customBounds=[Number(get("raw-time-min").value),Number(get("raw-time-max").value)];get("trend-range").value="custom";key="";render();};
 get("raw-refresh").onclick=()=>load(true);
 get("raw-prev").onclick=()=>{if(!busy&&pageNumber){pageNumber--;load(false);}};
 get("raw-next").onclick=()=>{if(!busy&&page&&!page.done){cursors[++pageNumber]=page.next_cursor;load(false);}};
 get("raw-segment").onchange=()=>{key="";render();};
 if(typeof setInterval!=="undefined")setInterval(()=>{if(busy)status(loadingDescription+" · 已用 "+fmt((performance.now()-lastAttempt)/1000,1)+" s");},1000);
 if(typeof setInterval!=="undefined")setInterval(rateView,1000);
 return {displaySettings:()=>({time_bin_enabled:timeEnabled,time_bin_s:timeWidth,page:loadedPage+1,aggregation:page?.aggregation||"none"}),render,reload:()=>load(true)};
})();
