# bot/learn.py — v0.3.4 自我进化引擎：根据用户写过的小说（题材/金手指/节奏/语言/质量）
# 不断积累经验，下次开书按偏好自动预填/推荐，并给出"学习进化报告"。
# 借鉴"偏好学习/个性化推荐"思路（累计统计 + 平滑），无外部依赖。
"""数据存 data/learn_profiles.json。记录维度：题材分布、各题材下最常用的金手指/节奏/语言、
平均成书质量（P5 质量分）。"""
import json, os, datetime
from .safeio import atomic_write_json

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
_P = os.path.join(_DATA, "learn_profiles.json")


def _load():
    try:
        return json.load(open(_P, encoding="utf-8"))
    except Exception:
        return {"total": 0, "genres": {}, "quality_sum": 0, "quality_n": 0, "recent": []}


def _save(d):
    os.makedirs(_DATA, exist_ok=True)
    atomic_write_json(_P, d)


def record_book(genre, hand="", pace="", lang=""):
    """开了一本新书：累计 题材分布 + 该题材下 金手指/节奏/语言 的偏好。"""
    d = _load()
    d["total"] = int(d.get("total") or 0) + 1
    g = genre or "未分类"
    gd = d.setdefault("genres", {}).setdefault(g, {"count": 0, "hand": {}, "pace": {}, "lang": {}})
    gd["count"] = int(gd.get("count") or 0) + 1
    for k, v in (("hand", hand), ("pace", pace), ("lang", lang)):
        if v and v not in ("自定义",):
            gd[k][v] = int(gd.get(k, {}).get(v, 0)) + 1
    d["recent"] = (d.get("recent") or []) + [g]
    d["recent"] = d["recent"][-20:]
    _save(d)
    return profile()


def record_quality(genre, score):
    """成书 P5 质量分 0-100：累计用于"我擅长/平均质量"。"""
    d = _load()
    try:
        s = float(score)
    except Exception:
        return profile()
    d["quality_sum"] = float(d.get("quality_sum") or 0) + s
    d["quality_n"] = int(d.get("quality_n") or 0) + 1
    gd = d.setdefault("genres", {}).setdefault(genre or "未分类", {})
    gd.setdefault("quality", []).append(round(s, 1))
    _save(d)
    return profile()


def _top(counter, n=3):
    return [k for k, _ in sorted((counter or {}).items(), key=lambda x: -x[1])[:n]]


def note_user_directive(key, text):
    """v2.3.0：把一条读者即时改稿指令记入学习画像（用户改稿口味样本）。

    只累积到 data/learn_profiles.json 的 user_directives（保留最近 50 条），供技能进化界面展示。
    绝不做长文本学习/统计推断，也不阻断任何调用方：全程 try/except，异常返回 {}。
    """
    try:
        text = (text or "").strip()
        if not text:
            return profile()
        d = _load()
        item = {"key": str(key or ""), "text": text[:120],
                "at": datetime.datetime.now().isoformat(timespec="seconds")}
        d["user_directives"] = (d.get("user_directives") or [])[-49:] + [item]
        _save(d)
        return profile()
    except Exception:
        return {}


def record_feedback_summary(summary):
    """保存一次反馈聚合快照（由调用方传入 aggregate() 的精简结果）。

    只做持久化，不在 import 时读反馈文件；summary 必须是可 JSON 序列化的 dict。
    """
    d = _load()
    if not isinstance(summary, dict):
        return profile()
    keep = ("total", "avg_rating", "low_rating_rate", "top_issues", "top_tags",
            "by_medium", "by_stage", "focus", "generated_at")
    snap = {k: summary.get(k) for k in keep if k in summary}
    try:
        json.dumps(snap, ensure_ascii=False)
    except (TypeError, ValueError):
        snap = {"note": "反馈摘要无法序列化，已丢弃"}
    d["feedback_summary"] = snap
    _save(d)
    return profile()


def _feedback_agg():
    """延迟 import 反馈模块：import learn 时绝不触碰 data/feedback.jsonl。"""
    try:
        from . import feedback
        return feedback.aggregate()
    except Exception:
        return {}


def _feedback_hints(agg, genres):
    """从聚合结果提取 优势 / 短板 / 下一步重点；没有反馈时给中性默认值。"""
    if not agg or not agg.get("total"):
        return [], [], []
    by_tag = agg.get("by_tag") or {}
    good, bad = [], []
    for tag, block in by_tag.items():
        n = int(block.get("rated") or 0)
        avg = block.get("avg_rating")
        if n < 2 or avg is None:
            continue
        if avg >= 4.2:
            good.append((tag, avg, n))
        elif avg <= 3.0 or float(block.get("low_rating_rate") or 0) >= 0.4:
            bad.append((tag, avg, n))
    good.sort(key=lambda x: (-x[1], -x[2]))
    bad.sort(key=lambda x: (x[1], -x[2]))
    strengths = [f"「{t}」相关反馈平均 {a:.1f} 分（{n} 条）" for t, a, n in good[:3]]
    weaknesses = [f"「{t}」相关反馈平均 {a:.1f} 分，低分率 {float(by_tag[t].get('low_rating_rate') or 0) * 100:.0f}%"
                  for t, a, _n in bad[:3]]
    focus = []
    try:
        from . import feedback
        focus = feedback.suggest_focus()[:3]
    except Exception:
        focus = []
    if not focus and genres:
        focus = [f"继续在「{genres[0]['genre']}」上积累样本，再判断稳定优势"]
    return strengths, weaknesses, focus


