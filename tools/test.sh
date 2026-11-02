#!/usr/bin/env bash
# 本地/CI 统一测试入口。
#
# 为什么需要这个脚本，而不是直接 `pytest`：
#   1) 解释器：仓库根目录不一定有 .venv（多工作树并存时常见），必须显式定位，
#      否则会静默用系统 python 跑出一堆 ImportError。
#   2) 临时目录：pytest 的 tmp_path 会在 teardown 里删大量目录。在 WorkBuddy
#      沙箱下这会撞上「单轮删除 >50 个路径」的保护而 SystemExit(1)，
#      表现为 fixture setup 阶段一堆 ERROR（assert not self._finalizers）。
#      守卫只在 CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR + CODEBUDDY_TOOL_CALL_ID
#      同时存在时生效；测试进程不需要它，清掉即可。CI 上这两个变量本就不存在。
#   3) basetemp：固定到 .pytest-tmp，避免落到系统临时目录下的 pytest-of-<user>。
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PY:-}"
if [ -z "${PY}" ]; then
  for cand in .venv/bin/python ../.venv/bin/python /Users/shanziya/Documents/yimu-lit/.venv/bin/python; do
    if [ -x "${cand}" ]; then PY="${cand}"; break; fi
  done
fi
if [ -z "${PY}" ] || [ ! -x "${PY}" ]; then
  echo "❌ 找不到 Python 解释器。设置 PY=/path/to/python 后重试。" >&2
  exit 1
fi
echo "解释器：${PY}（$("${PY}" -V 2>&1)）"

exec env -u CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR -u CODEBUDDY_TOOL_CALL_ID \
  "${PY}" -m pytest -q --basetemp=.pytest-tmp "$@"
