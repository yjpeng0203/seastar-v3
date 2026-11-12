"use strict";
const $ = id => document.getElementById(id);
const palette = ["#167c78", "#d08339", "#4d72b5", "#9873b8", "#ce6671", "#3893b3", "#7c9143", "#8c6856"];
const flagLabels = {low_snr:"低信噪比",baseline_short:"基线不足",baseline_drift:"基线斜率",edge:"边界截断",clipped:"主脉冲削顶",pileup:"脉冲重叠",timing_missing:"CFD 无有效交点",secondary:"存在次脉冲",adc_range:"ADC 越界",baseline_fallback:"替代基线区",tail_clipped:"尾部触轨"};
const statusLabels = {starting:"正在启动",waiting:"等待数据源",replaying:"处理历史积压",tailing:"增量监听中",complete:"回放完成",incomplete:"回放结束 · 数据不完整",paused:"分析已暂停",error:"需要检查",queued:"等待计算资源"};
let state=null, activeTab="overview", frozen=false, focusKey="", sourceEditing=false, configDirty=false, stringFilter="";
let knownChannels="", knownSources="", knownGroups="", lastRun="", selected=new Set(), charts=new Map(), exampleIndex="latest";
let requestBusy=false, failures=0;
const fmt=(v,d=2)=>v===null||v===undefined||!Number.isFinite(Number(v))?"—":Number(v).toLocaleString("zh-CN",{maximumFractionDigits:d});
const esc=v=>String(v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const bytes=v=>v>=1e9?fmt(v/1e9,2)+" GB":v>=1e6?fmt(v/1e6,1)+" MB":v>=1e3?fmt(v/1e3,1)+" kB":fmt(v,0)+" B";
const boardLabel=b=>{const n=Number(b);return Number.isInteger(n)&&n>=0&&n<=6?"G"+n+" · FEE "+n:"FEE "+b;};
const channelName=key=>{const parts=key.split(":");return boardLabel(parts[0])+" · CH "+parts[1];};
function sourceOptionText(s){
 const tag=s.string_group?s.string_group+" · ":"";
 const kind=s.kind==="dat"?"DAT":"ROOT 回放";
 return "["+tag+kind+"] "+s.key+(s.files.length>1?" · "+s.files.length+" 卷":"");
}
const selectedChannels=()=>state?state.channels.filter(c=>selected.has(c.key)):[];
const physical=()=>$ ("units").value==="physical";
function unit(metric){if(["area","tail_negative"].includes(metric))return physical()?"pC · 历史标度":"ADC·sample";if(metric==="amplitude")return physical()?"mV · 历史标度":"ADC";if(["baseline","noise"].includes(metric))return "ADC";if(metric==="snr")return "幅度 / 噪声 σ";if(metric==="pulses")return "脉冲数";return "ns";}
function scale(metric){if(!physical())return 1;if(["area","tail_negative"].includes(metric))return state.config.adc_mv*state.config.sample_ns/state.config.resistance;if(metric==="amplitude")return state.config.adc_mv;return 1;}
function showError(message){$("error").textContent=message;$("error").hidden=!message;}
async function control(command){try{const r=await fetch("api/control",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(command)});const answer=await r.json();if(!r.ok)throw Error(answer.error||"操作失败");showError("");setTimeout(poll,200);}catch(e){showError(e.message);}}
function setTab(tab){if(typeof queryClient!=="undefined")queryClient.cancelAll();if(tab!=="overview"&&typeof rawTrends!=="undefined")rawTrends.cancel();activeTab=tab;document.querySelectorAll("[data-tab]").forEach(b=>b.classList.toggle("active",b.dataset.tab===tab));document.querySelectorAll(".tab").forEach(s=>s.hidden=s.id!=="tab-"+tab);renderCharts();requestAnimationFrame(()=>charts.forEach(c=>c.resize()));}
document.querySelectorAll("[data-tab]").forEach(b=>b.addEventListener("click",()=>setTab(b.dataset.tab)));
function baseOption(xName,yName){return {animation:false,color:palette,textStyle:{fontFamily:'-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif',color:"#526879",fontSize:11},grid:{left:75,right:30,top:56,bottom:73},tooltip:{trigger:"axis",confine:true},legend:{type:"scroll",top:10,left:16,right:70,textStyle:{fontSize:10}},toolbox:{right:14,top:7,feature:{saveAsImage:{title:"保存 PNG",pixelRatio:2,backgroundColor:"#fff"},restore:{title:"恢复缩放"}}},xAxis:{type:"value",name:xName,nameLocation:"middle",nameGap:31,nameTextStyle:{fontSize:10},axisLabel:{hideOverlap:true,formatter:v=>fmt(v,3)},splitLine:{show:false},axisLine:{lineStyle:{color:"#b9cbd5"}}},yAxis:{type:"value",name:yName,scale:true,nameTextStyle:{fontSize:10},axisLabel:{formatter:v=>fmt(v,2)},splitLine:{lineStyle:{color:"#edf1f5"}}},dataZoom:[{id:"inside",type:"inside",filterMode:"none",throttle:100},{id:"slider",type:"slider",height:16,bottom:13,showDetail:false,borderColor:"#d7e1e7"}],series:[]};}
function draw(id,option){if(typeof dashboard!=="undefined")option=dashboard.option(id,option);const node=$(id);if(!node||node.closest("[hidden]"))return;let chart=charts.get(id),zoom=null,legend=null;if(!chart){chart=echarts.init(node,null,{renderer:"canvas"});charts.set(id,chart);}else{const old=chart.getOption();zoom=old.dataZoom;legend=old.legend?.[0]?.selected;}
 if(zoom&&option.dataZoom)option.dataZoom.forEach((z,i)=>{if(zoom[i]){z.start=zoom[i].start;z.end=zoom[i].end;}});if(legend&&option.legend)option.legend.selected=legend;chart.setOption(option,{notMerge:true,lazyUpdate:true});}
