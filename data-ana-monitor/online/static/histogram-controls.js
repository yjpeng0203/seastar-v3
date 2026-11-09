"use strict";
// Preferences are plot-only. Confirmed bounds are stored in the metric's original units.
const histogramControls = (() => {
  const storageKey = "seastar.histograms.v2";
  let preferences = {};
  try { preferences = JSON.parse(localStorage.getItem(storageKey) || "{}"); } catch (_) {}
  if (!preferences || typeof preferences !== "object" || Array.isArray(preferences)) preferences = {};
  const records = new Map();
  const get = id => document.getElementById(id);
  function valid(s) {
    return s && Number.isFinite(s.min) && Number.isFinite(s.max) && s.min < s.max &&
      Number.isInteger(s.bins) && s.bins >= 1 && s.bins <= 4096;
  }
  function persist() {
    try { localStorage.setItem(storageKey, JSON.stringify(preferences)); } catch (_) {}
  }
  function invalidate(r) {
    if (r.controller) r.controller.abort();
    r.generation++; r.busy = false; r.result = null; r.error = null; r.key = null;
    r.attemptedWaves = -1; r.lastAttempt = -Infinity;
  }
  function create(id) {
    const r = {id, generation: 0, busy: false, dirty: false, result: null,
      error: null, key: null, lastAttempt: -Infinity, attemptedWaves: -1};
    const form = document.createElement("form");
    form.className = "histogram-settings";
    form.setAttribute("aria-label", "分布图范围与分箱设置");
    r.form = form;
    for (const [field, title] of [["min", "横轴下限"], ["max", "横轴上限"], ["bins", "bin 数"]]) {
      const label = document.createElement("label"), caption = document.createElement("span");
      caption.textContent = title; label.append(caption);
      const input = document.createElement("input");
      input.id = id + "-" + field; input.name = field; input.type = "number"; input.required = true;
      input.step = field === "bins" ? "1" : "any";
      if (field === "bins") { input.min = "1"; }
      label.htmlFor = input.id; label.append(input); form.append(label);
      r[field] = input; r[field + "Caption"] = caption;
      input.addEventListener("input", () => {
        r.dirty = true;
        r.message.textContent = "输入尚未确认，图形保持当前已生效设置。";
      });
    }
    r.confirm = document.createElement("button"); r.confirm.type = "submit";
    r.confirm.className = "primary"; r.confirm.textContent = "确认"; form.append(r.confirm);
    const reset = document.createElement("button"); reset.type = "button";
    reset.textContent = "恢复自动"; form.append(reset);
    r.message = document.createElement("span"); r.message.className = "histogram-message";
    r.message.setAttribute("role", "status"); form.append(r.message);
    get(id).before(form);
    reset.addEventListener("click", () => {
      delete preferences[id]; persist(); invalidate(r); r.dirty = false; r.factor = null;
      charts.get(id)?.dispatchAction({type: "dataZoom", start: 0, end: 100}); renderCharts(true);
    });
    form.addEventListener("submit", event => {
      event.preventDefault();
      const lo = Number(r.min.value), hi = Number(r.max.value), bins = Number(r.bins.value);
      const spec = {min: lo / r.factor, max: hi / r.factor, bins, metric: r.metric};
      if (!r.min.value.trim() || !r.max.value.trim() || !valid(spec)) {
        r.message.textContent = "请填写有效范围（下限 < 上限），bin 数为 1–4096 的整数。";
        return;
      }
      preferences[id] = spec; persist(); invalidate(r); r.dirty = false;
      charts.get(id)?.dispatchAction({type: "dataZoom", start: 0, end: 100}); renderCharts(true);
    });
    records.set(id, r); return r;
  }
  function context(r) {
    return {run_id: state.run_id, metric: r.metric, population: r.population,
      channels: r.metric === "trigger_interval_log10_s" ? [] : selectedChannels().map(c => c.key).sort(), ...r.query,preview_points:typeof dashboard!=="undefined"&&!dashboard.isExpanded(r.id)?256:undefined};
  }
  function request(r, key) {
    const generation = ++r.generation;
    const controller = new AbortController(); r.controller = controller;
    const body = {...context(r), max_seq: state.seq || 0};
    r.busy = true; r.error = null; r.lastAttempt = performance.now();
    r.attemptedWaves = state.waves || 0;
    fetch("api/histogram", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body), signal: controller.signal})
      .then(async response => {
        const value = await response.json();
        if (!response.ok) throw Error(value.error || "分布统计失败");
        if (r.generation === generation && r.key === key) r.result = value;
      })
      .catch(error => {
        if (r.generation === generation && error.name !== "AbortError") r.error = error.message;
      })
      .finally(() => {
        if (r.generation !== generation) return;
        r.busy = false;
        if (state && r.key === key) renderCharts(true);
      });
  }
  function prepare(id, metric, automatic, factor, axisUnit) {
    const r = records.get(id) || create(id);
    r.metric = metric; r.factor = factor; r.axisUnit = axisUnit;
    r.population = metric === "pair_dt_ns" ? "good" :
      metric.includes("interval_log10_s") ? "all" : get("population").value;
    let spec = preferences[id];
    if (!valid(spec) || spec.metric !== metric) { delete preferences[id]; spec = null; }
    const native = !metric.includes("interval_log10_s") && metric !== "pair_dt_ns";
    const lows = automatic.map(v => v.d?.min).filter(Number.isFinite);
    const highs = automatic.map(v => v.d?.max).filter(Number.isFinite);
    let lo = lows.length ? Math.min(...lows) : 0, hi = highs.length ? Math.max(...highs) : 1;
    if (native) { lo = Math.floor(lo); hi = Math.floor(hi) + 1; }
    else if (lo === hi) { lo -= .5; hi += .5; }
    const exact = !!spec || native;
    r.query = spec || (native ? {binning: "native"} : {});
    if (exact && state?.run_id) {
      const key = JSON.stringify(context(r));
      if (key !== r.key) { invalidate(r); r.key = key; }
      if (!r.busy && r.attemptedWaves !== (state.waves || 0) && performance.now() - r.lastAttempt >= 30000)
        request(r, key);
    }
    const bounds = spec || (r.result ? r.result : {min: lo, max: hi, bins: native ? hi - lo : 200});
    const unitChanged = r.lastFactor !== factor;
    if (!r.dirty || unitChanged) {
      r.min.value = String(bounds.min * factor);
      r.max.value = String(bounds.max * factor);
      r.bins.value = String(bounds.bins);
      if (unitChanged) r.dirty = false;
    }
    r.lastFactor = factor;
    r.minCaption.textContent = "横轴下限 / " + axisUnit;
    r.maxCaption.textContent = "横轴上限 / " + axisUnit;
    let previewNote=get(id+"-preview-note");
    if(!previewNote){previewNote=document.createElement("p");previewNote.id=id+"-preview-note";previewNote.className="preview-note";get(id).after(previewNote);}
    previewNote.textContent=r.error?"统计失败："+r.error:r.busy&&!r.result?"正在读取统计缓存…":
      (spec?"自定义分箱":"原始细分箱")+" · Δ="+fmt((bounds.max-bounds.min)*factor/bounds.bins,6)+" "+axisUnit+" · 点击放大";
    r.confirm.disabled = !state?.run_id || r.busy;
    r.confirm.textContent = r.busy ? "统计中…" : "确认";
    if (!r.dirty) r.message.textContent = r.error ? "分布统计失败：" + r.error : r.busy ?
      "正在从已保存的逐波形指标精确统计…" : exact ?
        (spec ? "自定义" : "默认原始单位分箱") + " · " + bounds.bins + " bins · Δbin=" +
        fmt((bounds.max - bounds.min) * factor / bounds.bins, 6) + " " + axisUnit + (r.result?.preview ? " · 全范围显示预览，未合并计数" : "") +
        " · 有新增指标时约每 5 秒更新" : "保留原始 log/符合分箱；可手动指定范围和 bin 数。";
    return {custom: exact, spec: bounds, result: r.result, busy: r.busy, error: r.error};
  }
  function series(result, factor, nameForKey) {
    return result.series.map(item => {
      const d = {n: item.n, mean: item.mean, sigma: item.sigma, bin_width: result.bin_width,
        bin_origin: result.min, hist: item.hist || item.counts.map((count, i) =>
          [result.min + (i + .5) * result.bin_width, count])};
      const ordinate = count => get("normalized").checked && item.n ? count / item.n : count;
      // Adjacent positive bins share a vertical step; gaps break the log curve.
      // Disconnecting every bin makes subpixel-width peaks disappear at full range.
      let data;
      if (get("logy").checked) {
        data=[]; let previous=null;
        for (const [x,count] of d.hist) {
          if (!(count>0)) continue;
          const left=(x-d.bin_width/2)*factor,right=(x+d.bin_width/2)*factor;
          if(previous!==null && Math.abs(left-previous)>Math.abs(d.bin_width*factor)*.01) data.push([left,null]);
          data.push([left,ordinate(count)],[right,ordinate(count)]);previous=right;
        }
        if(previous!==null)data.push([previous,null]);
      } else data=(result.preview ? d.hist.map(([x,n])=>[x*factor,n]) : denseHist(d, factor)).map(([x,count])=>[x,ordinate(count)]);
      return {name: nameForKey(item.key), d, summary: item, data};
    });
  }
  return {prepare, series};
})();
