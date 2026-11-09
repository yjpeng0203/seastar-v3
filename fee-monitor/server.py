#!/usr/bin/env python3
"""Offline, read-only CSV monitor. Python 3.8+, standard library only."""
import argparse
import json
import mimetypes
import os
import re
import sys
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import webbrowser

ASSETS = Path(__file__).resolve().parent
CHUNK_SIZE = 4 * 1024 * 1024
STRING_DIR = re.compile(r"^g(\d+)$", re.IGNORECASE)
FEE_CSV = re.compile(r"^readback_fee_(\d+)(?:_|\.csv$)", re.IGNORECASE)


def infer_group(relative):
    for part in relative.parts[:-1]:
        match = STRING_DIR.match(part)
        if match:
            return f"G{int(match.group(1))}"
    match_fee = FEE_CSV.match(relative.name)
    if match_fee:
        fee_num = int(match_fee.group(1))
        return f"G{fee_num}" if 0 <= fee_num <= 6 else f"FEE {fee_num}"
    return ""


def make_handler(data_dir, sync=None):
    data_dir = Path(data_dir).resolve()

    class Handler(BaseHTTPRequestHandler):
        def send_body(self, body, content_type, status=200, headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (headers or {}).items():
                self.send_header(key, str(value))
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, value, status=200):
            self.send_body(json.dumps(value, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8", status)

        def do_GET(self):
            url = urlparse(self.path)
            try:
                if url.path == "/api/status":
                    self.send_json(sync.status() if sync else {"enabled": False})
                    return
                if url.path == "/api/files":
                    files = []
                    for path in data_dir.rglob("*"):
                        if path.suffix.lower() != ".csv" or not path.is_file():
                            continue
                        relative = path.relative_to(data_dir)
                        if any(part.startswith(".") or part == "results" for part in relative.parts):
                            continue
                        try:
                            path.resolve().relative_to(data_dir)
                            stat = path.stat()
                        except (ValueError, OSError):
                            continue
                        files.append({"name": relative.as_posix(),
                                      "size": stat.st_size,
                                      "group": infer_group(relative)})
                    self.send_json({"files": sorted(files, key=lambda f: f["name"])})
                    return
                if url.path == "/api/read":
                    query = parse_qs(url.query)
                    name = query.get("name", [""])[0]
                    path = (data_dir / name).resolve()
                    if not name or path.suffix.lower() != ".csv":
                        raise ValueError("请选择 CSV 文件")
                    try:
                        path.relative_to(data_dir)
                    except ValueError:
                        self.send_json({"error": "文件不在数据目录内"}, 403)
                        return
                    offset = int(query.get("offset", ["0"])[0])
                    if offset < 0:
                        raise ValueError("文件偏移量无效")
                    # Open first so a path replacement cannot mix stat and bytes.
                    with path.open("rb") as source:
                        stat = os.fstat(source.fileno())
                        generation = str(stat.st_dev) + ":" + str(stat.st_ino)
                        old_generation = query.get("generation", [""])[0]
                        old_mtime = query.get("mtime", [""])[0]
                        reset = (offset > stat.st_size or
                                 (old_generation and old_generation != generation) or
                                 (offset > 0 and offset == stat.st_size and old_mtime
                                  and old_mtime != str(stat.st_mtime_ns)))
                        if reset:
                            offset = 0
                        source.seek(offset)
                        body = source.read(min(CHUNK_SIZE, max(0, stat.st_size - offset)))
                    self.send_body(body, "application/octet-stream", headers={
                        "X-Next-Offset": offset + len(body),
                        "X-File-Size": stat.st_size,
                        "X-Generation": generation,
                        "X-Mtime": stat.st_mtime_ns,
                        "X-Reset": "1" if reset else "0",
                        "X-More": "1" if offset + len(body) < stat.st_size else "0",
                    })
                    return
                assets = {"/": "index.html", "/index.html": "index.html",
                          "/app.js": "app.js", "/data-worker.js": "data-worker.js",
                          "/vendor/echarts.min.js": "vendor/echarts.min.js"}
                if url.path not in assets:
                    self.send_json({"error": "页面不存在"}, 404)
                    return
                asset = ASSETS / assets[url.path]
                mime = mimetypes.guess_type(str(asset))[0] or "application/octet-stream"
                self.send_body(asset.read_bytes(), mime + "; charset=utf-8")
            except FileNotFoundError:
                self.send_json({"error": "文件不存在，请刷新文件列表"}, 404)
            except PermissionError:
                self.send_json({"error": "没有权限读取此文件"}, 403)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, OSError) as error:
                self.send_json({"error": str(error)}, 400)

        def log_message(self, *_args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ASSETS / "data",
                        help="CSV directory, including subdirectories (default: ./data)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the local browser")
    parser.add_argument("--local", action="store_true", help="Read local CSV directory without Pi SFTP sync")
    parser.add_argument("--remote", action="store_true", help="Mirror Pi CSVs over SSH/SFTP on this IPC")
    parser.add_argument("--config", type=Path, default=ASSETS / "config.json")
    parser.add_argument("--setup", action="store_true", help="Configure the Pi connection interactively")
    args = parser.parse_args()
    if args.local and (args.remote or args.setup):
        parser.error("--local 与 --remote / --setup 不能同时使用")
    sync = None
    if args.remote or args.setup:
        if os.name != "posix":
            parser.error("远端同步模式需要 Linux / POSIX 工控机和 OpenSSH 客户端")
        from sync_service import SyncService, load_config, login_password, setup_config
        try:
            if args.setup or not args.config.exists():
                if not sys.stdin.isatty():
                    parser.error("请先在工控机交互终端运行 sh start.sh --setup 配置连接")
                config = setup_config(args.config)
            else:
                config = load_config(args.config)
            password = login_password(config)
            sync = SyncService(config, password)
            args.data_dir = sync.root
        except (ValueError, OSError, EOFError) as error:
            parser.error(str(error))
    args.data_dir = args.data_dir.expanduser().resolve()
    if not args.data_dir.is_dir():
        parser.error("数据目录不存在: " + str(args.data_dir))
    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(args.data_dir, sync))
    except OSError as error:
        if sync:
            sync.stop()
        parser.exit(1, "无法启动: " + str(error) + "\n可用 --port 8766 更换端口。\n")
    port = server.server_address[1]
    local_url = "http://127.0.0.1:" + str(port)
    print("FEE CSV monitor: " + local_url, flush=True)
    if args.host == "0.0.0.0":
        print("LAN browser: http://<server-IP>:" + str(port), flush=True)
    elif args.host != "127.0.0.1":
        print("Network browser: http://" + args.host + ":" + str(port), flush=True)
    print("Data directory: " + str(args.data_dir), flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        if sync:
            sync.start()
        if args.open:
            if sys.platform.startswith("linux") and not (
                    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                print("No desktop detected; skipping browser launch. "
                      "Open the monitor from your control computer.", flush=True)
            else:
                # Some browser launchers wait for a process; HTTP must start regardless.
                Thread(target=webbrowser.open, args=(local_url,), daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if sync:
            sync.stop()
        server.server_close()


if __name__ == "__main__":
    main()