function line(name,data,index,extra={}){return {id:name,name,type:"line",data,showSymbol:false,connectNulls:false,lineStyle:{width:1.5,color:palette[index%palette.length]},itemStyle:{color:palette[index%palette.length]},emphasis:{disabled:true},...extra};}
function denseHist(distribution,factor=1){
 if(!distribution?.n||!distribution.hist.length)return [];
 const width=distribution.bin_width,origin=distribution.bin_origin??-128*width;
 const counts=new Map(distribution.hist.map(([x,n])=>[Math.round((x-origin)/width-.5),n]));
 // Keep empty-bin neighbours for steps without allocating the potentially million-bin span.
 const indices=new Set();for(const i of counts.keys()){indices.add(i-1);indices.add(i);indices.add(i+1);}
 return [...indices].sort((a,b)=>a-b).map(i=>[(origin+(i+.5)*width)*factor,counts.get(i)||0]);
}
function addLogMarkers(option,list,factor){
 if(!$("logy").checked)return;
 list.forEach((item,index)=>{
  const hist=(item.d?.hist||[]).filter(v=>v[1]>0),width=item.d?.bin_width;
  const points=hist.filter(([x],i)=>(!i||x-hist[i-1][0]>width*1.01)&&(i===hist.length-1||hist[i+1][0]-x>width*1.01));
  if(points.length)option.series.push({id:item.name+"-isolated",name:item.name,type:"scatter",large:true,largeThreshold:2000,
   data:points.map(([x,n])=>[x*factor,$("normalized").checked&&item.d.n?n/item.d.n:n]),symbolSize:2,
   silent:true,tooltip:{show:false},itemStyle:{color:palette[index%palette.length],opacity:1},emphasis:{disabled:true}});
 });
}
function histogramSummary(id,list,custom,plot){
 const summaryId=id+"-summary";let summary=$(summaryId);
 if(!summary){summary=document.createElement("p");summary.id=summaryId;summary.className="distribution-summary";$(id).after(summary);}
 if(custom){
  if(!plot.result){summary.textContent=plot.error?"分布未生成："+plot.error:"正在从已保存指标精确统计，稍候…";return;}
  summary.textContent="当前分箱范围内统计（缩放不改变）："+list.map(item=>item.name+" N="+fmt(item.summary.n,0)+" μ="+fmt(item.d.mean===null?null:item.d.mean*plot.factor,3)+" σ="+fmt(item.d.sigma===null?null:item.d.sigma*plot.factor,3)+"；范围下/上方 "+fmt(item.summary.underflow,0)+" / "+fmt(item.summary.overflow,0)+"；缺失 "+fmt(item.summary.missing,0)).join("； ")+" · 指标快照 #"+plot.result.max_seq;
 }else summary.textContent="累计统计（缩放不改变）："+list.map(item=>item.name+" N="+fmt(item.d.n,0)+" μ="+fmt(item.d.mean===null?null:item.d.mean*plot.factor,3)+" σ="+fmt(item.d.sigma===null?null:item.d.sigma*plot.factor,3)+" Δbin="+fmt(item.d.bin_width*plot.factor,3)).join("； ");
}
function hist(id,metric,override){
 const pop=$("population").value;
 const automatic=override||selectedChannels().map(c=>({name:channelName(c.key),d:c.stats[pop][metric]}));
 const factor=scale(metric),queryMetric=override!==undefined?"pair_dt_ns":metric;
 const plot=histogramControls.prepare(id,queryMetric,automatic,factor,unit(metric));plot.factor=factor;
 const list=plot.custom?(plot.result?histogramControls.series(plot.result,factor,key=>key.includes("/")?key+" ns":channelName(key)):[]):automatic;
 const option=baseOption(unit(metric),$("normalized").checked?"概率 / bin":"波形数 / bin");
 option.yAxis.type=$("logy").checked?"log":"value";option.yAxis.scale=false;
 if(plot.custom){option.xAxis.min=plot.spec.min*factor;option.xAxis.max=plot.spec.max*factor;}
 option.series=list.map((item,i)=>{
  const data=plot.custom?item.data:denseHist(item.d,factor).map(([x,n])=>[x,$("logy").checked&&n===0?null:$("normalized").checked&&item.d.n?n/item.d.n:n]);
  return line(item.name,data,i,{step:$("logy").checked?false:"middle",areaStyle:$("logy").checked?undefined:{opacity:.03}});
 });addLogMarkers(option,list,factor);dashboard.positiveAxis(option);draw(id,option);histogramSummary(id,list,plot.custom,plot);
}
function renderOverview(){if(typeof rawTrends!=="undefined")rawTrends.render();}
function renderWaveforms(){const c=state.channels.find(v=>v.key===focusKey);if(!c)return;const ex=exampleIndex==="latest"?c.latest:c.examples[Number(exampleIndex)];if(!ex)return;const dt=state.config.sample_ns;const factor=physical()?state.config.adc_mv:1;const label=physical()?"mV · 含基线，历史标度":"ADC · 含基线";let option=baseOption("波形内时间 / ns",label);option.series=[line("原始波形",ex.samples.map((v,i)=>[i*dt,v*factor]),0)];option.dataZoom.forEach(z=>{z.start=0;z.end=Math.min(100,(ex.end+40)/ex.samples.length*100);});option.series[0].markArea={silent:true,itemStyle:{opacity:.12},data:[[{name:"候选基线区",xAxis:0,itemStyle:{color:"#8594a0"}},{xAxis:ex.start*dt}],[{name:"主脉冲积分",xAxis:ex.start*dt,itemStyle:{color:"#239e7a"}},{xAxis:ex.end*dt}]]};const lines=[{yAxis:ex.baseline*factor,name:"基线"},{yAxis:(ex.baseline+3*ex.noise)*factor,name:"+3σ",label:{show:false}},{yAxis:(ex.baseline-3*ex.noise)*factor,name:"−3σ",label:{show:false}}];if(ex.cfd_ns!==null)lines.push({xAxis:ex.cfd_ns,name:"CFD"});option.series[0].markLine={silent:true,symbol:"none",lineStyle:{color:"#9b7b42",width:1,type:"dashed"},label:{formatter:"{b}",fontSize:10},data:lines};draw("waveform",option);
 $("wave-info").textContent=channelName(c.key)+" · t = "+fmt(ex.t,6)+" s · wave_id "+ex.wave_id+" · 实际积分 ["+ex.start+", "+ex.end+") 点（"+fmt(ex.start*dt,1)+"–"+fmt(ex.end*dt,1)+" ns） · 基线点 "+ex.baseline_points+" · SNR "+fmt(ex.snr,1)+" · "+(ex.good?"合格":"质量筛选未通过")+" · "+(ex.flag_names.map(f=>flagLabels[f]||f).join("、")||"无质量标记");
 option=baseOption("波形内时间 / ns",label);option.series=c.examples.map((v,i)=>line("抽样 "+(i+1),v.samples.map((s,k)=>[k*dt,s*factor]),i,{lineStyle:{width:.8,opacity:.35}}));option.series.push(line("合格波形平均",c.average.map((v,i)=>[i*dt,v*factor]),0,{lineStyle:{color:"#142d41",width:2.5}}));draw("average",option);$("average-note").textContent="平均使用 "+fmt(c.wave_count,0)+" 条合格波形；长度变化后单独重新累计";
 option=baseOption("相对主峰时间 / ns",physical()?"扣基线 mV":"扣基线 ADC");option.series=selectedChannels().map((v,i)=>line(channelName(v.key),v.aligned.map(([x,y])=>[x,y*factor]),i));draw("aligned",option);
 option=baseOption("波形内时间 / ns",label);option.grid={left:75,right:80,top:25,bottom:65};option.legend=undefined;option.tooltip={position:"top",confine:true,formatter:p=>"时间 "+fmt((p.value[0]+.5)*c.length*dt/128,1)+" ns<br>ADC "+fmt((p.value[1]+.5)*(state.config.adc_max+1)/64,1)+"<br>采样点数 "+fmt(Math.pow(10,p.value[2])-1,0)};option.xAxis={type:"category",name:"波形内时间 / ns",nameLocation:"middle",nameGap:30,data:Array.from({length:128},(_,i)=>fmt((i+.5)*c.length*dt/128,0)),axisLabel:{interval:15},splitArea:{show:false}};option.yAxis={type:"category",name:label,data:Array.from({length:64},(_,i)=>fmt((i+.5)*(state.config.adc_max+1)/64*factor,0)),axisLabel:{interval:7}};option.dataZoom=[{type:"inside",xAxisIndex:0,filterMode:"none"}];option.visualMap={min:0,max:Math.max(1,...c.density.map(v=>Math.log10(1+v[2]))),calculable:true,orient:"vertical",right:10,top:"middle",text:["log₁₀(1+N)",""],inRange:{color:["#f4f8fb","#bddedc","#4bafa2","#167c78","#173d53"]}};option.series=[{type:"heatmap",data:c.density.map(([x,y,n])=>[x,y,Math.log10(1+n)]),progressive:2000,emphasis:{disabled:true}}];draw("density",option);$("density-note").textContent="每 "+state.config.display_stride+" 条合格波形取 1 条，已累计 "+fmt(c.density_count,0)+" 条；越界点 "+fmt(c.density_outside,0);}
