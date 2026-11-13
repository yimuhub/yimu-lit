"""工艺检查（v4.36.0）：确定性、零 token、可复现的文本体检。

定位
----
这是「双层验收」里的第一层（S3-L1）。它只做**能被规则证明**的事：
正则能命中的、算术能算出的、前后文能对上的。凡是需要「品味」的判断，
一律不在这里做——那交给第二层（模型评审 + 证据回查），且第二层必须给出
能 grep 回查的原文引用，否则结论无效。

为什么不能再用「模型打一个分」
------------------------------
真实项目里出现过这些问题，而打分机制一个都没拦住：
  - 第 91 章模型复读，192 段里 151 段完全重复，写到「战马发出一声震耳欲」中断；
  - 第 7 章正文被「修改思路/去味方案/修正后章节」整段覆盖；
  - 第 51 章正文写「第 47 章，她在大牢里」；
  - 章节字数账本与正文脱节，60/100 章对不上。
这些全部是可确定性检出的。检出它们不需要模型，只需要规则 + 一点算术。

每条 finding 都带 `evidence`（原文片段）与 `metric`（度量值），
所以作者能自己回原文核对——**不可证伪的评审结论一律不采纳**。

用法
----
    from bot import craft_check
    rep = craft_check.check_chapter(text, n=91, cfg=cfg)
    rep = craft_check.check_book(chapters, cfg=cfg)   # chapters: [{"n":1,"title":"","text":"..."}]

纯函数、无副作用、不读盘、不调 LLM。
"""
from __future__ import annotations

import re
import statistics

from . import taste, wordcount

# ---------- 阈值（集中在这里，便于回归测试锁定） ----------

TH = {
    "repeat_para_ratio": 0.12,      # 重复段落占比超过 12% 视为复读
    "repeat_run": 6,                # 连续 6 段完全相同视为复读循环
    "burstiness_min": 0.30,         # 句长变异系数低于 0.30 视为过于均匀
    "ai_phrase_per_k": 3.0,         # 每千字模板句超过 3 处
    "para_long_chars": 180,         # 单段超过 180 字算长段
    "para_long_ratio": 0.25,        # 长段占比超过 25%
    "dialogue_min": 0.18,
    "dialogue_max": 0.60,
    "chapter_len_tol": 0.40,        # 章长偏离目标 40%
    "hook_tail_chars": 180,
    "count_error_delta": 2,         # 声称字数与实际差 ≥2 才算穿帮
}

_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

_END_PUNCT = "。！？…\"”』）】—"

# 冲突信号（开篇判定用）：都是具体动作/处境，不是形容词
_CONFLICT_WORDS = (
    "杀", "死", "血", "刀", "剑", "毒", "牢", "斩", "刺", "叛", "陷", "抄家", "灭门",
    "退婚", "休书", "废", "夺", "抢", "逼", "威胁", "跪", "审", "罪", "逃", "追",
    "救命", "危", "急", "滚", "打", "骂", "夺门", "踹", "摔", "砸", "怒", "吼",
)

# 钩子信号（章尾判定用）
_HOOK_WORDS = (
    "忽然", "突然", "就在这时", "没想到", "然而", "却", "直到", "原来", "竟",
    "门外", "身后", "声音", "血", "死", "来人了", "不对劲", "晚了", "来不及",
    "谁", "什么", "为什么", "怎么办",
)

# 爽点/推进信号（节奏判定用）
_PAYOFF_WORDS = (
    "打脸", "反转", "赢", "胜", "得手", "成功", "翻身", "逆袭", "揭穿", "真相",
    "斩杀", "夺回", "反击", "震慑", "服软", "认输", "跪下", "俯首", "败了",
    "原来如此", "终于", "扳倒", "翻盘", "占了上风",
)

# AI 模板句：只收「出现频率显著高于人类写作」的，不没收正常词
_AI_PHRASES = [
    (re.compile(r"不是[^，。！？]{1,24}而是"), "不是……而是……"),
    (re.compile(r"不是因为[^，。]{1,20}而是因为"), "不是因为……而是因为"),
    (re.compile(r"与此同时"), "与此同时"),
    (re.compile(r"值得一提的是"), "值得一提的是"),
    (re.compile(r"毫无疑问"), "毫无疑问"),
    (re.compile(r"显而易见"), "显而易见"),
    (re.compile(r"某种意义上"), "某种意义上"),
    (re.compile(r"不容置疑"), "不容置疑"),
    (re.compile(r"仿佛.{0,6}凝固"), "仿佛……凝固"),
    (re.compile(r"空气.{0,4}凝固"), "空气凝固"),
    (re.compile(r"瞳孔.{0,3}(骤|猛|微)缩"), "瞳孔骤缩"),
    (re.compile(r"嘴角.{0,6}(勾起|扬起).{0,6}(弧度|笑)"), "嘴角勾起一抹"),
    (re.compile(r"心中.{0,4}五味杂陈"), "五味杂陈"),
    (re.compile(r"(如同|宛如|好似)[^，。]{0,8}(惊雷|重锤|冰窖)"), "宛如惊雷/重锤"),
    (re.compile(r"深邃.{0,4}目光|目光.{0,4}深邃"), "深邃目光"),
    (re.compile(r"令人窒息"), "令人窒息"),
]

