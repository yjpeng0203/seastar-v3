"use strict";
const dashboard = (() => {
  let expanded = null, anchor = null, lastTasks = "";
  const models = new Map(), fullRawExtent = new Set();
  const rangeRecords=new Map(), rangeStorage="seastar.detail-histograms.v2";
  let ranges={};try{ranges=JSON.parse(localStorage.getItem(rangeStorage)||"{}");}catch(_){}
  if(!ranges||typeof ranges!=="object"||Array.isArray(ranges))ranges={};
  if(ranges.rates&&ranges.rates.unit!=="Hz")delete ranges.rates;
  function validRange(s){return s&&Number.isFinite(s.min)&&Number.isFinite(s.max)&&s.min<s.max&&Number.isFinite(s.max-s.min)&&Number.isInteger(s.bins)&&s.bins>=1&&s.bins<=4096;}
  function rangeControls(id,unit,factor,automatic,trend=true){
    let r=rangeRecords.get(id);
    if(!r){
      r={dirty:false};r.form=document.createElement("form");r.form.id=id+"-profile-settings";r.form.className="histogram-settings";
      r.form.setAttribute("aria-label",trend?"右侧直方图范围与分箱设置":"直方图范围与分箱设置");
      for(const [field,title] of [["min",trend?"读数下限":"横轴下限"],["max",trend?"读数上限":"横轴上限"],["bins","bin 数"]]){
        const label=document.createElement("label"),caption=document.createElement("span"),input=document.createElement("input");
        caption.textContent=title;label.append(caption);input.id=id+"-profile-"+field;input.name=field;input.type="number";input.required=true;
        input.step=field==="bins"?"1":"any";if(field==="bins"){input.min="1";input.max="4096";}
        label.append(input);r.form.append(label);r[field]=input;r[field+"Caption"]=caption;
        input.addEventListener("input",()=>{r.dirty=true;r.message.textContent="输入尚未确认，图形保持已生效设置。";});
      }
      const confirm=document.createElement("button");confirm.type="submit";confirm.textContent="确认";confirm.className="primary";
      const reset=document.createElement("button");reset.type="button";reset.textContent="恢复自动";
      if(trend){
        r.extent=document.createElement("button");r.extent.type="button";r.extent.id=id+"-raw-extent";
        r.extent.onclick=()=>{
          fullRawExtent.has(id)?fullRawExtent.delete(id):fullRawExtent.add(id);
          delete ranges[id];try{localStorage.setItem(rangeStorage,JSON.stringify(ranges));}catch(_){}
          r.dirty=false;charts.get(id)?.dispatchAction({type:"dataZoom",dataZoomId:"value-slider",start:0,end:100});renderCharts(true);
        };r.form.append(r.extent);
      }
      r.message=document.createElement("span");r.message.className="histogram-message";r.message.setAttribute("role","status");
      r.form.append(confirm,reset,r.message);document.getElementById(id).before(r.form);
      const apply=()=>{try{localStorage.setItem(rangeStorage,JSON.stringify(ranges));}catch(_){}
        r.dirty=false;charts.get(id)?.dispatchAction({type:"dataZoom",dataZoomId:trend?"value-slider":"inside",start:0,end:100});renderCharts(true);};
      r.form.addEventListener("submit",event=>{
        event.preventDefault();const spec={min:Number(r.min.value)/r.factor,max:Number(r.max.value)/r.factor,bins:Number(r.bins.value)};
        if(!r.min.value.trim()||!r.max.value.trim()||!validRange(spec)){r.message.textContent="请输入有限的上下限（下限 < 上限），bin 数为 1–4096 的整数。";return;}
        if(id==="rates")spec.unit="Hz";ranges[id]=spec;fullRawExtent.delete(id);apply();
      });reset.onclick=()=>{delete ranges[id];fullRawExtent.delete(id);apply();};rangeRecords.set(id,r);
    }
    const manual=validRange(ranges[id]),spec=manual?{min:ranges[id].min*factor,max:ranges[id].max*factor,bins:ranges[id].bins}:automatic;
    if(!r.dirty||r.factor!==factor){r.min.value=String(spec.min);r.max.value=String(spec.max);r.bins.value=String(spec.bins);r.dirty=false;}
    if(r.extent)r.extent.textContent=fullRawExtent.has(id)?"按趋势范围":id==="rates"?"计数率全范围":"原值全范围";
    r.factor=factor;r.minCaption.textContent=(trend?"分布下限 / ":"横轴下限 / ")+unit;r.maxCaption.textContent=(trend?"分布上限 / ":"横轴上限 / ")+unit;
    if(!r.dirty)r.message.textContent=(manual?"自定义已生效":"自动分箱")+" · Δbin="+fmt((spec.max-spec.min)/spec.bins,6)+" "+unit+(trend?" · 范围同步作用于左右 y 轴，范围外计数见图下统计":" · 范围外计数单独保留");
    return {...spec,manual};
  }
  function profileStatistics(id,model,lo,hi){
    let note=document.getElementById(id+"-range-statistics");
    if(!note){note=document.createElement("p");note.id=id+"-range-statistics";note.className="profile-note";document.getElementById(id+"-clock").after(note);}
    if(model.stored){note.textContent="所选完整时段的原始观测分布统计："+model.list.map((s,i)=>{
      const d=model.stored[i];return d?s.name+" N="+fmt(d.n,0)+" μ="+fmt(d.mean===null?null:d.mean*model.width,4)+" σ="+fmt(d.sigma===null?null:d.sigma*model.width,4)+"；范围下/上方 "+d.underflow+" / "+d.overflow:"";
    }).filter(Boolean).join("； ");return;}
    note.textContent=(model.rate?"所选时段计数率分布（N 为时间箱数）：":"当前显示页原始观测统计：")+model.list.map(series=>{
      let n=0,mean=0,m2=0,under=0,over=0,missing=0;
      for(const [t,v] of series.data){if(t<lo||t>hi)continue;if(!Number.isFinite(v)){missing++;continue;}
        if(v<model.spec.min){under++;continue;}if(v>model.spec.max){over++;continue;}
        n++;const delta=v-mean;mean+=delta/n;m2+=delta*(v-mean);
      }
      return series.name+" N="+fmt(n,0)+" μ="+fmt(n?mean:null,4)+" σ="+fmt(n?Math.sqrt(Math.max(0,m2/n)):null,4)+"；范围下/上方 "+under+" / "+over+"；缺失 "+missing;
    }).join("； ");
  }
  const dialog = document.createElement("dialog"); dialog.id = "plot-dialog";
  dialog.innerHTML = '<div class="detail-heading"><h2 id="detail-title"></h2><button type="button" id="close-detail">关闭 · Esc</button></div><div id="detail-tools" class="tools"></div><div id="detail-content"></div>';
  document.body.append(dialog);
  document.getElementById("close-detail").onclick = () => dialog.close();
  dialog.addEventListener("close", () => {
    if (!expanded) return;
    const article = expanded.closest("article"); anchor.replaceWith(article); article.classList.remove("expanded-card");
    expanded = null; renderCharts(true); requestAnimationFrame(() => charts.forEach(c => c.resize()));
  });
  function isExpanded(id) { return expanded?.id === id; }
  function detailTools() {
    const container = document.getElementById("detail-tools"); container.replaceChildren();
    const id=expanded?.id, hist=id?.endsWith("Hist")||id==="multiplicity", trend=models.has(id);
    const convertible=["amplitudeHist","chargeHist","tailHist","chargeTrend","waveform","average","aligned","density","correlation"].includes(id);
    const units=convertible?["units"]:[];
    const wave=["waveform","average","density"].includes(id);
    const population=["pairHist","intervalHist","triggerIntervalHist","multiplicity"].includes(id)?[]:["population"];
    const ids=hist?[...population,...units,"logy","normalized"]:trend?[...units,"trend-range",...(typeof rawTrends!=="undefined"?["raw-segment","raw-time-min","raw-time-max","time-bin-enabled","time-bin-seconds","show-stat-errors"]:[])]:wave?[...units,"focus",...(id==="waveform"?["example"]:[])]:units;
    for (const id of ids) {
      const original = document.getElementById(id), label = document.createElement("label");
      const control = original.cloneNode(true); control.id = "detail-" + id;
      label.textContent = ({population:"统计样本 ",units:"显示单位 ",logy:"logY ",normalized:"归一化 ","trend-range":"趋势范围 ",focus:"通道 ",example:"抽样波形 ","raw-segment":"时间段 ","raw-time-min":"采集秒下限 ","raw-time-max":"上限 ","time-bin-enabled":"时间分箱 ","time-bin-seconds":"时间箱宽 / s ","show-stat-errors":"统计误差带 "})[id];
      control.value = original.value; control.checked = original.checked;
      control.addEventListener("change", () => {original.value = control.value; original.checked = control.checked; original.dispatchEvent(new Event("change"));});
      label.append(control); container.append(label);
    }
    if(trend&&typeof rawTrends!=="undefined"){
      for(const id of ["raw-apply","time-bin-apply","raw-refresh","raw-prev","raw-next"]){
        const original=document.getElementById(id),copy=original.cloneNode(true);copy.id="detail-"+id;copy.disabled=original.disabled;
        copy.onclick=()=>original.onclick();container.append(copy);
      }
      const info=document.createElement("span");info.id="detail-raw-status";info.className="raw-detail-status";
      info.textContent=document.getElementById("raw-status").textContent;container.append(info);
    }
    const channels=document.createElement("div");channels.className="detail-channels";
    document.querySelectorAll("#channel-filters label").forEach(original=>{
      const input=original.querySelector("input"),label=document.createElement("label"),copy=document.createElement("input");
      copy.type="checkbox";copy.checked=input.checked;
      copy.onchange=()=>{input.checked=copy.checked;input.dispatchEvent(new Event("change"));};
      label.append(copy,document.createTextNode(original.textContent));channels.append(label);
    });
    if(!["multiplicity","triggerIntervalHist","waveform","average","density"].includes(id))container.append(channels);
  }
  function open(id) {
    if (expanded) return;
    const plot = document.getElementById(id), article = plot.closest("article");
    expanded = plot; anchor = document.createElement("div"); anchor.className = "detail-placeholder";
    anchor.textContent = article.querySelector("h3").textContent + " · 详情已打开";
    article.replaceWith(anchor); document.getElementById("detail-content").append(article); article.classList.add("expanded-card");
    document.getElementById("detail-title").textContent = article.querySelector("h3").firstChild.textContent.trim();
    detailTools(); dialog.showModal(); renderCharts(true); requestAnimationFrame(() => charts.forEach(c => c.resize()));
  }
  function decorate() {
    document.querySelectorAll(".plot").forEach(plot => {
      if (plot.dataset.previewReady) return;
      plot.dataset.previewReady = "1"; plot.tabIndex = 0; plot.setAttribute("role", "button");
      plot.setAttribute("aria-label", plot.closest("article").querySelector("h3").textContent + "，点击放大详情");
      const button = document.createElement("button"); button.className = "expand-plot"; button.type = "button";
      button.textContent = "放大详情"; button.onclick = () => open(plot.id); plot.closest("article").querySelector("h3").append(button);
      plot.addEventListener("click", () => {if (!expanded) open(plot.id);});
      plot.addEventListener("keydown", e => {if (!expanded && ["Enter", " "].includes(e.key)) {e.preventDefault(); open(plot.id);}});
    });
  }
  function option(id, value) {
    if (isExpanded(id)) return value;
    value.tooltip = {show:false}; value.legend = {...value.legend, show:false}; value.toolbox = {show:false};
    if (!Array.isArray(value.grid)) value.grid = {left:54,right:16,top:24,bottom:42};
    for (const axis of [...(Array.isArray(value.xAxis) ? value.xAxis : [value.xAxis]), ...(Array.isArray(value.yAxis) ? value.yAxis : [value.yAxis])]) {
      if (!axis) continue; axis.splitNumber = 3; axis.axisLabel = {...axis.axisLabel,fontSize:9};
      axis.nameTextStyle = {...axis.nameTextStyle,fontSize:9}; axis.nameGap = axis.type === "value" ? 26 : axis.nameGap;
    }
    value.dataZoom = (value.dataZoom || []).map(v => ({...v,show:false,disabled:true,zoomOnMouseWheel:false,moveOnMouseMove:false}));
    value.series.forEach(s => {s.silent = true;}); return value;
  }
  function tasks() {
    const node = document.getElementById("string-tasks"); if (!node) return;
    const signature = JSON.stringify([state.source?.key,state.tasks || []]); if (signature === lastTasks) return; lastTasks = signature;
    node.replaceChildren(...(state.tasks || []).map(t => {
      const button = document.createElement("button"); button.type = "button"; button.className = "string-task";
      button.classList.toggle("selected", t.key === state.source?.key);
      button.innerHTML = '<strong>'+esc(t.group)+'</strong><span>'+esc(statusLabels[t.status] || t.status)+'</span><b>'+fmt(t.waves,0)+' 波形</b><small>'+esc(t.error || (t.cache_mode === "validated analysis cache" ? "已复用完整分析结果" : t.cache_mode ? "首次分析 · "+fmt(t.rate,0)+" 波形/s"+(t.eta_seconds?" · 约剩 "+fmt(t.eta_seconds/60,1)+" 分钟":"") : "独立后台任务"))+'</small>';
      button.onclick = () => {sourceEditing = false; control({action:"select",source:t.key});}; return button;
    }));
  }
  function positiveAxis(option) {
    if (option.yAxis.type !== "log") return;
    let low = Infinity, high = 0;
    option.series.forEach(s => s.data.forEach(v => {const n = v[1]; if (n > 0) {low = Math.min(low,n); high = Math.max(high,n);}}));
    option.yAxis.min = Number.isFinite(low) ? Math.pow(10,Math.floor(Math.log10(low))) / 1.25 : .8;
    option.yAxis.max = high ? Math.pow(10,Math.ceil(Math.log10(high * 1.05))) : 10;
    option.yAxis.axisLabel.formatter = v => v >= 10000 || v < .001 ? Number(v).toExponential(0) : fmt(v,5);
  }
  const clockFormat = new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
  function reference(id, lo, hi) {
    let p = document.getElementById(id + "-clock");
    if (!p) {p = document.createElement("p");p.id = id+"-clock";p.className = "time-reference";document.getElementById(id).after(p);}
    const r = state.beijing_reference;
    p.textContent = r ? "北京时间参考：约 " + clockFormat.format(new Date(r.origin_unix_ms + lo*1000)) + " ～ " + clockFormat.format(new Date(r.origin_unix_ms + hi*1000)) + "（分钟级估计，非跨串同步）" : "北京时间参考：当前区段无可用 CSV 锚点";
    p.title = r ? r.label+"；有效锚点 "+r.anchors+"，剔除异常 "+r.rejected : "相对采集时间及全部计算保持不变";
  }
  function histogramSeries(name, color, rows, spec, lo, hi,stored=null,factor=1) {
    const width=(spec.max-spec.min)/spec.bins;
    const counts = new Map();
    for (const [t,v] of rows) if (t>=lo && t<=hi && Number.isFinite(v) && v>=spec.min && v<=spec.max) {const bin=Math.min(spec.bins-1,Math.max(0,Math.floor((v-spec.min)/width)));counts.set(bin,(counts.get(bin)||0)+1);}
    if(stored&&stored.hist.length>5000){
      const edges=[];let previous=null;
      for(const [x,n] of stored.hist){const a=(x-stored.bin_width/2)*factor,b=(x+stored.bin_width/2)*factor;
        if(previous!==null&&a>previous+Math.abs(stored.bin_width*factor)*.01)edges.push([0,previous],[0,a]);
        edges.push([n,a],[n,b]);previous=b;
      }
      return {id:"side-"+name,name,type:"line",xAxisIndex:1,yAxisIndex:1,data:edges,showSymbol:false,lineStyle:{color,width:1},emphasis:{disabled:true}};
    }
    const bars=stored?stored.hist.map(([x,n])=>[n,(x-stored.bin_width/2)*factor,(x+stored.bin_width/2)*factor]):[...counts].map(([bin,n])=>[n,spec.min+bin*width,bin===spec.bins-1?spec.max:spec.min+(bin+1)*width]);
    return {id:"side-"+name,name,type:"custom",xAxisIndex:1,yAxisIndex:1,
      dimensions:["观测记录数","读数下界","读数上界"],encode:{x:0,y:[1,2]},
      tooltip:{trigger:"item",formatter:p=>esc(name)+"<br>观测记录数："+fmt(p.value[0],0)+"<br>读数：["+fmt(p.value[1],4)+", "+fmt(p.value[2],4)+")"},
      data:bars,
      renderItem:(params,api)=>{
        const a=api.coord([0,api.value(1)]),b=api.coord([api.value(0),api.value(2)]);
        const shape=echarts.graphic.clipRectByRect({x:a[0],y:b[1],width:Math.max(0,b[0]-a[0]),height:Math.max(1,a[1]-b[1])},params.coordSys);
        return shape?{type:"rect",shape,style:{fill:color,opacity:.4}}:{type:"group",children:[]};
      },emphasis:{disabled:true}};
  }
  function uncertaintyBand(series,index){
    const groups=[];let group=[];
    for(const p of series.uncertainty||[]){if(Number.isFinite(p[1])&&Number.isFinite(p[2]))group.push(p);else if(group.length){groups.push(group);group=[];}}
    if(group.length)groups.push(group);
    return {id:series.id+"-stat-band",name:series.name,type:"custom",coordinateSystem:"cartesian2d",clip:true,data:groups.map((_,i)=>[i]),silent:true,z:1,tooltip:{show:false},emphasis:{disabled:true},
      renderItem:(params,api)=>{const g=groups[params.dataIndex],points=[];
        if(g.length===1){const p=g[0];points.push(api.coord([p[3],p[1]]),api.coord([p[4],p[1]]),api.coord([p[4],p[2]]),api.coord([p[3],p[2]]));}
        else{for(const p of g)points.push(api.coord([p[0],p[1]]));for(let i=g.length-1;i>=0;--i)points.push(api.coord([g[i][0],g[i][2]]));}
        return {type:"polygon",shape:{points},style:{fill:"#777777",opacity:.18}};}};
  }
  function trend(id,list,yName,width=1,view={}) {
    let minT=Infinity,maxT=-Infinity,minV=Infinity,maxV=-Infinity;
    const legend=charts.get(id)?.getOption().legend?.[0]?.selected||{};
    list.filter(s=>legend[s.name]!==false).forEach(s=>s.data.forEach(([t,v])=>{if(Number.isFinite(t)){minT=Math.min(minT,t);maxT=Math.max(maxT,t);}if(Number.isFinite(v)){minV=Math.min(minV,v);maxV=Math.max(maxV,v);}}));
    const showErrors=document.getElementById("show-stat-errors")?.checked!==false;
    if(showErrors)for(const s of list){if(legend[s.name]===false)continue;for(const p of s.uncertainty||[]){if(Number.isFinite(p[1]))minV=Math.min(minV,p[1]);if(Number.isFinite(p[2]))maxV=Math.max(maxV,p[2]);}}
    if (!Number.isFinite(minT)) {minT=0;maxT=1;}if(minT===maxT)maxT=minT+1;
    if(view.bounds){[minT,maxT]=view.bounds;}else if(state.raw_page_bounds){[minT,maxT]=state.raw_page_bounds;}else if(document.getElementById("trend-range").value==="all"&&Number.isFinite(state.relative_s)){minT=0;maxT=Math.max(maxT,state.relative_s);}
    if (!Number.isFinite(minV)) {minV=0;maxV=1;}
    const trendLow=minV,trendHigh=maxV;
    const metric=({rates:"rate",baselineTrend:"baseline",noiseTrend:"noise",amplitudeTrend:"amplitude",chargeTrend:"area",qualityTrend:"quality"})[id];
    const stored=state.raw_value_distributions?.[metric];
    const rawFor=series=>{if(id==="rates")return undefined;const source=series.raw_code===-1?state.raw_value_distributions?.event_interval:stored;return source?.find(v=>v.code===series.raw_code);};
    const allStored=list.filter(s=>legend[s.name]!==false).map(rawFor).filter(Boolean);
    if(allStored.length){minV=Math.min(...allStored.map(v=>v.min*width));maxV=Math.max(...allStored.map(v=>v.max*width));}
    const autoLo=id==="qualityTrend"?-.5:Math.floor(minV/width)*width,autoHi=id==="qualityTrend"?1.5:(Math.floor(maxV/width)+1)*width;
    const spec=rangeControls(id,yName,width,{min:allStored.length?minV:autoLo,max:allStored.length?maxV:autoHi,bins:allStored.length?allStored[0].bins:Math.max(1,Math.round((autoHi-autoLo)/width))});
    if(spec.manual){minV=spec.min;maxV=spec.max;}
    else if(fullRawExtent.has(id)){
      const pad=Math.max((maxV-minV)*.05,Math.abs(maxV)*.01,1e-9);
      minV=Math.min(minV-pad,spec.min);maxV=Math.max(maxV+pad,spec.max);
    }else if(id==="qualityTrend"){minV=0;maxV=1;}
    else{
      const span=trendHigh-trendLow,pad=span>0?span*.05:Math.max(Math.abs(trendLow)*.01,width*.05,1e-9);
      minV=trendLow>=0?Math.max(0,trendLow-pad):trendLow-pad;maxV=trendHigh+pad;
    }
    let viewNote=document.getElementById(id+"-view-range");
    if(!viewNote){viewNote=document.createElement("p");viewNote.id=id+"-view-range";viewNote.className="view-range-note";document.getElementById(id).after(viewNote);}
    let below=0,above=0,boundary=0;
    if(allStored.length){for(const d of allStored){below+=d.underflow||0;above+=d.overflow||0;
      for(const [x,n] of d.hist){if(id==="qualityTrend"&&d.min===-.5&&d.max===1.5&&d.bins===2){if(x*width<minV)below+=n;else if(x*width>maxV)above+=n;continue;}const a=(x-d.bin_width/2)*width,b=(x+d.bin_width/2)*width;
        if(b<minV)below+=n;else if(a>maxV)above+=n;
        else if(a<minV||b>maxV)boundary+=n;
      }
    }}else{for(const series of list)for(const [t,v] of series.data){if(!Number.isFinite(v))continue;if(v<minV)below++;else if(v>maxV)above++;}}
    viewNote.textContent=(spec.manual?"手动视窗":fullRawExtent.has(id)?"原值全范围":"趋势自适应")+" · 共享 y："+fmt(minV,6)+"–"+fmt(maxV,6)+" "+yName+
      " · 视窗外"+(boundary?"至少":"")+"：下 "+fmt(below,0)+" / 上 "+fmt(above,0)+(boundary?"；跨边界箱 "+fmt(boundary,0)+" 条，未细分":"")+" · 原始数据、分箱及完整计数保留";
    const detail=isExpanded(id), option=baseOption("相对采集时间 / s",yName);
    option.grid=[{left:detail?75:54,right:"32%",top:detail?55:25,bottom:detail?75:43},{left:"77%",right:detail?38:16,top:detail?55:25,bottom:detail?75:43}];
    const timeAxis={...option.xAxis,id:"time-axis",gridIndex:0,min:minT,max:maxT};
    option.xAxis=[timeAxis,{id:"count-axis",type:"value",gridIndex:1,name:"观测记录数",nameLocation:"middle",nameGap:28,splitNumber:2,axisLabel:{hideOverlap:true,formatter:v=>fmt(v,0)},splitLine:{show:false}}];
    option.yAxis=[{...option.yAxis,gridIndex:0,min:minV,max:maxV},{...option.yAxis,gridIndex:1,name:"",min:minV,max:maxV,axisLabel:{show:false}}];
    option.dataZoom=[{id:"time-inside",type:"inside",xAxisIndex:0,filterMode:"none"},{id:"time-slider",type:"slider",xAxisIndex:0,height:14,bottom:12,left:detail?75:54,right:"32%",showDetail:false},
      {id:"value-slider",type:"slider",yAxisIndex:[0,1],filterMode:"none",right:0,width:12,showDetail:false},
      {id:"count-slider",type:"slider",xAxisIndex:1,filterMode:"none",height:14,bottom:12,left:"77%",right:30,showDetail:false}];
    const old=charts.get(id)?.getOption().dataZoom?.find(d=>d.id==="time-slider"),a=old?.start??0,b=old?.end??100;
    const lo=minT+(maxT-minT)*a/100,hi=minT+(maxT-minT)*b/100;
        list.forEach(s=>{s.z=3;});
    option.series=list.concat(showErrors?list.filter(s=>s.uncertainty?.length).map(uncertaintyBand):[],list.map((s,i)=>histogramSeries(s.name,palette[i%palette.length],s.data,spec,lo,hi,rawFor(s),width)));
    if(id==="rates"){option.xAxis[1].name="时间箱数";for(const s of option.series)if(s.id?.startsWith("side-")){s.dimensions=["时间箱数","Hz 下界","Hz 上界"];s.tooltip={trigger:"item",formatter:p=>esc(p.seriesName)+"<br>时间箱数："+fmt(p.value[0],0)+"<br>Hz：["+fmt(p.value[1],4)+", "+fmt(p.value[2],4)+")"};}}
    draw(id,option);reference(id,lo,hi);models.set(id,{list,width,minT,maxT,spec,rate:id==="rates",stored:allStored.length?list.map(rawFor):null});profileStatistics(id,models.get(id),lo,hi);
    let note=document.getElementById(id+"-profile-note");
    if(!note){note=document.createElement("p");note.className="profile-note";note.id=id+"-profile-note";document.getElementById(id+"-clock").after(note);}
    note.textContent=view.note||state.raw_trend_note||"右侧按原始观测数值分箱；纵轴缩放仅改变可视范围。";
    const chart=charts.get(id);if(!chart || chart.__profileBound)return;chart.__profileBound=true;
    chart.on("legendselectchanged",()=>setTimeout(()=>renderCharts(true),0));
    chart.on("datazoom",()=>{
      const m=models.get(id),zoom=chart.getOption().dataZoom.find(v=>v.id==="time-slider");if(!m||!zoom)return;
      const low=m.minT+(m.maxT-m.minT)*zoom.start/100,high=m.minT+(m.maxT-m.minT)*zoom.end/100;
      if(m.stored){reference(id,low,high);return;}
      chart.setOption({series:m.list.map((s,i)=>histogramSeries(s.name,palette[i%palette.length],s.data,m.spec,low,high))});reference(id,low,high);profileStatistics(id,m,low,high);
    });
  }
  function countHistogram(id,rows,xName,yName){
    rows=rows.map(([v,n])=>[Number(v),Number(n)]).filter(([v,n])=>Number.isFinite(v)&&Number.isFinite(n)&&n>0);
    const values=rows.map(v=>v[0]),low=values.length?Math.min(...values)-.5:-.5,high=values.length?Math.max(...values)+.5:.5;
    const spec=rangeControls(id,xName,1,{min:low,max:high,bins:Math.max(1,Math.round(high-low))},false),width=(spec.max-spec.min)/spec.bins;
    let n=0,mean=0,m2=0,under=0,over=0;const counts=new Map();
    for(const [v,w] of rows){
      if(v<spec.min){under+=w;continue;}if(v>spec.max){over+=w;continue;}
      const before=n;n+=w;const delta=v-mean;mean+=delta*w/n;m2+=delta*delta*before*w/n;
      const bin=Math.min(spec.bins-1,Math.max(0,Math.floor((v-spec.min)/width)));counts.set(bin,(counts.get(bin)||0)+w);
    }
    const normalized=document.getElementById("normalized").checked,log=document.getElementById("logy").checked;
    const data=[...counts].map(([bin,w])=>[normalized&&n?w/n:w,spec.min+bin*width,bin===spec.bins-1?spec.max:spec.min+(bin+1)*width]);
    const option=baseOption(xName,normalized?"概率 / bin":yName+" / bin");option.legend={show:false};option.tooltip={trigger:"item",formatter:p=>"["+fmt(p.value[1],4)+", "+fmt(p.value[2],4)+"]<br>"+(normalized?"概率：":"计数：")+fmt(p.value[0],6)};
    option.xAxis.min=spec.min;option.xAxis.max=spec.max;option.yAxis.type=log?"log":"value";option.yAxis.scale=false;
    positiveAxis({yAxis:option.yAxis,series:[{data:data.map(v=>[v[1],v[0]])}]});
    option.series=[{id:id+"-counts",name:yName,type:"custom",data,dimensions:["频数","下界","上界"],encode:{x:[1,2],y:0},
      renderItem:(params,api)=>{const a=api.coord([api.value(1),log?option.yAxis.min:0]),b=api.coord([api.value(2),api.value(0)]);
        const shape=echarts.graphic.clipRectByRect({x:a[0],y:b[1],width:Math.max(.5,b[0]-a[0]),height:Math.max(0,a[1]-b[1])},params.coordSys);
        return shape?{type:"rect",shape,style:{fill:palette[0],opacity:.7}}:null;},emphasis:{disabled:true}}];draw(id,option);
    let note=document.getElementById(id+"-count-summary");if(!note){note=document.createElement("p");note.id=id+"-count-summary";note.className="distribution-summary";document.getElementById(id).after(note);}
    note.textContent="范围内 N="+fmt(n,0)+" μ="+fmt(n?mean:null,4)+" σ="+fmt(n?Math.sqrt(Math.max(0,m2/n)):null,4)+"；范围下/上方 "+fmt(under,0)+" / "+fmt(over,0)+"。统计使用原始整数重数及其计数。";
  }
  function histogramSettings(){const out={};for(const [id,name] of Object.entries({baselineTrend:"baseline",noiseTrend:"noise",amplitudeTrend:"amplitude",chargeTrend:"area",qualityTrend:"quality",rates:"rate"}))if(validRange(ranges[id]))out[name]=ranges[id];return out;}
  return {histogramSettings,isExpanded,open,decorate,option,tasks,positiveAxis,trend,countHistogram};
})();
