"""Read-only incremental DAT framing, run discovery, and closed ROOT replay."""
import base64
import hashlib
import os
from pathlib import Path
import re
import struct

import numpy as np
from mirror_state import committed_files

NAME = re.compile(r"^(.*_run\d+)_idx(\d+)_(\d{14})\.dat$", re.I)
FEE_ID = re.compile(r"(?:^|[_/])fee_(\d+)(?:[_.]|$)", re.I)
STRING_DIR = re.compile(r"^g(\d+)$", re.I)
DEFAULT_BOARD_STRINGS = {i: f"G{i}" for i in range(7)}


def infer_string_and_board(relative, board_map=None):
    mapping = DEFAULT_BOARD_STRINGS if board_map is None else board_map
    explicit_group = ""
    for part in relative.parts[:-1]:
        m = STRING_DIR.match(part)
        if m:
            explicit_group = f"G{int(m.group(1))}"
            break
    m_fee = FEE_ID.search(relative.name)
    board_id = int(m_fee.group(1)) if m_fee else None
    if explicit_group:
        return explicit_group, board_id
    if board_id is not None:
        mapped = mapping.get(board_id) or mapping.get(str(board_id))
        return mapped or f"FEE{board_id}", board_id
    return "", None


def discover(directory, board_map=None):
    root = Path(directory).resolve()
    commits = committed_files(root)
    groups = {}
    skip_dirs = {"online", "organizer_work", "csv"}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part.startswith((".", "results")) or part in skip_dirs for part in relative.parts[:-1]):
            continue
        if path.suffix.lower() not in (".dat", ".root") or not path.is_file():
            continue
        if path.name.lower().startswith("trigger_signal_"):
            continue  # Trigger sidecars are not waveform streams.
        try:
            path.resolve().relative_to(root)
            stat = path.stat()
        except (ValueError, OSError):
            continue
        if path.suffix.lower() == ".root" and not path.name.startswith("data_"):
            continue
        visible_size=stat.st_size
        if commits is not None:
            # Receiver mode exposes only files/bytes durably committed by the IPC writer.
            name=relative.as_posix()
            if name not in commits or path.suffix.lower()!='.dat':
                continue
            visible_size=commits[name]['committed']
            if stat.st_size<visible_size:
                raise ValueError("接收文件小于已提交水位: "+name)
        match = NAME.match(path.name)
        key = (relative.parent / (match[1] + "_" + match[3])).as_posix() if match else relative.as_posix()
        string_group, board_id = infer_string_and_board(relative, board_map)
        group = groups.setdefault(key, {"key": key, "kind": path.suffix[1:].lower(),
                                       "stamp": match[3] if match else "",
                                       "string_group": string_group, "board_id": board_id,
                                       "files": []})
        group["files"].append({"name": relative.as_posix(), "index": int(match[2]) if match else 1,
                               "size": visible_size, "mtime_ns": stat.st_mtime_ns})
    for group in groups.values():
        group["files"].sort(key=lambda f: (f["index"], f["name"]))
        group["bytes"] = sum(f["size"] for f in group["files"])
    return sorted(groups.values(), key=lambda g: (g["kind"] == "dat", g["stamp"],
                                                  max(f["mtime_ns"] for f in g["files"])), reverse=True)


