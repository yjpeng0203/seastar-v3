"""Single-volume DAT->ROOT runner (IPC C++ decoder + PyROOT C++ JIT fallback), batched hadd merger, and ROOT verifier."""
import atexit
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import OrganizerConfig
from .discovery import VolumeScan

REQUIRED_BRANCHES = (
    "trigger_number",
    "trigger_second",
    "trigger_nanosecond",
    "n_waves",
    "hunt_id",
    "ch_id",
    "wave_id",
    "second",
    "nanosecond",
    "samples",
    "waveform_points",
)

_PYROOT_DECODER_READY = False

_CPP_DECODER_SOURCE = r"""
#include <cstdint>
#include <fstream>
#include <string>
#include <vector>
#include "TFile.h"
#include "TTree.h"
#include "TObject.h"

namespace seastar_v2_jit {

static inline uint16_t be16(const uint8_t* p) {
    return static_cast<uint16_t>((static_cast<uint16_t>(p[0]) << 8) | static_cast<uint16_t>(p[1]));
}

static inline uint32_t be32(const uint8_t* p) {
    return (static_cast<uint32_t>(p[0]) << 24) |
           (static_cast<uint32_t>(p[1]) << 16) |
           (static_cast<uint32_t>(p[2]) << 8)  |
           static_cast<uint32_t>(p[3]);
}

int decode_dat_file(const char* in_path, const char* out_path) {
    std::ifstream in(in_path, std::ios::binary);
    if (!in.is_open()) {
        return -1;
    }

    TFile out(out_path, "RECREATE");
    if (out.IsZombie()) {
        return -2;
    }

    TTree tree("seastar", "Seastar waveform events");
    unsigned short trigger_number = 0;
    unsigned int trigger_second = 0;
    unsigned int trigger_nanosecond = 0;
    int n_waves = 0;
    std::vector<unsigned char> hunt_id;
    std::vector<unsigned char> ch_id;
    std::vector<unsigned char> wave_id;
    std::vector<unsigned int> second;
    std::vector<unsigned int> nanosecond;
    std::vector<std::vector<unsigned short>> samples;
    unsigned short waveform_points = 0;

    tree.Branch("trigger_number", &trigger_number, "trigger_number/s");
    tree.Branch("trigger_second", &trigger_second, "trigger_second/i");
    tree.Branch("trigger_nanosecond", &trigger_nanosecond, "trigger_nanosecond/i");
    tree.Branch("n_waves", &n_waves, "n_waves/I");
    tree.Branch("hunt_id", &hunt_id);
    tree.Branch("ch_id", &ch_id);
    tree.Branch("wave_id", &wave_id);
    tree.Branch("second", &second);
    tree.Branch("nanosecond", &nanosecond);
    tree.Branch("samples", &samples);
    tree.Branch("waveform_points", &waveform_points, "waveform_points/s");

    bool has_open_trigger = false;
    bool recovering = false;

    auto flush_event = [&]() {
        if (has_open_trigger && !samples.empty()) {
            n_waves = static_cast<int>(samples.size());
            tree.Fill();
        }
        hunt_id.clear();
        ch_id.clear();
        wave_id.clear();
        second.clear();
        nanosecond.clear();
        samples.clear();
        n_waves = 0;
        waveform_points = 0;
        has_open_trigger = false;
    };

    const size_t chunk_cap = 4 * 1024 * 1024;
    std::vector<uint8_t> buf;
    buf.reserve(chunk_cap * 2);
    std::vector<uint8_t> chunk(chunk_cap);

    while (in) {
        in.read(reinterpret_cast<char*>(chunk.data()), static_cast<std::streamsize>(chunk.size()));
        std::streamsize got = in.gcount();
        if (got <= 0) {
            break;
        }
        buf.insert(buf.end(), chunk.begin(), chunk.begin() + got);

        size_t offset = 0;
        const size_t length = buf.size();
        while (length - offset >= 16) {
            const uint8_t* p = buf.data() + offset;
            if (p[0] == 0xFF && p[1] == 0x12 && p[4] == 0 && p[5] == 0 && p[6] == 0 && p[7] == 0) {
                uint32_t t_nano = be32(p + 12);
                if (t_nano < 1000000000U) {
                    flush_event();
                    trigger_number = be16(p + 2);
                    trigger_second = be32(p + 8);
                    trigger_nanosecond = t_nano;
                    has_open_trigger = true;
                    recovering = false;
                    offset += 16;
                    continue;
                }
            }
            if (p[0] == 0xFE && !recovering && p[3] == 0) {
                uint32_t w_sec = be32(p + 4);
                uint32_t w_nano = be32(p + 8);
                uint32_t pts = be32(p + 12);
                if (w_nano < 1000000000U && pts >= 1U && pts <= 8192U) {
                    size_t frame_bytes = 16U + 2U * static_cast<size_t>(pts);
                    if (length - offset < frame_bytes) {
                        break;
                    }
                    if (has_open_trigger) {
                        hunt_id.push_back(static_cast<unsigned char>(p[1] >> 4));
                        ch_id.push_back(static_cast<unsigned char>(p[1] & 0x0F));
                        wave_id.push_back(static_cast<unsigned char>(p[2]));
                        second.push_back(w_sec);
                        nanosecond.push_back(w_nano);
                        waveform_points = static_cast<unsigned short>(pts);
                        std::vector<unsigned short> wave(pts);
                        const uint8_t* sp = p + 16;
                        for (uint32_t i = 0; i < pts; ++i) {
                            wave[i] = be16(sp + 2U * i);
                        }
                        samples.push_back(std::move(wave));
                    }
                    offset += frame_bytes;
                    continue;
                }
            }
            recovering = true;
            offset += 1;
        }
        if (offset > 0) {
            buf.erase(buf.begin(), buf.begin() + static_cast<std::ptrdiff_t>(offset));
        }
    }

    flush_event();
    int entries = static_cast<int>(tree.GetEntries());
    out.cd();
    tree.Write("", TObject::kOverwrite);
    out.Close();
    return entries;
}

} // namespace seastar_v2_jit
"""


