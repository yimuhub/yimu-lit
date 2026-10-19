# bot/learn.py — v0.3.4 自我进化引擎：根据用户写过的小说（题材/金手指/节奏/语言/质量）
# 不断积累经验，下次开书按偏好自动预填/推荐，并给出"学习进化报告"。
# 借鉴"偏好学习/个性化推荐"思路（累计统计 + 平滑），无外部依赖。
"""数据存 data/learn_profiles.json。记录维度：题材分布、各题材下最常用的金手指/节奏/语言、
平均成书质量（P5 质量分）。"""
import json, os

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
_P = os.path.join(_DATA, "learn_profiles.json")


def _load():
    try:
        return json.load(open(_P, encoding="utf-8"))
    except Exception:
        return {"total": 0, "genres": {}, "quality_sum": 0, "quality_n": 0, "recent": []}


def _save(d):
    os.makedirs(_DATA, exist_ok=True)
    json.dump(d, open(_P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


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
    """学习进化报告（给 UI 展示）。"""
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
    return {"total": total, "avg_quality": avg_q, "genres": genres,
            "insight": ("你已写 %d 本" % total) + (
                ("，最常写「%s」（%d 本）" % (genres[0]["genre"], genres[0]["count"]) if genres else "") +
                (("，历史平均成书质量 %.1f 分。系统已按你的偏好预填开书选项。" % avg_q) if avg_q is not None
                 else "，系统已按你的偏好预填开书选项。"))}
