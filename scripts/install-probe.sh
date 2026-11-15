#!/bin/sh
set -eu
set +x

if [ "$(uname -s)" != Linux ] || [ "$(id -u)" != 0 ] || [ ! -d /run/systemd/system ]; then
    printf '%s\n' '请在使用 systemd 的 Linux VPS 上以 root 或 sudo 运行。' >&2
    exit 1
fi
case "$(uname -m)" in
    x86_64|aarch64) ;;
    *) printf '%s\n' '目前支持 x86_64 / aarch64 VPS。' >&2; exit 1 ;;
esac
if ! command -v python3 >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update
        apt-get install -y python3 curl ca-certificates
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 curl ca-certificates
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3 curl ca-certificates
    else
        printf '%s\n' '请先安装 Python 3.9+、curl 和 ca-certificates。' >&2
        exit 1
    fi
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else "需要 Python 3.9+，请升级后再运行。")'
umask 077
CFSM_STAGE=$(mktemp -d)
trap 'rm -rf "$CFSM_STAGE"' EXIT HUP INT TERM
curl -fsSL --proto '=https' --connect-timeout 10 --max-time 120 \
    https://raw.githubusercontent.com/Versior/CF-Server-Monitor/main/scripts/install-probe.py \
    -o "$CFSM_STAGE/install-probe.py"
python3 "$CFSM_STAGE/install-probe.py" "$@"
