import json
import math
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler, Request

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from acquisition import DatParser, DatReader, SourceChanged, discover
from analysis import Analyzer, Config, Distribution
from engine import Engine, Trend
from server import Monitor, ThreadingHTTPServer, make_handler
from storage import Store
import check_env
import server as monitor_server


def trigger(number, ns):
    second, nano = divmod(ns, 1_000_000_000)
    return b"\xff\x12" + struct.pack(">HIII", number, 0, second, nano)


def waveform(values, ns, board=3, channel=0, wave_id=1):
    second, nano = divmod(ns, 1_000_000_000)
    return bytes([254, board * 16 + channel, wave_id, 0]) + struct.pack(">III", second, nano, len(values)) + np.array(values, dtype=">u2").tobytes()


def gaussian(center=50, width=5, height=800, baseline=197, n=504):
    x = np.arange(n)
    return np.round(baseline + height * np.exp(-.5 * ((x - center) / width) ** 2)).astype(np.uint16)


class OnlineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.config = Config(display_stride=1)
        cls.analyze = Analyzer(cls.config, cls.build.name)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def test_position_invariant_area_and_timing(self):
        expected_area = 800 * 5 * math.sqrt(2 * math.pi)
        rows = [self.analyze(gaussian(center=i)) for i in (45, 85, 180, 350)]
        for center, result in zip((45, 85, 180, 350), rows):
            self.assertTrue(result["good"], result)
            self.assertAlmostEqual(result["baseline"], 197, places=4)
            self.assertLess(abs(result["area"] / expected_area - 1), .01)
            self.assertAlmostEqual(result["peak_ns"], center * 2)
            self.assertLess(abs(result["fwhm_ns"] - 2.35482 * 5 * 2), .1)
            self.assertLess(result["start"], center)
            self.assertGreater(result["end"], center)
        self.assertLess(max(r["area"] for r in rows) - min(r["area"] for r in rows), 1)
        for a, b in zip(rows, rows[1:]):
            self.assertAlmostEqual(b["cfd_ns"] - a["cfd_ns"], b["peak_ns"] - a["peak_ns"])

    def test_tail_does_not_bias_main_area_and_no_overlap(self):
        clean = gaussian(center=70)
        values = clean.astype(float) - 80 * np.exp(-.5 * ((np.arange(504) - 140) / 10) ** 2)
        p, q = self.analyze(clean), self.analyze(np.round(values).astype(np.uint16))
        self.assertLess(abs(p["area"] - q["area"]), 5)
        self.assertGreater(q["tail_negative"], 1500)
        self.assertLess(q["end"], 120)
        self.assertGreater(q["area"] + q["tail_negative"], q["area"])

    def test_noise_negative_polarity_edge_clipping_and_pileup(self):
        rng = np.random.default_rng(7)
        p = self.analyze(np.round(200 + rng.normal(0, 1, 504)).astype(np.uint16))
        self.assertFalse(p["good"])
        self.assertIn("low_snr", p["flag_names"])
        self.assertIsNone(p["cfd_ns"])
        negative = (1000 - (gaussian(height=400, baseline=0)).astype(int)).astype(np.uint16)
        p = self.analyze(negative)
        self.assertTrue(p["good"])
        self.assertEqual(p["polarity"], -1)
        self.assertGreater(p["area"], 0)
        p = self.analyze(gaussian(center=3))
        self.assertFalse(p["good"])
        self.assertTrue("edge" in p["flag_names"] or "baseline_fallback" in p["flag_names"])
        clipped = np.minimum(gaussian(height=6000).astype(np.uint32), 4095).astype(np.uint16)
        p = self.analyze(clipped)
        self.assertFalse(p["good"])
        self.assertIn("clipped", p["flag_names"])
        double = gaussian(center=55, width=7).astype(int) + gaussian(center=80, width=7, baseline=0).astype(int)
        p = self.analyze(double.astype(np.uint16))
        self.assertIn("pileup", p["flag_names"])
        self.assertGreaterEqual(p["pulses"], 2)
        for v in p.values():
            if isinstance(v, float):
                self.assertTrue(math.isfinite(v))
        self.assertFalse(self.analyze(np.zeros(8, dtype=np.uint16))["good"])

    def test_cfd_below_noise_is_missing_not_a_spurious_crossing(self):
        values = gaussian(height=12).astype(int) + np.tile([-1, 1], 252)
        p = self.analyze(values.astype(np.uint16))
        self.assertTrue(p["good"], p)
        self.assertLess(p["amplitude"]*.1, self.config.timing_snr*p["noise"])
        self.assertIsNone(p["cfd_ns"])
        self.assertIsNone(p["rise_ns"])
        self.assertIn("timing_missing", p["flag_names"])

    def test_every_byte_chunk_boundary_checkpoint_and_resync(self):
        values = gaussian()
        raw = trigger(65535, 999999900) + waveform(values, 1000000150) + trigger(0, 2000000000)
        parser = DatParser()
        records = []
        for byte in raw:
            records.extend(parser.feed(bytes([byte])))
            parser = DatParser(parser.state())
        self.assertEqual([r["type"] for r in records], ["trigger", "wave", "trigger"])
        np.testing.assert_array_equal(records[1]["samples"], values)
        self.assertFalse(parser.buffer)
        self.assertEqual(parser.bad_bytes, 0)
        bad = DatParser()
        records = list(bad.feed(b"bad framing!!" + raw))
        self.assertEqual(len(records), 3)
        self.assertEqual(bad.bad_bytes, 13)
        self.assertEqual(bad.resyncs, 1)
        incomplete = DatParser()
        records = list(incomplete.feed(raw[:100]))
        self.assertEqual(len(records), 1)
        self.assertEqual(incomplete.bad_bytes, 0)
        self.assertTrue(incomplete.buffer)

    def test_rotation_split_record_and_missing_volume(self):
        raw = trigger(1, 10**9) + waveform(gaussian(), 10**9 + 250) + trigger(2, 2 * 10**9)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p = root / "data_fee_3_run000001_idx001_20261023100000.dat"
            p.write_bytes(raw[:117])
            source = discover(root)[0]
            reader, parser = DatReader(root), DatParser()
            rows = list(parser.feed(reader.read(source)))
            self.assertEqual(len(rows), 1)
            parser, reader = DatParser(parser.state()), DatReader(root, reader.state())
            q = root / p.name.replace("idx001", "idx003")
            q.write_bytes(raw[117:])
            self.assertFalse(reader.read(discover(root)[0]))
            self.assertEqual(reader.waiting_for_volume, 2)
            q.rename(root / p.name.replace("idx001", "idx002"))
            rows += list(parser.feed(reader.read(discover(root)[0])))
            self.assertEqual(len(rows), 3)
            self.assertFalse(parser.buffer)
            self.assertFalse(reader.read(discover(root)[0]))
            with p.open("ab") as stream:
                stream.write(b"oops")
            with self.assertRaises(SourceChanged):
                reader.read(discover(root)[0])

    def test_overwrite_anchor_and_discovery_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p = root / "x.dat"
            p.write_bytes(b"a" * 128)
            source = discover(root)[0]
            reader = DatReader(root)
            reader.read(source)
            p.write_bytes(b"b" * 256)
            with self.assertRaises(SourceChanged):
                reader.read(discover(root)[0])
            (root / "results_fake").mkdir()
            (root / "results_fake" / "data_fake.root").write_bytes(b"x")
            self.assertEqual(len(discover(root)), 1)

    def test_engine_coincidences_wrap_and_clock_segments(self):
        raw = trigger(65535, 10**9) + waveform(gaussian(), 10**9+250, channel=0)
        raw += waveform(gaussian(center=52), 10**9+252, channel=1) + trigger(0, 2*10**9)
        raw += waveform(gaussian(), 2*10**9+250, channel=0)
        engine = Engine(self.config, self.analyze)
        for r in DatParser().feed(raw):
            engine.consume(r)
        snap = engine.snapshot()
        self.assertEqual(snap["triggers"], 2)
        self.assertEqual(snap["waves"], 3)
        self.assertEqual(snap["number_gaps"], 0)
        self.assertEqual(snap["multiplicity"], {2: 1, 1: 1})
        self.assertEqual(snap["pairs"], {"3:0/3:1": 1})
        self.assertAlmostEqual(snap["pair_dt"]["3:0/3:1"]["mean"], 4)
        engine = Engine(self.config, self.analyze, json.loads(json.dumps(engine.state())))
        engine.consume({"type": "trigger", "ns": 5, "number": 1, "position": 5000})
        engine.consume({"type": "wave", "ns": 255, "position": 5001, "board": 3,
                        "channel": 0, "wave_id": 2, "samples": gaussian()})
        snap = engine.snapshot()
        self.assertEqual(snap["segment"], 1)
        self.assertEqual(snap["relative_s"], 0)
        self.assertEqual(snap["observed_s"], 1)

    def test_histogram_and_trends_preserve_counts(self):
        values = [-1e9, -1, -.1, 0, 1, 100, 1e8]
        d = Distribution()
        for v in values:
            d.add(v)
        self.assertEqual(sum(n for _, n in d.snapshot()["hist"]), len(values))
        self.assertAlmostEqual(d.mean, np.mean(values))
        self.assertAlmostEqual(d.snapshot()["sigma"] / np.std(values), 1.)
        d = Distribution(d.state())
        self.assertEqual(d.n, len(values))
        narrow = Distribution()
        for value in [197., 197.1, 196.9, 197.3]:
            narrow.add(value)
        self.assertLess(narrow.snapshot()["bin_width"], .01)
        trend = Trend()
        for i in range(10000):
            trend.add(i, [1, 1, 200, 1, 5, 10, 100, 1, 1])
        self.assertLessEqual(len(trend.rows), 600)
        self.assertLessEqual(len(trend.recent), 901)
        self.assertEqual(sum(v[0] for v in trend.rows.values()), 10000)
        data = trend.snapshot(9999)
        self.assertTrue(all(v["exposure"] <= v["width"] for v in data))
        self.assertEqual(data[0]["rate"], 1)

    def test_empty_trigger_clock_does_not_reset_waveform_time(self):
        raw = trigger(0, 10**9) + waveform(gaussian(), 10**9+250)
        raw += trigger(1, 9999*10**9)  # observed empty-trigger clock need not match
        raw += trigger(2, 2*10**9) + waveform(gaussian(), 2*10**9+250)
        engine = Engine(self.config, self.analyze)
        for record in DatParser().feed(raw):
            engine.consume(record)
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["triggers"], 3)
        self.assertEqual(snapshot["wave_events"], 2)
        self.assertEqual(snapshot["header_backsteps"], 1)
        self.assertEqual(snapshot["clock_backsteps"], 0)
        self.assertEqual(snapshot["relative_s"], 1)
        self.assertEqual(snapshot["multiplicity"], {1: 2, 0: 1})

    def test_pending_pair_snapshot_is_exact_without_double_counting(self):
        raw = trigger(0, 10**9) + waveform(gaussian(), 10**9+250)
        raw += waveform(gaussian(center=52), 10**9+250, channel=1)
        engine = Engine(self.config, self.analyze)
        for record in DatParser().feed(raw):
            engine.consume(record)
        for _ in range(3):
            self.assertEqual(engine.snapshot()["pair_dt"]["3:0/3:1"]["n"], 1)
        engine.consume({"type":"trigger", "ns":2*10**9,"number":1,"position":10})
        self.assertEqual(engine.snapshot()["pair_dt"]["3:0/3:1"]["n"], 1)

    def test_atomic_metrics_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.db")
            run = store.create("s", self.config, "k")
            store.save(run, {"offset": 5}, [])
            from storage import FIELDS
            row = {k: 1 for k in FIELDS}
            store.save(run, {"offset": 10}, [row])
            with self.assertRaises(sqlite3.IntegrityError):
                store.save(run, {"offset": 20}, [row])
            self.assertEqual(store.restore("s", self.config, "k")[1], {"offset": 10})
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM metrics").fetchone()[0], 1)
            store.close()

    def test_two_writers_cannot_clobber_one_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"test.db"
            first=Store(path)
            try:
                with self.assertRaises(RuntimeError):
                    Store(path)
            finally:
                first.close()
            second=Store(path)
            second.close()

    def test_environment_check_reports_missing_dependencies_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            absent=Path(directory)/"absent"
            with patch.object(check_env.shutil,"which",return_value=None), \
                    patch.object(check_env.importlib,"import_module",side_effect=ImportError("numpy unavailable")):
                checks={name:ok for name,ok,_ in check_env.inspect(absent)}
            self.assertFalse(checks["NumPy"])
            self.assertFalse(checks["C++ 编译器"])
            self.assertFalse(checks["数据目录"])
            self.assertFalse(absent.exists())

    def test_launcher_preserves_callers_relative_data_path(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"data").mkdir()
            launcher=Path(__file__).resolve().parents[1]/"start.sh"
            result=subprocess.run(["sh",str(launcher),"--check","--data-dir","data"],
                                  cwd=root,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn(str((root/"data").resolve()),result.stdout)
            self.assertFalse((root/"state").exists())

    def test_runtime_modules_use_python38_syntax(self):
        import ast
        for module in Path(__file__).resolve().parents[1].glob("*.py"):
            ast.parse(module.read_text(),filename=str(module),feature_version=8)

    def test_headless_start_matches_fee_monitor_workflow(self):
        import io
        with tempfile.TemporaryDirectory() as directory:
            output=io.StringIO()
            with patch.object(sys,"argv",["server.py","--single-source","--data-dir",directory,"--host","0.0.0.0","--open"]), \
                    patch("server.os.uname") as uname, \
                    patch.dict("server.os.environ",{},clear=True), \
                    patch("server.Monitor") as factory, \
                    patch("server.ThreadingHTTPServer") as http, \
                    patch("server.webbrowser.open") as browser, \
                    patch("sys.stdout",output):
                uname.return_value.sysname="Linux"
                http.return_value.server_port=8766
                http.return_value.serve_forever.side_effect=KeyboardInterrupt
                monitor_server.main()
                browser.assert_not_called()
                factory.return_value.start.assert_called_once()
                factory.return_value.close.assert_called_once()
                http.return_value.serve_forever.assert_called_once()
            self.assertIn("无桌面环境",output.getvalue())
            self.assertIn("运行服务的主机IP",output.getvalue())

    def test_busy_port_closes_state_and_reports_alternative(self):
        import io
        with tempfile.TemporaryDirectory() as directory:
            errors=io.StringIO()
            with patch.object(sys,"argv",["server.py","--single-source","--data-dir",directory]), \
                    patch("server.Monitor") as factory, \
                    patch("server.ThreadingHTTPServer",side_effect=OSError("Address already in use")), \
                    patch("sys.stderr",errors):
                with self.assertRaises(SystemExit) as error:
                    monitor_server.main()
                self.assertEqual(error.exception.code,1)
                factory.return_value.store.close.assert_called_once()
                factory.return_value.start.assert_not_called()
            self.assertIn("--port 8767",errors.getvalue())

    def test_local_launcher_never_starts_ssh_and_defaults_to_follow(self):
        import io
        with tempfile.TemporaryDirectory() as directory:
            for flags, expected_follow, replay in (([], True, False), (["--replay"], False, True),
                                                    (["--no-follow-new-runs"], False, False)):
                with self.subTest(flags=flags), \
                        patch.object(sys,"argv",["server.py","--single-source","--data-dir",directory]+flags), \
                        patch("server.Monitor") as factory, \
                        patch("server.Receiver") as receiver, \
                        patch("server.PasswordBroker") as password, \
                        patch("server.ThreadingHTTPServer") as http, \
                        patch("sys.stdout",io.StringIO()):
                    http.return_value.server_port=8766
                    http.return_value.serve_forever.side_effect=KeyboardInterrupt
                    monitor_server.main()
                    receiver.assert_not_called();password.assert_not_called()
                    self.assertEqual(factory.call_args.args[4],expected_follow)
                    self.assertEqual(factory.call_args.kwargs['replay'],replay)
                    self.assertIsNone(factory.call_args.kwargs['receiver'])

    def test_mislabeled_compressed_dat_is_rejected_without_consuming_bytes(self):
        import gzip
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            raw=trigger(0,10**9)+waveform(gaussian(),10**9+250)
            path=root/'data_fee_3_run000001_idx001_20261026171940.dat'
            compressed=gzip.compress(raw);path.write_bytes(compressed)
            reader=DatReader(root)
            with self.assertRaisesRegex(SourceChanged,'gzip'):
                reader.read(discover(root)[0])
            self.assertEqual(reader.offsets,{})
            self.assertEqual(path.read_bytes(),compressed)

    def test_local_replay_spans_volumes_and_reports_incomplete_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            data=root/'data';data.mkdir()
            name='data_fee_3_run000001_idx001_20261026171940.dat'
            raw=trigger(0,10**9)+waveform(gaussian(),10**9+250)
            (data/name).write_bytes(raw[:100])
            (data/name.replace('idx001','idx002')).write_bytes(raw[100:])
            selected=discover(data)[0]['key']
            # A later Run must not steal a historical replay selected explicitly.
            (data/name.replace('run000001','run000002')).write_bytes(raw)
            for partial in (False,True):
                if partial:
                    with (data/name.replace('idx001','idx002')).open('ab') as out:out.write(trigger(1,2*10**9)[:7])
                monitor=Monitor(data,root/('partial' if partial else 'complete'),self.config,
                                source=selected,follow=True,poll=.1,replay=True)
                monitor.start()
                try:
                    end=time.monotonic()+10
                    while time.monotonic()<end:
                        snap=monitor.snapshot()
                        if snap.get('status') in ('complete','incomplete','error'):break
                        time.sleep(.03)
                    self.assertFalse(snap.get('error'))
                    self.assertEqual(snap['status'],'incomplete' if partial else 'complete')
                    self.assertEqual(snap['waves'],1)
                    self.assertEqual(snap['source']['key'],selected)
                    self.assertFalse(snap['follow']);self.assertIsNone(snap['transfer'])
                    self.assertEqual(snap['input_mode'],'replay')
                    self.assertEqual(snap['data_dir'],str(data.resolve()))
                    self.assertFalse(snap['last_event_provisional'])
                    self.assertEqual(snap['pending_bytes'],7 if partial else 0)
                finally:monitor.close()
                monitor.last_arrival=time.time()-1000;monitor.publish()
                self.assertFalse(any('没有读到新增数据' in a['text'] for a in monitor.snapshot()['alerts']))
            self.assertEqual((data/name).read_bytes(),raw[:100])
            self.assertFalse((data/'.receiver').exists())

    def test_follow_new_run_same_second_respects_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            first=root/"data_fee_3_run000001_idx001_20261023100000.dat"
            second=root/first.name.replace("run000001","run000002")
            raw=trigger(0,10**9)+waveform(gaussian(),10**9+250)
            first.write_bytes(raw)
            monitor=Monitor(root,root/"state",self.config,follow=True,poll=.1)
            monitor.start()
            def wait_for(predicate):
                end=time.monotonic()+10
                while time.monotonic()<end:
                    snap=monitor.snapshot()
                    if snap.get("error"):
                        self.fail(snap["error"])
                    if predicate(snap):return snap
                    time.sleep(.05)
                self.fail("run switch timeout")
            try:
                wait_for(lambda s:s.get("waves")==1)
                monitor.commands.put({"action":"pause"})
                wait_for(lambda s:s.get("paused"))
                second.write_bytes(raw)
                # A scan really sees the new file while paused, without changing source.
                snap=wait_for(lambda s:len(s.get("sources",[]))==2)
                self.assertIn("run000001",snap["source"]["key"])
                monitor.commands.put({"action":"resume"})
                snap=wait_for(lambda s:"run000002" in (s.get("source") or {}).get("key","") and s.get("waves")==1)
                self.assertEqual(snap["triggers"],1)
            finally:
                monitor.close()

    def test_live_append_restart_http_and_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "live.dat"
            raw = trigger(1, 10**9) + waveform(gaussian(), 10**9+250) + trigger(2, 2*10**9)
            path.write_bytes(raw[:100])
            monitor = Monitor(root, root / "state", self.config, poll=.1)
            monitor.start()
            def wait_waves(expected):
                deadline = time.monotonic()+15
                while time.monotonic()<deadline:
                    snap = monitor.snapshot()
                    if snap.get("waves")==expected and snap.get("status")!="replaying":
                        return snap
                    if snap.get("error"):
                        self.fail(snap["error"])
                    time.sleep(.05)
                self.fail("analysis timeout: " + str(monitor.snapshot()))
            wait_waves(0)
            with path.open("ab") as f:
                f.write(raw[100:])
            snap = wait_waves(1)
            first_run = snap["run_id"]
            monitor.close()
            monitor = Monitor(root, root / "state", self.config, poll=.1)
            monitor.start()
            snap = wait_waves(1)
            self.assertEqual(snap["run_id"], first_run)
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
            except Exception:
                monitor.close()
                raise
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = "http://127.0.0.1:" + str(server.server_port)
            opener = build_opener(ProxyHandler({}))
            try:
                for route in ("/", "/app.js", "/style.css", "/vendor/echarts.min.js", "/api/snapshot", "/api/summary.json", "/api/metrics.csv"):
                    with opener.open(url+route) as response:
                        body=response.read()
                        self.assertTrue(body, route)
                        self.assertEqual(response.status, 200)
                        if route=="/api/metrics.csv":
                            self.assertEqual(len(body.decode("utf-8-sig").splitlines()), 2)
                for route in ("/server.py", "/../storage.py", "/state/analysis.sqlite3"):
                    with self.assertRaises(HTTPError) as error:
                        opener.open(url+route)
                    error.exception.close()
                bad = Request(url+"/api/control", data=b'{"action":"pause"}', headers={"Content-Type":"application/json", "Origin":"http://example.com"})
                with self.assertRaises(HTTPError) as error:
                    opener.open(bad)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
                with opener.open(url+"/api/metrics.csv?run_id="+first_run+"&max_seq=1") as response:
                    self.assertEqual(len(response.read().decode("utf-8-sig").splitlines()), 1)
                invalid = Request(url+"/api/control", data=b'{"action":"configure","source":"live.dat"}',
                                  headers={"Content-Type":"application/json"})
                with self.assertRaises(HTTPError) as error:
                    opener.open(invalid)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                from dataclasses import asdict
                config = asdict(self.config)
                config["adc_mv"] = 1.0
                request = Request(url+"/api/control", data=json.dumps({"action":"configure", "source":"live.dat", "config":config}).encode(),
                                  headers={"Content-Type":"application/json"})
                with opener.open(request) as response:
                    self.assertEqual(response.status, 202)
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    if monitor.snapshot().get("run_id")!=first_run and monitor.snapshot().get("waves")==1:
                        break
                    time.sleep(.05)
                self.assertNotEqual(monitor.snapshot()["run_id"], first_run)
                self.assertEqual(monitor.snapshot()["config"]["adc_mv"], 1.0)
                with opener.open(url+"/api/metrics.csv?run_id="+first_run) as response:
                    import csv, io
                    rows=list(csv.DictReader(io.StringIO(response.read().decode("utf-8-sig"))))
                    self.assertEqual(rows[0]["adc_mv"], "0.5")
            finally:
                server.shutdown();server.server_close();thread.join();monitor.close()


if __name__ == "__main__":
    unittest.main()
