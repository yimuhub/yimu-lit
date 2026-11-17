"""总编辑中枢（v4.37.0）——对话里的「一个人」，不是关键词路由。

作者只跟总编辑说话：聊天、评价、改设定、下指令、问进度、发脾气，都可以。
总编辑先真正理解他想要什么，再决定：
  · 只是回答/讨论（discuss）
  · 把意见翻译成工作单，交给写作/改写/审稿/规划（directive / rewrite_* / plan_change）
  · 调整流程（继续写、暂停、评分、巡检、改书名…）

与旧 planner 的区别：
  · planner 只认 18 个固定动作，遇到动作表外的表达只能落到「并入下一章」；
  · 总编辑带完整作品上下文、最近对话、在途任务、生效中的工作单，理解指代与否定；
  · 输出里带「对作者说的话」和「给下游的工作单」，不再把作者原话原样丢给下游。

不可用（无模型/解析失败）返回 None，调用方回退旧链路，不改变离线行为。
"""
from __future__ import annotations

import json
import os
import re

from . import brief as _brief
from . import planner as _planner

# ── 总编辑可派的动作（与 app.py `_run` / 执行器一一对应） ───────────────────

ACTIONS = {
    "discuss": {
        "desc": "只回答、讨论、给专业判断，不执行任何创作动作（问看法、闲聊、征求建议、否定上一版）",
        "params": {},
    },
    "report_status": {
        "desc": "汇报进度、字数、评分、当前在做什么",
        "params": {},
    },
    "show_outline": {
        "desc": "展示目录/大纲/章节列表",
        "params": {},
    },
    "show_briefs": {
        "desc": "列出当前生效的总编辑工作单（作者要求过的长期/指定章指令）",
        "params": {},
    },
    "write": {
        "desc": "推进写作：按当前节奏继续、写指定数量、或一口气写完",
        "params": {"count": "整数或 all；不填=按当前节奏继续",
                   "mode": "continue|resume_task|start，默认 continue"},
    },
    "pause": {"desc": "暂停创作", "params": {}},
    "rewrite_chapters": {
        "desc": "重写指定章节；改稿要求必须放进 brief，不要只把原话塞进 dimensions",
        "params": {"chapters": "[章号整数]"},
    },
    "rewrite_below": {
        "desc": "重写不达标/低分章节；作者给了分数阈值就填 below，否则按系统达标线",
        "params": {"below": "整数或 null", "target": "目标分或 null"},
    },
    "score_chapters": {
        "desc": "给章节评分；不带 chapters=补评所有未评分章",
        "params": {"chapters": "[章号整数] 或 null"},
    },
    "audit": {"desc": "一致性巡检：找全书矛盾、盘点超期未收的伏笔", "params": {}},
    "set_title": {"desc": "改书名/标题", "params": {"title": "新书名"}},
    "directive": {
        "desc": "把作者的口味/要求记成工作单，之后写作、改写、审稿都按它执行；"
                "不立即重写。全书长期要求用 scope=book，下一章一次用 scope=next，"
                "指定章用 scope=chapters",
        "params": {"brief": "见下方工作单结构"},
    },
    "revoke_briefs": {
        "desc": "撤销工作单（作者说「刚才那条不算」「别按那个改了」「撤掉」时用）",
        "params": {"index": "从1开始，1=最新的一条；不填=最新",
                   "chapter": "撤销指定章的章级工作单（可空）"},
    },
    "plan_change": {
        "desc": "调整大纲/卷弧/章卡结构（作者说某卷拖、某段要重排、结构不对时）",
        "params": {"scope": "book|volume|arc|window|chapter", "instruction": "作者的结构要求原文",
                   "chapters": "[章号]（可空）"},
    },
    "apply_plan_proposal": {
        "desc": "采纳最近一张待确认的规划提案（作者说「就按这个改」「应用提案」时）",
        "params": {"allow_past": "是否允许改到已写章节，默认 false"},
    },
    "export": {"desc": "导出成书（给操作指引）", "params": {}},
    "new_project": {"desc": "作者想另开一本新书（给操作指引，不在当前书里硬开）", "params": {}},
    "cancel_task": {
        "desc": "停止当前正在跑的改写/评分任务",
        "params": {"kind": "rewrite|score|all，默认 all"},
    },
}

