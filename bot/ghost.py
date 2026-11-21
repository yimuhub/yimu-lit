"""幽灵字补全（v5.2.0 AI 辅助写作）：写作台光标后的灰色建议字，Tab 一键采纳。

触发节制在前端（core/ghost.js：末尾判定/随机防抖 1200-1800ms/≥8s 间隔/≥12 字新输入/
60s 冷却）；本模块负责服务端一侧：

· 模式自判——句尾标点收住 → 续一拍（温度 0.8，上限 60 字）；
  句中逗号/顿号 → 补半句（温度 0.3，上限 25 字）。
· 上下文——人物卡（按正文命中挑取，复用 bot/canon 的名字登记能力）+
  场景锚点（末 2 句）+ 前文（末 3 段）。
· 默认关思考——沿用 model_rules/providers 的思考开关机制（deepseek 等供应商的
  extra_body.thinking 由 llm.chat 自动合并）；无从关的供应商按约定用
  max_tokens 收紧 + 提示词强调直接续写兜底。
· 限流——同一本书两次请求间隔 <8s 直接 429 + 剩余冷却秒数（前端据此进入冷却）。
· 清洗——产出必过 purity.strip_fragment + selection_ai.clean_result（纯净域口径），
  再由前端做最终保险，任何元信息小节不得进入正文。

不落稿：返回值只给前端渲染幽灵字，采纳与否由用户决定。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from . import canon, llm, prompts_store, purity, selection_ai
from .paths import DATA_ROOT
from .safeio import safe_join

# ---------------------------------------------------------------- 参数（与前端 core/ghost.js 口径一致）

MIN_INTERVAL_SECS = 8.0        # 同书两次请求最小间隔（请求过快 → 429 + 冷却秒数）
MAX_TEXT_CHARS = 20000         # 提交的正文上限（超长截尾，只影响上下文质量）
# 前端侧 60s 限流冷却窗（连续 429 时）在 core/ghost.js 实现，服务端只管间隔门。

# 模式注册表：句尾标点收住 → 续一拍；句中逗号 → 补半句
MODES: dict[str, dict] = {
    "beat": {"label": "续一拍", "temperature": 0.8, "max_chars": 60},
    "half": {"label": "补半句", "temperature": 0.3, "max_chars": 25},
}

# 句尾标点（含引号/书名号收尾）：收住了，续下一拍
_TAIL_STOP = tuple("。！？…!?”』」》…—")
# 句中标点：话说一半，补半句
_TAIL_MID = tuple("，、；：,:;")

_SCENE = "幽灵字"


class GhostRateLimited(Exception):
    """请求过快：带剩余冷却秒数，路由层转 429。"""

    def __init__(self, cooldown: float):
        super().__init__(f"幽灵字请求过快，请 {cooldown:.0f}s 后再试")
        self.cooldown = max(0.0, float(cooldown))


# ---------------------------------------------------------------- 限流门（按书 key）

_LAST_REQ: dict[str, float] = {}
_RATE_LOCK = threading.Lock()


def check_rate(key: str) -> float:
    """距上次请求不足 MIN_INTERVAL_SECS → 抛 GhostRateLimited（剩余冷却秒数）；
    通过则登记本次时间。路由层在进入补全前调用（每次请求恰好一次）。"""
    now = time.time()
    with _RATE_LOCK:
        last = _LAST_REQ.get(key or "", 0.0)
        remain = last + MIN_INTERVAL_SECS - now
        if remain > 0:
            raise GhostRateLimited(round(remain, 1))
        _LAST_REQ[key or ""] = now
        return 0.0


def reset_rate_for_tests() -> None:
    with _RATE_LOCK:
        _LAST_REQ.clear()


# ---------------------------------------------------------------- 模式自判

def detect_mode(text: str) -> str:
    """按正文末尾的自判：句尾标点收住 → beat（续一拍）；句中逗号等 → half（补半句）。
    其他（无标点结尾，话说一半）→ half 更保守。空白结尾按剥掉后的真末字判断。"""
    t = (text or "").rstrip()
    if not t:
        return "beat"          # 空文/新章：续一拍起步
    ch = t[-1]
    if ch in _TAIL_STOP:
        return "beat"
    if ch in _TAIL_MID:
        return "half"
    return "half"


def mode_of(text: str) -> dict:
    return MODES[detect_mode(text)]


# ---------------------------------------------------------------- 上下文组装

def tail_window(text: str, chars: int = 1200) -> str:
    """末尾上下文窗口：只保留离光标最近的 chars 个字符（后端二次钳制）。"""
    t = (text or "").replace("\r\n", "\n").strip()
    return t[-chars:] if len(t) > chars else t


def last_paragraphs(text: str, n: int = 3) -> str:
    """前文 = 末 n 段（\\n{2,} 分段，与编辑器模型同口径）。"""
    parts = [p.strip() for p in re.split(r"\n{2,}", (text or "")) if p.strip()]
    return "\n\n".join(parts[-n:]) if parts else ""


def last_sentences(text: str, n: int = 2) -> str:
    """场景锚点 = 末 n 句（。！？…！？收束；引号收尾并入上一句）。"""
    t = re.sub(r"\s+", "", (text or ""))
    if not t:
        return ""
    pieces = re.findall(r'[^。！？!?…]*[。！？!?…]+["”』」》]?', t)
    tail = t[sum(len(p) for p in pieces):]   # 末尾没有句号的部分（当前正在写的半句）
    if tail:
        pieces.append(tail)
    return "".join(pieces[-n:])


def _pdir_of(key: str) -> str:
    """书目录解析（防路径穿越）；key 为空或非法返回空串。"""
    if not key:
        return ""
    try:
        pdir = safe_join(os.path.join(DATA_ROOT, "projects"), key, label="书 key")
    except ValueError:
        return ""
    return pdir if os.path.isdir(pdir) else ""


def character_cards(key: str, body_text: str, limit: int = 3, cap: int = 500) -> str:
    """人物卡（按正文命中挑取）：读 00-人物档案.md，用 canon 的名字登记能力取人物名，
    挑「在光标附近正文中出现过」的卡片原文片段，拼成紧凑上下文。
    没有人物档案或没有命中 → 返回空串（不阻塞补全）。"""
    pdir = _pdir_of(key)
    if not pdir:
        return ""
    path = os.path.join(pdir, canon.CHARACTER_FILE)
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    if not raw.strip():
        return ""
    names = canon._names_from_text(raw)
    if not names:
        return ""
    # 命中判定用光标前的正文（尾窗优先，覆盖当前场景出场人物）
    probe = tail_window(body_text, 900)
    hit = [nm for nm in names if nm and nm in probe]
    if not hit:
        return ""
    # 按档案里的出场顺序截取每个人物的卡片段落（从名字行到下一个名字行之前）
    sections = _split_character_sections(raw, names)
    blocks = []
    for nm in hit[:limit]:
        seg = sections.get(nm) or ""
        seg = re.sub(r"\n{2,}", "\n", seg).strip()
        if seg:
            blocks.append(seg[: cap // max(1, limit)])
    return "\n".join(blocks)[:cap]


def _split_character_sections(raw: str, names: list[str]) -> dict[str, str]:
    """把人物档案按登记名切块：名字行（列表项/标题/加粗）到下一个名字行之前。"""
    out: dict[str, str] = {}
    cur, buf = "", []
    name_set = set(names)
    for line in (raw or "").splitlines():
        m = canon._NAME_LINE.match(line.strip())
        if m and m.group(1) in name_set:
            if cur and buf:
                out.setdefault(cur, "\n".join(buf))
            cur, buf = m.group(1), [line.strip()]
        elif cur:
            buf.append(line)
    if cur and buf:
        out.setdefault(cur, "\n".join(buf))
    return out


# ---------------------------------------------------------------- 提示词（自研文案，v5.3 起开放编辑：prompts/ghost.md）

# 公共铁则与两模式任务段（代码默认值与指纹源；prompts/ghost.md 与此处保持逐字一致）
_GHOST_BASE = ("你是网文写作台的幽灵字助手。作者停在正文中，光标后需要一小段建议文字。"
               "输出要求：直接输出续写正文本身，不要任何解释、前言、引号包裹、小节标题；"
               "禁止出现「以下是」「本章概要」「修改说明」等元信息；"
               "禁止以引号或括号收尾包裹整段；保持中文小说的叙事文体与既有语气。")
_GHOST_BEAT = ("本次模式【续一拍】：上一句已收住（句号/叹号/问号），"
               "顺着当前场景自然推进一小拍（一个动作、一句对话或一处细节），"
               "不超过 60 字，不开启新情节、不引入新人物、不总结不升华。")
_GHOST_HALF = ("本次模式【补半句】：上一句停在逗号/顿号处话说一半，"
               "只把当前这半句自然补完（≤25 字），立即停住；不另起一句、不推进情节、"
               "不引入新人物与新设定。")

# v5.3.0：登记代码默认值（提示词编辑页的指纹与「默认值已更新」判定来源）
prompts_store.register(
    "ghost", "幽灵字续写",
    desc="写作台光标后的灰色建议字与快捷流式续写：句尾收住续一拍（beat）、句中补半句（half）。",
    slots={
        "base": {"default": _GHOST_BASE, "label": "公共铁则", "vars": [],
                 "desc": "幽灵字共用输出铁则：只给续写正文本身，不带任何元信息。"},
        "beat": {"default": _GHOST_BEAT, "label": "续一拍·任务段",
                 "temp": MODES["beat"]["temperature"], "vars": [],
                 "desc": "上一句已收住（句号/叹号/问号）时的续写任务段。"},
        "half": {"default": _GHOST_HALF, "label": "补半句·任务段",
                 "temp": MODES["half"]["temperature"], "vars": [],
                 "desc": "上一句停在逗号/顿号处时的补写任务段。"},
    },
)


def _ghost_temperature(mode: str) -> float:
    """模式温度：提示词槽温度（用户层 → 内置层）→ MODES 代码默认。"""
    return prompts_store.temperature("ghost", mode, default=MODES[mode]["temperature"])


def system_prompt(mode: str) -> str:
    """幽灵字系统提示词：只输出成品续写，无解释无小节；模式差异（一拍/半句）。

    v5.3.0：提示词改为 prompts_store 开放槽（用户层 → 内置层 → 代码默认兜底），
    槽默认值即上方原字符串（兜底与指纹源），组装顺序不变：公共铁则 + 空行 + 模式段。
    """
    base = prompts_store.text("ghost", "base", default=_GHOST_BASE)
    if mode == "beat":
        return base + "\n\n" + prompts_store.text("ghost", "beat", default=_GHOST_BEAT)
    return base + "\n\n" + prompts_store.text("ghost", "half", default=_GHOST_HALF)


def build_messages(text: str, *, key: str = "", title: str = "", summary: str = "",
                   mode: str | None = None) -> tuple[list[dict], dict]:
    """组装 messages，返回 (messages, meta)。meta 带自判模式与裁剪后的上下文（供路由层回显）。
    上下文 = 人物卡（按正文命中）+ 场景锚点（末 2 句）+ 前文（末 3 段）。"""
    m = mode if mode in MODES else detect_mode(text)
    parts = []
    cards = character_cards(key, text)
    if (title or "").strip():
        parts.append(f"【章题】{title.strip()}")
    summ = re.sub(r"\s+", " ", (summary or "")).strip()
    if summ:
        parts.append(f"【本章概要】{summ[:300]}")
    if cards:
        parts.append(f"【相关人物卡（说话方式与关系要贴合）】\n{cards}")
    anchor = last_sentences(text, 2)
    if anchor:
        parts.append(f"【场景锚点（最近 2 句，接在此处续写）】{anchor}")
    prev = last_paragraphs(text, 3)
    if prev:
        parts.append(f"【前文（末 3 段，只用于理解语境，勿复述）】\n{prev[-1600:]}")
    if not parts:
        parts.append("【正文】（正文为空，请按章题起笔一小拍）")
    msgs = [{"role": "system", "content": system_prompt(m)},
            {"role": "user", "content": "\n\n".join(parts)}]
    return msgs, {"mode": m, **MODES[m]}


# ---------------------------------------------------------------- 清洗（纯净域口径）

_FRONT_META_RE = re.compile(r"##\s|===|【.*?】$")


def clean_suggestion(text: str, max_chars: int) -> str:
    """幽灵字产出清洗：思考标签/围栏/客套语（selection_ai 同口径）→ 续写片段净化
    （purity.strip_fragment：遇到元信息小节即截断）→ 模式字数上限截断。
    任何元信息小节不得进入正文（铁律 0）。"""
    t = selection_ai.clean_result(text or "")
    t = purity.strip_fragment(t).strip()
    # 首行若直接撞上小节标记/元信息行，整段丢弃（模型把建议写成说明的极端情形）
    first = (t.split("\n", 1)[0] or "").strip()
    if first.startswith(("##", "===", "【")) or _FRONT_META_RE.match(first) or not t:
        return ""
    # 只保留第一段（幽灵字是光标后的一小拍，不跨段）
    t = t.split("\n\n", 1)[0].strip()
    if len(t) > max_chars:
        cut = t[:max_chars]
        # 截在最后一个完整句读处（。！？，、；：），避免半字截断
        stop = max(cut.rfind(ch) for ch in "。！？…，、；：")
        t = cut[: stop + 1] if stop > 0 else cut
    return t.strip()


# ---------------------------------------------------------------- 调用封装

def _tight_max_tokens(mode: str) -> int:
    """max_tokens 收紧：短补全用不到大额度；同时是对「关不掉思考」的供应商的兜底
    （思考链一长就先撞上限，产出被截 → 清洗后为空 → 前端静默放弃本次建议）。"""
    return 96 if mode == "half" else 160


def run(text: str, *, key: str = "", title: str = "", summary: str = "",
        mode: str | None = None, cfg=None, chapter_no=None) -> dict:
    """一次性幽灵字补全。返回 {text, mode, chars, temperature}；失败抛 LLMError。
    限流门由路由层在调用前执行（check_rate），本函数不重复计时。
    记账：llm.book(key) + scene='幽灵字'（账本面板按场景筛选）。"""
    msgs, meta = build_messages(text, key=key, title=title, summary=summary, mode=mode)
    if key:
        llm.book(key)
    try:
        llm.set_chapter(int(chapter_no) if chapter_no else None)
    except (TypeError, ValueError):
        llm.set_chapter(None)
    # 默认关思考：deepseek 等供应商的 extra_body.thinking 开关由 llm.chat 按模型规则
    # 自动合并（model_rules/providers 的思考开关机制）；无开关的供应商靠
    # _tight_max_tokens 收紧 + 提示词「直接输出续写正文」双兜底。
    # v5.3.0：温度走 prompts_store 槽（用户层 → 内置层 → MODES 默认）。
    out = llm.chat(msgs, temperature=_ghost_temperature(meta["mode"]),
                   max_tokens=_tight_max_tokens(meta["mode"]), cfg=cfg, scene=_SCENE)
    cleaned = clean_suggestion(out, meta["max_chars"])
    if not cleaned:
        raise llm.LLMError("幽灵字结果为空，已放弃本次建议")
    return {"text": cleaned, "mode": meta["mode"], "label": meta["label"],
            "chars": len(cleaned), "temperature": meta["temperature"]}


def run_stream(text: str, *, on_delta=None, key: str = "", title: str = "",
               summary: str = "", mode: str | None = None, cfg=None,
               chapter_no=None) -> str:
    """流式续写（快捷续写用）：增量经 on_delta 实时回传，返回清洗后的全文。
    注意：清洗只能对全文做——前端先流式渲染原始增量，done 帧以后端清洗文本为准对账。"""
    msgs, meta = build_messages(text, key=key, title=title, summary=summary, mode=mode)
    if key:
        llm.book(key)
    try:
        llm.set_chapter(int(chapter_no) if chapter_no else None)
    except (TypeError, ValueError):
        llm.set_chapter(None)
    out = llm.chat_stream(msgs, on_delta=on_delta,
                          temperature=0.8,
                          max_tokens=900,      # 续写段比幽灵字长，但仍收紧防思考链拖长
                          cfg=cfg, scene="续写")
    cleaned = purity.strip_fragment(selection_ai.clean_result(out)).strip()
    if not cleaned:
        raise llm.LLMError("续写结果为空")
    return cleaned


def dump_state() -> str:
    """诊断用：限流门当前登记的书数（不暴露正文与时间戳）。"""
    with _RATE_LOCK:
        return json.dumps({"books": len(_LAST_REQ)}, ensure_ascii=False)
