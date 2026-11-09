#!/bin/sh
# 停采后一键归档整理脚本：
# 自动完成剩余末卷 decode -> hadd 合并 -> 慢控制 CSV 切片 -> G0..G6 目录组装与校验

set -eu
ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

DEFAULT_CFG="$ROOT_DIR/config/ipc.json"
if [ ! -f "$DEFAULT_CFG" ]; then
    DEFAULT_CFG="$ROOT_DIR/config/ipc.example.json"
fi

HAS_CONFIG=0
for arg in "$@"; do
    case "$arg" in
        --config|--config=*|-h|--help)
            HAS_CONFIG=1
            break
            ;;
    esac
done

if [ "$HAS_CONFIG" -eq 1 ]; then
    exec python3 -B -m organizer finalize "$@"
else
    exec python3 -B -m organizer finalize --config "$DEFAULT_CFG" "$@"
fi