_ACTION_ALIASES = {
    "answer": "discuss", "chat": "discuss", "discussion": "discuss",
    "status": "report_status", "progress": "report_status",
    "outline": "show_outline", "catalog": "show_outline",
    "briefs": "show_briefs", "list_briefs": "show_briefs",
    "write_next": "write", "resume_writing": "write", "start_writing": "write",
    "start_writing_all": "write", "batch_writing": "write", "resume_task": "write",
    "pause_writing": "pause", "stop": "pause",
    "rewrite": "rewrite_chapters", "rewrite_chapter": "rewrite_chapters",
    "rewrite_low_score": "rewrite_below", "rewrite_all_below": "rewrite_below",
    "score": "score_chapters", "score_all_unscored": "score_chapters",
    "consistency_audit": "audit",
    "title": "set_title", "rename": "set_title",
    "style": "directive", "constraint": "directive", "set_style": "directive",
    "add_constraint": "directive", "creative_hint": "directive",
    "revoke": "revoke_briefs", "cancel_brief": "revoke_briefs", "drop_brief": "revoke_briefs",
    "replan": "plan_change", "apply_replan": "apply_plan_proposal",
    "export_book": "export",
}

_SYSTEM_PROMPT = """你是「一幕神笔」的**总编辑**，也是作者在这部作品里唯一对话的人。
作者不会背命令、不会使用固定句式；他说的是人话，一句话里可能同时有聊天、评价、设定、改稿要求和进度询问。
你的职责不是匹配关键词，而是真正读懂他想要什么，然后把理解翻译成创作团队各岗位能直接执行的工作单。

## 你的团队
- 规划师：大纲、卷弧、章卡、人物档案、世界观
- 作家：写正文
- 审稿人：逐章评分、挑问题、核对连续性
- 改写师：按工作单定向重写
- 值班监制：后台看护停滞、质量闸门、伏笔账
你只对作者说话；派活通过 actions 完成。**不要**让作者去学命令，也**不要**把作者原话原样丢给下游。

## 可执行动作（action 只能取这些名字）
{actions}

## 工作单结构（directive / rewrite_chapters / rewrite_below 用）
{{
  "goal": "要达到什么效果（一句话，具体到可执行）",
  "must_keep": ["不能动的：人物性格、已发生事实、伏笔、视角、核心走向…"],
  "must_change": ["必须改掉的：具体场景/写法/毛病…"],
  "acceptance": ["怎样算改好了（可检查的标准）"],
  "avoid": ["明确禁止的做法"],
  "scope": "book | next | chapters",
  "chapters": [7, 8]
}}
scope 语义：
- book：作者说的是长期口味/全书要求（「以后都…」「整体…」「这本书…」），之后每一章都生效；
- next：只影响下一章（「下一章别写打斗」）；
- chapters：只影响指定章（必须填 chapters）。

## 判断原则（按优先级）
1. **先理解，再决定说还是做。** 作者可能在问看法、发泄情绪、否定你上一版方案，也可能在下指令。
   只是讨论/征求建议 → discuss，用你的专业判断直接回答，不要为了显得能干而乱派活。
2. **任何会影响作品的表达都要翻译成工作单。** 作者说「这段感情戏太假」「女主不像她」
   「别老写雨」「节奏拖了」——不要原样转给作家，要提炼成 goal / must_change / acceptance。
3. **区分长期口味与一次性指令。** 「以后」「整体」「这本书」→ book；「下一章」→ next；
   「第7章」→ chapters=[7]。
4. **一句话多件事可拆多步，但最多 4 步，按合理顺序。** 例：
   「把不达标的都重写，没评分的先打分，然后继续写 5 章」
   → [score_chapters, rewrite_below, write(count=5)]
5. **否定要接住。** 作者说「不行/换个思路/不是这个意思」时，先判断他否定的是哪一部分，
   调整工作单或方案，不要重复原方案。
6. **作者给了具体分数阈值**（「低于60分」「60分以下」）→ rewrite_below(below=该阈值)；
   没给分数（「未达标/不达标/质量不行」）→ rewrite_below，不要自己编造阈值。
7. **点名章节 + 改动** → rewrite_chapters(chapters=[N]) 并在 brief 里写清改法；
   作者只是表达对某章的看法、没让改 → directive(scope=chapters) 记录，不立即重写。
8. **结构问题**（卷拖、章序不对、大纲要重排）→ plan_change，把作者的话提炼成 instruction。
9. **只有真的缺关键信息才问**（比如「改那章」但全文没有任何指代线索）。能合理推断的直接做，
   并在 reply 里说明你的判断。
10. **不要编造项目里没有的事实**（章节号、分数、人物、情节）。不确定就看 context 或问。
11. 动作参数里的中文数字必须转成阿拉伯数字。
12. 作者撤回/否定之前的要求（「刚才那条不算」「别按那个改了」）→ revoke_briefs，
    撤掉对应工作单；不要用一条相反的新工作单去叠旧单。

## 输出格式（严格 JSON，不要 markdown 代码块，不要多余文字）
{{
  "understanding": "一句话复述你理解到的真实诉求（含隐含条件和指代）",
  "reply": "直接对作者说的话：自然、具体、像总编辑。不要复述 JSON，不要列第1步/第2步，不要说「我将调用…」",
  "actions": [
    {{"action": "动作名", "params": {{}}, "brief": {{}}}} 
  ],
  "confidence": 0.0-1.0
}}
reply 规则：
- 只讨论时，reply 就是你的完整回答；
- 要派活时，reply 简短说明你理解到了什么、准备怎么安排（执行结果会由系统接在后面）；
- 不确定时可以在 reply 里说明你的假设，但不要空泛。
actions 为空数组表示只说话不做事。

## 当前作品上下文（真实数据，务必据此判断）
{context}
"""


