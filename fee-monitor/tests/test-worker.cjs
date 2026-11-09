"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
let response;
const context = vm.createContext({
  TextDecoder: TextDecoder,
  self: {postMessage: function (message) { response = message; }}
});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../data-worker.js"), "utf8"), context);
let id = 0;
function send(type, data) {
  context.self.onmessage({data: Object.assign({type: type, id: ++id}, data || {})});
  if (response.error) throw Error(response.error);
  return response.result;
}
function append(text) { send("append", {buffer: Buffer.from(text, "utf8")}); }
function view(keys, extra) { return send("view", Object.assign({mode: "all", keys: keys || ["baseline_ch0"]}, extra || {})); }

// Split both escaped quotes and a UTF-8 sequence across arbitrary byte boundaries.
const csv = Buffer.from('\uFEFF"timestamp_ms",baseline_ch0,host_send_us,host_recv_us,valid,tod_time,note\r\n' +
  '1000,197,1000000,1000145,1,1970-01-01 08:00:01.00,"树莓派 ""测试""\n多行"\r\n' +
  '2000,999,1000000,1000200,0,1970-01-01 08:00:02.00,ok\r\n' +
  '5000,,5000000,5000100,1,1970-01-01 08:00:05.00,missing\r\n');
for (let i = 0; i < csv.length; i += 7) send("append", {buffer: csv.subarray(i, i + 7)});
let result = view(["baseline_ch0", "host_latency_us", "tod_offset_s"]);
assert.equal(result.count, 3);
assert.equal(result.invalid, 1);
assert.equal(result.repeated, 1);
assert.equal(result.gaps, 1);
assert.equal(result.latest.host_latency_us, 100);
assert.equal(result.latest.tod_offset_s, 0);
assert.equal(result.plots.baseline_ch0[2][1], null);
const initialHistogram = result.distributions[0].values.baseline_ch0;
assert.equal(initialHistogram.count, 2); // Missing numeric values never become zeros.
assert.equal(initialHistogram.bins.reduce(function (sum, bin) { return sum + bin[0]; }, 0), 2);
assert.equal(initialHistogram.bins.length, 200);
append("6000,198,6000000,6000100,1,1970-01-01 08:00:06.00,partial");
assert.equal(view().count, 3);
assert.equal(view().pending, true);
append("\n");
assert.equal(view().count, 4);
append("bad,row\n4000,1,1,1,1,1970-01-01 08:00:04.00,backwards\n");
assert.equal(view().badRows, 2);

send("reset");
append("timestamp_ms,baseline_ch0\n");
const lines = [];
for (let i = 0; i < 10000; i++) lines.push((1000 + i * 1000) + "," + (i === 4321 ? 9999 : i === 4322 ? -999 : 197));
append(lines.join("\n") + "\n");
result = view();
assert.equal(result.count, 10000);
assert(result.plots.baseline_ch0.length < 2000);
assert(result.plots.baseline_ch0.some(function (p) { return p[1] === 9999; }));
assert(result.plots.baseline_ch0.some(function (p) { return p[1] === -999; }));
assert.equal(result.distributions[0].values.baseline_ch0.count, 10000);
assert.equal(result.distributions[0].values.baseline_ch0.bins.reduce(function (n, b) { return n + b[0]; }, 0), 10000);
// The histogram counts every raw point, although the time plot has under 2,000.
result = view(["baseline_ch0"], {mode: "custom", lo: 4322000, hi: 4323000});
assert.equal(result.plots.baseline_ch0.length, 2);
assert.equal(result.plots.baseline_ch0[0][1], 9999);
assert.equal(result.plots.baseline_ch0[1][1], -999);
assert.equal(result.distributions[0].values.baseline_ch0.count, 2);
assert.equal(view(["baseline_ch0"], {mode: "custom", lo: 0, hi: 1}).distributions[0].values.baseline_ch0, undefined);

