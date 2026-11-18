"""故事引擎体检（v4.40.0）：把「章卡写了什么」与「正文写出了什么」逐条对账。

为什么需要这一层
----------------
工艺体检（craft_check）只证明「文本没坏」，它证明不了「故事在转」。
真实项目里最典型的产出物问题是：章卡写着「冲突：时代压力」，正文就真的写了一章
时代压力；卡上有「情绪：憋屈」，正文没有一个字让人憋屈；书名承诺「神棍」「工业
革命」，300 章里一次都没出现。这些都不是文笔缺陷，是**引擎空转**——每一章都在
推进纸面计划，但没有对抗、没有选择、没有代价。

本模块只做确定性对账：卡上的字段 → 正文里能不能找到对应证据。
零 token、零随机、可复现。结论全部带章号与证据片段，作者能回原文核对。

八项对账
--------
E01 书名/主题承诺：卡上 `engine.title_hit` 声称本章兑现承诺，正文却没有任何书名母题；
E02 对手行动：`engine.opponent_action` 是「局势/时代/环境/命运」这类空话，而不是在册对手的具体动作；
E03 情绪落点：`engine.emotion_beat` 有值，正文却没有可识别的情绪载体；
E04 人性选择：`engine.humanity_choice` 有值，正文却没有任何选择/代价信号；
E05 场景价值翻转：某场有选择却没有结果（选择发生了，但故事没有因此改变）；
E06 选择无代价：某场有选择却没有代价（白拿，不付账）；
E07 文书密度：流程说明压过具体的人做具体的事（复用 craft_check.C24）；
E08 谜题开合：埋了谜题，窗口内既没推进也没兑现——读者会忘。

输出：26-故事引擎体检.json / 26-故事引擎体检.md
"""
from __future__ import annotations

import json
import os

from . import canon, craft_check, longform, state as state_mod

REPORT_JSON = "26-故事引擎体检.json"
REPORT_MD = "26-故事引擎体检.md"
MYSTERY_WINDOW = 12

_ABSTRACT_OPPONENT = (
    "局势", "时代", "环境", "命运", "形势", "舆论", "规矩", "制度", "天意", "世道",
    "朝局", "时局", "压力", "现实", "大势", "潮流", "风气", "旧社会", "大环境",
)

_EMOTION_WORDS = (
    "怒", "恨", "怕", "惧", "慌", "悲", "痛", "哭", "笑", "喜", "愧", "羞", "耻",
    "酸", "苦", "憋", "闷", "寒", "暖", "颤", "抖", "咬牙", "攥", "发抖", "眼泪",
    "心口", "喉咙", "喘", "窒息", "发冷", "发烫", "恶心", "耳鸣", "腿软", "手心",
)

_CHOICE_WORDS = (
    "选择", "选", "要么", "宁可", "宁愿", "只能", "不得不", "舍", "放弃", "换",
    "代价", "权衡", "赌", "押", "豁出去", "认了", "决定",
)

_COST_WORDS = (
    "代价", "失去", "没了", "赔", "断", "伤", "死", "毁", "卖", "抵", "换",
    "欠", "背", "舍", "放弃", "流血", "疼", "痛", "后悔",
)


def _pdir(proj) -> str:
    return os.path.abspath(getattr(proj, "path", "") or ".")


def _issue(code, level, chapter, message, evidence="", metric=None):
    return {
        "code": code,
        "level": level,          # high / warn / info
        "chapter": chapter,
        "message": message,
        "evidence": (evidence or "")[:120],
        "metric": metric or {},
    }


def _clip(s: str, n: int = 80) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _chapter_texts(proj) -> list[dict]:
    try:
        return canon._chapters(proj)
    except Exception:
        return []


def _opponent_names(st: dict) -> list[str]:
    out = []
    for name, rec in (st.get("opponents") or {}).items():
        rec = rec if isinstance(rec, dict) else {}
        for nm in [str(name)] + [str(a) for a in (rec.get("aliases") or [])]:
            if 1 < len(nm) <= 12 and nm not in out:
                out.append(nm)
    return out