# ── 上下文采集 ─────────────────────────────────────────────

_MEDIUM_OF_PREFIX = {"s_": "script", "p_": "poetry", "e_": "prose", "t_": "thesis",
                     "c_": "children", "n_": "news", "k_": "copy", "o_": "other"}


def _medium_of(key: str) -> str:
    return _MEDIUM_OF_PREFIX.get(str(key or "")[:2], "novel")


def _chapter_rows(plan: dict) -> list[dict]:
    """小说/剧本/诗词/散文的计划结构不同，统一抽成 {no,title,status,score,words}。"""
    if not isinstance(plan, dict):
        return []
    seq = None
    for k in ("chapters", "scenes", "sections", "rounds", "items", "beats"):
        if isinstance(plan.get(k), list):
            seq = plan[k]
            break
    if not seq:
        return []
    scores = (plan.get("meta") or {}).get("chapter_scores") or {}
    out = []
    for i, c in enumerate(seq, 1):
        if not isinstance(c, dict):
            continue
        no = c.get("chapterNumber") or c.get("sceneNumber") or c.get("roundNumber") \
            or c.get("number") or c.get("no") or i
        try:
            no = int(no)
        except (TypeError, ValueError):
            continue
        sc = (scores.get(str(no)) or {}).get("score")
        out.append({
            "no": no,
            "title": str(c.get("title") or c.get("name") or "")[:40],
            "status": str(c.get("status") or ""),
            "score": sc if isinstance(sc, (int, float)) else None,
            "words": int(c.get("wordCount") or c.get("words") or 0),
        })
    return out


def recent_history(pdir: str, limit: int = 10) -> list[dict]:
    """最近对话（不含章节正文卡），供总编辑接住「刚才那个」「那就按你说的」。"""
    p = os.path.join(pdir or "", "chat_log.jsonl")
    if not p or not os.path.exists(p):
        return []
    rows = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-160:]
        for line in lines:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict) or (d.get("extra") or {}).get("type") == "chapter":
                continue
            role = str(d.get("role") or "")
            text = str(d.get("content") or "").strip()
            if role in ("user", "assistant") and text and len(text) < 1200:
                rows.append({"role": role, "content": text[:500]})
    except OSError:
        return []
    return rows[-limit:]


def _recent_events(key: str, n: int = 6) -> list[str]:
    try:
        from . import steps as _steps
        items = (_steps.since(key, cursor=0, limit=40) or {}).get("items") or []
        out = []
        for e in items[-n:]:
            label = e.get("agent_label") or e.get("agent") or ""
            title = e.get("title") or ""
            output = e.get("output") or ""
            out.append(f"{label}：{title}" + (f"（{output[:80]}）" if output else ""))
        return out
    except Exception:
        return []