function renderCharge(){hist("amplitudeHist","amplitude");hist("chargeHist","area");hist("tailHist","tail_negative");hist("baselineHist","baseline");hist("noiseHist","noise");const option=baseOption(unit("amplitude"),unit("area"));option.tooltip={trigger:"item",confine:true};option.series=selectedChannels().map((c,i)=>({type:"scatter",name:channelName(c.key),symbolSize:3,itemStyle:{opacity:.35,color:palette[i%palette.length]},data:c.scatter.map(([a,q])=>[a*scale("amplitude"),q*scale("area")]),large:true}));draw("correlation",option);}
function intervalHist(id,automatic){
 automatic=automatic.filter(item=>item.d);
 const metric=id==="triggerIntervalHist"?"trigger_interval_log10_s":"interval_log10_s";
 const plot=histogramControls.prepare(id,metric,automatic,1,"log₁₀(间隔 / s)");plot.factor=1;
 const list=plot.custom?(plot.result?histogramControls.series(plot.result,1,key=>key==="triggers"?"带波形触发":channelName(key)):[]):automatic;
 const option=baseOption("log₁₀(间隔 / s)",$("normalized").checked?"概率 / bin":"间隔次数");
 option.yAxis.type=$("logy").checked?"log":"value";option.xAxis.axisLabel.formatter=v=>"10^"+fmt(v,1)+" s";
 if(plot.custom){option.xAxis.min=plot.spec.min;option.xAxis.max=plot.spec.max;}
 option.series=list.map((item,i)=>line(item.name,plot.custom?item.data:denseHist(item.d).map(([x,n])=>[x,n===0&&$("logy").checked?null:$("normalized").checked&&item.d.n?n/item.d.n:n]),i,{step:"middle"}));
 addLogMarkers(option,list,1);dashboard.positiveAxis(option);draw(id,option);histogramSummary(id,list,plot.custom,plot);
}
function renderTiming(){[["peakHist","peak_ns"],["cfdHist","cfd_ns"],["thresholdHist","threshold_ns"],["fwhmHist","fwhm_ns"],["riseHist","rise_ns"],["fallHist","fall_ns"],["windowHist","window_ns"],["snrHist","snr"],["waveDtHist","wave_dt_ns"]].forEach(([id,m])=>hist(id,m));intervalHist("intervalHist",selectedChannels().map(c=>({name:channelName(c.key),d:c.intervals})));intervalHist("triggerIntervalHist",[{name:"可见触发",d:state.trigger_intervals}]);$("cfd-title").textContent="CFD "+fmt(state.config.cfd_fraction*100,0)+"% 到达时间";$("threshold-title").textContent="基线 + "+fmt(state.config.threshold_adc,1)+" ADC 定时";}
function renderQuality(){const list=selectedChannels(),names=list.map(c=>channelName(c.key)),matrix=[];list.forEach((a,i)=>list.forEach((b,j)=>{const key=[a.key,b.key].sort((x,y)=>{const a=x.split(":").map(Number),b=y.split(":").map(Number);return a[0]-b[0]||a[1]-b[1];}).join("/");matrix.push([i,j,i===j?(state.event_hits[a.key]||0):(state.pairs[key]||0)]);}));const max=Math.max(1,...matrix.filter(v=>v[0]!==v[1]).map(v=>v[2]));const option={animation:false,grid:{left:110,right:75,top:30,bottom:80},tooltip:{position:"top",confine:true,formatter:p=>esc(names[p.value[0]])+" × "+esc(names[p.value[1]])+"<br>同触发次数 "+fmt(p.value[2],0)},xAxis:{type:"category",data:names,axisLabel:{rotate:25,fontSize:10}},yAxis:{type:"category",data:names,axisLabel:{fontSize:10}},visualMap:{min:0,max,calculable:true,right:0,top:"middle",inRange:{color:["#edf5f6","#5daea5","#176a69"]}},series:[{type:"heatmap",data:matrix,label:{show:list.length<=8,formatter:p=>fmt(p.value[2],0),fontSize:11},emphasis:{itemStyle:{borderWidth:1,borderColor:"#142d41"}}}]};draw("coincidence",option);
 const pairs=Object.entries(state.pair_dt||{}).filter(([key])=>key.split("/").every(k=>selected.has(k))).map(([key,d])=>({name:key+" ns",d}));hist("pairHist","cfd_ns",pairs);
 dashboard.countHistogram("multiplicity",Object.entries(state.multiplicity||{}),"每个触发的波形数","触发数");
 const flags=Object.keys(flagLabels),q=baseOption("质量标记","波形数");q.grid.bottom=105;q.xAxis={type:"category",data:flags.map(f=>flagLabels[f]),axisLabel:{rotate:32,fontSize:10,interval:0}};q.dataZoom=[];q.series=list.map((c,i)=>({type:"bar",name:channelName(c.key),stack:"flags",data:flags.map(f=>c.flags[f]||0),itemStyle:{color:palette[i%palette.length]}}));draw("qualityFlags",q);hist("pulsesHist","pulses");
 const diagnostics=[["数据字节异常",fmt(state.bad_bytes,0)+" 字节；只在找到合法触发头后重新同步"],["未完成记录",fmt(state.pending_bytes,0)+" 字节；等待追加，不作为丢包"],["无触发头波形",fmt(state.orphan_waves,0)+" 条；忽略并报告"],["原始触发头回退",fmt(state.header_backsteps,0)+" 次；空触发不参与默认时间轴"],["触发编号不连续",fmt(state.number_gaps,0)+" 次；ROOT 不保存无波形触发，不能直接认定丢包"],["时间段",fmt(state.segment+1,0)+"；时钟回退 "+fmt(state.clock_backsteps,0)+" 次"],["末事件",state.last_event_provisional?"暂定统计，下一触发到达后确认；不是重复累加":"完整 ROOT 回放 / 当前可见记录"],["合格筛选","排除低 SNR、基线不足/替代基线、边界截断、主脉冲削顶、主窗重叠与 ADC 越界。次脉冲与负尾单列。"],["CFD / 阈值无交点","保留为空值，图和统计不填 0；上升沿必须具有有效相邻点括区。"],["零 / 回退通道间隔",list.map(c=>c.key+": "+c.zero_intervals+" / "+c.negative_intervals).join("；")||"—"]];$("diagnostic-table").innerHTML=diagnostics.map(([a,b])=>"<tr><td>"+esc(a)+"</td><td>"+esc(b)+"</td></tr>").join("");}
