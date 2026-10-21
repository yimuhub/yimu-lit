"""反 AI 腔检测器（v1.0.0 · 神笔文学创作台）

纯标准库实现，零依赖。对文本做**确定性**的 AI 味扫描。

**两级判定**（不装确定）：
  blocking  —— 明确该改：套路词、总结句、元话语、空洞权威、机械排比
  advisory  —— 需要人判断：电报体、机械重复、句长均一、抽象词堆叠

**重要**：脚本命中只是**定位线索**，零命中不等于文本没问题。
本检测器不替代 bot/skillkit 的三层人工/模型扫描（表达 / 用笔 / 故事）。

数据：词表为本项目自建（不引用任何第三方词表原文）。
"""
from __future__ import annotations

import re
from collections import Counter

# ---------- 词表（自建） ----------

# 意义膨胀：把普通事说成大事
INFLATED = [
    "至关重要", "不可或缺", "具有里程碑意义", "意义重大", "举足轻重",
    "至关重要", "深刻意义", "深远影响", "划时代", "无可比拟",
    "重要举措", "关键作用", "核心价值", "战略高度",
]

# 模糊主体：把具体的人换成泛泛的群体
VAGUE_AUTHORITY = [
    "研究表明", "专家表示", "学者认为", "普遍认为", "众所周知",
    "业内人士", "权威人士", "数据显示", "有数据显示", "大量研究",
    "社会普遍", "人们普遍", "大家都认为",
]

# 空洞过渡：连接词本身不承载内容
EMPTY_TRANSITION = [
    "不仅仅是", "不只是", "更是", "让我们一起", "让我们", "接下来我们来",
    "总而言之", "综上所述", "总的来说", "一言以蔽之", "归根结底",
    "在当今社会", "在当今时代", "随着科技的", "随着时代的发展",
    "众所周知", "毋庸置疑", "不可否认",
]

# 元话语：谈论文章本身而不是谈内容
META = [
    "本文将", "本文主要", "本篇文章", "接下来我们来探讨", "希望本文",
    "下面我们来", "我们先来看", "接下来我们来看", "以上就是",
    "相信大家看完", "欢迎在评论区", "欢迎留言讨论",
]

# 空洞形容 / 名词化
HOLLOW_ADJ = [
    "显著", "深刻", "生动", "丰富", "充分", "全面", "有效", "重要",
    "极大", "深刻地", "充分地", "有效地", "极大地",
]

# 套话开场
CLICHE_OPEN = [
    "在当今这个", "在当今这个时代", "随着时代的发展", "随着社会的发展",
    "在这个日新月异的时代", "在物欲横流的今天", "在这个钢筋水泥的城市",
    "在人工智能迅猛发展的今天",
]

# 套话结尾
CLICHE_END = [
    "让我们拭目以待", "我们拭目以待", "让我们一起期待", "相信未来会更好",
    "未来可期", "任重道远", "长路漫漫", "且行且珍惜",
    "这就是生活的意义", "愿每一份努力都被看见",
]

_ALL_LISTS = {
    "意义膨胀": INFLATED,
    "模糊权威": VAGUE_AUTHORITY,
    "空洞过渡": EMPTY_TRANSITION,
    "元话语": META,
    "空洞形容": HOLLOW_ADJ,
    "套话开场": CLICHE_OPEN,
    "套话结尾": CLICHE_END,
}

# ---------- 句法特征 ----------

# 抽象名词（AI 文的高频特征：把过程抽成名词）
_ABSTRACT_NOUNS = [
    "赋能", "抓手", "闭环", "生态", "势能", "壁垒", "护城河",
    "颗粒度", "对齐", "复盘", "打法", "组合拳", "顶层设计",
    "底层逻辑", "顶层逻辑", "抓手", "组合", "维度", "框架",
    "体系化", "生态化", "数字化转型", "协同效应", "价值主张",
]

