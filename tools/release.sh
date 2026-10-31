#!/usr/bin/env bash
# v4.15.15 发版三件套：tag + GitHub release 一次完成（防漏建 release 导致检查更新停旧版本）
# 用法: ./tools/release.sh "版本说明（用户视角）"
set -e
TAG="v$(cat VERSION | sed 's/^v//')"
MSG="${1:-发布 $TAG}"
git push origin main --tags
python3 - "$TAG" "$MSG" << 'PYEOF'
import json, sys, os, urllib.request
tag, msg = sys.argv[1], sys.argv[2]
token = None
try:
    import subprocess
    out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n",
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            token = line.split("=", 1)[1]
except Exception:
    pass
if not token:
    print("⚠ 未取到 GitHub 凭据，请手动创建 release"); sys.exit(0)
payload = json.dumps({"tag_name": tag, "target_commitish": "main", "name": f"一幕·YIMU 神笔 {tag}",
                      "body": msg, "draft": False, "prerelease": False}).encode()
req = urllib.request.Request("https://api.github.com/repos/yimuhub/yimu-lit/releases", data=payload, method="POST",
    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "yimu-lit"})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    print(f"✅ release 已建：{d.get('html_url')}")
except urllib.error.HTTPError as e:
    err = json.loads(e.read().decode())
    code = (err.get("errors") or [{}])[0].get("code", "")
    print("已存在" if code == "already_exists" else f"建 release 失败：{e.code} {err.get('message','')}")
PYEOF