let chartSignature="",chartSeq=-1,lastChartRender=-Infinity;
function renderCharts(force=false){if(!state||!state.config||typeof dashboard==="undefined")return;
 const signature=JSON.stringify([state.run_id,activeTab,[...selected],...["population","units","logy","normalized","trend-range","show-stat-errors"].map(id=>$(id).value+$(id).checked)]);
 if(!force&&signature===chartSignature&&(chartSeq===state.seq||performance.now()-lastChartRender<(activeTab==="overview"?10000:30000)))return;
 chartSignature=signature;chartSeq=state.seq;lastChartRender=performance.now();dashboard.decorate();dashboard.tasks();if(activeTab==="overview")renderOverview();if(activeTab==="waveforms")renderWaveforms();if(activeTab==="charge")renderCharge();if(activeTab==="timing")renderTiming();if(activeTab==="quality")renderQuality();}
function updateControls(){
 const groups=[...new Set(state.sources.map(s=>s.string_group).filter(Boolean))].sort((a,b)=>a.localeCompare(b,"en",{numeric:true}));
 const groupSig=groups.join(",");
 const filterEl=$("string-filter"), filterLabel=$("string-filter-label");
 if(filterEl&&filterLabel){
  const showFilter=groups.length>1;
  filterEl.hidden=filterLabel.hidden=!showFilter;
  if(groupSig!==knownGroups){
   knownGroups=groupSig;
   if(stringFilter&&!groups.includes(stringFilter))stringFilter="";
   filterEl.replaceChildren(...[["","全部串"],...groups.map(g=>[g,g])].map(([v,t])=>{const o=document.createElement("option");o.value=v;o.textContent=t;return o;}));
   filterEl.value=stringFilter;
  }
 }
 const visibleSources=stringFilter?state.sources.filter(s=>s.string_group===stringFilter):state.sources;
 const sourceSig=JSON.stringify(visibleSources.map(s=>[s.key,s.files.length,s.string_group||""]));
 if(sourceSig!==knownSources){knownSources=sourceSig;const old=$("source").value;$("source").replaceChildren(...visibleSources.map(s=>{const o=document.createElement("option");o.value=s.key;o.textContent=sourceOptionText(s);return o;}));$("source").value=sourceEditing&&visibleSources.some(s=>s.key===old)?old:(visibleSources.some(s=>s.key===state.source?.key)?state.source.key:visibleSources[0]?.key||"");}
 if(!sourceEditing&&state.source&&(!stringFilter||state.source.string_group===stringFilter))$("source").value=state.source.key;
 const channelSig=state.channels.map(c=>c.key).join(",");if(channelSig!==knownChannels){const old=knownChannels||"";knownChannels=channelSig;state.channels.forEach(c=>{if(!old.split(",").includes(c.key))selected.add(c.key);});$("channel-filters").replaceChildren(...state.channels.map((c,i)=>{const l=document.createElement("label"),input=document.createElement("input"),dot=document.createElement("span");input.type="checkbox";input.checked=selected.has(c.key);input.addEventListener("change",()=>{input.checked?selected.add(c.key):selected.delete(c.key);renderCharts();});dot.textContent=channelName(c.key);dot.style.color=palette[i%palette.length];l.append(input,dot);return l;}));$("focus").replaceChildren(...state.channels.map(c=>{const o=document.createElement("option");o.value=c.key;o.textContent=channelName(c.key);return o;}));if(!state.channels.some(c=>c.key===focusKey))focusKey=state.channels[0]?.key||"";$("focus").value=focusKey;}
 const c=state.channels.find(c=>c.key===focusKey);const ex=$("example");ex.replaceChildren();const latest=document.createElement("option");latest.value="latest";latest.textContent="最新抽样波形";ex.append(latest);if(c)c.examples.forEach((v,i)=>{const o=document.createElement("option");o.value=String(i);o.textContent="t="+fmt(v.t,4)+"s · id "+v.wave_id+(v.good?" · 合格":" · 质量标记");ex.append(o);});if(exampleIndex!=="latest"&&Number(exampleIndex)>=(c?.examples.length||0))exampleIndex="latest";ex.value=exampleIndex;
  $("restart").textContent=state.cache_mode?"重建当前串显示缓存":"重新分析（独立会话）";
 $("input-mode").textContent=(state.input_mode==="remote"?"远程接收":state.input_mode==="replay"?"本机历史回放":"本机 DAQ 目录直读")+" · 数据目录："+(state.data_dir||"—");$("follow").disabled=state.input_mode==="replay";$("follow").checked=state.follow;$("pause").disabled=["queued","starting"].includes(state.status);$("pause").textContent=state.paused?"继续当前串分析":"暂停当前串分析";$("select-source").disabled=!state.sources.length;$("restart").disabled=!state.source;
 if(!configDirty)for(const [k,v] of Object.entries(state.config)){const input=$("config-form").elements.namedItem(k);if(input)input.value=v;}}
