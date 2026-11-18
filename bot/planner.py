"""v4.22.0：创作 Agent 规划器（Planner）——先理解自然语言，再自主规划并执行。

设计动机（用户明确要求）：
    固定句式规则永远覆盖不全（「重写所有未达标的章节」曾被误判为创作要求并入下一章）。
    因此把「理解」交给 LLM：输入 = 用户原话 + 真实项目状态，输出 = 可执行的多步计划。
    规则匹配只保留为「无模型/断网」时的兜底，不再是主链路。

工作流（铁律）：
    ① 理解 —— 读懂用户到底想要什么（可结合当前项目状态：写到第几章、多少章不达标…）
    ② 规划 —— 拆成有序的 action 步骤（单步或多步）
    ③ 执行 —— 逐条交给 app.py 的 _run(action, params) 执行并回报

对外：
    ACTIONS            —— 动作注册表（执行器据此校验，防 LLM 幻觉出未知动作）
    build_context()    —— 采集真实项目状态（喂给理解层，避免凭空猜测）
    plan_actions()     —— 理解 + 规划，返回 {"understanding", "steps":[{action,title,params,why}]}
                          不可用/解析失败返回 None（调用方回退传统单意图）
"""
import json

# ── 动作注册表：名称与 app.py `_run(intent, params)` 的分支一一对应 ─────────────
ACTIONS = {
    "resume_task":        {"desc": "接着上次没做完的事继续（「继续」「接着写」「别停」「继续刚才那个」）",
                           "params": {}},
    "start_writing":      {"desc": "开始写这本书（从第一章起）", "params": {}},
    "start_writing_all":  {"desc": "一口气把剩余章节全部写完，中途不停", "params": {}},
    "resume_writing":     {"desc": "按当前节奏继续推进（没指定数量时的「继续」）", "params": {}},
    "batch_writing":      {"desc": "写指定数量后停下", "params": {"batch_size": "整数或 'all'"}},
    "pause_writing":      {"desc": "暂停创作", "params": {}},
    "rewrite_chapter":    {"desc": "重写/修改指定的某一章", "params": {"chapter_number": "整数"}},
    "rewrite_low_score":  {"desc": "重写低于某个分数的章节（用户给了分数阈值时）",
                           "params": {"below": "整数分数阈值", "target": "目标分（可选）"}},
    "rewrite_all_below":  {"desc": "重写所有未达标的章节（没给具体分数时，按系统达标线）", "params": {}},
    "score_all_unscored": {"desc": "给所有还没有评分的章节评分/打分", "params": {}},
    "consistency_audit":  {"desc": "一致性巡检：扫描全书矛盾、盘点超期未收的坑，产出修复计划", "params": {}},
    "query_status":       {"desc": "汇报进度/字数/状态", "params": {}},
    "query_outline":      {"desc": "展示大纲/目录/章节列表", "params": {}},
    "set_title":          {"desc": "改书名/标题", "params": {"title": "新书名"}},
    "set_style":          {"desc": "调整文风/语气/人称/叙事视角（并入后续创作）",
                           "params": {"style_desc": "风格要求原文"}},
    "add_constraint":     {"desc": "追加设定/人物/世界观/禁止项（并入后续创作）",
                           "params": {"constraint_text": "要求原文"}},
    "creative_hint":      {"desc": "普通创作建议、内容修改意见、闲聊——并入下一章创作",
                           "params": {}},
    "export_book":        {"desc": "导出/下载成书", "params": {}},
    "new_project":        {"desc": "想另开一本新书/重新开始", "params": {}},
}