def suggest(genre):
    """按某题材（无则全局累计）给 金手指/节奏/语言 的推荐预填。"""
    d = _load()
    g = d.get("genres", {}).get(genre)
    merged = {"hand": {}, "pace": {}, "lang": {}}
    if g:
        for k in ("hand", "pace", "lang"):
            merged[k] = dict(g.get(k) or {})
    for gg in d.get("genres", {}).values():
        for k in ("hand", "pace", "lang"):
            for kk, vv in (gg.get(k) or {}).items():
                merged[k][kk] = merged[k].get(kk, 0) + int(vv)
    return {k: _top(merged[k], 1)[0] for k in ("hand", "pace", "lang") if _top(merged[k], 1)}


def profile():
    """学习进化报告（给 UI 展示）。旧字段 total/avg_quality/genres/insight 保持不变。"""
    d = _load()
    total = int(d.get("total") or 0)
    avg_q = round(float(d.get("quality_sum") or 0) / int(d.get("quality_n") or 1), 1) if d.get("quality_n") else None
    genres = []
    for name, g in (d.get("genres") or {}).items():
        cnt = int(g.get("count") or 0)
        qs = g.get("quality") or []
        genres.append({"genre": name, "count": cnt,
                       "share": round(cnt / total * 100) if total else 0,
                       "avg_quality": round(sum(qs) / len(qs), 1) if qs else None,
                       "top_hand": _top(g.get("hand"), 1), "top_pace": _top(g.get("pace"), 1),
                       "top_lang": _top(g.get("lang"), 1)})
    genres.sort(key=lambda x: -x["count"])
    agg = _feedback_agg()
    strengths, weaknesses, focus = _feedback_hints(agg, genres)
    directives = d.get("user_directives") or []
    return {
        "total": total, "avg_quality": avg_q, "genres": genres,
        "insight": ("你已写 %d 本" % total) + (
            ("，最常写「%s」（%d 本）" % (genres[0]["genre"], genres[0]["count"]) if genres else "") +
            (("，历史平均成书质量 %.1f 分。系统已按你的偏好预填开书选项。" % avg_q) if avg_q is not None
             else "，系统已按你的偏好预填开书选项。")),
        # 反馈侧画像：读取失败时为空列表，绝不阻断旧接口
        "strengths": strengths,
        "weaknesses": weaknesses,
        "focus": focus,
        "feedback_total": int(agg.get("total") or 0) if isinstance(agg, dict) else 0,
        # v2.3.0：用户即时改稿指令样本（技能进化界面看改稿口味）
        "recent_directives": [x.get("text") for x in directives[-10:] if isinstance(x, dict) and x.get("text")],
        "directives_total": len(directives),
    }


def author_profile():
    """作者画像：题材分布 + 成书质量 + 反馈短板 + 风格偏好，供前端展示。

    与 profile() 的区别：面向「我是谁」而不是「系统学了什么」，字段更扁平。
    """
    d = _load()
    agg = _feedback_agg()
    total = int(d.get("total") or 0)
    genres = []
    for name, g in (d.get("genres") or {}).items():
        qs = g.get("quality") or []
        genres.append({
            "genre": name,
            "count": int(g.get("count") or 0),
            "avg_quality": round(sum(qs) / len(qs), 1) if qs else None,
            "top_hand": _top(g.get("hand"), 1),
            "top_pace": _top(g.get("pace"), 1),
            "top_lang": _top(g.get("lang"), 1),
        })
    genres.sort(key=lambda x: -x["count"])
    low_dims = []
    by_tag = agg.get("by_tag") if isinstance(agg, dict) else {}
    for tag, block in (by_tag or {}).items():
        if float(block.get("low_rating_rate") or 0) >= 0.4 and int(block.get("rated") or 0) >= 1:
            low_dims.append({"tag": tag, "low_rate": block.get("low_rating_rate"),
                             "avg_rating": block.get("avg_rating")})
    low_dims.sort(key=lambda x: -float(x.get("low_rate") or 0))
    style = genres[0]["top_lang"][0] if genres and genres[0].get("top_lang") else ""
    return {
        "total_books": total,
        "avg_quality": (round(float(d.get("quality_sum") or 0) / int(d.get("quality_n") or 1), 1)
                        if d.get("quality_n") else None),
        "genres": genres,
        "preferred_style": style,
        "common_low_dims": low_dims[:5],
        "feedback_total": int(agg.get("total") or 0) if isinstance(agg, dict) else 0,
        "focus": (agg.get("top_issues") or [])[:3] if isinstance(agg, dict) else [],
    }
