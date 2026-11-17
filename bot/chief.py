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

import difflib
import json
import os
import re
import time

from . import brief as _brief
from . import planner as _planner
from .paths import DATA_ROOT

# ── 总编辑可派的动作（与 app.py `_run` / 执行器一一对应） ───────────────────

ACTIONS = {
    "discuss": {
        "desc": "只回答、讨论、给专业判断，不执行任何创作动作（问看法、闲聊、征求建议、否定上一版）",
        "params": {},
    },
    "report_status": {
        "desc": "汇报当前作品的进度、字数、评分、当前在做什么",
        "params": {},
    },
    "read_chat": {
        "desc": "读取某部作品最近的对话内容（可以是另一本书）。作者说「读一下 X 最后交流了什么」"
                "「看看 X 最近聊到哪」「X 的聊天记录」时用；不要凭记忆编造对话",
        "params": {"project": "作者说的书名/关键词；不填=当前作品", "limit": "条数，默认 6"},
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
    "rewrite_short": {
        "desc": "按篇幅重写：作者说「低于3000字的章节重写」「太短的章节补上」「字数不够的」时用。"
                "这是按正文字数筛，不是按评分筛；和 rewrite_below 完全不同",
        "params": {"below_words": "字数阈值，默认 3000"},
    },
    "score_chapters": {
        "desc": "给章节评分；不带 chapters=补评所有未评分章",
        "params": {"chapters": "[章号整数] 或 null"},
    },
    "audit": {"desc": "一致性巡检：找全书矛盾、盘点超期未收的伏笔", "params": {}},
    "health_check": {
        "desc": "全书体检：检查字数不足、结尾截断、被写成说明文字、文件缺失的章节，"
                "并自动从版本库还原能还原的稿子；作者说「体检」「检查全书」「书稿有没有问题」时用",
        "params": {},
    },
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
    "rewrite_by_words": "rewrite_short", "rewrite_words": "rewrite_short",
    "expand_short": "rewrite_short", "rewrite_short_chapters": "rewrite_short",
    "score": "score_chapters", "score_all_unscored": "score_chapters",
    "consistency_audit": "audit",
    "health_check": "health_check", "check_book": "health_check", "book_health": "health_check",
    "体检": "health_check", "书稿体检": "health_check", "全书检查": "health_check",
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
6. **分清「字数门槛」和「分数阈值」——这是两种完全不同的任务：**
   - 作者说「低于3000字」「不到两千字的」「太短的章节」「字数不够的」「只有几百字的」→
     rewrite_short(below_words=该字数)，按正文实测字数筛，**绝不能**当成评分阈值去 rewrite_below；
   - 作者说「低于60分」「不达标/评分不够」→ rewrite_below(below=该分数)。
   上下文里给了「少于3000字的章节」实测清单，按它判断规模，不要自己猜。
7. **作者说「前面的任务不要了/只按最新的来/先停掉旧的再…」→ 同一轮 actions 里先
   cancel_task(kind=rewrite)，紧接着派新动作**（如 rewrite_short），两步一次做完。
   分解出来的步骤会自动连续执行到底，长任务在后台跑，**不要让作者再来说「继续」**。
8. **点名章节 + 改动** → rewrite_chapters(chapters=[N]) 并在 brief 里写清改法；
   作者只是表达对某章的看法、没让改 → directive(scope=chapters) 记录，不立即重写。
9. **结构问题**（卷拖、章序不对、大纲要重排）→ plan_change，把作者的话提炼成 instruction。
10. **只有真的缺关键信息才问**（比如「改那章」但全文没有任何指代线索）。能合理推断的直接做，
   并在 reply 里说明你的判断。
11. **不要编造项目里没有的事实**（章节号、分数、人物、情节）。不确定就看 context 或问。
12. 动作参数里的中文数字必须转成阿拉伯数字。
13. 作者撤回/否定之前的要求（「刚才那条不算」「别按那个改了」）→ revoke_briefs，
    撤掉对应工作单；不要用一条相反的新工作单去叠旧单。
14. 作者让你「读一下 / 看看 / 翻一下」某本书的聊天、交流、聊天记录（包括桌面端里的别的作品）
    → read_chat(project=作者说的书名或关键词)。上下文里有「其他作品」清单，按名字匹配；
    读不到就如实说没找到，绝不编造对话内容。只说当前作品时 project 留空。
15. **暂停/继续要接对任务。** 作者说「暂停」→ pause；说「继续」→ write(mode=resume_task)。
    reply 里必须说清这次继续的是**哪件事、哪些章**，不要用旧任务的进度冒充新要求。

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


_PLAN_FILES = ("02-写作计划.json", "剧本计划.json", "诗词计划.json", "散文计划.json",
               "论文计划.json", "计划.json")
_MEDIUM_LABEL = {"novel": "小说", "script": "剧本", "poetry": "诗词", "prose": "散文",
                 "thesis": "论文", "children": "童书", "news": "新闻", "copy": "文案",
                 "other": "其他"}


def _project_name(pdir: str, key: str) -> str:
    for fn in _PLAN_FILES:
        p = os.path.join(pdir, fn)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for k in ("novelName", "title", "name", "projectName"):
            if isinstance(d.get(k), str) and d[k].strip():
                return d[k].strip()
    base = os.path.basename(pdir or key)
    m = re.match(r"^[a-z_]*\d{8}-\d{6}-(.+)$", base)
    return (m.group(1) if m else base).strip("#_ ")


def _chat_rows(pdir: str, limit: int = 60) -> list[dict]:
    """该项目最近的对话行（跳过章节正文卡）。"""
    p = os.path.join(pdir or "", "chat_log.jsonl")
    if not p or not os.path.exists(p):
        return []
    rows = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f.readlines()[-400:]:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not isinstance(d, dict) or (d.get("extra") or {}).get("type") == "chapter" \
                        or d.get("type") == "chapter":
                    continue
                role = str(d.get("role") or "")
                text = str(d.get("content") or "").strip()
                if role in ("user", "assistant") and text:
                    rows.append({"role": role, "text": text, "ts": d.get("ts")})
    except OSError:
        return []
    return rows[-max(1, int(limit or 60)):]


def list_projects(limit: int = 30) -> list[dict]:
    """这台客户端上的全部作品（名字 + 最近一句对白），供总编辑跨书指代。"""
    root = os.path.join(DATA_ROOT, "projects")
    if not os.path.isdir(root):
        return []
    out = []
    try:
        names = [d for d in os.listdir(root) if not d.startswith(".")]
    except OSError:
        return []
    for key in names:
        pdir = os.path.join(root, key)
        if not os.path.isdir(pdir):
            continue
        try:
            mtime = os.path.getmtime(pdir)
        except OSError:
            mtime = 0
        name = _project_name(pdir, key)
        rows = _chat_rows(pdir, 6)
        last = ""
        if rows:
            r = rows[-1]
            last = ("作者：" if r["role"] == "user" else "总编辑：") + r["text"][:60].replace("\n", " ")
        out.append({"key": key, "name": name, "medium": _medium_of(key),
                    "mtime": mtime, "last": last})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:max(1, int(limit or 30))]


def _fmt_ts(ts) -> str:
    try:
        t = int(float(ts))
    except (TypeError, ValueError):
        return ""
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t > 0 else ""


def _clean_query(q: str) -> str:
    s = re.sub(r"[《》〈〉「」『』\"'“”‘’\s]", "", str(q or ""))
    for w in ("帮我","请","麻烦","读取一下","读一下","读一下","读取","看看","看一下","翻一下","查一下",
              "我桌面端","桌面端","电脑端","客户端","本地","最后交流的内容","最后交流","最后聊的",
              "最后聊天","最近交流","最近聊","聊天记录","交流内容","交流","聊天","内容是什么","内容",
              "是什么","的","这本书","那本书","作品"):
        s = s.replace(w, "")
    return s.strip("的了")


def read_last_chat(project: str = "", limit: int = 6,
                   current_key: str = "", current_pdir: str = "") -> str:
    """读取某部作品最近的对话。找不到就如实说，不编造。"""
    try:
        limit = max(1, min(int(limit or 6), 20))
    except (TypeError, ValueError):
        limit = 6
    q = _clean_query(project)
    target = None
    cur_name = _project_name(current_pdir, current_key) if current_pdir else ""
    if not q or (cur_name and (q in cur_name or cur_name in q)) \
            or (current_key and current_key in str(project or "")):
        target = {"key": current_key, "name": cur_name or current_key, "pdir": current_pdir}
    else:
        best, best_score = None, 0
        for it in list_projects(60):
            name = it["name"]
            if q and (q in name or name in q):
                score = min(len(q), len(name)) + (10 if q == name else 0)
            else:
                score = int(difflib.SequenceMatcher(None, q, name).ratio() * 10) \
                    if len(q) >= 2 else 0
                score = score if score >= 5 else 0
            if score > best_score:
                best, best_score = it, score
        if best:
            target = {"key": best["key"], "name": best["name"],
                      "pdir": os.path.join(DATA_ROOT, "projects", best["key"])}
    if not target or not target.get("pdir"):
        names = "、".join(f"《{x['name']}》" for x in list_projects(12))
        return (f"没找到你说的作品「{str(project or '').strip() or '当前作品'}」。"
                + (f"这台客户端上现在有：{names}。" if names else "这台客户端上还没有作品。"))
    rows = _chat_rows(target["pdir"], 60)
    if not rows:
        return f"《{target['name']}》还没有可读取的对话记录。"
    tail = rows[-limit:]
    lines = [f"《{target['name']}》最近的交流（最近 {len(tail)} 条，章节正文卡不在内）："]
    for r in tail:
        who = "作者" if r["role"] == "user" else "总编辑"
        ts = _fmt_ts(r.get("ts"))
        txt = r["text"].replace("\n", " ")
        lines.append(f"· {ts + ' ' if ts else ''}{who}：{txt[:300]}{'…' if len(txt) > 300 else ''}")
    return "\n".join(lines)


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


def _short_chapter_nos(plan: dict, pdir: str, below: int = 3000) -> list:
    """按磁盘正文字数找出篇幅不足的章（不信计划里的旧 wordCount）。"""
    try:
        from . import wordguard
        from . import writer as _writer
    except Exception:
        return []
    out = []
    for c in (plan or {}).get("chapters") or []:
        fp = os.path.join(pdir or "", c.get("filePath") or "")
        if not c.get("filePath") or not os.path.exists(fp):
            continue
        try:
            body = open(fp, encoding="utf-8", errors="replace").read()
            words = wordguard.cn_len(_writer._clean_chapter_text(body))
        except Exception:
            continue
        if words < int(below):
            try:
                out.append(int(c.get("chapterNumber") or 0))
            except (TypeError, ValueError):
                continue
    return sorted(out)


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
    ctx["short_chapters_3000"] = _short_chapter_nos(plan, pdir, 3000)
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
    ctx["other_projects"] = [p for p in list_projects(20) if p.get("key") != key]
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
    if ctx.get("short_chapters_3000"):
        _sk = ctx["short_chapters_3000"]
        extra.append(f"- **按正文实测**：少于 3000 字的章节共 {len(_sk)} 章"
                     f"（第 {'、'.join(map(str, _sk[:20]))}{'…' if len(_sk) > 20 else ''} 章）。"
                     f"作者说「低于N字/太短」时按字数筛（rewrite_short），不要按评分筛。")
    else:
        extra.append("- **按正文实测**：没有少于 3000 字的章节。")
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
    if ctx.get("other_projects"):
        ps = [f"  · 《{p['name']}》（{_MEDIUM_LABEL.get(p.get('medium'), '')}）"
              + (f" 最近：{p['last']}" if p.get("last") else "")
              for p in ctx["other_projects"][:15]]
        extra.append("- 这台客户端上的其他作品（作者可能让你跨书读取）：\n" + "\n".join(ps))
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
    for k in ("chapters", "chapter", "count", "mode", "below", "below_words", "target", "title",
              "scope", "instruction", "allow_past", "kind", "brief", "index",
              "project", "limit",
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
