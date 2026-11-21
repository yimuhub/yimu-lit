"""划词 AI（v5.1.0）：写作台选中文字后浮出的六动作文本处理。

动作与温度（逐动作定温，纠错要稳、扩写要放）：
  fix    纠错      0.10  只改错别字/标点，不动剧情
  polish 润色      0.70  提表达，不替用户扩剧情
  expand 扩写      0.85  在原句骨架上补细节
  deslop 去 AI 味  0.75  套话换具体细节（沿用本库三层去味词表风格）
  tone   按人物口吻 0.70  对白/叙述贴合指定人物说话方式
  custom 自定义指令 0.70  用户一句指令处理选区

上下文窗口：选区前后各 220 字 + 章题 + 本章概要（由前端从工作文件里截好传来，
后端仍做二次钳制，防超长 payload 直接灌进提示词）。

调用一律走 bot/llm.py 现有封装（chat / chat_stream），不新增网络层；
结果只回给前端预览面板，**不落稿**——替换与写盘由前端在选区校验通过后触发。
本模块不依赖项目目录，key 仅用于 token 记账。
"""
from __future__ import annotations

import re

from . import prompts_store

# ---------------------------------------------------------------- 词表（自建，与 slop_checker 同风格）
# 去 AI 味提示词里的「高频套话清单」——沿本库 slop_checker 词表风格自研，
# 只取写作场景高频项，不整表照搬（划词窗口小，清单要短）。
_SLOP_HINTS = (
    "不仅仅/不只是/更是、让我们、总而言之/综上所述、在当今这个…的时代、"
    "研究表明/专家表示/人们普遍认为、显然/无疑/毋庸置疑、"
    "眼中闪过一丝…、嘴角勾起一抹…、空气仿佛凝固了、时间仿佛静止了、"
    "他知道…也知道…、这不是…而是…、像是…又像是…、"
    "内心五味杂陈、眼中满是坚定、无法言喻的…、一股莫名的…"
)

# ---------------------------------------------------------------- 动作注册表

ACTIONS: dict[str, dict] = {
    "fix":    {"label": "纠错",      "temperature": 0.10, "max_chars": 4000},
    "polish": {"label": "润色",      "temperature": 0.70, "max_chars": 4000},
    "expand": {"label": "扩写",      "temperature": 0.85, "max_chars": 2000},
    "deslop": {"label": "去 AI 味",  "temperature": 0.75, "max_chars": 4000},
    "tone":   {"label": "按人物口吻", "temperature": 0.70, "max_chars": 2000},
    "custom": {"label": "自定义指令", "temperature": 0.70, "max_chars": 2000},
}

_CTX_WINDOW = 220          # 选区前后各取多少字做上下文
_PERSONA_FALLBACK = "自动识别"


class SelectionAIError(ValueError):
    """入参不合法（未知动作/空选区/超长选区）。"""


def window(text: str, limit: int = _CTX_WINDOW) -> str:
    """取上下文窗口：超长只保留离选区最近的末/头 limit 个字符。"""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= limit:
        return t
    return t[-limit:] if limit > 0 else ""


def window_head(text: str, limit: int = _CTX_WINDOW) -> str:
    """前文窗口：保留最靠近选区的 limit 个字符。"""
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t[:limit] if limit > 0 else ""


def _persona_block(persona: str) -> str:
    """按人物口吻的人物说明：用户指定优先；未指定则要求模型从上下文自行识别。"""
    p = (persona or "").strip()
    if p:
        return f"指定人物：{p}"
    return ("指定人物：未指定——请从上下文判断这段文字的主要说话者/视角人物，"
            "以其身份、年龄、教养与当前情绪说话；判断不出来就保持原语气。")


# ---------------------------------------------------------------- 提示词（自研文案，v5.3 起开放编辑：prompts/selection-ai.md）

# 六动作公共铁则（代码默认值与指纹源；prompts/selection-ai.md 与此处保持逐字一致）
_BASE_PROMPT = ("你是网文写作台的划词助手。用户在正文里选中了一小段文字，你只处理这一段。"
                "输出要求：直接输出处理后的文字成品，不要任何解释、前言、引号包裹或小节标题；"
                "不要输出「以下是」「修改说明」等元信息；保持中文小说的叙事文体。")