# 创作层元信息：正文里出现即属泄漏
_META_LEAK_RE = [
    (re.compile(r"第\s*[0-9０-９一二三四五六七八九十百零]+\s*章"), "引用章节号"),
    (re.compile(r"(?:如|见|参考)\s*(?:上|前|后)文"), "引用前后文"),
    (re.compile(r"本章|上一章|前一章|下一章|后面会|前文提到"), "创作层指代"),
]

# 过程产物：改稿工具的输出混进正文
_RESIDUE_RE = [
    (re.compile(r"修改思路|去味方案|修正后章节|改写说明|改稿说明"), "改稿说明"),
    (re.compile(r"评分|得分|权重|维度得分|综合分"), "评分表"),
    (re.compile(r"本章概要|章节备注|内容概要"), "章节元数据"),
    (re.compile(r"字数统计|共\s*\d+\s*字"), "字数统计"),
    (re.compile(r"^\s*\|.*\|", re.M), "表格"),
]

_MD_POLLUTION = [
    (re.compile(r"\*\*[^*\n]+\*\*"), "markdown 加粗"),
    (re.compile(r"(?m)^\s*#{1,6}\s+(?!本章概要|章首引子|正文|章节备注)\S"), "markdown 标题"),
    (re.compile(r"(?m)^\s*[-*•]\s+\S"), "markdown 列表"),
    (re.compile(r"`[^`\n]+`"), "代码标记"),
]

_QUOTE_RE = re.compile(r"“([^”]{1,40})”|「([^」]{1,40})」|\"([^\"]{1,40})\"")

# 数数穿帮只认「引号内容 + 这/那(可省) + 数字 + 个字」这种紧邻句式。
# 之所以收得这么死：`承乾二字` 这类把前文专名回指的说法是**正确**中文，
# 放宽一步就会把它误判成穿帮，而 A 层是阻断级，误报的代价高于漏报。
_COUNT_CLAIM_RE = re.compile(
    r"“([^”]{1,40})”\s*[，。：；、—…]*\s*(?:这|那|共|仅|才|只)?\s*"
    r"([一二三四五六七八九十两\d]{1,3})\s*个?\s*字"
)
# 宽松模式：引号与「N个字」之间允许插入「她在心中默念」这类引导语（≤8 字）。
# 引号内容要求 ≥2 字——单字引用（「沈」字旁）几乎都是「这个字怎么写」的正常用法，
# 一旦放进来就会把大量正常句子误判成穿帮。
_COUNT_CLAIM_LOOSE_RE = re.compile(
    r"“([^”]{2,40})”[^“”。！？\n]{0,8}?([一二三四五六七八九十两\d]{1,3})\s*个?\s*字"
)


# ---------- 工具函数 ----------

def _body(text: str) -> str:
    """只取正文，剔除 概要/备注 等框外内容；失败则原样返回。"""
    t = text or ""
    try:
        from . import purity
        b = purity.chapter_body(t, keep_intro=True)
        return b if b and b.strip() else t
    except Exception:
        return t


def _raw_body(text: str) -> str:
    """取正文但**不做 markdown 清洗**——格式污染检查必须看到原始符号。

    `_body()` 走 purity.chapter_body 会顺手剥掉 `**`、`#`，等于先清洁再体检，
    格式问题永远不会被发现。这里只按小节标题切掉框外的概要/备注。
    """
    lines, skip = [], False
    for ln in (text or "").split("\n"):
        s = ln.strip()
        if re.match(r"^#{1,6}\s*(?:本章概要|章节备注|内容概要|章节摘要)\s*$", s):
            skip = True
            continue
        if skip:
            if re.match(r"^#{1,6}\s*(?:章首引子|正文)\s*$", s):
                skip = False
            else:
                continue
        lines.append(ln)
    return "\n".join(lines)


