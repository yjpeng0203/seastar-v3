"use strict";
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const assert = require("node:assert/strict");
const echarts = require("../vendor/echarts.min.js");
const root = path.join(__dirname, "..");
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    clientWidth: 895, value: id === "bins" ? "200" : "", options: [], listeners: {},
    classList: {toggle: function () {}}, addEventListener: function (type, action) { this.listeners[type] = action; },
    closest: function () { return element(id + "-panel"); },
    replaceChildren: function (...items) { this.options = items; }, add: function (item) { this.options.push(item); }
  });
  return elements.get(id);
}
const workerContext = vm.createContext({TextDecoder: TextDecoder, self: {}});
vm.runInContext(fs.readFileSync(path.join(root, "data-worker.js"), "utf8"), workerContext);
class TestWorker {
  constructor() {
    const worker = this;
    workerContext.self.postMessage = function (data) { worker.onmessage({data: data}); };
  }
  postMessage(data) { workerContext.self.onmessage({data: data}); }
}
const chartAPI = Object.assign({}, echarts, {
  init: function () { return echarts.init(null, null, {renderer: "svg", ssr: true, width: 895, height: 410}); }
});
const app = vm.createContext({
  console: console, Intl: Intl, URLSearchParams: URLSearchParams,
  setTimeout: setTimeout, clearTimeout: clearTimeout, Worker: TestWorker,
  echarts: chartAPI, location: {protocol: "http:"},
  Option: function (label, value) { this.label = label; this.value = value; },
  AbortController: AbortController,
  window: {echarts: chartAPI, addEventListener: function () {}},
  document: {getElementById: element, querySelectorAll: function () { return []; }},
  fetch: async function () { return {ok: true, json: async function () { return {files: []}; }}; }
});
vm.runInContext(fs.readFileSync(path.join(root, "app.js"), "utf8"), app);
async function turn() { await new Promise(function (resolve) { setImmediate(resolve); }); }
// Context lexical bindings are intentionally accessed through the same runtime.
app.sample = Buffer.from("timestamp_ms,baseline_ch0,baseline_ch1,baseline_ch2\n1000,54,75,36\n2000,239,247,362\n3000,197,164,192\n");
(async function () {
  await vm.runInContext('request("append", {buffer: sample})', app);
  await vm.runInContext("requestView()", app);
  const chart = vm.runInContext('charts.get("baseline")', app);
  assert.equal(chart.getOption().legend[0].show, false);
  assert.equal(chart.getOption().dataZoom.find(d => d.id === "x-slider").show, false);
  vm.runInContext('chartLayout(charts.get("baseline"), true)', app);
  assert.equal(chart.getOption().legend[0].show, true);
  assert.equal(chart.getOption().dataZoom.find(d => d.id === "x-slider").show, true);
  assert.equal(chart.getOption().dataZoom.find(d => d.id === "x-inside").disabled, false);
  function bounds(label) {
    const axes = chart.getOption().yAxis;
    const zoom = chart.getOption().dataZoom.find(function (d) { return d.id === "y-slider"; });
    assert.equal(zoom.rangeMode[0], "percent", label);
    assert.equal(chart.convertToPixel({yAxisIndex: 0}, axes[0].min), chart.convertToPixel({yAxisIndex: 1}, axes[1].min));
    assert.equal(chart.convertToPixel({yAxisIndex: 0}, axes[0].max), chart.convertToPixel({yAxisIndex: 1}, axes[1].max));
    return axes;
  }
  element("bins").value = "0";
  await vm.runInContext("requestView()", app);
  assert.equal(vm.runInContext('current.distributions[1].bin_width', app), 1);
  assert.equal(vm.runInContext('current.distributions[1].bin_count', app), 327);
  assert.equal(vm.runInContext('current.distributions[1].native', app), true);
  element("bins").value = "200";
  await vm.runInContext("requestView()", app);
  let axes = bounds("initial");
  assert(axes[0].min <= 36 && axes[0].max >= 362);
  const originalView = vm.runInContext("JSON.stringify(view)", app);
  const countPixel = chart.convertToPixel({xAxisIndex: 1}, 1);
  chart.dispatchAction({type: "dataZoom", dataZoomId: "count-slider", start: 0, end: 20});
  await turn();
  assert(chart.convertToPixel({xAxisIndex: 1}, 1) > countPixel);
  assert.equal(vm.runInContext("JSON.stringify(view)", app), originalView);
  await vm.runInContext("requestView()", app);
  assert.equal(chart.getOption().dataZoom.find(function (d) { return d.id === "count-slider"; }).end, 20);
  assert.equal(vm.runInContext("current.distributions[1].values.baseline_ch2.count", app), 3);
  chart.dispatchAction({type: "dataZoom", dataZoomId: "count-inside", start: 0, end: 50});
  await turn();
  assert.equal(chart.getOption().dataZoom.find(function (d) { return d.id === "count-slider"; }).end, 50);
  const dataBeforeResize = JSON.stringify(chart.getOption().series.map(s => s.data));
  vm.runInContext('chartLayout(charts.get("baseline"), false); chartLayout(charts.get("baseline"), true)', app);
  assert.equal(chart.getOption().dataZoom.find(d => d.id === "count-slider").end, 50);
  assert.equal(JSON.stringify(chart.getOption().series.map(s => s.data)), dataBeforeResize);
  element("reset-view").onclick();
  assert.equal(chart.getOption().dataZoom.find(function (d) { return d.id === "count-slider"; }).end, 100);
  assert.equal(chart.convertToPixel({xAxisIndex: 1}, 1), countPixel);
  chart.dispatchAction({type: "legendToggleSelect", name: "ch2"});
  await turn();
  axes = bounds("hidden");
  assert(axes[0].max < 362);
  chart.dispatchAction({type: "legendToggleSelect", name: "ch2"});
  await turn();
  axes = bounds("restored");
  assert(axes[0].min <= 36 && axes[0].max >= 362);
  chart.dispatchAction({type: "dataZoom", dataZoomId: "count-slider", start: 0, end: 25});
  for (const bins of [400, 50, 200]) {
    element("bins").value = String(bins);
    await vm.runInContext("requestView()", app);
    assert.equal(chart.getOption().series.find(function (s) { return s.id === "hist-baseline_ch2"; }).data.length, bins);
    assert.equal(chart.getOption().dataZoom.find(function (d) { return d.id === "count-slider"; }).end, 25);
    bounds("changed bins");
  }
  chart.dispatchAction({type: "legendToggleSelect", name: "ch2"});
  await turn();
  element("bins").value = "50";
  await vm.runInContext("requestView()", app);
  chart.dispatchAction({type: "legendToggleSelect", name: "ch2"});
  await turn();
  axes = bounds("restored after changing bins while hidden");
  assert(axes[0].max >= 362);
  assert.equal(chart.getOption().series.find(function (s) { return s.id === "hist-baseline_ch2"; }).data.length, 50);
  chart.dispatchAction({type: "dataZoom", dataZoomId: "y-slider", start: 50, end: 100});
  bounds("vertical zoom");
  await vm.runInContext('view = {mode: "custom", lo: 1000, hi: 1000}; requestView()', app);
  assert.equal(vm.runInContext('current.distributions[1].values.baseline_ch2.count', app), 1);
  bounds("time window");
  element("reset-view").onclick();
  chart.dispatchAction({type: "dataZoom", dataZoomId: "y-slider", start: 25, end: 75});
  await element("calculate-stats").onclick();
  assert.equal(element("baseline-stats").hidden, false);
  assert.match(element("baseline-stats").innerHTML, /ch0<\/td><td>2<\/td><td>218<\/td><td>21<\/td>/);
  chart.dispatchAction({type: "dataZoom", dataZoomId: "count-slider", start: 0, end: 10});
  assert.match(element("statistics-notice").textContent, /变化/);
  await element("calculate-stats").onclick();
  assert.match(element("baseline-stats").innerHTML, /ch0<\/td><td>0<\/td><td>—<\/td><td>—<\/td>/);
  // Remote mode follows local mirrors, without browser requests to the Pi.
  const bodies = {
    "live.csv": Buffer.from("timestamp_ms,baseline_ch0\n10000,197\n11000,198\n"),
    "next.csv": Buffer.from("timestamp_ms,baseline_ch0\n12000,199\n")
  };
  const remote = {enabled: true, connected: true, phase: "waiting", source: "unit@pi:/csv",
    message: "waiting", current_file: "live.csv", last_success_ms: Date.now(),
    poll_seconds: 5, scan_seconds: 30, limit_kib_per_second: 128};
  app.fetch = async function (route) {
    assert(String(route).startsWith("/"));
    if (route === "/api/status") return {ok: true, json: async function () { return remote; }};
    if (route === "/api/files") return {ok: true, json: async function () {
      return {files: Object.keys(bodies).map(function (name) { return {name: name, size: bodies[name].length}; })};
    }};
    const query = new URL("http://localhost" + route).searchParams;
    const full = bodies[query.get("name")], offset = Number(query.get("offset"));
    const body = full.subarray(offset);
    const headers = {"X-Reset": "0", "X-Next-Offset": String(full.length),
      "X-Generation": "unit", "X-Mtime": "1", "X-File-Size": String(full.length), "X-More": "0"};
    return {ok: true, headers: {get: function (key) { return headers[key]; }},
      arrayBuffer: async function () { return body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength); }};
  };
  element("follow-latest").checked = true;
  await vm.runInContext("refreshSyncStatus()", app);
  await vm.runInContext("running", app);
  assert.equal(vm.runInContext("source.name", app), "live.csv");
  assert.equal(vm.runInContext("current.count", app), 2);
  remote.current_file = "next.csv";
  await vm.runInContext("refreshSyncStatus()", app);
  await vm.runInContext("running", app);
  assert.equal(vm.runInContext("source.name", app), "next.csv");
  assert.equal(vm.runInContext("current.latest.baseline_ch0", app), 199);
  element("follow-latest").checked = false;
  remote.current_file = "live.csv";
  await vm.runInContext("refreshSyncStatus()", app);
  assert.equal(vm.runInContext("source.name", app), "next.csv");
  remote.phase = "error"; remote.connected = false; remote.message = "simulated disconnect";
  await vm.runInContext("refreshSyncStatus()", app);
  assert.match(element("sync-state").textContent, /断线/);
  assert.equal(vm.runInContext("current.count", app), 1);
  remote.connected = true; remote.phase = "waiting"; remote.message = "waiting";
  bodies["readback_fee_3.csv"] = bodies["live.csv"];
  bodies["readback_fee_8.csv"] = bodies["next.csv"];
  remote.streams = [
    {id: "fee_3", label: "FEE 3", file: "readback_fee_3.csv", remote_file: "readback_fee_3.csv", available: true},
    {id: "fee_8", label: "FEE 8", file: "readback_fee_8.csv", remote_file: "readback_fee_8.csv", available: true}
  ];
  element("follow-latest").checked = true;
  await vm.runInContext("refreshSyncStatus()", app);
  await vm.runInContext("running", app);
  assert.equal(element("fee").options.length, 2);
  assert.equal(vm.runInContext("source.name", app), "readback_fee_3.csv");
  element("fee").value = "fee_8";
  await element("fee").onchange();
  await vm.runInContext("running", app);
  assert.equal(vm.runInContext("source.name", app), "readback_fee_8.csv");
  // Another board becoming globally newest must not change the chosen FEE.
  remote.current_file = "readback_fee_3.csv";
  await vm.runInContext("refreshSyncStatus()", app);
  assert.equal(vm.runInContext("source.name", app), "readback_fee_8.csv");
  // A local string filter must not label the previous string's file as missing.
  vm.runInContext(`
    syncEnabled = false;
    allFiles = [3, 4, 5].map(n => ({name: "G" + n + "/csv/readback_fee_" + n + ".csv", size: 100, group: "G" + n}));
    selectedStream = "fee_4";
    source = {name: allFiles[0].name};
    $("files").value = source.name;
    renderLocalStreams();
    renderFileOptions();
  `, app);
  assert.equal(element("fee").value, "fee_4");
  assert.deepEqual(element("files").options.map(o => o.value), ["", "G4/csv/readback_fee_4.csv"]);
  // A genuinely missing file from the selected string remains visible.
  vm.runInContext('source = {name: "G4/csv/readback_fee_4_old.csv"}; renderFileOptions()', app);
  assert.match(element("files").options.at(-1).label, /暂时不可用/);
  vm.runInContext("clearTimeout(syncTimer); clearTimeout(timer); clearTimeout(zoomTimer)", app);
  vm.runInContext("charts.forEach(function (chart) { chart.dispose(); })", app);
  console.log("Chart layout and legend checks passed.");
})().catch(function (error) {
  vm.runInContext("charts.forEach(function (chart) { chart.dispose(); })", app);
  console.error(error); process.exitCode = 1;
});
