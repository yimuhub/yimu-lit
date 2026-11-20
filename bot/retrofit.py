"""旧书整改（v4.40.0）：给已经写了几百章的书一份「先改哪 20 章」的可执行清单。

为什么不能直接全书重写
----------------------
一本 300 章、90 万字的书，全书重写是几十小时的 LLM 账单，而且会把已经没问题
的章节改坏。真实产出物的问题往往集中在少数结构位置：书名承诺从没兑现、
对手从未具名、某几章全是公文、某几章评分掉到 60 以下。先做外科手术，再决定
要不要大改，才是对作者时间和 token 都负责的做法。

本模块只读、只算，不调 LLM、不改正文。输出：
  27-旧书整改方案.json / 27-旧书整改方案.md

优先级（与作者定的顺序一致）：
  1. 字数不足 / 结尾截断（先补全，没资格谈文笔）
  2. 评分 < 60（已有评分里最差的那批）
  3. 故事引擎高风险章（书名承诺缺失、对手空转、场景无代价）
  4. 文书密度最高的章（读者反馈「在看公文」的位置）
  5. 章卡仍是空壳的章（需要先补规划，不是重写正文）
"""
from __future__ import annotations

import json
import os

from . import canon, craft_check, engine_check, longform

REPORT_JSON = "27-旧书整改方案.json"
REPORT_MD = "27-旧书整改方案.md"
TOP_N = 20


def _pdir(proj) -> str:
    return os.path.abspath(getattr(proj, "path", "") or ".")


def _chapters(proj) -> list[dict]:
    try:
        return canon._chapters(proj)
    except Exception:
        return []


def _score_map(proj) -> dict:
    try:
        from . import writer as _writer
        return {int(p["ch"]): p.get("score") for p in _writer.per_chapter_scores(proj)
                if isinstance(p.get("score"), (int, float))}
    except Exception:
        return {}


def plan(proj, top_n: int = TOP_N) -> dict:
    chapters = _chapters(proj)
    cards = {}
    try:
        cards = longform.load_cards(proj)
    except Exception:
        pass
    canon_data = {}
    try:
        canon_data = canon.scan(proj)
    except Exception:
        pass
    eng = {}
    try:
        eng = engine_check.scan(proj, chapters=chapters, cards=cards)
    except Exception:
        pass
    scores = _score_map(proj)
    min_words = int((proj.plan() or {}).get("minWordsPerChapter") or 3000)

    # 逐章确定性指标
    rows = []
    for ch in chapters:
        n = int(ch.get("n") or 0)
        text = ch.get("text") or ""
        if not n:
            continue
        try:
            from . import wordcount
            wc = wordcount.count(text)
        except Exception:
            wc = len(text)
        issues = []
        if wc < int(min_words * 0.9):
            issues.append(f"字数 {wc} < 验收线 {int(min_words * 0.9)}")
        try:
            hard = craft_check.hard_findings(text, n=n)
        except Exception:
            hard = []
        for f in hard:
            issues.append(f"{f['gate']} {f['message']}")
        try:
            pw = craft_check.check_paperwork_density(text, n=n)
        except Exception:
            pw = []
        for f in pw:
            issues.append(f"C24 {f['message']}")
        card = cards.get(n) or {}
        if not card.get("scenes") and not (card.get("engine") or {}):
            issues.append("章卡无场景链/引擎字段（先补规划）")
        sc = scores.get(n)
        score_rank = sc if isinstance(sc, (int, float)) else None
        rows.append({"n": n, "title": ch.get("title") or "", "words": wc,
                     "score": score_rank, "issues": issues,
                     "paperwork": ((pw[0].get("metric") or {}).get("per_k") if pw else 0)})

    row_map = {r["n"]: r for r in rows}
    eng_by_ch: dict[int, list] = {}
    for f in (eng.get("findings") or []):
        if f.get("chapter") and f.get("level") in ("high", "warn"):
            eng_by_ch.setdefault(int(f["chapter"]), []).append(f"{f['code']} {f['message']}")

    picks: dict[int, dict] = {}

    def _pick(n, tier, reason):
        if not n:
            return
        row = row_map.get(n) or {"n": n, "title": "", "words": 0, "score": scores.get(n),
                                 "issues": [], "paperwork": 0}
        rec = picks.get(n)
        if rec is None:
            picks[n] = {"n": n, "title": row.get("title") or "",
                        "score": row.get("score"), "words": row.get("words"),
                        "tier": tier, "reasons": [reason]}
        else:
            if tier < rec["tier"]:
                rec["tier"] = tier
            if reason not in rec["reasons"]:
                rec["reasons"].append(reason)

    # ① 字数不足 / 截断
    for r in rows:
        short = [x for x in r["issues"] if x.startswith("字数")]
        cut = [x for x in r["issues"] if x.startswith("C01")]
        if short or cut:
            _pick(r["n"], 1, "；".join(short + cut))
    # ② 评分 < 60
    for n, sc in scores.items():
        if isinstance(sc, (int, float)) and sc < 60:
            _pick(n, 2, f"评分 {sc} < 60")
    # ③ 引擎高风险/告警
    for n, items in eng_by_ch.items():
        _pick(n, 3, "；".join(items[:3]))
    # ④ 文书密度最高的章
    dense = sorted([r for r in rows if (r.get("paperwork") or 0) > 0],
                   key=lambda r: -(r["paperwork"] or 0))[:10]
    for r in dense:
        _pick(r["n"], 4, f"文书密度 {r['paperwork']}/千字，情节被流程挤占")
    # ⑤ 空章卡（只补规划，不重写）
    empty_cards = [r["n"] for r in rows
                   if any(x.startswith("章卡无") for x in r["issues"])]
    for n in empty_cards[:top_n]:
        _pick(n, 5, "章卡是空壳：先补场景链与引擎字段，再决定是否重写")

    ordered = sorted(picks.values(), key=lambda x: (x["tier"], x["n"]))
    surgical = ordered[:top_n]

    title_decision = (canon_data or {}).get("title_decision")
    title_fix = {
        "keep": "书名与正文一致，不改名。",
        "strengthen_title_motifs": "保留书名，把书名关键词写进后续章卡的目标/对手/事件。",
        "rename_or_strengthen": "书名关键词基本没兑现。二选一：按正文真实内容改名，或把关键词写回主线（需要成规模重写，不是补几句口号）。",
    }.get(title_decision, "先跑一次正典体检确定书名与正文是否一致。")

    return {
        "version": 1,
        "chapters": len(chapters),
        "words": sum(r["words"] for r in rows),
        "title": (canon_data or {}).get("title") or canon.book_title(proj),
        "title_decision": title_decision,
        "title_fix": title_fix,
        "title_coverage": (canon_data or {}).get("title_coverage"),
        "opponents": eng.get("opponents") or [],
        "engine_counts": eng.get("counts") or {},
        "empty_cards": len(empty_cards),
        "scores_known": len(scores),
        "surgical": surgical,
        "tiers": {
            "1_short_truncated": len([x for x in ordered if x["tier"] == 1]),
            "2_low_score": len([x for x in ordered if x["tier"] == 2]),
            "3_engine": len([x for x in ordered if x["tier"] == 3]),
            "4_paperwork": len([x for x in ordered if x["tier"] == 4]),
            "5_empty_card": len([x for x in ordered if x["tier"] == 5]),
        },
    }