@dataclass
class RootInspection:
    path: str
    valid: bool
    entries: int
    first_trigger: Optional[Tuple[int, int, int]]
    last_trigger: Optional[Tuple[int, int, int]]
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def has_pyroot() -> bool:
    try:
        import ROOT  # noqa: F401
        return True
    except ImportError:
        return False


def select_backend(config: OrganizerConfig) -> str:
    if config.backend == "ipc":
        dec_bin = config.resolve_decoder_bin()
        dec_cfg = config.resolve_decoder_config()
        if not dec_bin.is_file():
            raise FileNotFoundError(f"未找到工控机 decoder 可执行文件: {dec_bin}")
        if not dec_cfg.is_file():
            raise FileNotFoundError(f"未找到工控机 decoder 配置文件: {dec_cfg}")
        return "ipc"
    if config.backend == "pyroot":
        if not has_pyroot():
            raise RuntimeError("当前 Python 环境未安装 PyROOT，无法使用 pyroot 后端")
        return "pyroot"
    dec_bin = config.resolve_decoder_bin()
    dec_cfg = config.resolve_decoder_config()
    if dec_bin.is_file() and os.access(str(dec_bin), os.X_OK) and dec_cfg.is_file():
        return "ipc"
    if has_pyroot():
        return "pyroot"
    raise RuntimeError(
        f"未找到可执行 decoder ({dec_bin}) 且当前 Python 未配置 PyROOT；请检查 config.json 中的 decoder_bin 路径"
    )


