"use strict";
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const echarts=require('../../../fee-monitor/vendor/echarts.min.js');
const nodes=new Map();
function node(id){if(!nodes.has(id))nodes.set(id,{id,value:id==='trend-range'?'all':'',textContent:'',style:{},dataset:{},
  listeners:{},append(){},after(){},before(){},setAttribute(){},addEventListener(type,callback){this.listeners[type]=callback;},querySelector(){return node(id+'-child');},replaceChildren(){}});return nodes.get(id);}
const charts=new Map();
const context=vm.createContext({console,Map,Set,Intl,Number,Date,Math,echarts,charts,setTimeout:(f)=>setImmediate(f),palette:['#167c78'],
  state:{source:{key:'G3'},tasks:[],relative_s:3,beijing_reference:{origin_unix_ms:1793537432000,anchors:100,rejected:1}},
  document:{createElement:()=>{const n=node(Symbol());Object.defineProperty(n,"id",{get(){return this.nodeId;},set(id){this.nodeId=id;nodes.set(id,this);}});return n;},getElementById:node,body:{append(){}},querySelectorAll:()=>[]},
  fmt:(v)=>String(v),esc:v=>String(v),
  baseOption:(x,y)=>({animation:false,grid:{},xAxis:{type:'value',name:x},yAxis:{type:'value',name:y},legend:{},series:[],dataZoom:[]}),
  draw:(id,option)=>{let c=charts.get(id);if(!c){c=echarts.init(null,null,{renderer:'svg',ssr:true,width:1100,height:500});charts.set(id,c);}c.setOption(option,{notMerge:true});}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/dashboard.js'),'utf8'),context);
vm.runInContext('dashboard.trend("baselineTrend",[{id:"CH0",name:"CH0",type:"line",showSymbol:false,data:[[0,10],[1,null],[2,10],[3,12]]}],"ADC",1)',context);
const chart=charts.get('baselineTrend');
let option=chart.getOption();
assert.equal(option.yAxis[0].min,option.yAxis[1].min);assert.equal(option.yAxis[0].max,option.yAxis[1].max);
for(const y of [10,12])assert.equal(chart.convertToPixel({yAxisIndex:0},y),chart.convertToPixel({yAxisIndex:1},y));
let bars=option.series.find(s=>s.id==='side-CH0').data;
assert.equal(bars.reduce((n,b)=>n+b[0],0),3);assert.deepEqual(Array.from(bars[0]),[2,10,11]);
assert.match(node('baselineTrend-clock').textContent,/北京时间参考/);
const before=JSON.stringify(option.series.find(s=>s.id==='CH0').data);
context.renderCharts=()=>vm.runInContext('dashboard.trend("baselineTrend",[{id:"CH0",name:"CH0",type:"line",showSymbol:false,data:[[0,10],[1,null],[2,10],[3,12]]}],"ADC",1)',context);
chart.dispatchAction({type:'dataZoom',dataZoomId:'time-slider',start:0,end:50});
option=chart.getOption();bars=option.series.find(s=>s.id==='side-CH0').data;
assert.equal(bars.reduce((n,b)=>n+b[0],0),1);
assert.equal(JSON.stringify(option.series.find(s=>s.id==='CH0').data),before);
chart.dispatchAction({type:'dataZoom',dataZoomId:'time-slider',start:0,end:100});
node('baselineTrend-profile-min').value='10';node('baselineTrend-profile-max').value='11';node('baselineTrend-profile-bins').value='10';
const form=[...nodes.values()].find(n=>n.className==='histogram-settings');
form.listeners.submit({preventDefault(){}});
option=chart.getOption();assert.equal(option.yAxis[0].min,10);assert.equal(option.yAxis[1].max,11);
bars=option.series.find(s=>s.id==='side-CH0').data;
assert.equal(bars.reduce((n,b)=>n+b[0],0),2);assert(Math.abs(bars[0][2]-bars[0][1]-.1)<1e-12);
assert.match(node('baselineTrend-range-statistics').textContent,/范围下\/上方 0 \/ 1/);
vm.runInContext('dashboard.trend("baselineTrend",[{id:"CH0",name:"CH0",type:"line",showSymbol:false,data:[[0,.2],[1,null],[2,.2],[3,.24]]}],"pC",.02)',context);
assert.equal(chart.getOption().yAxis[0].min,.2);assert.equal(chart.getOption().yAxis[1].max,.22);
assert.equal(node('baselineTrend-profile-bins').value,'10');
const reset=[...nodes.values()].find(n=>n.textContent==='恢复自动');reset.onclick();
assert.equal(chart.getOption().series.find(s=>s.id==='side-CH0').data.reduce((n,b)=>n+b[0],0),3);
const bounds=vm.runInContext('(()=>{const o={yAxis:{type:"log",axisLabel:{}},series:[{data:[[0,0],[1,1],[2,1000000]]}]};dashboard.positiveAxis(o);return o.yAxis;})()',context);
assert(bounds.min>0&&bounds.min<1);assert(bounds.max>1000000);
const normalized=vm.runInContext('(()=>{const o={yAxis:{type:"log",axisLabel:{}},series:[{data:[[0,1e-7],[1,.5]]}]};dashboard.positiveAxis(o);return o.yAxis;})()',context);
assert(normalized.min>0&&normalized.min<1e-7);
const thumbnail=vm.runInContext('dashboard.option("thumbnail",{grid:{},xAxis:{},yAxis:{},series:[],dataZoom:[{type:"inside"}]})',context);
assert.equal(thumbnail.dataZoom[0].disabled,true);assert.equal(thumbnail.toolbox.show,false);
const appCode=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
context.renderResources=()=>{}; // The partial render extraction below tests channel controls only.
context.$=node;node('logy').checked=true;node('normalized').checked=false;
vm.runInContext(appCode.slice(appCode.indexOf('function addLogMarkers'),appCode.indexOf('function histogramSummary')),context);
const isolated=vm.runInContext('(()=>{const o={series:[]};addLogMarkers(o,[{name:"CH0",d:{hist:[[.5,2],[1.5,3],[3999.5,1]],bin_width:1,n:6}}],.02);return o.series[0];})()',context);
assert.equal(isolated.data.length,1);assert.equal(isolated.data[0][0],79.99);assert.equal(isolated.data[0][1],1);
node('logy').checked=false;node('normalized').checked=false;
context.renderCharts=()=>vm.runInContext('dashboard.countHistogram("multiplicity",[[0,2],[1,3],[3,1]],"每个触发的波形数","触发数")',context);
context.renderCharts();const multiples=charts.get('multiplicity');
assert.equal(multiples.getOption().series[0].data.reduce((n,v)=>n+v[0],0),6);
node('multiplicity-profile-min').value='0';node('multiplicity-profile-max').value='2';node('multiplicity-profile-bins').value='2';
node('multiplicity-profile-settings').listeners.submit({preventDefault(){}});
assert.equal(multiples.getOption().xAxis[0].min,0);assert.equal(multiples.getOption().xAxis[0].max,2);
assert.equal(multiples.getOption().series[0].data.reduce((n,v)=>n+v[0],0),5);
assert.match(node('multiplicity-count-summary').textContent,/范围下\/上方 0 \/ 1/);
// A raw outlier must not flatten a temporal mean curve by default.
context.state.raw_value_distributions={baseline:[{code:768,min:7,max:2614,bins:2607,bin_width:1,n:27,mean:200,sigma:10,underflow:0,overflow:0,hist:[[7.5,1],[160.5,2],[197.5,20],[230.5,3],[2613.5,1]]}]};
context.renderCharts=()=>vm.runInContext('dashboard.trend("baselineTrend",[{name:"CH0",raw_code:768,type:"line",data:[[0,165],[1,200],[3,222]]}],"ADC",1)',context);
context.renderCharts();let fitted=chart.getOption();
assert(Math.abs(fitted.yAxis[0].min-162.15)<1e-9);assert(Math.abs(fitted.yAxis[0].max-224.85)<1e-9);
assert.equal(fitted.yAxis[0].min,fitted.yAxis[1].min);assert.equal(fitted.yAxis[0].max,fitted.yAxis[1].max);
const rawBars=JSON.stringify(fitted.series.find(s=>s.id==='side-CH0').data),curve=JSON.stringify(fitted.series[0].data);
assert.match(node('baselineTrend-view-range').textContent,/下 3 \/ 上 4/);
node('baselineTrend-raw-extent').onclick();fitted=chart.getOption();assert(fitted.yAxis[0].max>2614);
assert.equal(JSON.stringify(fitted.series.find(s=>s.id==='side-CH0').data),rawBars);assert.equal(JSON.stringify(fitted.series[0].data),curve);
node('baselineTrend-raw-extent').onclick();assert(chart.getOption().yAxis[0].max<230);
context.state.raw_value_distributions.noise=[{code:768,min:0,max:753,bins:753,bin_width:1,n:101,mean:1,sigma:1,underflow:0,overflow:0,hist:[[.5,40],[1.5,60],[752.5,1]]}];
vm.runInContext('dashboard.trend("noiseTrend",[{name:"CH0",raw_code:768,type:"line",data:[[0,.9],[3,1.15]]}],"ADC",1)',context);
assert(charts.get('noiseTrend').getOption().yAxis[0].max<1.2);assert.match(node('noiseTrend-view-range').textContent,/跨边界箱 100/);
context.state.raw_value_distributions.quality=[{code:768,min:-.5,max:1.5,bins:2,bin_width:1,n:100,mean:.9,sigma:.3,underflow:0,overflow:0,hist:[[0,10],[1,90]]}];
vm.runInContext('dashboard.trend("qualityTrend",[{name:"CH0",raw_code:768,type:"line",data:[[0,.71],[3,.86]]}],"比例",1)',context);
assert.equal(charts.get('qualityTrend').getOption().yAxis[0].min,0);assert.equal(charts.get('qualityTrend').getOption().yAxis[0].max,1);
assert(!node('qualityTrend-view-range').textContent.includes('跨边界箱'));
// Statistical bands share the same coordinates and never change line values or histogram counts.
node('show-stat-errors').checked=true;
context.state.raw_value_distributions=null;
context.renderCharts=()=>vm.runInContext('dashboard.trend("rates",[{id:"CH0",name:"CH0",type:"line",data:[[.5,2,2,1],[1.5,0,0,1]],uncertainty:[[.5,.7,4.6,0,1,2],[1.5,0,1.84,1,2,0]]}],"Hz",1,{bounds:[0,2]})',context);
context.renderCharts();const rateChart=charts.get('rates');
assert.equal(rateChart.getOption().series[0].type,'line');
assert(rateChart.getOption().series.some(s=>s.id==='CH0-stat-band'));
assert(/#777|rgb\(119,119,119\)/.test(rateChart.renderToSVGString()));
assert(rateChart.getOption().yAxis[0].max>4.6);assert.equal(rateChart.getOption().xAxis[1].name,'时间箱数');
const rateLine=JSON.stringify(rateChart.getOption().series[0].data);
node('show-stat-errors').checked=false;context.renderCharts();assert(!rateChart.getOption().series.some(s=>s.id==='CH0-stat-band'));assert.equal(JSON.stringify(rateChart.getOption().series[0].data),rateLine);
node('show-stat-errors').checked=true;
const drawActual=context.draw;let legendResult;
charts.set('legendFit',{getOption:()=>({legend:[{selected:{event:false}}],dataZoom:[]}),on(){}});
context.draw=(id,option)=>{if(id==='legendFit')legendResult=option;else drawActual(id,option);};
vm.runInContext('dashboard.trend("legendFit",[{id:"CH0",name:"CH0",type:"line",data:[[0,2],[1,3]]},{id:"event",name:"event",type:"line",data:[[0,500],[1,600]]}],"Hz",1)',context);
assert(legendResult.yAxis[0].max<4);charts.delete('legendFit');
// A queued source has no channels: clear the previous string's controls.
node('channel-filters').replaceChildren=function(...items){this.children=items;};
node('channel-filters').children=['stale-G3-channel'];
node('config-form').elements={namedItem:()=>null};
context.state={status:'queued',source:{key:'G4',kind:'dat',string_group:'G4',files:[{}]},
  sources:[{key:'G4',kind:'dat',string_group:'G4',files:[{}]}],channels:[],config:{},input_mode:'local',follow:true};
vm.runInContext('let knownChannels="3:0",knownSources="",knownGroups="",lastRun="previous",stringFilter="",sourceEditing=false,configDirty=false,focusKey="3:0",exampleIndex="latest";const selected=new Set(["3:0"]);',context);
vm.runInContext(appCode.slice(appCode.indexOf('function sourceOptionText'),appCode.indexOf('const selectedChannels')),context);
vm.runInContext(appCode.slice(appCode.indexOf('function updateControls'),appCode.indexOf('function render(){')),context);
vm.runInContext(appCode.slice(appCode.indexOf('function render(){'),appCode.indexOf('updateControls();renderTransfer();'))+'updateControls();}',context);
vm.runInContext('render()',context);
assert.equal(node('channel-filters').children.length,0);
assert.equal(node('pause').disabled,true);
setImmediate(()=>{charts.forEach(c=>c.dispose());console.log('Shared-y alignment, complete time-bin counts, zoom, Beijing hint, log axes, statistical bands and thumbnails passed.');});