def opponent_names(st: dict) -> list[str]:
    """公开别名：给改写提示复用「在册对手」名单。"""
    return _opponent_names(st)


def _nonempty(v) -> bool:
    return bool(str(v or "").strip())


def check_chapter(text: str, n: int, card: dict, motifs: list[str],
                  opponents: list[str]) -> list:
    """单章对账：卡上每个引擎字段 → 正文证据。"""
    out = []
    body = text or ""
    card = card if isinstance(card, dict) else {}
    engine = card.get("engine") if isinstance(card.get("engine"), dict) else {}
    scenes = card.get("scenes") if isinstance(card.get("scenes"), list) else []

    # E01 书名/主题承诺
    hit = str(engine.get("title_hit") or "").strip()
    if hit:
        keys = [hit] + list(motifs or [])
        if not any(k and k in body for k in keys):
            out.append(_issue(
                "E01", "warn", n,
                f"章卡称本章兑现书名/主题承诺「{_clip(hit, 20)}」，正文没有出现任何相关母题",
                evidence=_clip(body[:80]),
                metric={"title_hit": hit, "motifs": motifs[:6]}))

    # E02 对手行动
    act = str(engine.get("opponent_action") or "").strip()
    if act:
        named = [nm for nm in opponents if nm and nm in act]
        abstract = [w for w in _ABSTRACT_OPPONENT if w in act]
        if abstract and not named:
            out.append(_issue(
                "E02", "warn", n,
                f"对手行动写成「{_clip(act, 30)}」——这是环境，不是某个在册对手做的具体动作",
                evidence=_clip(body[:80]),
                metric={"abstract": abstract[:4], "named": named}))

    # E03 情绪落点
    beat = str(engine.get("emotion_beat") or "").strip()
    if beat:
        carriers = [w for w in _EMOTION_WORDS if w in body]
        if beat not in body and not carriers:
            out.append(_issue(
                "E03", "info", n,
                f"章卡要求情绪落点「{_clip(beat, 20)}」，正文没有可识别的情绪载体（动作/生理反应/对白）",
                evidence=_clip(body[-80:])))

    # E04 人性选择
    hc = str(engine.get("humanity_choice") or "").strip()
    if hc:
        marks = [w for w in _CHOICE_WORDS if w in body]
        if not marks:
            out.append(_issue(
                "E04", "info", n,
                f"章卡要求人性选择「{_clip(hc, 24)}」，正文没有出现任何选择/取舍信号",
                evidence=_clip(body[:80])))

    # E05 / E06 场景链：选择必须有结果，且必须付代价
    for i, sc in enumerate(scenes, 1):
        if not isinstance(sc, dict):
            continue
        choice = str(sc.get("choice") or "").strip()
        cost = str(sc.get("cost") or "").strip()
        outcome = str(sc.get("outcome") or "").strip()
        if choice and not outcome:
            out.append(_issue(
                "E05", "warn", n,
                f"第{i}场有选择「{_clip(choice, 20)}」却没有结果——选择发生了，故事没有因此改变",
                evidence=_clip(sc.get("want") or "", 60)))
        if choice and not cost:
            out.append(_issue(
                "E06", "info", n,
                f"第{i}场有选择「{_clip(choice, 20)}」却没有代价——角色白拿，读者不会紧张",
                evidence=_clip(sc.get("obstacle") or "", 60)))
        if cost and not any(w in cost for w in _COST_WORDS) and not any(w in body for w in _COST_WORDS):
            out.append(_issue(
                "E06", "info", n,
                f"第{i}场写了代价「{_clip(cost, 20)}」，但正文没有可识别的损失信号",
                evidence=_clip(cost, 60)))

    # E07 文书密度（craft_check 的字段是 gate/severity，必须转成引擎统一结构）
    try:
        for f in craft_check.check_paperwork_density(body, n=n):
            out.append(_issue(
                "C24", "info", n,
                f.get("message") or "文书密度偏高",
                evidence=f.get("evidence") or "",
                metric=f.get("metric")))
    except Exception:
        pass

    return out


