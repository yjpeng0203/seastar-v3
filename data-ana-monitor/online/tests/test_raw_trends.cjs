"use strict";
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();function get(id){if(!nodes.has(id))nodes.set(id,{value:id==='trend-range'?'first':id==='population'?'all':'',options:[],textContent:'',disabled:false,replaceChildren(...v){this.options=v;}});return nodes.get(id);}
const rows=[[1,768,.1,.09999975,197,1,11,1,1,null,null],[2,768,.2,.19999975,198,2,22,10000,0,100000000,100000000],[3,768,.3,.29999975,199,3,33,3,1,100000000,100000000]];
const captured=new Map(),requests=[],rateRequests=[];
let clock=10000,holdRate=false,releaseRate=null,rateSignal=null;const timers=[];
const context=vm.createContext({console,Map,Number,Math,JSON,AbortController,performance:{now:()=>clock},setInterval:(fn,ms)=>timers.push({fn,ms}),frozen:false,
 document:{getElementById:get,createElement:()=>({})},state:{run_id:'a'.repeat(32),seq:6,relative_s:1,segment:0,input_mode:'replay'},
 selectedChannels:()=>[{key:'3:0'}],channelName:k=>k,palette:['#167c78'],scale:()=>1,unit:m=>m,fmt:v=>String(v),
 dashboard:{trend:(id,list,unit,width)=>captured.set(id,{list,unit,width})},
 fetch:async(_url,request)=>{const q=JSON.parse(request.body);if(_url==='api/rates'){rateRequests.push(q);rateSignal=request.signal;if(holdRate)await new Promise(resolve=>{releaseRate=resolve;});return {ok:true,json:async()=>({run_id:q.run_id,unit:'Hz',time_bin_s:q.time_bin_s,min_s:0,max_s:1,series:[{code:768,data:[[.5,3,3,1,1.367,5.918,0,1]]},{code:-1,data:[[.5,3,3,1,1.367,5.918,0,1]]}]})};}requests.push(q);if(_url.includes('time-trend'))return {ok:true,json:async()=>({run_id:q.run_id,rows:[[0,768,.5,.5,198,2,22,10004/3,2/3,100000000,null]],count:1,observations:3,aggregation:'time_mean',time_bin_s:q.time_bin_s,min_s:0,max_s:1,done:true,distributions:{area:[{code:768,n:3,hist:[[1.5,1],[3.5,1],[10000.5,1]]}]}})};return {ok:true,json:async()=>({run_id:q.run_id,rows:q.cursor?rows.slice(2):rows.slice(0,2),count:q.cursor?1:2,next_cursor:q.cursor?null:2,done:!!q.cursor,raw:true,aggregation:'none',sampling:'none'})};}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/raw-trends.js'),'utf8'),context);
(async()=>{
 vm.runInContext('rawTrends.render()',context);await new Promise(setImmediate);
 assert.deepEqual(Array.from(captured.get('chargeTrend').list[0].data,v=>v[1]),[1,10000]);
 assert.deepEqual(Array.from(captured.get('qualityTrend').list[0].data,v=>v[1]),[1,0]);
 assert.equal(captured.get('rates').unit,'计数率 / Hz');
 assert.equal(captured.get('rates').list[0].data[0][1],3); // N/T = 3 Hz, not 1/100ms = 10 Hz.
 assert.equal(captured.get('rates').list[0].uncertainty[0][1],1.367);
 get('population').value='good';vm.runInContext('rawTrends.render()',context);await new Promise(setImmediate);
 assert.deepEqual(Array.from(captured.get('chargeTrend').list[0].data,v=>v[1]),[1]);
 assert.deepEqual(Array.from(captured.get('qualityTrend').list[0].data,v=>v[1]),[1,0]);
 get('population').value='all';vm.runInContext('rawTrends.render()',context);await new Promise(setImmediate);get('raw-next').onclick();await new Promise(setImmediate);
 assert.equal(requests[3].cursor,2);assert.deepEqual(Array.from(captured.get('chargeTrend').list[0].data,v=>v[1]),[3]);
 assert.equal(captured.get('chargeTrend').list[0].data[0][0],.3);assert.equal(get('raw-next').disabled,true);
 get('raw-prev').onclick();await new Promise(setImmediate);
 assert.equal(requests[4].cursor,0);assert.deepEqual(Array.from(captured.get('chargeTrend').list[0].data,v=>v[1]),[1,10000]);
 get('time-bin-enabled').checked=true;get('time-bin-seconds').value='100';get('time-bin-apply').onclick();await new Promise(setImmediate);
 assert.equal(requests.at(-1).time_bin_s,100);assert.equal(captured.get('chargeTrend').list[0].type,'line');
 assert.equal(captured.get('chargeTrend').list[0].data[0][1],10004/3);
 assert.equal(context.state.raw_value_distributions.area[0].n,3);
 assert.equal(captured.get('qualityTrend').list[0].data[0][1],2/3);
 const countBefore=requests.length;get('time-bin-seconds').value='50';vm.runInContext('rawTrends.render()',context);await new Promise(setImmediate);assert.equal(requests.length,countBefore);
 get('time-bin-apply').onclick();await new Promise(setImmediate);assert.equal(requests.at(-1).time_bin_s,50);assert.equal(captured.get('chargeTrend').list[0].type,'line');
 // A slow rate request survives new data; the old graph remains while it completes.
 context.state.input_mode='local';get('trend-range').value='recent';clock+=1000;
 holdRate=true;vm.runInContext('rawTrends.render()',context);await new Promise(setImmediate);
 const rateCount=rateRequests.length,oldRate=captured.get('rates'),rawCount=requests.length;
 context.state.seq++;context.state.relative_s++;clock+=3000;
 timers.at(-1).fn();vm.runInContext('rawTrends.render()',context);await new Promise(setImmediate);
 assert.equal(rateRequests.length,rateCount);assert.equal(rateSignal.aborted,false);
 assert.equal(captured.get('rates'),oldRate);assert.equal(requests.length,rawCount);
 holdRate=false;releaseRate();await new Promise(setImmediate);
 clock+=1000;timers.at(-1).fn();await new Promise(setImmediate);
 assert.equal(rateRequests.length,rateCount+1);assert.equal(rateRequests.at(-1).live,true);
 assert.equal(timers.at(-1).ms,1000);
 assert.equal(requests.at(-1).include_distributions,false);
 // Full raw-value distributions are requested only after opening a trend detail.
 context.dashboard.isExpanded=id=>id==='chargeTrend';vm.runInContext('rawTrends.render()',context);await new Promise(setImmediate);
 assert.equal(requests.at(-1).include_distributions,true);
 console.log('Raw timestamps, unaveraged spikes, 0/1 flags, event intervals, explicit filtering and lossless page navigation passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
