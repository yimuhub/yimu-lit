"""正文纯净域（P0 铁律）——凡是「给读者看 / 给作者复制 / 导出成书」的正文，都必须过本模块。

正文（prose）= 章题（可选）+ 章首引子（可选）+ 正文内容。
除此之外的一切内容都不是正文，一个字符也不许出现在复制/预览/成书/导出里：
  · 规划元信息：本章概要、章节备注、章卡、伏笔清单
  · 生产元信息：修改说明、改写说明、去味说明、修订记录、自检清单
  · 评价元信息：审核意见、评分表、质量报告、字数统计、待改进项
  · 模型杂音：思考标签、代码围栏、「好的，以下是……」开场白、重复章题行

为什么单独成模块：这些内容会经 LLM 重写/去味/修复被写回章节文件，一旦有人用
黑名单（只剥「本章概要/章节备注」）去清洗，任何新增的小节名都会立刻漏进成书。
这里改用**白名单**：只认 `## 章首引子` / `## 正文` 两段，其余 `##` 小节一律丢弃；
再把没有标题的尾巴（评分行、修改说明列表）按噪声特征剥掉。

调用约定（改这里之前先读 功能表.md 的「铁律 0」）：
  · 复制/展示整章      → chapter_text(text)
  · 导出/成书（自带章题）→ chapter_body(text)
  · 写盘前净化结构      → sanitize_chapter(text)
  · 续写片段拼接前      → strip_fragment(text)
本模块不依赖 bot 内任何其它模块（除标准库），可被 writer / export / app / v2api 任意引用。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 基础归一

_BOM = "\ufeff"

_THINK_RE = re.compile(r"<(?:think|thinking|reasoning)>[\s\S]*?</(?:think|thinking|reasoning)>\s*", re.I)
_THINK_OPEN_RE = re.compile(r"<(?:think|thinking|reasoning)>[\s\S]*$", re.I)
_FENCE_RE = re.compile(r"```[a-zA-Z0-9]*\n?([\s\S]*?)\n?```")
# 客套开场白：必须在「好的/以下是/这是」之后紧跟标点或换行，且整句以冒号/换行收尾，
# 否则「这是雨夜。」这类正常正文会被吃掉（v4.22.1 修）。
_ACK_PREFIX_RE = re.compile(
    r"^(?:好的|没问题|收到|明白了|以下是|下面是|这是|当然|OK|Okay)[，,：:\s]+"
    r"(?:以下是|下面是|这是)?[^。\n]{0,30}(?:[：:]|\n)\s*")
_CH_TITLE_LINE_RE = re.compile(r"^#{1,3}\s*第\s*[0-9０-９一二三四五六七八九十百零]+\s*章[^\n]*$")
_CH_TITLE_ANY_RE = re.compile(r"第\s*[0-9０-９一二三四五六七八九十百零]+\s*章")
_HEADER_RE = re.compile(r"^(#{1,6})[ \t]*(.+?)[ \t]*$")
_SEP_LINE_RE = re.compile(r"^\s*(?:[-*_=]\s*){3,}$")
# 残留围栏行（整套内容被包在 ```markdown 里、但只有单边围栏时）
_FENCE_LINE_RE = re.compile(r"(?m)^[ \t]*```[a-zA-Z0-9_+-]*[ \t]*$")

# ── 章题之前的「前言噪音」────────────────────────────────────────────────────
# 模型常把「改写说明 + 修改逻辑清单」甩在章题前面（实测第90章开头整段都是），
# 而它不属于任何小节，靠小节白名单拦不住。规则：**章题之前的一切都不是正文**。
_LEAD_MARK_RE = re.compile(
    r"^[\s【\[（(]*"
    r"(?:这|那)?\s*(?:是|为)?\s*(?:一份|一个|一篇|一份针对)?\s*"
    r"(?:针对|关于|专门为|围绕|基于|按照|按|根据)?\s*"
    r"[《「“\"][^》」”\"]{0,40}[》」”\"]\s*"
    r"[^\n]{0,30}?"
    r"(?:改写|重写|修改|润色|去味|修订|调整|重制)")
_LEAD_HEAD_RE = re.compile(
    r"^[\s【\[（(]*"
    r"(?:(?:主要)?(?:修改|改写|重写|修订|润色|去味|调整|优化)"
    r"(?:逻辑|说明|要点|点|项|思路|原则|清单|方向|策略|方案|步骤|路径)"
    r"|(?:以下|下面|上述)(?:是|为)"
    r"|(?:修改|改写|重写)(?:如下|说明如下))")
# 「这是一份……完整重写版。」这类整段自述（无书名号也常见，第55章实证）
_LEAD_SELF_RE = re.compile(
    r"^[\s【\[（(]*(?:这|那|以下|下面|上面)\s*(?:是|为)?\s*(?:一份|一个|一篇)?[^\n]{0,50}?"
    r"(?:改写|重写|修改|润色|去味|修订|重制)(?:版|稿)?[^\n]{0,20}[。.：:]?$")
_LEAD_ITEM_RE = re.compile(r"^\s*(?:\d{1,2}[.、)）]|[-*•])\s+")

# ---------------------------------------------------------------- 正文契约标记
# v4.22.3：把「正文」变成显式契约区间——生成时要求模型只在标记之间写正文，
# 复制/导出时只取标记之间的内容。标记之外的任何东西（本章概要、章节备注、
# 修改说明、审核评分、字数统计、自检清单）在结构上就不属于正文，不靠猜。
#
#   === 一幕·YIMU 神笔工作台·开篇 ===
#   （章首引子 + 正文）
#   === 一幕·YIMU 神笔工作台·结尾 ===
#
# 容错：允许 ===/---/*** 、#、>、`、【】 等装饰，允许空格与 ·/•/・ 混用，
# 也接受简称「YIMU·开篇」。模型抄错一个字不至于整章失效。
MARK_OPEN = "一幕·YIMU 神笔工作台·开篇"
MARK_CLOSE = "一幕·YIMU 神笔工作台·结尾"

_MARK_NAME = r"一幕\s*[·•・.．]\s*YIMU\s*[·•・.．]?\s*神笔(?:工作台|创作台|创作系统)?\s*[·•・.．]\s*"
_DECOR = r"[\s=\-#*>*`~【\[「(（|:：]*"
_DECOR_END = r"[\s=\-#*<`~】\]」)）|]*"
_MARK_OPEN_RE = re.compile(r"(?m)^" + _DECOR + _MARK_NAME + r"开篇" + _DECOR_END + r"$")
_MARK_CLOSE_RE = re.compile(r"(?m)^" + _DECOR + _MARK_NAME + r"结尾" + _DECOR_END + r"$")
_MARK_ANY_RE = re.compile(r"(?m)^" + _DECOR + _MARK_NAME + r"(?:开篇|结尾)" + _DECOR_END + r"$")


def split_marked(text: str):
    """按契约标记切分正文 → (before, inner, after, ok)。

    ok=False 表示没找到成对标记（旧稿件/模型没照格式写），inner 返回原文。
    只有开标记 → 取其后全部；只有闭标记 → 取其前全部（模型漏写开篇时兜底）。
    """
    t = _norm(text)
    mo = _MARK_OPEN_RE.search(t)
    mc = _MARK_CLOSE_RE.search(t)
    if mo and mc and mc.start() > mo.end():
        return t[:mo.start()], t[mo.end():mc.start()], t[mc.end():], True
    if mo:
        return t[:mo.start()], t[mo.end():], "", True
    if mc:
        return "", t[:mc.start()], t[mc.end():], True
    return "", t, "", False


def _strip_marks(text: str) -> str:
    """去掉残留/重复的契约标记行（保留其内容）。"""
    return _MARK_ANY_RE.sub("", text or "")

# ---------------------------------------------------------------- 小节识别

# 白名单：只有这两类是「正文内容」
_KIND_CONTENT = ("intro", "body")
# 已知的规划元信息（内部工作文件里保留，对外一律剥掉）
_KIND_META = ("summary", "notes")

_INTRO_RE = re.compile(r"^(?:章首|章前)?引子$|^开篇引子$|^章首语$|^题记$")
_BODY_RE = re.compile(r"^(?:本章|全章)?正文(?:内容|部分|全文)?$")
_SUMMARY_RE = re.compile(r"^(?:本章)?(?:概要|摘要)$|^章节摘要$|^内容概要$")
_NOTES_RE = re.compile(r"^(?:本章|章节|作者|创作|下章|写作)?备注$|^写作备注$")

# 噪声小节：名字里带这些词的小节必然是生产/评价元信息，绝不属于正文
_NOISE_WORDS = (
    "修改", "改写", "修订", "去味", "去AI", "AI腔", "润色", "编辑说明", "改动",
    "评分", "得分", "打分", "百分制", "审稿", "审核", "审阅", "评审", "点评", "评估",
    "质量", "体检", "自检", "自查", "检查", "问题", "建议", "意见", "待改进", "待办",
    "字数", "统计", "清单", "报告", "总结", "结论", "说明", "对比", "记录",
    "元信息", "参数", "状态", "版本", "下一步", "流程", "耗时", "token", "Token",
)
_NOISE_HEADER_RE = re.compile("|".join(re.escape(w) for w in _NOISE_WORDS))

# 尾部无标题噪声行（模型常直接甩一段评分/说明，不给小节标题）
_TAIL_NOISE_PREFIX_RE = re.compile(
    r"^[【\[（(*\s]*(?:本章|全章|章节)?\s*"
    r"(?:修改说明|改写说明|修订说明|去味说明|润色说明|编辑说明|调整说明|改动说明|"
    r"重写策略|改写策略|修改策略|修改逻辑|改写逻辑|改动清单|"
    r"评分|得分|总分|综合评分|质量分|质量评分|审稿意见|审核意见|评审意见|点评|"
    r"字数统计|字数|自检|自查|待改进|修改要点|改动要点|说明|备注)"
    r"\s*[】\]）)*]*\s*[:：]")
_TAIL_SCORE_RE = re.compile(
    r"^[【\[（(*\s]*(?:综合|总体|加权)?\s*(?:评分|得分|总分|百分制|质量分)\s*"
    r"(?:为|是)?\s*[:：]?\s*\d{1,3}\s*(?:分|/100|/\s*100)")
_TAIL_TABLE_RE = re.compile(r"^\|.*(?:维度|权重|得分|评分|分值|项目).*\|")
_TAIL_DONE_RE = re.compile(r"^[（(]?\s*本章(?:完|结束|正文完)\s*[)）]?$")
_TAIL_WEIGHT_RE = re.compile(r"^\|\s*(?:结构|因果|人物|连续性|语言|读者体验|节奏|文笔)\s*\|")
# 列表项 / 编号项：跟元信息标记连在一起时属于噪声块的一部分
_LISTISH_RE = re.compile(r"^\s*(?:\d{1,2}[.、)）]|[-*•·])\s+")


def _norm(text: str) -> str:
    t = (text or "").replace(_BOM, "").replace("\r\n", "\n").replace("\r", "\n")
    return t


def _is_sep(line: str) -> bool:
    return bool(_SEP_LINE_RE.match(line or ""))


def _is_noise_header(title: str) -> bool:
    t = re.sub(r"[\s:：、.。·—\-]+", "", title or "")
    return bool(t) and bool(_NOISE_HEADER_RE.search(t))


def _classify(title: str) -> str:
    """小节标题 → summary / intro / body / notes / noise / unknown。"""
    t = re.sub(r"^[\s\d一二三四五六七八九十、.．)）(（]+", "", (title or "").strip())
    t = re.sub(r"[\s:：。.]+$", "", t)
    if _SUMMARY_RE.match(t):
        return "summary"
    if _INTRO_RE.match(t):
        return "intro"
    if _BODY_RE.match(t):
        return "body"
    if _NOTES_RE.match(t):
        return "notes"
    if _is_noise_header(t):
        return "noise"
    return "unknown"


def _split_sections(text: str):
    """把文本切成 (preamble, [(kind, title, level, lines)])；只把 1~2 级标题当结构分节。

    3 级及以下标题默认保留在所属小节内容里（正文内部的小节标记不参与结构判定）；
    但如果 3 级标题**本身就是一个已知小节名**（`### 正文`、`### 本章概要`），那它是
    模型把一个完整章节又嵌套写了一遍（第86章实证），必须当结构分节处理，否则嵌套的
    概要与备注会跟着正文一起流出去。
    """
    preamble: list[str] = []
    secs: list[list] = []
    cur = None
    titled = False
    for line in text.split("\n"):
        m = _HEADER_RE.match(line)
        if m and len(m.group(1)) == 1 and _CH_TITLE_LINE_RE.match(line.strip()):
            if not titled and cur is None:      # 「# 第N章 标题」是章题，不是小节
                preamble.append(line)
                titled = True
                continue
            continue                            # 重复章题行直接丢
        if m and not _is_sep(line):
            _lvl = len(m.group(1))
            _known = _classify(m.group(2).strip()) in ("summary", "intro", "body", "notes")
            if _lvl <= 2 or (_lvl == 3 and _known):
                title, level = m.group(2).strip(), _lvl
                # 「## 章首引子 雨是从傍晚开始下的。」→ 标题 + 首行内容
                kind, rest = _classify(title), ""
                for known, k in ((_SUMMARY_RE, "summary"), (_INTRO_RE, "intro"),
                                 (_BODY_RE, "body"), (_NOTES_RE, "notes")):
                    mm = known.match(title)
                    if mm:
                        kind, rest = k, title[mm.end():].strip(" ：:，,。")
                        break
                cur = [kind, title, level, ([rest] if rest else [])]
                secs.append(cur)
                continue
        if cur is None:
            preamble.append(line)
        else:
            cur[3].append(line)
    return preamble, secs


def _clean_para_head(s: str) -> str:
    """去掉段落开头的分隔线（`---`）——模型常用它把说明段隔开，会让识别落空。"""
    return re.sub(r"^(?:\s*(?:[-*_]\s*){3,}\s*\n)+", "", s or "").strip()


def strip_tail_noise(text: str) -> str:
    """剥掉正文尾部没有小节标题的元信息段（评分/修改说明/字数统计/评分表）。

    v4.22.2：先找「元信息起头段」（`**修改说明**：`、`重写策略：`…），若它之后的
    所有段落都只是列表项/表格/说明，就整块切掉——否则编号列表会让逐段回溯提前收手，
    把半截修改说明留在正文里（第61章实证）。
    """
    paras = re.split(r"\n\s*\n", text or "")
    cut = None
    for i, p in enumerate(paras):
        s = _clean_para_head(p)
        if not s or not (_TAIL_NOISE_PREFIX_RE.match(s) or _TAIL_SCORE_RE.match(s)):
            continue
        rest = [x.strip() for x in paras[i + 1:] if x.strip()]
        if all(_LISTISH_RE.match(x) or _is_tail_noise_para(x) or _TAIL_TABLE_RE.match(x)
               or _is_sep(x) or len(x) <= 160 for x in rest):
            cut = i
            break
    if cut is not None:
        paras = paras[:cut]
    while paras:
        p = paras[-1].strip()
        if not p or _is_sep(p) or _TAIL_DONE_RE.match(p) or _is_tail_noise_para(p):
            paras.pop()
            continue
        break
    return "\n\n".join(x for x in paras if x.strip())


def _is_tail_noise_para(p: str) -> bool:
    p = _clean_para_head(p)
    if not p:
        return True
    if _TAIL_NOISE_PREFIX_RE.match(p) or _TAIL_SCORE_RE.match(p):
        return True
    if _TAIL_TABLE_RE.match(p):
        return True
    lines = [ln for ln in p.split("\n") if ln.strip()]
    if lines and all(_TAIL_WEIGHT_RE.match(ln.strip()) or _TAIL_TABLE_RE.match(ln.strip())
                     or ln.strip().startswith("|") for ln in lines):
        return True
    # 单行小结：「本章 86 分，主要问题：……」
    if len(p) <= 120 and re.search(r"(本章|全章)[^。\n]{0,10}(评分|得分|字数)", p):
        return True
    return False


def _drop_lead_noise(text: str) -> str:
    """剥掉文章开头的「改写说明 + 修改逻辑清单」块（无章题可锚定时用）。

    只吞开头连续的噪声段：命中标记段 → 连同紧随其后的编号/项目列表一起丢；
    一旦遇到正常正文段落立即停止，不会误吃正文。
    """
    paras = re.split(r"\n\s*\n", text or "")
    i, dropped = 0, False
    while i < len(paras):
        p = paras[i].strip()
        if not p:
            i += 1
            continue
        if _LEAD_MARK_RE.match(p) or _LEAD_HEAD_RE.match(p) or _LEAD_SELF_RE.match(p):
            dropped = True
            i += 1
            while i < len(paras) and (not paras[i].strip() or _LEAD_ITEM_RE.match(paras[i].strip())):
                i += 1
            continue
        break
    return "\n\n".join(paras[i:]) if dropped else text


def strip_model_noise(text: str) -> str:
    """剥模型输出杂音：思考标签、代码围栏、客套开场白、章题之前的前言。

    关键规则（v4.22.2）：**章题之前的内容一律不是正文**。
    模型经常把「这是一份针对《第N章 …》的最小改写 / 主要修改逻辑：1. …」整段甩在
    章题前面，它不属于任何 `##` 小节，白名单拦不住——只能靠章题这个锚点切掉。
    """
    t = _norm(text)
    t = _THINK_RE.sub("", t)
    t = _THINK_OPEN_RE.sub("", t)
    t = _FENCE_RE.sub(r"\1", t)
    t = _FENCE_LINE_RE.sub("", t)            # 单边/残留围栏行
    t = _strip_marks(t)                      # 残留的正文契约标记行
    for _ in range(3):                       # 客套开场白可能连着多行
        t2 = _ACK_PREFIX_RE.sub("", t.lstrip())
        if t2 == t:
            break
        t = t2
    lines = t.split("\n")
    ti = next((i for i, ln in enumerate(lines) if _CH_TITLE_LINE_RE.match(ln.strip())), -1)
    if ti > 0:
        lines = lines[ti:]                   # 章题之前的前言/说明整块丢弃
    if lines and _CH_TITLE_LINE_RE.match(lines[0].strip()):
        # 章题只留第一行：正文中间重复出现的题头是模型回显，一律剥掉
        keep = [lines[0]]
        for ln in lines[1:]:
            if _CH_TITLE_LINE_RE.match(ln.strip()):
                continue
            keep.append(ln)
        lines = keep
    t = "\n".join(lines)
    return _drop_lead_noise(t).strip()


def strip_md_style(text: str) -> str:
    """排版净化：去加粗/斜体/行首列表符/反引号/分隔线，只留正常中文标点。

    行首 # 的结构标题不在这里处理——结构判定由 chapter_text/sanitize_chapter 负责。
    """
    t = re.sub(r"\*{1,3}([^*\n]{1,80}?)\*{1,3}", r"\1", text or "")
    t = re.sub(r"(?m)^(\s*)[-*•]\s+", r"\1", t)
    t = re.sub(r"_{1,3}([^_\n]{1,80}?)_{1,3}", r"\1", t)
    t = re.sub(r"`+", "", t)
    t = re.sub(r"(?m)^\s*([-*_]\s*){3,}$", "", t)
    return t


def _join(blocks) -> str:
    out = "\n\n".join(b for b in blocks if b and b.strip())
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def chapter_title(text: str) -> str:
    """取章题（`# 第N章 标题` 的标题部分），没有则空串。"""
    head, _secs = _split_sections(strip_model_noise(text))
    for ln in head:
        s = ln.strip()
        if not s:
            continue
        if _CH_TITLE_LINE_RE.match(s):
            return re.sub(r"^#{1,6}\s*", "", s).strip()
    return ""


def _content_blocks(secs, strict: bool):
    """按白名单挑出正文块：intro + body（strict 时丢弃其它全部小节）。

    同类小节只认第一个：模型偶尔会把整章再嵌套写一遍（`### 正文` 再起一段），
    第二次出现的 正文/引子 属于重复内容，不能拼进来。
    """
    kept = []
    seen = set()
    for kind, _title, _level, lines in secs:
        if kind in _KIND_CONTENT:
            if kind in seen:
                continue
            seen.add(kind)
            kept.append((kind, lines))
        elif kind in _KIND_META or kind == "noise":
            continue
        elif not strict:
            kept.append((kind, lines))
    return kept


def _render(kept, keep_intro: bool = True) -> str:
    parts = []
    for kind, lines in kept:
        if kind == "intro" and not keep_intro:
            continue
        body = "\n".join(lines)
        body = strip_tail_noise(body) if kind == "body" else body.strip("\n")
        if body.strip():
            parts.append(body)
    return _join(parts)


def _cn_len(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def _drop_meta_sections(text: str) -> str:
    """整段删掉元信息小节（概要/备注/说明/评分……），其余原样保留。

    「本章概要」按规范是 300-500 字单段；若它明显超长（>700 汉字），说明模型漏写了
    `## 正文` 头、正文被并进了概要段——此时保留首段之后的内容，宁可多留正文，
    也不整章丢光（防「清洗把书洗没了」）。
    """
    head, secs = _split_sections(text)
    out = list(head)
    for kind, title, level, lines in secs:
        if kind in _KIND_META or kind == "noise":
            if kind == "summary":
                inner = "\n".join(lines).strip("\n")
                if _cn_len(inner) > 700:      # 超规范长度 → 正文被并进了概要段
                    paras = re.split(r"\n\s*\n", inner)
                    out.append("\n\n".join(paras[1:] if len(paras) > 1 else paras))
            continue
        out.append("#" * max(2, level) + " " + title)
        out.extend(lines)
    return "\n".join(out)


def _strip_title_and_tail(raw: str, text: str, keep_intro: bool = True) -> str:
    out = text
    title = chapter_title(raw)
    if title:
        out = re.sub(r"(?m)^#{1,4}\s*" + re.escape(title) + r"\s*$", "", out, count=1)
    if not keep_intro:
        out = re.sub(r"(?m)^#{1,4}\s*(?:章首|章前)?引子\s*$", "", out, count=1)
    return strip_tail_noise(out.strip("\n"))


def chapter_body(text: str, keep_intro: bool = True) -> str:
    """可导出的正文内容：章首引子 + 正文，不含章题与任何小节标记/元信息。"""
    # ① 有契约标记时只取标记之间——标记之外在结构上就不是正文，无需猜测
    _before, _inner, _after, _marked = split_marked(text)
    raw = strip_model_noise(_inner if _marked else text)
    _head, secs = _split_sections(raw)
    kinds = {s[0] for s in secs}
    has_body = "body" in kinds
    has_intro = "intro" in kinds
    if has_body or has_intro:
        # 有正式结构 → 白名单：除 引子/正文 外的小节全部丢弃
        out = _render(_content_blocks(secs, strict=has_body), keep_intro=keep_intro)
    else:
        # 没有结构标记：删掉元信息小节后，整篇当正文
        out = _strip_title_and_tail(raw, _drop_meta_sections(raw), keep_intro)
    out = strip_md_style(out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def chapter_text(text: str, keep_title: bool = True, keep_intro: bool = True,
                 fallback_title: str = "") -> str:
    """可复制/可展示的整章：章题（可选）+ 章首引子 + 正文。

    fallback_title：文件里没有 `# 第N章 …` 题头时的兜底标题（计划里的章名）。
    历史稿件有一批只有 `## 章首引子 / ## 正文`、没有题头，复制出来不该变成无题散文。
    """
    body = chapter_body(text, keep_intro=keep_intro)
    if not keep_title:
        return body
    title = chapter_title(text) or (fallback_title or "").strip()
    if title and body:
        return f"# {title}\n\n{body}"
    if title:
        return f"# {title}"
    return body


def sanitize_chapter(text: str) -> str:
    """写盘前净化：保留内部四段结构（概要/引子/正文/备注），剔除外来小节与尾部噪声。

    章节文件是工作文件，允许带「本章概要 / 章节备注」（供 meta 抽取与回溯）；
    但绝不允许重写/去味/评分产物被写成新的小节永久留在文件里。

    v4.22.3：有正文契约标记时，标记行本身去掉，但**标记之外的内容照旧解析**
    （概要与备注要留在工作文件里，供回溯与 meta 抽取）。
    """
    _before, _inner, _after, _marked = split_marked(text)
    if _marked:
        text = _join([_before, _inner, _after])
    raw = strip_model_noise(text)
    head, secs = _split_sections(raw)
    keep_kinds = ("summary", "intro", "body", "notes")
    kept = []
    _seen = set()
    for s in secs:
        if s[0] not in keep_kinds:
            continue
        if s[0] in _seen:          # 嵌套重复的整章块（第86章）：只留第一次
            continue
        _seen.add(s[0])
        kept.append(s)
    head_text = "\n".join(ln.rstrip() for ln in head).strip("\n")
    if kept and _head_is_noise(head_text):
        # 头部是「改写说明块」：只留章题行，说明整块丢弃（第50章实证）
        head_text = "\n".join(ln for ln in head if _CH_TITLE_LINE_RE.match(ln.strip())).strip("\n")
    if not kept:                       # 没有可保留小节：头部就是全文，尾部噪声要剥
        head_text = strip_tail_noise(head_text)
    out = [head_text] if head_text.strip() else []
    for kind, title, level, lines in kept:
        body = "\n".join(lines).strip("\n")
        if kind in ("body", "notes"):
            # 备注段尾部也常被模型塞「修改说明/评分」——同样按噪声特征剥掉
            body = strip_tail_noise(body)
        out.append("#" * max(2, level) + " " + title)
        if body.strip():
            out.append(body)
    text2 = _join(out)
    return text2 + "\n" if text2 else ""


_FRAGMENT_HEADER_RE = re.compile(r"^(?:#{1,6}\s*)?(?:本章|全章)?(?:正文|续写|接上(?:文|一章)?)(?:内容|部分)?\s*[:：]?\s*$")

# 头部噪声判定用的元信息词（出现 ≥2 个不同词就认定为「改写说明块」）
_META_WORD_RE = re.compile(
    r"改写|重写|修改说明|改写说明|润色|去味|修订|评分|得分|审稿|审核|去重|"
    r"最小改动|最小改写|诊断|修改要点|改动清单")


def _head_is_noise(head_text: str) -> bool:
    """章题之前的那一坨是不是「改写说明块」（而不是正文开头）。

    有章题时判据从严（≥2 个不同元信息词），无章题时从宽（≥1 个）——因为无章题的
    文件若还在开头谈「重写/审查」，那一整块必然是生产说明（第50章实证）。
    """
    s = (head_text or "").strip()
    if not s:
        return True
    body = re.sub(r"(?m)^#{1,3}\s*第\s*\d+\s*章[^\n]*$", "", s).strip()
    has_title = bool(re.search(r"(?m)^#{1,3}\s*第\s*\d+\s*章", s))
    if not body:
        return False                      # 只有章题 → 保留
    if _LEAD_MARK_RE.match(body) or _LEAD_HEAD_RE.match(body) or _LEAD_SELF_RE.match(body):
        return True
    hits = len(set(_META_WORD_RE.findall(body)))
    return hits >= (2 if has_title else 1)


def merge_meta(clean_body: str, old_raw: str) -> str:
    """把旧文件里的「本章概要 / 章节备注」补回纯净正文之后（编辑器只编辑正文时用）。

    编辑器默认只显示正文，用户保存时不能让工作文件丢掉规划元信息；同时旧文件里
    的修改说明/评分等污染也不会被带回来（只回填 summary / notes 两类）。
    """
    raw = strip_model_noise(old_raw or "")
    _head, secs = _split_sections(raw)
    body = (clean_body or "").strip("\n")
    have = {title.strip() for kind, title, _lv, _ln in _split_sections(body)[1]}
    add = []
    for kind, title, level, lines in secs:
        if kind in _KIND_META and title.strip() not in have:
            inner = "\n".join(lines).strip("\n")
            add.append("#" * max(2, level) + " " + title)
            if inner.strip():
                add.append(inner)
    if not add:
        return body + "\n" if body else ""
    return _join([body, "\n\n".join(add)]) + "\n"


def strip_fragment(text: str) -> str:
    """续写片段净化：去客套/思考/围栏/续写小节标题，遇到元信息小节即截断。"""
    t = strip_model_noise(text)
    lines = t.split("\n")
    while lines and (not lines[0].strip() or _FRAGMENT_HEADER_RE.match(lines[0].strip())
                     or _is_sep(lines[0])):
        lines.pop(0)
    kept = []
    for ln in lines:
        m = _HEADER_RE.match(ln)
        if m and len(m.group(1)) <= 2:
            kind = _classify(m.group(2))
            if kind in _KIND_META or kind == "noise":
                break
            if kind in ("body", "intro"):
                continue
        kept.append(ln)
    return _join(kept)


def is_clean(text: str) -> bool:
    """自检：给定文本是否已经不含任何非正文小节（供测试与诊断用）。"""
    _head, secs = _split_sections(strip_model_noise(text))
    return all(s[0] in _KIND_CONTENT or s[0] == "unknown" for s in secs)