# 六动作各自的任务段（{{变量}} 由调用方替换；默认模板与原拼接结果逐字一致）
_TASK_PROMPTS: dict[str, str] = {
    "fix": ("本次任务【纠错】：只改正错别字、用词错误与标点问题（重复标点、"
            "引号方向、顿号逗号混用）；【不动剧情】：不增删句子、不调整语序、不改情节与人物，"
            "原文没有错的字词一律原样保留。"),
    "polish": ("本次任务【润色】：提升这句话的表达——把平庸直白的表述改得更有画面感与"
               "节奏感；【不替用户扩剧情】：不得新增情节、人物、设定与对话轮次，只打磨既有内容的"
               "措辞；长度与原文相当（±20%）。"),
    "expand": ("本次任务【扩写】：在原句骨架上补细节——感官描写、动作分解、"
               "环境反应都可以加，但【不得引入新情节、新人物、新设定】，也不得提前消耗伏笔；"
               "扩写后长度约为原文的 1.5-2.5 倍。"),
    "deslop": ("本次任务【去 AI 味】：用具体的动作、物件、感官细节替换空话套话，"
               "删除模板化的排比与总结句。高频套话清单（出现即优先改写）：{{套话清单}}"
               "。保留情节走向、关键名词与字数量级，不新增情节。"),
    "tone": ("本次任务【按人物口吻】：让这段文字（尤其对白）贴合指定人物的说话方式"
             "——用词、句长、口头禅、语气都向人物靠拢；不改剧情与信息量。\n{{人物说明}}"),
    "custom": ("本次任务【自定义指令】：用户给你的指令如下，严格执行；指令与"
               "「不新增情节/不改小节结构」冲突时以不破坏正文为限，并在改写中体现指令意图。\n"
               "用户指令：{{用户指令}}"),
}

# v5.3.0：登记代码默认值（提示词编辑页的指纹与「默认值已更新」判定来源）
prompts_store.register(
    "selection-ai", "划词润色",
    desc="写作台选中文字后的六动作（纠错/润色/扩写/去AI味/按人物口吻/自定义指令）系统提示词。",
    slots={
        "base": {"default": _BASE_PROMPT, "label": "六动作公共铁则", "vars": [],
                 "desc": "六动作共用的输出铁则：只给成品文本，不带任何元信息。"},
        **{name: {"default": tpl, "label": f"{ACTIONS[name]['label']}·任务段",
                  "temp": ACTIONS[name]["temperature"],
                  "vars": prompts_store.slot_vars(tpl),
                  "desc": f"「{ACTIONS[name]['label']}」动作的专属任务段。"}
           for name, tpl in _TASK_PROMPTS.items()},
    },
)


def system_prompt(action: str, persona: str = "", instruction: str = "") -> str:
    """六动作各一个系统提示词。共约守三条铁则：只输出成品文本、不加任何说明、不破坏纯净域。

    v5.3.0：提示词改为 prompts_store 开放槽（用户层 → 内置层 → 代码默认兜底），
    槽默认值即上方原字符串（兜底与指纹源），组装顺序不变：公共铁则 + 空行 + 任务段。
    """
    if action not in ACTIONS:
        raise SelectionAIError(f"未知动作：{action}")
    base = prompts_store.text("selection-ai", "base", default=_BASE_PROMPT)
    # 先取槽文本（用户层 → 内置层 → 代码默认模板），再替换 {{变量}}——
    # 用户在编辑页改写时保留 {{占位}} chip 也能正常填充。
    task = prompts_store.text("selection-ai", action, default=_TASK_PROMPTS[action])
    if action == "deslop":
        task = prompts_store.fill(task, 套话清单=_SLOP_HINTS)
    elif action == "tone":
        task = prompts_store.fill(task, 人物说明=_persona_block(persona))
    elif action == "custom":
        ins = (instruction or "").strip()
        task = prompts_store.fill(task, 用户指令=ins if ins else "（用户未填写，按通顺润色处理）")
    return base + "\n\n" + task


def build_messages(action: str, selection: str, before: str = "", after: str = "",
                   title: str = "", summary: str = "", persona: str = "",
                   instruction: str = "") -> list[dict]:
    """组装 messages：system（动作铁则）+ user（章题/概要/前后文/选区）。"""
    if action not in ACTIONS:
        raise SelectionAIError(f"未知动作：{action}（可用：{'/'.join(ACTIONS)}）")
    sel = (selection or "").strip()
    if not sel:
        raise SelectionAIError("选区为空，请先在正文里选中一段文字")
    max_chars = ACTIONS[action]["max_chars"]
    if len(sel) > max_chars:
        raise SelectionAIError(f"选区超过 {max_chars} 字，请缩小范围后重试")
    parts = []
    if (title or "").strip():
        parts.append(f"【章题】{title.strip()}")
    summ = re.sub(r"\s+", " ", (summary or "")).strip()
    if summ:
        parts.append(f"【本章概要】{summ[:400]}")
    if (before or "").strip():
        parts.append(f"【选区前文】…{window(before)}")
    if (after or "").strip():
        parts.append(f"【选区后文】{window_head(after)}…")
    parts.append("【选区原文（只处理这一段）】\n" + sel)
    user_p = "\n\n".join(parts)
    return [{"role": "system", "content": system_prompt(action, persona, instruction)},
            {"role": "user", "content": user_p}]