function render(){if(!state)return;renderResources();if(typeof dashboard!=="undefined")dashboard.tasks();if(state.run_id!==lastRun){lastRun=state.run_id;knownChannels=null;selected.clear();focusKey="";exampleIndex="latest";charts.forEach(c=>c.clear());}updateControls();renderTransfer();$("connection").textContent=statusLabels[state.status]||state.status;$("lamp").className=state.status==="error"?"bad":["tailing","replaying"].includes(state.status)?"live":"";$("updated").textContent="页面 "+new Date().toLocaleTimeString("zh-CN",{hour12:false});$("triggers").textContent=fmt(state.triggers,0);$("empty-triggers").textContent="带波形 "+fmt(state.wave_events,0)+(state.source?.kind==="root"?" · 空触发原始总数未知":" · 空触发 "+fmt(state.multiplicity?.[0]||0,0));$("waves").textContent=fmt(state.waves,0);$("channels-count").textContent=state.channels.length+" 个板号 / 通道组合";$("good").textContent=state.waves?fmt(state.good/state.waves*100,1)+"%":"—";$("good-count").textContent="合格 "+fmt(state.good,0)+"；全部指标仍保留";$("duration").textContent=fmt(state.relative_s,1)+" s";$("time-segment").textContent="相对段 "+fmt((state.segment||0)+1,0)+" · 非绝对时间";$("backlog").textContent=state.backlog_unit==="events"?fmt(state.backlog,0)+" 事件":bytes(state.backlog||0);$("throughput").textContent=state.cache_mode==="validated analysis cache"?"已复用完整分析 · 当前无需重算":"处理能力 "+fmt(state.analysis_waves_per_s,0)+" 波形/s · 含检查点写盘";$("file").textContent=state.file||"等待可用数据文件";$("basis").textContent=(state.time_basis||"")+" 频谱 / 均值为累计量，趋势只显示当前时间段。";$("alerts").replaceChildren(...(state.alerts||[]).map(a=>{const d=document.createElement("div");d.className="alert "+a.level;d.textContent=a.text;return d;}));
 const population=$("population").value;$("amplitude-th").textContent="幅度 "+unit("amplitude");$("area-th").textContent="积分 "+unit("area");$("channel-table").innerHTML=state.channels.map(c=>{const m=c.stats[population];const flags=Object.entries(c.flags).sort((a,b)=>b[1]-a[1]).slice(0,2).map(([k,n])=>(flagLabels[k]||k)+" "+fmt(n,0)).join("；");return "<tr><td><button data-focus=\""+esc(c.key)+"\">"+esc(channelName(c.key))+"</button></td><td>"+fmt(c.n,0)+" / "+fmt(c.good,0)+"</td><td>"+fmt(c.good/c.n*100,1)+"%</td><td>"+fmt(m.baseline.mean)+"</td><td>"+fmt(m.noise.mean)+"</td><td>"+fmt(m.amplitude.mean===null?null:m.amplitude.mean*scale("amplitude"))+"</td><td>"+fmt(m.area.mean===null?null:m.area.mean*scale("area"))+"</td><td>"+fmt(m.cfd_ns.mean)+"</td><td>"+fmt(c.drift_adc)+"</td><td class=\"flag\">"+esc(flags||"—")+"</td></tr>";}).join("");$("channel-table").querySelectorAll("[data-focus]").forEach(b=>b.addEventListener("click",()=>{focusKey=b.dataset.focus;$("focus").value=focusKey;exampleIndex="latest";setTab("waveforms");}));
 const exportQuery="?run_id="+encodeURIComponent(state.run_id||"")+"&max_seq="+(state.seq||0);$("all-export").href="api/metrics.csv"+exportQuery;$("channel-export").href="api/metrics.csv"+exportQuery+(focusKey?"&channel="+encodeURIComponent(focusKey):"");const provenance=[["会话",state.run_id||"—"],["算法",state.version||"—"],["完整分析 SHA256",state.analysis_sha256||"—"],["内核 SHA256",state.kernel_sha256||"—"],["硬件时间原点 ns",String(state.origin_ns??"—")],["最新硬件计数 ns",String(state.last_ns??"—")],["规则",state.config.sample_ns+" ns/sample; "+state.config.adc_mv+" mV/ADC; "+state.config.resistance+" Ω"],["指标数据库",bytes(state.database_bytes||0)],["运行模式",state.source?.kind==="root"?"完整 ROOT 回放":state.input_mode==="replay"?"本机 DAT 历史回放":state.input_mode==="remote"?"远程接收 DAT 增量监听":"本机 DAT 直读增量监听"]];$("provenance").innerHTML=provenance.map(([k,v])=>"<dt>"+esc(k)+"</dt><dd>"+esc(v)+"</dd>").join("");$("footer-source").textContent=(state.source?.key||"等待数据源")+" · "+(state.version||"");renderCharts();}
