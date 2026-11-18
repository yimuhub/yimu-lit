"""导出前确定性清理（v4.41.0）。

解决的问题
----------
交付闸门扫描的是「真正会进入成书的正文」。历史稿件里有一批问题是**纯格式残留**，
不需要模型、也不该让模型重写：

  - `<escape>` / `<text>` 等模型标签（被算成非中文残留 N01，还会伪装成截断 N03）；
  - 正文里重复回显的章题行（「第三章 第一次交易与打脸」）；
  - 「第N章 / 上一章 / 前一章 / 本章」这类创作层指代（N02）；
  - `>` 引用块、`###` 标题、`**加粗**` 等排版残留（N18）；
  - 模型把「诊断/修订方案」写回正文（第217/264章实证）。

这些都能被规则证明、且清理后语义不变。以前导出只做体检，不做清理，于是闸门
每次都报同一批问题；前端再把章节丢给整章重写，重写分数没提高又被回滚，形成
「每次导出都是同一份内容」的死循环。这里把「能确定性修的」在闸门前修掉，
剩下的（真正的半句截断）才交给补写流程。
"""
from __future__ import annotations

import os
import re

from . import book_health, craft_check, purity

_TAG_RE = re.compile(
    r"</?(?:escape|text|think|analysis|output|markdown|tool|result|answer)\b[^>]*>",
    re.I)
_MISSING_RE = re.compile(r"【\s*(?:缺失|待补|TODO|待定)[^】]{0,60}】", re.I)
_CH_NUM = r"[0-9０-９一二三四五六七八九十百零两]+"
_CHAPTER_LINE_RE = re.compile(rf"^第\s*{_CH_NUM}\s*章(?:\s+\S.{{0,30}})?$")
_CHAPTER_HEAD_RE = re.compile(rf"^(#{{1,6}})\s*(第\s*{_CH_NUM}\s*章.*)$")
_NUMBER_RE = re.compile(rf"第\s*{_CH_NUM}\s*章")
_KNOWN_SECTION = ("本章概要", "章首引子", "正文", "章节备注")

# 模型诊断/修订方案的开头特征。只在段首连续命中时剥离，避免误吃正文。
_NOTE_HEADS = (
    "当前文本", "目前文本", "当前诊断", "目前诊断", "本次定点", "本次修订",
    "已能确认", "目前已能确认", "原句：", "原句:", "建议替换为", "修改思路",
    "改写说明", "改稿说明", "修订原则", "请补充", "请直接粘贴", "请补发",
    "为避免大面积误改", "我再直接交付", "目前这份", "当前这份", "这处保留",
    "这处修改", "这处调整", "拿到完整", "输出完整", "只修改", "只交付",
)
_NOTE_MARKS = (
    "blocking 项", "blocking项", "建议替换为", "本次定点修订", "定点修订原则",
    "为避免大面积误改", "我再直接交付", "剩余章节", "章节末尾不完整",
    "章节备注在", "原文及剩余", "完整修订版", "只修改诊断指出的位置",
    "不擅自补写", "请补充", "请直接粘贴", "请补发", "无法可靠", "诊断",
    "Markdown", "修改说明", "修订原则", "保留原有", "不擅自", "保持情节",
)


