#!/usr/bin/env python3
"""升版本号并同步所有「版本号副本」（一条命令，别再到处手改）。

为什么需要它：版本号的事实来源是 `VERSION`，副本却散在四处——
浏览器缓存戳两处（`static/v2/index.html` 的 `?v=NNN` 与
`static/v2/js/app.js` 的 `CSS_V = 'NNN'`），以及 `config.example.json` /
`package.json`（还有本地 `config.json`）里的 `"version"` 字段。
前者不改，用户刷新后拿到的还是旧 JS：界面没变、按钮没修，但版本号
显示已经是新的；后者不改，仓库里就会出现「VERSION 4.32.1、元数据
1.4.0」这种三套口径——那些字段代码不读、界面不显示，纯声明性，
但会让人搞不清「现在到底是哪一版」。

用法：
    python tools/bump_version.py              # 补丁位 +1：4.24.11 → 4.24.12
    python tools/bump_version.py minor        # 次版本：4.24.11 → 4.25.0
    python tools/bump_version.py major        # 主版本：4.24.11 → 5.0.0
    python tools/bump_version.py --set 4.24.12
    python tools/bump_version.py --stamp-only # 只 +1 资源戳（改了 JS/CSS 但不算新版本）
    python tools/bump_version.py --meta-only  # 不升版本、不动资源戳，只把元数据补齐到 VERSION

同步范围：VERSION、资源戳两处、config.example.json、package.json；
本地 config.json 存在时一并同步（被 .gitignore 忽略，不会进仓库）。
不碰 git（提交仍走 tools/commit.sh）。
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
# 只替换文件里第一处顶层 "version": "..."（package.json 的 dependencies
# 等嵌套对象里也可能有 version 键，不能全局替换）。
RE_META_VERSION = re.compile(r'(?m)^(\s*"version"\s*:\s*")[^"]*(")')
META_FILES = ("config.example.json", "package.json", "config.json")


def sync_meta_versions(ver: str) -> dict:
    """把元数据里的 version 字段同步到 ver；返回 {文件名: True/False/None}。

    None 表示文件不存在——`config.json` 是每台机器自管的本地配置，
    新 clone 和 CI 里都没有，不能当成失败。
    """
    changed = {}
    for name in META_FILES:
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            changed[name] = None
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        text2, n = RE_META_VERSION.subn(
            lambda m: m.group(1) + ver + m.group(2), text, count=1)
        dirty = bool(n) and text2 != text
        if dirty:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text2)
        changed[name] = dirty
    return changed


def meta_detail(changed: dict) -> str:
    return "，".join(
        f"{k} " + ("已同步" if v else "缺失(跳过)" if v is None else "已一致")
        for k, v in changed.items())


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
    ap = argparse.ArgumentParser(description="升版本号并同步所有版本号副本")
    ap.add_argument("kind", nargs="?", default="patch", choices=["patch", "minor", "major"])
    ap.add_argument("--set", dest="explicit", help="直接指定版本号，如 4.24.12")
    ap.add_argument("--stamp-only", action="store_true", help="只 +1 资源戳，不改版本")
    ap.add_argument("--no-stamp", action="store_true", help="不改资源戳")
    ap.add_argument("--meta-only", action="store_true",
                    help="不升版本、不动资源戳，只把元数据 version 补齐到当前 VERSION")
    args = ap.parse_args()

    old = read_version()
    if args.meta_only:
        changed = sync_meta_versions(old)
        print(f"VERSION 不变：{old}")
        print(f"元数据 version → {old}（{meta_detail(changed)}）")
        return 0
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
    out.append(f"元数据 version → {new}（{meta_detail(sync_meta_versions(new))}）")
    print("\n".join(out))
    print(f"\n下一步：把 v{new} 的说明写进 CHANGELOG.md，再跑 tools/commit.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
