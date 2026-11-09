"""IPC-side CSV mirroring. No writes or shell commands are sent to the Pi."""
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import threading
import time

from sftp_reader import SFTPReader

DEFAULTS = {
    "host": "", "user": "pi", "port": 22, "auth": "password",
    "remote_path": "/home/pi/seastar-daq/fee-monitor/data",
    "local_dir": "~/.local/share/fee-monitor/mirror", "identity_file": "",
    "poll_seconds": 5, "scan_seconds": 30, "limit_kib_per_second": 128,
}

def stream_key(name):
    match = re.match(r"readback_fee_(\d+)(?:_|\.csv$)", PurePosixPath(name).name, re.IGNORECASE)
    return "fee_" + str(int(match.group(1))) if match else "other"


def stream_label(key):
    if key.startswith("fee_"):
        num = int(key[4:])
        return f"G{num} · FEE {num}" if 0 <= num <= 6 else f"FEE {num}"
    return "其他 CSV"


def newest_key(name, attributes):
    live = bool(re.fullmatch(r"readback_fee_\d+\.csv", PurePosixPath(name).name, re.IGNORECASE))
    return attributes.get("mtime", 0), live, name


def validate_config(values):
    config = DEFAULTS.copy()
    config.update(values)
    if any("password" in key.lower() for key in config):
        raise ValueError("配置不能保存密码；请在启动时隐藏输入")
    if not isinstance(config["host"], str) or not config["host"] or re.search(r"[\s/@\x00]", config["host"]) or config["host"].startswith("-"):
        raise ValueError("请在 config.json 填写正确的树莓派 IP 或 SSH 主机名")
    if not isinstance(config["user"], str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", config["user"]):
        raise ValueError("SSH 用户名无效")
    if not isinstance(config["port"], int) or not 1 <= config["port"] <= 65535:
        raise ValueError("SSH 端口必须是 1–65535")
    if not isinstance(config["remote_path"], str):
        raise ValueError("remote_path 必须是路径字符串")
    remote = PurePosixPath(config["remote_path"])
    if not remote.is_absolute() or ".." in remote.parts or str(remote) == "/":
        raise ValueError("remote_path 必须是 CSV 文件或数据目录的绝对路径")
    config["remote_path"] = str(remote)
    if config["auth"] not in ("password", "key"):
        raise ValueError("auth 必须是 password 或 key")
    for key, minimum, maximum in [("poll_seconds", 2, 3600), ("scan_seconds", 5, 3600),
                                  ("limit_kib_per_second", 1, 10240)]:
        if not isinstance(config[key], (int, float)) or not minimum <= config[key] <= maximum:
            raise ValueError(key + " 超出允许范围")
    config["local_dir"] = str(Path(config["local_dir"]).expanduser().resolve())
    return config


def load_config(path):
    return validate_config(json.loads(Path(path).read_text(encoding="utf-8")))


def setup_config(path):
    config = DEFAULTS.copy()
    if Path(path).exists():
        config.update(json.loads(Path(path).read_text(encoding="utf-8")))
    print("在工控机配置树莓派连接；配置不保存登录密码。")
    prompts = [("host", "树莓派 IP / SSH 主机名"),
               ("user", "SSH 用户名"), ("port", "SSH 端口"),
               ("remote_path", "树莓派 CSV 目录或单个 CSV 的绝对路径")]
    for key, label in prompts:
        value = input(label + " [" + str(config[key]) + "]: ").strip()
        if value:
            config[key] = int(value) if key == "port" else value
    default = "2" if config.get("auth") == "key" else "1"
    while True:
        choice = input("登录方式：1=密码登录（稍后隐藏输入密码），2=SSH密钥 [" + default + "]: ").strip().lower() or default
        if choice in ("1", "password", "2", "key"):
            config["auth"] = "password" if choice in ("1", "password") else "key"
            break
        print("此处只选择 1 或 2，不要输入密码；密码将在后面单独隐藏输入。")
    value = input("工控机镜像目录 [" + str(config["local_dir"]) + "]: ").strip()
    if value:
        config["local_dir"] = value
    config = validate_config(config)
    path = Path(path)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return config


def login_password(config):
    return getpass.getpass("树莓派 SSH 密码（仅保留在内存，输入不回显）: ") if config["auth"] == "password" else None


class SyncService:
    def __init__(self, config, password=None, client_factory=None):
        self.config = config
        self.password = password
        self.client_factory = client_factory
        identity = json.dumps([config[k] for k in ("host", "port", "user", "remote_path")])
        source_id = hashlib.sha256(identity.encode()).hexdigest()[:12]
        self.root = Path(config["local_dir"]) / source_id
        self.internal = self.root / ".sync"
        self.internal.mkdir(parents=True, exist_ok=True)
        self.lock_file = (self.internal / "lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise ValueError("此镜像目录已被另一个同步进程使用")
        self.state_path = self.internal / "progress.json"
        self.state = {"initialized": False, "seen": [], "files": {}, "current": None, "latest": {}}
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))
        self.stop_event = threading.Event()
        self.mutex = threading.Lock()
        self.client = None
        self.thread = None
        self.verified = set()
        self.active_until = {}
        self.next_scan = self.next_poll = 0
        self.copy_cursor = 0
        self.info = {"enabled": True, "phase": "connecting", "connected": False,
                     "message": "正在连接树莓派", "last_success_ms": None,
                     "last_data_ms": None, "session_bytes": 0,
                     "source": config["user"] + "@" + config["host"] + ":" + config["remote_path"],
                     "poll_seconds": config["poll_seconds"], "scan_seconds": config["scan_seconds"],
                     "limit_kib_per_second": config["limit_kib_per_second"]}

    def _update(self, **fields):
        with self.mutex:
            self.info.update(fields)

    def status(self):
        with self.mutex:
            return dict(self.info)

    def _save(self):
        temporary = self.state_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.state_path)

    def _local(self, relative):
        path = (self.root / relative).resolve()
        path.relative_to(self.root.resolve())
        return path

    def _remote(self, relative):
        remote = self.config["remote_path"]
        return remote if remote.lower().endswith(".csv") else posixpath.join(remote, relative)

    def _walk(self):
        root = self.config["remote_path"]
        if root.lower().endswith(".csv"):
            return {posixpath.basename(root): self.client.stat(root)}
        pending, result, visited = [("", 0)], {}, 0
        while pending:
            relative, depth = pending.pop()
            visited += 1
            if visited > 128 or depth > 6:
                raise OSError("数据目录过大或过深；请将 remote_path 缩小到实际 CSV 目录")
            for name, attributes in self.client.listdir(posixpath.join(root, relative)):
                if name.startswith("."):
                    continue
                path = posixpath.join(relative, name)
                mode = attributes.get("mode", 0)
                if stat.S_ISDIR(mode):
                    pending.append((path, depth + 1))
                elif stat.S_ISREG(mode) and name.lower().endswith(".csv"):
                    result[path] = attributes
        return result

    def _remember(self, name, attributes):
        files = self.state["files"]
        if name not in files:
            files[name] = {"offset": 0, "head": "", "tail": "", "size": 0, "mtime": 0}
        record = files[name]
        size, mtime = attributes.get("size", 0), attributes.get("mtime", 0)
        if (size == record["size"] and mtime != record["mtime"]) or size < record["offset"]:
            self.verified.discard(name)
        if size != record["size"] or mtime != record["mtime"]:
            self.active_until[name] = time.monotonic() + 60
        record["size"], record["mtime"] = size, mtime
        record["missing"] = False

    def scan(self):
        found = self._walk()
        seen = set(self.state["seen"])
        if found:
            latest = max(found, key=lambda name: newest_key(name, found[name]))
            by_stream = {}
            for name, attributes in found.items():
                group = stream_key(name)
                previous = by_stream.get(group)
                if previous is None or newest_key(name, attributes) > newest_key(previous, found[previous]):
                    by_stream[group] = name
            self.state["latest"].update(by_stream)
            latest_files = set(by_stream.values())
            for name, attributes in found.items():
                # Promote every FEE's latest file even if an older version marked it seen.
                if (self.state["initialized"] and name not in seen) or name in latest_files or name in self.state["files"]:
                    self._remember(name, attributes)
            self.state["current"] = latest
        for name, record in self.state["files"].items():
            record["missing"] = name not in found
        self.state["seen"] = sorted(seen | set(found))
        self.state["initialized"] = True
        self.next_scan = time.monotonic() + self.config["scan_seconds"]
        self.next_poll = time.monotonic() + self.config["poll_seconds"]
        self._save()

    def _replace(self, name):
        path = self._local(name)
        if path.exists():
            archive = self.root / ".archive" / (str(time.time_ns()) + "-" + path.name)
            archive.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, archive)
        record = self.state["files"][name]
        record.update(offset=0, head="", tail="")
        self.verified.discard(name)
        self._save()

    def _check_local(self, name):
        record = self.state["files"][name]
        path = self._local(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        length = path.stat().st_size if path.exists() else 0
        if length < record["offset"]:
            self._replace(name)
        elif length > record["offset"]:
            # Bytes without a committed checkpoint are replayed after a crash.
            with path.open("r+b") as stream:
                stream.truncate(record["offset"])

    def _copy(self, name, budget):
        record = self.state["files"][name]
        self._check_local(name)
        if record["size"] < record["offset"]:
            self._replace(name)
        handle = self.client.open(self._remote(name))
        started = time.monotonic()
        read_bytes = 0
        try:
            mismatch = False
            if name not in self.verified and record["head"]:
                head = bytes.fromhex(record["head"])
                actual = self.client.read_at(handle, 0, len(head))
                read_bytes += len(actual)
                mismatch = actual != head
            if record["offset"] and record["tail"]:
                tail = bytes.fromhex(record["tail"])
                actual = self.client.read_at(handle, record["offset"] - len(tail), len(tail))
                read_bytes += len(actual)
                mismatch = mismatch or actual != tail
            if mismatch:
                self._replace(name)
            self.verified.add(name)
            path = self._local(name)
            mode = "r+b" if path.exists() else "w+b"
            copied = 0
            with path.open(mode) as target:
                target.seek(record["offset"])
                while copied < budget and record["offset"] < record["size"] and not self.stop_event.is_set():
                    length = min(32768, budget - copied, record["size"] - record["offset"])
                    data = self.client.read_at(handle, record["offset"], length)
                    if not data:
                        raise OSError("源文件正在切换或截断，将重新检查后续传")
                    target.write(data)
                    target.flush()
                    os.fsync(target.fileno())  # IPC storage only.
                    if not record["head"]:
                        record["head"] = data[:512].hex()
                    record["tail"] = (bytes.fromhex(record["tail"]) + data)[-64:].hex()
                    record["offset"] += len(data)
                    copied += len(data)
                    read_bytes += len(data)
                    self._save()
                    self._update(last_data_ms=int(time.time() * 1000),
                                 session_bytes=self.status()["session_bytes"] + len(data))
                    wait = read_bytes / (self.config["limit_kib_per_second"] * 1024) - (time.monotonic() - started)
                    if wait > 0:
                        self.stop_event.wait(wait)
            return copied
        finally:
            self.client.close_handle(handle)

    def cycle(self):
        now = time.monotonic()
        if now >= self.next_scan:
            self.scan()
        files, current = self.state["files"], self.state["current"]
        latest_files = set(self.state["latest"].values())
        if now >= self.next_poll:
            for name in files:
                if not files[name].get("missing") and (name in latest_files or self.active_until.get(name, 0) >= now):
                    try:
                        self._remember(name, self.client.stat(self._remote(name)))
                    except FileNotFoundError:
                        files[name]["missing"] = True
                        self.next_scan = 0
            self.next_poll = time.monotonic() + self.config["poll_seconds"]
        copied, budget = 0, 256 * 1024
        queue = [name for name in sorted(files, key=lambda n: (n in latest_files, files[n]["mtime"], n), reverse=True)
                 if not files[name].get("missing") and
                 (files[name]["size"] != files[name]["offset"] or name not in self.verified or not self._local(name).exists())]
        if queue:
            start = self.copy_cursor % len(queue)
            queue = queue[start:] + queue[:start]
            processed = 0
            # Share one global limit fairly, so a large FEE cannot hide other FEEs.
            for name in queue:
                record = files[name]
                self._update(phase="syncing", message="正在同步新增内容 / 限速补齐历史")
                try:
                    copied += self._copy(name, min(budget - copied, 32768 if len(queue) > 1 else budget))
                except FileNotFoundError:
                    record["missing"] = True
                    self.next_scan = 0
                processed += 1
                if copied >= budget or self.stop_event.is_set():
                    break
            self.copy_cursor = start + processed
        remaining = sum(max(0, row["size"] - row["offset"]) for row in files.values() if not row.get("missing"))
        unavailable = sum(bool(row.get("missing")) for row in files.values())
        selected = files.get(current, {})
        streams = []
        for key, name in sorted(self.state["latest"].items(),
                                key=lambda item: (item[0] == "other", int(item[0][4:]) if item[0].startswith("fee_") else 0)):
            record = files[name]
            streams.append({"id": key, "label": stream_label(key),
                            "remote_file": name, "file": name if self._local(name).exists() else None,
                            "available": not record.get("missing"),
                            "source_bytes": record["size"], "mirrored_bytes": record["offset"],
                            "pending_bytes": max(0, record["size"] - record["offset"])})
        self._update(connected=True, phase="syncing" if remaining else "warning" if unavailable else "waiting",
                     message="正在限速补齐历史" if remaining else
                     "部分源文件暂时缺失，保留本地缓存并等待文件恢复" if unavailable else "已连接，等待新增记录",
                     last_success_ms=int(time.time() * 1000),
                     pending_bytes=remaining, unavailable_files=unavailable,
                     current_file=current if current and self._local(current).exists() else None,
                     source_bytes=selected.get("size", 0), mirrored_bytes=selected.get("offset", 0),
                     streams=streams)
        return copied, remaining

    def _run(self):
        delay = 5
        while not self.stop_event.is_set():
            try:
                self._update(phase="connecting", connected=False, message="正在连接树莓派")
                self.client = self.client_factory(self.config, self.password) if self.client_factory else \
                    SFTPReader.connect(self.config, self.password, cancel_event=self.stop_event)
                self._update(connected=True)
                self.verified.clear()
                self.next_scan = self.next_poll = 0
                while not self.stop_event.is_set():
                    try:
                        copied, remaining = self.cycle()
                    except FileNotFoundError:
                        self._update(connected=True, phase="error", message="远端文件暂时不存在，保留本地缓存并等待重试")
                        self.next_scan = 0
                        self.stop_event.wait(self.config["poll_seconds"])
                        continue
                    delay = 5
                    if not copied:
                        self.stop_event.wait(max(0.1, min(self.next_poll, self.next_scan) - time.monotonic()))
            except (OSError, ValueError) as error:
                detail = str(error)
                if self.client:
                    self.client.close()
                    self.client = None
                if "Host key verification failed" in detail:
                    detail = "SSH 主机尚未确认或主机密钥已变更，请先在工控机终端正常 SSH 登录并核实主机"
                self._update(phase="error", connected=False, message=detail)
                if "Permission denied" in detail:
                    self._update(message="SSH 登录失败，请检查密码或密钥后重新启动监控")
                    self.stop_event.wait()
                else:
                    self.stop_event.wait(delay)
                    delay = min(60, delay * 2)
            finally:
                if self.client:
                    self.client.close()
                    self.client = None
        self._update(phase="stopped", connected=False, message="同步已停止；本地缓存可继续查看")

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        if self.lock_file.closed:
            return
        self.stop_event.set()
        if self.client:
            self.client.close()
        if self.thread:
            self.thread.join(timeout=3)
        self.password = None
        if self.thread and self.thread.is_alive():
            return  # Keep the cache lock until process exit if local storage is stuck.
        fcntl.flock(self.lock_file, fcntl.LOCK_UN)
        self.lock_file.close()
