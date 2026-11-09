#!/bin/sh
set -eu
script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    echo "需要 Python 3.8 或更高版本。请在运行服务的主机安装 Python。"
    exit 1
fi
if [ "${1-}" = "--check" ]; then
    shift
    exec python3 "$script_dir/check_env.py" "$@"
fi
exec python3 "$script_dir/server.py" "$@"