class DatParser:
    def __init__(self, state=None):
        s = state or {}
        self.buffer = bytearray(base64.b64decode(s.get("buffer", "")))
        self.position = s.get("position", 0)
        self.bad_bytes = s.get("bad_bytes", 0)
        self.resyncs = s.get("resyncs", 0)
        self.orphans = s.get("orphans", 0)
        self.has_trigger = s.get("has_trigger", False)
        self.recovering = s.get("recovering", False)

    def state(self):
        return {"buffer": base64.b64encode(self.buffer).decode(), "position": self.position,
                "bad_bytes": self.bad_bytes, "resyncs": self.resyncs, "orphans": self.orphans,
                "has_trigger": self.has_trigger, "recovering": self.recovering}

    def feed(self, data):
        self.buffer.extend(data)
        offset = 0
        while len(self.buffer) - offset >= 16:
            head = self.buffer[offset:offset + 16]
            valid_trigger = (head[:2] == b"\xff\x12" and head[4:8] == b"\0" * 4
                             and struct.unpack_from(">I", head, 12)[0] < 1_000_000_000)
            if valid_trigger:
                number = struct.unpack_from(">H", head, 2)[0]
                second, nano = struct.unpack_from(">II", head, 8)
                self.has_trigger = True
                self.recovering = False
                yield {"type": "trigger", "position": self.position + offset, "number": number,
                       "ns": second * 1_000_000_000 + nano}
                offset += 16
                continue
            if head[0] == 254 and not self.recovering:
                second, nano, points = struct.unpack_from(">III", head, 4)
                valid_wave = head[3] == 0 and nano < 1_000_000_000 and 1 <= points <= 8192
                if valid_wave:
                    size = 16 + 2 * points
                    if len(self.buffer) - offset < size:
                        break  # Keep a partial payload, including across volume boundaries.
                    if self.has_trigger:
                        payload = bytes(self.buffer[offset + 16:offset + size])
                        yield {"type": "wave", "position": self.position + offset,
                               "board": head[1] >> 4, "channel": head[1] & 15, "wave_id": head[2],
                               "ns": second * 1_000_000_000 + nano,
                               "samples": np.frombuffer(payload, dtype=">u2")}
                    else:
                        self.orphans += 1
                    offset += size
                    continue
            # Require a validated trigger after corruption; waveform payloads may contain FE.
            if not self.recovering:
                self.resyncs += 1
                self.recovering = True
            self.bad_bytes += 1
            offset += 1
        if offset:
            del self.buffer[:offset]
            self.position += offset


class SourceChanged(RuntimeError):
    pass


class DatReader:
    def __init__(self, directory, state=None):
        self.directory = Path(directory).resolve()
        s = state or {}
        self.offsets = s.get("offsets", {})
        self.signatures = s.get("signatures", {})
        self.anchors = s.get("anchors", {})
        self.heads = s.get("heads", {})
        self.first_index = s.get("first_index")
        self.current = s.get("current")
        self.finished = False
        self.waiting_for_volume = None

    def state(self):
        return {"offsets": self.offsets, "signatures": self.signatures,
                "anchors": self.anchors, "heads": self.heads,
                "first_index": self.first_index, "current": self.current}

    def read(self, source, max_bytes=4 * 1024 * 1024):
        files = source["files"]
        commits = committed_files(self.directory)
        self.waiting_for_volume = None
        if self.first_index is None:
            self.first_index = files[0]["index"]
        names = {f["name"] for f in files}
        if set(self.offsets) - names:
            raise SourceChanged("已读取的分卷消失，停止以保护统计；恢复文件或建立新会话")
        expected = self.first_index
        current_index = next((f["index"] for f in files if f["name"] == self.current), self.first_index)
        for item in files:
            if item["index"] != expected:
                self.waiting_for_volume = expected
                return b""
            expected += 1
            name = item["name"]
            path = (self.directory / name).resolve()
            path.relative_to(self.directory)
            offset = self.offsets.get(name, 0)
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                visible_size=stat.st_size
                if commits is not None:
                    if name not in commits:
                        raise SourceChanged("接收清单不再包含已选文件: "+name)
                    visible_size=commits[name]['committed']
                    if stat.st_size<visible_size or [stat.st_dev,stat.st_ino]!=commits[name].get('local_identity'):
                        raise SourceChanged("已提交接收文件被更改: "+name)
                if visible_size >= 3 and stream.read(3) == b"\x1f\x8b\x08":
                    raise SourceChanged("文件名是 DAT，但内容是 gzip 压缩包: " + name +
                                        "；请核对原始 DAT，不能将压缩字节当波形读取；也可选择完整 ROOT 回放")
                signature = [stat.st_dev, stat.st_ino]
                if (name in self.signatures and signature != self.signatures[name]) or visible_size < offset:
                    raise SourceChanged("数据文件被替换或截短: " + name + "；请建立新会话")
                if item["index"] < current_index and visible_size != offset:
                    raise SourceChanged("已切换的旧分卷仍在变化: " + name + "；停止以避免记录乱序")
                if name in self.heads:
                    size, digest = self.heads[name]
                    stream.seek(0)
                    if hashlib.sha256(stream.read(size)).hexdigest() != digest:
                        raise SourceChanged("文件开头发生变化: " + name + "；请建立新会话")
                if offset:
                    stream.seek(max(0, offset - 64))
                    anchor = hashlib.sha256(stream.read(min(64, offset))).hexdigest()
                    if anchor != self.anchors.get(name):
                        raise SourceChanged("已读取的字节发生变化: " + name + "；请建立新会话")
                self.signatures[name] = signature
                stream.seek(offset)
                data = stream.read(min(max_bytes,max(0,visible_size-offset)))
                self.current = name
                if data:
                    offset += len(data)
                    self.offsets[name] = offset
                    stream.seek(max(0, offset - 64))
                    self.anchors[name] = hashlib.sha256(stream.read(min(64, offset))).hexdigest()
                    stream.seek(0)
                    self.heads[name] = [min(64, offset), hashlib.sha256(stream.read(min(64, offset))).hexdigest()]
                    return data
        return b""


