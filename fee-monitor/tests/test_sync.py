import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sftp_reader import SFTPReader
from sync_service import DEFAULTS, SyncService, load_config, setup_config

SFTP_SERVER = next((p for p in [
    "/usr/libexec/sftp-server", "/usr/lib/openssh/sftp-server",
    "/usr/lib/ssh/sftp-server", shutil.which("sftp-server")
] if p and Path(p).is_file()), None)


class CountingReader(SFTPReader):
    def __init__(self):
        self.reads = []
        self.opens = 0
        process = subprocess.Popen([SFTP_SERVER, "-R"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        super().__init__(process, timeout=3)

    def read_at(self, handle, offset, length):
        self.reads.append((offset, length))
        return super().read_at(handle, offset, length)

    def open(self, path):
        self.opens += 1
        return super().open(path)


class ConfigurationTest(unittest.TestCase):
    def test_setup_reprompts_auth_and_never_saves_misentered_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            old = dict(DEFAULTS, host="unit.invalid", auth="old-misentered-secret",
                       local_dir=directory)
            path.write_text(json.dumps(old))
            with patch("builtins.input", side_effect=["", "", "", "", "new-misentered-secret", "1", ""]), \
                    patch("builtins.print") as output:
                config = setup_config(path)
            self.assertEqual(config["auth"], "password")
            self.assertNotIn("misentered-secret", path.read_text())
            self.assertNotIn("misentered-secret", str(output.call_args_list))
            self.assertEqual(config["host"], "unit.invalid")

    def test_invalid_setup_leaves_existing_config_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = json.dumps(dict(DEFAULTS, host="unit.invalid", local_dir=directory))
            path.write_text(original)
            with patch("builtins.input", side_effect=["", "", "", "/", "1", ""]), \
                    patch("builtins.print"), self.assertRaises(ValueError):
                setup_config(path)
            self.assertEqual(path.read_text(), original)

    def test_config_validation_and_password_not_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = dict(DEFAULTS, host="pi.example", local_dir=directory)
            path.write_text(json.dumps(config))
            self.assertEqual(load_config(path)["poll_seconds"], 5)
            config["password"] = "unit-test-secret"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "不能保存密码"):
                load_config(path)

    def test_ssh_uses_known_hosts_and_password_is_not_in_arguments(self):
        config = dict(DEFAULTS, host="pi.example")
        with patch("sftp_reader.shutil.which", return_value="/usr/bin/ssh"), \
                patch("sftp_reader.subprocess.Popen") as launch, \
                patch.object(SFTPReader, "__init__", return_value=None):
            SFTPReader.connect(config, "unit-test-secret")
            args, options = launch.call_args.args[0], launch.call_args.kwargs
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("Compression=no", args)
            self.assertNotIn("unit-test-secret", str(args))
            self.assertEqual(options["env"]["FEE_MONITOR_SSH_PASSWORD"], "unit-test-secret")
            self.assertTrue(options["start_new_session"])
            self.assertEqual(args[-3:], ["--", "pi@pi.example", "sftp"])
        helper = Path(__file__).resolve().parents[1] / "ssh_askpass.py"
        self.assertTrue(helper.stat().st_mode & 0o111)

    def test_cancel_during_handshake_closes_child_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, bufsize=0)
        cancel = threading.Event()
        timer = threading.Timer(0.05, cancel.set)
        timer.start()
        try:
            with self.assertRaisesRegex(OSError, "已停止"):
                SFTPReader(process, cancel_event=cancel)
            self.assertIsNotNone(process.poll())
        finally:
            timer.join()
            if process.poll() is None:
                process.kill()
                process.wait()