def _paras(text: str) -> list[str]:
    out = []
    for ln in (text or "").split("\n"):
        s = ln.strip()
        if not s:
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)
        if s:
            out.append(s)
    return out


def _sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？；!?;…]+", text or "")
    return [p.strip() for p in parts if p.strip()]


def _clip(s: str, n: int = 80) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s if len(s) <= n else s[: n - 1] + "…"


def _cn2int(s: str) -> int:
    """把「四」「十二」「二十三」「一百零五」转成整数；失败返回 -1。"""
    s = (s or "").strip()
    if not s:
        return -1
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return -1
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        else:
            return -1
    return total + section + num


def _finding(gate_id, layer, severity, chapter, message, evidence="", metric=None):
    return {
        "gate": gate_id,
        "layer": layer,
        "severity": severity,     # block / warn / info
        "chapter": chapter,
        "message": message,
        "evidence": evidence,
        "metric": metric or {},
    }


# ---------- 单项检查 ----------

def check_truncated(text, n=0):
    """半句截断：正文末尾没有句末标点，且最后一段不像完整收束。"""
    body = _body(text).rstrip()
    if not body:
        return []
    # 去掉可能残留的 markdown 标记再判
    tail = re.sub(r"[#*`>\s]+$", "", body)
    if not tail:
        return []
    last = tail[-1]
    if last in _END_PUNCT:
        return []
    # 「话没说完」的合法结尾：中文引号刚开、对话中断，允许，但只允许出现在最后 30 字
    if last in "，、：；—":
        pass
    last_para = _paras(tail)[-1] if _paras(tail) else tail[-40:]
    return [_finding(
        "C01", "A", "block", n,
        f"章末疑似半句截断：结尾停在「{_clip(tail[-24:], 24)}」，没有句末标点",
        evidence=_clip(last_para, 100),
        metric={"last_char": last, "tail_len": len(tail)})]


def check_repeat(text, n=0):
    """复读循环：重复段落占比过高，或出现连续大段完全重复。"""
    paras = _paras(_body(text))
    if len(paras) < 8:
        return []
    seen, dup = {}, 0
    run = best_run = 1
    prev = None
    for p in paras:
        if p == prev:
            run += 1
            best_run = max(best_run, run)
        else:
            run = 1
        prev = p
        if len(p) >= 12:
            if p in seen:
                dup += 1
            else:
                seen[p] = 1
    ratio = dup / max(1, len(paras))
    if ratio >= TH["repeat_para_ratio"] or best_run >= TH["repeat_run"]:
        return [_finding(
            "C02", "A", "block", n,
            f"疑似复读循环：{len(paras)} 段中 {dup} 段重复（{ratio:.0%}），最长连续重复 {best_run} 段",
            evidence=_clip(next((p for p in paras if paras.count(p) > 1), ""), 80),
            metric={"paras": len(paras), "dup": dup, "ratio": round(ratio, 3),
                    "max_run": best_run})]
    return []


def check_meta_leak(text, n=0):
    """元信息泄漏：正文里出现章节号、「本章」这类创作层指代。"""
    body = _body(text)
    out = []
    for rx, label in _META_LEAK_RE:
        m = rx.search(body)
        if m:
            # 「第X章」出现在引号内的对白里也可能误报，取上下文判断
            s = max(0, m.start() - 30)
            ctx = body[s:m.end() + 30]
            out.append(_finding(
                "C03", "A", "block", n,
                f"元信息泄漏（{label}）：正文出现「{m.group(0)}」",
                evidence=_clip(ctx, 90),
                metric={"hit": m.group(0)}))
            break
    return out


def check_process_residue(text, n=0):
    """过程产物残留：改稿说明/评分表/章节元数据混进正文。"""
    body = _body(text)
    for rx, label in _RESIDUE_RE:
        m = rx.search(body)
        if m:
            s = max(0, m.start() - 20)
            return [_finding(
                "C04", "A", "block", n,
                f"过程产物残留（{label}）：正文出现「{m.group(0)[:20]}」",
                evidence=_clip(body[s:m.end() + 40], 90),
                metric={"hit": label})]
    return []


