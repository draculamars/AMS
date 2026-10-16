#!/usr/bin/env bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_ROOT"
if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -r requirements.lock
else
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.lock
fi
printf '%s\n' '安装完成。运行 ./scripts/run.sh，访问 http://127.0.0.1:8765'
