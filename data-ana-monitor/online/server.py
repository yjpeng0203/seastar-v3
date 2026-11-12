#!/usr/bin/env python3
"""SeaStar read-only DAT live analysis / ROOT replay, local and LAN web dashboard."""
import argparse
import csv
from dataclasses import asdict
import hashlib
import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
import os
from pathlib import Path
import queue
import threading
import sys
import time
from urllib.parse import parse_qs, urlparse
import webbrowser

from acquisition import DatParser, DatReader, RootReader, discover
from analysis import Analyzer, Config, dumps, VERSION
from engine import Engine
from storage import FIELDS, Store, export_rows, read_run
from receiver import Receiver
from remote_reader import RemoteConfig
from password_auth import PasswordBroker
from histograms import query_histogram

ASSETS = Path(__file__).resolve().parent / "static"
# These releases share the same analysis and reader semantics; only UI/startup modes changed.
PLOT_COMPATIBLE_BACKENDS = ('7e72e8f0a5a9b7e637bc0998f595e1ef3833780fa0b616d8ae1573c67bc356f3',
                            'ab9c7d44e93f23a6742b4cf500180c6fe541ca2046e7c9e2d9a894b16ea8c79e',
                            'e0445348c0e23f6e3240b7d481f58cb8da4f377a2238496912f4d447dc15840a',
                            '8a2f1968f7a2f8ffa7c83da76b67bf808f2c8da8d008ad559232fdb9aba1761f',
                            '568c1a65a11254e702a7225f6ad8a6430fc8da4c9c789302a80b05270f479dfe',
                            '4634e0ea18841130ff23b45f97191270b759b25182a09250e23836bc8b943800',
                            '900b5c6dfd9a53991a1cb3fc493aaa18c0325c98d10935ee029a4783c08e6d3c',
                            'a3eead73490f6978e0ed30b831ba003c8d7c6f5d8ae6fb2ae0179eb473fe3481',
                            '579510d638bc6a46b2cc68e6b3a1721aea8c726c521950d58fa1e7a634df0821',
                            '1a81bbb9f6d040620608ee1732bd2844b9b3cf283ce94c0aed3ab6fe147a1fa3')