def _cn(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def _is_note_para(p: str) -> bool:
    s = re.sub(r"\*+", "", (p or "").strip()).strip()
    if not s:
        return True
    if s.startswith(("-", "*", "•", "·")):
        return True
    if len(s) < 100 and re.match(r"^\d{1,2}[.、)]\s*", s):
        return True
    head = s[:80]
    if any(head.startswith(h) for h in _NOTE_HEADS):
        return True
    hits = sum(1 for m in _NOTE_MARKS if m in s)
    return hits >= 2


def _strip_note_prefix(text: str) -> str:
    """剥掉正文开头整段「诊断 + 修订原则」；找不到可信正文就原样返回。"""
    paras = re.split(r"\n\s*\n", text or "")
    i = 0
    while i < len(paras) and _is_note_para(paras[i]):
        i += 1
    if i <= 0:
        return text
    rest = "\n\n".join(paras[i:]).strip()
    if _cn(rest) < 200:
        return text
    return rest


def _drop_note_paras(text: str) -> str:
    """模型把诊断/修订清单夹在正文中间时，逐段清掉；正文不会因此洗没。"""
    paras = re.split(r"\n\s*\n", text or "")
    kept = [p for p in paras if not _is_note_para(p)]
    if len(kept) == len(paras):
        return text
    rest = "\n\n".join(kept).strip()
    if _cn(rest) < 200:
        return text
    return rest


def _normalize_headings(text: str) -> str:
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if not s.startswith("#"):
            out.append(line)
            continue
        title = s.lstrip("#").strip()
        if not title:
            continue
        if title in _KNOWN_SECTION or title.startswith(_KNOWN_SECTION):
            out.append(line)
            continue
        if s.startswith("###"):
            # 正文里的小标题（「### 重新开机」）降为普通段落，不算排版污染
            out.append(title)
        else:
            out.append(line)
    return "\n".join(out)


def _drop_title_echo(text: str) -> str:
    out = []
    seen_title = False
    for line in text.split("\n"):
        s = line.strip()
        m = _CHAPTER_HEAD_RE.match(s)
        if m:
            body = m.group(2).strip()
            nums = _NUMBER_RE.findall(body)
            if len(nums) > 1:
                body = nums[0] + body[body.rfind(nums[-1]) + len(nums[-1]):].strip()
            # 只保留第一个章题作为题头；正文中间的回显一律删除
            if not seen_title:
                out.append("# " + re.sub(r"\s+", " ", body).strip())
                seen_title = True
            continue
        if seen_title and _CHAPTER_LINE_RE.match(s):
            continue
        out.append(line)
    return "\n".join(out)


def _clean_prose(text: str) -> str:
    t = _TAG_RE.sub("", text or "")
    t = _MISSING_RE.sub("", t)
    t = re.sub(r"(?m)^\s*>\s?", "", t)          # 引用块标记；保留文字
    t = craft_check._strip_meta_refs(t)
    t = purity.strip_md_style(t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _clean_structured(text: str) -> str:
    """只清理可导出的引子/正文，保留章题与概要/备注原文。"""
    head, secs = purity._split_sections(text)
    title_lines = [ln for ln in head if _CHAPTER_HEAD_RE.match(ln.strip())]
    if not secs:
        body = _clean_prose("\n".join(ln for ln in head
                                      if not _CHAPTER_HEAD_RE.match(ln.strip())))
        return "\n".join(title_lines + ([body] if body else []))
    out = list(title_lines)
    for kind, title, level, lines in secs:
        body = "\n".join(lines)
        if kind in ("intro", "body"):
            body = _clean_prose(body)
        out.append("#" * max(2, level) + " " + title)
        if body.strip():
            out.append(body)
    return "\n\n".join(x.strip("\n") for x in out if x.strip())


def clean_chapter_text(raw: str, n: int = 0, title: str = "") -> str:
    """返回清理后的完整章节文本（保留概要/正文/备注结构）。"""
    t = _TAG_RE.sub("", raw or "")
    t = _MISSING_RE.sub("", t)
    t = _strip_note_prefix(t)
    t = _drop_note_paras(t)
    t = _drop_title_echo(t)
    t = _normalize_headings(t)
    t = _clean_structured(t)
    try:
        t = purity.sanitize_chapter(t)
    except Exception:
        pass
    return t.strip() + "\n" if t.strip() else ""


def repair(proj, chapters=None) -> dict:
    """在导出闸门前把纯格式残留清掉；只写确有必要修改的章节。"""
    plan = proj.plan() or {}
    only = {int(x) for x in (chapters or []) if str(x).strip().isdigit()} or None
    changed, skipped = [], []
    for ch in plan.get("chapters") or []:
        try:
            no = int(ch.get("chapterNumber") or 0)
        except (TypeError, ValueError):
            continue
        if not no or (only and no not in only):
            continue
        fp = os.path.join(proj.path, ch.get("filePath") or "")
        if not ch.get("filePath") or not os.path.exists(fp):
            continue
        try:
            raw = open(fp, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        new = clean_chapter_text(raw, no, ch.get("title") or "")
        if not new.strip() or new.strip() == raw.strip():
            continue
        old_body = purity.chapter_body(raw)
        new_body = purity.chapter_body(new)
        # 清理不能把正文洗没：去除的是诊断/元信息段，正文允许少一半以内。
        if _cn(new_body) < max(200, int(_cn(old_body) * 0.5)):
            skipped.append(no)
            continue
        try:
            res = book_health.save_chapter(proj, no, new, label="导出前确定性清理",
                                           kind="manual", snapshot=True)
        except Exception as e:
            skipped.append(no)
            proj.log(f"⚠️ 第{no}章导出前清理失败：{e}")
            continue
        if res.get("ok") and not res.get("unchanged"):
            changed.append(no)
    if changed:
        proj.log("🧹 导出前确定性清理 %d 章（标签/章题回显/章节号引用/排版残留）：%s" % (
            len(changed), "、".join(f"第{n}章" for n in changed[:12])
            + ("…" if len(changed) > 12 else "")))
    return {"changed": changed, "skipped": skipped}