send("reset");
append("timestamp_ms,baseline_ch0,baseline_ch1\n1000,0,10\n2000,10,10\n3000,,10\n");
result = view(["baseline_ch0", "baseline_ch1"], {groups: [["baseline_ch0", "baseline_ch1"]], bins: 20});
const distribution = result.distributions[0], bins = distribution.values.baseline_ch0.bins;
assert.equal(bins[0][0], 1);
assert.equal(bins[bins.length - 1][0], 1); // Include the maximum in the final bin.
assert.equal(distribution.values.baseline_ch1.count, 3);
assert.equal(bins[0][1], distribution.values.baseline_ch1.bins[0][1]);
assert.equal(bins[bins.length - 1][2], distribution.values.baseline_ch1.bins[bins.length - 1][2]);
result = view(["baseline_ch1"], {bins: 400});
assert.equal(result.distributions[0].values.baseline_ch1.bins.length, 400);
assert(result.distributions[0].min < 10 && result.distributions[0].max > 10);
assert.equal(result.distributions[0].values.baseline_ch1.bins.reduce(function (n, b) { return n + b[0]; }, 0), 3);
assert.deepEqual(Object.keys(view(["baseline_ch1"], {groups: [[]]}).distributions[0].values), []);
send("reset");
append("timestamp_ms,baseline_ch0,baseline_ch1\n1000,10,1000000000001\n2000,10,1000000000002\n3000,20,1000000000003\n4000,30,\n5000,30,\n6000,30,\n7000,40,\n8000,,\n");
function stats(group, extra) {
  return send("statistics", Object.assign({
    lo: 1000, hi: 8000, limit: 8, bins: 20,
    groups: [Object.assign({keys: ["baseline_ch0"], valueMin: 0, valueMax: 100,
      countMin: 0, countMax: 100}, group || {})]
  }, extra || {}))[0][0];
}
let selected = stats({valueMin: 15, valueMax: 35, countMin: 2, countMax: 3});
assert.equal(selected.n, 3); // Only the three 30s; the bin containing 20 has frequency 1.
assert.equal(selected.mean, 30);
assert.equal(selected.sigma, 0);
selected = stats({valueMin: 10, valueMax: 20, countMin: 1, countMax: 2});
assert.equal(selected.n, 3);
assert(Math.abs(selected.mean - 40 / 3) < 1e-12);
assert(Math.abs(selected.sigma - Math.sqrt(200 / 9)) < 1e-12);
assert.equal(stats({countMin: 4}).n, 0);
assert.equal(stats({countMin: 4}).mean, null);
assert.equal(stats({countMin: 4}).sigma, null);
assert.equal(stats({valueMin: 40, valueMax: 40}).n, 1);
assert.equal(stats({valueMin: 40, valueMax: 40}).sigma, 0);
assert.equal(stats({countMin: 2, countMax: 2}, {hi: 3000}).n, 2);
assert.equal(stats({}, {hi: 8000, limit: 2}).n, 2);
selected = stats({keys: ["baseline_ch1"], valueMin: 1e12, valueMax: 1e12 + 10});
assert.equal(selected.n, 3);
assert.equal(selected.mean, 1000000000002);
assert(Math.abs(selected.sigma - Math.sqrt(2 / 3)) < 1e-12);
const oldVersion = view().dataVersion;
send("reset");
assert.throws(function () { stats({}, {dataVersion: oldVersion}); }, /已重置/);
assert.throws(function () { send("reset"); append("other,baseline_ch0\n"); }, /timestamp_ms/);

