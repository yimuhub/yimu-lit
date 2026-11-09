#!/usr/bin/env bash
# 发版：打 tag → 推 tag → 建 GitHub Release。幂等，重复跑安全。
#
# 用法：
#   ./tools/release.sh                  # 按 VERSION 自动判断要不要发；正文取 CHANGELOG 该版一节
#   ./tools/release.sh "用户视角说明"    # 同上，但 Release 正文用这句（旧调用方式仍可用）
#   ./tools/release.sh --force          # tag 已存在也重建 Release（补正文用）
#
# 为什么单独一个脚本：commit.sh 在「没有改动可提交」时会提前退出，
# 而那时 VERSION 可能已经改好了——发版不该被一起跳过。
#
# 旧版（v4.15.15）只做 `git push origin main --tags`：不建 tag，而且推的是本地 main
# ——本工作树里 main 通常落后于 origin/main，等于把旧提交往回推。现在改成推当前 HEAD 的 tag。
set -euo pipefail
cd "$(dirname "$0")/.."

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

# 所有参数里，非 -- 开头的第一个当作 Release 正文
BODY=""
FORCE=0
for a in "$@"; do
    case "$a" in
        --force) FORCE=1 ;;
        -*) ;;
        *) [ -z "$BODY" ] && BODY="$a" ;;
    esac
done

VER="$(cat VERSION | sed 's/^v//')"
CUR="v${VER}"
LATEST="$(git tag -l 'v[0-9]*' --sort=-v:refname | head -1)"

# 注意：中文全角标点会被 bash 当成变量名的一部分（`$CUR（` 会变成变量 CUR（），
# 所以变量一律写成 ${CUR}。这个坑让 v4.23.1 第一次发版直接中断。
#
# v4.24.0：这里原先有一句「tag 已存在就 exit 0，无需再发」。它把 tag 存在当成
# 已发版，于是手动打过 tag（为了等 CI 绿再发）之后跑本脚本，会打印「无需再发」
# 而 Release 根本没建。幂等该由 make_releases.py 判定（它会查已有 Release 并跳过），
# 下面的打 tag / 推 tag 也各有守卫，本脚本整体可重复执行，不需要这个快路径。

NEWEST="$(printf '%s\n%s\n' "${LATEST}" "${CUR}" | sort -V | tail -1)"
if [ "${NEWEST}" != "${CUR}" ] && [ "${FORCE}" = 0 ]; then
    echo "⚠️  VERSION=${VER} 比最新 tag ${LATEST} 还旧，不自动发版" >&2
    echo "   要补发旧版本：${PY} tools/make_releases.py --apply ${CUR}" >&2
    exit 0
fi

if ! git rev-parse -q --verify "refs/tags/${CUR}" >/dev/null; then
    echo "▸ 打 tag ${CUR}（上一个：${LATEST:-无}）"
    git tag "${CUR}" HEAD
fi
if ! git ls-remote --tags origin "${CUR}" 2>/dev/null | grep -q "refs/tags/${CUR}$"; then
    echo "▸ 推送 tag ${CUR}"
    git push origin "${CUR}"
fi

if [ -n "${BODY}" ]; then
    # 旧调用方式：正文就用传进来的这句话
    echo "▸ 建 Release（正文用命令行给的那句）"
    TOKEN="$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null | sed -n 's/^password=//p')"
    if [ -z "${TOKEN}" ]; then
        echo "⚠️  没有 GitHub 凭据，请手动建 Release" >&2
        exit 0
    fi
    PAYLOAD="$("${PY}" - "${CUR}" "${BODY}" <<'PYEOF'
import json, sys
tag, body = sys.argv[1], sys.argv[2]
print(json.dumps({"tag_name": tag, "name": f"一幕·YIMU 神笔 {tag}",
                  "body": body, "draft": False, "prerelease": False}, ensure_ascii=False))
PYEOF
)"
    CODE="$(curl -s -o /tmp/release_resp.json -w '%{http_code}' \
        -X POST -H "Authorization: token ${TOKEN}" -H "Accept: application/vnd.github+json" \
        -H 'Content-Type: application/json' -d "${PAYLOAD}" \
        https://api.github.com/repos/yimuhub/yimu-lit/releases)"
    if [ "${CODE}" = "201" ]; then
        "${PY}" -c "import json;print('✅ ' + json.load(open('/tmp/release_resp.json'))['html_url'])"
    else
        echo "建 Release 返回 HTTP ${CODE}：$(head -c 300 /tmp/release_resp.json)" >&2
        exit 1
    fi
else
    echo "▸ 建 Release（正文取 CHANGELOG 该版一节）"
    # v4.32.1：默认就带 --force。推 tag 会触发 desktop-build，它的
    # softprops/action-gh-release 会建一个 name=tag、正文为空的 Release
    # 来挂 DMG/EXE；不带 --force 时 make_releases.py 看到「已存在」直接跳过，
    # 于是 v4.30.2 / v4.31.0 / v4.32.0 三个 Release 全是空壳，
    # 而 CHANGELOG 里其实写得很全。--force 走 PATCH 只覆盖标题与正文，
    # 不动已上传的 assets，幂等可重复执行；传 --force 仍兼容（等同默认）。
    "${PY}" tools/make_releases.py --apply --force "${CUR}"
fi

echo "✅ 发版完成：https://github.com/yimuhub/yimu-lit/releases/tag/${CUR}"
