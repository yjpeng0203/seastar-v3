#!/bin/sh
# 本机 (Mac / 调试机) 历史数据集只读回放与校验脚本：
# 支持 20261101_1102/{G0..G6}/{raw_data,rec_data,csv} 多串目录及 data_1026_1027 平铺目录

set -eu
ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"

usage() {
    cat <<EOF
用法: sh scripts/debug_local.sh <数据集目录> [选项]

示例:
  sh scripts/debug_local.sh ../data_1026_1027 --csv-dir ..
  sh scripts/debug_local.sh /path/to/20261101_1102 --open
  sh scripts/debug_local.sh ../data_1026_1027 --verify-only

选项:
  --csv-dir <dir>     显式指定慢控制 CSV 所在目录 (默认使用 <数据集目录>)
  --fee-port <port>   慢控制监视器端口 (默认 8765)
  --ana-port <port>   波形分析监视器端口 (默认 8766)
  --verify-only       仅执行只读数据完整性校验并退出，不启动 Web 服务
  --open              自动在本地浏览器打开监控页面
  -h, --help          显示帮助信息
EOF
}

if [ $# -eq 0 ]; then
    usage
    exit 1
fi

case "$1" in
    -h|--help) usage; exit 0 ;;
esac

DATASET_DIR="$(CDPATH= cd -- "$1" && pwd)"
shift

CSV_DIR="$DATASET_DIR"
FEE_PORT="8765"
ANA_PORT="8766"
VERIFY_ONLY=0
OPEN_FLAG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --csv-dir) CSV_DIR="$(CDPATH= cd -- "$2" && pwd)"; shift 2 ;;
        --fee-port) FEE_PORT="$2"; shift 2 ;;
        --ana-port) ANA_PORT="$2"; shift 2 ;;
        --verify-only) VERIFY_ONLY=1; shift ;;
        --open) OPEN_FLAG="--open"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 1 ;;
    esac
done

cd "$ROOT_DIR"

if [ "$VERIFY_ONLY" -eq 1 ]; then
    python3 -B -m organizer verify "$DATASET_DIR" --no-sha256
    exit $?
fi

echo "监视器直接启动；完整只读体检请使用 --verify-only。"

SESSION_SLUG="$(basename "$DATASET_DIR" | tr -cs 'A-Za-z0-9._-' '_')"
STATE_DIR="$ROOT_DIR/runtime/replay-$SESSION_SLUG"
if [ "$(uname -s)" = "Linux" ]; then
    STATE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/seastar-v3/replay-$SESSION_SLUG"
fi
STATE_DIR="${SEASTAR_STATE_DIR:-$STATE_DIR}"
LOG_DIR="$ROOT_DIR/runtime/logs"
mkdir -p "$STATE_DIR" "$LOG_DIR"

FEE_PID=""
cleanup() {
    echo ""
    echo "[debug_local] 正在关闭本地回放服务..."
    if [ -n "$FEE_PID" ] && kill -0 "$FEE_PID" 2>/dev/null; then
        kill "$FEE_PID" 2>/dev/null || true
    fi
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo ""
echo "启动双监视器只读回放 (所有缓存写入 $STATE_DIR) ==="
python3 -B "$ROOT_DIR/fee-monitor/server.py" \
    --local \
    --data-dir "$CSV_DIR" \
    --port "$FEE_PORT" \
    $OPEN_FLAG >"$LOG_DIR/fee-replay.log" 2>&1 &
FEE_PID=$!
echo "  - 慢控制 CSV 监视器 : http://127.0.0.1:$FEE_PORT (目录: $CSV_DIR)"
echo "  - 波形/ROOT 监视器  : http://127.0.0.1:$ANA_PORT (目录: $DATASET_DIR)"
echo "按 Ctrl+C 同时关闭两个回放服务。"

python3 -B "$ROOT_DIR/data-ana-monitor/online/server.py" \
    --replay \
    --data-dir "$DATASET_DIR" \
    --state-dir "$STATE_DIR" \
    --port "$ANA_PORT" \
    $OPEN_FLAG
