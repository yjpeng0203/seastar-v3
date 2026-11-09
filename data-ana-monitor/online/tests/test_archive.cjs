"use strict";
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map();
function node(id){
 if(!nodes.has(id)){
  const e={value:'',checked:false,disabled:false,hidden:false,textContent:'',listeners:{},inputs:[],dataset:{},
   addEventListener(t,f){this.listeners[t]=f;},querySelectorAll(){return this.inputs;},classList:{toggle(){}}};
  Object.defineProperty(e,'innerHTML',{get(){return this.html||'';},set(html){this.html=html;if(id==='archive-runs')this.inputs=[...html.matchAll(/data-run="([^"]*)"([^>]*)/g)].map(m=>({dataset:{run:m[1]},checked:m[2].includes('checked'),listeners:{},addEventListener(t,f){this.listeners[t]=f;}}));}});
  nodes.set(id,e);
 }
 return nodes.get(id);
}
function run(board,id,stamp){return {key:`data_fee_${board}_run${id}_${stamp}`,board_id:board,string_group:'G'+board,run_id:id,start_stamp:stamp,volumes:2,sealed_volumes:2,bytes:8192,missing_indices:[]};}
let value={status:'idle',enabled:true,available:true,settings:{output_dir:'/organized',csv_dir:'/mirror',session_name:'',decoder_bin:'../../build/decoder',decoder_config:'../../cfg/config.json'},raw_data_dir:'/raw',paths:{},progress:{},runs:[run(3,'010189','20261106011338'),run(4,'010189','20261106011339'),run(5,'010189','20261106011339'),run(7,'010100','20261103000000')]};
const requests=[];
const context=vm.createContext({console,Map,Set,Object,String,Number,JSON,setInterval(){},document:{getElementById:node,querySelectorAll:()=>[]},fetch:async(_url,options)=>{
 assert.equal(_url,'api/archive');
 if(options.method==='POST'){
  const q=JSON.parse(options.body);requests.push(q);value={...value,settings:q.settings};
  if(q.action==='start')value={...value,status:'running',elapsed_s:125,progress:{stage:'decode',message:'正在解码 G3'}};
 }
 return {ok:true,json:async()=>JSON.parse(JSON.stringify(value))};
}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/archive.js'),'utf8'),context);
const turn=()=>new Promise(setImmediate);
(async()=>{
 await turn();
 assert.equal(node('archive-runs').inputs.filter(i=>i.checked).length,3);
 assert(!node('archive-runs').inputs.find(i=>i.dataset.run.includes('fee_7')).checked);
 assert.equal(node('archive-start').disabled,true);
 assert.equal(node('archive-decoder-config').value,'../../cfg/config.json');
 node('archive-output').value='/chosen-output';node('archive-output').listeners.input();
 await vm.runInContext('archivePanel.poll()',context);assert.equal(node('archive-output').value,'/chosen-output');
 await vm.runInContext('archivePanel.action("start")',context);assert.equal(requests.length,0);assert.match(node('archive-error').textContent,/确认/);
 node('archive-confirm').checked=true;node('archive-confirm').listeners.change();assert.equal(node('archive-start').disabled,false);
 await vm.runInContext('archivePanel.action("check")',context);assert.equal(requests[0].settings.output_dir,'/chosen-output');
 await vm.runInContext('archivePanel.action("start")',context);
 const q=requests.at(-1);assert.equal(q.confirmed_stopped,true);assert.deepEqual(q.boards,[3,4,5]);assert.equal(q.runs.length,3);
 assert.equal(node('archive-start').disabled,true);assert.equal(node('archive-check').disabled,true);assert.equal(node('archive-confirm').checked,false);
 assert.equal(node('archive-progress').textContent,'正在解码 G3');assert.match(node('archive-status').textContent,/00:02:05/);
 value={...value,status:'complete',progress:{stage:'complete',message:'归档完成'},result:{session_dir:'/organized/20261106_1106',verification:{ok:true,warnings:['<script>warning</script>']},runs:[{string_group:'G3',run_str:'010189',volume_count:2,trigger_signal_files:[{relative_path:'G3/raw_data/trigger_signal_fee_3.dat'}],waves:4036,rec_root:{relative_path:'G3/rec_data/test.root'},csv:{relative_path:null,note:'未匹配 CSV <details>'}},{string_group:'G4',run_str:'010189',volume_count:3,waves:42,rec_root:{relative_path:'G4/rec_data/test.root'},csv:{relative_path:'G4/csv/test.csv',rows:10}}]}};
 await vm.runInContext('archivePanel.poll()',context);
 assert.equal(node('archive-result').hidden,false);assert.match(node('archive-result-path').textContent,/20261106_1106/);
 assert.equal(node('archive-warnings').hidden,false);assert.match(node('archive-warnings').innerHTML,/&lt;script&gt;warning/);assert.match(node('archive-warnings').innerHTML,/CSV &lt;details&gt;/);
 assert.match(node('archive-results').innerHTML,/未匹配/);assert.match(node('archive-results').innerHTML,/2 卷 \+ 1 份触发记录/);assert.match(node('archive-results').innerHTML,/G4 \/ 010189<\/td><td>3<\/td>/);assert.equal(node('archive-start').disabled,true);
 value={...value,status:'error',error:'最终 ROOT 校验工具未执行成功',result:{...value.result,status:'generated_unverified',verification:{ok:false,warnings:['文件已生成，最终校验未通过']}}};
 await vm.runInContext('archivePanel.poll()',context);
 assert.match(node('archive-result-title').textContent,/已生成文件.*最终校验未通过/);
 assert.equal(node('archive-result').hidden,false);assert.match(node('archive-error').textContent,/校验工具/);
 node('archive-confirm').checked=true;node('archive-confirm').listeners.change();assert.equal(node('archive-start').disabled,false);
 value.runs[0].bytes+=1024;
 await vm.runInContext('archivePanel.poll()',context);assert.equal(node('archive-confirm').checked,false);
 // Explicit run selection survives polling and newest-run discovery.
 const old=node('archive-runs').inputs.find(i=>i.dataset.run.includes('fee_7'));old.checked=true;old.listeners.change();
 value.runs.push(run(3,'010190','20261106123000'));
 await vm.runInContext('archivePanel.poll()',context);assert(node('archive-runs').inputs.find(i=>i.dataset.run.includes('fee_7')).checked);
 assert(!node('archive-runs').inputs.find(i=>i.dataset.run.includes('010190')).checked);
 node('archive-latest').listeners.click();assert.equal(node('archive-runs').inputs.filter(i=>i.checked).length,1);
 value={...value,status:'disabled',enabled:false};await vm.runInContext('archivePanel.poll()',context);
 assert.equal(node('archive-start').disabled,true);assert.equal(node('archive-check').disabled,true);
 console.log('Archive UI: latest-run selection, explicit stop confirmation, dirty settings, full result paths and escaped CSV warnings passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
