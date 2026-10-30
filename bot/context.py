"""长篇上下文装配：把"整文件注入 + 随意截断"换成有预算、有优先级的连续简报。

为什么需要它：写第 900 章时，00/01/03/09/10/11/12 加起来可能几十万字，
整段塞给模型既贵又稀释注意力。这里按 L0–L4 分层，先保证硬约束（本章章卡、
权威状态、上一章结尾、卷弧目标），再按预算给最近摘要/伏笔/文风/参考材料，
最后才给可丢的长程信息。每个块都在段落边界截断并标"已压缩"。

约束：
  * build() 是确定性的，**不调用 LLM**；
  * 缺失新内核数据时自动回退旧 Markdown，旧项目也能跑；
  * 返回的 dict 必须兼容 writer._build_ctx 的键（workflow.write_chapter 只读这些键）。
"""
from __future__ import annotations

import math
import os
import re

from . import longform, state as state_mod

DEFAULT_BUDGET = 24000
_MIN_BLOCK = 120
_LAYER_NAMES = {0: "L0", 1: "L1", 2: "L2", 3: "L3", 4: "L4"}


# ---------- 文件读取 ----------

def _pdir(proj) -> str:
    return os.path.abspath(getattr(proj, "path", "") or ".")


def _read(pdir: str, name: str, cap: int = 200000) -> str:
    try:
        return open(os.path.join(pdir, name), encoding="utf-8").read()[:cap]
    except OSError:
        return ""


def _flat_plan(proj) -> dict:
    try:
        plan = proj.plan() if hasattr(proj, "plan") else None
    except Exception:
        plan = None
    return plan if isinstance(plan, dict) else {}


def _resolve_cfg(proj, cfg) -> dict:
    if isinstance(cfg, dict) and cfg:
        return cfg
    plan = _flat_plan(proj)
    c = plan.get("config")
    return c if isinstance(c, dict) else {}


def _read_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------- 单项素材 ----------

def _style_text(pdir: str) -> str:
    """沿用旧 _build_ctx 的文风拼装：03 基准 + style.json 技能 + 蒸馏风格。"""
    text = _read(pdir, "03-文风基准.md")
    try:
        import json as _json
        sp = os.path.join(pdir, "style.json")
        if os.path.exists(sp):
            sk = _json.load(open(sp, encoding="utf-8"))
            d = sk.get("style") or {}
            parts = [f"{k}：{v}" for k, v in [
                ("题材", sk.get("genre", "")), ("基调", sk.get("tone", "")),
                ("视角", sk.get("perspective", "")), ("金手指", d.get("金手指", "")),
                ("风格基调", d.get("基调", "")), ("节奏", d.get("节奏", "")),
                ("语言", d.get("语言", "")), ("卷", d.get("卷", "")),
                ("发布平台", sk.get("platform", "")),
                ("卖点", "、".join(str(s) for s in (sk.get("sells") or [])))] if v]
            if parts:
                text += "\n\n## 创作风格技能（本作品按此技能设定书写，严格遵守）\n" + "\n".join(parts)
            if sk.get("platform_note"):
                text += "\n\n## 平台节奏要求（" + str(sk["platform"]) + "）\n" + str(sk["platform_note"])
    except Exception:
        pass
    try:
        import json as _json2
        sr = os.path.join(pdir, "style_ref.json")
        if os.path.exists(sr):
            sc = _json2.load(open(sr, encoding="utf-8"))
            if sc.get("style_prompt"):
                text += ("\n\n## 专属蒸馏风格（" + str(sc.get("name", ""))
                         + "，按此技法写，不改变剧情）\n" + sc["style_prompt"])
    except Exception:
        pass
    return text


def _style_anchor(pdir: str, style_text: str) -> str:
    """文风锚点：优先"本书校准段落"（第 1 章定稿回填），其次基准段落。"""
    for heading in ("本书校准段落", "基准段落"):
        m = re.search(rf"##\s*[^\n]*{heading}[^\n]*\n(.*?)(?=\n##\s|\Z)", style_text, re.S)
        if m:
            body = m.group(1).strip()
            if body:
                return state_mod.clip_text(f"【{heading}】\n{body}", 1400)
    return state_mod.clip_text(style_text, 800)


