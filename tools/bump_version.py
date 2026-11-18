#!/usr/bin/env python3
"""升版本号并同步前端资源戳（一条命令，别再三处手改）。

为什么需要它：版本号在 `VERSION`，而浏览器缓存戳在**两个**地方——
`static/v2/index.html` 的 `?v=NNN` 与 `static/v2/js/app.js` 的 `CSS_V = 'NNN'`。
只改 VERSION 不改戳，用户刷新后拿到的还是旧 JS：界面没变、按钮没修，
但版本号显示已经是新的——这种「看起来更新了其实没更新」最难排查。

用法：
    python tools/bump_version.py              # 补丁位 +1：4.24.11 → 4.24.12
    python tools/bump_version.py minor        # 次版本：4.24.11 → 4.25.0
    python tools/bump_version.py major        # 主版本：4.24.11 → 5.0.0
    python tools/bump_version.py --set 4.24.12
    python tools/bump_version.py --stamp-only # 只 +1 资源戳（改了 JS/CSS 但不算新版本）

只动这三个文件，不碰 git（提交仍走 tools/commit.sh）。
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(ROOT, "VERSION")
INDEX_FILE = os.path.join(ROOT, "static", "v2", "index.html")
APPJS_FILE = os.path.join(ROOT, "static", "v2", "js", "app.js")

RE_INDEX = re.compile(r"\?v=(\d+)")
RE_CSSV = re.compile(r"const CSS_V = '(\d+)'")


def read_version():
    with open(VERSION_FILE, encoding="utf-8") as f:
        return f.read().strip().lstrip("v")


def parse(ver):
    parts = [int(x) for x in re.split(r"[.\-+]", ver) if x.isdigit()]
    while len(parts) < 3:
        parts.append(0)
    return parts[:3]


def bump(ver, kind):
    major, minor, patch = parse(ver)
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def current_stamp():
    """取现有资源戳（两处应一致；不一致时取较大值，避免回退到旧缓存）。"""
    stamps = []
    for path, rx in ((INDEX_FILE, RE_INDEX), (APPJS_FILE, RE_CSSV)):
        try:
            with open(path, encoding="utf-8") as f:
                stamps += [int(m) for m in rx.findall(f.read())]
        except OSError:
            pass
    return max(stamps) if stamps else 0


def write_stamp(stamp):
    """把资源戳写到两处；返回各自替换的处数。"""
    changed = {}
    with open(INDEX_FILE, encoding="utf-8") as f:
        html = f.read()
    html2, n1 = RE_INDEX.subn(f"?v={stamp}", html)
    if n1:
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            f.write(html2)
    changed["index.html"] = n1

    with open(APPJS_FILE, encoding="utf-8") as f:
        js = f.read()
    js2, n2 = RE_CSSV.subn(f"const CSS_V = '{stamp}'", js)
    if n2:
        with open(APPJS_FILE, "w", encoding="utf-8") as f:
            f.write(js2)
    changed["app.js(CSS_V)"] = n2
    return changed


def main():
    ap = argparse.ArgumentParser(description="升版本号并同步前端资源戳")
    ap.add_argument("kind", nargs="?", default="patch", choices=["patch", "minor", "major"])
    ap.add_argument("--set", dest="explicit", help="直接指定版本号，如 4.24.12")
    ap.add_argument("--stamp-only", action="store_true", help="只 +1 资源戳，不改版本")
    ap.add_argument("--no-stamp", action="store_true", help="不改资源戳")
    args = ap.parse_args()

    old = read_version()
    new = old if args.stamp_only else (args.explicit or bump(old, args.kind))
    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        print(f"❌ 版本号格式应为 X.Y.Z，收到：{new}", file=sys.stderr)
        return 1

    if not args.stamp_only:
        with open(VERSION_FILE, "w", encoding="utf-8") as f:
            f.write(new + "\n")

    out = [f"VERSION: {old} → {new}" if not args.stamp_only else f"VERSION 不变：{new}"]
    if not args.no_stamp:
        old_stamp = current_stamp()
        stamp = old_stamp + 1
        changed = write_stamp(stamp)
        detail = "，".join(f"{k} {v} 处" for k, v in changed.items())
        out.append(f"资源戳: {old_stamp} → {stamp}（{detail}）")
        if not all(changed.values()):
            out.append("⚠️  有文件没匹配到戳，检查 index.html / app.js 是否被改过格式")
    print("\n".join(out))
    print(f"\n下一步：把 v{new} 的说明写进 CHANGELOG.md，再跑 tools/commit.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