def check_count_error(text, n=0):
    """数数错误：说「四个字」但引号里不是四个字——AI 分词导致的典型穿帮。

    只认紧邻句式（引号 → 这/那 → 数字 → 个字），并且跳过三类合法写法：
      * 引号内含省略号（`"替……长孙家。" 这两个字`）——缩略引用，不能直接数字数；
      * 引号内含标点占位；
      * 声称数与实际差 < 2（±1 在中文里常是「含不含标点」的口径差）。
    """
    body = _body(text)
    out = []
    seen = set()
    for rx in (_COUNT_CLAIM_RE, _COUNT_CLAIM_LOOSE_RE):
        for m in rx.finditer(body):
            if m.start() in seen:
                continue
            seen.add(m.start())
            inner = m.group(1) or ""
            claimed = _cn2int(m.group(2))
            if claimed < 2 or claimed > 20:
                continue
            if "…" in inner or "..." in inner:      # 缩略引用不算穿帮
                continue
            actual = len(re.findall(r"[\u4e00-\u9fff]", inner))
            if actual < 2 or actual > 20:
                continue
            if abs(actual - claimed) >= TH["count_error_delta"]:
                out.append(_finding(
                    "C05", "A", "block", n,
                    f"数数穿帮：文中说「{claimed}个字」，引号内「{_clip(inner, 16)}」实际 {actual} 个汉字",
                    evidence=_clip(body[max(0, m.start() - 24):m.end() + 4], 90),
                    metric={"claimed": claimed, "actual": actual}))
                if len(out) >= 3:
                    return out
    return out


def check_format_pollution(text, n=0):
    """格式污染：正文出现 markdown 符号/代码块/表格/英文残留。

    `>` 单独处理：章首题记用 `> 引文` 是常见排版习惯，只在**正文中后段**
    出现且数量超过 2 行时才算污染——否则会把刻意设计的题记误判成格式错误。
    """
    body = _raw_body(text)
    for rx, label in _MD_POLLUTION:
        m = rx.search(body)
        if m:
            return [_finding(
                "C06", "A", "block", n,
                f"格式污染（{label}）：正文出现「{m.group(0)[:16]}」",
                evidence=_clip(body[max(0, m.start() - 20):m.end() + 20], 80),
                metric={"hit": label})]
    lines = body.split("\n")
    quote_rows = [(i, ln) for i, ln in enumerate(lines) if re.match(r"^\s*>\s+\S", ln)]
    if len(quote_rows) > 2:
        bad = [(i, ln) for i, ln in quote_rows if i > 3]
        if bad:
            i, ln = bad[0]
            return [_finding(
                "C06", "A", "block", n,
                f"格式污染（引用块）：正文中段出现 {len(quote_rows)} 行 `>` 引用",
                evidence=_clip(ln, 80),
                metric={"hit": "引用块", "rows": len(quote_rows)})]
    # 英文残留：连续 3 个以上英文单词（专有名词容忍 2 个）
    words = re.findall(r"\b[A-Za-z]{3,}\b", body)
    if len(words) > 8:
        return [_finding(
            "C06", "A", "block", n,
            f"英文残留偏多：正文出现 {len(words)} 个英文词",
            evidence=_clip("、".join(words[:8]), 80),
            metric={"count": len(words)})]
    return []


def check_opening(text, n=0, cfg=None):
    """开篇要素：前 N 字内是否出现冲突与主角处境。"""
    sp = taste.spec(taste.form_of(cfg), cfg)
    need = int((sp.get("opening") or {}).get("hook_within_chars", 500))
    body = _body(text)
    head = body[:need]
    if len(head) < 100:
        return []
    conflict = [w for w in _CONFLICT_WORDS if w in head]
    has_dialogue = bool(re.search(r"[“「\"]", head))
    if conflict or has_dialogue:
        return []
    # 纯环境/设定铺陈的进一步证据：首段无人物动作、无对话
    first = _paras(head)
    first = first[0] if first else head
    return [_finding(
        "C10", "B", "warn", n,
        f"开篇前 {need} 字内没有可识别的冲突或对话，疑似大段铺垫",
        evidence=_clip(first, 90),
        metric={"scanned": len(head), "conflict_hits": len(conflict)})]


def check_hook_tail(text, n=0, cfg=None):
    """章尾钩子（仅连载型强制）：结尾需有悬念/转折/未完成动作。"""
    form = taste.form_of(cfg)
    sp = taste.spec(form, cfg)
    if not (sp.get("rhythm") or {}).get("hook_every_chapter"):
        return []
    body = _body(text).rstrip()
    if not body:
        return []
    tail = body[-TH["hook_tail_chars"]:]
    signals = []
    if "？" in tail or "?" in tail:
        signals.append("问句")
    if "……" in tail or "..." in tail:
        signals.append("省略号")
    if any(w in tail for w in _HOOK_WORDS):
        signals.append("转折词")
    paras = _paras(tail)
    if paras and len(paras[-1]) <= 18:
        signals.append("短句断章")
    if len(signals) >= 1:
        return []
    return [_finding(
        "C11", "B", "warn", n,
        "章尾没有可识别的钩子（无问句/省略/转折/短句断章），连载读者缺少点下一章的理由",
        evidence=_clip(tail[-100:], 100),
        metric={"signals": signals})]