def _book_file(pdir: str, names) -> str:
    for fn in names:
        p = os.path.join(pdir or "", fn)
        if os.path.exists(p):
            try:
                return open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                return ""
    return ""


def build_context(proj, st, key, digest: str = "", history=None) -> dict:
    """采集总编辑做判断所需的真实状态（纯读，不调 LLM）。"""
    ctx = {}
    try:
        ctx = _planner.build_context(proj, st, key) or {}
    except Exception:
        ctx = {"state": (st or {}).get("state") or "", "done": (st or {}).get("done") or 0,
               "total": (st or {}).get("total") or 0}
    pdir = getattr(proj, "path", "") or ""
    plan = {}
    try:
        plan = (proj.plan() or {}) if proj else {}
    except Exception:
        plan = {}
    cfg = plan.get("config") if isinstance(plan.get("config"), dict) else {}
    ctx["medium"] = _medium_of(key)
    ctx["pdir"] = pdir
    ctx["config"] = {k: cfg.get(k) for k in
                     ("genre", "idea", "tone", "perspective", "protagonist", "theme", "chapters")
                     if cfg.get(k)}
    ctx["chapter_rows"] = _chapter_rows(plan)
    try:
        from . import taste as _taste
        ctx["form"] = _taste.form_of(cfg)
    except Exception:
        ctx["form"] = ""
    chars = plan.get("characters") or plan.get("cast") or []
    if chars:
        ctx["characters"] = [
            f"{c.get('name', '')}（{c.get('role', '') or '角色'}）：{str(c.get('desc') or c.get('personality') or '')[:80]}"
            for c in chars[:12] if isinstance(c, dict)]
    else:
        ctx["characters"] = []
    ctx["outline_tail"] = _book_file(pdir, ("01-大纲.md", "02-大纲.md"))[-800:]
    ctx["open_briefs"] = _brief.open_items(pdir) if pdir else []
    ctx["recent_events"] = _recent_events(key)
    ctx["history"] = history if history is not None else recent_history(pdir)
    ctx["digest"] = digest or ""
    try:
        from . import writer as _w
        ctx["pending_queue"] = _w.pending_queue(key) if pdir else []
    except Exception:
        ctx["pending_queue"] = []
    return ctx


def _context_text(ctx: dict) -> str:
    lines = []
    try:
        lines.append(_planner._ctx_text(ctx).strip())
    except Exception:
        pass
    med_label = {"novel": "小说", "script": "剧本", "poetry": "诗词", "prose": "散文",
                 "thesis": "论文", "children": "童书", "news": "新闻", "copy": "文案",
                 "other": "其他"}.get(ctx.get("medium"), ctx.get("medium") or "")
    extra = [f"- 文体：{med_label}" + (f"；形态：{ctx['form']}" if ctx.get("form") else "")]
    cfg = ctx.get("config") or {}
    if cfg:
        extra.append("- 本书设定：" + "；".join(f"{k}={v}" for k, v in cfg.items()))
    if ctx.get("characters"):
        extra.append("- 主要人物：\n  " + "\n  ".join(ctx["characters"][:10]))
    rows = ctx.get("chapter_rows") or []
    if rows:
        head = [f"{r['no']}｜{r['title']}｜{r['status']}"
                + (f"｜{r['score']}分" if r.get("score") is not None else "")
                + (f"｜{r['words']}字" if r.get("words") else "")
                for r in rows[:60]]
        extra.append("- 章节账本（章号｜标题｜状态｜分数｜字数）：\n  " + "\n  ".join(head))
        if len(rows) > 60:
            extra.append(f"  …（另有 {len(rows) - 60} 章未展开）")
    if ctx.get("outline_tail"):
        extra.append("- 大纲尾部：\n" + ctx["outline_tail"])
    if ctx.get("open_briefs"):
        bs = []
        for i, b in enumerate(ctx["open_briefs"][:8], 1):
            bs.append(f"  {i}. {_brief.render_one(b)}")
        extra.append("- 当前生效的工作单（新→旧，1=最新；作者撤销时按序号）：\n" + "\n".join(bs))
    if ctx.get("pending_queue"):
        extra.append("- 尚未消化的即时指令：" + "；".join(str(x.get("text") or "")[:60]
                                                       for x in ctx["pending_queue"][:5]))
    if ctx.get("recent_events"):
        extra.append("- 团队最近动作：\n  " + "\n  ".join(f"  · {x}" for x in ctx["recent_events"]))
    if ctx.get("running_rewrite"):
        extra.append("- 注意：该书当前有改写任务在跑。")
    if ctx.get("digest"):
        extra.append("- 更早的对话摘要：\n" + str(ctx["digest"])[:1200])
    hist = ctx.get("history") or []
    if hist:
        hs = [f"  {h['role']}：{h['content']}" for h in hist[-10:]]
        extra.append("- 最近对话（旧→新）：\n" + "\n".join(hs))
    lines.extend(extra)
    return "\n".join(x for x in lines if x)