_SYSTEM_PROMPT = """你是「一幕·YIMU 神笔」创作台的**任务规划 Agent**。用户用任意口语表达诉求，
你要先真正读懂他要什么，再拆成可执行的动作步骤。你不是关键词匹配器——不要照抄任何例句，
按语义判断。

## 可用动作（action 只能取这些名字）
{actions}

## 输出格式（严格 JSON，不要 markdown 代码块，不要多余文字）
{{
  "understanding": "一句话复述你理解到的真实诉求（含你注意到的隐含条件，如涉及哪些章节）",
  "steps": [
    {{"action": "动作名", "title": "这一步做什么（中文短语）", "params": {{}}, "why": "为什么这一步必要"}}
  ]
}}

## 判断原则（按优先级）
0. **先读「项目记忆」再判断**。「继续 / 接着 / 别停 / 继续刚才那个 / 还没写完的那个呢」
   一律是 **resume_task**——不是重新开始，也不是按字面理解成「写下一章」。
   项目记忆里写了「进行中的任务」和「中断原因」：若上次是批量重写停在半路，就恢复重写；
   若是补评分、补章卡，就恢复那一件；若上次是写作停在半路，就恢复写作。
   **不要**因为用户只说了两个字就臆造新任务，也**不要**一律当成「写下一章」。
   只有用户明确说「重新开始 / 从头写 / 重写这本书」时才用 new_project / start_writing。
1. **先看用户到底要什么，别被字面词带偏**：说「重写」「改改」不等于「继续写」；
   提到「所有/全部/没达标的/低分的」= 批量处理，不是写新章。
2. **可多步**：一句话里有多个诉求就拆成多步，按合理顺序排列。
   例：「把不达标的都重写，没评分的先打分，然后继续写 5 章」
   → [score_all_unscored, rewrite_all_below, batch_writing(5)]
3. **用户给了具体分数阈值**（如「低于60分」「60分以下」）→ rewrite_low_score（below=该阈值）；
   **用户没提分数**（只说「未达标/不达标/质量不行/整体提一提」）→ 必须用 **rewrite_all_below**，
   不要自己编造 80 之类的阈值。
4. **纯内容建议**（「节奏快一点」「加点反转」）→ creative_hint（会并入后续创作，不要误当新任务）。
5. **只问不写**（「写到哪了」「多少分」）→ query_status。
6. 拿不准时**宁少勿多**：单步即可，不要臆造用户没说的动作。
7. 用户点名了章节 → rewrite_chapter(chapter_number)；「从第 N 章起重写/后面都要改」
   → rewrite_chapter 传该章号（若用户意在全部后续，可用 rewrite_all_below 但要在 title 说明）。
8. **params 只填该动作需要的字段**，没有就是 {{}}。中文数字要转成阿拉伯数字。

## 当前项目状态（真实数据，务必据此判断，不要假设）
{context}
"""


def build_context(proj, st, key) -> dict:
    """采集真实项目状态，供理解层判断（不调 LLM，纯读）。"""
    ctx = {
        "state": (st or {}).get("state") or "",
        "done": int((st or {}).get("done") or 0),
        "total": int((st or {}).get("total") or 0),
        "words": int((st or {}).get("words") or 0),
        "running": bool((st or {}).get("running")),
        "paused": bool((st or {}).get("paused")),
        "name": (st or {}).get("name") or "",
    }
    try:
        from bot import writer as _w
        ctx["pass_line"] = _w._pass_line()
    except Exception:
        ctx["pass_line"] = 80
    try:
        plan = (proj.plan() or {}) if proj else {}
        meta_scores = (plan.get("meta") or {}).get("chapter_scores") or {}
        chapters = plan.get("chapters") or []
        completed = [c for c in chapters if c.get("status") == "completed"]
        scored = []
        unscored = []
        for c in completed:
            no = c.get("chapterNumber")
            sc = (meta_scores.get(str(no)) or {}).get("score")
            if isinstance(sc, (int, float)):
                scored.append((int(no), float(sc)))
            else:
                unscored.append(int(no))
        below = sorted([n for n, s in scored if s < ctx["pass_line"]])
        ctx["scored_count"] = len(scored)
        ctx["unscored_count"] = len(unscored)
        ctx["unscored_chapters"] = unscored[:20]
        ctx["below_count"] = len(below)
        ctx["below_chapters"] = below[:20]
        if scored:
            ctx["avg_score"] = round(sum(s for _, s in scored) / len(scored), 1)
            ctx["min_score"] = min(s for _, s in scored)
        ctx["batch_size"] = str((plan.get("config") or {}).get("batch_size") or "")
        ctx["next_chapter"] = next((int(c.get("chapterNumber")) for c in chapters
                                    if c.get("status") != "completed"), 0)
    except Exception:
        pass
    # v4.22.2：项目记忆（任务档案 + 章内草稿 + 最近事件）——「继续」全靠这一段才答得对
    try:
        pdir = proj.path if proj else ""
        if pdir:
            from bot import journal as _jn
            ctx["memory"] = _jn.recall_text(pdir, n=6)
            task = _jn.current_task(pdir) or {}
            ctx["active_task"] = {
                "kind": task.get("kind") or "",
                "label": _jn.KIND_LABEL.get(task.get("kind") or "", task.get("kind") or ""),
                "title": task.get("title") or "",
                "current": task.get("current"),
                "done": task.get("done"),
                "total": task.get("total"),
                "stage": task.get("stage"),
                "note": task.get("note") or "",
            } if task else None
            _no = int(ctx.get("next_chapter") or 0)
            if _no:
                from bot import writer as _w2
                _d = _w2.draft_info(proj, _no)
                if _d.get("has"):
                    ctx["draft"] = _d
    except Exception:
        pass
    return ctx


