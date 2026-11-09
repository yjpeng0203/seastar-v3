"""Read-only SFTP v3 operations over the installed OpenSSH client.

SSH handles encryption, authentication and known_hosts. This module only frames
STAT, OPEN(READ), READ, OPENDIR, READDIR and CLOSE; no remote write operations.
"""
import os
from pathlib import Path
import select
import shutil
import struct
import subprocess
import time


def u32(value):
    return struct.pack(">I", value)


def string(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return u32(len(value)) + value


class Packet:
    def __init__(self, data):
        self.data = data
        self.position = 0

    def take(self, size):
        end = self.position + size
        if size < 0 or end > len(self.data):
            raise OSError("无效的 SFTP 响应")
        data = self.data[self.position:end]
        self.position = end
        return data

    def integer(self):
        return struct.unpack(">I", self.take(4))[0]

    def text(self):
        return self.take(self.integer())

    def attributes(self):
        flags = self.integer()
        result = {}
        if flags & 1:
            result["size"] = struct.unpack(">Q", self.take(8))[0]
        if flags & 2:
            self.take(8)
        if flags & 4:
            result["mode"] = self.integer()
        if flags & 8:
            self.integer()
            result["mtime"] = self.integer()
        if flags & 0x80000000:
            for _ in range(self.integer()):
                self.text()
                self.text()
        return result


class SFTPReader:
    def __init__(self, process, timeout=20, cancel_event=None):
        self.process = process
        self.timeout = timeout
        self.cancel_event = cancel_event
        self.request_id = 0
        self.errors = b""
        self.stderr_open = True
        try:
            self._send(1, u32(3))
            kind, packet = self._receive()
            if kind != 2 or packet.integer() != 3:
                raise OSError("树莓派 SSH 服务需要支持 SFTP v3")
        except Exception:
            self.close()
            raise

    @classmethod
    def connect(cls, config, password=None, cancel_event=None):
        if not shutil.which("ssh"):
            raise OSError("工控机未安装 OpenSSH 客户端，请先安装 openssh-client")
        command = ["ssh", "-T", "-s", "-p", str(config["port"]),
                   "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
                   "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=2",
                   "-o", "Compression=no", "-o", "LogLevel=ERROR",
                   "-o", "NumberOfPasswordPrompts=1"]
        environment = os.environ.copy()
        environment.pop("FEE_MONITOR_SSH_PASSWORD", None)
        if password is None:
            command += ["-o", "BatchMode=yes"]
        else:
            command += ["-o", "PreferredAuthentications=password"]
            environment.update({
                "SSH_ASKPASS": str(Path(__file__).with_name("ssh_askpass.py")),
                "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": "fee-monitor",
                "FEE_MONITOR_SSH_PASSWORD": password,
            })
        if config.get("identity_file"):
            command += ["-i", str(Path(config["identity_file"]).expanduser())]
        command += ["--", config["user"] + "@" + config["host"], "sftp"]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, bufsize=0, env=environment,
                                   start_new_session=True)
        return cls(process, cancel_event=cancel_event)

    def _read(self, size):
        result = bytearray()
        deadline = time.monotonic() + self.timeout
        while len(result) < size:
            if self.cancel_event and self.cancel_event.is_set():
                raise OSError("同步已停止")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = self.errors.decode("utf-8", "replace").strip()
                raise TimeoutError(detail or "SSH/SFTP 读取超时")
            streams = []
            if self.stderr_open:
                streams.append(self.process.stderr)
            streams.append(self.process.stdout)
            ready, _, _ = select.select(streams, [], [], min(remaining, 0.5))
            for stream in ready:
                if stream is self.process.stderr:
                    part = os.read(stream.fileno(), 4096)
                    self.stderr_open = bool(part)
                    self.errors = (self.errors + part)[-8192:]
                else:
                    part = os.read(stream.fileno(), size - len(result))
                    if not part:
                        detail = self.errors.decode("utf-8", "replace").strip()
                        raise OSError(detail or "SSH/SFTP 连接已断开")
                    result.extend(part)
        return bytes(result)

    def _send(self, kind, body):
        data = bytes([kind]) + body
        self.process.stdin.write(u32(len(data)) + data)
        self.process.stdin.flush()

    def _receive(self):
        size = struct.unpack(">I", self._read(4))[0]
        if size < 1 or size > 16 * 1024 * 1024:
            raise OSError("无效的 SFTP 数据包长度")
        data = self._read(size)
        return data[0], Packet(data[1:])

    def _request(self, kind, body):
        self.request_id += 1
        self._send(kind, u32(self.request_id) + body)
        response, packet = self._receive()
        if packet.integer() != self.request_id:
            raise OSError("SFTP 请求编号不匹配")
        if response == 101:
            code = packet.integer()
            detail = packet.text().decode("utf-8", "replace")
            if code == 1:
                return None
            if code == 2:
                raise FileNotFoundError(detail)
            if code == 3:
                raise PermissionError(detail)
            if code:
                raise OSError("SFTP: " + detail)
        return response, packet

    def stat(self, path):
        response = self._request(17, string(path))
        if not response or response[0] != 105:
            raise OSError("无法获取远端文件状态")
        return response[1].attributes()

    def open(self, path):
        response = self._request(3, string(path) + u32(1) + u32(0))
        if not response or response[0] != 102:
            raise OSError("无法只读打开远端文件")
        return response[1].text()

    def read_at(self, handle, offset, length):
        response = self._request(5, string(handle) + struct.pack(">Q", offset) + u32(length))
        if response is None:
            return b""
        if response[0] != 103:
            raise OSError("无效的远端文件数据")
        data = response[1].text()
        if len(data) > length:
            raise OSError("SFTP 返回数据超过请求长度")
        return data

    def close_handle(self, handle):
        self._request(4, string(handle))

    def listdir(self, path):
        response = self._request(11, string(path))
        if not response or response[0] != 102:
            raise OSError("无法打开远端数据目录")
        handle = response[1].text()
        result = []
        try:
            while True:
                response = self._request(12, string(handle))
                if response is None:
                    break
                if response[0] != 104:
                    raise OSError("无效的远端目录响应")
                packet = response[1]
                for _ in range(packet.integer()):
                    name = packet.text().decode("utf-8")
                    packet.text()  # SFTP v3 longname is informational only.
                    attributes = packet.attributes()
                    if name not in (".", "..") and "/" not in name and "\x00" not in name:
                        result.append((name, attributes))
        finally:
            self.close_handle(handle)
        return result

    def close(self):
        process = self.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()