def check_chapter_len(text, n=0, cfg=None):
    """章长：偏离目标字数 ±40% 报警。"""
    body = _body(text)
    w = wordcount.count(body)
    target = taste.chapter_words_of(cfg)
    lo = int(target * (1 - TH["chapter_len_tol"]))
    hi = int(target * (1 + TH["chapter_len_tol"]))
    if w < lo or w > hi:
        kind = "过短（信息量可能不足）" if w < lo else "过长（考虑拆章或压缩）"
        return [_finding(
            "C12", "B", "warn", n,
            f"章长{kind}：{w} 字，目标 {target} 字（区间 {lo}–{hi}）",
            evidence="", metric={"words": w, "target": target})]
    return []


def check_dialogue_ratio(text, n=0):
    """对话占比：过低时叙述压死节奏，过高时缺少画面与内心。

    分两档：<10% 记 warn（结构问题，读者会觉得「一直在讲，没在演」），
    10%~15% 记 info（偏叙述，提示而不打扰）。本文类中位约 15%~30%，
    所以 15% 以下才提示，避免把正常的叙述型历史文误判成缺陷。
    """
    body = _body(text)
    total = len(re.findall(r"[\u4e00-\u9fff]", body))
    if total < 300:
        return []
    dlg = sum(len(re.findall(r"[\u4e00-\u9fff]", g))
              for m in _QUOTE_RE.finditer(body) for g in m.groups() if g)
    ratio = dlg / total
    if ratio < 0.10:
        return [_finding(
            "C13", "B", "warn", n,
            f"对话占比严重偏低：{ratio:.0%}（建议 ≥15%），叙述压过场景，读者缺少「在演」的感觉",
            evidence="", metric={"ratio": round(ratio, 3)})]
    if ratio < 0.15:
        return [_finding(
            "C13", "B", "info", n,
            f"对话占比偏低：{ratio:.0%}（同类作品中位约 20%~30%）",
            evidence="", metric={"ratio": round(ratio, 3)})]
    if ratio > TH["dialogue_max"]:
        return [_finding(
            "C13", "B", "info", n,
            f"对话占比偏高：{ratio:.0%}（建议 ≤{TH['dialogue_max']:.0%}），画面与内心可能不足",
            evidence="", metric={"ratio": round(ratio, 3)})]
    return []


def check_burstiness(text, n=0):
    """句长节律：变异系数过低读起来像说明书。"""
    body = _body(text)
    sents = _sentences(body)
    if len(sents) < 40:
        return []
    lens = [len(re.findall(r"[\u4e00-\u9fff]", s)) for s in sents]
    lens = [x for x in lens if x > 0]
    if len(lens) < 40:
        return []
    mean = statistics.mean(lens)
    if mean <= 0:
        return []
    cv = statistics.pstdev(lens) / mean
    if cv < TH["burstiness_min"]:
        return [_finding(
            "C20", "C", "info", n,
            f"句长过于均匀：变异系数 {cv:.2f}（<{TH['burstiness_min']}），节律接近模板",
            evidence=_clip(sents[0], 60),
            metric={"cv": round(cv, 3), "mean_len": round(mean, 1)})]
    return []


def check_ai_phrases(text, n=0):
    """AI 模板句密度：每千字命中数。"""
    body = _body(text)
    w = max(1, wordcount.count(body))
    hits, samples = [], []
    for rx, label in _AI_PHRASES:
        for m in rx.finditer(body):
            hits.append(label)
            if len(samples) < 4:
                s = max(0, m.start() - 12)
                samples.append(_clip(body[s:m.end() + 12], 50))
    per_k = len(hits) * 1000.0 / w
    if per_k >= TH["ai_phrase_per_k"]:
        uniq = sorted(set(hits))
        return [_finding(
            "C21", "C", "info", n,
            f"AI 模板句密度偏高：每千字 {per_k:.1f} 处（阈值 {TH['ai_phrase_per_k']}），"
            f"命中 {len(uniq)} 类",
            evidence=" ｜ ".join(samples),
            metric={"per_k": round(per_k, 2), "labels": uniq[:6], "count": len(hits)})]
    return []