def _prev_tail(proj, no: int, pdir: str) -> str:
    """上一章结尾 1200–1800 字；从段落边界开始，避免半句话。"""
    if no <= 1:
        return ""
    text = ""
    plan = _flat_plan(proj)
    chapters = plan.get("chapters") if isinstance(plan.get("chapters"), list) else []
    for ch in chapters:
        if _read_int(ch.get("chapterNumber")) == no - 1:
            fp = ch.get("filePath")
            if isinstance(fp, str) and fp:
                text = _read(pdir, fp, 200000)
            break
    if not text:
        try:
            prefix = f"第{no - 1:02d}章"
            for name in sorted(os.listdir(pdir)):
                if name.startswith(prefix) and name.endswith(".md"):
                    text = _read(pdir, name, 200000)
                    break
        except OSError:
            text = ""
    if not text:
        return ""
    tail = text[-1800:]
    # 从第一个段落边界开始（前 300 字内找换行），否则保留原样
    cut = tail.find("\n", 0, 300)
    if cut > 0:
        tail = tail[cut + 1:]
    return tail.strip()


def _outline_row(outline: str, no: int) -> str:
    """从大纲抠第 no 章附近的规划段，避免整本大纲塞进上下文。"""
    m = re.search(rf"第{no}章[^\n]*\n(.*?)(?=\n第{no + 1}章|\n##|\Z)", outline, re.S)
    return m.group(0).strip() if m else ""


def _outline_brief(outline: str, no: int) -> str:
    if not outline:
        return ""
    row = _outline_row(outline, no)
    head = outline[:1200].strip()
    if row and row not in head:
        return head + "\n……\n" + row
    return row or head


def _long_summary(pdir: str) -> str:
    vol = _read(pdir, "20-卷摘要.md", 12000).strip()
    legacy = _read(pdir, "11-章节摘要.md", 40000).strip()
    parts = []
    if vol:
        parts.append(vol)
    if legacy:
        parts.append("【旧章节摘要（末尾）】\n" + legacy[-2000:])
    return "\n\n".join(parts)


def _graph_block(proj, no: int, query: str, cfg: dict) -> tuple[dict, list, str]:
    """故事图谱 + 语义检索；可配 p5_pipeline 关，失败一律降级为空。"""
    pdir = _pdir(proj)
    graph_ctx, retrieved, text = {}, [], ""
    if cfg.get("p5_pipeline", True):
        try:
            from . import graph as _graph
            graph_ctx = _graph.active_context(pdir, no)
        except Exception:
            graph_ctx = {}
        try:
            from . import retrieval as _retr
            retrieved = _retr.retrieve(query, pdir, k=3, exclude_ch=(no - 1))
        except Exception:
            retrieved = []
    g = graph_ctx or {}
    parts = []
    if g.get("persons") or g.get("active_threads"):
        s = "【故事图谱·本章相关实体】\n- 相关人物：" + ("、".join(g.get("persons", [])[:8]) or "（未登记）")
        if g.get("active_threads"):
            s += "\n- 活跃/临近兑现伏笔：" + "、".join(g.get("active_threads", [])[:6])
        parts.append(s)
    if retrieved:
        s = "【相关既往片段（防遗忘设定，勿复述）】"
        for x in retrieved:
            s += f"\n- [第{x.get('ch') or '?'}章·{x.get('src')}] {(x.get('snippet') or '')[:150]}"
        parts.append(s)
    if parts:
        text = "\n".join(parts)
    return graph_ctx, retrieved, text


