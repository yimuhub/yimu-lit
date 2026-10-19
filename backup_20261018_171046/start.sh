#!/usr/bin/env bash
# 跨平台启动（Linux/macOS）：python run.py
cd "$(dirname "$0")"
PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
exec "$PY" run.py "$@"
