#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m uvicorn app.server:app --host "${HOST:-127.0.0.1}" --port "${PORT:-7862}" --workers 1 --timeout-graceful-shutdown 5