def _ctx_text(ctx: dict) -> str:
    """把状态字典转成给模型看的中文摘要（紧凑，省 token）。"""
    if not ctx:
        return "（无）"
    lines = [
        f"- 作品：《{ctx.get('name') or '未命名'}》；状态 {ctx.get('state') or '未知'}"
        f"{'（正在写作）' if ctx.get('running') else ''}{'（已暂停）' if ctx.get('paused') else ''}",
        f"- 进度：{ctx.get('done', 0)}/{ctx.get('total', 0)} 章，已写 {ctx.get('words', 0)} 字"
        f"{f'；下一章是第 {ctx.get("next_chapter")} 章' if ctx.get('next_chapter') else ''}",
        f"- 达标线：{ctx.get('pass_line', 80)} 分",
    ]
    if ctx.get("scored_count") is not None:
        lines.append(f"- 评分：已评 {ctx.get('scored_count')} 章"
                     f"{('（均分 ' + str(ctx.get('avg_score')) + ('，最低 ' + str(ctx.get('min_score')) if ctx.get('min_score') is not None else '') + '）') if ctx.get('avg_score') is not None else ''}"
                     f"；未评 {ctx.get('unscored_count', 0)} 章"
                     f"{('：第 ' + '、'.join(map(str, ctx.get('unscored_chapters') or [])) + ' 章') if ctx.get('unscored_chapters') else ''}")
        lines.append(f"- 未达标（< {ctx.get('pass_line')} 分）：{ctx.get('below_count', 0)} 章"
                     f"{('：第 ' + '、'.join(map(str, ctx.get('below_chapters') or [])) + ' 章') if ctx.get('below_chapters') else ''}")
    if ctx.get("batch_size"):
        lines.append(f"- 开书批量设置：{ctx['batch_size']}")
    if ctx.get("draft"):
        d = ctx["draft"]
        lines.append(f"- 第 {d.get('chapter')} 章有中断前草稿 {d.get('words')} 字（继续=从草稿末尾续写，不要重写整章）")
    # 项目记忆放最后：这是「上次在干什么」的权威答案，比一切推断都优先
    if ctx.get("memory"):
        lines.append("")
        lines.append("## 项目记忆（上次在做什么 / 刚发生了什么）")
        lines.append(str(ctx["memory"]))
    return "\n".join(lines)


def plan_actions(text: str, ctx: dict | None = None):
    """理解 + 规划。返回 {"understanding": str, "steps": [...]} 或 None（不可用/解析失败）。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        from bot import llm
        actions_txt = "\n".join(f"- {name}：{meta['desc']}"
                                + (f"（params: {json.dumps(meta['params'], ensure_ascii=False)}）" if meta["params"] else "")
                                for name, meta in ACTIONS.items())
        sys_p = _SYSTEM_PROMPT.format(actions=actions_txt, context=_ctx_text(ctx or {}))
        msgs = [{"role": "system", "content": sys_p},
                {"role": "user", "content": text}]
        res = None
        for _attempt in range(2):      # 限流/瞬时失败重试一次（规划器是主链路，宁可多等）
            try:
                res = llm.chat_json(msgs, cfg=None, stage="planning",
                                    agent="planner", temperature=0.2)
                if isinstance(res, dict) and isinstance(res.get("steps"), list):
                    break
            except Exception as _e:
                if _attempt == 0:
                    import time as _t
                    _t.sleep(1.5)
                else:
                    print(f"[planner] 规划失败（回退单意图）：{type(_e).__name__}: {_e}", flush=True)
                    return None
        if not isinstance(res, dict):
            return None
        steps = res.get("steps")
        if not isinstance(steps, list):
            return None
        clean = []
        for stp in steps:
            if not isinstance(stp, dict):
                continue
            act = str(stp.get("action") or "").strip()
            if act not in ACTIONS:
                continue          # 幻觉动作直接丢弃
            params = stp.get("params") if isinstance(stp.get("params"), dict) else {}
            clean.append({
                "action": act,
                "title": str(stp.get("title") or ACTIONS[act]["desc"])[:40],
                "params": params,
                "why": str(stp.get("why") or "")[:80],
            })
        if not clean:
            return None
        return {"understanding": str(res.get("understanding") or "")[:200], "steps": clean[:5]}
    except Exception:
        return None
