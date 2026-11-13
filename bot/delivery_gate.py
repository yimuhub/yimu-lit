"""统一交付标准 · 校验器（v4.33.7）。

纯规则、零 LLM、零副作用：把 ``delivery_spec`` 的问题类型逐条落到可执行的检查上。
每个问题类型在单个检查单元（一章 / 一集）内最多产出一条汇总 finding，报告不会爆炸。

对外入口：
  check_chapter / check_novel    小说（章节级 / 全书级）
  check_episode / check_script   剧本（集级 / 全剧级）
  check_delivery                 导出前统一入口
  export_ready / report_markdown / rewrite_directives
"""
from __future__ import annotations

import re
from collections import Counter

from . import delivery_spec as spec

RE_LINE = re.compile(r"(?m)^\s*([\u4e00-\u9fa5A-Za-z0-9·]{1,14})\s*[：:]\s*(.+)$")
RE_SCENE = re.compile(r"(?m)^\s*\*{0,2}(?:场|第)\s*\d+\s*场?\s*[：:]\s*(?:内\s*/\s*外|内外|内景|外景|内|外)?\s*([^*\n]+)")
RE_SCENE_NO = re.compile(r"(?m)^\s*\*{0,2}(?:场|第)\s*(\d+)\s*场?\s*[：:]")
RE_CHAPTER_NUM = re.compile(r"第\s*\d+\s*章")
RE_TITLE_CLAIM = re.compile(r"([十百千万亿\d]+)\s*(?:只|头|个|名|位)?\s*([\u4e00-\u9fff]{2,6})")
# 数值口径：进度分数（4/5、27/100000）与百分比，仅在出现进度语义提示词时计入
RE_FRACTION = re.compile(r"(\d{1,7})\s*[/／]\s*(\d{1,7})")
RE_PERCENT = re.compile(r"(\d{1,3})\s*[%％]")
PROGRESS_HINTS = ("收服", "进度", "契约", "收集", "任务", "签到", "吞噬")
RE_PAREN_ACTION = re.compile(r"(?m)^\s*[（(][^\n]*")
RE_BRACK_ACTION = re.compile(r"(?m)^\s*【[^\n]*")
TAIL_OK = set("。！？!?…．”’\"')）】》」』>~-—*`|")
TAIL_BAD = set("，,、；;：:")
TIME_WORDS = ("白天", "夜晚", "夜", "日", "晨", "清晨", "黄昏", "傍晚", "深夜",
              "正午", "午后", "凌晨", "黎明", "上午", "下午", "入夜", "日", "昏暗", "暴雨中", "雨后")
PLACE_SUFFIX = ("外围", "边缘", "入口", "深处", "外", "内", "后方", "侧", "中心", "上层", "下层")


