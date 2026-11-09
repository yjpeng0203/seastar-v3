"""Read saved analysis objects and rebin scalar metrics without reanalysing waveforms."""
import csv
from array import array
import hashlib
import json
import math
from pathlib import Path

# Common, explicit focus windows. Values outside each window remain counted and
# are retained in the full-range spectra and original scalar TTree.
FOCUS = {
    "baseline": (100, 400), "noise": (0, 20), "amplitude": (0, 500),
    "area": (-100, 5000), "tail_negative": (0, 2000),
    "peak_ns": (0, 150), "cfd_ns": (0, 150), "threshold_ns": (0, 150),
    "fwhm_ns": (0, 80), "rise_ns": (0, 40), "fall_ns": (0, 100),
    "wave_dt_ns": (-10, 600), "pulses": (-.5, 16.5),
    "window_ns": (0, 250), "snr": (0, 200),
}


def unit_bin_count(low, high):
    width = high-low
    if not math.isfinite(width) or width <= 0 or not float(width).is_integer():
        raise ValueError("横轴跨度须为正整数，才能保持每 1 个数值单位一个 bin")
    return int(width)


# Fixed native bins precede display-unit conversion.
FOCUS = {name:(low,high,unit_bin_count(low,high)) for name,(low,high) in FOCUS.items()}


def converted_binning(native_spec, scale):
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("单位换算系数必须为正有限数")
    low, high, bins = native_spec
    return low*scale, high*scale, bins


def reusable_raw_cache(analysis, specs):
    """Reuse a matching native-bin cache from an earlier figure revision."""
    import ROOT as R
    for path in sorted(analysis.parent.glob("figures_*/analysis.json"),reverse=True):
        record=json.loads(path.read_text())
        if record.get("source_analysis")!=str(analysis.resolve()) or not record.get("figure_cache_key"):continue
        cache=Path(__file__).resolve().parents[1]/"runtime/figure-cache"/record["figure_cache_key"]
        if not (cache/"complete.json").is_file():continue
        file=R.TFile.Open(str(cache/"focused.root"),"READ")
        if not file or file.IsZombie():continue
        binning=file.Get("binning")
        ranges=json.loads(binning.GetTitle()) if binning else {}
        matches=all(ranges.get(name)==list(spec) for name,spec in specs.items())
        file.Close()
        if matches:return cache
    return None

REBIN_CPP = r"""
#include <TTreeReader.h>
#include <TTreeReaderValue.h>
#include <memory>
#include <cmath>
namespace seastar_figure {
std::vector<Long64_t> rebin(TTree* tree, const std::vector<std::string>& names,
                           const std::vector<double>& low, const std::vector<double>& high,
                           const std::vector<int>& bins) {
    const int nm=names.size();
    std::vector<size_t> offsets(nm+1);
    for(int m=0;m<nm;++m) offsets[m+1]=offsets[m]+2*8*(bins[m]+3);
    std::vector<Long64_t> out(offsets.back());
    TTreeReader reader(tree);
    TTreeReaderValue<int> channel(reader,"channel"), good(reader,"good"), pulses(reader,"pulses");
    std::vector<std::unique_ptr<TTreeReaderValue<double>>> fields;
    for(auto name:names) fields.emplace_back(name=="pulses"?nullptr:new TTreeReaderValue<double>(reader,name.c_str()));
    Long64_t processed=0;
    while(reader.Next()) {
        ++processed; int ch=*channel;
        if(ch<0 || ch>=8) throw std::runtime_error("Unexpected channel in metric TTree");
        for(int m=0;m<nm;++m) {
            double value=fields[m]?**fields[m]:*pulses;
            int bin;
            if(!std::isfinite(value)) bin=bins[m]+2;
            else if(value<low[m]) bin=bins[m];
            else if(value>high[m]) bin=bins[m]+1;
            else bin=std::min(bins[m]-1,int((value-low[m])*bins[m]/(high[m]-low[m])));
            for(int pop=0;pop<=int(*good!=0);++pop) ++out[offsets[m]+(pop*8+ch)*(bins[m]+3)+bin];
        }
    }
    if(processed!=tree->GetEntries()) throw std::runtime_error("Incomplete scalar rebin pass");
    return out;
}
}
"""

