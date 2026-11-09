/* Full data stays in this worker; the UI receives a min/max envelope per plot. */
"use strict";
let columns, series, times, count, badRows, invalid, gaps, repeated, decoder;
let field, row, quoted, afterQuote, previousSend;
let dataVersion = 0;

function reset() {
  dataVersion++;
  columns = null; series = {}; times = []; count = badRows = invalid = gaps = repeated = 0;
  field = ""; row = []; quoted = afterQuote = false; previousSend = null;
  decoder = new TextDecoder("utf-8");
}
reset();

function number(value) {
  return value === undefined || value.trim() === "" ? NaN : Number(value);
}

function finishRow() {
  row.push(field); field = ""; afterQuote = false;
  const cells = row; row = [];
  if (cells.length === 1 && cells[0].trim() === "") return;
  if (!columns) {
    cells[0] = cells[0].replace(/^\uFEFF/, "");
    columns = cells.map(function (c) { return c.trim(); });
    if (!columns.includes("timestamp_ms")) throw Error("CSV 缺少 timestamp_ms 列");
    if (new Set(columns).size !== columns.length) throw Error("CSV 存在重复列名");
    columns.forEach(function (c) { if (c !== "tod_time") series[c] = []; });
    series.host_latency_us = []; series.host_age_s = []; series.tod_offset_s = [];
    return;
  }
  if (cells.length !== columns.length) { badRows++; return; }
  const values = {};
  columns.forEach(function (c, i) { values[c] = c === "tod_time" ? cells[i] : number(cells[i]); });
  const t = values.timestamp_ms;
  if (!Number.isFinite(t) || t < 0 || t > 8640000000000000 ||
      (count && t < times[count - 1])) { badRows++; return; }
  if (count && t - times[count - 1] > 2000) gaps++;
  if (values.valid !== undefined && values.valid !== 1) invalid++;
  if (Number.isFinite(values.host_send_us) && values.host_send_us === previousSend) repeated++;
  previousSend = values.host_send_us;
  values.host_latency_us = values.host_recv_us - values.host_send_us;
  values.host_age_s = (t * 1000 - values.host_send_us) / 1000000;
  const tod = values.tod_time;
  const todMs = typeof tod === "string" && /^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d/.test(tod)
    ? Date.parse(tod.replace(" ", "T") + "+08:00") : NaN;
  values.tod_offset_s = (t - todMs) / 1000;
  Object.keys(series).forEach(function (c) { series[c].push(values[c]); });
  times.push(t); count++;
}

function ingest(buffer) {
  const text = decoder.decode(buffer, {stream: true});
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"') { quoted = false; afterQuote = true; }
      else field += c;
    } else if (afterQuote && c === '"') {
      field += '"'; quoted = true; afterQuote = false;
    } else if (c === ",") {
      row.push(field); field = ""; afterQuote = false;
    } else if (c === "\n") {
      finishRow();
    } else if (c !== "\r") {
      if (c === '"' && field === "" && !afterQuote) quoted = true;
      else {
        if (afterQuote && c.trim()) throw Error("CSV 引号后出现无效字符");
        field += c;
      }
    }
  }
}

