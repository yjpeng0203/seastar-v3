import json
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen, build_opener, ProxyHandler
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import CHUNK_SIZE, ThreadingHTTPServer, make_handler
import server as monitor


class MonitorTest(unittest.TestCase):
    def test_remote_first_launch_setup_keeps_password_out_of_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            cache = Path(directory) / "mirror"
            cache.mkdir()
            with patch.object(sys, "argv", ["server.py", "--remote", "--setup",
                                           "--config", str(config_path)]), \
                    patch("sys.stdin.isatty", return_value=True), \
                    patch("builtins.input", side_effect=["unit.invalid", "", "", "", "", str(cache)]), \
                    patch("sync_service.getpass.getpass", return_value="unit-test-secret"), \
                    patch("sync_service.SyncService") as sync_factory, \
                    patch("server.ThreadingHTTPServer") as factory, \
                    patch("sys.stdout", io.StringIO()):
                sync = sync_factory.return_value
                sync.root = cache
                service = factory.return_value
                service.server_address = ("127.0.0.1", 8765)
                service.serve_forever.side_effect = KeyboardInterrupt
                monitor.main()
                sync.start.assert_called_once()
                sync.stop.assert_called_once()
                self.assertEqual(sync_factory.call_args.args[1], "unit-test-secret")
                saved = config_path.read_text()
                self.assertNotIn("unit-test-secret", saved)
                self.assertEqual(json.loads(saved)["host"], "unit.invalid")

    def test_headless_open_skips_browser_and_starts_service(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch.object(sys, "argv", ["server.py", "--data-dir", directory,
                                           "--host", "0.0.0.0", "--open"]), \
                    patch("server.sys.platform", "linux"), \
                    patch.dict("server.os.environ", {}, clear=True), \
                    patch("server.webbrowser.open") as browser, \
                    patch("server.ThreadingHTTPServer") as factory, \
                    patch("sys.stdout", output):
                service = factory.return_value
                service.server_address = ("0.0.0.0", 8765)
                service.serve_forever.side_effect = KeyboardInterrupt
                monitor.main()
                self.assertEqual(factory.call_args.args[0], ("0.0.0.0", 8765))
                browser.assert_not_called()
                service.serve_forever.assert_called_once()
                service.server_close.assert_called_once()
            self.assertIn("No desktop detected", output.getvalue())
            self.assertIn("LAN browser", output.getvalue())

    def test_browser_wait_cannot_block_http_service(self):
        served, finished = threading.Event(), threading.Event()
        blocked = []

        def browser_wait(_url):
            blocked.append(not served.wait(1))
            finished.set()

        def serve():
            served.set()
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, "argv", ["server.py", "--data-dir", directory, "--open"]), \
                    patch("server.sys.platform", "linux"), \
                    patch.dict("server.os.environ", {"DISPLAY": ":99"}, clear=True), \
                    patch("server.webbrowser.open", side_effect=browser_wait), \
                    patch("server.ThreadingHTTPServer") as factory, \
                    patch("sys.stdout", io.StringIO()):
                service = factory.return_value
                service.server_address = ("127.0.0.1", 8765)
                service.serve_forever.side_effect = serve
                monitor.main()
                self.assertTrue(finished.wait(2))
                self.assertEqual(blocked, [False])
                service.server_close.assert_called_once()

    def test_read_append_replace_and_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "回读.csv"
            initial = b"timestamp_ms,baseline_ch0\n1000,197\n"
            path.write_bytes(initial)
            outside = root.parent / (root.name + "-outside.csv")
            outside.write_bytes(b"private")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = build_opener(ProxyHandler({}))
            url = "http://127.0.0.1:" + str(server.server_address[1])

            def get(route):
                return opener.open(url + route, timeout=5)

            def read(offset=0, generation="", mtime=""):
                return get("/api/read?" + urlencode({
                    "name": path.name, "offset": offset,
                    "generation": generation, "mtime": mtime}))

            try:
                with get("/") as response:
                    self.assertIn("FEE".encode(), response.read())
                with get("/api/files") as response:
                    self.assertEqual(json.load(response)["files"][0]["name"], path.name)
                with read() as response:
                    self.assertEqual(response.read(), initial)
                    offset = int(response.headers["X-Next-Offset"])
                    generation = response.headers["X-Generation"]
                    mtime = response.headers["X-Mtime"]
                with path.open("ab") as stream:
                    stream.write(b"2000,198")
                with read(offset, generation, mtime) as response:
                    self.assertEqual(response.read(), b"2000,198")
                    self.assertEqual(response.headers["X-Reset"], "0")
                    offset = int(response.headers["X-Next-Offset"])
                    mtime = response.headers["X-Mtime"]
                with path.open("ab") as stream:
                    stream.write(b"\n")
                with read(offset, generation, mtime) as response:
                    self.assertEqual(response.read(), b"\n")
                    offset = int(response.headers["X-Next-Offset"])
                    mtime = response.headers["X-Mtime"]
                with read(offset, generation, mtime) as response:
                    self.assertEqual(response.read(), b"")
                    self.assertEqual(response.headers["X-Reset"], "0")
                path.write_bytes(initial)
                with read(offset, generation, mtime) as response:
                    self.assertEqual(response.headers["X-Reset"], "1")
                    self.assertEqual(response.read(), initial)
                    offset = int(response.headers["X-Next-Offset"])
                    mtime = response.headers["X-Mtime"]
                path.write_bytes(initial.replace(b"197", b"199"))
                stat = path.stat()
                import os
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
                with read(offset, generation, mtime) as response:
                    self.assertEqual(response.headers["X-Reset"], "1")
                    self.assertIn(b"199", response.read())
                    mtime = response.headers["X-Mtime"]
                rotated = root / "new.csv"
                rotated.write_bytes(initial)
                rotated.replace(path)
                with read(offset, generation, mtime) as response:
                    self.assertEqual(response.headers["X-Reset"], "1")
                path.write_bytes(b"x" * (CHUNK_SIZE + 1))
                with read() as response:
                    self.assertEqual(len(response.read()), CHUNK_SIZE)
                    self.assertEqual(response.headers["X-More"], "1")
                for route in ["/server.py", "/api/read?name=../" + outside.name,
                              "/api/read?name=missing.csv", "/api/read?name=x.txt",
                              "/api/read?name=x.csv&offset=-1"]:
                    with self.assertRaises(HTTPError) as error:
                        get(route)
                    error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                outside.unlink()


if __name__ == "__main__":
    unittest.main()