PAIR_CPP = r"""
#include <map>
namespace seastar_pair_bins {
std::vector<double> rebin(TTree *tree,int limit) {
    int bins=2*limit,stride=bins+4;
    std::vector<double> out(64*stride);std::map<int,double> first;
    TTreeReader r(tree);TTreeReaderValue<Long64_t> event(r,"event");
    TTreeReaderValue<int> ch(r,"channel"),good(r,"good");TTreeReaderValue<double> cfd(r,"cfd_ns");
    Long64_t last=-1;
    auto flush=[&](){for(auto a=first.begin();a!=first.end();++a)for(auto b=std::next(a);b!=first.end();++b) {
        if(!std::isfinite(a->second)||!std::isfinite(b->second))continue;
        double v=b->second-a->second;int offset=(a->first*8+b->first)*stride;
        int bin=std::max(0,std::min(bins-1,int(v+limit)));
        ++out[offset+bin];++out[offset+bins];out[offset+bins+1]+=v;out[offset+bins+2]+=v*v;
    }};
    Long64_t count=0;
    while(r.Next()) {++count;if(*event!=last){flush();first.clear();last=*event;}first.emplace(*ch,*good?*cfd:NAN);}
    flush();if(count!=tree->GetEntries())throw std::runtime_error("Incomplete within-board pair pass");return out;
}
}
"""


