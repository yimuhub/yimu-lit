#!/usr/bin/env python3
"""按版本给 GitHub 建 Release：tag 已是事实来源，这里只负责「缺哪个补哪个」。

## 数据从哪来
- **版本序列**：`git tag -l 'v4.*'`（version 排序），不再硬编码——新增版本不用改脚本。
- **正文**：优先用 CHANGELOG.md 里该版本那一节（人工维护、信息最全）；
  CHANGELOG 没有的早期版本，退回该 tag 的提交说明，并标注「历史版本补录」。
- **对比链接**：与排序里的上一个 tag 比，能从 v4.18.0 一路点到现在。

## 用法
    python tools/make_releases.py                # 只打印计划（默认 dry-run）
    python tools/make_releases.py --apply        # 真发布
    python tools/make_releases.py --apply v4.24.0 v4.24.1   # 只发指定版本

幂等：已存在同名 Release 直接跳过，可重复运行。按版本升序创建，
所以 Releases 页的时间倒序恰好就是版本倒序，不会乱。
"""
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "yimuhub/yimu-lit"
PREFIX = "一幕·YIMU 神笔 "
# 这几个版本当初没单独提交，与 v4.23.0 共用一个 commit：
# 给 compare 链接只会点出别人的改动，所以只写说明、不给链接。
SAME_COMMIT = {"v4.22.1", "v4.22.3"}


def token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):]
    raise SystemExit("找不到 GitHub 凭据（git credential fill 无 password）")


def git(*args) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout


def tags() -> list:
    out = git("tag", "-l", "v4.*", "--sort=v:refname")
    return [t for t in out.split() if re.fullmatch(r"v4\.\d+\.\d+", t)]


def changelog() -> dict:
    s = open("CHANGELOG.md", encoding="utf-8").read()
    parts = re.split(r"(?m)^## (v[\d.]+)\s*[—\-–]\s*(.*)$", s)
    return {parts[i]: parts[i + 2] for i in range(1, len(parts), 3)}


def trim(body: str) -> str:
    """去掉正文末尾的空行与分隔线（CHANGELOG 各节之间常有 --- ）。"""
    lines = body.strip("\n").splitlines()
    while lines and (not lines[-1].strip()
                     or re.fullmatch(r"[-*_]{3,}", lines[-1].strip())):
        lines.pop()
    return "\n".join(lines).strip()


def api(url: str, tok: str, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(f"https://api.github.com{url}", data=data, method=method)
    req.add_header("Authorization", f"token {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def body_for(v: str, prev: str, sec: dict) -> str:
    if v in sec:
        text = trim(sec[v])
    else:
        subj = git("log", "-1", "--format=%s", v).strip()
        text = (f"{subj}\n\n> 历史版本补录：CHANGELOG 未收录本版本，"
                f"说明取自提交记录 `{git('rev-parse', '--short', v).strip()}`。")
    if v in SAME_COMMIT:
        return text + ("\n\n---\n\n> **说明**：本版改动与 v4.23.0 落在同一个提交"
                       "（`dcbb6c1`），期间没有单独提交，因此 tag 指向同一 commit，"
                       "不提供版本间对比。")
    if not prev:
        return text
    return text + (f"\n\n---\n\n**完整对比**："
                   f"[{prev}...{v}](https://github.com/{REPO}/compare/{prev}...{v})")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_ = "--apply" in sys.argv
    tok = token()
    sec = changelog()
    order = tags()
    if args:
        order = [t for t in order if t in set(args)]
        if not order:
            raise SystemExit(f"没有匹配的 tag：{args}")

    status, rels = api(f"/repos/{REPO}/releases?per_page=100", tok)
    have = {r["tag_name"] for r in rels} if status == 200 else set()
    print(f"本地 tag {len(tags())} 个 | 已有 Release {len(have)} 个 | 本次候选 {len(order)} 个\n")

    all_tags = tags()
    created, skipped, failed = [], 0, []
    for v in order:
        i = all_tags.index(v)
        prev = all_tags[i - 1] if i > 0 else ""
        text = body_for(v, prev, sec)
        name = f"{PREFIX}{v}"
        kind = "CHANGELOG" if v in sec else "提交说明"

        if v in have:
            skipped += 1
            continue
        if not apply_:
            print(f"[DRY] {v:<9} {kind:<5} body={len(text):>4} 字  prev={prev or '-'}")
            continue

        code, res = api(f"/repos/{REPO}/releases", tok, "POST", {
            "tag_name": v, "name": name, "body": text,
            "draft": False, "prerelease": False,
        })
        if code in (200, 201):
            created.append(v)
            print(f"✓ {v:<9} [{kind}] {res.get('html_url')}")
        else:
            failed.append(v)
            print(f"✗ {v:<9} HTTP {code}: {res.get('message')} {res.get('errors')}")

    if apply_:
        print(f"\n发布 {len(created)} 个 | 跳过已存在 {skipped} 个 | 失败 {len(failed)} 个")
        if failed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
