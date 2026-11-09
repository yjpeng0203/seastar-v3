"use strict";
const archivePanel = (() => {
  const el = id => document.getElementById(id);
  const fieldIds = {output_dir:"archive-output",csv_dir:"archive-csv",session_name:"archive-name",decoder_bin:"archive-decoder-bin",decoder_config:"archive-decoder-config"};
  const labels = {idle:"等待归档",checking:"正在检查环境",running:"正在归档",complete:"归档完成",error:"需要检查",disabled:"当前模式不支持归档"};
  const escape = value => String(value ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const count = value => Number(value || 0).toLocaleString("zh-CN");
  const size = value => value >= 1073741824 ? (value/1073741824).toFixed(2)+" GiB" : (value/1048576).toFixed(2)+" MiB";
  let current = null, selected = new Set(), selectionDirty = false, settingsDirty = false;
  let requesting = false, polling = false, revision = 0, actionError = "", networkError = "", runsSignature = "", selectionSignature = "";

  function selectLatest(runs) {
    const newest = [...runs].sort((a,b)=>String(b.start_stamp || b.key.match(/_(\d{14})$/)?.[1] || "").localeCompare(String(a.start_stamp || a.key.match(/_(\d{14})$/)?.[1] || "")))[0];
    return new Set(newest ? runs.filter(r=>String(r.run_id)===String(newest.run_id)).map(r=>r.key) : []);
  }
  function busy() { return requesting || ["running","checking"].includes(current?.status); }
  function updateButtons() {
    const disabled = busy() || current?.status === "disabled" || !current;
    el("archive-check").disabled = disabled; el("archive-latest").disabled = disabled;
    el("archive-start").disabled = disabled || !selected.size || !el("archive-confirm").checked;
    el("archive-confirm").disabled = disabled;
    Object.values(fieldIds).forEach(id=>{el(id).disabled=disabled;});
    el("archive-runs").querySelectorAll("input[data-run]").forEach(input=>{input.disabled=disabled;});
    const error = actionError || networkError || current?.error || "";
    el("archive-error").textContent = error; el("archive-error").hidden = !error;
  }
  function render(value) {
    current = value;
    if (!settingsDirty) for (const [key,id] of Object.entries(fieldIds)) if (typeof value.settings?.[key] === "string") el(id).value=value.settings[key];
    const runs = value.runs || [];
    const available = new Set(runs.map(r=>r.key));
    const before = [...selected].sort().join("\n");
    selected = selectionDirty ? new Set([...selected].filter(key=>available.has(key))) : selectLatest(runs);
    const selectedSignature = JSON.stringify(runs.filter(r=>selected.has(r.key)).map(r=>[r.key,r.volumes,r.bytes,r.sealed_volumes,r.missing_indices]));
    if (before !== [...selected].sort().join("\n") || (selectionSignature && selectionSignature !== selectedSignature)) el("archive-confirm").checked=false;
    selectionSignature = selectedSignature;
    const signature = JSON.stringify(runs);
    if (signature !== runsSignature) {
      runsSignature = signature;
      el("archive-runs").innerHTML = runs.length ? runs.map(r=>
        '<tr><td><label><input type="checkbox" data-run="'+escape(r.key)+'"'+(selected.has(r.key)?' checked':'')+'>'+escape(r.string_group || "FEE "+r.board_id)+'</label></td><td>'+escape(r.run_id)+'</td><td>'+count(r.sealed_volumes)+' / '+count(r.volumes)+'</td><td>'+size(r.bytes || 0)+'</td><td>'+escape(r.missing_indices?.length ? "缺卷："+r.missing_indices.map(n=>"idx"+String(n).padStart(3,"0")).join("、") : r.sealed_volumes<r.volumes ? "等待末卷静默" : "分卷齐全")+'</td></tr>').join("") : '<tr><td colspan="5">当前目录尚未发现可归档的 DAT Run。</td></tr>';
      el("archive-runs").querySelectorAll("input[data-run]").forEach(input=>input.addEventListener("change",()=>{
        input.checked?selected.add(input.dataset.run):selected.delete(input.dataset.run);
        selectionDirty=true;el("archive-confirm").checked=false;renderSelection();updateButtons();
      }));
    }
    el("archive-runs").querySelectorAll("input[data-run]").forEach(input=>{input.checked=selected.has(input.dataset.run);});
    renderSelection();
    const elapsed = Number(value.elapsed_s);
    const clock = ["running","complete"].includes(value.status) && Number.isFinite(elapsed) ? " · 用时 "+[Math.floor(elapsed/3600),Math.floor(elapsed/60)%60,Math.floor(elapsed)%60].map(n=>String(n).padStart(2,"0")).join(":") : "";
    el("archive-status").textContent = (labels[value.status] || value.status || "等待归档")+clock;
    el("archive-source").textContent = "原始 DAT 目录："+(value.raw_data_dir || "—");
    const paths = value.paths || {};
    el("archive-paths").textContent = paths.decoder_bin || paths.decoder_config ? "实际程序："+(paths.decoder_bin || "—")+"；实际配置："+(paths.decoder_config || "—") : "相对路径以原始 DAT 目录为基准。";
    el("archive-notice").textContent = value.notice || "";el("archive-notice").hidden = !value.notice;
    el("archive-progress").textContent = value.progress?.message || (value.status === "disabled" ? "请在本机 DAT 目录监控模式使用归档功能。" : "等待开始归档");
    const stage = value.progress?.stage === "manifest" ? "verify" : value.progress?.stage;
    document.querySelectorAll("[data-archive-stage]").forEach(node=>node.classList.toggle("active",node.dataset.archiveStage === stage));
    const result = value.result;
    el("archive-result").hidden = !result;
    if (result) {
      el("archive-result-title").textContent = value.status === "complete" ? "本次归档结果" : result.verification?.ok === false ? "已生成文件 · 最终校验未通过" : "上次归档结果";
      el("archive-result-path").textContent = result.session_dir ? "批次保存位置："+result.session_dir : result.message || "所选 Run 已归档。";
      const notes = [...(result.verification?.warnings || [])];
      for (const run of result.runs || []) if (!run.csv?.relative_path) notes.push((run.string_group || "FEE "+run.board_id)+"："+(run.csv?.note || "未找到匹配的慢控制 CSV"));
      const warnings = [...new Set(notes.map(String))];
      el("archive-warnings").innerHTML = warnings.map(note=>"<li>"+escape(note)+"</li>").join("");el("archive-warnings").hidden = !warnings.length;
      el("archive-results").innerHTML = (result.runs || []).map(run=>"<tr><td>"+escape(run.string_group || "FEE "+run.board_id)+" / "+escape(run.run_str || run.run_key)+"</td><td>"+count(run.volume_count)+(run.trigger_signal_files?.length ? " 卷 + "+count(run.trigger_signal_files.length)+" 份触发记录" : "")+"</td><td>"+count(run.waves)+"</td><td>"+escape(run.rec_root?.relative_path || "—")+"</td><td>"+(run.csv?.relative_path ? escape(run.csv.relative_path)+" · "+count(run.csv.rows)+" 行" : "未匹配，见上方提示")+"</td></tr>").join("");
    }
    updateButtons();
  }
  function renderSelection() {
    const list=(current?.runs || []).filter(r=>selected.has(r.key));
    el("archive-selection").textContent = "已选择 "+list.length+" 个 Run · "+count(list.reduce((n,r)=>n+Number(r.volumes || 0),0))+" 卷 · "+size(list.reduce((n,r)=>n+Number(r.bytes || 0),0))+"。默认选择最新一轮，可逐项调整。";
  }
  async function poll() {
    if (polling || requesting) return;
    polling=true;const version=revision;
    try {
      const response=await fetch("api/archive",{cache:"no-store"});const value=await response.json();
      if (!response.ok) throw Error(value.error || "HTTP "+response.status);
      if(version===revision){networkError="";render(value);}
    } catch(error){networkError="无法更新归档状态："+error.message;updateButtons();}
    finally{polling=false;}
  }
  async function action(name) {
    if (busy() || current?.status === "disabled") return;
    if (name === "start" && (!selected.size || !el("archive-confirm").checked)) {actionError="请选择本轮 Run，并确认所选数据已停止写入。";updateButtons();return;}
    const settings=Object.fromEntries(Object.entries(fieldIds).map(([key,id])=>[key,el(id).value.trim()]));
    const runs=[...selected],boards=[...new Set((current?.runs || []).filter(r=>selected.has(r.key)).map(r=>r.board_id))];
    requesting=true;revision++;actionError="";networkError="";updateButtons();
    try {
      const response=await fetch("api/archive",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:name,settings,runs,boards,confirmed_stopped:el("archive-confirm").checked})});
      const value=await response.json();if(!response.ok)throw Error(value.error || "归档操作失败");
      settingsDirty=false;if(name === "start" && value.status === "running")el("archive-confirm").checked=false;render(value);
    } catch(error){actionError=error.message;}
    finally{requesting=false;updateButtons();}
  }
  Object.values(fieldIds).forEach(id=>el(id).addEventListener("input",()=>{settingsDirty=true;}));
  el("archive-confirm").addEventListener("change",updateButtons);
  el("archive-form").addEventListener("submit",event=>{event.preventDefault();action("start");});
  el("archive-check").addEventListener("click",()=>action("check"));
  el("archive-latest").addEventListener("click",()=>{selected=selectLatest(current?.runs || []);selectionDirty=false;el("archive-confirm").checked=false;render(current);});
  poll();setInterval(poll,2000);
  return {poll,action,render};
})();