def check_paragraph_len(text, n=0):
    """段落长度：手机阅读长段会拉低完读率。"""
    paras = _paras(_body(text))
    if len(paras) < 5:
        return []
    longs = [p for p in paras if len(p) > TH["para_long_chars"]]
    ratio = len(longs) / len(paras)
    if ratio > TH["para_long_ratio"]:
        return [_finding(
            "C22", "C", "info", n,
            f"长段占比过高：{len(longs)}/{len(paras)} 段超过 {TH['para_long_chars']} 字（{ratio:.0%}）",
            evidence=_clip(longs[0], 80) if longs else "",
            metric={"ratio": round(ratio, 3)})]
    return []


def check_desc_heavy(text, n=0):
    """描写压过事件：环境/心理段占比过高。"""
    paras = _paras(_body(text))
    if len(paras) < 8:
        return []
    scene_words = ("光", "影", "风", "雨", "雪", "云", "月", "天色", "空气", "气味",
                   "声音", "冰冷", "温暖", "寂静", "喧嚣")
    desc = 0
    for p in paras:
        has_dlg = bool(re.search(r"[“「\"]", p))
        has_act = any(w in p for w in ("走", "推", "拉", "抓", "站", "坐", "转", "抬", "落", "冲", "踢", "握"))
        senses = sum(1 for w in scene_words if w in p)
        if not has_dlg and not has_act and senses >= 2:
            desc += 1
    ratio = desc / len(paras)
    if ratio > 0.35:
        return [_finding(
            "C23", "C", "info", n,
            f"描写压过事件：{desc}/{len(paras)} 段是无动作无对白的纯描写（{ratio:.0%}）",
            evidence="", metric={"ratio": round(ratio, 3)})]
    return []


def payoff_signals(text) -> int:
    """爽点/推进信号计数——供节奏判定复用。"""
    body = _body(text)
    return sum(1 for w in _PAYOFF_WORDS if w in body)


# ---------- 章节级 ----------

_CHAPTER_CHECKS = (
    check_truncated, check_repeat, check_meta_leak, check_process_residue,
    check_count_error, check_format_pollution, check_opening, check_hook_tail,
    check_chapter_len, check_dialogue_ratio, check_burstiness, check_ai_phrases,
    check_paragraph_len, check_desc_heavy,
)