def _characters_block(pdir: str, card: dict) -> str:
    """相关人物档案在前，完整人物档案在后（由预算截断）。"""
    legacy = _read(pdir, "00-人物档案.md")
    focus = (card.get("state_focus") or {}).get("characters") or []
    parts = []
    if focus:
        lines = ["【本章相关人物档案（优先遵守）】"]
        for c in focus[:6]:
            bits = []
            for key, label in (("status", "状态"), ("location", "位置"),
                               ("goal", "目标"), ("knowledge", "已知")):
                val = c.get(key)
                if isinstance(val, list):
                    val = "；".join(str(x) for x in val[-3:])
                if val:
                    bits.append(f"{label}：{val}")
            if c.get("relations"):
                rel = "、".join(f"{k}→{v}" for k, v in list(c["relations"].items())[:4])
                bits.append("关系：" + rel)
            lines.append(f"- {c.get('name')}：" + ("；".join(bits) or "（见人物档案）"))
        parts.append("\n".join(lines))
    if legacy:
        parts.append("【完整人物档案（节选）】\n" + legacy)
    return "\n\n".join(parts)


def _recent_summaries_text(proj, no: int) -> str:
    rows = longform._load_chapter_summaries(proj)
    lines = []
    for n in sorted(rows, reverse=True):
        if n >= no:
            continue
        sm = rows[n].get("summary") if isinstance(rows[n], dict) else ""
        if sm:
            lines.append(f"- 第{n}章：{sm}")
        if len(lines) >= 5:
            break
    return "\n".join(reversed(lines))


def _foreshadow_text(card: dict) -> str:
    due = card.get("foreshadow_due") or {}
    lines = []
    for label, key in (("已逾期（必须处理）", "overdue"), ("本章窗口到期", "due"),
                       ("临近兑现", "upcoming")):
        items = due.get(key) or []
        for f in items[:8]:
            lines.append(f"- [{label}] [{f.get('id')}] {f.get('description') or ''}"
                         f"（第{f.get('due_from') or '?'}–{f.get('due_to') or '?'}章）")
    return "\n".join(lines) or "（暂无到期伏笔）"


def _warnings(proj, card: dict) -> list[str]:
    st = state_mod.load(_pdir(proj))
    warnings = state_mod.validate(st)
    done = max(0, _read_int((st.get("metrics") or {}).get("last_chapter"), 0))
    due = state_mod.foreshadow_due(st, max(1, done or card.get("n") or 1))
    for f in due["overdue"][:5]:
        warnings.append(f"伏笔「{f.get('id')}」已逾期（计划第{f.get('due_to')}章兑现）")
    known = st.get("foreshadowing") or {}
    for fid in card.get("foreshadow_payoff") or []:
        if fid and fid not in known:
            warnings.append(f"章卡要求本章兑现伏笔「{fid}」，但状态库未登记")
    return warnings[:10]


def _references_block(references, cfg: dict, plan: dict) -> str:
    refs = references or cfg.get("references") or plan.get("references")
    if not refs:
        return ""
    lines = ["【参考资料（只作事实/细节参考；禁止逐句复制；引用时标注 [R编号]）】"]
    for i, r in enumerate(refs if isinstance(refs, list) else [refs], 1):
        if isinstance(r, dict):
            rid = str(r.get("id") or f"R{i}")
            title = str(r.get("title") or r.get("name") or "").strip()
            body = str(r.get("excerpt") or r.get("summary") or r.get("text") or "").strip()
        else:
            rid, title, body = f"R{i}", "", str(r).strip()
        if not (title or body):
            continue
        lines.append(f"[{rid}] {title}：{body[:700]}")
        if len(lines) > 12:
            break
    return "\n".join(lines) if len(lines) > 1 else ""


# ---------- 预算装配 ----------

