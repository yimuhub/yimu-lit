#!/usr/bin/env python3
"""旧作品正文净化自愈（v4.22.1）。

v4.22.1 之前写盘的章节文件，可能残留「修改说明 / 审核评分 / 质量自检 / 字数统计」
等过程元信息小节，以及无标题尾部噪声（`本章评分 86 分`、评分表）。这些残留会顺着
成书与导出流出去。本脚本用 `bot.purity.sanitize_chapter()` 就地重洗一遍。

用法：
    python tools/purify_data.py                 # 体检（默认，只读，不写任何文件）
    python tools/purify_data.py --apply         # 实际写回（自动备份到 .purify-backup/）
    python tools/purify_data.py --root projects # 指定项目根目录
    python tools/purify_data.py --apply --prune-manuscript
                                               # 同时删除过期的 成书-*.md（它是构建产物）

安全约定：
  * 默认 dry-run，只打印「将删除哪些小节 / 节省多少字」。
  * `--apply` 才写盘，且先把原文件复制到同项目下的 `.purify-backup/<时间戳>/`。
  * 净化后正文为空（异常文件）时**跳过不写**，绝不把文件洗没。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import purity  # noqa: E402

CHAP_GLOB_PREFIX = "第"
MANUSCRIPT_PREFIX = "成书-"


def _iter_projects(root: str):
    if not os.path.isdir(root):
        return
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if os.path.isdir(path):
            yield path


def _walk_files(proj_dir: str):
    """返回 (章节文件列表, 成书文件列表)，递归但不进备份目录。"""
    chapters, books = [], []
    for dirpath, dirnames, filenames in os.walk(proj_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            if fn.startswith(MANUSCRIPT_PREFIX):
                books.append(full)
            elif fn.startswith(CHAP_GLOB_PREFIX):
                chapters.append(full)
    return sorted(chapters), sorted(books)


def _diff(old: str, new: str) -> tuple[str, str]:
    """返回 (是否真有污染被去掉, 人读说明)。

    只差空行/换行归一化不算污染——那样会把整库文件都标成「需净化」，属于噪声。
    """
    import re

    cut = len(old) - len(new)
    old_heads = re.findall(r"^#{1,3}\s*(.+)$", old, re.M)
    new_heads = set(re.findall(r"^#{1,3}\s*(.+)$", new, re.M))
    lost = [h.strip() for h in old_heads if h not in new_heads]
    if lost:
        return True, f"删除小节：{'、'.join(lost[:6])}（-{cut} 字）"
    if cut > 0:
        return True, f"剥离尾部噪声（-{cut} 字）"
    return False, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="旧作品正文净化自愈（默认只读体检）")
    ap.add_argument("--root", default="projects", help="项目根目录，默认 projects/")
    ap.add_argument("--apply", action="store_true", help="实际写回（默认只体检）")
    ap.add_argument(
        "--prune-manuscript",
        action="store_true",
        help="同时删除过期的 成书-*.md（构建产物，下次导出自动重建）",
    )
    args = ap.parse_args()

    root = args.root
    if not os.path.isdir(root):
        print(f"✗ 目录不存在：{root}")
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    total_files = dirty_files = total_cut = 0
    backups: list[tuple[str, str]] = []
    pruned = 0

    for proj in _iter_projects(root):
        chapters, books = _walk_files(proj)
        if not chapters and not books:
            continue
        proj_dirty = 0
        for path in chapters:
            total_files += 1
            try:
                with open(path, encoding="utf-8") as f:
                    old = f.read()
            except Exception as e:
                print(f"  ! 读取失败 {path}: {e}")
                continue
            new = purity.sanitize_chapter(old)
            if not new.strip():
                print(f"  ! 净化后为空，已跳过（请人工检查）：{path}")
                continue
            dirty, hint = _diff(old, new)
            if not dirty:
                continue
            proj_dirty += 1
            dirty_files += 1
            total_cut += max(0, len(old) - len(new))
            print(f"  · {os.path.relpath(path, root)}  {hint}")
            if args.apply:
                bak_dir = os.path.join(proj, ".purify-backup", stamp)
                os.makedirs(bak_dir, exist_ok=True)
                bak = os.path.join(bak_dir, os.path.basename(path))
                shutil.copy2(path, bak)
                backups.append((path, bak))
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)

        if args.prune_manuscript and books:
            for b in books:
                print(f"  - 删除过期成书 {os.path.relpath(b, root)}")
                pruned += 1
                if args.apply:
                    os.remove(b)

        if proj_dirty:
            print(f"  └ {os.path.basename(proj)}：{proj_dirty} 章需净化")

    print()
    print("─" * 60)
    print(f"扫描章节文件：{total_files}  其中需净化：{dirty_files}  可减字符：{total_cut}")
    if args.prune_manuscript:
        print(f"过期成书文件：{pruned}（{'已删除' if args.apply else '未删除，加 --apply 生效'}）")
    if not args.apply:
        print("这是体检模式，未改动任何文件。确认无误后加 --apply 执行。")
    else:
        if backups:
            print(f"已写回 {len(backups)} 个文件，原文件备份在各自项目的 .purify-backup/{stamp}/")
        print("提示：成书文件已过期的项目，重新导出一次即可重建（现在走逐章净化口径）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
