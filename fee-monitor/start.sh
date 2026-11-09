#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
    echo "工控机需要 Python 3.8 或更高版本。"
    exit 1
fi
local_mode=0
for arg in "$@"; do
    case "$arg" in
        --local|--data-dir|--data-dir=*|-h|--help)
            local_mode=1
            break
            ;;
    esac
done
if [ "$local_mode" -eq 1 ]; then
    exec python3 server.py "$@"
fi
if ! command -v ssh >/dev/null 2>&1; then
    echo "工控机需要 OpenSSH 客户端，例如 sudo apt install openssh-client。"
    exit 1
fi
exec python3 server.py --remote "$@"