def export_plot_data(analysis, target, include_readback=True):
    """Write focused ROOT histograms and exact count tables, with a reusable cache."""
    import ROOT as R
    import shutil
    analysis, target = Path(analysis), Path(target)
    metadata = json.loads((analysis / "analysis.json").read_text())
    source = analysis / "root" / "analysis.root"
    with (analysis/"tables/channel_statistics.csv").open() as stream:
        statistics = {(row["metric"],row["population"],int(row["channel"])):row for row in csv.DictReader(stream)}
    specs=dict(FOCUS)
    for name in FOCUS:
        rows=[row for (metric,pop,ch),row in statistics.items() if metric==name and pop=="all" and int(row["n"])>0]
        if rows:
            low=math.floor(min(float(row["min"]) for row in rows))
            high=max(low+1,math.ceil(max(float(row["max"]) for row in rows)))
            if name=="pulses":low,high=-.5,16.5
            specs["full_"+name]=(low,high,int(high-low))
    native_specs=dict(specs)
    charge_scale=metadata["config"]["adc_mv"]*metadata["config"]["sample_ns"]/metadata["config"]["resistance"]
    conversions={"charge_pc":("area",charge_scale),"tail_negative_pc":("tail_negative",charge_scale),
                 "amplitude_mv":("amplitude",metadata["config"]["adc_mv"]),
                 "baseline_mv":("baseline",metadata["config"]["adc_mv"]),
                 "noise_mv":("noise",metadata["config"]["adc_mv"])}
    for destination,(field,scale) in conversions.items():
        for prefix in ("","full_"):specs[prefix+destination]=converted_binning(native_specs[prefix+field],scale)
    key = hashlib.sha256((str(source.resolve()) + str(source.stat().st_size) + str(source.stat().st_mtime_ns)
                          + json.dumps(specs) + "native-bin-then-convert-v1" + REBIN_CPP).encode()).hexdigest()
    cache = Path(__file__).resolve().parents[1] / "runtime" / "figure-cache" / key
    for sub in ("tables", "root", "qa", "png", "pdf", "svg"): (target/sub).mkdir(parents=True, exist_ok=True)
    if not (cache/"complete.json").exists():
        previous=reusable_raw_cache(analysis,native_specs)
        if previous:
            cache.mkdir(parents=True,exist_ok=True)
            for name in ("focused.root","focused_bin_counts.csv","focus_coverage.csv","complete.json","pairs_complete.json","count_axes_complete.json"):
                if (previous/name).is_file():shutil.copy2(previous/name,cache/name)
    if not (cache/"complete.json").exists():
        cache.mkdir(parents=True, exist_ok=True)
        root = R.TFile.Open(str(source), "READ")
        if not root or root.IsZombie(): raise ValueError("Cannot open saved analysis ROOT")
        tree = root.Get("metrics")
        if not tree or tree.GetEntries() != metadata["waves"]: raise ValueError("Metric count does not match metadata")
        if not hasattr(R, "seastar_figure") and not R.gInterpreter.Declare(REBIN_CPP):
            raise RuntimeError("Cannot initialize scalar rebinning")
        names = list(native_specs)
        fields=[name[5:] if name.startswith("full_") else name for name in names]
        result = R.seastar_figure.rebin(tree, fields, [native_specs[k][0] for k in names],
                                       [native_specs[k][1] for k in names], [native_specs[k][2] for k in names])
        out = R.TFile.Open(str(cache/"focused.root"), "RECREATE")
        if not out or out.IsZombie(): raise ValueError("Cannot create focused ROOT file")
        R.TH1.AddDirectory(False)
        with (cache/"focused_bin_counts.csv").open("w",newline="") as bins_file, (cache/"focus_coverage.csv").open("w",newline="") as coverage_file:
            bins_writer=csv.writer(bins_file);bins_writer.writerow(("metric","population","channel","bin_low","bin_high","count"))
            coverage_writer=csv.writer(coverage_file);coverage_writer.writerow(("metric","population","channel","shown","below","above","missing"))
            offset=0
            for m,name in enumerate(names):
                low,high,bins=native_specs[name];width=(high-low)/bins
                assert width==1
                for pop,label in enumerate(("all","accepted")):
                    for ch in range(8):
                        start=offset+(pop*8+ch)*(bins+3)
                        counts=list(result[start:start+bins+3])
                        field=name[5:] if name.startswith("full_") else name
                        expected=statistics.get((field,"all" if pop==0 else "good",ch))
                        if expected:
                            assert sum(counts[:bins+2])==int(expected["n"])
                            assert counts[bins+2]==int(expected["missing"])
                        hist_name=("full_" if name.startswith("full_") else "")+label+"_"+field+f"_ch{ch}"
                        hist=R.TH1D(hist_name,"",bins,low,high)
                        for i,count in enumerate([counts[bins]]+counts[:bins]+[counts[bins+1]]):hist.SetBinContent(i,count)
                        hist.SetEntries(sum(counts[:bins+2]));hist.Write()
                        # Sparse CSV: unlisted bins have exactly zero count. ROOT retains every bin.
                        for i,count in enumerate(counts[:bins]):
                            if count:bins_writer.writerow((name,label,ch,low+i*width,low+(i+1)*width,count))
                        coverage_writer.writerow((name,label,ch,sum(counts[:bins]),counts[bins],counts[bins+1],counts[bins+2]))
                offset+=2*8*(bins+3)
            R.TNamed("binning",json.dumps(specs)).Write()
        out.Close();root.Close()
        (cache/"complete.json").write_text(json.dumps({"cache_key":key,"waveforms":metadata["waves"]}))
    if not (cache/"pairs_complete.json").is_file():
        root=R.TFile.Open(str(source),"READ")
        if not hasattr(R,"seastar_pair_bins") and not R.gInterpreter.Declare(PAIR_CPP):raise RuntimeError("Cannot prepare within-board pair bins")
        rows=[row for (metric,pop,ch),row in statistics.items() if metric=="cfd_ns" and pop=="all" and int(row["n"])>0]
        limit=max(1,math.ceil(max(float(row["max"]) for row in rows)-min(float(row["min"]) for row in rows))) if rows else 1
        result=R.seastar_pair_bins.rebin(root.Get("metrics"),limit);bins=2*limit
        out=R.TFile.Open(str(cache/"focused.root"),"UPDATE")
        for a in range(8):
            for b in range(a+1,8):
                start=(a*8+b)*(bins+4);n=result[start+bins]
                original=root.Get(f"pair_{a}_{b}")
                assert int(n)==(int(original.GetEntries()) if original else 0)
                hist=R.TH1D(f"unit_pair_{a}_{b}","",bins,-limit,limit);hist.SetDirectory(0)
                for i in range(bins):hist.SetBinContent(i+1,result[start+i])
                hist.SetEntries(n);hist.PutStats(array('d',[n,n,result[start+bins+1],result[start+bins+2]]));hist.Write()
        out.Close();root.Close();(cache/"pairs_complete.json").write_text(json.dumps({"bin_width_ns":1,"scope":"same event within one board"}))
    if not (cache/"count_axes_original_v2.json").is_file():
        original=R.TFile.Open(str(source),"READ");out=R.TFile.Open(str(cache/"focused.root"),"UPDATE")
        for name in ("multiplicity","trigger_interval"):
            hist=original.Get(name).Clone("unit_"+name);hist.SetDirectory(0)
            # Time-interval distributions already have native log-space bins.
            # Preserve them exactly; changing the display is not a reason to merge bins.
            hist.Write(hist.GetName(),R.TObject.kOverwrite)
        out.Close();original.Close();(cache/"count_axes_original_v2.json").write_text('{"binning":"unchanged from source analysis"}')
    if not (cache/"conversions_complete.json").is_file():
        output=R.TFile.Open(str(cache/"focused.root"),"UPDATE")
        for destination,(field,scale) in conversions.items():
            for prefix in ("","full_"):
                low,high,bins=specs[prefix+destination]
                for population in ("all","accepted"):
                    for ch in range(8):
                        raw=output.Get(f"{prefix}{population}_{field}_ch{ch}")
                        converted=raw.Clone(f"{prefix}{population}_{destination}_ch{ch}");converted.SetDirectory(0)
                        moments=array('d',[0.]*4);raw.GetStats(moments)
                        # Only change the axis calibration. Never call Rebin or SetBins here.
                        converted.GetXaxis().SetLimits(low,high)
                        converted.PutStats(array('d',[moments[0],moments[1],moments[2]*scale,moments[3]*scale*scale]))
                        assert converted.GetNbinsX()==raw.GetNbinsX()==bins
                        assert converted.GetEntries()==raw.GetEntries()
                        assert converted.Integral(0,bins+1)==raw.Integral(0,bins+1)
                        converted.Write(converted.GetName(),R.TObject.kOverwrite)
        R.TNamed("binning",json.dumps(specs)).Write("binning",R.TObject.kOverwrite)
        R.TNamed("unit_conversion","Native ADC bins are fixed; axis calibration changes only coordinates, never counts.").Write("unit_conversion",R.TObject.kOverwrite)
        output.Close()
        for filename in ("focused_bin_counts.csv","focus_coverage.csv"):
            temporary=cache/(filename+".tmp")
            with (cache/filename).open(newline="") as source_csv, temporary.open("w",newline="") as dest_csv:
                reader=csv.DictReader(source_csv);writer=csv.DictWriter(dest_csv,fieldnames=reader.fieldnames);writer.writeheader()
                for row in reader:
                    name=row["metric"]
                    if name not in native_specs:continue
                    writer.writerow(row)
                    prefix="full_" if name.startswith("full_") else "";field=name[5:] if prefix else name
                    for destination,(native,scale) in conversions.items():
                        if field!=native:continue
                        calibrated=dict(row,metric=prefix+destination)
                        if "bin_low" in row:
                            calibrated["bin_low"]=float(row["bin_low"])*scale
                            calibrated["bin_high"]=float(row["bin_high"])*scale
                        writer.writerow(calibrated)
            temporary.replace(cache/filename)
        (cache/"conversions_complete.json").write_text(json.dumps(conversions))
        (cache/"complete.json").write_text(json.dumps({"cache_key":key,"waveforms":metadata["waves"],"binning_basis":"native values before conversion"}))
    shutil.copy2(cache/"focused.root",target/"root/focused.root")
    for name in ("focused_bin_counts.csv","focus_coverage.csv"):
        shutil.copy2(cache/name,target/"tables"/name)
    (target/"tables/binning.json").write_text(json.dumps({"native_adc_bin_width":1,"native_integral_bin_width":1,"binning_basis":"native ADC/integral values before conversion",
        "interval_log_binning":"unchanged from source analysis",
        "conversion_preserves_bin_counts":True,"unit_conversions":conversions,
        "display_bin_widths":{name:(high-low)/bins for name,(low,high,bins) in specs.items()},
        "zero_csv_bins":"implicit zeros","ranges":specs},indent=2))
    for name in ("channel_statistics.csv","quality.csv","trends.csv","trigger_rates.csv","slow_control_statistics.csv"):
        if analysis!=target and (analysis/"tables"/name).is_file(): shutil.copy2(analysis/"tables"/name,target/"tables"/name)
    metadata["source_analysis"]=str(analysis.resolve())
    metadata["figure_cache_key"]=key
    metadata["binning_basis"]="native values before display-unit conversion"
    metadata["histogram_unit_conversions"]=conversions
    (target/"analysis.json").write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
    # Build readback objects only when the source analysis did not already store them.
    original=R.TFile.Open(str(source),"READ")
    has_readback=bool(original.Get("fpga_temperature_c_trend"));original.Close()
    if include_readback and not has_readback and metadata.get("source_csv"):
        readbacks=R.TFile.Open(str(target/"root/readback.root"),"RECREATE")
        records=[]
        for path in metadata["source_csv"]:
            with open(path,newline="",encoding="utf-8-sig") as stream:records.extend(csv.DictReader(stream))
        records.sort(key=lambda row:float(row["timestamp_ms"]))
        stats=[]
        fields=[prefix+str(ch) for prefix in ("baseline_ch","hit_rate_ch") for ch in range(8)]
        fields += ["fpga_temperature_c","optical_module_reset_count","clock_chip_reset_count"]
        for field in fields:
            values=[]
            for row in records:
                try:value=float(row.get(field,""))
                except ValueError:continue
                if math.isfinite(value):values.append((float(row["timestamp_ms"])/1000,value))
            if not values:continue
            raw=[v for _,v in values];mean=sum(raw)/len(raw)
            stats.append(dict(field=field,n=len(raw),mean=mean,sigma=math.sqrt(sum((v-mean)**2 for v in raw)/len(raw)),min=min(raw),max=max(raw)))
            envelope=[];step=max(1,math.ceil(len(values)/500))
            for start in range(0,len(values),step):
                bucket=values[start:start+step];envelope.extend(sorted({min(bucket,key=lambda p:p[1]),max(bucket,key=lambda p:p[1])}))
            graph=R.TGraph(len(envelope),array('d',[p[0] for p in envelope]),array('d',[p[1] for p in envelope]));graph.SetName(field+"_trend");graph.Write()
        readbacks.Close()
        with (target/"tables/slow_control_statistics.csv").open("w",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=("field","n","mean","sigma","min","max"));writer.writeheader();writer.writerows(stats)
    return metadata