# ── 理解与决策 ─────────────────────────────────────────────

def _actions_text() -> str:
    out = []
    for name, meta in ACTIONS.items():
        p = meta.get("params") or {}
        p_txt = f"（params: {json.dumps(p, ensure_ascii=False)}）" if p else ""
        out.append(f"- {name}：{meta['desc']}{p_txt}")
    return "\n".join(out)


def _norm_action(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("action") or raw.get("name") or "").strip()
    name = _ACTION_ALIASES.get(name, name)
    if name not in ACTIONS:
        return None
    params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    # 模型偶尔把参数平铺在 action 对象上
    for k in ("chapters", "chapter", "count", "mode", "below", "target", "title",
              "scope", "instruction", "allow_past", "kind", "brief", "index",
              "goal", "must_keep", "must_change", "acceptance", "avoid"):
        if k in raw and k not in params:
            params[k] = raw[k]
    if "chapter" in params and "chapters" not in params:
        params["chapters"] = params.pop("chapter")
    ch = params.get("chapters")
    if isinstance(ch, (int, float, str)):
        ch = [ch]
    if isinstance(ch, list):
        nums = []
        for x in ch:
            try:
                n = int(x)
            except (TypeError, ValueError):
                m = re.search(r"\d+", str(x))
                if not m:
                    continue
                n = int(m.group())
            if 0 < n < 10000:
                nums.append(n)
        params["chapters"] = nums
    brief = raw.get("brief") if isinstance(raw.get("brief"), dict) else params.get("brief")
    if not isinstance(brief, dict):
        # 模型偶尔把工作单字段直接平铺在 params 里
        _bk = ("goal", "must_keep", "must_change", "acceptance", "avoid",
               "scope", "chapters", "summary", "requirement")
        if name in ("directive", "rewrite_chapters", "rewrite_below", "plan_change") \
                and any(k in params for k in _bk):
            brief = {k: params[k] for k in _bk if k in params}
        else:
            brief = None
    return {"action": name, "params": params, "brief": brief}


def deliberate(text: str, ctx: dict) -> dict | None:
    """理解作者的话并给出决策。不可用/解析失败返回 None（调用方回退旧链路）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        from . import llm
        sys_p = _SYSTEM_PROMPT.format(actions=_actions_text(), context=_context_text(ctx or {}))
        msgs = [{"role": "system", "content": sys_p},
                {"role": "user", "content": text}]
        raw = llm.chat_json(msgs, cfg=None, stage="planning", agent="chief",
                            temperature=0.2, use_cache=False)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    reply = str(raw.get("reply") or raw.get("answer") or "").strip()
    understanding = str(raw.get("understanding") or "").strip()[:300]
    actions = []
    for a in (raw.get("actions") or [])[:6]:
        na = _norm_action(a)
        if na:
            actions.append(na)
    # 去重，最多 4 步
    seen, uniq = set(), []
    for a in actions:
        sig = (a["action"], json.dumps(a.get("params") or {}, ensure_ascii=False, sort_keys=True))
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(a)
    actions = uniq[:4]
    # discuss 只允许单独出现
    if any(a["action"] == "discuss" for a in actions):
        actions = [a for a in actions if a["action"] == "discuss"][:1]
    if not reply and not actions:
        return None
    if not reply:
        reply = "收到，我来安排。"
    try:
        conf = float(raw.get("confidence", 0.7))
    except (TypeError, ValueError):
        conf = 0.7
    return {"understanding": understanding, "reply": reply,
            "actions": actions, "confidence": max(0.0, min(1.0, conf))}
