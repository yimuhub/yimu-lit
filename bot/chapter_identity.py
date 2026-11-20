"""章节身份单一事实源：计划标题 = 文件名 = 正文 H1。

走查实证：同一本书里三套标题互相漂移——计划叫“医疗走廊的目击者”，文件名叫
“第09章-抽屉里的纸.md”，正文第一行是“## 章首引子”；导出用计划标题，用户
在 Finder 里看到旧名，20/20 章全部错位。

本模块只做确定性、可回滚的迁移：
  * 文件改名：第NN章-<计划标题>.md（旧文件先复制到 .versions/chNNN/）；
  * 正文补/换 H1：`# 第NN章 <计划标题>`；
  * 计划 filePath 同步到新文件。
不删除任何正文，不改变章节内容，不调用 LLM。
"""
from __future__ import annotations

import datetime
import os
import re
import shutil


def safe_title(title: str, fallback: str = "") -> str:
    t = re.sub(r'[\\/:*?"<>|\n\r\t]+', "", str(title or "").strip())
    t = re.sub(r"\s+", "", t)[:24]
    return t or fallback or "未命名"


def canonical_name(no: int, title: str, fallback: str = "") -> str:
    return f"第{int(no):02d}章-{safe_title(title, fallback)}.md"


def _h1(no: int, title: str) -> str:
    return f"# 第{int(no):02d}章 {safe_title(title)}"


def _backup(proj, no: int, path: str) -> None:
    try:
        d = os.path.join(proj.path, ".versions", f"ch{int(no):03d}")
        os.makedirs(d, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dst = os.path.join(d, f"{ts}-canonical-before.md")
        shutil.copy2(path, dst)
    except Exception:
        pass


def _ensure_h1(text: str, no: int, title: str) -> str:
    head = _h1(no, title)
    # 已在第一行就是本章 H1：替换标题（兼容 "# 第 07 章 旧名"）
    first = text.splitlines()[0] if text.splitlines() else ""
    if re.match(r"^#\s*第\s*0?%d\s*章" % int(no), first.strip()):
        rest = text.split("\n", 1)
        return head + ("\n" + rest[1] if len(rest) > 1 else "\n")
    # 其它情况（以 ## 本章概要 / 章首引子 / 正文段落开头）：补在文件最前
    return head + "\n\n" + text.lstrip("\n")


def sync_project(proj, *, rename: bool = True, persist: bool = True) -> dict:
    """把项目内所有章节对齐到计划标题；返回 {renamed, heads, conflicts}。"""
    plan = proj.plan() or {}
    chapters = plan.get("chapters") or []
    changed = renamed = heads = 0
    conflicts = []
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        try:
            no = int(ch.get("chapterNumber") or 0)
        except (TypeError, ValueError):
            continue
        title = str(ch.get("title") or "").strip()
        if not no or not title:
            continue
        old_rel = str(ch.get("filePath") or "").strip()
        old = os.path.join(proj.path, os.path.basename(old_rel)) if old_rel else ""
        if not old or not os.path.isfile(old):
            continue
        new_name = canonical_name(no, title)
        new = os.path.join(proj.path, new_name)
        if rename and os.path.basename(old) != new_name:
            if os.path.exists(new):
                conflicts.append({"n": no, "old": os.path.basename(old), "new": new_name})
            else:
                _backup(proj, no, old)
                try:
                    os.replace(old, new)
                    ch["filePath"] = new_name
                    renamed += 1
                    changed += 1
                    old = new
                except OSError as e:
                    conflicts.append({"n": no, "old": os.path.basename(old), "new": new_name,
                                      "error": str(e)[:80]})
        # H1 统一（无论是否改名都做）
        try:
            txt = open(old, encoding="utf-8", errors="replace").read()
            fixed = _ensure_h1(txt, no, title)
            if fixed != txt:
                from .safeio import atomic_write_text
                atomic_write_text(old, fixed)
                heads += 1
        except Exception as e:
            conflicts.append({"n": no, "old": os.path.basename(old), "error": f"h1:{e}"[:80]})
    if persist and changed:
        proj.save_plan(plan)
    try:
        proj.log(f"🧷 章节身份对齐：改名 {renamed} 章，补/换 H1 {heads} 章"
                 + (f"，冲突 {len(conflicts)} 项" if conflicts else ""))
    except Exception:
        pass
    return {"renamed": renamed, "heads": heads, "conflicts": conflicts}