def _cn(s: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", s or ""))


def _finding(code: str, location: str, detail: str) -> dict:
    p = spec.BY_CODE[code]
    return {"code": code, "name": p["name"], "severity": p["severity"],
            "stage": p["stage"], "action": p["action"], "scope": p["scope"],
            "location": location, "detail": detail}


# 工艺体检（craft_check）的闸门号 → 交付标准的禁止项号。
# 两个体系分开维护：craft_check 面向写作时的即时体检（带原文证据），
# delivery_spec 面向交付判定（带档位与处置动作），这里做一次映射，避免规则写两遍。
_CRAFT_TO_CODE = {
    "C01": "N03",   # 半句截断
    "C02": "N16",   # 章内复读
    "C03": "N02",   # 元信息泄漏
    "C04": "N02",   # 过程产物残留
    "C05": "N17",   # 计数穿帮
    "C06": "N18",   # 排版符号污染
}


def _craft_findings(text: str, n) -> list[dict]:
    """跑工艺硬伤检查并映射成交付标准 finding；每章每类只出一条，避免报告爆炸。"""
    try:
        from . import craft_check
        raw = craft_check.hard_findings(text or "", n=int(n) if str(n).isdigit() else 0)
    except Exception:
        return []
    loc = f"第{n}章" if n else "全书"
    out, seen = [], set()
    for f in raw:
        code = _CRAFT_TO_CODE.get(f.get("gate"))
        if not code or code in seen:
            continue
        seen.add(code)
        detail = f.get("message") or ""
        if f.get("evidence"):
            detail += f"｜证据：{str(f['evidence'])[:60]}"
        out.append(_finding(code, loc, detail))
    return out


def _latin_hits(text: str) -> list[str]:
    return [w for w in spec.RE_LATIN_TOKEN.findall(text or "")
            if w.upper() not in spec.LATIN_ALLOW]


# 元信息词分两档（v4.36.0）：
#   强信号 —— 只可能来自创作流程（本章概要/字数统计/审稿意见…），出现即判 N02；
#   弱信号 —— 日常叙事也合法：系统流小说里「系统核心自检报告弹出来了」、
#             游戏流里「评分：S」都是故事内设定，判成元信息泄漏会误伤整个题材。
#             这类只记 N19 告警，不阻断交付，交给人确认。
_META_WEAK = ("评分", "得分", "百分制", "自检")

# 弱信号词只有作为**字段标签**出现时才算泄漏：行首「本章评分：九十五分。」
# 是评分表残留（v4.33.7 的原始事故），而「系统核心自检报告弹出来了」是叙事。
_WEAK_LABEL_RE = re.compile(
    r"(?m)^\s*(?:本章|全文|整体|总分)?\s*(" + "|".join(_META_WEAK) + r")\s*[：:｜|]")


def _meta_hits(text: str) -> list[str]:
    found = []
    _t0 = text or ""
    _m = _WEAK_LABEL_RE.search(_t0)
    _labelled = _m.group(1) if _m else ""
    for w in spec.META_LEAK:
        if w not in _t0:
            continue
        if w in _META_WEAK and w != _labelled:
            continue           # 叙事里出现的歧义词留给 N19，不阻断
        found.append(w)
    # 「章节号元引用」只扫叙述与台词正文：集头/场头/标题行里出现「第N章/第N集」是
    # 改编集名与场景结构的合法写法（如「# 第1集：试炼」「**场2：外 山道 晨**」），先剥离再扫
    _t = _RE_STRUCT_LINE.sub("", text or "")
    for name, pat in getattr(spec, "META_RESIDUAL_PATTERNS", ()):
        try:
            if pat.search(_t if name == "章节号元引用" else (text or "")):
                found.append(name)
        except re.error:
            continue
    return found


def _meta_weak_hits(text: str) -> list[str]:
    """歧义元信息词（评分/得分/自检…）：只提示，不阻断。

    已被判成「字段标签」（N02）的那个词不再重复报 N19。
    """
    _t = text or ""
    _m = _WEAK_LABEL_RE.search(_t)
    _labelled = _m.group(1) if _m else ""
    return [w for w in _META_WEAK if w != _labelled and w in _t]


# 结构性标题行：markdown 标题 / 整行加粗（集头·场头）/ 场标题行 / [内 外] 场景 slug
_RE_STRUCT_LINE = re.compile(
    r"(?m)^\s*(?:"
    r"#{1,6}[^\n]*"
    r"|\*{2}[^*\n]+\*{2}\s*"
    r"|(?:场|第)\s*\d+\s*场?\s*[：:][^\n]*"
    r"|\[\s*[内内外外][^\]\n]*\]\s*"
    r")\s*$")


def _tail_state(text: str) -> tuple[bool, str]:
    t = (text or "").rstrip().rstrip("*#-> ").rstrip()
    if not t:
        return True, "正文为空"
    last = t[-1]
    if last in TAIL_BAD:
        return True, f"以「{last}」结尾（半句截断）"
    if last not in TAIL_OK and ("\u4e00" <= last <= "\u9fff" or last.isalpha()):
        return True, f"以汉字/字母「{last}」结尾（无收束标点）"
    return False, ""


def _grams(s: str, n: int = 3) -> set[str]:
    z = re.sub(r"\s+", "", s or "")
    return {z[i:i + n] for i in range(max(0, len(z) - n + 1))}


def _jaccard(a: str, b: str) -> float:
    x, y = _grams(a), _grams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def _first_para(text: str) -> str:
    return next((p.strip() for p in (text or "").split("\n") if p.strip()), "")


def _dialogue(text: str) -> list[tuple[str, str]]:
    return [(m.group(1).strip(), m.group(2).strip()) for m in RE_LINE.finditer(text or "")]


def _heading_kinds(text: str) -> list[str]:
    return [name for name, pat in spec.SCENE_HEADING_PATTERNS.items() if pat.search(text or "")]


def _scene_count(text: str) -> int:
    return sum(len(p.findall(text or "")) for p in spec.SCENE_HEADING_PATTERNS.values())


def _spoken_chars(text: str) -> int:
    """只统计会被配音念出来的对白字数，动作行不计入时长。"""
    return sum(_cn(re.sub(r"^[（(][^）)]*[）)]\s*", "", line)) for _, line in _dialogue(text))


# ---------------------------------------------------------------- 章节级

# 落盘前三查只管「污染类」禁止项（N01 非中文残留 / N02 元信息泄漏 /
# N15 世界观违禁物——调用方传项目红线词表才启用）。
# N03 半句截断由字数保障体系的末尾续写补足流程处理，不在此拦截。
PURITY_BLOCK_CODES = ("N01", "N02", "N15")


def chapter_purity_blocks(text: str, *, title: str = "", chapter_no: int = 0,
                          banned_terms: list | None = None) -> list[dict]:
    """写章热路径用：只返回会污染正文的禁止项（N01/N02/N15），命中即应回灌重写。"""
    out = [f for f in check_chapter(text, title=title, chapter_no=chapter_no,
                                    profile="universal")
           if f["severity"] == "block" and f["code"] in PURITY_BLOCK_CODES]
    for t in (banned_terms or []):
        t = str(t).strip()
        if t and t in (text or ""):
            out.append(_finding("N15", f"第{chapter_no}章" if chapter_no else "正文",
                                f"违禁物「{t}」出现在正文"))
    return out


def check_chapter(text: str, *, title: str = "", chapter_no: int = 0,
                  profile: str = "universal") -> list[dict]:
    loc = f"第{chapter_no}章" if chapter_no else "正文"
    out: list[dict] = []
    body = text or ""

    hits = _latin_hits(body)
    if hits:
        out.append(_finding("N01", loc, f"{len(hits)} 处，例：{', '.join(dict.fromkeys(hits))[:80]}"))
    meta = _meta_hits(body)
    if meta:
        out.append(_finding("N02", loc, f"命中 {len(meta)} 类：{', '.join(meta[:6])}"))
    weak = _meta_weak_hits(body)
    if weak:
        out.append(_finding("N19", loc, f"出现「{', '.join(weak[:4])}」，请确认是故事内设定用语"))
    bad, why = _tail_state(body)
    if bad:
        out.append(_finding("N03", loc, why))

    nums = RE_CHAPTER_NUM.findall(title or "")
    if len(nums) > 1 or (nums and (title or "").strip().rstrip("：: ") == nums[0]):
        out.append(_finding("N04", loc, f"标题「{title}」编号回显"))

    lo = spec.threshold("chapter_min_chars", profile)
    hi = spec.threshold("chapter_max_chars", profile)
    w = _cn(body)
    if w and (w < int(lo) or w > int(hi)):
        out.append(_finding("N05", loc, f"{w} 字，超出 {lo}-{hi} 区间"))

    first = _first_para(body)
    if first and len(first) < 120:
        rest = body[len(first):][:300]
        if _jaccard(first, rest) > float(spec.threshold("intro_dup_jaccard", profile)):
            out.append(_finding("N06", loc, f"题记与正文重合 {_jaccard(first, rest):.2f}"))

    slop = sum(body.count(w) for w in spec.SLOP_TERMS)
    if slop > int(spec.threshold("slop_per_chapter", profile)):
        out.append(_finding("N07", loc, f"套语 {slop} 次 > {spec.threshold('slop_per_chapter', profile)}"))

    tail = body[-int(spec.threshold("hook_tail_chars", profile)):]
    if tail.strip() and not any(k in tail for k in spec.HOOK_SIGNALS):
        out.append(_finding("N08", loc, "章末窗口无新事实/新代价/新威胁信号"))
    return out


# ---------------------------------------------------------------- 全书级

_RE_MEASURE_AFTER = re.compile(r"\s*[斤个只块杯天年米公里份小时元亩位名句话页层批trips]")


def _progress_window(text: str, pos: int, width: int = 14) -> str:
    lo, hi = max(0, pos - width), min(len(text), pos + width)
    return text[lo:hi]


def _is_quantity_fraction(text: str, m: "re.Match") -> bool:
    """3/4 斤、2/3 杯这类数量分数不是进度口径，排除。"""
    tail = text[m.end(): m.end() + 3]
    return bool(_RE_MEASURE_AFTER.match(tail))


def numeric_findings(rows: list[dict]) -> list[dict]:
    """N13 数值口径冲突（确定性、零状态）：
    1) 口径打架——同一部作品里「进度类分数 a/b」出现多个不同分母；
    2) 数值回退——同一分母下分子变小，或进度百分比明显回退（≥10 个点）。
    只统计带进度语义提示词（收服/进度/契约…）上下文的数值，避免误伤普通数字。"""
    out: list[dict] = []
    fracs: list[tuple[int, int, int, str]] = []   # (章号, a, b, snippet)
    pcts: list[tuple[int, int, str]] = []
    for c in rows:
        n = c.get("n") or 0
        no = int(n) if str(n).isdigit() else 0
        text = c.get("text") or ""
        for m in RE_FRACTION.finditer(text):
            if _is_quantity_fraction(text, m):
                continue
            if any(h in _progress_window(text, m.start()) for h in PROGRESS_HINTS):
                fracs.append((no, int(m.group(1)), int(m.group(2)),
                              _progress_window(text, m.start()).strip()))
        for m in RE_PERCENT.finditer(text):
            if any(h in _progress_window(text, m.start()) for h in PROGRESS_HINTS):
                pcts.append((no, int(m.group(1)), _progress_window(text, m.start()).strip()))
    dens = sorted({b for _, _, b, _ in fracs})
    if len(dens) > 1:
        ex = [f"{a}/{b}(第{n}章)" for n, a, b, _ in fracs if b in dens[1:]][:4]
        out.append(_finding("N13", "全书",
                            f"进度分母口径打架：{dens}，例：{', '.join(ex)}"))
    by_den: dict[int, tuple[int, int]] = {}   # 分母 -> (最大分子, 章号)
    for no, a, b, _ in sorted(fracs):
        prev = by_den.get(b)
        if prev and a < prev[0]:
            out.append(_finding("N13", f"第{no}章",
                                f"进度回退：{a}/{b} 早于第{prev[1]}章的 {prev[0]}/{b}"))
        by_den[b] = (max(a, prev[0] if prev else 0), prev[1] if prev and prev[0] >= a else no)
    best = None
    for no, v, _ in sorted(pcts):
        if best and v < best[0] - 10:
            out.append(_finding("N13", f"第{no}章",
                                f"进度回退：{v}% 早于第{best[1]}章的 {best[0]}%"))
        best = (v, no) if not best or v >= best[0] else best
    return out


def check_novel(chapters: list[dict], *, book_title: str = "", claim_term: str = "",
                entity_aliases: dict | None = None, profile: str = "novel_submission") -> dict:
    findings: list[dict] = []
    rows = [c for c in (chapters or []) if isinstance(c, dict)]
    for c in rows:
        n = c.get("n") or 0
        _base = check_chapter(c.get("text") or "", title=c.get("title") or "",
                              chapter_no=int(n) if str(n).isdigit() else 0,
                              profile=profile)
        _craft = _craft_findings(c.get("text") or "", n)
        if _craft:
            # 工艺闸门给的条目带原文证据，比 check_chapter 的「命中 N 类」更有用；
            # 同章同编号只留带证据的那条，否则「阻断 N 项」会因重复计数虚高
            # （《入赘三年》实拍：40 章元信息泄漏被记成 49 项）。
            _drop = {f["code"] for f in _craft}
            _base = [f for f in _base if f["code"] not in _drop]
        findings.extend(_base)
        findings.extend(_craft)

    # N06 模板化开篇：首句高相似占比
    opens = [_first_para(c.get("text") or "")[:60] for c in rows]
    opens = [o for o in opens if o]
    if len(opens) >= 5:
        common = Counter(opens).most_common(1)[0][1]
        if common / len(opens) > float(spec.threshold("template_open_ratio", profile)):
            findings.append(_finding("N06", "全书", f"{common}/{len(opens)} 章开篇句式重复"))

    # N09 相邻章事件重演
    prev = ""
    for c in rows:
        cur = c.get("text") or ""
        n = c.get("n") or "?"
        shared = [m for m in spec.EVENT_MARKERS if m in prev and m in cur]
        sim = _jaccard(prev[:200], cur[:200])
        if shared or (prev and sim > float(spec.threshold("adjacent_event_jaccard", profile))):
            findings.append(_finding("N09", f"第{n}章",
                                     f"与上一章重复事件 {shared[:2] or '开头重合'}，相似度 {sim:.2f}"))
        prev = cur

    # N10 终章开新坑
    if rows:
        last = rows[-1]
        tail_text = (last.get("text") or "")[-400:]
        hits = [k for k in spec.NEW_ARC_TAIL if k in tail_text]
        ln = last.get("n") or len(rows)
        if hits:
            findings.append(_finding("N10", f"第{ln}章", f"终章开新坑：{', '.join(hits[:5])}"))
        bad, why = _tail_state(last.get("text") or "")
        if bad:
            findings.append(_finding("N03", f"第{ln}章", why))

    # N11 标题承诺兑现
    term = claim_term or ""
    if not term and book_title:
        m = RE_TITLE_CLAIM.search(book_title)
        if m:
            term = m.group(1) + m.group(2)
    if term:
        all_text = "\n".join(c.get("text") or "" for c in rows)
        cnt = all_text.count(term)
        tail_ten = "\n".join(c.get("text") or "" for c in rows[-10:])
        if cnt < 3 or term not in tail_ten:
            findings.append(_finding("N11", "书名",
                                     f"主张「{term}」全书 {cnt} 次，末 10 章{'有' if term in tail_ten else '无'}兑现"))

    # N12 实体命名漂移
    if entity_aliases:
        all_text = "\n".join(c.get("text") or "" for c in rows)
        for canonical, aliases in entity_aliases.items():
            for a in aliases:
                if a and a != canonical and a in all_text:
                    findings.append(_finding("N12", "全书", f"「{canonical}」被写成「{a}」"))

    # N13 数值口径冲突
    findings.extend(numeric_findings(rows))

    hook_total = len(rows)
    hook_miss = sum(1 for f in findings if f["code"] == "N08")
    metrics = {"chapters": len(rows),
               "total_chars": sum(_cn(c.get("text") or "") for c in rows),
               "hook_rate": round(1 - hook_miss / hook_total, 3) if hook_total else 0.0}
    return _report("novel", profile, findings, metrics)


# ---------------------------------------------------------------- 集级

# 场（scene）级适用的禁止项：剧本引擎逐场写作，行级污染检查（S01/S02/S03/S11/N02/N03）
# 对单场有效；S04 集预算 / S10 集钩子 / S14 场号唯一是「整集」口径，只在 check_episode 用。
SCENE_SAFE_BLOCK_CODES = ("S01", "S02", "S03", "S11", "N02", "N03")


def scene_block_findings(text: str, *, scene_no: int = 0) -> list[dict]:
    """单场定稿前的行级污染检查（不套整集预算/钩子口径）。"""
    return [f for f in check_episode(text, ep_no=scene_no)
            if f["severity"] == "block" and f["code"] in SCENE_SAFE_BLOCK_CODES]


def check_episode(text: str, *, ep_no: int = 0, profile: str = "universal") -> list[dict]:
    loc = f"第{ep_no}集" if ep_no else "剧本"
    out: list[dict] = []
    body = text or ""
    lines = _dialogue(body)
    max_line = int(spec.threshold("max_line_chars", profile))

    paren = [(sp, ln) for sp, ln in lines if ln.startswith(("（", "("))]
    if paren:
        out.append(_finding("S01", loc, f"{len(paren)}/{len(lines)} 行动作写进对白，例：{paren[0][0]}：{paren[0][1][:28]}"))

    dirty = [(sp, ln) for sp, ln in lines
             if any(a in sp for a in spec.SPEAKER_ACTION)
             or len(sp) > int(spec.threshold("max_speaker_name_chars", profile))]
    if dirty:
        out.append(_finding("S02", loc, f"{len(dirty)} 处说话人粘连，例：{dirty[0][0]}"))

    long = [(sp, ln) for sp, ln in lines if _cn(ln) > max_line]
    if long:
        out.append(_finding("S03", loc, f"{len(long)} 句 >{max_line} 字，最长 {max(_cn(ln) for _, ln in long)} 字"))

    spoken = _spoken_chars(body)
    scenes = _scene_count(body)
    if spoken > int(spec.threshold("episode_max_spoken_chars", profile)) or scenes < int(spec.threshold("min_scenes", profile)):
        out.append(_finding("S04", loc, f"对白 {spoken} 字 / {scenes} 场，超出档位预算"))

    kinds = _heading_kinds(body)
    if len(kinds) > 1:
        out.append(_finding("S05", loc, f"混用场标题写法：{', '.join(kinds)}"))

    abstract = [w for w in spec.ABSTRACT_EMOTION if w in body]
    if abstract:
        out.append(_finding("S07", loc, f"{sum(body.count(w) for w in abstract)} 处，例：{abstract[:3]}"))

    for i in range(1, len(lines)):
        a, b = lines[i - 1], lines[i]
        if a[0] != b[0] and a[1] and a[1].rstrip("。！？!?…") == b[1].rstrip("。！？!?…"):
            out.append(_finding("S09", loc, f"相邻两行不同角色说同一句：{a[0]} / {b[0]}「{a[1][:24]}」"))
            break

    # S15 动作句写进台词：无括号的纯动作短语开头的台词行（「林野：后退一步。」）
    acts = [(sp, ln) for sp, ln in lines
            if ln.startswith(spec.ACTION_LINE_STARTS) and _cn(ln) <= 12
            and not any(q in ln for q in ("「", "“", '"'))]
    if acts:
        out.append(_finding("S15", loc, f"{len(acts)} 行动作写进台词，例：{acts[0][0]}：{acts[0][1][:20]}"))

    # S14 场次编号重复：同一集内两个相同编号的场标题
    nos = [int(m.group(1)) for m in RE_SCENE_NO.finditer(body)]
    dup = sorted({n for n in nos if nos.count(n) > 1})
    if dup:
        out.append(_finding("S14", loc, f"场号重复：{dup[:5]}"))

    head = body[:int(spec.threshold("open_conflict_chars", profile))]
    tail = body[-int(spec.threshold("tail_hook_chars", profile)):]
    miss = []
    if not any(k in head for k in spec.OPEN_CONFLICT):
        miss.append("集首无强冲突")
    if not any(k in tail for k in spec.HOOK_SIGNALS):
        miss.append("集尾无钩子")
    if miss:
        out.append(_finding("S10", loc, "、".join(miss)))

    hits = _latin_hits(body)
    if hits:
        out.append(_finding("S11", loc, f"{len(hits)} 处，例：{', '.join(dict.fromkeys(hits))[:80]}"))
    meta = _meta_hits(body)
    if meta:
        out.append(_finding("N02", loc, f"命中 {len(meta)} 类：{', '.join(meta[:6])}"))
    _weak = _meta_weak_hits(body)
    if _weak:
        out.append(_finding("N19", loc, f"出现「{', '.join(_weak[:4])}」，请确认是台词内设定用语"))
    bad, why = _tail_state(body)
    if bad:
        out.append(_finding("N03", loc, why))
    return out


# ---------------------------------------------------------------- 全剧级

def _canonical_place(p: str) -> str:
    z = p.strip()
    for t in TIME_WORDS:
        z = z.replace(t, "")
    for s in PLACE_SUFFIX:
        z = z.replace(s, "")
    return re.sub(r"[\s·｜|，,：:：]+", "", z)


def check_script(episodes: list[dict], *, assets: dict | None = None,
                 shots: list[dict] | None = None, profile: str = "script_production") -> dict:
    rows = [e for e in (episodes or []) if isinstance(e, dict)]
    findings: list[dict] = []
    total_seconds = 0.0
    for e in rows:
        n = e.get("n") or 0
        text = e.get("text") or ""
        no = int(n) if str(n).isdigit() else 0
        findings.extend(check_episode(text, ep_no=no, profile=profile))
        total_seconds += _spoken_chars(text) / float(spec.threshold("chars_per_second", profile))

    # S02 一次性说话人占比
    names = Counter()
    for e in rows:
        names.update(sp for sp, _ in _dialogue(e.get("text") or ""))
    once = sum(1 for v in names.values() if v == 1)
    if names and once / len(names) > float(spec.threshold("max_speaker_once_ratio", profile)):
        findings.append(_finding("S02", "全剧",
                                 f"{once}/{len(names)} 个说话人只出现一次（>{spec.threshold('max_speaker_once_ratio', profile):.0%}）"))

    # S05 全剧场标题 schema 统一
    kinds = set()
    for e in rows:
        kinds.update(_heading_kinds(e.get("text") or ""))
    if len(kinds) > 1:
        findings.append(_finding("S05", "全剧", f"混用 {len(kinds)} 种场标题：{', '.join(sorted(kinds))}"))

    # S13 括号体系混用：全角（）动作行与方头【】动作行并存
    has_paren = any(RE_PAREN_ACTION.search(e.get("text") or "") for e in rows)
    has_brack = any(RE_BRACK_ACTION.search(e.get("text") or "") for e in rows)
    if has_paren and has_brack:
        findings.append(_finding("S13", "全剧", "（）与【】两种括号体系混用"))

    # N13 数值口径冲突
    findings.extend(numeric_findings(rows))

    # S06 出片资产与镜头字段
    if assets is not None:
        missing = [k for k in spec.REQUIRED_ASSETS if not assets.get(k)]
        if missing:
            findings.append(_finding("S06", "成片包", f"缺资产：{', '.join(missing)}"))
    if shots is not None:
        lack = Counter()
        for s in shots:
            for f in spec.REQUIRED_SHOT_FIELDS:
                if f not in s or s.get(f) in (None, ""):
                    lack[f] += 1
        if lack:
            findings.append(_finding("S06", "镜头表", f"字段缺失：{dict(lack.most_common(5))}"))

    # S08 地点锚点别名
    groups: dict[str, set[str]] = {}
    for e in rows:
        for p in RE_SCENE.findall(e.get("text") or ""):
            key = _canonical_place(p)
            if key:
                groups.setdefault(key, set()).add(p.strip())
    alias = {k: sorted(v) for k, v in groups.items() if len(v) > 1}
    if alias:
        sample = list(alias.items())[:3]
        findings.append(_finding("S08", "全剧", f"{len(alias)} 组场景别名，例：{sample}"))

    # S12 时长
    mx = float(spec.threshold("ep_seconds_max", profile))
    over = [i + 1 for i, e in enumerate(rows)
            if _spoken_chars(e.get("text") or "") / float(spec.threshold("chars_per_second", profile)) > mx]
    if over:
        findings.append(_finding("S12", "全剧", f"{len(over)} 集 >{mx:.0f} 秒，例：第 {over[:5]} 集"))

    metrics = {"episodes": len(rows), "speakers": len(names),
               "estimated_seconds": round(total_seconds),
               "scene_alias_groups": len(alias),
               "hook_rate": round(1 - sum(1 for f in findings if f["code"] == "S10") / len(rows), 3) if rows else 0.0}
    return _report("script", profile, findings, metrics)


# ---------------------------------------------------------------- 统一入口

def _report(kind: str, profile: str, findings: list[dict], metrics: dict) -> dict:
    counts = Counter(f["severity"] for f in findings)
    prof = spec.profile(profile)
    blocking = [f for f in findings if f["severity"] == "block"
                and f["code"] in prof.get("block_on_export", ())]
    return {"spec_version": spec.SPEC_VERSION, "profile": profile,
            "enforcement": prof.get("enforcement", "advisory"), "kind": kind,
            "findings": findings,
            "counts": {"block": counts.get("block", 0), "warn": counts.get("warn", 0),
                       "info": counts.get("info", 0)},
            "metrics": metrics, "blocking_count": len(blocking),
            "ready": len(blocking) == 0,
            "notes": [],
            "summary": f"{counts.get('block', 0)} 项禁止项 / {counts.get('warn', 0)} 项告警"}


def check_delivery(chapters: list[dict], *, title: str = "", kind: str = "novel",
                   profile: str | None = None) -> dict:
    """导出前统一入口：按文体路由到对应校验器，产出《交付就绪报告》。"""
    prof = profile or ("script_production" if kind == "script" else
                       "novel_submission" if kind == "novel" else "universal")
    if kind == "script":
        return check_script(chapters, profile=prof)
    if kind == "novel":
        return check_novel(chapters, book_title=title, profile=prof)
    findings = []
    for c in chapters or []:
        loc = f"第{c.get('n') or 1}节"
        t = c.get("text") or ""
        if _latin_hits(t):
            findings.append(_finding("N01", loc, f"{len(_latin_hits(t))} 处非中文残留"))
        if _meta_hits(t):
            findings.append(_finding("N02", loc, "元信息泄漏"))
        bad, why = _tail_state(t)
        if bad:
            findings.append(_finding("N03", loc, why))
    return _report("mini", "universal", findings, {"sections": len(chapters or [])})


def export_ready(report: dict, *, enforcement: str | None = None) -> tuple[bool, str]:
    """导出闸门。enforcing 档位：有任一禁止项即拒绝；advisory：只报告不阻断。"""
    mode = enforcement or report.get("enforcement") or "advisory"
    n = int(report.get("blocking_count") or 0)
    if mode == "enforcing" and n:
        codes = sorted({f["code"] for f in report.get("findings", [])
                        if f["severity"] == "block"})
        return False, f"交付未就绪：{n} 项禁止项（{', '.join(codes[:8])}），详见交付就绪报告"
    return True, (f"交付就绪（{mode}，{n} 项禁止项仅记录）" if n else "交付就绪：无禁止项")


def rewrite_directives(findings: list[dict]) -> list[str]:
    """把禁止项转成回灌给写作 Agent 的机器处置指令（不是给人的改稿建议）。"""
    return [f"[{f['code']}] {f['name']} @ {f['location']}：{f['action']}；命中={f['detail']}"
            for f in findings if f["severity"] == "block"]


def report_markdown(report: dict) -> str:
    head = ["# 交付就绪报告", "",
            f"- 规格版本：{report.get('spec_version')}",
            f"- 交付档位：{report.get('profile')}（{report.get('enforcement')}）"]
    # v4.36.0：标尺说明（形态 + 平台基准）。放在结论前，作者先知道「拿什么标准衡量我」，
    # 再看那一串禁止项——否则报告读起来只像挑刺。
    notes = [f"- 标尺：{n}" for n in (report.get("notes") or []) if n]
    lines = head + notes + [
        f"- 结论：{report.get('summary')}；禁止项阻断 {report.get('blocking_count')} 项",
        f"- 指标：{report.get('metrics')}", "",
        "| 编号 | 级别 | 位置 | 问题类型 | 命中口径 |",
        "| :--- | :--- | :--- | :--- | :--- |"]
    for f in report.get("findings", [])[:200]:
        detail = str(f["detail"]).replace("|", "/")[:110]
        lines.append(f"| {f['code']} | {f['severity']} | {f['location']} | {f['name']} | {detail} |")
    return "\n".join(lines) + "\n"