const actual = process.argv[2];
if (actual) {
  send("reset");
  const buffer = fs.readFileSync(actual);
  const started = performance.now();
  for (let offset = 0; offset < buffer.length; offset += 4 * 1024 * 1024)
    send("append", {buffer: buffer.subarray(offset, offset + 4 * 1024 * 1024)});
  result = view(["baseline_ch2", "fpga_temperature_c", "host_latency_us"],
    {groups: [["baseline_ch2"], ["fpga_temperature_c"], ["host_latency_us"]]});
  assert.equal(result.count, 220615);
  assert.equal(result.badRows, 0);
  assert.equal(result.latest.fpga_temperature_c, 45.77);
  assert(result.plots.baseline_ch2.some(function (p) { return p[1] === 362; }));
  assert(result.plots.baseline_ch2.some(function (p) { return p[1] === 36; }));
  result.distributions.forEach(function (group) {
    Object.values(group.values).forEach(function (hist) {
      assert.equal(hist.count, 220615);
      assert.equal(hist.bins.reduce(function (sum, bin) { return sum + bin[0]; }, 0), 220615);
    });
  });
  // Independently count the original values landing in the bin containing 192.
  const originalLines = buffer.toString("utf8").trim().split(/\r?\n/);
  const column = originalLines.shift().split(",").indexOf("baseline_ch2");
  const bin = result.distributions[0].values.baseline_ch2.bins.find(function (b) { return b[1] <= 192 && b[2] > 192; });
  const expected = originalLines.reduce(function (n, line) {
    const value = Number(line.split(",")[column]);
    return n + (value >= bin[1] && value < bin[2] ? 1 : 0);
  }, 0);
  assert.equal(bin[0], expected);
  const channelValues = originalLines.map(function (line) { return Number(line.split(",")[column]); });
  const frequencies = new Array(200).fill(0);
  channelValues.forEach(function (value) { frequencies[Math.min(199, Math.floor(value / 2))]++; });
  const kept = channelValues.filter(function (value) {
    const frequency = frequencies[Math.min(199, Math.floor(value / 2))];
    return value >= 185 && value <= 200 && frequency >= 500 && frequency <= 100000;
  });
  const independentMean = kept.reduce(function (sum, value) { return sum + value; }, 0) / kept.length;
  const independentSigma = Math.sqrt(kept.reduce(function (sum, value) {
    return sum + Math.pow(value - independentMean, 2);
  }, 0) / kept.length);
  selected = stats({keys: ["baseline_ch2"], valueMin: 185, valueMax: 200,
    countMin: 500, countMax: 100000}, {lo: result.first, hi: result.last, limit: result.count, bins: 200});
  assert.equal(selected.n, kept.length);
  assert(Math.abs(selected.mean - independentMean) < 1e-9);
  assert(Math.abs(selected.sigma - independentSigma) < 1e-9);
  console.log("Original CSV: " + result.count + " rows, " + Math.round(performance.now() - started) + " ms, extremes preserved.");
}
console.log("Worker checks passed.");

// Native default: ADC bin edges precede any display scaling; sparse gaps stay exact.
send("reset");
append("timestamp_ms,baseline_ch0,fpga_temperature_c\n1000,-.2,25.1\n2000,.2,25.2\n3000,.9,25.3\n4000,4000,25.4\n");
result = view(["baseline_ch0", "fpga_temperature_c"], {groups: [["baseline_ch0"], ["fpga_temperature_c"]], bins: 0});
assert.equal(result.distributions[0].bin_width, 1);
assert.equal(result.distributions[0].bin_count, 4002);
assert.deepEqual(JSON.parse(JSON.stringify(result.distributions[0].values.baseline_ch0.bins)),
  [[1, -1, 0], [2, 0, 1], [1, 4000, 4001]]);
assert.equal(result.distributions[1].bin_count, 200);
const nativeStats = send("statistics", {lo: 1000, hi: 4000, limit: 4, bins: 0,
  groups: [{keys: ["baseline_ch0"], valueMin: -1, valueMax: 4001, countMin: 2, countMax: 2}]});
assert.equal(nativeStats[0][0].n, 2);
assert.equal(nativeStats[0][0].mean, .55);
console.log("Native ADC default and frequency-selection checks passed.");