def _ensure_pyroot_decoder(_work_dir: Optional[Path] = None) -> Any:
    global _PYROOT_DECODER_READY
    import ROOT
    ROOT.gROOT.SetBatch(True)
    if not _PYROOT_DECODER_READY:
        dict_dir = Path(tempfile.mkdtemp(prefix="seastar_root_dict_"))
        atexit.register(lambda p=dict_dir: shutil.rmtree(p, ignore_errors=True))
        prev_cwd = os.getcwd()
        try:
            os.chdir(str(dict_dir))
            ROOT.gSystem.SetBuildDir(str(dict_dir), True)
            ROOT.gInterpreter.GenerateDictionary("vector<unsigned short>;vector<vector<unsigned short> >", "vector")
        finally:
            os.chdir(prev_cwd)
        ok = ROOT.gInterpreter.Declare(_CPP_DECODER_SOURCE)
        if not ok:
            raise RuntimeError("初始化内置 ROOT C++ 波形转换器失败")
        _PYROOT_DECODER_READY = True
    return ROOT


def inspect_root_file(
    root_path: Path,
    root_bin: str = "root",
    work_dir: Optional[Path] = None,
) -> RootInspection:
    target = Path(root_path).resolve()
    if not target.is_file() or target.stat().st_size <= 0:
        return RootInspection(
            path=str(target),
            valid=False,
            entries=0,
            first_trigger=None,
            last_trigger=None,
            error="ROOT 文件不存在或为空",
        )
    if has_pyroot():
        return _inspect_root_with_pyroot(target)
    return _inspect_root_with_cli(target, root_bin=root_bin, work_dir=work_dir)


def _inspect_root_with_pyroot(target: Path) -> RootInspection:
    import ROOT
    ROOT.gROOT.SetBatch(True)
    tfile = ROOT.TFile.Open(str(target), "READ")
    if not tfile or tfile.IsZombie() or tfile.TestBit(ROOT.TFile.kRecovered):
        if tfile:
            tfile.Close()
        return RootInspection(
            path=str(target),
            valid=False,
            entries=0,
            first_trigger=None,
            last_trigger=None,
            error="ROOT 文件损坏（Zombie）或未正常关闭（kRecovered）",
        )
    try:
        tree = tfile.Get("seastar")
        if not tree:
            return RootInspection(
                path=str(target),
                valid=False,
                entries=0,
                first_trigger=None,
                last_trigger=None,
                error="ROOT 文件缺少 TTree 'seastar'",
            )
        missing = [b for b in REQUIRED_BRANCHES if not tree.GetBranch(b)]
        if missing:
            return RootInspection(
                path=str(target),
                valid=False,
                entries=0,
                first_trigger=None,
                last_trigger=None,
                error=f"ROOT TTree 'seastar' 缺少必要分支: {', '.join(missing)}",
            )
        entries = int(tree.GetEntries())
        first_trig: Optional[Tuple[int, int, int]] = None
        last_trig: Optional[Tuple[int, int, int]] = None
        if entries > 0:
            if tree.GetEntry(0) <= 0:
                return RootInspection(
                    path=str(target),
                    valid=False,
                    entries=entries,
                    first_trigger=None,
                    last_trigger=None,
                    error="无法读取首条 ROOT 事件 (entry 0)",
                )
            first_trig = (
                int(tree.trigger_number),
                int(tree.trigger_second),
                int(tree.trigger_nanosecond),
            )
            if tree.GetEntry(entries - 1) <= 0:
                return RootInspection(
                    path=str(target),
                    valid=False,
                    entries=entries,
                    first_trigger=first_trig,
                    last_trigger=None,
                    error=f"无法读取末条 ROOT 事件 (entry {entries - 1})",
                )
            last_trig = (
                int(tree.trigger_number),
                int(tree.trigger_second),
                int(tree.trigger_nanosecond),
            )
        return RootInspection(
            path=str(target),
            valid=True,
            entries=entries,
            first_trigger=first_trig,
            last_trigger=last_trig,
        )
    finally:
        tfile.Close()