class Monitor:
    def __init__(self, data_dir, state_dir, config=None, source=None, follow=False, poll=.5, receiver=None, replay=False, read_gate=None, pressure=None):
        self.data_dir, self.state_dir = Path(data_dir).resolve(), Path(state_dir).resolve()
        self.config = config or Config()
        self.receiver=receiver
        self.read_gate=read_gate
        self.pressure=pressure
        self.replay = replay
        self.poll, self.requested_source, self.follow = poll, source, follow and not replay
        self.analyzer = Analyzer(self.config, self.state_dir / "build")
        self.kernel = hashlib.sha256((Path(__file__).parent / "adaptive_waveform.h").read_bytes()).hexdigest()
        self.identity = hashlib.sha256(b"".join((Path(__file__).parent / name).read_bytes() for name in
                                               ("native.cpp", "adaptive_waveform.h", "analysis.py", "engine.py",
                                                "acquisition.py", "storage.py", "server.py", "mirror_state.py",
                                                "remote_reader.py", "receiver.py", "password_auth.py",
                                                "ssh_askpass.py", "histograms.py"))).hexdigest()
        self.store = Store(self.state_dir / "analysis.sqlite3")
        self.stop = threading.Event()
        self.commands = queue.Queue()
        self.lock = threading.Lock()
        self.cached = {"status": "starting", "channels": [], "sources": [], "config": asdict(self.config)}
        self.sources, self.source, self.run_id = [], None, None
        self.reader, self.parser, self.engine = None, None, None
        self.error, self.paused, self.last_arrival, self.last_scan = None, False, None, 0.
        self.host_bytes, self.host_waves, self.host_seconds = 0, 0, 0.
        self.live_counter = None
        self.thread = threading.Thread(target=self.work, daemon=True, name="seastar-analysis")

    def start(self):
        if self.source and self.source['kind']=='dat' and not self.replay and self.live_counter is None:
            from live_rates import LiveRates
            self.live_counter=LiveRates(self.data_dir,self.state_dir,self.source['key'],self.follow)
        self.thread.start()

    def select(self, source, fresh=False):
        if self.live_counter and (source['kind']!='dat' or self.live_counter.source_key!=source['key']):
            self.live_counter.close(); self.live_counter=None
        if not self.replay and source['kind']=='dat' and self.thread.ident is not None and self.live_counter is None:
            from live_rates import LiveRates
            self.live_counter=LiveRates(self.data_dir,self.state_dir,source['key'],self.follow)
        if self.live_counter:
            self.live_counter.follow.set() if self.follow else self.live_counter.follow.clear()
        run_id, state = (None, None) if fresh else self.store.restore(source["key"], self.config, self.identity)
        if not fresh and run_id is None:
            for prior in PLOT_COMPATIBLE_BACKENDS:
                old_id,old_state=self.store.restore(source['key'],self.config,prior)
                if (old_state and old_state.get('kernel_sha256')==self.kernel and
                        old_state.get('engine',{}).get('version')==VERSION):
                    run_id,state=old_id,old_state
                    break
        if source["kind"] == "dat":
            reader = DatReader(self.data_dir, (state or {}).get("reader"))
        else:
            reader = RootReader(self.data_dir, source, (state or {}).get("reader"))
        if self.reader and hasattr(self.reader, "close"):
            self.reader.close()
        self.source, self.reader = source, reader
        self.error, self.paused = None, False
        self.run_id = run_id or self.store.create(source["key"], self.config, self.identity)
        self.run_backend=self.store.db.execute('SELECT kernel FROM runs WHERE id=?',(self.run_id,)).fetchone()[0]
        self.engine = Engine(self.config, self.analyzer, (state or {}).get("engine"))
        self.parser = DatParser((state or {}).get("parser"))
        self.last_arrival = (state or {}).get("last_arrival")
        self.host_bytes = self.host_waves = 0
        self.host_seconds = 0.
        self.checkpoint([])
        self.publish()
        self.store.rebuild_native(self.run_id,asdict(self.config))

    def checkpoint(self, metrics):
        self.store.save(self.run_id, {"engine": self.engine.state(), "reader": self.reader.state(),
                                     "parser": self.parser.state(), "last_arrival": self.last_arrival,
                                     "kernel_sha256": self.kernel}, metrics)

    def scan(self):
        self.sources = discover(self.data_dir)
        self.last_scan = time.monotonic()
        if self.source:
            self.source = next((s for s in self.sources if s["key"] == self.source["key"]), self.source)
        if not self.source and self.sources:
            selected = None
            if self.requested_source:
                selected = next((s for s in self.sources if s["key"] == self.requested_source or
                                 any(f["name"] == self.requested_source for f in s["files"])), None)
                if selected is None:
                    self.error = "等待指定数据源: " + self.requested_source
                    return
            self.select(selected or self.sources[0])
        elif self.follow and not self.paused and not self.error and self.source and self.source["kind"] == "dat" and self.sources:
            # Only follow newer runs of the same FEE; never silently drop another board's run.
            prefix = self.source["key"].split("_run", 1)[0]
            candidates = [s for s in self.sources if s["kind"] == "dat" and
                          s["key"].split("_run", 1)[0] == prefix and
                          (s["stamp"], s["key"]) > (self.source["stamp"], self.source["key"])]
            if candidates and self.backlog() == 0 and not self.parser.buffer:
                self.select(candidates[0])

    def backlog(self):
        if not self.reader or not self.source:
            return 0
        if self.source["kind"] == "root":
            return self.reader.entries - self.reader.offset
        return sum(max(0, f["size"] - self.reader.offsets.get(f["name"], 0)) for f in self.source["files"])

    def control(self, command):
        action = command.get("action")
        if action in ("select", "reset", "configure"):
            key = command.get("source") or (self.source or {}).get("key")
            source = next((s for s in self.sources if s["key"] == key), None)
            if not source:
                raise ValueError("数据源不存在，请刷新列表")
            if action == "configure":
                config = Config(**command["config"])
                analyzer = Analyzer(config, self.state_dir / "build")
                previous = self.config, self.analyzer
                self.config, self.analyzer = config, analyzer
            self.follow = bool(command.get("follow", False)) and not self.replay
            try:
                self.select(source, fresh=action in ("reset", "configure"))
            except Exception:
                if action == "configure":
                    self.config, self.analyzer = previous
                raise
        elif action == "pause":
            self.paused = True
        elif action == "resume":
            if self.error and self.source:
                self.select(self.source)
            self.paused = False
        elif action == "follow":
            self.follow = bool(command.get("follow")) and not self.replay
            if self.live_counter:self.live_counter.follow.set() if self.follow else self.live_counter.follow.clear()
        elif action in ('receive-pause','receive-resume'):
            if self.receiver is None:
                raise ValueError("当前不是远程接收模式")
            self.receiver.pause() if action=='receive-pause' else self.receiver.resume()
        else:
            raise ValueError("未知操作")

    def publish(self):
        snap = self.engine.snapshot() if self.engine else {"channels": [], "config": asdict(self.config)}
        age = max(0., time.time() - self.last_arrival) if self.last_arrival else None
        backlog = self.backlog()
        finished = bool(self.reader and (self.reader.finished or (self.replay and not backlog)))
        incomplete = bool(self.replay and self.reader and
                          (getattr(self.reader, "waiting_for_volume", None) is not None or
                           (finished and (getattr(self.reader, "first_index", 1) not in (None, 1) or
                                          (self.parser and (self.parser.buffer or self.parser.bad_bytes or
                                                            self.parser.orphans))))))
        status = ("error" if self.error else "paused" if self.paused else "waiting" if not self.source else
                  "incomplete" if incomplete else "replaying" if backlog else "complete" if finished else "tailing")
        alerts = []
        if self.error:
            alerts.append({"level": "error", "text": self.error})
        if self.parser and self.parser.bad_bytes:
            alerts.append({"level": "error", "text": f"跳过 {self.parser.bad_bytes} 个异常字节，重新同步 {self.parser.resyncs} 次；受影响触发可能不完整"})
        if self.parser and self.parser.orphans:
            alerts.append({"level": "warning", "text": f"忽略 {self.parser.orphans} 条无触发头波形；检查是否从中间分卷开始"})
        if self.reader and getattr(self.reader, "waiting_for_volume", None) is not None:
            alerts.append({"level": "error", "text": f"等待缺失分卷 idx{self.reader.waiting_for_volume:03d}；后续分卷尚未读取"})
        if self.reader and getattr(self.reader, "first_index", 1) not in (None, 1):
            alerts.append({"level": "warning", "text": "从中间分卷开始，累计量只涵盖本会话可见的数据"})
        if age is not None and age >= 5 and not finished and not self.paused and not backlog:
            alerts.append({"level": "warning", "text": f"{age:.0f} 秒没有读到新增数据；请核查取数状态或文件缓冲"})
        if self.parser and self.parser.buffer:
            text = (f"离线数据末尾有 {len(self.parser.buffer)} 字节未完成记录；末条记录未计入统计，请核对分卷是否完整"
                    if self.replay and finished else
                    f"保留 {len(self.parser.buffer)} 字节未完成记录，等待追加；不计为丢包")
            alerts.append({"level": "warning" if self.replay and finished else "info", "text": text})
        if snap.get("clock_backsteps"):
            alerts.append({"level": "error", "text": "电子学时钟发生回退，趋势已切到新的独立时间段；没有拼接为连续绝对时间"})
        if snap.get("header_backsteps") and self.source and self.source["kind"] == "dat":
            alerts.append({"level": "warning", "text": f"原始触发头时间戳回退 {snap['header_backsteps']} 次。默认时轴只用带波形事件（与 ROOT 一致）；空触发仅累计，不据此计算总触发率。"})
        for channel in snap["channels"]:
            if channel["n"] >= 30 and channel["good"] / channel["n"] < .5:
                alerts.append({"level": "warning", "text": channel["key"] + " 的合格波形不足 50%，请检查质量标记和波形窗口"})
            noise = channel["stats"]["good"]["noise"]["mean"]
            if channel["good"] >= 256 and noise and abs(channel["drift_adc"] or 0) > max(5, 5 * noise):
                alerts.append({"level": "warning", "text": channel["key"] + " 的近期基线相对初始参考发生明显漂移"})
        transfer=self.receiver.snapshot() if self.receiver else None
        if transfer:
            if transfer.get('error'):
                alerts.append({'level':'error' if transfer['status']=='blocked' else 'warning',
                               'text':'树莓派接收：'+transfer['error']})
            if transfer.get('file_noatime') is False:
                alerts.append({'level':'info','text':'源系统未启用免 atime 读取；未更改树莓派挂载参数，文件读取可能更新访问时间。'})
            if transfer.get('source_free_bytes') is not None and transfer['source_free_bytes']<2*1024**3:
                alerts.append({'level':'warning','text':'树莓派源盘可用空间不足 2 GiB；本程序不自动删除原始采集文件。'})
        snap.update({"status": status, "error": self.error, "alerts": alerts,
                     "sources": self.sources, "source": self.source, "run_id": self.run_id,
                     "kernel_sha256": self.kernel, "analysis_sha256": getattr(self,'run_backend',self.identity),
                     "server_sha256":self.identity,
                     "follow": self.follow, "paused": self.paused,
                     "pending_bytes": len(self.parser.buffer) if self.parser else 0,
                     "bad_bytes": self.parser.bad_bytes if self.parser else 0,
                     "orphan_waves": self.parser.orphans if self.parser else 0,
                     "backlog": backlog, "backlog_unit": "events" if self.source and self.source["kind"] == "root" else "bytes",
                     "file": self.reader.current if self.reader else None,
                     "transfer":transfer,
                     "input_mode": "replay" if self.replay else "remote" if self.receiver else "local",
                     "data_dir": str(self.data_dir), "state_dir": str(self.state_dir),
                     "age_s": age, "published_host_unix_s": time.time(),
                     "analysis_waves_per_s": self.host_waves / self.host_seconds if self.host_seconds else 0,
                     "database_bytes": sum(p.stat().st_size for p in
                                           (self.store.path, Path(str(self.store.path)+"-wal"), Path(str(self.store.path)+"-shm"))
                                           if p.exists()),
                     "time_basis": "带波形事件的电子学触发时间减去首条带波形触发（与 ROOT 一致）；空触发仅累计。带波形时钟回退后新起时间段。非 UTC / 北京时间。",
                     "last_event_provisional": bool(self.engine and self.engine.pending and not finished)})
        # Preserve integer-nanosecond precision even for counters larger than JS's 2**53.
        for field in ("origin_ns", "last_ns"):
            if snap.get(field) is not None:
                snap[field] = str(snap[field])
        for channel in snap.get('channels',[]):
            channel.pop('trend',None);channel.pop('recent',None)
        snap.pop('trigger_trend',None);snap.pop('trigger_recent',None)
        snap['trend_mode']='raw_observations'
        from time_reference import reference
        snap["beijing_reference"]=reference(self.data_dir,self.source,snap.get("origin_ns"))
        with self.lock:
            self.cached = snap

    def snapshot(self):
        with self.lock:
            return self.cached

    def query_target(self,request):
        snap=self.snapshot()
        if request.get('run_id')!=snap.get('run_id'):raise ValueError('分析会话已变化，请刷新')
        return dict(kind='sqlite',path=str(self.store.path),state_dir=str(self.state_dir),snapshot=snap)

    def rates(self, request):
        if request.get('live') and request.get('population','all')=='all' and self.source and self.source['kind']=='dat' and not self.replay:
            from live_rates import query
            return query(self.state_dir,self.source['key'],request,self.snapshot())
        from rates import sqlite_rates
        result=sqlite_rates(self.store.path,request,self.snapshot())
        result.update(live=False,analysis_limited=True,published_host_unix_s=self.snapshot().get('published_host_unix_s'))
        return result

    def work(self):
        last_publish = 0.
        try:
            while not self.stop.is_set():
                try:
                    while True:
                        try:
                            command = self.commands.get_nowait()
                        except queue.Empty:
                            break
                        self.control(command)
                    if time.monotonic() - self.last_scan > 1:
                        self.scan()
                    read = False
                    if self.pressure is not None and self.pressure.is_set():
                        self.cached=dict(self.cached,resource_paused=True)
                        self.stop.wait(.5)
                        continue
                    if self.source and not self.paused and not self.error:
                        start = time.perf_counter()
                        from contextlib import nullcontext
                        with self.read_gate if self.read_gate else nullcontext():
                            incoming = self.reader.read(self.source)
                        read = bool(incoming)
                        if read:
                            before = self.engine.waves
                            records = list(self.parser.feed(incoming)) if self.source["kind"] == "dat" else incoming
                            waves=[record['samples'] for record in records if record['type']=='wave']
                            pulses=iter(self.analyzer.batch(waves)) if hasattr(self.analyzer,'batch') else None
                            metrics = []
                            for record in records:
                                result = self.engine.consume(record,next(pulses) if pulses is not None and record["type"]=="wave" else None)
                                if result is not None:
                                    metrics.append(result)
                            self.last_arrival = time.time()
                            self.host_bytes += len(incoming) if isinstance(incoming, bytes) else 0
                            self.checkpoint(metrics)
                            self.host_waves += self.engine.waves - before
                            self.host_seconds += time.perf_counter() - start
                    if time.monotonic() - last_publish >= .75 or not read:
                        self.publish()
                        last_publish = time.monotonic()
                    if not read:
                        self.stop.wait(self.poll)
                except Exception as error:
                    # Reconstruct before publishing after a failed batch; no in-memory partial commit leaks.
                    self.error = str(error)
                    self.paused = True
                    if self.source and self.run_id:
                        row = self.store.db.execute("SELECT state FROM runs WHERE id=?", (self.run_id,)).fetchone()
                        if row and row[0]:
                            state = json.loads(row[0])
                            self.engine = Engine(self.config, self.analyzer, state["engine"])
                            self.parser = DatParser(state["parser"])
                    self.publish()
                    self.stop.wait(self.poll)
        finally:
            if self.reader and hasattr(self.reader, "close"):
                self.reader.close()

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=30)
        if self.thread.is_alive():
            raise RuntimeError("分析线程未在 30 秒内停止；数据库保留上一次完整检查点")
        if self.live_counter:self.live_counter.close()
        if hasattr(self,'query_jobs'):self.query_jobs.close()
        self.store.close()


