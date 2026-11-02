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
set -euo pipefail
cd "$(dirname "$0")/.."

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

echo "▸ 暂存改动"
git add -A

if git diff --cached --quiet; then
    echo "✅ 工作区干净，没有需要提交的改动"
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
echo "   本地主目录若提示落后：cd /Users/shanziya/Documents/yimu-lit && git merge --ff-only origin/main"