def _inspect_root_with_cli(target: Path, root_bin: str = "root", work_dir: Optional[Path] = None) -> RootInspection:
    exe = shutil.which(root_bin) or (root_bin if Path(root_bin).is_file() else None)
    if not exe:
        return RootInspection(
            path=str(target),
            valid=False,
            entries=0,
            first_trigger=None,
            last_trigger=None,
            error=f"未安装 PyROOT 且未找到 ROOT 命令行程序: {root_bin}",
        )
    scratch = Path(work_dir) if work_dir else target.parent
    scratch.mkdir(parents=True, exist_ok=True)
    func_name = f"inspect_seastar_{os.getpid()}"
    macro_path = scratch / f"{func_name}.C"
    macro_code = (
        r"""
#include <iostream>
#include "TFile.h"
#include "TTree.h"

void __FUNC_NAME__(const char* path) {
    TFile* f = TFile::Open(path, "READ");
    if (!f || f->IsZombie() || f->TestBit(TFile::kRecovered)) {
        std::cout << "SEASTAR_ROOT_JSON:{\"valid\":false,\"entries\":0,\"error\":\"zombie_or_recovered\"}" << std::endl;
        if (f) f->Close();
        return;
    }
    TTree* t = (TTree*)f->Get("seastar");
    if (!t) {
        std::cout << "SEASTAR_ROOT_JSON:{\"valid\":false,\"entries\":0,\"error\":\"missing_seastar_tree\"}" << std::endl;
        f->Close();
        return;
    }
    const char* req[] = {
        "trigger_number", "trigger_second", "trigger_nanosecond", "n_waves",
        "hunt_id", "ch_id", "wave_id", "second", "nanosecond", "samples", "waveform_points"
    };
    for (const char* b : req) {
        if (!t->GetBranch(b)) {
            std::cout << "SEASTAR_ROOT_JSON:{\"valid\":false,\"entries\":0,\"error\":\"missing_branch\"}" << std::endl;
            f->Close();
            return;
        }
    }
    Long64_t n = t->GetEntries();
    unsigned short tnum = 0;
    unsigned int tsec = 0, tns = 0;
    t->SetBranchAddress("trigger_number", &tnum);
    t->SetBranchAddress("trigger_second", &tsec);
    t->SetBranchAddress("trigger_nanosecond", &tns);
    if (n == 0) {
        std::cout << "SEASTAR_ROOT_JSON:{\"valid\":true,\"entries\":0,\"first\":null,\"last\":null}" << std::endl;
        f->Close();
        return;
    }
    t->GetEntry(0);
    unsigned short f_num = tnum; unsigned int f_sec = tsec, f_ns = tns;
    t->GetEntry(n - 1);
    unsigned short l_num = tnum; unsigned int l_sec = tsec, l_ns = tns;
    std::cout << "SEASTAR_ROOT_JSON:{\"valid\":true,\"entries\":" << n
              << ",\"first\":[" << f_num << "," << f_sec << "," << f_ns << "]"
              << ",\"last\":[" << l_num << "," << l_sec << "," << l_ns << "]}" << std::endl;
    f->Close();
}
""".replace("__FUNC_NAME__", func_name)
    )
    try:
        macro_path.write_text(macro_code, encoding="utf-8")
        escaped = str(target).replace("\\", "\\\\").replace('"', '\\"')
        call_arg = f'{macro_path}("{escaped}")'
        proc = subprocess.run(
            [exe, "-l", "-b", "-q", call_arg],
            capture_output=True,
            text=True,
            timeout=120,
        )
        for line in (proc.stdout + "\n" + proc.stderr).splitlines():
            if line.startswith("SEASTAR_ROOT_JSON:"):
                payload = json.loads(line.split("SEASTAR_ROOT_JSON:", 1)[1])
                if not payload.get("valid"):
                    return RootInspection(
                        path=str(target),
                        valid=False,
                        entries=0,
                        first_trigger=None,
                        last_trigger=None,
                        error=str(payload.get("error", "invalid_root")),
                    )
                first = tuple(payload["first"]) if payload.get("first") else None
                last = tuple(payload["last"]) if payload.get("last") else None
                return RootInspection(
                    path=str(target),
                    valid=True,
                    entries=int(payload["entries"]),
                    first_trigger=first,  # type: ignore[arg-type]
                    last_trigger=last,    # type: ignore[arg-type]
                )
        return RootInspection(
            path=str(target),
            valid=False,
            entries=0,
            first_trigger=None,
            last_trigger=None,
            error=f"root CLI 未返回有效校验结果 (code={proc.returncode}): {proc.stderr.strip()[-200:]}",
        )
    finally:
        if macro_path.exists():
            macro_path.unlink()


