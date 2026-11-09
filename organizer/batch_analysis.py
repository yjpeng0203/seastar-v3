"""Batch ROOT analysis and classified scientific outputs under each string's results/."""
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
ONLINE = PROJECT / "data-ana-monitor" / "online"
METRICS = ("baseline", "noise", "amplitude", "area", "charge_pc", "tail_negative", "peak_ns",
           "cfd_ns", "threshold_ns", "fwhm_ns", "rise_ns", "fall_ns", "wave_dt_ns", "pulses", "window_ns", "snr")
FLAG_LABELS = ("low SNR", "short baseline", "baseline drift", "edge", "clipped", "pileup",
               "missing CFD", "secondary", "ADC range", "fallback baseline", "tail clipped")
CONFIG_FIELDS = ("sample_ns", "snr", "end_sigma", "end_fraction", "cfd_fraction", "threshold_adc",
                 "timing_snr", "adc_max", "polarity", "min_baseline", "return_run", "padding",
                 "adc_mv", "resistance", "display_stride")


def load_analysis_config(path=None):
    # Import the owning validator without importing the monitor or creating its state.
    import importlib.util
    from dataclasses import asdict
    spec = importlib.util.spec_from_file_location("seastar_batch_config", ONLINE / "analysis.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    values = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    return asdict(module.Config(**values))


def build_analyzer():
    compiler, root_config, rootcling = (shutil.which(name) for name in ("c++", "root-config", "rootcling"))
    if not all((compiler, root_config, rootcling)):
        raise RuntimeError("批次分析需要 ROOT 开发环境（root-config/rootcling）及 C++ 编译器")
    source = Path(__file__).with_name("batch_analyze.cpp")
    flags = subprocess.check_output([root_config, "--cflags", "--libs"], text=True).strip()
    digest = hashlib.sha256(source.read_bytes() + (ONLINE / "adaptive_waveform.h").read_bytes() + flags.encode()).hexdigest()
    build = PROJECT / "runtime" / "batch-analysis" / "build" / digest[:16]
    executable = build / "seastar-analyze"
    if not executable.exists():
        build.mkdir(parents=True, exist_ok=True)
        linkdef = build / "LinkDef.h"
        linkdef.write_text('#include <vector>\n#ifdef __CLING__\n#pragma link C++ class std::vector<std::vector<unsigned short>>+;\n#endif\n')
        dictionary = build / "dictionary.cxx"
        subprocess.run([rootcling, "-f", str(dictionary), str(linkdef)], check=True, cwd=build)
        temporary = executable.with_suffix(".tmp")
        subprocess.run([compiler, "-O3", str(source), str(dictionary), "-o", str(temporary)] + shlex.split(flags), check=True, cwd=build)
        temporary.replace(executable)
    return executable, digest


def discover_inputs(dataset, groups=None):
    dataset = Path(dataset).expanduser().resolve()
    if not dataset.is_dir():
        raise ValueError("批次目录不存在: " + str(dataset))
    selected = set(groups or [])
    available = {p.name: p for p in dataset.iterdir() if p.is_dir() and re.fullmatch(r"G\d+", p.name)}
    if selected - set(available):
        raise ValueError("未找到串: " + ", ".join(sorted(selected - set(available))))
    tasks = []
    for group, directory in sorted(available.items()):
        if selected and group not in selected:
            continue
        for source in sorted((directory / "rec_data").glob("data_fee_*.root")):
            match = re.fullmatch(r"data_fee_(\d+)_.+\.root", source.name)
            if match:
                # Read the actual FEE number, independent of the Gx label (spare-board mapping).
                csv_sources = [p for p in sorted((directory / "csv").glob("*.csv"))
                               if re.fullmatch(r"readback_fee_" + match[1] + r"(?:_.*)?\.csv", p.name)]
                exact = directory / "csv" / (source.stem.replace("data_fee_", "readback_fee_", 1) + ".csv")
                if exact in csv_sources:
                    csv_sources = [exact]
                elif len(csv_sources) > 1:
                    raise ValueError("多个 CSV 无法确定与 ROOT 的对应关系: " + str(source))
                tasks.append((group, int(match[1]), source, csv_sources))
    if not tasks:
        raise ValueError("所选串的 rec_data/ 下没有可分析的 data_fee_*.root；请先完成 decode")
    return tasks


def write_metadata(path, metadata):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def analyze_dataset(dataset, groups=None, config_path=None, max_events=0):
    if max_events < 0:
        raise ValueError("max-events 不能为负数")
    tasks = discover_inputs(dataset, groups)
    config = load_analysis_config(config_path)
    executable, backend = build_analyzer()
    results = []
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    for group, board, source, csv_sources in tasks:
        label = "analysis_" + stamp + ("_sample" if max_events else "")
        results_root = source.parent.parent / "results"
        output = results_root / label
        suffix = 1
        while output.exists():
            output = results_root / (label + "_" + str(suffix)); suffix += 1
        for directory in ("png", "pdf", "root", "tables"):
            (output / directory).mkdir(parents=True, exist_ok=True)
        inputs = [source] + csv_sources
        stats = {str(p): {"size": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns} for p in inputs}
        metadata = {"schema_version": "seastar-analysis-v1", "status": "running", "group": group, "board": board,
                    "source_root": str(source), "source_csv": [str(p) for p in csv_sources], "input_stats": stats,
                    "config": config, "backend_sha256": backend,
                    "kernel_sha256": hashlib.sha256((ONLINE / "adaptive_waveform.h").read_bytes()).hexdigest(),
                    "max_events": max_events, "sampled": bool(max_events), "output_dir": str(output),
                    "created_beijing": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                    "units": {"area": "ADC.sample", "amplitude": "ADC", "charge_pc": "historical pC scale",
                              "timing": "ns relative to waveform sample zero", "t": "relative electronic trigger time, segmented"},
                    "notes": ["ROOT input omits original empty triggers; raw trigger total is unknown.",
                              "All/good populations are separate; missing timing is NaN, never zero.",
                              "Histogram ranges may merge bins when extended; exact moments are in tables and raw metric values in ROOT.",
                              "Density is sampled; averages, metrics, counts and scalar spectra use every applicable waveform.",
                              "Slow-control timestamps are Beijing time; no cross-clock absolute alignment is assumed."]}
        write_metadata(output / "analysis.json", metadata)
        config_file = output / "analysis-config.txt"
        config_file.write_text(" ".join(str(config[k]) for k in CONFIG_FIELDS) + "\n")
        print("[analyze] " + group + " " + source.name + " -> " + str(output), flush=True)
        process = None
        try:
            cmd = [str(executable), str(source), str(output / "root" / "analysis.root"), str(output / "tables"),
                   str(config_file), str(board), str(max_events), str(output / "counts.json")]
            with (output / "analysis.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in process.stdout:
                    log.write(line); log.flush(); print("[" + group + "] " + line.rstrip(), flush=True)
                process.stdout.close()
                if process.wait():
                    raise RuntimeError("C++ 分析失败，详见 " + str(output / "analysis.log"))
                metadata.update(json.loads((output / "counts.json").read_text()))
                import ROOT
                root_output=ROOT.TFile.Open(str(output/"root/analysis.root"),"UPDATE")
                if not root_output or root_output.IsZombie():raise RuntimeError("Cannot save analysis provenance")
                ROOT.TNamed("analysis_metadata",json.dumps(metadata,ensure_ascii=False)).Write("analysis_metadata",ROOT.TObject.kOverwrite)
                root_output.Close()
                write_metadata(output / "analysis.json", metadata)
                subprocess.run([sys.executable, "-B", "-m", "organizer.batch_analysis", "--render", str(output)],
                               cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT, check=True)
            changed = [str(p) for p in inputs if stats[str(p)] != {"size": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns}]
            if changed:
                raise RuntimeError("分析过程中输入发生变化: " + ", ".join(changed))
            metadata["status"] = "sample_complete" if max_events else "complete"
            metadata["finished_beijing"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
            write_metadata(output / "analysis.json", metadata)
            results.append(metadata)
            print("[analyze] " + group + " " + metadata["status"] + ": " + str(metadata["waves"]) + " waveforms", flush=True)
        except BaseException as error:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            if process is not None and process.stdout is not None:
                process.stdout.close()
            metadata["status"] = "failed"; metadata["error"] = str(error)
            write_metadata(output / "analysis.json", metadata)
            raise
    return results


def render_results(output, target=None):
    """Rebuild figures with native ROOT graphics; reuse all saved scalar metrics."""
    from .figure_data import export_plot_data
    output=Path(output).resolve();target=Path(target).resolve() if target else output
    metadata=export_plot_data(output,target)
    macro=Path(__file__).with_name("render_analysis.C")
    metadata["figure_created_beijing"]=datetime.now(timezone(timedelta(hours=8))).isoformat()
    metadata["figure_renderer_sha256"]=hashlib.sha256(macro.read_bytes()).hexdigest()
    metadata["render_status"]="running"
    write_metadata(target/"analysis.json",metadata)
    arguments=[str(output/"root/analysis.root"),str(target/"root/focused.root"),str(target),metadata["group"]]
    call=str(macro)+"("+",".join(json.dumps(value,ensure_ascii=False) for value in arguments)+","+",".join(str(metadata[key]) for key in ("events","waves","good","segments"))+","+str(metadata["config"]["display_stride"])+")"
    try:
        with (target/"render.log").open("w") as log:
            subprocess.run(["root","-l","-b","-q",call],stdout=log,stderr=subprocess.STDOUT,check=True)
        metadata["render_status"]="complete"
    except BaseException:
        metadata["render_status"]="failed";write_metadata(target/"analysis.json",metadata);raise
    write_metadata(target/"analysis.json",metadata)
    return target


def render_saved(dataset, groups=None):
    """Create a new figure revision from completed analyses, without decoding or integration."""
    dataset=Path(dataset).expanduser().resolve()
    if (dataset/"analysis.json").is_file():
        analyses=[dataset]
    else:
        analyses=[]
        for group in sorted(dataset.glob("G*")):
            if not group.is_dir() or (groups and group.name not in groups):continue
            candidates=[]
            for path in (group/"results").glob("analysis_*/analysis.json"):
                meta=json.loads(path.read_text())
                if meta.get("status")=="complete" and not meta.get("sampled"):candidates.append(path.parent)
            if candidates:analyses.append(sorted(candidates)[-1])
    if not analyses:raise ValueError("没有可复用的完整分析结果")
    stamp=datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    results=[]
    for analysis in analyses:
        target=analysis.parent/("figures_"+stamp);suffix=1
        while target.exists():target=analysis.parent/("figures_"+stamp+"_"+str(suffix));suffix+=1
        target.mkdir(parents=True)
        print("[render] "+analysis.parent.parent.name+" -> "+str(target),flush=True)
        render_results(analysis,target)
        results.append(str(target))
    return results


if __name__ == "__main__":
    if len(sys.argv)==3 and sys.argv[1]=="--render":
        render_results(Path(sys.argv[2]))
    else:
        raise SystemExit("Use python3 -m organizer analyze DATASET")
