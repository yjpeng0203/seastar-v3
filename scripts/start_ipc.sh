#!/bin/sh
# 工控机一键启动脚本：
# 1) 启动 data-ana-monitor (:8766) 实时只读分析工控机本地 DAQ 波形 (.dat)
# 2) 波形监控网页提供测试结束后的完整数据归档
# 3) 前台启动 fee-monitor (:8765) 通过只读 SFTP 镜像树莓派慢控制 CSV（支持交互式输入密码）

set -eu
ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

CONFIG_FILE="$ROOT_DIR/config/ipc.json"
if [ ! -f "$CONFIG_FILE" ]; then
    CONFIG_FILE="$ROOT_DIR/config/ipc.example.json"
fi

BIND_HOST="0.0.0.0"
FEE_PORT="8765"
ANA_PORT="8766"
ENABLE_WATCH=1
ENABLE_FEE=1
RAW_DIR=""

usage() {
    cat <<EOF
用法: sh scripts/start_ipc.sh [选项]

选项:
  --config <path>     指定 organizer 配置文件 (默认优先读取 config/ipc.json)
  --raw-dir <dir>     覆盖 DAQ 原始 .dat 目录 (默认读取配置文件中的 raw_data_dir)
  --host <ip>         Web 监听地址 (默认 0.0.0.0，支持局域网访问)
  --fee-port <port>   慢控制监视器端口 (默认 8765)
  --ana-port <port>   波形在线监视器端口 (默认 8766)
  --no-watch          兼容旧选项；测试结束归档统一由网页触发
  --no-fee            不连接树莓派 fee-monitor，仅启动本地波形监控与网页数据归档
  --setup-fee         首次交互式配置树莓派 SSH 连接信息
  -h, --help          显示本帮助信息
EOF
}

SETUP_FEE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --config) CONFIG_FILE="$2"; shift 2 ;;
        --raw-dir) RAW_DIR="$2"; shift 2 ;;
        --host) BIND_HOST="$2"; shift 2 ;;
        --fee-port) FEE_PORT="$2"; shift 2 ;;
        --ana-port) ANA_PORT="$2"; shift 2 ;;
        --no-watch) ENABLE_WATCH=0; shift ;;
        --no-fee) ENABLE_FEE=0; shift ;;
        --setup-fee) SETUP_FEE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 1 ;;
    esac
done

if [ -z "$RAW_DIR" ]; then
    RAW_DIR="$(python3 -B -c 'import json,sys; d=json.load(open(sys.argv[1], encoding="utf-8")); print(d.get("organizer", d).get("raw_data_dir", "/home/ipc/disk/debug/data/both"))' "$CONFIG_FILE")"
fi

RUNTIME_DIR="$ROOT_DIR/runtime/ipc"
LOG_DIR="$RUNTIME_DIR/logs"
mkdir -p "$LOG_DIR" "$RUNTIME_DIR/ana-state"

ANA_PID=""

cleanup() {
    echo ""
    echo "[start_ipc] 正在停止后台服务..."
    if [ -n "$ANA_PID" ] && kill -0 "$ANA_PID" 2>/dev/null; then
        kill "$ANA_PID" 2>/dev/null || true
    fi
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "=== SeaStar v3 工控机联调服务启动 ==="
echo "配置文件     : $CONFIG_FILE"
echo "DAQ 波形目录 : $RAW_DIR"
echo "运行日志目录 : $LOG_DIR"

set -- --archive-config "$CONFIG_FILE"

python3 -B "$ROOT_DIR/data-ana-monitor/online/server.py" "$@" \
    --data-dir "$RAW_DIR" \
    --state-dir "$RUNTIME_DIR/ana-state" \
    --host "$BIND_HOST" \
    --port "$ANA_PORT" >"$LOG_DIR/ana-monitor.log" 2>&1 &
ANA_PID=$!
echo "[1/3] 波形在线分析监视器已启动 -> http://127.0.0.1:$ANA_PORT (PID=$ANA_PID)"

if [ "$ENABLE_WATCH" -eq 1 ]; then
    echo "[2/3] 测试结束后，在波形监控网页选择 Run 并一键归档（解码、合并 ROOT、CSV、校验）"
else
    echo "[2/3] 不运行独立解码守护；需要时在波形监控网页归档"
fi

if [ "$ENABLE_FEE" -eq 1 ]; then
    echo "[3/3] 正在启动慢控制 CSV 监视器 -> http://127.0.0.1:$FEE_PORT"
    if [ "$SETUP_FEE" -eq 1 ]; then
        python3 -B "$ROOT_DIR/fee-monitor/server.py" --remote --setup --host "$BIND_HOST" --port "$FEE_PORT"
    else
        python3 -B "$ROOT_DIR/fee-monitor/server.py" --remote --host "$BIND_HOST" --port "$FEE_PORT"
    fi
else
    echo "[3/3] 已跳过慢控制监视器 (--no-fee)；按 Ctrl+C 停止波形监视器。"
    wait
fi
