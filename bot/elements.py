"""故事元件库（v4.40.0）：给规划器一份「可选的冲突/情绪/人性/谜题/场景」菜单。

这里不做运行时随机抽卡。随机元件只会制造公式化的义务：同一张卡被抽到两次，
作者就得为它硬编一场戏。正确用法是**规划时由规划师按本章目标挑选 1–3 个**，
写进章卡 `elements`，写作时只兑现已选元件。
"""
from __future__ import annotations

import os
import re
from functools import cache

CATEGORIES = {
    "conflict": "冲突",
    "emotion": "情绪",
    "humanity": "人性",
    "mystery": "谜题",
    "scene": "场景",
}

_GENRE_PRIORITY = {
    "history": ("conflict", "scene", "humanity", "mystery", "emotion"),
    "xuanhuan": ("conflict", "mystery", "emotion", "humanity", "scene"),
    "xianxia": ("conflict", "humanity", "mystery", "scene", "emotion"),
    "scifi": ("mystery", "conflict", "humanity", "scene", "emotion"),
    "mystery": ("mystery", "conflict", "scene", "humanity", "emotion"),
    "romance": ("emotion", "humanity", "conflict", "scene", "mystery"),
    "urban": ("conflict", "emotion", "humanity", "scene", "mystery"),
    "workplace": ("conflict", "humanity", "scene", "emotion", "mystery"),
    "military": ("conflict", "scene", "humanity", "mystery", "emotion"),
    "horror": ("mystery", "scene", "emotion", "humanity", "conflict"),
}

_FALLBACK = {
    "conflict": [("债主逼债", "期限与金额具体到某一天。"), ("内鬼泄密", "自己人把情报卖给对手。")],
    "emotion": [("憋屈后的反手", "先吃亏，再用规则打回去。"), ("当众被认可", "被最有分量的人公开承认。")],
    "humanity": [("救人还是保项目", "救一个人会毁掉全盘进度。"), ("复仇的边界", "仇人已无力反抗，杀不杀。")],
    "mystery": [("失踪的账册", "账册不见，但每页都有人记得。"), ("被改过的图纸", "改动很小，后果很大。")],
    "scene": [("当众对质", "私下矛盾搬到众人面前。"), ("雨夜交易", "天气替双方保密，也放大风险。")],
}


def _root() -> str:
    try:
        from . import skill
        return os.path.join(skill._skill_root(), "references", "elements")
    except Exception:
        return ""


def _parse(path: str) -> list[dict]:
    out = []
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"^\s*[-*+]\s*(?:\*\*)?(.+?)(?:\*\*)?\s*[:：]\s*(.+)$", line)
        if not m:
            continue
        name = m.group(1).strip()[:24]
        desc = m.group(2).strip()[:120]
        if name:
            out.append({"name": name, "desc": desc})
    return out


@cache
def library() -> dict[str, list[dict]]:
    root = _root()
    out = {}
    for cat in CATEGORIES:
        items = _parse(os.path.join(root, f"{cat}.md")) if root else []
        if not items:
            items = [{"name": n, "desc": d} for n, d in _FALLBACK.get(cat, [])]
        out[cat] = items
    return out


def _genre_key(genre) -> str:
    try:
        from . import skillkit
        return skillkit._genre_key(genre) or ""
    except Exception:
        return ""


def menu(genre=None, limit: int = 10) -> list[dict]:
    """按题材给出元件菜单（确定性顺序，不随机）。"""
    lib = library()
    order = _GENRE_PRIORITY.get(_genre_key(genre), tuple(CATEGORIES))
    out = []
    for i in range(max(len(v) for v in lib.values()) if lib else 0):
        for cat in order:
            items = lib.get(cat) or []
            if i < len(items):
                x = dict(items[i])
                x["category"] = cat
                x["label"] = CATEGORIES.get(cat, cat)
                out.append(x)
            if len(out) >= limit:
                return out
    return out[:limit]


def menu_text(genre=None, limit: int = 10) -> str:
    return "\n".join(f"- [{x['label']}] {x['name']}：{x['desc']}" for x in menu(genre, limit))


def names(genre=None, limit: int = 20) -> list[str]:
    return [x["name"] for x in menu(genre, limit)]


def has(name: str) -> bool:
    n = str(name or "").strip()
    return any(x["name"] == n for items in library().values() for x in items)