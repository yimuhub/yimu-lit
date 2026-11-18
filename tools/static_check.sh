#!/usr/bin/env bash
# 未定义变量门禁 —— 绑定/弹窗式 ReferenceError（点了没反应）的一次性根治
#   Python: pyflakes 的 undefined name
#   前端:   eslint 的 no-undef
#
# ── 一条纪律：**工具缺失算失败，不算通过** ──
# 上一版写的是 `.venv/bin/python -m pyflakes ... | grep -q "undefined name"`。
# 在没有 .venv 的工作树里，命令直接不存在、grep 什么也抓不到，于是照样打印
# 「✅ Python undefined name: 0」——门禁空转，每次「体检通过」都是假的。
# 现在改成先确认解释器和 pyflakes 都真的能跑，跑不了就红。
set -uo pipefail
cd "$(dirname "$0")/.."

FAIL=0

# ── 1. 选解释器 ──
PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "❌ 找不到可用的 Python，门禁无法执行（不是通过）"
    exit 1
fi
if ! "$PY" -c "import pyflakes" >/dev/null 2>&1; then
    echo "❌ $PY 缺少 pyflakes —— 门禁无法执行（不是通过）"
    echo "   装一下：$PY -m pip install pyflakes"
    exit 1
fi

# ── 2. Python: undefined name ──
PYVER="$("$PY" -c 'import pyflakes;print(pyflakes.__version__)' 2>/dev/null)"
PYOUT="$("$PY" -m pyflakes app.py bot/*.py 2>&1 || true)"
if printf '%s' "$PYOUT" | grep -q "undefined name"; then
    echo "❌ Python undefined name 命中："
    printf '%s\n' "$PYOUT" | grep "undefined name"
    FAIL=1
else
    echo "✅ Python undefined name: 0  ($PY, pyflakes $PYVER)"
fi

# ── 3. 前端: no-undef ──
if ! command -v npx >/dev/null 2>&1; then
    echo "❌ 找不到 npx，前端门禁无法执行（不是通过）"
    exit 1
fi
LOG=/tmp/eslint_gate.txt
npx --yes eslint@8 --no-eslintrc --env browser,es2022 \
    --parser-options ecmaVersion:2022,sourceType:module \
    --rule '{"no-undef":"error"}' "static/v2/js/**/*.js" > "$LOG" 2>&1
RC=$?

if [ "$RC" -eq 0 ]; then
    echo "✅ 前端 no-undef: 0"
elif [ "$RC" -eq 1 ]; then
    echo "❌ 前端 no-undef 命中："
    grep -B1 "is not defined" "$LOG" | grep -E "\.js|error" | head -20
    FAIL=1
else
    # 2 = 配置/执行出错。以前这种情况会被 grep 吞掉当成通过。
    echo "❌ 前端门禁无法执行（eslint 退出码 $RC，多半是 npx 取不到包）："
    tail -15 "$LOG"
    exit 1
fi

exit "$FAIL"
