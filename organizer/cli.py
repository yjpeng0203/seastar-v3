"""Command-line interface for SeaStar v2 automated incremental decode & archive organizer."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
from typing import Any, Dict, List, Optional

from .config import OrganizerConfig, load_organizer_config
from .decoder_runner import has_pyroot, select_backend
from .discovery import discover_runs
from .packager import finalize_session, verify_dataset, watch_once
from .state import StateStore, decode_lock


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="配置文件 JSON 路径（例如 config/ipc.json）")
    parser.add_argument("--raw-dir", type=str, default=None, help="DAQ 原始 .dat 目录（默认 /home/ipc/disk/debug/data/both）")
    parser.add_argument("--csv-dir", type=str, default=None, help="fee-monitor 本地 CSV 镜像目录或 CSV 目录")
    parser.add_argument("--output-root", type=str, default=None, help="整理归档根目录")
    parser.add_argument("--work-dir", type=str, default=None, help="增量 decode 临时与检查点缓存目录")
    parser.add_argument("--decoder-bin", type=str, default=None, help="工控机 decoder 可执行文件路径")
    parser.add_argument("--decoder-config", type=str, default=None, help="工控机 decoder config.json 路径")
    parser.add_argument("--backend", choices=["auto", "ipc", "pyroot"], default=None, help="单卷转换后端")
    parser.add_argument("--layout", choices=["group", "flat"], default=None, help="归档目录布局：group（默认 G0..G6）或 flat")
    parser.add_argument("--link-mode", choices=["hardlink", "copy"], default=None, help="原始 .dat 归档方式：hardlink（默认）或 copy")
    parser.add_argument("--settle-seconds", type=float, default=None, help="分卷静默确认秒数（默认 3.0）")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")


def _build_config_from_args(args: argparse.Namespace) -> OrganizerConfig:
    overrides: Dict[str, Any] = {
        "raw_data_dir": getattr(args, "raw_dir", None),
        "csv_mirror_dir": getattr(args, "csv_dir", None),
        "output_root": getattr(args, "output_root", None),
        "work_dir": getattr(args, "work_dir", None),
        "decoder_bin": getattr(args, "decoder_bin", None),
        "decoder_config": getattr(args, "decoder_config", None),
        "backend": getattr(args, "backend", None),
        "layout": getattr(args, "layout", None),
        "link_mode": getattr(args, "link_mode", None),
        "settle_seconds": getattr(args, "settle_seconds", None),
    }
    if getattr(args, "poll_seconds", None) is not None:
        overrides["poll_seconds"] = args.poll_seconds
    return load_organizer_config(path=getattr(args, "config", None), overrides=overrides)


def _cmd_check(args: argparse.Namespace) -> int:
    cfg = _build_config_from_args(args)
    raw_dir = cfg.resolve_raw_dir()
    csv_dir = cfg.resolve_csv_dir()
    out_dir = cfg.resolve_output_root()
    dec_bin = cfg.resolve_decoder_bin()
    dec_cfg = cfg.resolve_decoder_config()
    hadd_exe = shutil.which(cfg.hadd_bin) or (cfg.hadd_bin if Path(cfg.hadd_bin).is_file() else None)
    root_exe = shutil.which(cfg.root_bin) or (cfg.root_bin if Path(cfg.root_bin).is_file() else None)

    backend_ok = True
    selected_backend = ""
    backend_error = ""
    try:
        selected_backend = select_backend(cfg)
    except Exception as exc:
        backend_ok = False
        backend_error = str(exc)

    runs = discover_runs(raw_dir, cfg, finalize=False) if raw_dir.is_dir() else []
    free_gib: Optional[float] = None
    probe_dir = out_dir if out_dir.exists() else (out_dir.parent if out_dir.parent.exists() else Path.cwd())
    try:
        usage = shutil.disk_usage(probe_dir)
        free_gib = round(usage.free / (1024 ** 3), 2)
    except OSError:
        free_gib = None

    report = {
        "ok": raw_dir.is_dir() and backend_ok and bool(hadd_exe),
        "raw_data_dir": {"path": str(raw_dir), "exists": raw_dir.is_dir()},
        "csv_mirror_dir": {"path": str(csv_dir), "exists": csv_dir.exists()},
        "output_root": {"path": str(out_dir), "free_gib": free_gib},
        "decoder_bin": {"path": str(dec_bin), "exists": dec_bin.is_file(), "executable": os.access(str(dec_bin), os.X_OK)},
        "decoder_config": {"path": str(dec_cfg), "exists": dec_cfg.is_file()},
        "hadd_bin": hadd_exe,
        "root_bin": root_exe,
        "pyroot_available": has_pyroot(),
        "selected_backend": selected_backend,
        "backend_error": backend_error,
        "board_map": cfg.board_map,
        "layout": cfg.layout,
        "runs": [
            {
                "run_key": r.key,
                "string_group": r.string_group,
                "board_id": r.board_id,
                "volumes": len(r.volumes),
                "sealed_volumes": sum(1 for v in r.volumes if v.sealed),
                "missing_indices": r.missing_indices,
                "bytes": r.total_bytes,
            }
            for r in runs
        ],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("=== SeaStar v2 Organizer 环境与数据目录自检 ===")
        print(f"原始波形目录 : {raw_dir} ({'存在' if raw_dir.is_dir() else '不存在'})")
        print(f"慢控制 CSV   : {csv_dir} ({'存在' if csv_dir.exists() else '暂不存在'})")
        print(f"归档输出目录 : {out_dir} (可用空间: {free_gib if free_gib is not None else '未知'} GiB, 布局: {cfg.layout})")
        print(f"转换后端     : {selected_backend or '不可用'} {('(' + backend_error + ')') if backend_error else ''}")
        print(f"decoder 程序 : {dec_bin}")
        print(f"decoder 配置 : {dec_cfg}")
        print(f"hadd / root  : hadd={hadd_exe or '缺失'} | root={root_exe or '缺失'} | PyROOT={'可用' if report['pyroot_available'] else '未启用'}")
        print(f"板号→串映射  : {', '.join(f'fee_{k}->{v}' for k, v in sorted(cfg.board_map.items()))}")
        print(f"发现采集 Run : {len(runs)} 个")
        for r in runs:
            sealed = sum(1 for v in r.volumes if v.sealed)
            miss = f" · 缺失分卷 {r.missing_indices}" if r.missing_indices else ""
            print(f"  - [{r.string_group}] {r.key}: 共 {len(r.volumes)} 卷（已封卷 {sealed} 卷，{r.total_bytes / 1048576:.1f} MiB）{miss}")
    return 0 if report["ok"] else 1


def _cmd_watch(args: argparse.Namespace) -> int:
    cfg = _build_config_from_args(args)
    work_dir = cfg.resolve_work_dir()
    state = StateStore(work_dir / "state.sqlite3")
    stop_event = threading.Event()

    def _handle_signal(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        if args.once:
            summary = watch_once(cfg, state)
            if args.json:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                print(
                    f"[watch] 扫描 Run {summary['runs_found']} 个 · 本轮新增 decode {len(summary['decoded_now'])} 卷 "
                    f"· 已缓存封卷 {summary['cached_sealed']} 卷 · 活跃末卷 {summary['active_unsealed']} 卷"
                )
                for err in summary["errors"]:
                    print(f"[watch][错误] {err['volume']}: {err['error']}", file=sys.stderr)
            return 1 if summary["errors"] else 0

        if not args.json:
            print(
                f"[watch] 已启动后台增量封卷 decode 守护：raw={cfg.resolve_raw_dir()} "
                f"work={work_dir} 间隔={cfg.poll_seconds}s（按 Ctrl+C 停止）",
                flush=True,
            )
        while not stop_event.is_set():
            summary = watch_once(cfg, state)
            if summary["decoded_now"] or summary["errors"]:
                if args.json:
                    print(json.dumps(summary, ensure_ascii=False), flush=True)
                else:
                    if summary["decoded_now"]:
                        print(f"[watch] 新增完成分卷 decode: {', '.join(summary['decoded_now'])}", flush=True)
                    for err in summary["errors"]:
                        print(f"[watch][告警] {err['volume']}: {err['error']}", file=sys.stderr, flush=True)
            stop_event.wait(max(0.5, float(cfg.poll_seconds)))
        return 0
    finally:
        state.close()


def _cmd_finalize(args: argparse.Namespace) -> int:
    cfg = _build_config_from_args(args)
    if getattr(args, "analyze", False) and cfg.layout != "group":
        raise ValueError("--analyze 需要 --layout group")
    work_dir = cfg.resolve_work_dir()
    state = StateStore(work_dir / "state.sqlite3")
    try:
        result = finalize_session(
            config=cfg,
            state=state,
            session_name=args.session_name,
            run_filter=args.run,
            board_filter=args.board,
            since_stamp=args.since_stamp,
            allow_partial=args.allow_partial,
            include_already_finalized=args.reprocess,
        )
        if getattr(args, "analyze", False) and result["status"] == "ok":
            from .batch_analysis import analyze_dataset
            from contextlib import redirect_stdout
            with redirect_stdout(sys.stderr if args.json else sys.stdout):
                result["analysis"] = analyze_dataset(Path(result["session_dir"]), config_path=args.analysis_config)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if result["status"] == "nothing_to_finalize":
                print(f"[finalize] {result['message']}")
                return 0
            print(f"=== 归档整理完成: {result['session_dir']} (布局: {result['layout']}) ===")
            for run in result["runs"]:
                csv_info = run["csv"]
                csv_desc = (
                    f"{csv_info['relative_path']} ({csv_info['rows']} 行)"
                    if csv_info.get("relative_path")
                    else f"无匹配 CSV ({csv_info.get('note', '')})"
                )
                print(
                    f"  - [{run['string_group']} · FEE {run['board_id']}] {run['run_key']}: "
                    f"{run['volume_count']} 卷 (复用缓存 {run['cached_volumes_reused']} 卷 / 新转 {run['freshly_decoded_volumes']} 卷) "
                    f"-> {run['rec_root']['relative_path']} (entries={run['rec_root']['entries']}, waves={run['waves']}) "
                    f"| CSV: {csv_desc}"
                )
        return 0
    finally:
        state.close()


def _cmd_verify(args: argparse.Namespace) -> int:
    report = verify_dataset(
        dataset_dir=args.dataset_dir,
        root_bin=args.root_bin,
        check_sha256=not args.no_sha256,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        status_str = "通过 (OK)" if report["ok"] else "发现异常 (FAILED)"
        print(f"=== 数据集只读校验: {report['dataset_dir']} [{status_str}] ===")
        print(
            f"目录结构: {report['layout']} | manifest.json: {'有' if report['has_manifest'] else '无'} "
            f"| SHA256SUMS.txt: {'有' if report['has_sha256sums'] else '无'}"
        )
        for grp in report["groups"]:
            print(
                f"  - [{grp['group']}] 板号={grp['board_ids']} | .dat={len(grp['dat_files'])} 个 "
                f"(完好带波形触发={grp['clean_dat_wave_events']}, 波形数={grp['clean_dat_waves']}, 损坏={len(grp['corrupted_dat_files'])}) "
                f"| .root={len(grp['root_files'])} 个 (entries={grp['root_total_entries']}) "
                f"| .csv={len(grp['csv_files'])} 个"
            )
        for warn in report["warnings"]:
            print(f"  [提示] {warn}")
        for err in report["errors"]:
            print(f"  [错误] {err}")
    return 0 if report["ok"] else 2


def _cmd_analyze(args: argparse.Namespace) -> int:
    from .batch_analysis import analyze_dataset
    if args.json:
        from contextlib import redirect_stdout
        with redirect_stdout(sys.stderr):
            results = analyze_dataset(args.dataset_dir, args.group, args.analysis_config, args.max_events)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        analyze_dataset(args.dataset_dir, args.group, args.analysis_config, args.max_events)
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    from .batch_analysis import render_saved
    render_saved(args.dataset_dir, args.group)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m organizer",
        description="SeaStar v2 自动增量 DAT->ROOT 转换与多串 (G0..G6) 数据归档工具",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="检查工控机/本机环境、decoder 路径与待处理分卷状态")
    _add_common_args(p_check)
    p_check.set_defaults(func=_cmd_check)

    p_watch = sub.add_parser("watch", help="采集期间后台监听并增量 decode 已封卷 .dat 文件")
    _add_common_args(p_watch)
    p_watch.add_argument("--once", action="store_true", help="仅执行一轮扫描与封卷 decode 后退出")
    p_watch.add_argument("--poll-seconds", type=float, default=None, help="轮询扫描间隔秒数")
    p_watch.set_defaults(func=_cmd_watch)

    p_fin = sub.add_parser("finalize", help="停采后一键完成末卷 decode、hadd 合并、CSV 切片与标准目录归档")
    _add_common_args(p_fin)
    p_fin.add_argument("--session-name", type=str, default=None, help="手动指定归档顶层目录名（例如 20261101_1102）")
    p_fin.add_argument("--run", action="append", default=None, help="仅归档指定 Run（可多次传入，如 run010158）")
    p_fin.add_argument("--board", type=int, action="append", default=None, help="仅归档指定电子学板号（如 --board 3 --board 4）")
    p_fin.add_argument("--since-stamp", type=str, default=None, help="仅归档起始时间戳 >= YYYYMMDDHHMMSS 的 Run")
    p_fin.add_argument("--allow-partial", action="store_true", help="允许在缺失中间 idx 分卷时仍然归档剩余分卷")
    p_fin.add_argument("--reprocess", action="store_true", help="重新打包已归档过的 Run")
    p_fin.add_argument("--analyze", action="store_true", help="归档完成后执行全量分析，结果写入各串 results/")
    p_fin.add_argument("--analysis-config", type=Path, default=None, help="批次分析参数 JSON（与在线分析参数一致）")
    p_fin.set_defaults(func=_cmd_finalize)

    p_ver = sub.add_parser("verify", help="只读校验已整理的数据目录（支持 G0..G6 分层结构与旧版平铺结构）")
    p_ver.add_argument("dataset_dir", type=Path, help="要校验的数据集目录路径")
    p_ver.add_argument("--root-bin", type=str, default="root", help="ROOT CLI 路径")
    p_ver.add_argument("--no-sha256", action="store_true", help="跳过 SHA-256 全量哈希计算以加快大目录检查")
    p_ver.add_argument("--json", action="store_true", help="输出结构化 JSON")
    p_ver.set_defaults(func=_cmd_verify)

    p_analysis = sub.add_parser("analyze", help="全量分析批次 ROOT 与慢控制 CSV，按串生成 PNG/PDF/ROOT/统计表")
    p_analysis.add_argument("dataset_dir", type=Path, help="批次目录，包含 Gx/rec_data 和 Gx/csv")
    p_analysis.add_argument("--group", action="append", default=None, help="只分析指定串，例如 --group G3，可多次指定")
    p_analysis.add_argument("--analysis-config", type=Path, default=None, help="分析参数 JSON；默认与在线分析一致")
    p_analysis.add_argument("--max-events", type=int, default=0, help="每份 ROOT 的抽样事件上限；0=全量，抽样结果明确标记 sample")
    p_analysis.add_argument("--json", action="store_true")
    p_analysis.set_defaults(func=_cmd_analyze)

    p_render = sub.add_parser("render", help="复用已有完整分析，生成新版 ROOT 图谱与精简/完整报告，不重新积分")
    p_render.add_argument("dataset_dir", type=Path, help="批次目录或单个已完成的分析目录")
    p_render.add_argument("--group", action="append", default=None)
    p_render.set_defaults(func=_cmd_render)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in ("watch", "finalize"):
            cfg = _build_config_from_args(args)
            if args.command == "watch":select_backend(cfg)
            with decode_lock(cfg.resolve_work_dir()):
                return int(args.func(args))
        return int(args.func(args))
    except Exception as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
