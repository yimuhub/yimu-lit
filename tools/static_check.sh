#!/usr/bin/env bash
# v4.17.4：未定义变量门禁——绑定弹窗式 ReferenceError（点击无反应）的一次性根治
# Python: pyflakes undefined name；前端: eslint no-undef。任一非零即失败。
set -e
cd "$(dirname "$0")/.."
PY_FAIL=0
if ! .venv/bin/python -m pyflakes app.py bot/*.py 2>/dev/null | grep -q "undefined name"; then
  echo "✅ Python undefined name: 0"
else
  echo "❌ Python undefined name 命中："
  .venv/bin/python -m pyflakes app.py bot/*.py 2>/dev/null | grep "undefined name"
  PY_FAIL=1
fi
npx --yes eslint@8 --no-eslintrc --env browser,es2022 \
  --parser-options ecmaVersion:2022,sourceType:module \
  --rule '{"no-undef":"error"}' "static/v2/js/**/*.js" > /tmp/eslint_gate.txt 2>&1 || true
if grep -q "is not defined" /tmp/eslint_gate.txt; then
  echo "❌ 前端 no-undef 命中："
  grep -B1 "is not defined" /tmp/eslint_gate.txt | grep -E "\.js|error" | head -20
  exit 1
fi
echo "✅ 前端 no-undef: 0"