function bound(value, upper) {
  let lo = 0, hi = count;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (times[mid] < value || (upper && times[mid] === value)) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function envelope(key, start, end, buckets) {
  const values = series[key];
  if (!values || end <= start) return [];
  const result = [];
  function push(i) {
    result.push([times[i], Number.isFinite(values[i]) ? values[i] : null]);
  }
  if (end - start <= buckets * 2) {
    for (let i = start; i < end; i++) push(i);
    return result;
  }
  push(start);
  const step = (end - start) / buckets;
  for (let b = 0; b < buckets; b++) {
    const left = start + Math.floor(b * step);
    const right = Math.min(end, start + Math.floor((b + 1) * step));
    let min = -1, max = -1;
    for (let i = left; i < right; i++) {
      if (!Number.isFinite(values[i])) continue;
      if (min < 0 || values[i] < values[min]) min = i;
      if (max < 0 || values[i] > values[max]) max = i;
    }
    if (min < 0) push(left);
    else {
      push(Math.min(min, max));
      if (min !== max) push(Math.max(min, max));
    }
  }
  push(end - 1);
  return result;
}

function histogram(keys, start, end, bins) {
  let min = Infinity, max = -Infinity;
  keys.forEach(function (key) {
    const values = series[key];
    if (!values) return;
    for (let i = start; i < end; i++) {
      if (!Number.isFinite(values[i])) continue;
      min = Math.min(min, values[i]); max = Math.max(max, values[i]);
    }
  });
  if (!Number.isFinite(min)) return {min: 0, max: 1, values: {}};
  const native = bins === 0 && keys.every(function (key) { return key.startsWith("baseline_ch"); });
  if (bins === 0) bins = 200;
  const rawMin = min, rawMax = max;
  if (min === max) {
    const padding = Math.max(0.5, Math.abs(min) * 0.01);
    min -= padding; max += padding;
  }
  const power = Math.pow(10, Math.floor(Math.log10((max - min) / 5)));
  const ratio = (max - min) / (5 * power);
  const step = (ratio <= 1 ? 1 : ratio <= 2 ? 2 : ratio <= 5 ? 5 : 10) * power;
  min = Math.floor(min / step) * step; max = Math.ceil(max / step) * step;
  if (native) { min = Math.floor(rawMin); max = Math.floor(rawMax) + 1; bins = max - min; }
  const width = (max - min) / bins, values = {};
  keys.forEach(function (key) {
    const source = series[key];
    if (!source) return;
    const counts = native ? {} : new Array(bins).fill(0);
    let total = 0;
    for (let i = start; i < end; i++) {
      if (!Number.isFinite(source[i])) continue;
      const bin = Math.max(0, Math.min(bins - 1, Math.floor((source[i] - min) / width)));
      counts[bin] = (counts[bin] || 0) + 1; total++;
    }
    values[key] = {count: total, bins: Object.keys(counts).map(function (index) {
      const i = Number(index), n = counts[index];
      return [n, min + i * width, i === bins - 1 ? max : min + (i + 1) * width];
    })};
  });
  return {min: min, max: max, interval: step, bin_count: bins, bin_width: width, native: native, values: values};
}

function render(request) {
  const first = times[0], last = times[count - 1];
  let lo = first, hi = last;
  if (request.mode === "tail" && count) lo = Math.max(first, last - request.span);
  if (request.mode === "custom") { lo = request.lo; hi = request.hi; }
  const start = bound(lo, false), end = bound(hi, true);
  const plots = {};
  request.keys.forEach(function (key) { plots[key] = envelope(key, start, end, 900); });
  const bins = Number.isInteger(request.bins) && (request.bins === 0 || request.bins >= 20 && request.bins <= 400) ? request.bins : 200;
  const distributions = (request.groups || [request.keys]).map(function (group) {
    return histogram(group, start, end, bins);
  });
  const latest = {};
  Object.keys(series).forEach(function (key) {
    latest[key] = count && Number.isFinite(series[key][count - 1]) ? series[key][count - 1] : null;
  });
  return {count: count, badRows: badRows, invalid: invalid, gaps: gaps,
          repeated: repeated, pending: !!(field || row.length || quoted || afterQuote),
          first: first, last: last, lo: lo, hi: hi, plots: plots, distributions: distributions, latest: latest,
          columns: columns || [], dataVersion: dataVersion};
}

function selectionStatistics(request) {
  if (request.dataVersion !== undefined && request.dataVersion !== dataVersion)
    throw Error("数据文件已重置，请重新计算");
  const start = bound(request.lo, false);
  const end = Math.min(bound(request.hi, true), request.limit, count);
  const bins = Number.isInteger(request.bins) && (request.bins === 0 || request.bins >= 20 && request.bins <= 400) ? request.bins : 200;
  return request.groups.map(function (group) {
    // Frequencies use the complete time window, before the readout-value cut.
    const distribution = histogram(group.keys, start, end, bins);
    return group.keys.map(function (key) {
      const values = series[key], hist = distribution.values[key];
      let n = 0, mean = 0, m2 = 0;
      if (values && hist) {
        const width = distribution.bin_width, binCount = distribution.bin_count;
        const frequencies = new Map(hist.bins.map(function (bin) {
          return [Math.round((bin[1] - distribution.min) / width), bin[0]];
        }));
        for (let i = start; i < end; i++) {
          const value = values[i];
          if (!Number.isFinite(value) || value < group.valueMin || value > group.valueMax) continue;
          const bin = Math.max(0, Math.min(binCount - 1, Math.floor((value - distribution.min) / width)));
          const frequency = frequencies.get(bin) || 0;
          if (frequency < group.countMin || frequency > group.countMax) continue;
          n++;
          const delta = value - mean;
          mean += delta / n;
          m2 += delta * (value - mean);
        }
      }
      return {key: key, n: n, mean: n ? mean : null, sigma: n ? Math.sqrt(Math.max(0, m2 / n)) : null};
    });
  });
}

self.onmessage = function (event) {
  const message = event.data;
  try {
    if (message.type === "reset") reset();
    if (message.type === "append") ingest(message.buffer);
    if (message.type === "view") {
      self.postMessage({id: message.id, result: render(message)});
    } else if (message.type === "statistics") {
      self.postMessage({id: message.id, result: selectionStatistics(message)});
    } else self.postMessage({id: message.id, ok: true});
  } catch (error) {
    self.postMessage({id: message.id, error: error.message});
  }
};
