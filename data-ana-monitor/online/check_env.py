#!/usr/bin/env python3
"""Only inspect the server environment; do not install, compile, or start a service."""
import argparse
import importlib
import importlib.util
import os
from pathlib import Path
import platform
import shutil
import sys


def inspect(data_dir=None):
    system = platform.system()
    checks = [
        ("Python", sys.version_info >= (3, 8), platform.python_version() + "（需要 3.8+）"),
        ("操作系统", system in ("Linux", "Darwin"), system + " / " + platform.machine() +
         "（当前后台支持 Linux/macOS，Windows 使用 WSL）"),
    ]
    try:
        numpy = importlib.import_module("numpy")
        minimum = tuple(int(v) for v in numpy.__version__.split(".")[:2]) >= (1, 19)
        checks.append(("NumPy", minimum, numpy.__version__ + "（需要 1.19+）"))
    except Exception as error:
        checks.append(("NumPy", False, str(error)))
    compiler = shutil.which("c++") or shutil.which("g++")
    checks.append(("C++ 编译器", bool(compiler), compiler or "首次编译分析内核需要 c++ 或 g++"))
    if data_dir is not None:
        path = Path(data_dir).expanduser().resolve()
        checks.append(("数据目录", path.is_dir() and os.access(path, os.R_OK | os.X_OK), str(path)))
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="检查服务所在主机上的可读目录")
    parser.add_argument("--remote",type=Path,help="检查树莓派接收配置；本命令不连接、不询问密码")
    args = parser.parse_args(argv)
    checks = inspect(args.data_dir)
    if args.remote:
        try:
            from remote_reader import RemoteConfig
            remote=RemoteConfig.load(args.remote)
            checks.append(('远程配置',True,remote.host+':'+remote.remote_dir+'；'+remote.pattern+'；auth='+remote.auth))
        except (ValueError,OSError,TypeError) as error:
            checks.append(('远程配置',False,str(error)))
        ssh=shutil.which('ssh')
        checks.append(('OpenSSH',bool(ssh),ssh or '需要系统 ssh 客户端（openssh-client）'))
    print("检查对象：运行 Python 服务的这台主机；浏览器可在另一台电脑。")
    for name, ok, detail in checks:
        print(("[OK] " if ok else "[缺少] ") + name + ": " + detail)
    try:
        root = importlib.util.find_spec("ROOT")
    except (ImportError, ValueError):
        root = None
    print("[可选] ROOT: " + ("已找到模块；完整 ROOT 回放时再检查文件" if root else "未找到；DAT 实时分析不需要 ROOT"))
    print("[说明] 已离线打包 ECharts；不需要 Node.js/npm/CDN。本命令不编译内核、不创建数据库。")
    if not all(ok for _, ok, _ in checks):
        print("Linux 缺少依赖时，可由管理员安装：sudo apt install python3 python3-numpy g++")
        return 1
    print("环境检查通过。首次启动时仍会实际编译内核；编译失败会显示原因。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