async function poll(){if(requestBusy||frozen)return;requestBusy=true;try{const response=await fetch("api/snapshot",{cache:"no-store"});if(!response.ok)throw Error("HTTP "+response.status);state=await response.json();failures=0;showError("");render();}catch(e){failures++;showError("无法更新网页："+e.message+"。已显示的数据保留；检查服务是否仍在运行。");$("connection").textContent="连接中断";$("lamp").className="bad";}finally{requestBusy=false;}}
$("select-source").addEventListener("click",()=>{sourceEditing=false;control({action:"select",source:$("source").value,follow:$("follow").checked});});$("source").addEventListener("change",()=>{sourceEditing=false;control({action:"select",source:$("source").value});});if($("string-filter"))$("string-filter").addEventListener("change",()=>{stringFilter=$("string-filter").value;sourceEditing=Boolean(stringFilter);knownSources="";updateControls();const target=state.sources.find(s=>s.string_group===stringFilter);if(target){sourceEditing=false;control({action:"select",source:target.key});}});$("follow").addEventListener("change",()=>control({action:"follow",follow:$("follow").checked}));$("pause").addEventListener("click",()=>control({action:state?.paused?"resume":"pause"}));$("freeze").addEventListener("click",()=>{frozen=!frozen;$("freeze").textContent=frozen?"恢复画面更新":"冻结画面";if(!frozen){exampleIndex="latest";poll();}});
$("restart").addEventListener("click",()=>control({action:"reset",source:state?.source?.key,follow:state?.follow}));$("focus").addEventListener("change",()=>{focusKey=$("focus").value;exampleIndex="latest";updateControls();renderWaveforms();});$("example").addEventListener("change",()=>{exampleIndex=$("example").value;if(exampleIndex!=="latest"){frozen=true;$("freeze").textContent="恢复画面更新";}renderWaveforms();});
["population","units","logy","normalized","trend-range","show-stat-errors"].forEach(id=>$(id).addEventListener("change",()=>{if(state)render();}));$("reset-zoom").addEventListener("click",()=>charts.forEach(c=>c.dispatchAction({type:"restore"})));
$("config-form").addEventListener("input",()=>{configDirty=true;});$("config-form").addEventListener("submit",event=>{event.preventDefault();if(!state?.source)return;const config={...state.config};for(const [key,value] of new FormData(event.currentTarget))config[key]=Number(value);configDirty=false;control({action:"configure",source:state.source.key,config,follow:state.follow});});
window.addEventListener("resize",()=>charts.forEach(c=>c.resize()));poll();setInterval(poll,2000);

