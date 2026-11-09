"""Bounded real-waveform checks for batch products and original-data isolation."""
import csv
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from organizer.batch_analysis import analyze_dataset, discover_inputs, load_analysis_config, render_saved
from organizer.figure_data import FOCUS, unit_bin_count, converted_binning
from organizer.packager import verify_dataset


class BatchAnalysisTest(unittest.TestCase):
    def test_histogram_bins_follow_one_unit_rule(self):
        self.assertEqual(unit_bin_count(0,4000),4000)
        self.assertEqual(unit_bin_count(100,400),300)
        self.assertEqual(unit_bin_count(-.5,16.5),17)
        self.assertTrue(all(high-low==bins for low,high,bins in FOCUS.values()))
        for bounds in ((0,0),(2,1),(0,.5)):
            with self.assertRaises(ValueError):unit_bin_count(*bounds)

    def test_unit_conversion_preserves_native_bin_count(self):
        self.assertEqual(converted_binning((0,4000,4000),.02),(0,80,4000))
        self.assertEqual(converted_binning((0,4000,4000),.5),(0,2000,4000))
        self.assertEqual(converted_binning((-100,5000,5100),.02),(-2,100,5100))
        for scale in (0,-1,float('nan')):
            with self.assertRaises(ValueError):converted_binning((0,4000,4000),scale)
    def test_discovery_does_not_reconsume_results_and_matches_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for group, board in (("G3", 3), ("G4", 7)):
                for sub in ("rec_data", "csv", "results/old/root"):
                    (root/group/sub).mkdir(parents=True)
                (root/group/"rec_data"/f"data_fee_{board}_range.root").write_bytes(b"root")
                (root/group/"csv"/f"readback_fee_{board}_range.csv").write_text("timestamp_ms\n")
                (root/group/"csv"/f"readback_fee_{board}_other.csv").write_text("timestamp_ms\n")
                (root/group/"results/old/root/analysis.root").write_bytes(b"root")
            tasks = discover_inputs(root)
            self.assertEqual([item[1] for item in tasks], [3, 7])
            self.assertTrue(all(len(item[3]) == 1 and item[3][0].name.endswith("_range.csv") for item in tasks))
            self.assertEqual(len(discover_inputs(root, ["G4"])), 1)
            with self.assertRaises(ValueError): discover_inputs(root, ["G6"])
            # Analysis products must not make the acquisition audit report a broken ROOT.
            (root/"G3/rec_data/data_fee_3_range.root").unlink()
            (root/"G4/rec_data/data_fee_7_range.root").unlink()
            report = verify_dataset(root, check_sha256=False)
            self.assertTrue(report["ok"], report["errors"])
            self.assertTrue(all(not g["root_files"] for g in report["groups"]))

    def test_config_uses_live_validation(self):
        self.assertEqual(load_analysis_config()["sample_ns"], 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"bad.json"; path.write_text('{"sample_ns":0}')
            with self.assertRaises(ValueError): load_analysis_config(path)

    def test_real_waveform_outputs_and_live_kernel_agree(self):
        sample = PROJECT/"runtime/debug-20261101_1102/archives/real-waveform-functional-sample"
        if not sample.is_dir(): self.skipTest("optional real waveform fixture is absent")
        import ROOT
        import numpy as np
        sys.path.insert(0, str(PROJECT/"data-ana-monitor/online"))
        from analysis import Analyzer, Config
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); group = root/"G3"
            (group/"rec_data").mkdir(parents=True); (group/"csv").mkdir()
            original = next((sample/"G3/rec_data").glob("*.root"))
            original_csv = next((sample/"G3/csv").glob("*.csv"))
            # Copies prevent the test from ever writing inside a source dataset.
            import shutil
            shutil.copy2(original, group/"rec_data"/original.name)
            shutil.copy2(original_csv, group/"csv"/original_csv.name)
            before = [(p, p.stat().st_size, p.stat().st_mtime_ns) for p in group.rglob("*") if p.is_file()]
            result = analyze_dataset(root, max_events=80)[0]
            output = Path(result["output_dir"])
            self.assertEqual(result["status"], "sample_complete")
            self.assertEqual(result["events"], 80)
            self.assertTrue((output/"pdf/summary.pdf").is_file())
            self.assertTrue((output/"png/good_charge_pc.png").is_file())
            self.assertTrue((output/"png/good_charge_pc_logy.png").is_file())
            self.assertTrue((output/"png/good_amplitude_logy.png").is_file())
            self.assertTrue((output/"png/good_amplitude_mv_logy.png").is_file())
            for metric in ("charge_pc","amplitude","amplitude_mv"):
                for suffix in ("","_logy"):
                    self.assertTrue((output/"png"/("full_channels_"+metric+suffix+".png")).is_file())
            f = ROOT.TFile.Open(str(output/"root/analysis.root")); rows = f.Get("metrics")
            self.assertEqual(rows.GetEntries(), result["waves"])
            analyzer = Analyzer(Config(), root/"build")
            source = ROOT.TFile.Open(str(original)); tree = source.Get("seastar")
            index = 0
            for event in range(80):
                tree.GetEntry(event)
                for waveform in tree.samples:
                    expected = analyzer(np.asarray(waveform, dtype=np.uint16));rows.GetEntry(index)
                    for metric in ("area", "baseline", "noise", "amplitude", "cfd_ns", "fwhm_ns", "flags", "good"):
                        actual = getattr(rows, metric)
                        if expected[metric] is None: self.assertTrue(math.isnan(actual))
                        else: self.assertAlmostEqual(actual, expected[metric], places=9)
                    index += 1
            source.Close(); f.Close()
            with (output/"tables/channel_statistics.csv").open() as stream:
                stats = list(csv.DictReader(stream))
            self.assertEqual(sum(int(r["n"]) for r in stats if r["population"]=="all" and r["metric"]=="area"), result["waves"])
            for path, size, mtime in before:
                self.assertEqual((path.stat().st_size,path.stat().st_mtime_ns),(size,mtime))
            again = analyze_dataset(root, max_events=1)[0]
            self.assertNotEqual(again["output_dir"], result["output_dir"])
            self.assertTrue((output/"analysis.json").is_file())
            saved_root=output/"root/analysis.root";before_stat=(saved_root.stat().st_size,saved_root.stat().st_mtime_ns)
            revision=Path(render_saved(output)[0])
            self.assertEqual((saved_root.stat().st_size,saved_root.stat().st_mtime_ns),before_stat)
            focused=ROOT.TFile.Open(str(revision/"root/focused.root"))
            with (revision/"rendering.json").open() as stream: rendered=json.load(stream)
            self.assertIn("full_channels_charge_pc_logy",rendered["overview_pages"])
            self.assertIn("full_channels_amplitude_mv_logy",rendered["overview_pages"])
            self.assertNotIn("good_charge_pc",rendered["overview_pages"])
            for metric in ("charge_pc","amplitude","amplitude_mv"):
                for population in ("all","accepted"):
                    for ch in range(8):
                        hist=focused.Get(f"full_{population}_{metric}_ch{ch}")
                        self.assertEqual(hist.GetBinContent(0)+hist.GetBinContent(hist.GetNbinsX()+1),0)
            for key in focused.GetListOfKeys():
                item=focused.Get(key.GetName())
                if item.InheritsFrom("TH1"):
                    width=.02 if any(metric in item.GetName() for metric in ("charge_pc","tail_negative_pc")) else .5 if any(metric in item.GetName() for metric in ("amplitude_mv","baseline_mv","noise_mv")) else .05 if item.GetName()=="unit_trigger_interval" else 1
                    self.assertAlmostEqual(item.GetBinWidth(1),width)
            for prefix in ("","full_"):
                for population in ("all","accepted"):
                    for ch in range(8):
                        for physical,native,scale in (("charge_pc","area",.02),("tail_negative_pc","tail_negative",.02),
                                                      ("amplitude_mv","amplitude",.5),("baseline_mv","baseline",.5),("noise_mv","noise",.5)):
                            raw=focused.Get(f"{prefix}{population}_{native}_ch{ch}")
                            converted=focused.Get(f"{prefix}{population}_{physical}_ch{ch}")
                            self.assertEqual(raw.GetNbinsX(),converted.GetNbinsX())
                            self.assertEqual(raw.GetEntries(),converted.GetEntries())
                            self.assertAlmostEqual(raw.GetXaxis().GetXmin()*scale,converted.GetXaxis().GetXmin())
                            self.assertAlmostEqual(raw.GetXaxis().GetXmax()*scale,converted.GetXaxis().GetXmax())
                            self.assertTrue(all(raw.GetBinContent(i)==converted.GetBinContent(i) for i in range(raw.GetNbinsX()+2)))
            original_analysis=ROOT.TFile.Open(str(saved_root))
            for name in ("multiplicity","trigger_interval"):
                a=original_analysis.Get(name);b=focused.Get("unit_"+name)
                self.assertEqual(a.GetNbinsX(),b.GetNbinsX())
                self.assertTrue(all(a.GetBinContent(i)==b.GetBinContent(i) for i in range(a.GetNbinsX()+2)))
            original_analysis.Close()
            focused.Close()
            self.assertTrue((revision/"pdf/diagnostics.pdf").is_file())

    def test_finalize_can_chain_analysis_and_keep_verify_clean(self):
        fixture = PROJECT/"runtime/debug-20261101_1102/archive-fixture/raw"
        csv_fixture = PROJECT/"runtime/debug-20261101_1102/archives/real-waveform-functional-sample/G3/csv"
        if not fixture.is_dir(): self.skipTest("optional real waveform fixture is absent")
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp); raw=base/"raw";raw.mkdir()
            for source in fixture.glob("data_fee_3_*.dat"): shutil.copy2(source,raw/source.name)
            command=[sys.executable,"-B","-m","organizer","finalize","--raw-dir",str(raw),
                     "--csv-dir",str(csv_fixture),"--output-root",str(base/"output"),"--backend","pyroot",
                     "--link-mode","copy","--settle-seconds","0","--session-name","test","--analyze","--json"]
            result=subprocess.run(command,cwd=PROJECT,capture_output=True,text=True,timeout=120)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            report=json.loads(result.stdout)
            self.assertEqual(report["analysis"][0]["status"],"complete")
            self.assertEqual(report["analysis"][0]["waves"],4036)
            self.assertTrue(verify_dataset(Path(report["session_dir"]))["ok"])


if __name__ == "__main__": unittest.main()