# 中文数字 + 抽象名词的组合（典型 AI 句式）
_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\n]+")
_CLAUSE_SPLIT = re.compile(r"[，,、：:]")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s.strip()]


def _clauses(sent: str) -> list[str]:
    return [c.strip() for c in _CLAUSE_SPLIT.split(sent) if c.strip()]


def _cn_len(s: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", s))


# ---------- 检测 ----------

def scan(text: str, min_len: int = 40) -> dict:
    """扫描文本的 AI 味。返回报告 dict。

    报告结构：
      {verdict, findings[], stats, summary}
    verdict ∈ pass | review
    """
    findings: list[dict] = []
    txt = text or ""

    # ---- 词表命中 ----
    for cat, words in _ALL_LISTS.items():
        for w in words:
            start = 0
            while True:
                i = txt.find(w, start)
                if i < 0:
                    break
                # 定位到所在句
                line = txt.count("\n", 0, i) + 1
                findings.append({
                    "type": "word", "severity": "blocking", "category": cat,
                    "word": w, "line": line, "pos": i,
                    "message": f"命中「{cat}」词：{w}",
                })
                start = i + len(w)

    # ---- 总结句 ----
    for s in _sentences(txt):
        for pat in (r"^(综上所述|总而言之|总的来说|一言以蔽之)[，,]?",
                    r"(综上所述|总而言之)[。.]?$"):
            if re.search(pat, s):
                findings.append({
                    "type": "summary", "severity": "blocking", "category": "总结句",
                    "text": s[:40],
                    "message": f"总结句开头/结尾：{s[:30]}",
                })
                break

    # ---- 元话语句 ----
    for s in _sentences(txt):
        for w in META:
            if w in s:
                findings.append({
                    "type": "meta", "severity": "blocking", "category": "元话语",
                    "text": s[:40],
                    "message": f"元话语句：{s[:30]}",
                })
                break

    # ---- 空洞权威 ----
    for s in _sentences(txt):
        if any(w in s for w in VAGUE_AUTHORITY) and _cn_len(s) < 60:
            findings.append({
                "type": "authority", "severity": "blocking", "category": "模糊权威",
                "text": s[:40],
                "message": f"短句里出现模糊权威表述：{s[:30]}",
            })

    # ---- 机械排比：连续三个同构分句 ----
    for s in _sentences(txt):
        cl = _clauses(s)
        if len(cl) >= 3:
            pats = []
            for c in cl[:4]:
                pats.append(_clause_pattern(c))
            # 三连且模式完全相同（简化：长度相近 + 起始虚词相同）
            if pats[0] == pats[1] == pats[2] and pats[0]:
                findings.append({
                    "type": "parallel", "severity": "advisory", "category": "机械排比",
                    "text": s[:40],
                    "message": f"连续三句结构雷同：{s[:30]}",
                })

    # ---- 电报体：连续三个极短句 ----
    sents = _sentences(txt)
    for i in range(len(sents) - 2):
        trio = sents[i:i + 3]
        if all(_cn_len(x) <= 6 for x in trio) and all(_cn_len(x) >= 1 for x in trio):
            findings.append({
                "type": "telegraph", "severity": "advisory", "category": "电报体",
                "text": " / ".join(trio)[:40],
                "message": f"连续三句极短（电报体）：{' / '.join(trio)[:30]}",
            })

    # ---- 句长均一 ----
    lens = [_cn_len(s) for s in sents if _cn_len(s) > 0]
    if len(lens) >= 6:
        avg = sum(lens) / len(lens)
        var = sum((x - avg) ** 2 for x in lens) / len(lens)
        sd = var ** 0.5
        if sd / avg < 0.28 and avg > 12:
            findings.append({
                "type": "uniform", "severity": "advisory", "category": "句长均一",
                "message": f"句长变化过小（平均 {avg:.0f} 字，标准差 {sd:.1f}），节奏偏平",
            })

    # ---- 抽象名词密度 ----
    if txt:
        hit = sum(txt.count(w) for w in _ABSTRACT_NOUNS)
        density = hit * 100 / max(1, _cn_len(txt))
        if density > 0.35:
            found = [w for w in _ABSTRACT_NOUNS if w in txt]
            findings.append({
                "type": "abstract", "severity": "advisory", "category": "抽象名词化",
                "message": f"抽象名词密度偏高（{density:.1f}/百字）：{'、'.join(found[:5])}",
            })

    # ---- 句首套话 ----
    first = sents[0] if sents else ""
    for w in CLICHE_OPEN:
        if first.startswith(w):
            findings.append({
                "type": "open", "severity": "blocking", "category": "套话开场",
                "text": first[:40],
                "message": f"套话开场：{first[:30]}",
            })

    # ---- 句尾套话 ----
    if sents:
        last = sents[-1]
        for w in CLICHE_END:
            if w in last:
                findings.append({
                    "type": "end", "severity": "blocking", "category": "套话结尾",
                    "text": last[:40],
                    "message": f"套话结尾：{last[:30]}",
                })

    # ---- 统计 ----
    blocking = [x for x in findings if x["severity"] == "blocking"]
    advisory = [x for x in findings if x["severity"] == "advisory"]
    by_cat = Counter(x.get("category", "") for x in findings)

    return {
        "verdict": "review" if blocking else ("pass" if not advisory else "review"),
        "findings": sorted(findings, key=lambda x: (x["severity"] != "blocking", x.get("pos", 0))),
        "stats": {
            "chars": _cn_len(txt),
            "sentences": len(sents),
            "blocking": len(blocking),
            "advisory": len(advisory),
            "by_category": dict(by_cat),
        },
        "summary": (f"blocking {len(blocking)} 处 / advisory {len(advisory)} 处"
                    f"（正文 {_cn_len(txt)} 字，{len(sents)} 句）"),
    }


def _clause_pattern(clause: str) -> str:
    """粗略的句式模式：首字 + 长度档。用于识别机械排比。"""
    n = _cn_len(clause)
    if n == 0:
        return ""
    head = clause[0]
    bucket = "S" if n <= 5 else ("M" if n <= 12 else "L")
    return f"{head}{bucket}"


def top_words(text: str, n: int = 15) -> list[tuple[str, int]]:
    """高频词（去停用词），用于看机械重复。"""
    stop = set("的了是在和就有也都而及与着或一个我们你们他们这那些什么因为所以但是"
               "可以自己没有也是这样那样不是的时候如果因为所以然后而且不过"
               "我你他她它它们之其于与及但却则乃亦兮")
    # 2-gram 滑窗：中文没有空格分词，用 2 字滑窗统计更接近真实重复
    txt = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    c = Counter(txt[i:i + 2] for i in range(len(txt) - 1))
    c = Counter({w: k for w, k in c.items() if w not in stop})
    return c.most_common(n)


def check_repetition(text: str, threshold: int = 5) -> list[dict]:
    """机械复现：同一词组在文中反复出现。"""
    out = []
    for w, cnt in top_words(text, 40):
        if cnt >= threshold:
            out.append({
                "type": "repetition", "severity": "advisory", "category": "机械复现",
                "word": w, "count": cnt,
                "message": f"「{w}」出现 {cnt} 次，注意机械复现",
            })
    return out


def report_yaml(rep: dict) -> str:
    lines = [
        f"判定: {rep.get('verdict')}",
        f"摘要: {rep.get('summary', '')}",
        "命中项:",
    ]
    fds = rep.get("findings") or []
    if not fds:
        lines.append("  - 无")
    else:
        for x in fds:
            sev = "阻断" if x.get("severity") == "blocking" else "建议"
            cat = x.get("category", "")
            msg = str(x.get("message", "")).replace('"', "'")
            lines.append(f'  - [{sev}] {cat}：{msg}')
    return "\n".join(lines) + "\n"