def _assemble(blocks: list[dict], budget: int) -> tuple[dict[str, str], dict]:
    """按 L0 硬保留、L1→L4 优先级的顺序分配预算，低优先级先丢。"""
    kept: dict[str, str] = {}
    compressed: list[str] = []
    dropped: list[dict] = []
    used = 0

    l0 = [b for b in blocks if b["layer"] == 0]
    l0_caps = sum(b["cap"] for b in l0)
    scale = 1.0
    if l0_caps > budget:
        # L0 永不丢，但可以压缩：按比例缩小每个硬约束块
        scale = budget / l0_caps
    for b in l0:
        cap = max(160, int(b["cap"] * scale))
        text = b["text"]
        if len(text) > cap:
            text = state_mod.clip_text(text, cap)
            compressed.append(b["name"])
        kept[b["name"]] = text
        used += len(text)

    remaining = max(0, budget - used)
    for layer in (1, 2, 3, 4):
        for b in sorted((x for x in blocks if x["layer"] == layer), key=lambda x: x["priority"]):
            cap = min(b["cap"], remaining)
            if cap < _MIN_BLOCK or not b["text"]:
                dropped.append({"name": b["name"], "layer": _LAYER_NAMES[layer],
                                "chars": len(b["text"]), "reason": "预算不足"})
                continue
            text = b["text"]
            if len(text) > cap:
                text = state_mod.clip_text(text, cap)
                compressed.append(b["name"])
            kept[b["name"]] = text
            used += len(text)
            remaining = max(0, remaining - len(text))

    layers: dict[str, int] = {name: 0 for name in _LAYER_NAMES.values()}
    for b in blocks:
        if b["name"] in kept:
            layers[_LAYER_NAMES[b["layer"]]] += len(kept[b["name"]])
    meta = {
        "budget": budget,
        "used": used,
        "layers": layers,
        "dropped": dropped,
        "compressed": compressed,
        "estimates": {"tokens": estimate_tokens("".join(kept.values()))},
    }
    return kept, meta


# ---------- 公共接口 ----------

