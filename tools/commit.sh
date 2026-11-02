#!/usr/bin/env bash
# v4.23.0 一键提交：体检 → 暂存 → commit → 推送 origin/main
#
# 为什么要脚本：快速迭代期每次改动都要落库，手工敲 add/commit/push 容易
#   ① 漏文件（新模块忘了 git add，别人一拉就 ImportError）
#   ② 漏版本号（代码进库了，VERSION/CHANGELOG 还是上一版）
#   ③ 把本地运行产物、个人目录一起提交进去
# 三件事都由脚本兜住。
#
# 用法：
#   ./tools/commit.sh "新增: v4.23.0 一句话说清改了什么"
#
# 版本号以 VERSION 为准；说明里必须带 v版本号，CHANGELOG 里必须有这一版条目，
# 否则直接拒绝提交——宁可现在拦一下，也不要事后翻提交历史找「哪版改的」。
#
# 这个脚本**不跑 CI**：体检在本地做（静态检查 + 需要时手动跑 pytest），
# 推上去就是纯代码，不触发任何 Actions 分钟数。
#
# 顺带发版：推送后比较 VERSION 与最新 tag，领先就自动
#   git tag → 推 tag → 建 GitHub Release（正文取 CHANGELOG 该版一节）。
# 版本号没动就什么都不做，不会重复发。
set -euo pipefail
cd "$(dirname "$0")/.."

# 解释器优先级：$SHENBI_PY → 仓库 .venv → 系统 python3
# 接线体检要 import app.py，所以必须是装了依赖的那个解释器。
if [ -n "${SHENBI_PY:-}" ]; then
    PY="$SHENBI_PY"
elif [ -x .venv/bin/python ]; then
    PY=".venv/bin/python"
else
    PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "❌ 找不到 Python（可用 SHENBI_PY 指定）" >&2
    exit 1
fi

MSG="${1:-}"
if [ -z "$MSG" ]; then
    echo "用法: ./tools/commit.sh \"新增: v4.23.0 说明\"" >&2
    exit 1
fi

VER="$(cat VERSION | sed 's/^v//')"
if ! printf '%s' "$MSG" | grep -q "v$VER"; then
    echo "❌ 提交说明里没有当前版本 v$VER（VERSION 是 $VER）" >&2
    echo "   要么补进说明，要么先把 VERSION 改成你要的版本" >&2
    exit 1
fi
if ! grep -q -- "$VER" CHANGELOG.md; then
    echo "❌ CHANGELOG.md 里找不到 $VER，先把这一版写进去" >&2
    exit 1
fi

echo "▸ 本地体检（不联网、不进 CI）"
bash tools/static_check.sh

# 接线体检：前端 api.xxx 必须真实存在，前端 /api 路径必须有后端路由。
# 加这一步是因为 v4.21.1 删掉 api.onboarding 后，新建作品弹窗空白了好几个版本，
# 期间 eslint 和 pytest 全绿——没有任何检查看这两者的对应关系。
echo "▸ 接线体检（api.* ←→ api.js、前端路径 ←→ 后端路由）"
if ! SHENBI_AUTH=0 "$PY" tools/audit_wiring.py --strict > /tmp/audit_wiring.txt 2>&1; then
    cat /tmp/audit_wiring.txt >&2
    echo "❌ 接线体检未通过，已中止提交" >&2
    exit 1
fi
grep -E "^✅" /tmp/audit_wiring.txt || true

echo "▸ 暂存改动"
git add -A

if git diff --cached --quiet; then
    echo "✅ 工作区干净，没有需要提交的改动"
    # 只改了 VERSION 却没别的改动时，这里仍应把 tag/Release 补上
    bash tools/release.sh
    exit 0
fi

echo "▸ 本次将提交："
git diff --cached --stat | tail -20

git commit -q -m "$MSG"

# 推送策略：直接推到远端 main。
# 不用 git branch -f main —— 主工作目录检出着 main，git 会拒绝改写它；
# 而 HEAD:main 同样能把提交送上去，本地 main 落后时下次 ff 一下即可。
git push origin HEAD:main

echo "✅ 已推送 origin/main ← $(git rev-parse --short HEAD)"

# 发版交给 tools/release.sh（幂等）：VERSION 比最新 tag 新就打 tag + 建 Release。
# 单独成脚本是因为「没有改动可提交」时也要能发版，见下面那个提前退出的分支。
bash tools/release.sh