def verify_root_matches_expectation(
    inspection: RootInspection,
    expected_entries: int,
    expected_first_trigger: Optional[Tuple[int, int, int]],
    expected_last_trigger: Optional[Tuple[int, int, int]],
    context_label: str,
) -> None:
    if not inspection.valid:
        raise RuntimeError(f"{context_label} 校验失败: {inspection.error}")
    if inspection.entries != int(expected_entries):
        raise RuntimeError(
            f"{context_label} 事件数不匹配: ROOT entries={inspection.entries}, 预期带波形触发数={expected_entries}"
        )
    if expected_entries > 0:
        if expected_first_trigger is not None and tuple(inspection.first_trigger or ()) != tuple(expected_first_trigger):
            raise RuntimeError(
                f"{context_label} 首事件触发信息不匹配: ROOT={inspection.first_trigger}, 预期={expected_first_trigger}"
            )
        if expected_last_trigger is not None and tuple(inspection.last_trigger or ()) != tuple(expected_last_trigger):
            raise RuntimeError(
                f"{context_label} 末事件触发信息不匹配: ROOT={inspection.last_trigger}, 预期={expected_last_trigger}"
            )


def decode_volume(
    vol_path: Path,
    part_root_path: Path,
    scan: VolumeScan,
    config: OrganizerConfig,
    use_nice: bool = False,
) -> Tuple[str, RootInspection]:
    """Decode a single .dat volume into part_root_path atomically and verify against scan."""
    source = Path(vol_path).resolve()
    if scan.is_gzip:
        raise ValueError(f"拒绝 decode 异常文件 {source.name}: 文件头为 gzip (1f8b08)，疑似被 tar/gzip 误覆盖")
    if scan.bad_bytes > 0:
        raise ValueError(f"拒绝自动 decode 含损坏字节的分卷 {source.name}: bad_bytes={scan.bad_bytes}")

    backend = select_backend(config)
    part_root_path = Path(part_root_path).resolve()
    part_root_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = part_root_path.with_name(f".{part_root_path.stem}.{os.getpid()}.tmp.root")
    if tmp_out.exists():
        tmp_out.unlink()

    try:
        if backend == "ipc":
            dec_bin = config.resolve_decoder_bin()
            dec_cfg = config.resolve_decoder_config()
            raw_cwd = config.resolve_raw_dir()
            if not raw_cwd.is_dir():
                raw_cwd = source.parent
            cmd: List[str] = []
            if use_nice and config.nice_increment > 0 and shutil.which("nice"):
                cmd.extend(["nice", "-n", str(int(config.nice_increment))])
            cmd.extend([str(dec_bin), str(source), str(tmp_out), "--config", str(dec_cfg)])
            proc = subprocess.run(
                cmd,
                cwd=str(raw_cwd),
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()[-400:]
                raise RuntimeError(f"decoder 返回非零退出码 ({proc.returncode}) [{source.name}]: {detail}")
        elif backend == "pyroot":
            ROOT = _ensure_pyroot_decoder(config.resolve_work_dir())
            rc = int(ROOT.seastar_v2_jit.decode_dat_file(str(source), str(tmp_out)))
            if rc < 0:
                raise RuntimeError(f"PyROOT 转换失败 (rc={rc}): {source.name}")
        else:
            raise ValueError(f"未知后端: {backend}")

        inspection = inspect_root_file(tmp_out, root_bin=config.root_bin, work_dir=config.resolve_work_dir())
        verify_root_matches_expectation(
            inspection=inspection,
            expected_entries=scan.wave_events,
            expected_first_trigger=scan.first_wave_trigger,
            expected_last_trigger=scan.last_wave_trigger,
            context_label=f"单卷 decode ({source.name})",
        )
        os.replace(tmp_out, part_root_path)
        inspection.path = str(part_root_path)
        return backend, inspection
    finally:
        if tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError:
                pass


def _run_hadd_single(hadd_exe: str, target_root: Path, inputs: Sequence[Path], use_nice: bool, nice_inc: int) -> None:
    cmd: List[str] = []
    if use_nice and nice_inc > 0 and shutil.which("nice"):
        cmd.extend(["nice", "-n", str(int(nice_inc))])
    cmd.extend([hadd_exe, "-f", str(target_root)])
    cmd.extend(str(p) for p in inputs)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not target_root.is_file():
        detail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise RuntimeError(f"hadd 合并失败 (code={proc.returncode}): {detail}")


def merge_part_roots(
    part_roots: Sequence[Path],
    output_root_path: Path,
    expected_entries: int,
    expected_first_trigger: Optional[Tuple[int, int, int]],
    expected_last_trigger: Optional[Tuple[int, int, int]],
    config: OrganizerConfig,
    temp_dir: Path,
    use_nice: bool = False,
) -> RootInspection:
    """Merge 1..N per-volume .root files via batched hadd, verify total entries & endpoints, and atomically place output."""
    if not part_roots:
        raise ValueError("没有可合并的分卷 ROOT 文件")

    output_root_path = Path(output_root_path).resolve()
    output_root_path.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(temp_dir).resolve()
    temp_dir.mkdir(parents=True, exist_ok=True)

    tmp_final = output_root_path.with_name(f".{output_root_path.stem}.{os.getpid()}.tmp.root")
    intermediate_files: List[Path] = []
    if tmp_final.exists():
        tmp_final.unlink()

    try:
        if len(part_roots) == 1:
            shutil.copy2(part_roots[0], tmp_final)
        else:
            hadd_exe = shutil.which(config.hadd_bin) or (config.hadd_bin if Path(config.hadd_bin).is_file() else None)
            if not hadd_exe:
                raise FileNotFoundError(f"未找到 ROOT 合并工具 hadd: {config.hadd_bin}")
            batch_size = max(2, int(config.hadd_batch_size))
            current_level = [Path(p).resolve() for p in part_roots]
            level_idx = 0
            while len(current_level) > batch_size:
                next_level: List[Path] = []
                for chunk_idx in range(0, len(current_level), batch_size):
                    chunk = current_level[chunk_idx:chunk_idx + batch_size]
                    if len(chunk) == 1:
                        next_level.append(chunk[0])
                        continue
                    batch_out = temp_dir / f".hadd_L{level_idx}_B{chunk_idx // batch_size}_{os.getpid()}.root"
                    _run_hadd_single(hadd_exe, batch_out, chunk, use_nice, config.nice_increment)
                    intermediate_files.append(batch_out)
                    next_level.append(batch_out)
                current_level = next_level
                level_idx += 1
            _run_hadd_single(hadd_exe, tmp_final, current_level, use_nice, config.nice_increment)

        inspection = inspect_root_file(tmp_final, root_bin=config.root_bin, work_dir=temp_dir)
        verify_root_matches_expectation(
            inspection=inspection,
            expected_entries=expected_entries,
            expected_first_trigger=expected_first_trigger,
            expected_last_trigger=expected_last_trigger,
            context_label=f"合并 ROOT ({output_root_path.name})",
        )
        os.replace(tmp_final, output_root_path)
        inspection.path = str(output_root_path)
        return inspection
    finally:
        if tmp_final.exists():
            try:
                tmp_final.unlink()
            except OSError:
                pass
        for item in intermediate_files:
            if item.exists():
                try:
                    item.unlink()
                except OSError:
                    pass
