"""Lorebook：从故事圣经/大纲里抽「设定词条」，按本章关键词检索后精准注入。

全书设定可能上万字，整段塞进上下文既贵又稀释注意力；这里只给本章真正相关的词条。
解析两种来源：
  1) 09-故事圣经.md 的「设定词典」Markdown 表格（名称/定义/首次出现/已知/真相/揭示计划）
  2) 01-大纲.md 里写作时追加的「- 设定：名称（已知：…，真相：…）」
"""
from __future__ import annotations

import os
import re

_CELL = re.compile(r"^\s*\|(.+)\|\s*$")
_TERM_LINE = re.compile(r"^\s*-\s*设定[:：]\s*(.+?)[（(]已知[:：](.*?)(?:[，,]\s*真相[:：](.*?))?[）)]\s*$")


def _clean(s: str) -> str:
    return re.sub(r"[*`]", "", (s or "").strip())


def _read(path: str, limit: int = 200000) -> str:
    try:
        return open(path, encoding="utf-8").read()[:limit]
    except OSError:
        return ""


def _from_table(text: str) -> list[dict]:
    out, cols, in_dict = [], None, False
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            in_dict = "设定词典" in line
            cols = None
            continue
        if not in_dict:
            continue
        m = _CELL.match(line)
        if not m:
            continue
        cells = [_clean(c) for c in m.group(1).split("|")]
        if cols is None:
            if any(c in ("名称", "术语", "设定") for c in cells):
                cols = cells
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        row = dict(zip(cols, cells, strict=False))
        name = row.get("名称") or row.get("术语") or row.get("设定") or ""
        if not name:
            continue
        out.append({"name": name,
                    "def": row.get("定义") or row.get("说明") or "",
                    "first": row.get("首次出现") or row.get("首次") or "",
                    "known": row.get("已知") or "",
                    "truth": row.get("真相") or "",
                    "plan": row.get("揭示计划") or row.get("揭示") or ""})
    return out


def entries(proj_path: str) -> list[dict]:
    merged: dict[str, dict] = {}
    bible = _read(os.path.join(proj_path, "09-故事圣经.md"))
    for e in _from_table(bible):
        merged.setdefault(e["name"], e)
    outline = _read(os.path.join(proj_path, "01-大纲.md"))
    for line in outline.splitlines():
        m = _TERM_LINE.match(line)
        if not m:
            continue
        name, known, truth = _clean(m.group(1)), _clean(m.group(2)), _clean(m.group(3) or "")
        if name and name not in merged:
            merged[name] = {"name": name, "def": "", "first": "", "known": known,
                            "truth": truth, "plan": ""}
    return list(merged.values())


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[\s，。！？,.!?:：；;、“”‘’（）()《》\-]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def relevant(proj_path: str, query: str, limit: int = 6, budget: int = 1600) -> str:
    """按关键词/二字组重合度挑最相关的设定词条，返回可直接拼进 prompt 的文本。"""
    items = entries(proj_path)
    if not items:
        return ""
    q = query or ""
    qb = _bigrams(q)
    scored = []
    for it in items:
        blob = " ".join([it["name"], it["def"], it["known"], it["truth"]])
        hit = bool(it["name"]) and it["name"] in q
        overlap = len(qb & _bigrams(blob))
        # 名称未命中时要求至少 2 个二字组重合，避免「无关/系统」这类通用词误召回
        if not hit and overlap < 2:
            continue
        score = overlap + (100 + len(it["name"]) if hit else 0)
        scored.append((score, it))
    if not scored:
        return ""
    scored.sort(key=lambda x: x[0], reverse=True)
    lines, used = [], 0
    for _score, it in scored[:max(1, limit)]:
        bits = [f"定义：{it['def']}"] if it["def"] else []
        if it["known"]:
            bits.append(f"已知：{it['known']}")
        if it["truth"]:
            bits.append(f"真相：{it['truth']}")
        if it["first"]:
            bits.append(f"首次：{it['first']}")
        if it["plan"]:
            bits.append(f"揭示计划：{it['plan']}")
        line = f"- 【{it['name']}】" + "；".join(bits)
        if used + len(line) > budget and lines:
            break
        lines.append(line[:budget])
        used += len(line)
    return "\n".join(lines)
