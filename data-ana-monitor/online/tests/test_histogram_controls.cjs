"use strict";
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.join(__dirname, '../static'), elements = new Map(), bodies = [];
function element(id) {
  if (!elements.has(id)) elements.set(id, {value:'', checked:false, textContent:'', listeners:{},
    append(){}, before(){}, setAttribute(){}, addEventListener(type, callback){this.listeners[type]=callback;}});
  return elements.get(id);
}
element('population').value='all';
const fixture={min:0,max:4000,bins:4000,bin_width:1,max_seq:6,
  series:[{key:'5:0',n:6,mean:1,sigma:1,hist:[[.5,2],[1.5,3],[3999.5,1]]}]};
const context=vm.createContext({console,Number,Map,Set,AbortController,
  localStorage:{getItem(){return null;},setItem(){}}, performance:{now:()=>10000},
  document:{getElementById:element,createElement:()=>{const node=element(Symbol());Object.defineProperty(node,"id",{set(id){elements.set(id,node);}});return node;}},
  state:{run_id:'a'.repeat(32),seq:6,waves:6},selectedChannels:()=>[{key:'5:0'}],
  charts:new Map(),renderCharts(){},fmt:(x)=>String(x),
  fetch:async (_url,request)=>{bodies.push(JSON.parse(request.body));return {ok:true,json:async()=>fixture};}});
const app=fs.readFileSync(path.join(root,'app.js'),'utf8');
vm.runInContext(app.slice(app.indexOf('function denseHist('),app.indexOf('function histogramSummary')),context);
vm.runInContext(fs.readFileSync(path.join(root,'histogram-controls.js'),'utf8'),context);
(async()=>{
  vm.runInContext('histogramControls.prepare("chargeHist","area",[],1,"ADC·sample")',context);
  await new Promise(setImmediate);
  const raw=vm.runInContext('histogramControls.prepare("chargeHist","area",[],1,"ADC·sample")',context);
  const physical=vm.runInContext('histogramControls.prepare("chargeHist","area",[],.02,"pC")',context);
  assert.equal(bodies.length,1);assert.equal(bodies[0].binning,'native');
  assert.equal(raw.spec.bins,4000);assert.equal(physical.spec.bins,4000);
  assert.equal(element('chargeHist-max').value,'80');
  for(const normalized of [false,true]) {
    element('normalized').checked=normalized;
    const a=vm.runInContext('histogramControls.series('+JSON.stringify(fixture)+',1,k=>k)[0].data',context);
    const b=vm.runInContext('histogramControls.series('+JSON.stringify(fixture)+',.02,k=>k)[0].data',context);
    assert.deepEqual(Array.from(a,v=>v[1]),Array.from(b,v=>v[1]));
    a.forEach((v,i)=>assert.equal(b[i][0],v[0]*.02));
    assert(Math.abs(a.reduce((sum,v)=>sum+v[1],0)-(normalized?1:6))<1e-12);
    assert(a.length<15); // Empty bins are retained by step neighbours, never merged.
  }
  element('logy').checked=true;
  const log=vm.runInContext('histogramControls.series('+JSON.stringify(fixture)+',.02,k=>k)[0].data',context);
  assert.equal(log.length,8);assert.equal(log[5][0],79.98);assert.equal(log[6][0],80);
  assert.equal(log[5][1],log[6][1]);assert.equal(log[7][1],null);
  assert.equal(log[1][0],log[2][0]);assert.notEqual(log[1][1],log[2][1]);
  element('chargeHist-min').value='0';element('chargeHist-max').value='80';element('chargeHist-bins').value='4000';
  const createdForm=[...elements.values()].find(e=>e.listeners.submit);
  createdForm.listeners.submit({preventDefault(){}});
  const manual=vm.runInContext('histogramControls.prepare("chargeHist","area",[],.02,"pC")',context);
  assert.equal(manual.spec.max,4000);assert.equal(manual.spec.bins,4000);
  await new Promise(setImmediate);
  assert.equal(bodies[1].max,4000);assert.equal(bodies[1].bins,4000);
  console.log('Native bins, unit invariance, sparse gaps, normalization and manual override checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
