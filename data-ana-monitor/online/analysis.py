"""Adaptive waveform kernel and bounded, mergeable online statistics."""
import ctypes
from dataclasses import dataclass
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import shutil
import subprocess

import numpy as np

VERSION = "adaptive-1.2"
FLAG_NAMES = {1: "low_snr", 2: "baseline_short", 4: "baseline_drift", 8: "edge",
              16: "clipped", 32: "pileup", 64: "timing_missing", 128: "secondary",
              256: "adc_range", 512: "baseline_fallback", 1024: "tail_clipped"}
REJECT_MASK = 1 | 2 | 8 | 16 | 32 | 256 | 512
METRICS = ("baseline", "noise", "amplitude", "area", "tail_negative", "peak_ns",
           "cfd_ns", "threshold_ns", "fwhm_ns", "rise_ns", "fall_ns", "wave_dt_ns",
           "pulses", "window_ns", "snr")


@dataclass
class Config:
    sample_ns: float = 2.0
    snr: float = 5.0
    end_sigma: float = 1.5
    end_fraction: float = 0.005
    cfd_fraction: float = 0.10
    threshold_adc: float = 4.0
    timing_snr: float = 3.0
    adc_max: int = 4095
    polarity: int = 0
    min_baseline: int = 12
    return_run: int = 3
    padding: int = 2
    adc_mv: float = 0.5
    resistance: float = 50.0
    display_stride: int = 10

    def __post_init__(self):
        bounds = {"sample_ns": (0.01, 1000), "snr": (2, 30), "end_sigma": (0.5, 10),
                  "end_fraction": (0.0001, .1), "cfd_fraction": (.01, .9),
                  "threshold_adc": (.1, 65535), "timing_snr": (1, 10), "adc_mv": (.0001, 1000),
                  "resistance": (1, 10000), "adc_max": (16, 65535),
                  "min_baseline": (6, 256), "return_run": (2, 20),
                  "padding": (0, 32), "display_stride": (1, 10000)}
        for name, (lo, hi) in bounds.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError("参数超出范围: " + name)
        for name in ("adc_max", "min_baseline", "return_run", "padding", "display_stride"):
            if not isinstance(getattr(self, name), int):
                raise ValueError("参数必须为整数: " + name)
        if isinstance(self.polarity, bool) or self.polarity not in (-1, 0, 1):
            raise ValueError("polarity 必须为 -1、0、1")


class NativeConfig(ctypes.Structure):
    _fields_ = [(k, ctypes.c_double) for k in ("sample_ns", "snr", "end_sigma", "end_fraction", "cfd_fraction", "threshold_adc", "timing_snr")] + [
        (k, ctypes.c_int) for k in ("adc_max", "polarity", "min_baseline", "return_run", "padding")]


class NativePulse(ctypes.Structure):
    _fields_ = [(k, ctypes.c_double) for k in ("baseline", "noise", "amplitude", "area", "tail_negative", "cfd_ns", "threshold_ns", "fwhm_ns", "rise_ns", "fall_ns", "peak_ns")] + [
        (k, ctypes.c_int) for k in ("start", "end", "peak", "polarity", "pulses", "baseline_points")] + [("flags", ctypes.c_uint32)]


