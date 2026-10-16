#!/bin/sh
# Run this on your own computer, after extracting the downloaded ZIP.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo '请先安装 Python 3.9 或更新版本，再运行本脚本。'
  exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
  echo '需要 Python 3.9 或更新版本。'
  exit 1
fi
if [ ! -x .venv-local-sender/bin/python ]; then
  python3 -m venv .venv-local-sender
fi
.venv-local-sender/bin/python -m pip install -r requirements-local-sender.txt
if [ ! -f bilibili_cookie.txt ] && [ "$#" -eq 0 ]; then
  echo ''
  echo '请先在本机浏览器登录 B 站，再在这个目录运行：'
  echo '.venv-local-sender/bin/python -m pip install -U browser-cookie3'
  echo '.venv-local-sender/bin/python get_bilibili_cookie.py --browser chrome'
  echo 'Chrome 可换成 edge / firefox / safari / brave / chromium。'
  echo '成功导出后，再运行本脚本。说明见 docs/local-sender.md。'
  exit 1
fi
exec .venv-local-sender/bin/python local_sender.py "$@"
