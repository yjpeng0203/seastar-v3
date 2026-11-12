"use strict";
const queryClient=(()=>{
 const active=new Map();let sequence=0;
 const client=typeof crypto!=="undefined"&&crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+Math.random().toString(36).slice(2);
 function cancel(owner){
  const item=active.get(owner);if(!item)return;
  active.delete(owner);item.controller.abort();
  fetch("api/query-cancel",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query_id:item.id}),keepalive:true}).catch(()=>{});
 }
 function start(owner,controller){cancel(owner);const id=client+"-"+(++sequence);active.set(owner,{id,controller});return id;}
 function finish(id){for(const [owner,item] of active)if(item.id===id){active.delete(owner);break;}}
 function cancelAll(){for(const owner of [...active.keys()])cancel(owner);}
 if(typeof window!=="undefined")window.addEventListener("pagehide",cancelAll);
 return {start,finish,cancel,cancelAll};
})();