def check_chapter(text, n=0, cfg=None) -> list:
    """跑一章的全部检查；任何单项异常都不影响其他项（体检不能因一项崩掉）。

    开篇检查只对「黄金章」跑（连载 3 章 / 出版 1 章）——第 80 章的前 500 字
    本来就不需要抛冲突，对全书跑会产出大量无意义告警。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    golden = int((taste.spec(taste.form_of(cfg), cfg).get("opening") or {})
                 .get("golden_chapters", 3))
    checks = _CHAPTER_CHECKS if (not n or n <= golden) else tuple(
        f for f in _CHAPTER_CHECKS if f is not check_opening)
    out = []
    for fn in checks:
        try:
            if fn in (check_opening, check_hook_tail, check_chapter_len):
                out.extend(fn(text, n=n, cfg=cfg))
            else:
                out.extend(fn(text, n=n))
        except Exception:
            continue
    return out


# ---------- 全书级 ----------

# 只跑 A 层（硬伤）的检查集合——交付闸门用这一组，不需要形态配置
_HARD_CHECKS = (
    check_truncated, check_repeat, check_meta_leak, check_process_residue,
    check_count_error, check_format_pollution,
)


def hard_findings(text, n=0) -> list:
    """只跑硬伤层检查（交付闸门用）。

    与 `check_chapter` 分开的原因：交付闸门拿不到本书 cfg，而 B/C 层判定
    依赖形态（连载要钩子、出版不要）。A 层是形态无关的，跑它最安全。
    """
    out = []
    for fn in _HARD_CHECKS:
        try:
            out.extend(fn(text, n=n))
        except Exception:
            continue
    return out


def check_flat_streak(chapters, cfg=None) -> list:
    """连续平淡章：连载型连续 N 章没有爽点/推进信号。"""
    form = taste.form_of(cfg)
    sp = taste.spec(form, cfg)
    limit = int((sp.get("rhythm") or {}).get("max_flat_chapters", 2))
    streak, out = [], []
    for ch in chapters:
        n = int(ch.get("n") or 0)
        if payoff_signals(ch.get("text") or "") == 0:
            streak.append(n)
        else:
            if len(streak) > limit:
                out.append(_finding(
                    "C15", "B", "warn", streak[-1],
                    f"连续 {len(streak)} 章无可识别的爽点或推进信号：第 {streak[0]}–{streak[-1]} 章",
                    evidence="", metric={"range": [streak[0], streak[-1]]}))
            streak = []
    if len(streak) > limit:
        out.append(_finding(
            "C15", "B", "warn", streak[-1],
            f"结尾连续 {len(streak)} 章无爽点或推进信号：第 {streak[0]}–{streak[-1]} 章",
            evidence="", metric={"range": [streak[0], streak[-1]]}))
    return out


def check_ending(chapters, cfg=None) -> list:
    """结局闭合（仅出版型）：末章要有收束信号。"""
    form = taste.form_of(cfg)
    sp = taste.spec(form, cfg)
    if not (sp.get("ending") or {}).get("closed"):
        return []
    if not chapters:
        return []
    last = chapters[-1]
    body = _body(last.get("text") or "")
    if not body:
        return []
    tail = body[-600:]
    closed_words = ("后来", "从此", "多年后", "终于", "落幕", "尘埃落定", "再也没有",
                    "此后", "归于", "安葬", "离开", "结束了", "落幕了")
    if any(w in tail for w in closed_words):
        return []
    # 留了明显续作钩（末句是悬念问句）同样算未闭合
    return [_finding(
        "C14", "B", "warn", int(last.get("n") or 0),
        "出版型结局未见收束：末章结尾没有给出主线落点（无「后来/终于/落幕」类收束信号）",
        evidence=_clip(tail[-100:], 100),
        metric={"tail_has_question": "？" in tail})]


def check_book(chapters, cfg=None) -> dict:
    """全书体检：逐章跑检查 + 跨章节奏/结局，输出分级汇总与可回查证据。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    form = taste.form_of(cfg)
    findings = []
    for ch in chapters or []:
        try:
            findings.extend(check_chapter(ch.get("text") or "", n=int(ch.get("n") or 0), cfg=cfg))
        except Exception:
            continue
    try:
        findings.extend(check_flat_streak(chapters or [], cfg))
        findings.extend(check_ending(chapters or [], cfg))
    except Exception:
        pass

    def _sev(s):
        return [f for f in findings if f["severity"] == s]

    blocks = _sev("block")
    warns = _sev("warn")
    infos = _sev("info")
    # 按闸门聚合，便于「先修哪一类」
    by_gate = {}
    for f in findings:
        g = by_gate.setdefault(f["gate"], {
            "gate": f["gate"], "layer": f["layer"], "severity": f["severity"],
            "name": (taste.gate(f["gate"]) or {}).get("name", f["gate"]),
            "chapters": [], "count": 0, "message": f["message"],
        })
        g["count"] += 1
        if f["chapter"] and f["chapter"] not in g["chapters"]:
            g["chapters"].append(f["chapter"])
        if f["severity"] == "block":
            g["severity"] = "block"
    gates = sorted(by_gate.values(), key=lambda x: ({"block": 0, "warn": 1, "info": 2}[x["severity"]],
                                                    -x["count"]))
    for g in gates:
        g["chapters"].sort()

    return {
        "form": form,
        "form_label": taste.FORMS[form]["label"],
        "chapters": len(chapters or []),
        "words": sum(wordcount.count(c.get("text") or "") for c in (chapters or [])),
        "findings": findings,
        "gates": gates,
        "counts": {"block": len(blocks), "warn": len(warns), "info": len(infos)},
        "pass_a": len(blocks) == 0,
        "blocked_chapters": sorted({f["chapter"] for f in blocks if f["chapter"]}),
        "warn_chapters": sorted({f["chapter"] for f in warns if f["chapter"]}),
    }