def selection_ai_meta() -> dict:
    """给路由/前端用的动作元信息（label/温度/选区上限）。"""
    return {k: {"label": v["label"], "temperature": v["temperature"], "max_chars": v["max_chars"]}
            for k, v in ACTIONS.items()}


# ---------------------------------------------------------------- 结果清洗

_THINK_RE = re.compile(r"<(?:think|thinking|reasoning)>[\s\S]*?</(?:think|thinking|reasoning)>\s*", re.I)
_THINK_OPEN_RE = re.compile(r"<(?:think|thinking|reasoning)>[\s\S]*$", re.I)
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\n?([\s\S]*?)\n?```$", re.M)
_ACK_RE = re.compile(
    r"^(?:好的|没问题|收到|明白了|以下是|下面是|这是|当然|OK|Okay)[，,：:\s]+"
    r"(?:以下是|下面是|这是)?[^。\n]{0,30}(?:[：:]|\n)\s*")
_WRAP_QUOTE_RE = re.compile(r"^[「『“\"']([\s\S]*?)[」』”\"']$")
_META_LINE_RE = re.compile(r"^(?:修改说明|润色说明|去味说明|扩写说明|说明|备注|评分)\s*[:：]")


def clean_result(text: str) -> str:
    """剥模型杂音（思考标签/围栏/客套语/整体引号包裹），返回可替换进正文的纯文本。

    划词结果是小段正文，不走章节级白名单清洗（那会误伤没有小节结构的短文），
    只做与 purity 同精神的杂音剥离；替换动作发生在前端选区校验之后，双重兜底。
    """
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _THINK_RE.sub("", t)
    t = _THINK_OPEN_RE.sub("", t)
    t = t.strip()
    m = _FENCE_RE.match(t.strip())
    if m:
        t = m.group(1).strip()
    for _ in range(3):
        t2 = _ACK_RE.sub("", t.lstrip())
        if t2 == t:
            break
        t = t2
    # 逐行剥「说明：」类元信息行（模型偶尔把改动说明混进结果）
    lines = [ln for ln in t.split("\n") if ln.strip() and not _META_LINE_RE.match(ln.strip())]
    t = "\n".join(lines).strip()
    q = _WRAP_QUOTE_RE.match(t)
    if q and len(q.group(1).strip()) > 0:
        t = q.group(1).strip()
    return t.strip()


# ---------------------------------------------------------------- 调用封装

def _book_ctx(key: str, chapter_no) -> None:
    """token 记账上下文（key + 章号）；key 为空则不挂账。"""
    from . import llm
    if key:
        llm.book(key)
    try:
        llm.set_chapter(int(chapter_no) if chapter_no else None)
    except (TypeError, ValueError):
        llm.set_chapter(None)


def _temperature_of(action: str) -> float:
    """动作温度：提示词槽温度（用户层 → 内置层）→ ACTIONS 代码默认。"""
    return prompts_store.temperature("selection-ai", action,
                                     default=ACTIONS[action]["temperature"])


def run(action: str, selection: str, *, before: str = "", after: str = "",
        title: str = "", summary: str = "", persona: str = "", instruction: str = "",
        key: str = "", chapter_no=None, cfg=None) -> str:
    """一次性调用：返回清洗后的成品文本（不落稿）。失败抛 LLMError。"""
    from . import llm
    msgs = build_messages(action, selection, before=before, after=after, title=title,
                          summary=summary, persona=persona, instruction=instruction)
    _book_ctx(key, chapter_no)
    out = llm.chat(msgs, temperature=_temperature_of(action), cfg=cfg, stage="review")
    cleaned = clean_result(out)
    if not cleaned:
        raise llm.LLMError("划词结果为空，模型可能没有正常返回，请重试")
    return cleaned


def run_stream(action: str, selection: str, *, on_delta=None, before: str = "",
               after: str = "", title: str = "", summary: str = "", persona: str = "",
               instruction: str = "", key: str = "", chapter_no=None, cfg=None) -> str:
    """流式调用：增量经 on_delta(piece) 实时回传（前端逐字显示），返回清洗后的全文。

    llm.chat_stream 失败会自动回退一次性 chat()，这里只需处理最终清洗与空结果。
    """
    from . import llm
    msgs = build_messages(action, selection, before=before, after=after, title=title,
                          summary=summary, persona=persona, instruction=instruction)
    _book_ctx(key, chapter_no)
    out = llm.chat_stream(msgs, on_delta=on_delta,
                          temperature=_temperature_of(action), cfg=cfg, stage="review")
    cleaned = clean_result(out)
    if not cleaned:
        raise llm.LLMError("划词结果为空，模型可能没有正常返回，请重试")
    return cleaned
