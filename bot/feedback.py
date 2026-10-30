"""用户反馈采集与聚合（一幕·YIMU 神笔 · 技能自我进化底座）。

职责：
1. 把「评分 / 标签 / 批注 / 修改前后稿」落成一行一条 JSON（data/feedback.jsonl）；
2. 用确定性规则聚合出「哪些文体、阶段、技能、标签最容易出问题」；
3. 把「用户实际怎么改稿」导出成 before/after 配对，供候选技能生成与固定评测使用。

数据文件：``data/feedback.jsonl``（追加写；超过 ~20MB 自动轮转为 .1）。
安全边界：
- 所有字符串先清控制字符，再按字段上限裁剪；
- 密钥样式（sk-…、Bearer …）与用户主目录路径会被替换成占位符；
- 本模块只做本地文件读写，不发网络请求、不调用 LLM。

为什么不用数据库：反馈是低频写入、按行追加即可，JSONL 坏一行不影响其它行，
测试也能直接 monkeypatch 文件路径。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
UTC = timezone.utc  # v4.7.1 兼容 Python 3.9/3.10（datetime.UTC 自 3.11 起才有；与 timezone.utc 等价）

from . import medium as medium_mod
from .safeio import atomic_write_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
#: 反馈主文件；测试可 monkeypatch 到 tmp_path。
FEEDBACK_PATH = os.path.join(DATA_DIR, "feedback.jsonl")
#: 单文件上限，超过就轮转为 .1（只保留一份历史，避免无限膨胀）。
MAX_FILE_BYTES = 20 * 1024 * 1024

MAX_NOTE = 2000
MAX_DRAFT = 20000
MAX_PAIR_DRAFT = 4000
MAX_DIFF_SEGMENTS = 20
MAX_SEGMENT_CHARS = 400
MAX_LINES = 50000

_LOCK = threading.Lock()

# 控制字符（保留 \n \t）与常见凭据/主目录路径
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,}|"
    r"(?:api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;，；]{4,})",
    re.I,
)
_HOME_RE = re.compile(r"(/Users/[^\s，。；、）】]+|[A-Za-z]:\\Users\\[^\s，。；、）】]+)")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

_TAG_RULES: list[tuple[tuple[str, ...], str]] = [
    (("拖", "水", "节奏", "慢", "冗长"), "压缩过渡与解释段：一章只保留一个核心冲突，删掉不改变局面的对话。"),
    (("人设", "ooc", "降智", "人物", "动机"), "先写人物「此刻最想要什么 / 最怕失去什么」，台词与选择必须由这两条驱动。"),
    (("对白", "台词", "对话", "书面"), "对白一次只回答一件事，允许答非所问；删掉解释设定的句子，让信息藏在动作里。"),
    (("逻辑", "因果", "bug", "漏洞", "不合理"), "每个转折前补一个可被读者提前看到的原因；写完用「因为…所以…」逐段复述一遍。"),
    (("ai", "腔", "套话", "空泛", "假"), "删掉总结句与形容词堆叠，换成可被看见的动作、数量、专有名词；一句一个信息。"),
    (("重复", "复读", "老套", "桥段"), "建立重复清单：同一爽点/比喻/句式 10 章内不得复用两次；轮换冲突类型与场景空间。"),
    (("说明", "信息", "设定", "堆砌"), "设定拆成三份：本章必须知道的、可以演示的、留到后面的；只交付第一份。"),
    (("结尾", "钩子", "断章", "悬念"), "章末用「新事实 / 新代价 / 新威胁」收束，不用纯情绪感叹；最后一屏必须产生下一个问题。"),
    (("格式", "排版", "场景头", "规范"), "按行业格式重排：场景头、动作行、对白、转场各占其位；交付前通读场景头是否齐全。"),
    (("格律", "平仄", "押韵", "韵部"), "先定韵部再落笔；成稿后逐句核对平仄与押韵，宁可换字，不要破格。"),
    (("意象", "比喻", "陈词"), "意象要来自同一场景的物件系统；删掉「仿佛/宛如」后面的抽象名词，换成具体动作。"),
    (("结构", "散", "跑题", "主线"), "用一句话写出每段的功能；删掉不推进主线也不加深人物的段落。"),
    (("细节", "空洞", "笼统"), "每 300 字至少一个可验证细节：时间、地点、数量、品牌、身体感受。"),
    (("风格", "语气", "不像", "改写"), "先摘 3 句用户原稿作为文风锚点，新写的句子逐句对照句长与用词密度。"),
]

_MEDIUM_DEFAULTS = {
    "novel": [
        "先补齐卷弧与章卡，再动笔：每章只推进一个核心冲突，章末留下新问题。",
        "连续性问题优先于文采：人物位置、时间、伤势、物品先对齐状态库。",
        "爽点按类型轮换，同一桥段 10 章内不重复；重复出现时改变代价而不是加大音量。",
    ],
    "script": [
        "每场戏先写「谁要什么 / 谁挡着 / 结果变了什么」，说不清就删。",
        "对白承担动作而不是解释；把设定说明改成冲突中的选择。",
        "集尾钩子落在新事实或新代价上，不用纯情绪收尾。",
    ],
    "poetry": [
        "先定体式、韵部与情感核心，再找意象；意象必须服务同一场景。",
        "成稿后逐句核对平仄、押韵与对仗，格律问题先于词句润色。",
        "删掉解释性的抽象词，把情绪交给动作、光线、声音与器物。",
    ],
    "prose": [
        "每段只做一件事：叙述、描写、议论、转折各司其职。",
        "用可验证的细节替代形容词；没有事实就留缺口标记，不要用套话填。",
        "完稿后朗读一遍，删掉读起来打结的句子与重复的句首。",
    ],
    "": [
        "先把「用户改了什么」记录清楚，再谈改法；没有 before/after 的反馈不可执行。",
        "低分反馈按标签聚类，一次只修一类问题，避免全文重写。",
        "修订后回到同一批固定评测用例复跑，确认没有把别的阶段改坏。",
    ],
}


# ---------------------------------------------------------------- 基础工具

def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def _redact(text: str) -> str:
    """凭据与绝对路径不进日志；宁可少记，不可泄露。"""
    text = _SECRET_RE.sub("[已隐藏]", text)
    return _HOME_RE.sub("[路径]", text)


def _clean(value, limit: int) -> tuple[str, bool]:
    """清控制字符 + 脱敏 + 截断；返回 (文本, 是否被截断)。"""
    if value is None:
        return "", False
    if not isinstance(value, str):
        value = str(value)
    value = _CTRL_RE.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    value = _redact(value).strip()
    if len(value) > limit:
        return value[:limit] + "…", True
    return value, False


def _clean_list(value, *, max_items: int, max_len: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = re.split(r"[,，;；\s]+", value)
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        s, _ = _clean(item, max_len)
        if s and s not in out:
            out.append(s)
        if len(out) >= max_items:
            break
    return out


def _clean_meta(value, depth: int = 0):
    """只保留可 JSON 序列化的浅层结构；密钥键直接丢弃。"""
    if depth > 3:
        return None
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:30]:
            key = _CTRL_RE.sub("", str(k))[:50]
            if re.search(r"(token|secret|password|api[_-]?key|authorization)", key, re.I):
                continue
            cv = _clean_meta(v, depth + 1)
            if cv is not None:
                out[key] = cv
        return out
    if isinstance(value, (list, tuple)):
        return [_clean_meta(v, depth + 1) for v in list(value)[:20]]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return _redact(_CTRL_RE.sub("", value))[:500]
    return None


def _atomic_append(rec: dict) -> None:
    """单行 JSON 追加。O_APPEND + 一次 write，进程内加锁，进程间靠 O_APPEND 原子性。"""
    path = FEEDBACK_PATH
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
    data = line.encode("utf-8")
    with _LOCK:
        try:
            if os.path.exists(path) and os.path.getsize(path) > MAX_FILE_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data)
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)


def _iter_records():
    """逐行读；坏行静默跳过（反馈文件可能被手工编辑或写到一半）。"""
    try:
        with open(FEEDBACK_PATH, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


# ---------------------------------------------------------------- 写入

def record(feedback: dict) -> dict:
    """校验并追加一条反馈；成功返回落盘记录（含 ok=True），非法返回中文错误。"""
    if not isinstance(feedback, dict):
        return _err("反馈必须是 JSON 对象")

    medium = str(feedback.get("medium") or "").strip()
    if medium and not medium_mod.is_medium(medium):
        return _err(f"文体非法：{medium}")

    rating = feedback.get("rating")
    if rating in ("", None):
        rating = None
    else:
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            return _err("rating 必须是 1–5 的整数或 null")
        if rating < 1 or rating > 5:
            return _err("rating 必须在 1–5 之间")

    accepted = feedback.get("accepted")
    if accepted not in (True, False, None):
        return _err("accepted 必须是 true / false / null")

    chapter_no = feedback.get("chapter_no")
    if chapter_no in ("", None):
        chapter_no = None
    else:
        try:
            chapter_no = int(chapter_no)
        except (TypeError, ValueError):
            return _err("chapter_no 必须是整数")
        if chapter_no < 0 or chapter_no > 10_000_000:
            return _err("chapter_no 超出合理范围")

    note, note_cut = _clean(feedback.get("note"), MAX_NOTE)
    before, before_cut = _clean(feedback.get("before"), MAX_DRAFT)
    after, after_cut = _clean(feedback.get("after"), MAX_DRAFT)

    meta = feedback.get("meta")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        return _err("meta 必须是 JSON 对象")
    meta = _clean_meta(meta) or {}
    try:
        if len(json.dumps(meta, ensure_ascii=False)) > 8000:
            meta = {"_truncated": True, "note": "meta 过大已裁剪"}
    except (TypeError, ValueError):
        meta = {"_truncated": True, "note": "meta 无法序列化已丢弃"}

    if before_cut:
        meta["before_truncated"] = True
    if after_cut:
        meta["after_truncated"] = True
    if note_cut:
        meta["note_truncated"] = True

    kind = _clean(feedback.get("kind") or "review", 20)[0] or "review"
    kind = re.sub(r"[^a-z0-9_-]", "", kind.lower()) or "review"

    rec = {
        "id": "fb_" + secrets.token_hex(6),
        "ts": _now(),
        "kind": kind,
        "project_key": _clean(feedback.get("project_key"), 120)[0],
        "medium": medium,
        "stage": _clean(feedback.get("stage"), 12)[0],
        "chapter_no": chapter_no,
        "message_id": _clean(feedback.get("message_id"), 200)[0],
        "rating": rating,
        "tags": _clean_list(feedback.get("tags"), max_items=20, max_len=40),
        "note": note,
        "before": before,
        "after": after,
        "skill_names": _clean_list(feedback.get("skill_names"), max_items=50, max_len=80),
        "accepted": accepted,
        "meta": meta,
    }
    try:
        _atomic_append(rec)
    except OSError as exc:
        return _err(f"反馈写入失败：{exc.__class__.__name__}")
    rec["ok"] = True
    return rec


def capture_edit(project_key: str, chapter_no, before: str, after: str, **kw) -> dict:
    """记录一次「用户改稿」，用差异摘要代替重复存两整章。

    - similarity：0–1，序列相似度；越低说明用户重写幅度越大
    - 增删字数按实际增删片段统计，不是简单长度差
    - 超长时 before/after 各截断到 4000 字，并标 truncated；完整差异摘要仍保留
    """
    if not isinstance(before, str) or not isinstance(after, str):
        return _err("before / after 必须是字符串")
    sim = difflib.SequenceMatcher(None, before, after, autojunk=False).ratio()
    added = removed = 0
    segments: list[dict] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, before, after, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old = before[i1:i2]
        new = after[j1:j2]
        if tag in ("replace", "delete"):
            removed += len(old)
        if tag in ("replace", "insert"):
            added += len(new)
        if len(segments) < MAX_DIFF_SEGMENTS:
            segments.append({
                "op": tag,
                "at": i1,
                "before": _clip_segment(old),
                "after": _clip_segment(new),
            })
    before_store = before[:MAX_PAIR_DRAFT]
    after_store = after[:MAX_PAIR_DRAFT]
    meta = {
        "kind": "diff",
        "similarity": round(sim, 4),
        "chars_added": added,
        "chars_removed": removed,
        "segments": segments,
        "segment_count": len(segments),
        "segments_truncated": len(segments) >= MAX_DIFF_SEGMENTS,
        "before_chars": len(before),
        "after_chars": len(after),
        "truncated": len(before) > MAX_PAIR_DRAFT or len(after) > MAX_PAIR_DRAFT,
    }
    user_meta = kw.pop("meta", None)
    if isinstance(user_meta, dict):
        meta.update(user_meta)
    kw.setdefault("kind", "edit")
    kw.setdefault("accepted", None)
    return record({
        "project_key": project_key,
        "chapter_no": chapter_no,
        "before": before_store,
        "after": after_store,
        "meta": meta,
        **kw,
    })


def _clip_segment(text: str) -> str:
    if len(text) <= MAX_SEGMENT_CHARS:
        return text
    return text[:MAX_SEGMENT_CHARS] + "…"


# ---------------------------------------------------------------- 读取

def list_feedback(project_key: str = "", medium: str = "", stage: str = "",
                  limit: int = 100) -> list[dict]:
    """按写入时间倒序返回最近的反馈；坏行跳过，limit 做上下限保护。"""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 100
    out: list[dict] = []
    for rec in _iter_records():
        if project_key and rec.get("project_key") != project_key:
            continue
        if medium and rec.get("medium") != medium:
            continue
        if stage and rec.get("stage") != stage:
            continue
        out.append(rec)
    return list(reversed(out))[:limit]


def _rate_block(recs: list[dict]) -> dict:
    rated = [r for r in recs if isinstance(r.get("rating"), int)]
    n = len(rated)
    avg = round(sum(r["rating"] for r in rated) / n, 2) if n else None
    low = sum(1 for r in rated if r["rating"] <= 2)
    return {
        "count": len(recs),
        "rated": n,
        "avg_rating": avg,
        "low_count": low,
        "low_rating_rate": round(low / n, 4) if n else 0.0,
    }


def aggregate() -> dict:
    """聚合：结构稳定，空数据也返回完整键，前端不用判空。"""
    recs = list(_iter_records())
    by_medium: dict[str, dict] = {}
    by_stage: dict[str, dict] = {}
    by_tag: dict[str, dict] = {}
    by_skill: dict[str, dict] = {}
    tag_low: dict[str, int] = {}
    tag_all: dict[str, int] = {}
    edits: list[dict] = []

    for rec in recs:
        med = rec.get("medium") or "未标注"
        stg = rec.get("stage") or "未标注"
        by_medium[med] = _accumulate(by_medium.get(med), rec)
        by_stage[stg] = _accumulate(by_stage.get(stg), rec)
        rating = rec.get("rating")
        low = isinstance(rating, int) and rating <= 2
        for tag in rec.get("tags") or []:
            tag = str(tag)[:40]
            by_tag[tag] = _accumulate(by_tag.get(tag), rec)
            tag_all[tag] = tag_all.get(tag, 0) + 1
            if low:
                tag_low[tag] = tag_low.get(tag, 0) + 1
        for name in rec.get("skill_names") or []:
            name = str(name)[:80]
            by_skill[name] = _accumulate(by_skill.get(name), rec)
        if rec.get("kind") == "edit" or (rec.get("before") and rec.get("after")):
            edits.append(rec)

    for block in list(by_medium.values()) + list(by_stage.values()) + \
            list(by_tag.values()) + list(by_skill.values()):
        rated = block.pop("_ratings", [])
        n = len(rated)
        block["rated"] = n
        block["avg_rating"] = round(sum(rated) / n, 2) if n else None
        low = block.pop("_low", 0)
        block["low_count"] = low
        block["low_rating_rate"] = round(low / n, 4) if n else 0.0

    sims = [float((r.get("meta") or {}).get("similarity") or 0) for r in edits]
    accepted = [r for r in edits if r.get("accepted") is not None]
    accept_rate = (round(sum(1 for r in accepted if r["accepted"]) / len(accepted), 4)
                   if accepted else None)
    base = _rate_block(recs)
    top_issues = [
        {"tag": k, "count": v}
        for k, v in sorted(tag_low.items(), key=lambda x: (-x[1], x[0]))[:5]
    ]
    if not top_issues:
        top_issues = [
            {"tag": k, "count": v}
            for k, v in sorted(tag_all.items(), key=lambda x: (-x[1], x[0]))[:5]
        ]

    return {
        "total": len(recs),
        "rated": base["rated"],
        "avg_rating": base["avg_rating"],
        "low_count": base["low_count"],
        "low_rating_rate": base["low_rating_rate"],
        "by_medium": by_medium,
        "by_stage": by_stage,
        "by_tag": by_tag,
        "by_skill": by_skill,
        "edits": {
            "count": len(edits),
            "avg_similarity": round(sum(sims) / len(sims), 4) if sims else None,
            "accept_rate": accept_rate,
            "with_diff": sum(1 for r in edits if (r.get("meta") or {}).get("segments")),
        },
        "top_issues": top_issues,
        "top_tags": [{"tag": k, "count": v}
                     for k, v in sorted(tag_all.items(), key=lambda x: (-x[1], x[0]))[:10]],
        "recent_notes": [str(r.get("note"))[:200] for r in reversed(recs)
                         if r.get("note")][:5],
        "generated_at": _now(),
    }


def _accumulate(block: dict | None, rec: dict) -> dict:
    block = block or {"count": 0, "_ratings": [], "_low": 0}
    block["count"] += 1
    rating = rec.get("rating")
    if isinstance(rating, int):
        block["_ratings"].append(rating)
        if rating <= 2:
            block["_low"] += 1
    return block


def suggest_focus(medium: str = "", stage: str = "") -> list[str]:
    """把低分标签与改稿幅度翻译成中文改进建议；纯规则，不调用 LLM。"""
    recs = list_feedback(medium=medium, stage=stage, limit=1000)
    text = " ".join(
        " ".join(str(t) for t in (r.get("tags") or [])) + " " + str(r.get("note") or "")
        for r in recs
    ).lower()
    low_tags: dict[str, int] = {}
    for rec in recs:
        rating = rec.get("rating")
        if isinstance(rating, int) and rating <= 2:
            for tag in rec.get("tags") or []:
                key = str(tag)[:40]
                low_tags[key] = low_tags.get(key, 0) + 1

    out: list[str] = []
    for keys, advice in _TAG_RULES:
        if any(k in text or k in low_tags for k in keys):
            if advice not in out:
                out.append(advice)
        if len(out) >= 6:
            break

    sims = [float((r.get("meta") or {}).get("similarity") or 0)
            for r in recs if r.get("kind") == "edit"]
    if sims and sum(sims) / len(sims) < 0.6:
        out.insert(0, "用户对生成稿的平均改写幅度超过 40%：先对照 before/after 摘要校准文风与信息密度，再谈局部润色。")

    rated = [r["rating"] for r in recs if isinstance(r.get("rating"), int)]
    if rated and sum(1 for x in rated if x <= 2) / len(rated) > 0.3:
        out.insert(0, "低分占比偏高：暂停扩写，先用固定评测集复跑当前技能，确认问题集中在哪个阶段。")

    if not out:
        out = list(_MEDIUM_DEFAULTS.get(medium, _MEDIUM_DEFAULTS[""]))
    return out[:6]


def export_pairs(limit: int = 200) -> list[dict]:
    """导出有实质差异的 before/after，供候选技能生成与评测；过滤纯格式变动。"""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    out: list[dict] = []
    for rec in reversed(list(_iter_records())):
        before = rec.get("before")
        after = rec.get("after")
        if not isinstance(before, str) or not isinstance(after, str):
            continue
        nb = re.sub(r"\s+", "", before)
        na = re.sub(r"\s+", "", after)
        if not nb or not na or nb == na:
            continue
        ratio = difflib.SequenceMatcher(None, nb, na, autojunk=False).ratio()
        if ratio > 0.999 and abs(len(na) - len(nb)) < 2:
            continue
        out.append({
            "id": rec.get("id"),
            "ts": rec.get("ts"),
            "project_key": rec.get("project_key") or "",
            "medium": rec.get("medium") or "",
            "stage": rec.get("stage") or "",
            "chapter_no": rec.get("chapter_no"),
            "rating": rec.get("rating"),
            "tags": rec.get("tags") or [],
            "skill_names": rec.get("skill_names") or [],
            "before": before,
            "after": after,
            "similarity": round(ratio, 4),
            "meta": rec.get("meta") or {},
        })
        if len(out) >= limit:
            break
    return out


def prune(max_lines: int = MAX_LINES) -> dict:
    """只保留最近 N 行；用于长期运行后的体积控制。"""
    try:
        max_lines = max(1, int(max_lines))
    except (TypeError, ValueError):
        return _err("max_lines 必须是正整数")
    with _LOCK:
        try:
            with open(FEEDBACK_PATH, encoding="utf-8") as f:
                lines = [ln for ln in f if ln.strip()]
        except OSError:
            return {"ok": True, "kept": 0, "removed": 0}
        kept = lines[-max_lines:]
        try:
            atomic_write_text(FEEDBACK_PATH, "".join(kept))
        except OSError as exc:
            return _err(f"裁剪失败：{exc.__class__.__name__}")
    return {"ok": True, "kept": len(kept), "removed": len(lines) - len(kept)}