@unittest.skipUnless(SFTP_SERVER, "OpenSSH sftp-server not installed on the development host")
class MirrorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.config = dict(DEFAULTS, host="unit.invalid", remote_path=str(self.source),
                           local_dir=str(self.root / "mirror"), limit_kib_per_second=10240)
        self.service = SyncService(self.config)
        self.client = CountingReader()
        self.service.client = self.client

    def tearDown(self):
        self.service.stop()
        self.temporary.cleanup()

    def poll(self, scan=False):
        self.service.next_poll = 0
        if scan:
            self.service.next_scan = 0
        return self.service.cycle()

    def write(self, name, data, modified):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (modified, modified))
        return path

    def test_real_sftp_append_restart_rotation_and_minimal_reads(self):
        old = self.write("old.csv", b"timestamp_ms,x\n100,1\n", 1000)
        content = b"timestamp_ms,x\n" + b"1000,197\n" * 10000
        latest = self.write("2026-10-26/回读.csv", content, 2000)
        self.poll(scan=True)
        relative = "2026-10-26/回读.csv"
        mirror = self.service.root / relative
        self.assertEqual(mirror.read_bytes(), content)
        self.assertFalse((self.service.root / old.name).exists())
        self.assertEqual(self.service.status()["pending_bytes"], 0)
        before = len(self.client.reads), self.client.opens
        self.poll()
        self.assertEqual((len(self.client.reads), self.client.opens), before)
        with latest.open("ab") as stream:
            stream.write(b"2000,198")  # Partial row is copied byte-for-byte.
        self.poll()
        self.assertEqual(mirror.read_bytes(), content + b"2000,198")
        with latest.open("ab") as stream:
            stream.write(b"\n")
        self.poll()
        self.assertEqual(mirror.read_bytes(), latest.read_bytes())
        expected = mirror.read_bytes()
        # Crash after local write but before committing a checkpoint.
        with mirror.open("ab") as stream:
            stream.write(b"uncommitted")
        self.service.stop()
        self.service = SyncService(self.config)
        self.client = CountingReader()
        self.service.client = self.client
        self.poll(scan=True)
        self.assertEqual(mirror.read_bytes(), expected)
        self.assertLessEqual(sum(size for _, size in self.client.reads), 1024)
        self.assertEqual(self.service.status()["session_bytes"], 0)
        replacement = b"timestamp_ms,x\n3000,201\n"
        self.write(relative, replacement, 3000)
        self.poll()
        self.assertEqual(mirror.read_bytes(), replacement)
        self.assertIn(expected, [p.read_bytes() for p in (self.service.root / ".archive").glob("*.csv")])
        # Growing overwrite must not be mistaken for append.
        replacement = b"timestamp_ms,x\n" + b"4000,202\n" * 100
        self.write(relative, replacement, 4000)
        self.poll()
        self.assertEqual(mirror.read_bytes(), replacement)
        new_data = b"timestamp_ms,x\n5000,203\n"
        self.write("2026-10-27/new.csv", new_data, 5000)
        self.poll(scan=True)
        self.assertEqual(self.service.status()["current_file"], "2026-10-27/new.csv")
        self.assertEqual((self.service.root / "2026-10-27/new.csv").read_bytes(), new_data)
        self.assertNotIn("password", self.service.state_path.read_text())

    def test_same_size_replacement_missing_cache_and_single_writer_lock(self):
        path = self.write("live.csv", b"timestamp_ms,x\n1000,1\n", 1000)
        self.poll(scan=True)
        with self.assertRaisesRegex(ValueError, "另一个同步进程"):
            SyncService(self.config)
        self.write(path.name, b"timestamp_ms,x\n2000,2\n", 2000)
        self.poll()
        mirror = self.service.root / path.name
        self.assertEqual(mirror.read_bytes(), path.read_bytes())
        mirror.unlink()
        self.poll()
        self.assertEqual(mirror.read_bytes(), path.read_bytes())

    def test_ssh_disconnect_recovers_and_source_is_unchanged(self):
        source = self.write("live.csv", b"timestamp_ms,x\n1000,1\n", 1000)
        expected = source.read_bytes(), source.stat().st_mtime_ns
        self.client.close()
        self.service.client = None
        calls, phases = [], []
        original_update = self.service._update

        def update(**fields):
            phases.append(fields.get("phase"))
            original_update(**fields)

        def connect(*_args):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("simulated disconnect")
            return CountingReader()

        original_wait = self.service.stop_event.wait
        self.service.stop_event.wait = lambda seconds=None: original_wait(min(seconds or 0, 0.01))
        self.service._update = update
        self.service.client_factory = connect
        self.service.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.service.status().get("connected"):
            time.sleep(0.01)
        self.assertTrue(self.service.status()["connected"])
        self.assertIn("error", phases)
        self.assertGreaterEqual(len(calls), 2)
        self.service.stop()
        self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), expected)

    def test_single_file_and_transfer_throttle(self):
        source = self.write("fixed.csv", b"timestamp_ms,x\n" + b"1000,1\n" * 1000, 1000)
        self.service.stop()
        self.config.update(remote_path=str(source), limit_kib_per_second=1)
        self.service = SyncService(self.config)
        self.client = CountingReader()
        self.service.client = self.client

        def no_directory_scan(_path):
            raise AssertionError("single-file mode must not scan directories")

        self.client.listdir = no_directory_scan
        waits = []
        self.service.stop_event.wait = lambda seconds: waits.append(seconds) or False
        self.poll(scan=True)
        self.assertEqual((self.service.root / source.name).read_bytes(), source.read_bytes())
        self.assertGreater(sum(waits), source.stat().st_size / 1024 - 0.5)

    def test_missing_old_source_does_not_block_new_file(self):
        old = self.write("old.csv", b"timestamp_ms,x\n" + b"1000,1\n" * 50000, 1000)
        self.poll(scan=True)
        self.assertGreater(self.service.status()["pending_bytes"], 0)
        new_data = b"timestamp_ms,x\n2000,2\n"
        self.write("new.csv", new_data, 2000)
        old.unlink()
        self.poll(scan=True)
        self.assertEqual((self.service.root / "new.csv").read_bytes(), new_data)
        self.assertEqual(self.service.status()["current_file"], "new.csv")
        self.assertEqual(self.service.status()["unavailable_files"], 1)

    def test_each_fee_latest_file_and_migration_from_single_fee_cache(self):
        live = {}
        for fee in (3, 5, 7, 8):
            live[fee] = self.write("readback_fee_" + str(fee) + ".csv",
                                   b"timestamp_ms,x\n" + b"1000,1\n" * 10000, 2000)
        for fee in (3, 5):
            self.write("readback_fee_" + str(fee) + "_10261246-10261716.csv",
                       b"timestamp_ms,x\n1000,9\n", 2000)
        self.write("data_fee_3_run.dat", b"must-not-transfer", 3000)
        # Previous releases recorded all names as seen, but tracked only FEE 8.
        self.service.state.update(initialized=True, seen=[p.name for p in self.source.glob("*.csv")],
                                  current="readback_fee_8.csv")
        self.service._remember("readback_fee_8.csv", self.client.stat(str(live[8])))
        self.poll(scan=True)
        streams = self.service.status()["streams"]
        self.assertEqual([s["id"] for s in streams], ["fee_3", "fee_5", "fee_7", "fee_8"])
        for item in streams:
            self.assertGreater(item["mirrored_bytes"], 0)
            self.assertEqual(item["remote_file"], "readback_" + item["id"] + ".csv")
        while self.service.status()["pending_bytes"]:
            self.poll()
        for fee, path in live.items():
            self.assertEqual((self.service.root / path.name).read_bytes(), path.read_bytes())
        self.assertFalse((self.service.root / "data_fee_3_run.dat").exists())
        self.assertFalse(list(self.service.root.glob("*10261246*")))
        before = len(self.client.reads)
        self.poll()
        self.assertEqual(len(self.client.reads), before)
        # Poll every FEE's latest file even after the initial active timeout.
        self.service.active_until = {}
        with live[3].open("ab") as target:
            target.write(b"2000,2\n")
        self.poll()
        self.assertEqual((self.service.root / live[3].name).read_bytes(), live[3].read_bytes())


if __name__ == "__main__":
    unittest.main()
