"""导演工作单（v4.37.0）——总编辑把作者的话翻译成各岗位能直接执行的指令。

作者不会用固定句式，也说不清「结构/因果/人物」这些术语。总编辑理解他的话后，
产出一张工作单：要什么效果、什么不能动、什么必须改、怎样算改好了、禁止什么、
作用范围是全书/下一章/指定章。写作、改写、审稿、重规划都读同一张单子，
避免每个岗位各自猜测作者意图。
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import uuid

from .safeio import atomic_write_text

FILE_NAME = "briefs.jsonl"
_LOCK = threading.RLock()
_SCOPES = ("book", "next", "chapters")
_SCOPE_ALIASES = {
    "全书": "book", "整本": "book", "本书": "book", "长期": "book", "book": "book",
    "下一章": "next", "这一章": "next", "下章": "next", "next": "next",
    "指定章": "chapters", "这几章": "chapters", "章节": "chapters", "chapters": "chapters",
}
_STATUS = ("open", "done", "revoked", "superseded")


def _path(pdir: str) -> str:
    return os.path.join(pdir, FILE_NAME)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _text(v, cap=500) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        v = "；".join(str(x) for x in v if str(x).strip())
    return str(v).strip()[:cap]


def _items(v, cap=240, limit=12) -> list[str]:
    if isinstance(v, str):
        v = [x for x in v.replace("\n", "；").split("；") if x.strip()]
    if not isinstance(v, (list, tuple)):
        return []
    out, seen = [], set()
    for x in v:
        s = _text(x, cap)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _chapters(v) -> list[int]:
    if isinstance(v, (int, float)):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for x in v:
        try:
            n = int(str(x).strip())
        except (TypeError, ValueError):
            continue
        if 0 < n < 10000 and n not in out:
            out.append(n)
    return out[:40]


def normalize(raw: dict, source_text: str = "", kind: str = "directive") -> dict:
    """把总编辑输出的 brief（或半成品）规整成可持久化的工作单。"""
    raw = raw if isinstance(raw, dict) else {}
    scope = str(raw.get("scope") or "book").strip().lower()
    scope = _SCOPE_ALIASES.get(scope, scope)
    if scope not in _SCOPES:
        scope = "book"
    chapters = _chapters(raw.get("chapters"))
    if scope == "chapters" and not chapters:
        scope = "book"
    goal = _text(raw.get("goal"), 400)
    if not goal:
        goal = _text(raw.get("summary") or raw.get("requirement") or source_text, 400)
    return {
        "id": f"b-{datetime.datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}",
        "created_at": _now(),
        "kind": str(kind or "directive"),
        "status": "open",
        "scope": scope,
        "chapters": chapters,
        "goal": goal,
        "must_keep": _items(raw.get("must_keep") or raw.get("keep")),
        "must_change": _items(raw.get("must_change") or raw.get("change")),
        "acceptance": _items(raw.get("acceptance") or raw.get("criteria")),
        "avoid": _items(raw.get("avoid") or raw.get("banned")),
        "source_text": _text(source_text, 300),
    }


def load(pdir: str) -> list[dict]:
    """读取全部工作单（新→旧）。损坏行跳过，绝不影响写作主链路。"""
    p = _path(pdir)
    if not os.path.exists(p):
        return []
    out = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict) and d.get("id"):
                    out.append(d)
    except OSError:
        return []
    return list(reversed(out))


def add(pdir: str, raw: dict, source_text: str = "", kind: str = "directive") -> dict:
    """追加一张工作单并落盘。返回规整后的 dict。"""
    if not pdir or not os.path.isdir(pdir):
        return {}
    item = normalize(raw, source_text=source_text, kind=kind)
    try:
        with _LOCK:
            os.makedirs(pdir, exist_ok=True)
            with open(_path(pdir), "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        return {}
    return item


def _rewrite(pdir: str, items: list[dict]) -> None:
    body = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in items)
    atomic_write_text(_path(pdir), body)


def set_status(pdir: str, brief_id: str, status: str) -> bool:
    if status not in _STATUS:
        return False
    try:
        with _LOCK:
            items = list(reversed(load(pdir)))
            hit = False
            for x in items:
                if x.get("id") == brief_id:
                    x["status"] = status
                    x["closed_at"] = _now()
                    hit = True
                    break
            if hit:
                _rewrite(pdir, items)
            return hit
    except Exception:
        return False


def revoke(pdir: str, brief_ids=None, chapter=None) -> int:
    """撤销工作单：给了 id 就撤这些；否则撤指定章/全部。返回撤销条数。"""
    ids = {str(x) for x in (brief_ids or []) if str(x).strip()}
    n = 0
    try:
        with _LOCK:
            items = list(reversed(load(pdir)))
            for x in items:
                if x.get("status") != "open":
                    continue
                if ids and x.get("id") not in ids:
                    continue
                if chapter is not None and int(chapter) not in (x.get("chapters") or []) \
                        and x.get("scope") != "book":
                    continue
                x["status"] = "revoked"
                x["closed_at"] = _now()
                n += 1
            if n:
                _rewrite(pdir, items)
    except Exception:
        return 0
    return n


def open_items(pdir: str) -> list[dict]:
    return [x for x in load(pdir) if x.get("status") == "open"]


def applicable(pdir: str, chapter=None) -> list[dict]:
    """当前生效的工作单（新→旧）。book 全书生效；chapters 命中指定章才生效。"""
    out = []
    try:
        ch = int(chapter) if chapter is not None else None
    except (TypeError, ValueError):
        ch = None
    for x in open_items(pdir):
        scope = x.get("scope")
        if scope == "book":
            out.append(x)
        elif scope == "chapters" and (ch is None or ch in (x.get("chapters") or [])):
            out.append(x)
    return out


def render_one(x: dict) -> str:
    lines = []
    if x.get("goal"):
        lines.append(f"目标：{x['goal']}")
    for label, key in (("必须保留", "must_keep"), ("必须改到", "must_change"),
                       ("验收标准", "acceptance"), ("禁止", "avoid")):
        if x.get(key):
            lines.append(f"{label}：{'；'.join(x[key])}")
    scope = x.get("scope")
    if scope == "chapters" and x.get("chapters"):
        lines.append(f"作用范围：第{'、'.join(map(str, x['chapters']))}章")
    elif scope == "book":
        lines.append("作用范围：全书长期有效")
    return "；".join(lines)


def render_lines(x: dict) -> list[str]:
    """给改写维度/队列用的短句列表。"""
    out = []
    if x.get("goal"):
        out.append(f"【总编辑工作单】{x['goal']}")
    if x.get("must_keep"):
        out.append("必须保留：" + "；".join(x["must_keep"]))
    if x.get("must_change"):
        out.append("必须改到：" + "；".join(x["must_change"]))
    if x.get("acceptance"):
        out.append("验收标准：" + "；".join(x["acceptance"]))
    if x.get("avoid"):
        out.append("禁止：" + "；".join(x["avoid"]))
    return out


def render_block(pdir: str, agent: str = "writer", chapter=None, limit: int = 6,
                 max_chars: int = 2400) -> str:
    """把当前生效的工作单渲染成某个岗位的提示词块；没有则返回空串。"""
    items = applicable(pdir, chapter=chapter)[:limit]
    if not items:
        return ""
    if agent == "reviewer":
        head = ("【审稿依据·总编辑工作单（作者授意，评分与挑问题时必须据此检查；"
                "通用 rubric 不能替代作者的验收标准）】")
    elif agent == "planner":
        head = "【结构要求·总编辑工作单（重规划/调整章卡时必须落实）】"
    else:
        head = ("【总编辑工作单（作者授意，优先级高于一般风格偏好；与本章冲突时以此为准，"
                "较新的工作单优先）】")
    lines = [head]
    for i, x in enumerate(items, 1):
        body = render_one(x)
        if body:
            lines.append(f"{i}. {body}")
    out = "\n".join(lines)
    return out[:max_chars]