def render(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    L = [f"# 旧书整改方案 · {data.get('title') or ''}", ""]
    L.append(f"- 正文 {data.get('chapters', 0)} 章 / 约 {data.get('words', 0)} 字")
    L.append(f"- 已有评分 {data.get('scores_known', 0)} 章；"
             f"引擎高 {(data.get('engine_counts') or {}).get('high', 0)} 项、"
             f"告警 {(data.get('engine_counts') or {}).get('warn', 0)} 项")
    L.append(f"- 空壳章卡 {data.get('empty_cards', 0)} 张")
    L.append(f"- 书名承诺覆盖率：{(data.get('title_coverage') or 0):.0%}"
             f" → **{data.get('title_decision') or 'unknown'}**")
    L.append(f"  {data.get('title_fix') or ''}")
    if data.get("opponents"):
        L.append(f"- 在册对手：{'、'.join(data['opponents'][:8])}")
    L.append("")
    L.append("## 先改这 20 章（按作者定的顺序排好）")
    L.append("")
    L.append("| 顺序 | 章 | 层级 | 分数 | 字数 | 原因 |")
    L.append("|---|---|---|---|---|---|")
    for i, x in enumerate(data.get("surgical") or [], 1):
        tier = {1: "①字数/截断", 2: "②低分", 3: "③引擎", 4: "④文书密度", 5: "⑤空章卡"}.get(x["tier"], str(x["tier"]))
        L.append(f"| {i} | 第{x['n']}章 {x.get('title') or ''} | {tier} | {x.get('score') if x.get('score') is not None else '—'} "
                 f"| {x.get('words', 0)} | {'；'.join(x.get('reasons') or [])[:80]} |")
    L.append("")
    L.append("## 结论")
    tiers = data.get("tiers") or {}
    L.append(f"- ①字数/截断 {tiers.get('1_short_truncated', 0)} 章 · ②低分 {tiers.get('2_low_score', 0)} 章 · "
             f"③引擎 {tiers.get('3_engine', 0)} 章 · ④文书密集 {tiers.get('4_paperwork', 0)} 章 · "
             f"⑤空章卡 {tiers.get('5_empty_card', 0)} 章")
    L.append("- 建议：先处理①和②（它们决定书能不能读），再处理③；⑤只补规划，不重写正文。")
    L.append("- 全部重写之前先做这份手术清单——大多数书不需要 300 章一起重写。")
    L.append("")
    L.append("_本方案零 token 生成，所有原因都可回原文核对；执行时每章改稿分必须高于原分，否则回滚。_")
    return "\n".join(L)


def run(proj, quiet: bool = False) -> dict:
    data = plan(proj)
    pdir = _pdir(proj)
    try:
        with open(os.path.join(pdir, REPORT_JSON), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        with open(os.path.join(pdir, REPORT_MD), "w", encoding="utf-8") as f:
            f.write(render(data))
    except OSError as e:
        if not quiet:
            try:
                proj.log(f"⚠️ 旧书整改方案写入失败：{e}")
            except Exception:
                pass
    if not quiet:
        try:
            proj.log(f"🧾 旧书整改方案：建议先改 {len(data.get('surgical') or [])} 章"
                     f"（书名处置：{data.get('title_decision')}），详见 {REPORT_MD}")
        except Exception:
            pass
    return data


def brief(data: dict) -> str:
    if not isinstance(data, dict) or not data:
        return ""
    n = len(data.get("surgical") or [])
    return (f"旧书整改：建议先做 {n} 章外科手术（①字数 {data.get('tiers', {}).get('1_short_truncated', 0)} · "
            f"②低分 {data.get('tiers', {}).get('2_low_score', 0)} · "
            f"③引擎 {data.get('tiers', {}).get('3_engine', 0)}）；书名处置 {data.get('title_decision')}")