class RootReader:
    """ROOT is optional and only used for replay of an immutable, closed file."""
    def __init__(self, directory, source, state=None):
        try:
            import ROOT
        except ImportError as error:
            raise RuntimeError("ROOT 回放需要安装 ROOT 6 并使用匹配的 Python；DAT 实时分析不需要 ROOT") from error
        ROOT.gROOT.SetBatch(True)
        self.path = (Path(directory) / source["files"][0]["name"]).resolve()
        self.path.relative_to(Path(directory).resolve())
        stat = self.path.stat()
        self.signature = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        s = state or {}
        if s.get("signature", self.signature) != self.signature:
            raise SourceChanged("ROOT 文件已变化；仅支持完整、关闭的 ROOT 文件，请建立新会话")
        self.offset = s.get("offset", 0)
        self.file = ROOT.TFile.Open(str(self.path), "READ")
        if not self.file or self.file.IsZombie() or self.file.TestBit(ROOT.TFile.kRecovered):
            raise ValueError("ROOT 文件损坏或尚未正常关闭")
        self.tree = self.file.Get("seastar")
        required = ("trigger_number", "trigger_second", "trigger_nanosecond", "ch_id", "hunt_id",
                    "wave_id", "second", "nanosecond", "samples", "n_waves", "waveform_points")
        if not self.tree or any(not self.tree.GetBranch(n) for n in required):
            raise ValueError("ROOT 缺少 seastar 树或必要字段")
        self.entries = int(self.tree.GetEntries())
        self.finished = self.offset >= self.entries
        self.current = source["files"][0]["name"]
        self.waiting_for_volume = None

    def state(self):
        return {"offset": self.offset, "signature": self.signature}

    def read(self, _source, max_events=2048):
        stat = self.path.stat()
        if [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns] != self.signature:
            raise SourceChanged("ROOT 文件正在变化；实时监控请选 DAT")
        records = []
        for index in range(self.offset, min(self.entries, self.offset + max_events)):
            if self.tree.GetEntry(index) <= 0:
                raise ValueError("ROOT 事件读取失败: " + str(index))
            t = self.tree
            arrays = [getattr(t, n) for n in ("ch_id", "hunt_id", "wave_id", "second", "nanosecond", "samples")]
            if any(len(a) != int(t.n_waves) for a in arrays):
                raise ValueError("ROOT 数组长度不一致: " + str(index))
            if int(t.trigger_nanosecond) >= 1_000_000_000:
                raise ValueError("ROOT 纳秒字段无效: " + str(index))
            records.append({"type": "trigger", "position": index, "number": int(t.trigger_number),
                            "ns": int(t.trigger_second) * 1_000_000_000 + int(t.trigger_nanosecond)})
            for j, samples in enumerate(t.samples):
                if len(samples) != int(t.waveform_points) or not 1 <= len(samples) <= 8192:
                    raise ValueError("ROOT 采样长度无效: " + str(index))
                def number(value):
                    return ord(value) if isinstance(value, str) else int(value)
                nano = int(t.nanosecond[j])
                if nano >= 1_000_000_000:
                    raise ValueError("ROOT 波形纳秒字段无效")
                records.append({"type": "wave", "position": index, "board": number(t.hunt_id[j]),
                                "channel": number(t.ch_id[j]), "wave_id": number(t.wave_id[j]),
                                "ns": int(t.second[j]) * 1_000_000_000 + nano,
                                "samples": np.asarray(samples, dtype=np.uint16).copy()})
            self.offset = index + 1
        self.finished = self.offset >= self.entries
        return records

    def close(self):
        self.file.Close()
