"use strict";
const $ = function (id) { return document.getElementById(id); };
const formatDate = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
});
function date(t) { return Number.isFinite(t) ? formatDate.format(t).replace(/\//g, "-") : "—"; }
function numeric(v, digits) { return Number.isFinite(v) ? v.toLocaleString("zh-CN", {maximumFractionDigits: digits === undefined ? 2 : digits}) : "—"; }
function byteSize(value) {
  const bytes = value || 0;
  return bytes >= 1048576 ? numeric(bytes / 1048576) + " MiB" :
    bytes >= 1024 ? numeric(bytes / 1024) + " KiB" : numeric(bytes, 0) + " B";
}
function status(text, active) { $("status").textContent = text; $("dot").classList.toggle("running", !!active); }
function message(text) { $("message").textContent = text || ""; }

const palette = ["#167d8d", "#d18a29", "#8b62b2", "#3b9c78", "#d16c75", "#5985c2", "#9b9d36", "#845c48"];
const definitions = [
  {id: "hit", fields: Array.from({length: 8}, function (_, i) { return ["hit_rate_ch" + i, "ch" + i]; })},
  {id: "baseline", fields: Array.from({length: 8}, function (_, i) { return ["baseline_ch" + i, "ch" + i]; })},
  {id: "temperature-chart", fields: [["fpga_temperature_c", "FPGA"]]},
  {id: "reset-chart", step: true, fields: [["optical_module_reset_count", "光模块"], ["clock_chip_reset_count", "时钟芯片"]]},
  {id: "latency-chart", fields: [["host_latency_us", "收发耗时"]]},
  {id: "offset-chart", fields: [["host_age_s", "采集 − 主机发送"], ["tod_offset_s", "采集 − TOD"]]}
];
const keys = definitions.flatMap(function (d) { return d.fields.map(function (f) { return f[0]; }); });
const charts = new Map();
let expanded = null, panelAnchor = null, toolsAnchor = null;
let worker, workerId = 0, pending = new Map();
let source = null, epoch = 0, timer = null, running = null, controller = null;
let paused = false, view = {mode: "all"}, current = null, viewId = 0, zoomTimer;
let statisticsBusy = false, statisticsReady = false, statisticsRevision = 0;
let syncEnabled = null, syncTimer = null;
let selectedStream = "", streamOptionsKey = "", mirroredFilesKey = "", allFiles = [];

function chartLayout(chart, detail) {
  chart.setOption({
    grid: [
      {left: detail ? 66 : 56, right: "27%", top: detail ? 66 : 16, bottom: detail ? 71 : 44},
      {left: "78%", right: detail ? 43 : 18, top: detail ? 66 : 16, bottom: detail ? 71 : 44}
    ],
    legend: {show: detail, type: "scroll"}, tooltip: {show: detail},
    title: [{}, {show: detail}],
    dataZoom: [
      {id: "x-inside", disabled: !detail}, {id: "x-slider", show: detail},
      {id: "count-inside", disabled: !detail}, {id: "count-slider", show: detail},
      {id: "y-slider", show: detail}
    ],
    xAxis: [{splitNumber: detail ? 6 : 3}, {nameGap: detail ? 28 : 24}],
    yAxis: [{splitNumber: detail ? 5 : 3, axisLabel: {formatter: function (v) {
      return Math.abs(v) >= 1e6 ? v.toExponential(1).replace("e+", "e") : numeric(v, 2);
    }}}, {splitNumber: detail ? 5 : 3}],
    series: chart.getOption().series.map(function (s) { return {id: s.id, silent: !detail}; })
  });
}

function restorePlot() {
  if (!expanded) return;
  const id = expanded.querySelector(".plot").id;
  panelAnchor.replaceWith(expanded); toolsAnchor.replaceWith($("range-tools"));
  expanded = panelAnchor = toolsAnchor = null;
  const chart = charts.get(id);
  if (chart) { chartLayout(chart, false); chart.resize(); }
}

function openPlot(id) {
  if (expanded || !charts.has(id)) return;
  expanded = $(id).closest(".panel");
  panelAnchor = document.createElement("div"); panelAnchor.className = "detail-placeholder";
  panelAnchor.textContent = expanded.querySelector("h2").textContent + " · 详情已打开";
  expanded.replaceWith(panelAnchor); $("detail-content").append(expanded);
  const tools = $("range-tools"); toolsAnchor = document.createElement("span"); tools.replaceWith(toolsAnchor);
  $("detail-tools").append(tools);
  $("detail-title").textContent = expanded.querySelector("h2").textContent;
  $("plot-dialog").showModal();
  const chart = charts.get(id); chart.resize(); chartLayout(chart, true);
}

document.querySelectorAll(".panel").forEach(function (panel) {
  const plot = panel.querySelector(".plot"), button = document.createElement("button");
  button.type = "button"; button.className = "expand-plot"; button.textContent = "放大详情";
  button.onclick = function () { openPlot(plot.id); }; panel.querySelector(".panel-head").append(button);
  plot.tabIndex = 0; plot.setAttribute("role", "button");
  plot.setAttribute("aria-label", panel.querySelector("h2").textContent + "，点击放大详情");
  plot.addEventListener("click", function () { if (!expanded) openPlot(plot.id); });
  plot.addEventListener("keydown", function (event) {
    if (!expanded && ["Enter", " "].includes(event.key)) { event.preventDefault(); openPlot(plot.id); }
  });
});
$("close-detail").onclick = function () { $("plot-dialog").close(); };
$("plot-dialog").addEventListener("close", function () { if (!$("plot-dialog").open) restorePlot(); });

function streamKey(name) {
  const groupMatch = /(?:^|\/)G(\d+)\//i.exec(name);
  const match = /(?:^|\/)readback_fee_(\d+)(?:_|\.csv$)/i.exec(name);
  if (match) return "fee_" + Number(match[1]);
  if (groupMatch) return "fee_" + Number(groupMatch[1]);
  return "other";
}

function streamLabel(id, groupHint) {
  if (id && id.startsWith("fee_")) {
    const n = Number(id.slice(4));
    const g = groupHint || (Number.isInteger(n) && n >= 0 && n <= 6 ? "G" + n : "");
    return g ? g + " · FEE " + n : "FEE " + n;
  }
  return groupHint || "其他 CSV";
}

function request(type, payload, transfer) {
  return new Promise(function (resolve, reject) {
    const id = ++workerId;
    pending.set(id, {resolve: resolve, reject: reject});
    worker.postMessage(Object.assign({type: type, id: id}, payload || {}), transfer || []);
  });
}

function histogramSeries(field, color) {
  return {
    id: "hist-" + field[0], name: field[1], type: "custom", data: [],
    xAxisIndex: 1, yAxisIndex: 1, encode: {x: 0, y: [1, 2]},
    dimensions: ["频数", "下界", "上界"], emphasis: {disabled: true},
    tooltip: {trigger: "item", formatter: function (p) {
      const last = p.value[2] === current?.distributions[definitions.findIndex(d => d.fields.some(f => f[0] === field[0]))]?.max;
      return p.seriesName + "<br>" + numeric(p.value[1], 6) + " ≤ 读数 " +
        (last ? "≤ " : "< ") + numeric(p.value[2], 6) + "<br>记录数　" + numeric(p.value[0], 0);
    }},
    renderItem: function (params, api) {
      // Keep a graphic for every bin so changing the bin count can remove it safely.
      if (!api.value(0)) return {type: "group", children: []};
      const bottom = api.coord([0, api.value(1)]);
      const top = api.coord([api.value(0), api.value(2)]);
      const shape = echarts.graphic.clipRectByRect({
        x: bottom[0], y: top[1], width: Math.max(0, top[0] - bottom[0]),
        height: Math.max(0, bottom[1] - top[1])
      }, params.coordSys);
      return shape ? {type: "rect", shape: shape,
        style: {fill: color, stroke: color, lineWidth: 0.5, opacity: 0.45}} : {type: "group", children: []};
    }
  };
}

function initializeChart(definition) {
  const element = $(definition.id);
  if (!element.clientWidth) return;
  if (charts.has(definition.id)) return;
  const chart = echarts.init(element, null, {renderer: "canvas", devicePixelRatio: Math.min(window.devicePixelRatio || 1, 2)});
  charts.set(definition.id, chart);
  chart.setOption({
    animation: false, color: palette, useUTC: true,
    textStyle: {fontFamily: "Arial, sans-serif", color: "#61747e"},
    grid: [
      {left: 66, right: "27%", top: 66, bottom: 71},
      {left: "78%", right: 43, top: 66, bottom: 71}
    ],
    legend: {top: 10, left: 15, right: "27%", itemWidth: 13, itemHeight: 6,
      data: definition.fields.map(function (f) { return {name: f[1], icon: "roundRect"}; }),
      textStyle: {fontSize: 11}},
    tooltip: {trigger: "axis", confine: true, axisPointer: {type: "line"},
      formatter: function (params) {
        params = params.filter(function (p) { return !p.seriesId.startsWith("hist-"); });
        if (!params.length) return "";
        let result = date(params[0].value[0]);
        params.forEach(function (p) { result += "<br>" + p.marker + p.seriesName + "　" + numeric(p.value[1], 3); });
        return result;
      }},
    xAxis: [
      {id: "time-axis", type: "time", gridIndex: 0, axisLine: {lineStyle: {color: "#c6d4d7"}}, splitLine: {show: false},
        axisLabel: {fontSize: 10, hideOverlap: true, formatter: function (value) { return date(value).slice(5).replace(" ", "\n"); }}},
      {id: "count-axis", type: "value", gridIndex: 1, min: 0, minInterval: 1, splitNumber: 2,
        name: "记录数", nameLocation: "middle", nameGap: 28, nameTextStyle: {fontSize: 11},
        splitLine: {show: false}, axisLine: {show: true, lineStyle: {color: "#c6d4d7"}},
        axisLabel: {fontSize: 10, hideOverlap: true, formatter: function (v) { return v >= 10000 ? numeric(v / 10000, 1) + "万" : numeric(v, 0); }}}
    ],
    yAxis: [
      {type: "value", gridIndex: 0, scale: true, splitLine: {lineStyle: {color: "#edf1f1"}},
        axisLabel: {fontSize: 10, formatter: function (v) { return numeric(v, 2); }}},
      {type: "value", gridIndex: 1, scale: true, axisLabel: {show: false},
        axisTick: {show: false}, axisLine: {show: true, lineStyle: {color: "#dce5e6"}},
        splitLine: {lineStyle: {color: "#edf1f1"}}}
    ],
    dataZoom: [
      {id: "x-inside", type: "inside", xAxisIndex: 0, filterMode: "none", throttle: 100},
      {id: "x-slider", type: "slider", xAxisIndex: 0, filterMode: "none", height: 19, bottom: 15, left: 66, right: "27%",
        borderColor: "#dce5e6", showDetail: false, brushSelect: false, dataBackground: {lineStyle: {color: "#7aa79f"}}},
      {id: "count-inside", type: "inside", xAxisIndex: 1, filterMode: "none", throttle: 100,
        start: 0, end: 100, rangeMode: ["percent", "percent"]},
      {id: "count-slider", type: "slider", xAxisIndex: 1, filterMode: "none", height: 14,
        bottom: 15, left: "78%", right: 43, showDataShadow: false,
        borderColor: "#dce5e6", showDetail: false, brushSelect: false,
        start: 0, end: 100, rangeMode: ["percent", "percent"]},
      {id: "y-slider", type: "slider", yAxisIndex: [0, 1], filterMode: "none", width: 13,
        right: 10, top: 66, bottom: 72, showDetail: false, brushSelect: false,
        start: 0, end: 100, rangeMode: ["percent", "percent"]}
    ],
    series: definition.fields.map(function (f, i) {
      return {id: f[0], name: f[1], type: "line", data: [], showSymbol: false,
              connectNulls: false, step: definition.step ? "end" : false,
              lineStyle: {width: 1.3, color: palette[i]}, itemStyle: {color: palette[i]}, emphasis: {disabled: true}};
    }).concat(definition.fields.map(function (f, i) { return histogramSeries(f, palette[i]); }))
  });
  chart.on("legendselectchanged", function () { requestView().catch(showError); });
  chart.on("datazoom", function (event) {
    invalidateStatistics(false);
    const events = event.batch || [event];
    const xEvent = events.find(function (item) { return item.dataZoomId === "x-inside" || item.dataZoomId === "x-slider"; });
    if (!xEvent || !current || current.first === current.last) return;
    const option = chart.getOption().dataZoom.find(function (d) { return d.id === "x-slider"; });
    const start = xEvent.start === undefined ? option.start : xEvent.start;
    const end = xEvent.end === undefined ? option.end : xEvent.end;
    view = {mode: "custom", lo: current.first + (current.last - current.first) * start / 100,
            hi: current.first + (current.last - current.first) * end / 100};
    updateRangeButtons();
    clearTimeout(zoomTimer);
    zoomTimer = setTimeout(function () { requestView().catch(showError); }, 100);
  });
  chartLayout(chart, expanded === element.closest(".panel"));
}

function updateRangeButtons() {
  document.querySelectorAll("[data-range]").forEach(function (button) {
    const range = button.dataset.range;
    button.classList.toggle("active", range === "all" ? view.mode === "all" :
      view.mode === "tail" && view.span === Number(range));
  });
}

function invalidateStatistics(clear) {
  statisticsRevision++;
  if (clear) {
    statisticsReady = false; $("statistics-notice").textContent = "";
    definitions.forEach(function (definition) {
      const container = $(definition.id + "-stats");
      container.hidden = true; container.innerHTML = "";
    });
  } else if (statisticsReady) {
    $("statistics-notice").textContent = "范围或数据已变化；下方是上次计算的快照，请重新计算。";
    definitions.forEach(function (definition) {
      if ($(definition.id + "-stats").hidden === false)
        $(definition.id + "-stats-state").textContent = "结果快照 · 范围或数据已变化，请重新计算";
    });
  }
}

async function calculateStatistics() {
  if (!current || !current.count || statisticsBusy) return;
  const thisEpoch = epoch, button = $("calculate-stats");
  statisticsBusy = true; button.disabled = true; button.textContent = "正在计算…";
  try {
    // Finish any pending time/legend update before capturing the displayed axes.
    await requestView();
    if (thisEpoch !== epoch || !current || !current.count) return;
    const snapshot = current, revision = statisticsRevision, bins = Number($("bins").value), panels = [];
    definitions.forEach(function (definition, index) {
      const chart = charts.get(definition.id);
      if (!chart) return;
      const option = chart.getOption(), selected = option.legend[0].selected || {};
      const fields = definition.fields.filter(function (field) {
        return selected[field[1]] !== false && snapshot.distributions[index].values[field[0]];
      });
      if (!fields.length) return;
      const y = option.dataZoom.find(function (d) { return d.id === "y-slider"; });
      const x = option.dataZoom.find(function (d) { return d.id === "count-slider"; });
      panels.push({definition: definition, fields: fields, group: {
        keys: fields.map(function (field) { return field[0]; }),
        valueMin: y.startValue, valueMax: y.endValue, countMin: x.startValue, countMax: x.endValue
      }});
    });
    const results = await request("statistics", {
      lo: snapshot.lo, hi: snapshot.hi, limit: snapshot.count, bins: bins, dataVersion: snapshot.dataVersion,
      groups: panels.map(function (panel) { return panel.group; })
    });
    if (thisEpoch !== epoch) return;
    definitions.forEach(function (definition) { $(definition.id + "-stats").hidden = true; });
    const stale = revision !== statisticsRevision;
    panels.forEach(function (panel, index) {
      const id = panel.definition.id, group = panel.group;
      const container = $(id + "-stats");
      const frequency = Math.ceil(group.countMin) <= Math.floor(group.countMax)
        ? numeric(Math.ceil(group.countMin), 0) + "–" + numeric(Math.floor(group.countMax), 0)
        : "无符合条件的整数频数";
      container.innerHTML = '<p id="' + id + '-stats-state">' +
        (stale ? "结果快照 · 范围或数据已变化，请重新计算" : "结果快照 · σ 为总体标准差（分母 N）") +
        '</p><p>时间：' + date(snapshot.lo) + " 至 " + date(snapshot.hi) +
        "<br>读数：" + numeric(group.valueMin, 6) + "–" + numeric(group.valueMax, 6) +
        "；bin 记录数：" + frequency + "；" + snapshot.distributions[definitions.indexOf(panel.definition)].bin_count + " bins（两轴边界均包含）</p>" +
        "<table><thead><tr><th>通道</th><th>N</th><th>Mean</th><th>σ</th></tr></thead><tbody>" +
        results[index].map(function (row, i) {
          return "<tr><td>" + panel.fields[i][1] + "</td><td>" + numeric(row.n, 0) +
            "</td><td>" + numeric(row.mean, 6) + "</td><td>" + numeric(row.sigma, 6) + "</td></tr>";
        }).join("") + "</tbody></table>";
      container.hidden = false;
    });
    statisticsReady = panels.length > 0;
    $("statistics-notice").textContent = !panels.length ? "当前区间没有可统计的通道。" :
      stale ? "范围或数据已变化；下方是点击时的结果快照，请重新计算。" :
      "已按直方图两轴范围计算，结果在各图的放大详情中。使用原始值，σ 为总体标准差。";
    charts.forEach(function (chart) { chart.resize(); });
  } catch (error) { $("statistics-notice").textContent = "统计失败：" + (error.message || error); }
  finally {
    statisticsBusy = false; button.disabled = !current || !current.count;
    button.textContent = "计算各通道 mean / σ";
  }
}

function paint(result) {
  invalidateStatistics(!result.count);
  current = result;
  $("calculate-stats").disabled = !result.count || statisticsBusy;
  $("count").textContent = numeric(result.count, 0);
  $("range").textContent = result.count ? ((result.last - result.first) / 3600000).toFixed(2) + " 小时" : "等待完整记录";
  $("temperature").textContent = numeric(result.latest.fpga_temperature_c);
  $("latency").textContent = numeric(result.latest.host_latency_us, 0);
  $("resets").textContent = numeric(result.latest.optical_module_reset_count, 0) + " / " + numeric(result.latest.clock_chip_reset_count, 0);
  $("latest").textContent = date(result.last);
  $("empty").hidden = result.count > 0;
  $("charts").hidden = !result.count;
  $("quality").textContent = "无效标志 " + numeric(result.invalid, 0) + " 条 · 跳过格式/时间异常 " +
    numeric(result.badRows, 0) + " 条 · 间隔 >2s " + numeric(result.gaps, 0) +
    " 次 · 主机发送时间连续重复 " + numeric(result.repeated, 0) + " 条" +
    (result.pending ? " · 等待末行写入换行符" : "");
  if (!result.count) return;
  definitions.forEach(function (definition, index) {
    initializeChart(definition);
    const chart = charts.get(definition.id);
    if (!chart) return;
    const min = result.first === result.last ? result.first - 1000 : result.first;
    const max = result.first === result.last ? result.last + 1000 : result.last;
    const start = Math.max(0, Math.min(100, (result.lo - min) / (max - min) * 100));
    const end = Math.max(0, Math.min(100, (result.hi - min) / (max - min) * 100));
    const present = definition.fields.some(function (f) { return (result.plots[f[0]] || []).some(function (p) { return p[1] !== null; }); });
    const distribution = result.distributions[index];
    const extent = {min: distribution.min, max: distribution.max, interval: distribution.interval || null};
    const yZoom = chart.getOption().dataZoom.find(function (d) { return d.id === "y-slider"; });
    const countZoom = chart.getOption().dataZoom.find(function (d) { return d.id === "count-slider"; });
    chart.setOption({
      title: [
        {text: present ? "" : "当前区间没有此字段的有效读数", left: "25%", top: "45%",
          textStyle: {fontSize: 12, fontWeight: "normal", color: "#87979e"}},
        {show: expanded === $(definition.id).closest(".panel"), text: "分布 · " + (distribution.bin_count || 0) + " bins" + (distribution.native ? " · 1 ADC/bin" : ""), left: "78%", top: 12,
          textStyle: {fontSize: 11, fontWeight: "normal", color: "#61747e"}}
      ],
      xAxis: [{id: "time-axis", min: min, max: max}],
      yAxis: [extent, Object.assign({}, extent)],
      dataZoom: [{id: "x-inside", start: start, end: end}, {id: "x-slider", start: start, end: end},
        {id: "count-inside", start: countZoom.start, end: countZoom.end},
        {id: "count-slider", start: countZoom.start, end: countZoom.end},
        {id: "y-slider", start: yZoom.start, end: yZoom.end}],
      series: definition.fields.map(function (f) { return {id: f[0], data: result.plots[f[0]] || []}; })
        .concat(definition.fields.filter(function (f) {
          // Hidden custom series have no graphics; retain their data until visible.
          return distribution.values[f[0]];
        }).map(function (f) {
          const histogram = distribution.values[f[0]];
          return {id: "hist-" + f[0], data: histogram.bins};
        }))
    });
  });
}

async function requestView() {
  const id = ++viewId, thisEpoch = epoch;
  const groups = definitions.map(function (definition) {
    const chart = charts.get(definition.id);
    const selected = chart ? chart.getOption().legend[0].selected || {} : {};
    return definition.fields.filter(function (f) { return selected[f[1]] !== false; })
      .map(function (f) { return f[0]; });
  });
  const result = await request("view", Object.assign({keys: keys, groups: groups, bins: Number($("bins").value)}, view));
  if (id === viewId && thisEpoch === epoch) paint(result);
}
function showError(error) { message(error.message || String(error)); status("读取失败 · 可重试", false); }

function renderLocalStreams() {
  if (syncEnabled) return;
  const seen = new Map();
  allFiles.forEach(function (file) {
    const id = streamKey(file.name);
    if (!seen.has(id)) seen.set(id, file.group || "");
  });
  const list = Array.from(seen.entries()).sort(function (a, b) {
    if (a[0] === "other") return 1;
    if (b[0] === "other") return -1;
    return Number(a[0].slice(4)) - Number(b[0].slice(4));
  });
  const show = list.length > 1;
  $("fee-label").hidden = $("fee").hidden = !show;
  if (!show) {
    if (selectedStream && !syncEnabled) selectedStream = "";
    return;
  }
  if (selectedStream && !seen.has(selectedStream)) selectedStream = "";
  const optionKey = "local:" + list.map(function (item) { return item[0] + ":" + item[1]; }).join("|");
  if (optionKey !== streamOptionsKey) {
    $("fee").replaceChildren(new Option("全部串 / FEE", ""));
    list.forEach(function (item) {
      $("fee").add(new Option(streamLabel(item[0], item[1]), item[0]));
    });
    streamOptionsKey = optionKey;
  }
  $("fee").value = selectedStream;
}

function renderFileOptions() {
  const selected = $("files").value;
  $("files").replaceChildren(new Option("选择 CSV 文件…", ""));
  const visible = (!syncEnabled && selectedStream)
    ? allFiles.filter(function (file) { return streamKey(file.name) === selectedStream; })
    : allFiles;
  visible.forEach(function (file) {
    const prefix = file.group && !file.name.toUpperCase().startsWith(file.group.toUpperCase() + "/")
      ? "[" + file.group + "] " : "";
    $("files").add(new Option(prefix + file.name + "  (" + (file.size / 1048576).toFixed(1) + " MB)", file.name));
  });
  if (Array.from($("files").options).some(function (option) { return option.value === selected; })) $("files").value = selected;
  else if (source && (syncEnabled || !selectedStream || streamKey(source.name) === selectedStream)) {
    $("files").add(new Option(source.name + "（暂时不可用）", source.name));
    $("files").value = source.name;
  }
}

async function listFiles() {
  try {
    const response = await fetch("/api/files", {cache: "no-store"});
    if (!response.ok) throw Error("无法获取数据文件列表");
    const data = await response.json();
    allFiles = data.files || [];
    if (syncEnabled === false) renderLocalStreams();
    renderFileOptions();
    if (!allFiles.length) message("数据目录内没有 CSV。请把日志放入 data 文件夹，或用 --data-dir 指定采集目录，再刷新列表。");
    else message("");
    return true;
  } catch (error) { showError(error); return false; }
}

async function refreshSyncStatus() {
  clearTimeout(syncTimer);
  try {
    const response = await fetch("/api/status", {cache: "no-store"});
    if (!response.ok) throw Error("无法连接工控机监控服务");
    const info = await response.json();
    syncEnabled = !!info.enabled;
    $("sync-panel").hidden = !syncEnabled;
    if (!syncEnabled) {
      renderLocalStreams();
      renderFileOptions();
      return;
    }
    const streams = info.streams || [];
    $("fee-label").hidden = $("fee").hidden = !streams.length;
    if (streams.length) {
      if (!streams.some(function (item) { return item.id === selectedStream; })) {
        selectedStream = source ? streamKey(source.name) : streams[0].id;
        if (!streams.some(function (item) { return item.id === selectedStream; })) selectedStream = streams[0].id;
      }
      const optionKey = "remote:" + streams.map(function (item) { return item.id + ":" + item.available; }).join("|");
      if (optionKey !== streamOptionsKey) {
        $("fee").replaceChildren();
        streams.forEach(function (item) {
          $("fee").add(new Option(item.label + (item.available === false ? "（源暂缺）" : ""), item.id));
        });
        streamOptionsKey = optionKey;
      }
      $("fee").value = selectedStream;
    }
    const stream = streams.find(function (item) { return item.id === selectedStream; });
    const progress = stream || info;
    const target = stream ? stream.file : info.current_file;
    let refreshed = false;
    const filesKey = streams.filter(function (item) { return item.file; })
      .map(function (item) { return item.file; }).sort().join("|");
    if (streams.length && filesKey !== mirroredFilesKey) {
      refreshed = await listFiles();
      if (refreshed) mirroredFilesKey = filesKey;
    }
    const overdue = info.last_success_ms && Date.now() - info.last_success_ms > 30000;
    $("sync-panel").className = "sync-panel" +
      (!info.connected || info.phase === "error" ? " error" : info.phase === "waiting" && !overdue ? " online" : "");
    $("sync-state").textContent = info.phase === "error" || !info.connected && info.phase !== "connecting"
      ? "树莓派已断线 / 同步异常 · 仍可查看本地缓存"
      : info.phase === "connecting" ? "正在连接树莓派"
      : overdue ? "同步检查超时 · 当前显示本地缓存"
      : info.unavailable_files ? "已连接 · 部分源文件缺失，保留已缓存内容"
      : info.phase === "syncing" || info.pending_bytes ? "正在限速补传历史 · 各 FEE 共享总限速" : "树莓派已连接 · 等待新增记录";
    $("sync-detail").textContent = info.source + " · " + info.message +
      " · 最近成功检查：" + date(info.last_success_ms);
    $("sync-progress").textContent = "同步文件：" + (stream ? stream.remote_file : info.current_file || "等待源 CSV") +
      " · 已缓存 " + byteSize(progress.mirrored_bytes) + " / " + byteSize(progress.source_bytes) +
      "；本 FEE 待补 " + byteSize(progress.pending_bytes) + "，全部待补 " +
      byteSize(info.pending_bytes) + " · 远端检查 " + info.poll_seconds +
      " 秒 / 新文件扫描 " + info.scan_seconds + " 秒 · 限速 " + info.limit_kib_per_second + " KiB/s";
    if (target && $("follow-latest").checked && !paused &&
        (!source || source.name !== target)) {
      if (!refreshed) await listFiles();
      if (Array.from($("files").options).some(function (option) { return option.value === target; })) {
        $("files").value = target;
        await selectFile(target);
      }
    }
  } catch (error) {
    $("sync-panel").hidden = false;
    $("sync-panel").className = "sync-panel error";
    $("sync-state").textContent = "工控机监控服务连接失败 · 图表保留已加载数据";
    $("sync-detail").textContent = error.message || String(error);
  } finally {
    if (syncEnabled !== false) syncTimer = setTimeout(refreshSyncStatus, 2000);
  }
}

async function poll(thisEpoch) {
  const active = source;
  const before = current ? current.count : 0;
  controller = new AbortController();
  let more = true, updated = false;
  try {
    message("");
    do {
      const query = new URLSearchParams({name: active.name, offset: String(active.offset),
        generation: active.generation, mtime: active.mtime});
      const response = await fetch("/api/read?" + query, {cache: "no-store", signal: controller.signal});
      if (!response.ok) {
        const error = await response.json();
        throw Error(error.error || "文件读取失败");
      }
      const buffer = await response.arrayBuffer();
      if (thisEpoch !== epoch) return;
      if (response.headers.get("X-Reset") === "1") {
        await request("reset"); view = {mode: "all"}; updateRangeButtons();
        message("检测到文件截断、覆盖或替换，已重新读取。");
        updated = true;
      }
      active.offset = Number(response.headers.get("X-Next-Offset"));
      active.generation = response.headers.get("X-Generation");
      active.mtime = response.headers.get("X-Mtime");
      const size = Number(response.headers.get("X-File-Size"));
      if (buffer.byteLength) {
        await request("append", {buffer: buffer}, [buffer]); updated = true;
      }
      if (thisEpoch !== epoch) return;
      more = response.headers.get("X-More") === "1";
      status(more ? "正在读取 " + Math.round(active.offset / Math.max(size, 1) * 100) + "%" : "正在绘图", false);
      $("file-info").textContent = active.name + " · 已读取 " + (active.offset / 1048576).toFixed(2) + " MB";
    } while (more && thisEpoch === epoch);
    if (updated || !current) await requestView();
    if (thisEpoch !== epoch) return;
    if (current && current.count) $("file-info").textContent += " · " + date(current.first) + " 至 " + date(current.last);
    const added = current ? Math.max(0, current.count - before) : 0;
    $("checked").textContent = "上次检查 " + date(Date.now()).slice(11) + (added ? " · 新增 " + numeric(added, 0) + " 条" : " · 暂无新增");
    status(paused ? "已暂停自动检查" : "自动检查中 · " + Number($("interval").value) / 1000 + " 秒", !paused);
  } catch (error) {
    if (thisEpoch === epoch && error.name !== "AbortError") {
      // A parser error must restart at byte zero on the next attempt.
      if (active.offset) { active.offset = 0; active.generation = ""; active.mtime = ""; await request("reset"); current = null; }
      showError(error);
    }
  }
}

function schedule() {
  clearTimeout(timer);
  if (source && !paused) timer = setTimeout(startPoll, Number($("interval").value));
}
function startPoll() {
  if (running || !source) return running;
  const thisEpoch = epoch;
  running = poll(thisEpoch).finally(function () {
    running = null;
    if (thisEpoch === epoch) schedule();
  });
  return running;
}

async function selectFile(name) {
  if (expanded) { restorePlot(); $("plot-dialog").close(); }
  const thisEpoch = ++epoch;
  invalidateStatistics(true);
  clearTimeout(timer); clearTimeout(zoomTimer); viewId++;
  if (controller) controller.abort();
  if (running) await running;
  if (thisEpoch !== epoch) return;
  await request("reset");
  if (thisEpoch !== epoch) return;
  source = name ? {name: name, offset: 0, generation: "", mtime: ""} : null;
  current = null; view = {mode: "all"}; paused = false;
  $("pause").textContent = "暂停更新"; $("pause").disabled = $("refresh").disabled = !source;
  charts.forEach(function (chart) { chart.dispose(); }); charts.clear();
  updateRangeButtons();
  paint({count: 0, latest: {}, invalid: 0, badRows: 0, gaps: 0, repeated: 0, plots: {}});
  current = null;
  if (source) { status("开始读取…", false); startPoll(); }
  else { $("file-info").textContent = "从数据目录选择 CSV 文件。"; status("等待选择文件", false); }
}
$("files").addEventListener("change", function () {
  $("follow-latest").checked = false;
  if (syncEnabled) {
    selectedStream = streamKey($("files").value);
    if (Array.from($("fee").options).some(function (option) { return option.value === selectedStream; }))
      $("fee").value = selectedStream;
  }
  selectFile($("files").value).catch(showError);
});
$("fee").onchange = function () {
  selectedStream = $("fee").value;
  if (syncEnabled) {
    $("follow-latest").checked = true;
    paused = false; $("pause").textContent = "暂停更新";
    return refreshSyncStatus();
  }
  renderFileOptions();
  const candidates = allFiles.filter(function (file) { return !selectedStream || streamKey(file.name) === selectedStream; });
  if (selectedStream && candidates.length === 1 && (!source || source.name !== candidates[0].name)) {
    $("files").value = candidates[0].name;
    return selectFile(candidates[0].name).catch(showError);
  }
};
$("follow-latest").onchange = function () { if ($("follow-latest").checked) refreshSyncStatus(); };
$("pause").onclick = function () {
  paused = !paused; $("pause").textContent = paused ? "继续更新" : "暂停更新";
  status(paused ? "已暂停自动检查" : "自动检查中", !paused);
  if (paused) clearTimeout(timer);
  else { startPoll(); if (syncEnabled) refreshSyncStatus(); }
};
$("refresh").onclick = function () { clearTimeout(timer); startPoll(); };
$("refresh-list").onclick = listFiles;
$("interval").onchange = schedule;
$("calculate-stats").onclick = calculateStatistics;
$("bins").onchange = function () { if (source) requestView().catch(showError); };
$("range-tools").onclick = function (event) {
  const button = event.target.closest("[data-range]");
  if (!button) return;
  view = button.dataset.range === "all" ? {mode: "all"} : {mode: "tail", span: Number(button.dataset.range)};
  updateRangeButtons(); if (source) requestView().catch(showError);
};
$("reset-view").onclick = function () {
  view = {mode: "all"}; updateRangeButtons();
  charts.forEach(function (chart) { chart.setOption({dataZoom: [
    {id: "y-slider", start: 0, end: 100},
    {id: "count-inside", start: 0, end: 100}, {id: "count-slider", start: 0, end: 100}
  ]}); });
  if (source) requestView().catch(showError);
};
window.addEventListener("resize", function () { charts.forEach(function (chart) { chart.resize(); }); });

try {
  if (location.protocol === "file:") throw Error("请先运行 python3 server.py --open，再通过 http://127.0.0.1:8765 打开页面。");
  if (!window.echarts) throw Error("图表库未加载，请保留完整的 vendor 文件夹。");
  worker = new Worker("data-worker.js");
  worker.onmessage = function (event) {
    const data = event.data, task = pending.get(data.id);
    if (!task) return;
    pending.delete(data.id);
    if (data.error) task.reject(Error(data.error));
    else task.resolve(data.result || data);
  };
  worker.onerror = function (event) {
    const error = Error(event.message || "后台数据处理失败");
    pending.forEach(function (task) { task.reject(error); }); pending.clear(); showError(error);
  };
  listFiles();
  refreshSyncStatus();
} catch (error) {
  showError(error); $("files").disabled = true;
}