def check_mystery(chapters: list[dict], cards: dict) -> list:
    """E08 谜题开合：setup 之后窗口内必须有 pay/advance，否则读者会忘。"""
    moves = []
    for ch in chapters:
        n = int(ch.get("n") or 0)
        card = cards.get(n)
        if not isinstance(card, dict):
            continue
        engine = card.get("engine") if isinstance(card.get("engine"), dict) else {}
        mv = str(engine.get("mystery_move") or "").strip().lower()
        if mv in ("setup", "pay", "advance"):
            moves.append((n, mv))
    moves.sort()
    out = []
    for idx, (n, mv) in enumerate(moves):
        if mv != "setup":
            continue
        follow = [(m, v) for m, v in moves[idx + 1:] if m <= n + MYSTERY_WINDOW]
        if not any(v in ("pay", "advance") for _m, v in follow):
            out.append(_issue(
                "E08", "warn", n,
                f"第{n}章埋下谜题，之后 {MYSTERY_WINDOW} 章内既没推进也没兑现",
                metric={"window": MYSTERY_WINDOW}))
    return out


def _book_level(chapters: list[dict], motifs: list[str], opponents: list[str],
                cards: dict) -> list:
    out = []
    total = len(chapters)
    if total >= 20 and motifs:
        covered = sum(1 for ch in chapters if any(m and m in (ch.get("text") or "") for m in motifs))
        ratio = covered / total
        if ratio < 0.05:
            out.append(_issue(
                "E00", "high", 0,
                f"书名承诺整体未兑现：「{'、'.join(motifs[:4])}」只在 {covered}/{total} 章出现（{ratio:.0%}）",
                metric={"covered": covered, "chapters": total, "ratio": round(ratio, 3)}))
    if total >= 20 and opponents:
        hit = sum(1 for ch in chapters if any(nm in (ch.get("text") or "") for nm in opponents))
        if hit == 0:
            out.append(_issue(
                "E00", "high", 0,
                f"在册对手 {len(opponents)} 人（{'、'.join(opponents[:4])}）从未在正文出现——全书没有具体对抗者",
                metric={"opponents": opponents[:8]}))
    empty_cards = 0
    for ch in chapters:
        n = int(ch.get("n") or 0)
        card = cards.get(n)
        if not isinstance(card, dict):
            empty_cards += 1
            continue
        if not card.get("scenes") and not (card.get("engine") or {}):
            empty_cards += 1
    if total >= 20 and empty_cards / total > 0.5:
        out.append(_issue(
            "E00", "warn", 0,
            f"{empty_cards}/{total} 章没有场景链/引擎字段（旧章卡只写了目标与冲突），"
            "故事引擎无法对账——建议先跑一次章卡回填",
            metric={"empty": empty_cards, "chapters": total}))
    return out


def scan(proj, chapters: list[dict] | None = None, cards: dict | None = None) -> dict:
    pdir = _pdir(proj)
    chapters = chapters if isinstance(chapters, list) else _chapter_texts(proj)
    if cards is None:
        try:
            cards = longform.load_cards(proj)
        except Exception:
            cards = {}
    try:
        st = state_mod.load(pdir)
    except Exception:
        st = state_mod.empty_state()

    title = canon.book_title(proj)
    motifs = canon.title_motifs(title)
    opponents = _opponent_names(st)

    findings = []
    for ch in chapters:
        n = int(ch.get("n") or 0)
        if not n:
            continue
        findings.extend(check_chapter(ch.get("text") or "", n, cards.get(n) or {}, motifs, opponents))
    findings.extend(check_mystery(chapters, cards))
    findings.extend(_book_level(chapters, motifs, opponents, cards))

    high = [f for f in findings if f.get("level") == "high"]
    warn = [f for f in findings if f.get("level") == "warn"]
    info = [f for f in findings if f.get("level") == "info"]
    by_code = {}
    for f in findings:
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1

    return {
        "version": 1,
        "title": title,
        "motifs": motifs,
        "opponents": opponents,
        "chapters": len(chapters),
        "findings": findings,
        "counts": {"high": len(high), "warn": len(warn), "info": len(info)},
        "by_code": by_code,
        "high_chapters": sorted({f["chapter"] for f in high if f["chapter"]}),
        "warn_chapters": sorted({f["chapter"] for f in warn if f["chapter"]}),
    }