def make_handler(monitor):
    from query_jobs import QueryRunner
    if not hasattr(monitor,'query_jobs'):monitor.query_jobs=QueryRunner(monitor.state_dir)
    class Handler(BaseHTTPRequestHandler):
        def disconnected(self):
            import select
            import socket
            try:
                ready,_,_=select.select([self.connection],[],[],0)
                return bool(ready) and self.connection.recv(1,socket.MSG_PEEK)==b''
            except (OSError,ValueError):return True
        def send(self, body, mime="application/json; charset=utf-8", status=200, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            try:
                if url.path == "/api/archive":
                    service = getattr(monitor, "archive_service", None)
                    self.send(dumps(service.snapshot() if service else dict(status="disabled",enabled=False,error="当前服务未启用测试结束归档",settings={},paths={},runs=[])).encode())
                    return
                snap = monitor.snapshot()
                if url.path in ("/api/status", "/api/snapshot"):
                    self.send(dumps(snap).encode())
                elif url.path == "/api/summary.json":
                    # The same snapshot includes analysis rules and kernel identity for provenance.
                    self.send(dumps(snap).encode(), headers={"Content-Disposition": 'attachment; filename="seastar-summary.json"'})
                elif url.path == "/api/metrics.csv":
                    if not snap.get("run_id"):
                        self.send(dumps({"error": "尚无分析会话"}).encode(), status=400)
                        return
                    query = parse_qs(url.query)
                    run_id = query.get("run_id", [snap["run_id"]])[0]
                    if hasattr(monitor,'export_csv'):
                        channel=query.get('channel',[None])[0]
                        max_seq=int(query['max_seq'][0]) if 'max_seq' in query else None
                        if max_seq is not None and max_seq<0:raise ValueError('记录水位无效')
                        chunks=monitor.export_csv(run_id,channel,max_seq)
                        first=next(chunks,b'')
                        self.send_response(200);self.send_header('Content-Type','text/csv; charset=utf-8')
                        self.send_header('Content-Disposition','attachment; filename="seastar-metrics.csv"');self.end_headers()
                        self.wfile.write(first)
                        for chunk in chunks:self.wfile.write(chunk)
                        return
                    config, identity, saved = read_run(monitor.store.path, run_id)
                    channel = query.get("channel", [None])[0]
                    if channel is not None:
                        parts = channel.split(":")
                        if len(parts) != 2 or any(not p.isdigit() or not 0 <= int(p) <= 255 for p in parts):
                            raise ValueError("通道无效")
                    max_seq = int(query["max_seq"][0]) if "max_seq" in query else None
                    if max_seq is not None and max_seq < 0:
                        raise ValueError("max_seq 不能为负数")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="seastar-metrics.csv"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    text = io.StringIO()
                    writer = csv.writer(text)
                    writer.writerow(("run_id",) + FIELDS + ("sample_ns", "adc_mv", "resistance_ohm", "kernel_sha256", "analysis_sha256"))
                    self.wfile.write(("\ufeff" + text.getvalue()).encode())
                    text.seek(0); text.truncate()
                    for rows in export_rows(monitor.store.path, run_id, channel, max_seq):
                        for row in rows:
                            writer.writerow((run_id,) + row + (config["sample_ns"], config["adc_mv"], config["resistance"],
                                                               saved.get("kernel_sha256", ""), identity))
                        self.wfile.write(text.getvalue().encode())
                        text.seek(0); text.truncate()
                else:
                    files = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
                             "/histogram-controls.js":"histogram-controls.js", "/query-client.js":"query-client.js", "/dashboard.js":"dashboard.js", "/raw-trends.js":"raw-trends.js", "/archive.js":"archive.js",
                             "/style.css": "style.css", "/vendor/echarts.min.js": "vendor/echarts.min.js",
                             "/vendor/LICENSE": "vendor/LICENSE", "/vendor/NOTICE": "vendor/NOTICE"}
                    if url.path not in files:
                        self.send(b'{"error":"not found"}', status=404)
                        return
                    path = ASSETS / files[url.path]
                    self.send(path.read_bytes(), (mimetypes.guess_type(str(path))[0] or "text/plain") + "; charset=utf-8")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, OSError) as error:
                self.send(dumps({"error": str(error)}).encode(), status=400)

        def do_POST(self):
            try:
                if self.path not in ("/api/control","/api/histogram","/api/raw-trend","/api/time-trend","/api/rates","/api/archive","/api/query-cancel"):
                    self.send(b'{"error":"not found"}', status=404)
                    return
                origin = self.headers.get("Origin")
                if origin and urlparse(origin).netloc != self.headers.get("Host"):
                    self.send(b'{"error":"origin rejected"}', status=403)
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("需要不超过 8 KiB 的 JSON 请求")
                command = json.loads(self.rfile.read(size))
                if not isinstance(command,dict):raise ValueError('需要 JSON 对象')
                if self.path=='/api/query-cancel':
                    self.send(dumps(monitor.query_jobs.cancel(command.get('query_id'))).encode());return
                heavy=self.path in ('/api/time-trend','/api/raw-trend','/api/histogram') or (self.path=='/api/rates' and not (command.get('live') and command.get('population','all')=='all' and not getattr(monitor,'replay',False) and (monitor.snapshot().get('source') or {}).get('kind')=='dat'))
                if heavy and hasattr(monitor,'query_target'):
                    value=monitor.query_jobs.run(monitor.query_target(command),self.path,command,self.disconnected)
                    self.send(dumps(value).encode());return
                if self.path == "/api/archive":
                    service = getattr(monitor, "archive_service", None)
                    if service is None:raise ValueError("当前服务未启用测试结束归档")
                    self.send(dumps(service.control(command)).encode());return
                if self.path=='/api/rates':
                    if hasattr(monitor,'rates'):value=monitor.rates(command)
                    else:
                        from rates import sqlite_rates
                        value=sqlite_rates(monitor.store.path,command,monitor.snapshot())
                    self.send(dumps(value).encode());return
                if self.path=='/api/time-trend':
                    if hasattr(monitor,'time_trend'):value=monitor.time_trend(command)
                    else:
                        from time_bins import sqlite_time_bins
                        value=sqlite_time_bins(monitor.store.path,command,monitor.snapshot())
                    self.send(dumps(value).encode());return
                if self.path=='/api/raw-trend':
                    if hasattr(monitor,'raw_trend'):value=monitor.raw_trend(command)
                    else:
                        from raw_trends import sqlite_page
                        value=sqlite_page(monitor.store.path,command,monitor.snapshot())
                    self.send(dumps(value).encode());return
                if self.path=='/api/histogram':
                    self.send(dumps(monitor.histogram(command) if hasattr(monitor,"histogram") else query_histogram(monitor.store.path,command)).encode())
                    return
                if not isinstance(command, dict) or command.get("action") not in ("select", "reset", "configure", "pause", "resume", "follow", "receive-pause", "receive-resume"):
                    raise ValueError("无效操作")
                if command["action"] == "configure":
                    if not isinstance(command.get("config"), dict):
                        raise ValueError("需要完整 config 对象")
                    Config(**command["config"])
                if command["action"] in ("select", "configure") and command.get("source") not in [s["key"] for s in monitor.snapshot()["sources"]]:
                    raise ValueError("数据源不存在")
                if monitor.commands.qsize() > 8:
                    raise ValueError("操作过于频繁，请等待当前操作完成")
                monitor.commands.put(command)
                self.send(b'{"accepted":true}', status=202)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
                self.send(dumps({"error": str(error)}).encode(), status=400)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent
    parser.add_argument("--data-dir", type=Path, default=None, help="本机 DAQ 输出或历史数据目录；默认直接只读，不连接树莓派")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--remote",type=Path,help="树莓派只读增量接收配置 JSON；只需在工控机启动")
    parser.add_argument("--source", help="数据源 key 或相对文件名；不指定时选择最新 DAT")
    follow = parser.add_mutually_exclusive_group()
    follow.add_argument("--follow-new-runs", dest="follow_new_runs", action="store_true", default=None,
                        help="自动跟随同一 FEE 的更新 Run（在线模式默认开启）")
    follow.add_argument("--no-follow-new-runs", dest="follow_new_runs", action="store_false",
                        help="固定当前 Run，不自动切换")
    parser.add_argument("--replay", action="store_true", help="本机历史数据回放；读完显示完成，不跟随新 Run、不报取数停滞")
    parser.add_argument("--workers",type=int,default=3,help="独立串分析并发上限，默认 3")
    parser.add_argument("--single-source",action="store_true",help="兼容单源调试；默认所有串持续处理")
    parser.add_argument("--archive-config",type=Path,help="网页归档使用的 organizer 配置；也可在网页设置结果与 CSV 目录")
    parser.add_argument("--config", type=Path, help="分析参数 JSON；参数变更创建独立会话")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--poll", type=float, default=.5)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    if args.replay and (args.remote or args.follow_new_runs is True):
        parser.error("--replay 不能与 --remote 或 --follow-new-runs 同时使用")
    args.data_dir=(args.data_dir or (here/'runtime'/'received' if args.remote else here.parent)).expanduser().resolve()
    args.state_dir=(args.state_dir or (here/'runtime'/'analysis' if args.remote else here/'state')).expanduser().resolve()
    if (not args.remote and not args.data_dir.is_dir()) or not .1 <= args.poll <= 60 or not 0 <= args.port <= 65535 or not 1<=args.workers<=16:
        parser.error("数据目录不存在、poll 不在 0.1–60 秒范围或端口无效")
    config = Config(**json.loads(args.config.read_text())) if args.config else Config()
    receiver=None
    auth=None
    try:
        if args.remote:
            remote_config=RemoteConfig.load(args.remote)
            if remote_config.auth=='password':
                if not sys.stdin.isatty():
                    raise RuntimeError("密码模式请从工控机交互终端启动；无人值守开机服务可使用 SSH 密钥或 agent")
                auth=PasswordBroker(getpass.getpass('树莓派 SSH 密码（仅存工控机运行内存，不保存）：'))
            receiver=Receiver(remote_config,args.data_dir,auth=auth)
        from fleet import FleetMonitor
        factory=Monitor if args.single_source else FleetMonitor
        extra={} if args.single_source else {'workers':args.workers}
        monitor = factory(args.data_dir, args.state_dir, config, args.source,
                          args.follow_new_runs if args.follow_new_runs is not None else not args.replay,
                          args.poll, receiver=receiver, replay=args.replay,**extra)
    except (RuntimeError, OSError, ValueError, TypeError) as error:
        if receiver:receiver.close()
        if auth:auth.close()
        parser.exit(1, "无法启动分析服务: " + str(error) + "\n可先运行 sh start.sh --check 检查环境。\n")
    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(monitor))
    except OSError as error:
        monitor.store.close()
        if receiver:receiver.close()
        if auth:auth.close()
        parser.exit(1, "无法监听 " + args.host + ":" + str(args.port) + ": " + str(error) +
                    "\n端口被占用时，可用 --port 8767 更换端口。\n")
    server.daemon_threads=True
    from archive_service import ArchiveService
    monitor.archive_service = ArchiveService(args.data_dir,args.state_dir,args.archive_config,enabled=not (args.replay or receiver))
    if receiver:receiver.start()
    monitor.start()
    url = "http://127.0.0.1:" + str(server.server_port)
    print("SeaStar waveform monitor: " + url, flush=True)
    print("Data (read-only): " + str(args.data_dir.resolve()), flush=True)
    print("Checkpoints / metrics: " + str(args.state_dir.resolve()), flush=True)
    if not receiver:
        print("Mode: " + ("本机历史数据回放" if args.replay else "本机 DAQ 目录直读") +
              "；不连接树莓派，不复制 DAT，分析状态单独保存。", flush=True)
    if receiver:
        print("Remote read-only: "+receiver.config.host+":"+receiver.config.remote_dir,flush=True)
        print("树莓派不安装文件；单 SSH 长连接，禁止远端字节码缓存，续传状态仅写工控机。",flush=True)
    if args.host == "0.0.0.0":
        print("LAN: http://<运行服务的主机IP>:" + str(server.server_port), flush=True)
    elif args.host != "127.0.0.1":
        print("Network browser: http://" + args.host + ":" + str(server.server_port), flush=True)
    if args.open:
        if os.name == "posix" and os.uname().sysname == "Linux" and not (
                os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            print("无桌面环境，跳过打开浏览器；请在工控机浏览器访问运行服务主机的地址。", flush=True)
        else:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        monitor.archive_service.close()
        monitor.close()
        if receiver:receiver.close()
        if auth:auth.close()


if __name__ == "__main__":
    main()