def estimate_tokens(text: str) -> int:
    """中文按 ~1.5 字/token 的保守估算；不依赖 tiktoken。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = max(0, len(text) - cjk)
    return int(math.ceil(cjk / 1.5 + other / 4.0))


def build(proj, ch, cfg=None, references=None) -> dict:
    """确定性装配本章上下文；返回兼容 writer._build_ctx 的 dict（外加新键）。"""
    pdir = _pdir(proj)
    plan = _flat_plan(proj)
    cfg = _resolve_cfg(proj, cfg)
    no = max(1, _read_int((ch or {}).get("chapterNumber"), 1))
    title = (ch or {}).get("title") or f"第{no}章"
    budget = max(4000, min(100000, _read_int(cfg.get("context_budget"), DEFAULT_BUDGET)))

    card = longform.build_chapter_card(proj, no, cfg)
    digest = longform.plan_digest(proj)
    card_text = longform.render_card(card)
    prev_tail = _prev_tail(proj, no, pdir)
    query = " ".join(x for x in (title, card.get("goal"), card.get("conflict"),
                                 prev_tail[-500:]) if x)
    state_excerpt = state_mod.excerpt(state_mod.load(pdir), query,
                                      max_chars=5000, focus_chars=3000)
    outline = _read(pdir, "01-大纲.md")
    style_text = _style_text(pdir)
    structure = []
    if card.get("volume_goal"):
        structure.append(f"- 第{card.get('volume')}卷「{card.get('volume_title')}」目标：{card['volume_goal']}")
    if card.get("arc_goal"):
        structure.append(f"- 第{card.get('arc')}弧「{card.get('arc_title')}」目标：{card['arc_goal']}")
    if digest.get("issues"):
        structure.append("- 规划提示：" + "；".join(str(x) for x in digest["issues"][:4]))
    blueprint = ""
    bp_md = _read(pdir, "02b-总架构.md", 20000)
    if bp_md:
        blueprint += "## 总架构（节选）\n" + bp_md[:2400]
    sk_md = _read(pdir, "17-写作骨架.md", 8000)
    if sk_md:
        blueprint += "\n\n## 写作骨架（总体状态，动手前先读）\n" + sk_md[:1800]
    graph_ctx, retrieved, graph_text = _graph_block(proj, no, query, cfg)
    refs_text = _references_block(references, cfg, plan)
    warnings = _warnings(proj, card)
    style_anchor = _style_anchor(pdir, style_text)
    lore_relevant = ""
    if cfg.get("lorebook", True):
        try:
            from . import lorebook
            lore_relevant = lorebook.relevant(pdir, f"{title} {card.get('goal') or ''[:300]} {prev_tail[-400:]}")
        except Exception:
            lore_relevant = ""

    blocks = [
        {"name": "card", "text": card_text, "layer": 0, "priority": 0, "cap": 1600},
        {"name": "state_excerpt", "text": state_excerpt, "layer": 0, "priority": 1, "cap": 5000},
        {"name": "prev_tail", "text": prev_tail, "layer": 0, "priority": 2, "cap": 1800},
        {"name": "structure", "text": "\n".join(structure), "layer": 0, "priority": 3, "cap": 800},
        {"name": "recent", "text": _recent_summaries_text(proj, no), "layer": 1, "priority": 10, "cap": 2200},
        {"name": "foreshadow", "text": _foreshadow_text(card), "layer": 1, "priority": 11, "cap": 1200},
        {"name": "warnings", "text": "\n".join(f"- {w}" for w in warnings), "layer": 1, "priority": 12, "cap": 900},
        {"name": "clue_ledger", "text": _read(pdir, "10-线索账本.md", 6000), "layer": 1, "priority": 13, "cap": 1200},
        {"name": "characters", "text": _characters_block(pdir, card), "layer": 2, "priority": 20, "cap": 3000},
        {"name": "lore", "text": lore_relevant, "layer": 2, "priority": 20, "cap": 1200},
        {"name": "style", "text": style_text, "layer": 2, "priority": 21, "cap": 2600},
        {"name": "blueprint", "text": blueprint, "layer": 2, "priority": 22, "cap": 2400},
        {"name": "outline", "text": _outline_brief(outline, no), "layer": 2, "priority": 23, "cap": 4000},
        {"name": "references", "text": refs_text, "layer": 3, "priority": 30, "cap": 4000},
        {"name": "long_summary", "text": _long_summary(pdir), "layer": 4, "priority": 40, "cap": 1400},
        {"name": "story_bible", "text": _read(pdir, "09-故事圣经.md", 12000), "layer": 4, "priority": 41, "cap": 1400},
        {"name": "timeline", "text": _read(pdir, "12-时间线与连续性.md", 12000), "layer": 4, "priority": 42, "cap": 900},
        {"name": "covenant", "text": _read(pdir, "13-一致性纲领.md", 12000), "layer": 1, "priority": 15, "cap": 1500},
        {"name": "graph", "text": graph_text, "layer": 4, "priority": 43, "cap": 900},
    ]
    blocks = [b for b in blocks if isinstance(b.get("text"), str) and b["text"].strip()]
    parts, meta = _assemble(blocks, budget)

    # chapter_plan：优先新章卡，旧项目回退大纲行
    chapter_plan = parts.get("card") or _outline_row(outline, no) or outline[-3000:]
    outline_text = parts.get("outline") or _outline_brief(outline, no)
    characters_text = parts.get("characters") or _read(pdir, "00-人物档案.md", 8000)
    style_out = parts.get("style") or style_text[:6000]

    ctx = {
        # ---- 兼容 writer._build_ctx / workflow.write_chapter 的键 ----
        "chapter_no": no,
        "chapter_title": title,
        "outline_text": outline_text,
        "chapter_plan": chapter_plan,
        "characters_text": characters_text,
        "style_text": style_out,
        "prev_tail": parts.get("prev_tail", ""),
        "cfg": cfg,
        "continuation": bool((ch or {}).get("continuation")),
        "story_bible": parts.get("story_bible", ""),
        "clue_ledger": parts.get("clue_ledger", ""),
        "chapter_summaries": parts.get("recent", ""),
        "timeline": parts.get("timeline", ""),
        "covenant": parts.get("covenant", ""),
        "lore_relevant": parts.get("lore") or lore_relevant,
        "graph_ctx": graph_ctx,
        "retrieval_ctx": retrieved,
        "blueprint": parts.get("blueprint", ""),
        "queue_note": "",
        # ---- 新内核键 ----
        "state_excerpt": parts.get("state_excerpt", ""),
        "longform_card": card,
        "plan_digest": digest,
        "references_block": parts.get("references", ""),
        "continuity_warnings": warnings,
        "style_anchor": style_anchor,
        "context_meta": meta,
    }
    return ctx


def build_legacy(proj, ch, cfg=None) -> dict:
    """完全沿用旧 _build_ctx 的兜底；导入失败时复制其最小语义。"""
    try:
        from . import writer as _writer
        ctx = _writer._build_ctx(proj, ch)
        if isinstance(ctx, dict):
            if cfg:
                ctx.setdefault("cfg", cfg)
            return ctx
    except Exception:
        pass
    # 兜底：旧项目文件直读（不依赖新内核，也不调用 LLM）
    pdir = _pdir(proj)
    plan = _flat_plan(proj)
    no = max(1, _read_int((ch or {}).get("chapterNumber"), 1))
    outline = _read(pdir, "01-大纲.md")
    prev_tail = _prev_tail(proj, no, pdir)
    chapter_plan = _outline_row(outline, no) or outline[-3000:]
    return {
        "chapter_no": no,
        "chapter_title": (ch or {}).get("title") or f"第{no}章",
        "outline_text": outline,
        "chapter_plan": chapter_plan,
        "characters_text": _read(pdir, "00-人物档案.md", 8000),
        "style_text": _style_text(pdir)[:6000],
        "prev_tail": prev_tail,
        "cfg": _resolve_cfg(proj, cfg),
        "continuation": bool((ch or {}).get("continuation")),
        "story_bible": _read(pdir, "09-故事圣经.md"),
        "clue_ledger": _read(pdir, "10-线索账本.md"),
        "chapter_summaries": _read(pdir, "11-章节摘要.md"),
        "timeline": _read(pdir, "12-时间线与连续性.md"),
        "lore_relevant": "",
        "graph_ctx": {},
        "retrieval_ctx": [],
        "blueprint": "",
        "queue_note": "",
        "_plan": plan,  # 仅供排障，workflow 忽略未知键
    }


def render_context_block(ctx: dict) -> str:
    """把新键拼成"长篇连续性简报"，供主 Agent 注入 blueprint 或未来 workflow 使用。"""
    if not isinstance(ctx, dict):
        return ""
    parts = ["【长篇连续性简报（写前必读）】"]
    digest = ctx.get("plan_digest") or {}
    if digest:
        vol = digest.get("current_volume") or {}
        arc = digest.get("current_arc") or {}
        try:
            progress = f"{float(digest.get('word_progress') or 0):.1%}"
        except (TypeError, ValueError):
            progress = "0%"
        parts.append(
            f"- 进度：{digest.get('chapters_written', 0)}/{digest.get('chapters_planned', 0)} 章；"
            f"第{vol.get('no', '?')}卷「{vol.get('title', '')}」第{arc.get('no', '?')}弧；"
            f"字数进度 {progress}；"
            f"伏笔 开放{digest.get('foreshadow', {}).get('open', 0)}/"
            f"逾期{digest.get('foreshadow', {}).get('overdue', 0)}"
        )
    card = ctx.get("longform_card")
    if isinstance(card, dict):
        parts.append(longform.render_card(card))
    if ctx.get("state_excerpt"):
        parts.append("【权威状态（本章相关优先）】\n" + str(ctx["state_excerpt"]))
    warnings = ctx.get("continuity_warnings") or []
    if warnings:
        parts.append("【连续性警告】\n" + "\n".join(f"- {w}" for w in warnings[:10]))
    if ctx.get("chapter_summaries"):
        parts.append("【最近 5 章摘要】\n" + str(ctx["chapter_summaries"]))
    if ctx.get("style_anchor"):
        parts.append(str(ctx["style_anchor"]))
    if ctx.get("references_block"):
        parts.append(str(ctx["references_block"]))
    parts.append("【纪律】只写本章卡范围内的事件；引用状态以简报为准；参考资料不得逐句复制。")
    return "\n\n".join(parts)
