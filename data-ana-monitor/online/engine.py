"""Bounded live aggregates; individual metrics are persisted separately by the worker."""
from collections import Counter, deque
from dataclasses import asdict
import math

import numpy as np

from analysis import Distribution, METRICS, VERSION


def channel_order(key):
    return tuple(int(value) for value in key.split(":"))


class Trend:
    # [all waves, good waves, baseline sum, noise sum, good area sum,
    #  good amplitude sum, good CFD sum, good CFD count, triggers]
    def __init__(self, state=None):
        s = state or {}
        self.width = s.get("width", 1)
        self.rows = {int(k): np.array(v, dtype=float) for k, v in s.get("rows", {}).items()}
        self.recent = {int(k): np.array(v, dtype=float) for k, v in s.get("recent", {}).items()}

    def add(self, t, row):
        if t < 0:
            return
        while int(t / self.width) >= 600:
            merged = {}
            for k, values in self.rows.items():
                merged.setdefault(k // 2, np.zeros(9))[:] += values
            self.rows = merged
            self.width *= 2
        k = int(t / self.width)
        self.rows.setdefault(k, np.zeros(9))[:] += row
        k = int(t)
        self.recent.setdefault(k, np.zeros(9))[:] += row
        if len(self.recent) > 901:
            self.recent = {i: v for i, v in self.recent.items() if i >= k - 899}

    def state(self):
        return {"width": self.width, "rows": {k: v.tolist() for k, v in self.rows.items()},
                "recent": {k: v.tolist() for k, v in self.recent.items()}}

    def snapshot(self, end, recent=False):
        width = 1 if recent else self.width
        rows = self.recent if recent else self.rows
        start = max(0, int(end) - 299) if recent else 0
        # Empty bins inside observed acquisition time represent zero counts, not missing profiles.
        out = []
        for k in range(start, int(end / width) + 1):
            v = rows.get(k, np.zeros(9))
            exposure = max(0., min(end, (k + 1) * width) - k * width)
            def mean(index, n):
                return float(v[index] / n) if n else None
            out.append({"t": (k * width + min(end, (k + 1) * width)) / 2,
                        "width": width, "exposure": exposure,
                        "rate": float(v[0] / exposure) if exposure > 0 else None,
                        "good_rate": float(v[1] / exposure) if exposure > 0 else None,
                        "trigger_rate": float(v[8] / exposure) if exposure > 0 else None,
                        "baseline": mean(2, v[0]), "noise": mean(3, v[0]),
                        "area": mean(4, v[1]), "amplitude": mean(5, v[1]),
                        "cfd_ns": mean(6, v[7]),
                        "good_fraction": float(v[1] / v[0]) if v[0] else None})
        return out


class Channel:
    def __init__(self, key, config, state=None):
        s = state or {}
        self.key, self.config = key, config
        self.n, self.good = s.get("n", 0), s.get("good", 0)
        self.flags = Counter(s.get("flags", {}))
        self.stats = {kind: {m: Distribution(s.get("stats", {}).get(kind, {}).get(m)) for m in METRICS}
                      for kind in ("all", "good")}
        self.intervals = Distribution(s.get("intervals"))
        self.last_ns = s.get("last_ns")
        self.zero_intervals = s.get("zero_intervals", 0)
        self.negative_intervals = s.get("negative_intervals", 0)
        self.trend = Trend(s.get("trend"))
        self.examples = deque(s.get("examples", []), maxlen=8)
        self.latest = s.get("latest")
        self.length = s.get("length", 0)
        self.shape_changes = s.get("shape_changes", 0)
        self.wave_count = s.get("wave_count", 0)
        self.wave_sum = np.array(s.get("wave_sum", []), dtype=float)
        self.aligned_sum = np.array(s.get("aligned_sum", [0.] * 512), dtype=float)
        self.aligned_count = np.array(s.get("aligned_count", [0] * 512), dtype=np.int64)
        self.density = np.array(s.get("density", [[0] * 64 for _ in range(128)]), dtype=np.int64)
        self.density_count = s.get("density_count", 0)
        self.density_outside = s.get("density_outside", 0)
        self.first_baselines = s.get("first_baselines", [])
        self.recent_baselines = deque(s.get("recent_baselines", []), maxlen=128)
        self.polarities = Counter(s.get("polarities", {}))
        self.scatter = deque(s.get("scatter", []), maxlen=1000)

    def add(self, record, pulse, t, trigger_ns):
        self.n += 1
        self.good += pulse["good"]
        self.flags.update(pulse["flag_names"])
        self.polarities[str(pulse["polarity"])] += 1
        pulse = dict(pulse, wave_dt_ns=record["ns"] - trigger_ns)
        for name in METRICS:
            self.stats["all"][name].add(pulse[name])
            if pulse["good"]:
                self.stats["good"][name].add(pulse[name])
        if self.last_ns is not None:
            dt = record["ns"] - self.last_ns
            if dt > 0:
                self.intervals.add(math.log10(dt * 1e-9))
            elif dt == 0:
                self.zero_intervals += 1
            else:
                self.negative_intervals += 1
        self.last_ns = record["ns"]
        good, cfd = pulse["good"], pulse["cfd_ns"]
        self.trend.add(t, [1, good, pulse["baseline"], pulse["noise"],
                           pulse["area"] if good else 0, pulse["amplitude"] if good else 0,
                           cfd if good and cfd is not None else 0,
                           int(good and cfd is not None), 0])
        if good:
            if len(self.first_baselines) < 128:
                self.first_baselines.append(pulse["baseline"])
            self.recent_baselines.append(pulse["baseline"])
            values = np.asarray(record["samples"], dtype=float)
            if len(values) != self.length:
                self.shape_changes += bool(self.length)
                self.length = len(values)
                self.wave_sum = np.zeros(self.length)
                self.wave_count = 0
                self.density.fill(0)
                self.density_count = self.density_outside = 0
            self.wave_count += 1
            self.wave_sum += values
            low, high = max(0, pulse["peak"] - 128), min(len(values), pulse["peak"] + 384)
            dst = slice(low - pulse["peak"] + 128, high - pulse["peak"] + 128)
            self.aligned_sum[dst] += pulse["polarity"] * (values[low:high] - pulse["baseline"])
            self.aligned_count[dst] += 1
            if self.good % self.config.display_stride == 0:
                self.scatter.append([pulse["amplitude"], pulse["area"], t])
                ix = np.minimum(127, np.arange(len(values)) * 128 // len(values))
                iy = (values * 64 / (self.config.adc_max + 1)).astype(int)
                mask = (iy >= 0) & (iy < 64)
                np.add.at(self.density, (ix[mask], iy[mask]), 1)
                self.density_outside += int((~mask).sum())
                self.density_count += 1
        if self.n % self.config.display_stride == 1 or self.config.display_stride == 1:
            example = dict(pulse, t=t, samples=record["samples"].tolist(), wave_id=record["wave_id"])
            self.latest = example
            self.examples.append(example)
        return pulse

    def state(self):
        return {"n": self.n, "good": self.good, "flags": dict(self.flags),
                "stats": {k: {m: d.state() for m, d in v.items()} for k, v in self.stats.items()},
                "intervals": self.intervals.state(), "last_ns": self.last_ns,
                "zero_intervals": self.zero_intervals, "negative_intervals": self.negative_intervals,
                "trend": self.trend.state(), "examples": list(self.examples), "latest": self.latest,
                "length": self.length, "shape_changes": self.shape_changes, "wave_count": self.wave_count,
                "wave_sum": self.wave_sum.tolist(), "aligned_sum": self.aligned_sum.tolist(),
                "aligned_count": self.aligned_count.tolist(), "density": self.density.tolist(),
                "density_count": self.density_count, "density_outside": self.density_outside,
                "first_baselines": self.first_baselines, "recent_baselines": list(self.recent_baselines),
                "polarities": dict(self.polarities), "scatter": list(self.scatter)}

    def snapshot(self, end):
        ys = np.divide(self.aligned_sum, self.aligned_count, out=np.zeros(512), where=self.aligned_count > 0)
        density = [[int(i), int(j), int(self.density[i, j])] for i, j in zip(*np.nonzero(self.density))]
        drift = (float(np.mean(self.recent_baselines) - np.mean(self.first_baselines))
                 if self.first_baselines and self.recent_baselines else None)
        return {"key": self.key, "n": self.n, "good": self.good, "flags": dict(self.flags),
                "stats": {k: {m: d.snapshot() for m, d in v.items()} for k, v in self.stats.items()},
                "intervals": self.intervals.snapshot(), "zero_intervals": self.zero_intervals,
                "negative_intervals": self.negative_intervals, "drift_adc": drift,
                "trend": self.trend.snapshot(end), "recent": self.trend.snapshot(end, recent=True),
                "examples": list(self.examples), "latest": self.latest, "length": self.length,
                "shape_changes": self.shape_changes, "wave_count": self.wave_count,
                "average": (self.wave_sum / self.wave_count).tolist() if self.wave_count else [],
                "aligned": [[(i - 128) * self.config.sample_ns, float(ys[i])]
                            for i in range(512) if self.aligned_count[i]],
                "density": density, "density_count": self.density_count,
                "density_outside": self.density_outside, "polarities": dict(self.polarities),
                "scatter": list(self.scatter)}


class Engine:
    def __init__(self, config, analyzer, state=None):
        s = state or {}
        self.config, self.analyzer = config, analyzer
        self.channels = {k: Channel(k, config, v) for k, v in s.get("channels", {}).items()}
        self.triggers, self.waves = s.get("triggers", 0), s.get("waves", 0)
        self.wave_events = s.get("wave_events", 0)
        self.last_header_ns = s.get("last_header_ns")
        self.header_backsteps = s.get("header_backsteps", 0)
        self.seq = s.get("seq", 0)
        self.origin_ns, self.last_ns = s.get("origin_ns"), s.get("last_ns")
        self.segment = s.get("segment", 0)
        self.completed_ns = s.get("completed_ns", 0)
        self.clock_backsteps = s.get("clock_backsteps", 0)
        self.number_gaps = s.get("number_gaps", 0)
        self.last_number = s.get("last_number")
        self.pending = s.get("pending")
        self.multiplicity = Counter({int(k): v for k, v in s.get("multiplicity", {}).items()})
        self.pairs = Counter(s.get("pairs", {}))
        self.event_hits = Counter(s.get("event_hits", {}))
        self.pair_dt = {k: Distribution(v) for k, v in s.get("pair_dt", {}).items()}
        self.trigger_intervals = Distribution(s.get("trigger_intervals"))
        self.trigger_trend = Trend(s.get("trigger_trend"))

    def _finish_event(self):
        if not self.pending:
            return
        self.multiplicity[self.pending["n"]] += 1
        channels = sorted(self.pending["channels"], key=channel_order)
        self.event_hits.update(channels)
        for i, a in enumerate(channels):
            for b in channels[i + 1:]:
                key = a + "/" + b
                self.pairs[key] += 1
                ta, tb = self.pending["channels"][a], self.pending["channels"][b]
                if ta is not None and tb is not None:
                    self.pair_dt.setdefault(key, Distribution()).add(tb - ta)

    def consume(self, record, pulse=None):
        self.seq += 1
        if record["type"] == "trigger":
            self._finish_event()
            now = record["ns"]
            if self.last_header_ns is not None and now < self.last_header_ns:
                self.header_backsteps += 1
            self.last_header_ns = now
            if self.last_number is not None and record["number"] != (self.last_number + 1) % 65536:
                self.number_gaps += 1
            self.last_number = record["number"]
            self.triggers += 1
            self.pending = {"ns": now, "number": record["number"], "n": 0, "channels": {}}
            return None
        if self.pending is None:
            raise ValueError("波形前没有触发记录")
        if self.pending["n"] == 0:
            # Empty-trigger timestamps are not assumed to share the waveform-event clock.
            # In the supplied DAT they step backwards 175 times; ROOT omits these events.
            now = self.pending["ns"]
            if self.origin_ns is None:
                self.origin_ns = now
            if self.last_ns is not None:
                dt = now - self.last_ns
                if dt < 0:
                    self.clock_backsteps += 1
                    self.completed_ns += self.last_ns - self.origin_ns
                    self.segment += 1
                    self.origin_ns = now
                    self.trigger_trend = Trend()
                    for channel in self.channels.values():
                        channel.trend = Trend()
                        channel.last_ns = None
                elif dt > 0:
                    self.trigger_intervals.add(math.log10(dt * 1e-9))
            self.last_ns = now
            self.wave_events += 1
            self.trigger_trend.add((now - self.origin_ns) * 1e-9, [0] * 8 + [1])
        self.waves += 1
        self.pending["n"] += 1
        key = str(record["board"]) + ":" + str(record["channel"])
        if key not in self.channels:
            self.channels[key] = Channel(key, self.config)
        channel = self.channels[key]
        t = (self.pending["ns"] - self.origin_ns) * 1e-9
        pulse = channel.add(record, self.analyzer(record["samples"]) if pulse is None else pulse, t, self.pending["ns"])
        # Multiple waveforms from the same channel are not independent channel coincidences.
        self.pending["channels"].setdefault(key, pulse["cfd_ns"] if pulse["good"] else None)
        return dict(pulse, seq=self.seq, position=record["position"], board=record["board"],
                    channel=record["channel"], wave_id=record["wave_id"], raw_ns=record["ns"],
                    trigger_number=self.pending["number"], t=t, segment=self.segment,
                    points=len(record["samples"]))

    def state(self):
        return {"version": VERSION, "config": asdict(self.config),
                "channels": {k: c.state() for k, c in self.channels.items()},
                "triggers": self.triggers, "waves": self.waves, "seq": self.seq,
                "wave_events": self.wave_events, "last_header_ns": self.last_header_ns,
                "header_backsteps": self.header_backsteps,
                "origin_ns": self.origin_ns, "last_ns": self.last_ns, "segment": self.segment,
                "completed_ns": self.completed_ns, "clock_backsteps": self.clock_backsteps,
                "last_number": self.last_number, "number_gaps": self.number_gaps,
                "pending": self.pending, "multiplicity": dict(self.multiplicity),
                "pairs": dict(self.pairs), "event_hits": dict(self.event_hits),
                "pair_dt": {k: d.state() for k, d in self.pair_dt.items()},
                "trigger_intervals": self.trigger_intervals.state(), "trigger_trend": self.trigger_trend.state()}

    def snapshot(self):
        end = (self.last_ns - self.origin_ns) * 1e-9 if self.last_ns is not None else 0.
        multi, pairs = self.multiplicity.copy(), self.pairs.copy()
        hits = self.event_hits.copy()
        pair_dt = {k: d.snapshot() for k, d in self.pair_dt.items()}
        if self.pending:
            multi[self.pending["n"]] += 1
            names = sorted(self.pending["channels"], key=channel_order)
            hits.update(names)
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    key = a + "/" + b
                    pairs[key] += 1
                    ta, tb = self.pending["channels"][a], self.pending["channels"][b]
                    if ta is not None and tb is not None:
                        temporary = Distribution(self.pair_dt[key].state() if key in self.pair_dt else None)
                        temporary.add(tb - ta)
                        pair_dt[key] = temporary.snapshot()
        return {"version": VERSION, "config": asdict(self.config), "triggers": self.triggers,
                "seq": self.seq,
                "wave_events": self.wave_events, "header_backsteps": self.header_backsteps,
                "waves": self.waves, "good": sum(c.good for c in self.channels.values()),
                "origin_ns": self.origin_ns, "last_ns": self.last_ns, "relative_s": end,
                "observed_s": self.completed_ns * 1e-9 + end, "segment": self.segment,
                "clock_backsteps": self.clock_backsteps, "number_gaps": self.number_gaps,
                "multiplicity": dict(multi), "pairs": dict(pairs), "event_hits": dict(hits),
                "pair_dt": pair_dt,
                "pending_event": bool(self.pending), "trigger_intervals": self.trigger_intervals.snapshot(),
                "trigger_trend": self.trigger_trend.snapshot(end),
                "trigger_recent": self.trigger_trend.snapshot(end, recent=True),
                "channels": [self.channels[k].snapshot(end) for k in sorted(self.channels, key=channel_order)]}