class Analyzer:
    def __init__(self, config, build_dir):
        self.config = config
        source = Path(__file__).resolve().parent
        digest = hashlib.sha256((source / "native.cpp").read_bytes() +
                                (source / "adaptive_waveform.h").read_bytes() +
                                (platform.system() + platform.machine()).encode()).hexdigest()[:16]
        build_dir = Path(build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)
        library = build_dir / ("kernel-" + digest + ".so")
        if not library.exists():
            compiler = shutil.which("c++") or shutil.which("g++")
            if not compiler:
                raise RuntimeError("首次启动需要 C++ 编译器。树莓派/IPC: sudo apt install g++ python3-numpy")
            temporary = library.with_suffix(".tmp-" + str(os.getpid()))
            try:
                result = subprocess.run([compiler, "-std=c++11", "-O3", "-shared", "-fPIC",
                                         str(source / "native.cpp"), "-o", str(temporary)],
                                        capture_output=True, text=True, timeout=120)
                if result.returncode:
                    raise RuntimeError("分析内核编译失败: " + result.stderr[-3000:])
                temporary.replace(library)
            finally:
                temporary.unlink(missing_ok=True)
        self.library = ctypes.CDLL(str(library))
        self.function = self.library.seastar_analyze
        self.function.argtypes = [ctypes.POINTER(ctypes.c_uint16), ctypes.c_int,
                                  ctypes.POINTER(NativeConfig), ctypes.POINTER(NativePulse)]
        self.function.restype = None
        self.native_config = NativeConfig(**{name: getattr(config, name) for name, _ in NativeConfig._fields_})

    def __call__(self, samples):
        values = np.asarray(samples)
        if values.ndim != 1 or len(values) > 8192 or not np.issubdtype(values.dtype, np.integer) or np.any(values < 0) or np.any(values > 65535):
            raise ValueError("波形必须为不超过 8192 点的 uint16 数组")
        values = np.ascontiguousarray(values, dtype=np.uint16)
        pulse = NativePulse()
        self.function(values.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)), len(values),
                      ctypes.byref(self.native_config), ctypes.byref(pulse))
        out = {name: getattr(pulse, name) for name, _ in pulse._fields_}
        out = {key: (None if isinstance(v, float) and not math.isfinite(v) else v) for key, v in out.items()}
        out["good"] = not bool(pulse.flags & REJECT_MASK)
        out["flag_names"] = [name for bit, name in FLAG_NAMES.items() if pulse.flags & bit]
        out["window_ns"] = (pulse.end - pulse.start) * self.config.sample_ns
        out["snr"] = pulse.amplitude / pulse.noise if pulse.noise else 0.
        return out

    def batch(self, arrays):
        if not arrays:return []
        sizes=np.array([len(v) for v in arrays],dtype=np.int32)
        if np.any(sizes<1) or np.any(sizes>8192):raise ValueError('波形长度无效')
        values=np.concatenate(arrays)
        if not np.issubdtype(values.dtype,np.integer) or np.any(values<0) or np.any(values>65535):
            raise ValueError('波形必须为 uint16')
        values=np.ascontiguousarray(values,dtype=np.uint16)
        offsets=np.concatenate(([0],np.cumsum(sizes))).astype(np.int32)
        results=(NativePulse*len(arrays))()
        function=self.library.seastar_analyze_batch
        function.argtypes=[ctypes.POINTER(ctypes.c_uint16),ctypes.POINTER(ctypes.c_int),ctypes.c_int,
                           ctypes.POINTER(NativeConfig),ctypes.POINTER(NativePulse)]
        function(values.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)),
                 offsets.ctypes.data_as(ctypes.POINTER(ctypes.c_int)),len(arrays),ctypes.byref(self.native_config),results)
        output=[]
        for pulse in results:
            out={name:getattr(pulse,name) for name,_ in pulse._fields_}
            out={k:None if isinstance(v,float) and not math.isfinite(v) else v for k,v in out.items()}
            out['good']=not bool(pulse.flags & REJECT_MASK)
            out['flag_names']=[name for bit,name in FLAG_NAMES.items() if pulse.flags & bit]
            out['window_ns']=(pulse.end-pulse.start)*self.config.sample_ns
            out['snr']=pulse.amplitude/pulse.noise if pulse.noise else 0.
            output.append(out)
        return output


class Distribution:
    """Exact moments; dyadic histogram merges preserve counts without splitting bins."""
    def __init__(self, state=None):
        s = state or {}
        self.n = s.get("n", 0)
        self.mean = s.get("mean", 0.)
        self.m2 = s.get("m2", 0.)
        self.lo = s.get("lo")
        self.hi = s.get("hi")
        self.span = s.get("span", 1.)
        self.center = s.get("center", 0.)
        self.bins = np.array(s.get("bins", [0] * 256), dtype=np.int64)

    def add(self, value):
        if value is None or not math.isfinite(value):
            return
        if not self.n:
            self.center = value
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (value - self.mean)
        self.lo = value if self.lo is None else min(self.lo, value)
        self.hi = value if self.hi is None else max(self.hi, value)
        while not self.center - self.span <= value < self.center + self.span:
            merged = self.bins.reshape(-1, 2).sum(axis=1)
            self.bins.fill(0)
            self.bins[64:192] = merged
            self.span *= 2
        index = min(255, max(0, int((value - self.center + self.span) * 128 / self.span)))
        self.bins[index] += 1

    def state(self):
        return {"n": self.n, "mean": self.mean, "m2": self.m2, "lo": self.lo,
                "hi": self.hi, "span": self.span, "center": self.center, "bins": self.bins.tolist()}

    def snapshot(self):
        return {"n": self.n, "mean": self.mean if self.n else None,
                "sigma": math.sqrt(max(0., self.m2 / self.n)) if self.n else None,
                "min": self.lo, "max": self.hi, "bin_width": self.span / 128,
                "bin_origin": self.center - self.span,
                "hist": [[self.center + (i + .5) * self.span / 128 - self.span, int(n)]
                         for i, n in enumerate(self.bins) if n]}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