def render_report(data: dict, cap: int = 40) -> str:
    if not isinstance(data, dict):
        return ""
    c = data.get("counts") or {}
    L = [f"# 故事引擎体检 · {data.get('title') or ''}", ""]
    L.append(f"- 在册章节 {data.get('chapters', 0)} · 书名母题："
             + ("、".join(data.get("motifs") or []) or "（未切出）"))
    L.append(f"- 在册对手：{'、'.join(data.get('opponents') or []) or '（无）'}")
    L.append(f"- 高（必须处理）**{c.get('high', 0)}** 项 · "
             f"告警 {c.get('warn', 0)} 项 · 提示 {c.get('info', 0)} 项")
    if data.get("high_chapters"):
        L.append(f"- 高优先涉及章节：第 {', '.join(str(x) for x in data['high_chapters'][:40])} 章")
    if data.get("by_code"):
        L.append("- 分布：" + "、".join(f"{k}×{v}" for k, v in sorted(data["by_code"].items())))
    L.append("")
    L.append("## 高优先（引擎空转，不处理会一直写下去）")
    highs = [f for f in (data.get("findings") or []) if f.get("level") == "high"]
    if not highs:
        L.append("无。")
    else:
        for f in highs[:cap]:
            where = f"第{f['chapter']}章" if f["chapter"] else "全书"
            L.append(f"- **{where} · {f['code']}** {f['message']}")
            if f.get("evidence"):
                L.append(f"  > {f['evidence']}")
    L.append("")
    L.append("## 告警（按章）")
    warns = [f for f in (data.get("findings") or []) if f.get("level") == "warn"]
    if not warns:
        L.append("无。")
    else:
        for f in warns[:cap]:
            where = f"第{f['chapter']}章" if f["chapter"] else "全书"
            L.append(f"- **{where} · {f['code']}** {f['message']}")
    L.append("")
    L.append("## 提示（工艺层，只记录不拦截）")
    infos = [f for f in (data.get("findings") or []) if f.get("level") == "info"]
    L.append(f"{len(infos)} 项（明细见 JSON）。" if infos else "无。")
    return "\n".join(L)


def run(proj, quiet: bool = False) -> dict:
    data = scan(proj)
    pdir = _pdir(proj)
    try:
        with open(os.path.join(pdir, REPORT_JSON), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        with open(os.path.join(pdir, REPORT_MD), "w", encoding="utf-8") as f:
            f.write(render_report(data))
    except OSError as e:
        if not quiet:
            try:
                proj.log(f"⚠️ 故事引擎体检报告写入失败：{e}")
            except Exception:
                pass
    if not quiet:
        try:
            c = data.get("counts") or {}
            proj.log(f"🔧 故事引擎体检：高 {c.get('high', 0)} / 告警 {c.get('warn', 0)} / "
                     f"提示 {c.get('info', 0)}（已写 {REPORT_MD}）")
        except Exception:
            pass
    return data


def brief(data: dict) -> str:
    if not isinstance(data, dict) or not data:
        return ""
    c = data.get("counts") or {}
    if not (c.get("high") or c.get("warn")):
        return "故事引擎对账通过"
    return (f"故事引擎：高 {c.get('high', 0)} 项、告警 {c.get('warn', 0)} 项"
            + (f"，优先第{data['high_chapters'][0]}章" if data.get("high_chapters") else ""))