$("summary-export").addEventListener("click",event=>{if(!state)return;event.preventDefault();const url=URL.createObjectURL(new Blob([JSON.stringify({...state,trend_view:typeof rawTrends!=="undefined"?rawTrends.displaySettings():null,value_histograms:typeof dashboard!=="undefined"?dashboard.histogramSettings():{}},null,2)],{type:"application/json"}));const a=document.createElement("a");a.href=url;a.download="seastar-summary-"+(state.run_id||"waiting")+".json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});

function renderTransfer(){
 const t=state?.transfer;$("transfer-panel").hidden=!t;if(!t)return;
 const labels={starting:"准备接收",connecting:"正在连接",receiving:"增量接收中",waiting:"等待源文件追加",reconnecting:"断线后退避重连",blocked:"已停止 · 需要核查",paused:"接收已暂停",disk_full:"工控机空间不足",stopped:"接收已停止"};
 $("transfer-status").textContent=labels[t.status]||t.status;
 $("transfer-source").textContent=t.host+":"+t.remote_dir+" · "+t.pattern+" · 限速 "+fmt(t.max_mib_per_second,2)+" MiB/s · "+t.received_files+" 个接收文件 · "+t.connections+" 次成功连接"+(t.file?" · "+t.file:"");
 $("transfer-bytes").textContent=bytes(t.committed_bytes);$("transfer-backlog").textContent=bytes(t.backlog_bytes);
 $("transfer-free").textContent=t.source_free_bytes===null?"尚未获取":bytes(t.source_free_bytes);
 $("transfer-mode").textContent=t.file_noatime===null?"尚未读取":t.file_noatime?"只读 · 免 atime":"只读 · 可能更新 atime";
 $("receive-pause").textContent=t.paused?"继续接收":t.status==="blocked"?"核查后重试":"暂停接收";
}
$("receive-pause").addEventListener("click",()=>{const t=state?.transfer;if(t)control({action:t.paused||t.status==="blocked"?"receive-resume":"receive-pause"});});

function renderResources(){
 const node=$("runtime-status");if(!node)return;
 const r=state.resources,q=state.query_tasks;
 node.textContent=(r?"监控进程常驻内存 "+bytes(r.rss_bytes)+" · "+(state.resource_paused?"内存保护：完整分析暂缓，轻量计数继续":"资源正常"):"")+
  (q?" · 重统计 "+(q.active?"执行中 PID "+q.active.pid+" · "+bytes(q.active.rss_bytes||0):"空闲")+" · 排队 "+q.queued:"");
 node.title=r?(r.processes||[]).map(p=>p.role+" PID "+p.pid+" · "+bytes(p.rss_bytes)).join("\n"):"";
}