def render_report(rep: dict, cap: int = 30) -> str:
    """把体检结果渲染成作者能读的 Markdown（每条结论都带原文证据）。"""
    if not isinstance(rep, dict):
        return ""
    c = rep.get("counts") or {}
    L = [f"# 工艺体检 · {rep.get('form_label', '')}", ""]
    L.append(f"- 章节 {rep.get('chapters', 0)} · 字数 {rep.get('words', 0)}")
    L.append(f"- 硬伤 **{c.get('block', 0)}** 项 · 结构告警 {c.get('warn', 0)} 项 · 体验提示 {c.get('info', 0)} 项")
    L.append(f"- 判定：{'硬伤清零 ✅' if rep.get('pass_a') else '**存在硬伤，不可交付** ❌'}")
    if rep.get("blocked_chapters"):
        L.append(f"- 需处理章节：第 {', '.join(str(x) for x in rep['blocked_chapters'][:40])} 章")
    L.append("")
    L.append("## 按类型排序（先修上层）")
    L.append("")
    L.append("| 闸门 | 层级 | 项数 | 涉及章 | 说明 |")
    L.append("|---|---|---|---|---|")
    for g in (rep.get("gates") or [])[:cap]:
        chs = g.get("chapters") or []
        chs_txt = ("第" + "、".join(str(x) for x in chs[:10]) + "章") if chs else "全书"
        if len(chs) > 10:
            chs_txt += f" 等 {len(chs)} 章"
        L.append(f"| {g['gate']} {g['name']} | {taste.LAYER_LABEL.get(g['layer'], g['layer'])} "
                 f"| {g['count']} | {chs_txt} | {_clip(g['message'], 60)} |")
    L.append("")
    L.append("## 硬伤明细（每条都可回原文核对）")
    blocks = [f for f in (rep.get("findings") or []) if f["severity"] == "block"]
    if not blocks:
        L.append("无。")
    else:
        for f in blocks[:cap]:
            L.append(f"- **第{f['chapter']}章 · {f['gate']}** {f['message']}")
            if f.get("evidence"):
                L.append(f"  > {f['evidence']}")
    return "\n".join(L)


# ---------- 确定性自愈 ----------
# 只修「规则能证明、修完不会更差」的东西。凡是需要重写句子的，一律返回原样留给模型。
# 这一层的价值：复读、元信息泄漏、格式污染、数数穿帮这四类占了真实事故的绝大多数，
# 而它们全都能零 token 修掉——不必再为此跑一轮昂贵的整章重写。

def _int2cn(n: int) -> str:
    digs = "零一二三四五六七八九"
    if n < 0:
        return str(n)
    if n < 10:
        return digs[n]
    if n < 20:
        return "十" + (digs[n % 10] if n % 10 else "")
    if n < 100:
        return digs[n // 10] + "十" + (digs[n % 10] if n % 10 else "")
    return str(n)


def _dedupe_paragraphs(text: str) -> str:
    """删除完全重复的段落（保留首次出现），只对 ≥12 字的整段生效。"""
    seen = set()
    out = []
    for ln in text.split("\n"):
        key = ln.strip()
        if len(key) >= 12:
            if key in seen:
                continue
            seen.add(key)
        out.append(ln)
    return "\n".join(out)


def _strip_meta_refs(text: str) -> str:
    """把正文里的创作层指代换成叙事内表述。"""
    t = re.sub(r"第\s*[0-9０-９一二三四五六七八九十百零]+\s*章", "先前", text)
    t = re.sub(r"(?:如|见|参考)\s*(?:上|前|后)文", "前面的事", t)
    t = re.sub(r"上一章|前一章", "先前", t)
    t = re.sub(r"下一章", "之后", t)
    t = re.sub(r"本章", "眼下这一回", t)
    return t


def _fix_count_claims(text: str) -> str:
    """把「N个字」改成引号内实际的字数。"""
    def _rep(m):
        inner = m.group(1) or ""
        if "…" in inner or "..." in inner:
            return m.group(0)
        actual = len(re.findall(r"[\u4e00-\u9fff]", inner))
        if actual < 2 or actual > 20:
            return m.group(0)
        return m.group(0)[:m.start(2) - m.start(0)] + _int2cn(actual) + m.group(0)[m.end(2) - m.start(0):]
    for rx in (_COUNT_CLAIM_RE, _COUNT_CLAIM_LOOSE_RE):
        text = rx.sub(_rep, text)
    return text


def autofix(text: str, findings=None) -> tuple:
    """按硬伤类型做确定性修复，返回 (新文本, 已修复项列表)。

    只处理四类：复读、元信息泄漏、数数穿帮、格式污染。
    半句截断（C01）**不在这里修**——补什么句子属于创作，必须回到模型。
    """
    body = text or ""
    gates = {f.get("gate") for f in (findings or []) if f.get("severity") == "block"}
    applied = []
    if "C02" in gates:
        new = _dedupe_paragraphs(body)
        if new != body:
            body = new
            applied.append("复读去重")
    if "C03" in gates:
        new = _strip_meta_refs(body)
        if new != body:
            body = new
            applied.append("元信息改写")
    if "C05" in gates:
        new = _fix_count_claims(body)
        if new != body:
            body = new
            applied.append("数数校正")
    if "C06" in gates:
        try:
            from . import purity
            new = purity.strip_md_style(body)
            if new != body:
                body = new
                applied.append("格式清洗")
        except Exception:
            pass
    return body, applied
