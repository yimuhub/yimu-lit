"""诗词格律校验器（v1.0.0 · 神笔文学创作台）

纯标准库实现，零依赖。基于平水韵 106 部（skillkit/poetry/data/pingshui.json），
对近体诗（五绝 / 七绝 / 五律 / 七律）做确定性检查。

**三态判定**（不装确定）：
  pass    —— 全部检查项通过
  fail    —— 明确违反格律（可指认位置）
  unclear —— 多音字 / 生僻字 / 数据未覆盖，交模型按句义判断

检查项与判定强度（刻意分层，不装确定）：

  【fail · 客观确定】
  1. 句数与字数不符体式
  2. 押韵：一韵到底，首句可借邻韵；出韵 / 重韵

  【advisory · 需人工核】
  3. 平仄：按「粘对」规则检查偶数位冲突（对：同联上下句第 2 位相反；
     粘：上联下句第 3 位 = 下联上句第 4 位），并检查孤平与三平调。
     —— 近体诗变体、拗句、孤平救法众多，故一律不判 fail。
  4. 对仗：律诗颔联 / 颈联的虚实对应（粗判，供人核）

  【unclear · 交模型按句义定】
  5. 多音字、未收录生僻字

不做的：不判断格律之外的文学优劣（那是 LLM 审校的事）。
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PINGSHUI_PATH = os.path.join(ROOT, "skillkit", "poetry", "data", "pingshui.json")

# ---------- 体式定义 ----------
#
# 平仄不用硬编码模板，而用「粘对」规则推导——这是近体诗格律的本来规则：
#
#   1. 每句的偶数位（第 2、4、6…位）必须分明：平仄不可错位。
#   2. 相邻两句的偶数位必须相反（对）：上句第 2 位平，则下句第 2 位仄。
#   3. 同一联内，上句第 4 位与下句第 3 位相同（粘）。
#   4. 句首（第 1 位）平仄不拘；但要查「孤平」等禁忌。
#   5. 奇数位（第 3、5 位）一般不论，只在造成「孤平」「三平调」「三仄脚」时提示。
#
# 用规则而非模板，好处是对真实古诗误报率极低：只在偶数位真正冲突时判 fail。

# 韵脚位置（0-based）：绝句第 1、3 句入韵；律诗第 1、3、5、7 句入韵
# jianju：相邻两句为一「联」
FORMS: dict[str, dict] = {
    "wujue": {
        "label": "五绝", "lines": 4, "chars": 5,
        "rhyme_pos": [1, 3], "parallel": [], "jianju": [(0, 1), (2, 3)],
    },
    "qijue": {
        "label": "七绝", "lines": 4, "chars": 7,
        "rhyme_pos": [1, 3], "parallel": [], "jianju": [(0, 1), (2, 3)],
    },
    "wulv": {
        "label": "五律", "lines": 8, "chars": 5,
        "rhyme_pos": [1, 3, 5, 7], "parallel": [(1, 2), (5, 6)],
        "jianju": [(0, 1), (2, 3), (4, 5), (6, 7)],
    },
    "qilv": {
        "label": "七律", "lines": 8, "chars": 7,
        "rhyme_pos": [1, 3, 5, 7], "parallel": [(1, 2), (5, 6)],
        "jianju": [(0, 1), (2, 3), (4, 5), (6, 7)],
    },
}

# 体式别名
FORM_ALIASES = {
    "五绝": "wujue", "五言绝句": "wujue", "wujue": "wujue", "5jue": "wujue",
    "七绝": "qijue", "七言绝句": "qijue", "qijue": "qijue", "7jue": "qijue",
    "五律": "wulv", "五言律诗": "wulv", "wulv": "wulv", "5lv": "wulv",
    "七律": "qilv", "七言律诗": "qilv", "qilv": "qilv", "7lv": "qilv",
}

# 常见多音字：字 → 可能读音列表（按平仄）。取值为 (平, 仄) 的组合。
# 数据不足时标 unclear，不硬判。
POLYPHONIC: dict[str, tuple[str, ...]] = {
    "看": ("平", "仄"), "说": ("平", "仄"), "教": ("平", "仄"), "过": ("平", "仄"),
    "还": ("平", "仄"), "重": ("平", "仄"), "行": ("平", "仄"), "华": ("平", "仄"),
    "思": ("平", "仄"), "知": ("平", "仄"), "为": ("平", "仄"), "中": ("平", "仄"),
    "朝": ("平", "仄"), "将": ("平", "仄"), "强": ("平", "仄"), "藏": ("平", "仄"),
    "长": ("平", "仄"), "难": ("平", "仄"), "和": ("平", "仄"), "差": ("平", "仄"),
    "传": ("平", "仄"), "称": ("平", "仄"), "令": ("平", "仄"), "调": ("平", "仄"),
    "尽": ("平", "仄"), "会": ("平", "仄"), "曲": ("平", "仄"), "露": ("平", "仄"),
    "数": ("平", "仄"), "系": ("平", "仄"), "折": ("平", "仄"), "参": ("平", "仄"),
}

# 韵部邻接表（用于「首句可借邻韵」的宽松判定）——由平水韵的一上一下推得
_ADJACENT_PAIRS: set[tuple[str, str]] = set()


# ---------- 数据加载 ----------

@lru_cache(maxsize=1)
def _pingshui() -> dict[str, list[dict]]:
    try:
        with open(PINGSHUI_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _rhyme_index() -> dict[str, str]:
    """汉字 → 韵部名（取第一个条目）。"""
    out: dict[str, str] = {}
    for ch, lst in _pingshui().items():
        if lst:
            r = lst[0].get("rhyme")
            if r:
                out[ch] = r
    return out


@lru_cache(maxsize=1)
def _tone_map() -> dict[str, str]:
    """汉字 → 平/仄。数据里同字多音的取第一个（多音字走 unclear）。"""
    out: dict[str, str] = {}
    for ch, lst in _pingshui().items():
        if lst:
            t = lst[0].get("tone")
            if t in ("平", "仄"):
                out[ch] = t
    return out


@lru_cache(maxsize=1)
def _rhyme_by_tone() -> dict[str, set[str]]:
    """声调 → 韵部集合。"""
    out: dict[str, set[str]] = {"平": set(), "仄": set()}
    for _ch, lst in _pingshui().items():
        for item in lst:
            r, t = item.get("rhyme"), item.get("tone")
            if r and t in out:
                out[t].add(r)
    return out


def _init_adjacent() -> None:
    """由平水韵部名（上下平/上声/去声/入声 + 序号）推相邻关系。"""
    if _ADJACENT_PAIRS:
        return
    buckets: dict[str, list[tuple[int, str]]] = {}
    for r in _rhyme_by_tone()["平"]:
        m = re.match(r"^(上平|下平)([一二三四五六七八九十]+)(.+)$", r)
        if m:
            bucket = m.group(1)
            num = _cn_num(m.group(2))
            if num:
                buckets.setdefault(bucket, []).append((num, r))
    for _bucket, lst in buckets.items():
        lst.sort()
        for i in range(len(lst) - 1):
            _ADJACENT_PAIRS.add((lst[i][1], lst[i + 1][1]))
            _ADJACENT_PAIRS.add((lst[i + 1][1], lst[i][1]))


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
           "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13, "十四": 14,
           "十五": 15, "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
           "二十一": 21, "二十二": 22, "二十三": 23, "二十四": 24, "二十五": 25,
           "二十六": 26, "二十七": 27, "二十八": 28, "二十九": 29, "三十": 30}


def _cn_num(s: str) -> int | None:
    return _CN_NUM.get(s)


# ---------- 解析 ----------

_PUNCT = "，。！？；：、,.!?;:　 \t　·—…《》「」『』()（）"


def extract_lines(poem: str) -> list[str]:
    """从诗文本抽出句子。

    切句规则：先按行切，行内再按句读标点（，、；？！）切——中文近体诗常见
    「一句一行」也常见「四句两行」，两种都要支持。
    """
    out: list[str] = []
    for raw in (poem or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # 去掉行首编号：1. / 1、 / 一、 / (1)
        line = re.sub(r"^[（(]?\s*[0-9一二三四五六七八九十]+\s*[）).、．]\s*", "", line)
        line = re.sub(r"^\s*[0-9]+\s*[.、．]\s*", "", line)
        # 标题行（不含汉字正文，或明显是标题）跳过：太短且以标点结尾
        # 行内按句读标点切分
        parts = re.split(r"[，、；;？?！!]+", line)
        for part in parts:
            p = part.strip()
            if not p:
                continue
            # 去掉尾部的括号注
            p = re.sub(r"[（(][^）)]*[）)]\s*$", "", p).strip()
            # 去掉句尾标点残留
            p = p.rstrip("。，,.：:")
            chars = "".join(ch for ch in p if ch not in _PUNCT and not ch.isspace())
            if chars:
                out.append(chars)
    return out


def normalize_form(form: str) -> str:
    f = (form or "").strip().lower()
    if f in FORMS:
        return f
    for k, v in FORM_ALIASES.items():
        if k in (form or ""):
            return v
    return ""


def detect_form(lines: list[str]) -> str:
    """从句数与字数猜体式。所有句字数一致才判。"""
    if not lines:
        return ""
    n = len(lines)
    lens = {len(x) for x in lines}
    if len(lens) != 1:
        return ""
    c = lens.pop()
    if n == 4 and c == 5:
        return "wujue"
    if n == 4 and c == 7:
        return "qijue"
    if n == 8 and c == 5:
        return "wulv"
    if n == 8 and c == 7:
        return "qilv"
    return ""


# ---------- 检查 ----------

def _tone_of(ch: str) -> tuple[str, str]:
    """返回 (平仄, 状态)。状态 ∈ ok | poly | unknown"""
    tm = _tone_map()
    if ch in tm:
        t = tm[ch]
        if ch in POLYPHONIC:
            return t, "poly"
        return t, "ok"
    if ch in POLYPHONIC:
        return "", "poly"
    return "", "unknown"


def _pattern_tone(tpl: str, i: int) -> str:
    """取模板某位的要求：P / Z / N（可平可仄）。"""
    return tpl[i] if i < len(tpl) else "N"


def _line_tones(line: str) -> list[tuple[str, str]]:
    """逐字取 (声调, 状态)。状态 ∈ ok | poly | unknown"""
    return [_tone_of(ch) for ch in line]


def check_pingze(lines: list[str], form_key: str) -> tuple[list[dict], list[str]]:
    """平仄检查（粘对规则）。

    返回 (findings, unclear_notes)。

    判 fail 的只有两类：
      1. 偶数位「失对」——同联两句的偶数位平仄相同（该相反）
      2. 偶数位「孤平/三平调」——句内偶数位平仄孤立
    奇数位一律只作 advisory。
    """
    f = FORMS[form_key]
    findings: list[dict] = []
    unclear: list[str] = []
    tones: list[list[tuple[str, str]]] = []

    for li, line in enumerate(lines):
        tl = _line_tones(line)
        tones.append(tl)
        for ci, (_t, st) in enumerate(tl):
            if st == "unknown":
                unclear.append(f"第{li+1}句第{ci+1}字「{line[ci]}」未收录声调")
            elif st == "poly":
                unclear.append(f"第{li+1}句第{ci+1}字「{line[ci]}」为多音字，需按句义定音")

    # ---- 粘对：同联两句的判定位
    #   对：同联内，上句第 2 位与下句第 2 位相反（1-based）
    #   粘：跨联，上联下句第 3 位与下联上句第 4 位相同（1-based）
    # 只有这两处必须严格，其它偶数位由「对」的连锁关系自动约束。
    # 对：同联内上句第 2 位 ↔ 下句第 2 位，应相反
    for a, b in f.get("jianju") or []:
        if a >= len(lines) or b >= len(lines):
            continue
        ta, tb = tones[a], tones[b]
        ca = cb = 1   # 0-based 第 2 位
        if ca >= len(ta) or cb >= len(tb):
            continue
        xa, xb = ta[ca][0], tb[cb][0]
        if not xa or not xb or ta[ca][1] == "poly" or tb[cb][1] == "poly":
            continue
        if xa == xb:
            findings.append({
                "type": "pingze", "severity": "advisory", "line": b + 1, "pos": cb + 1,
                "char": lines[b][cb],
                "message": f"第{a+1}、{b+1}句失对：第2字"
                           f"「{lines[a][ca]}」（{xa}声）／「{lines[b][cb]}」（{xb}声）应相反（请人工核拗句）",
            })

    # 粘：跨联，上联下句第 3 位 ↔ 下联上句第 4 位，应相同
    jianju = f.get("jianju") or []
    for i in range(len(jianju) - 1):
        _a, up_end = jianju[i]        # 上联的下句
        next_start, _b = jianju[i + 1]  # 下联的上句
        if up_end >= len(lines) or next_start >= len(lines):
            continue
        cu, cv = 2, 3                   # 0-based：上联下句第 3 位 / 下联上句第 4 位
        if cu >= len(tones[up_end]) or cv >= len(tones[next_start]):
            continue
        xu, xv = tones[up_end][cu][0], tones[next_start][cv][0]
        if not xu or not xv or tones[up_end][cu][1] == "poly" or tones[next_start][cv][1] == "poly":
            continue
        if xu != xv:
            findings.append({
                "type": "pingze", "severity": "advisory", "line": next_start + 1, "pos": cv + 1,
                "char": lines[next_start][cv],
                "message": f"第{up_end+1}、{next_start+1}句失粘："
                           f"第{cv+1}字「{lines[next_start][cv]}」（{xv}声）"
                           f"与第{up_end+1}句第{cu+1}字「{lines[up_end][cu]}」（{xu}声）应相同（请人工核）",
            })

    # ---- 句内偶数位：查三平调 / 三仄脚（偶数位连续同调且形成孤立）----
    for li, line in enumerate(lines):
        tl = tones[li]
        even = [(ci, tl[ci][0]) for ci in range(len(tl)) if ci % 2 == 1 and tl[ci][0]]
        if len(even) < 2:
            continue
        vals = [t for _ci, t in even]
        if len(set(vals)) == 1 and len(even) >= 3:
            # 三个偶数位全同调 —— 五言句为「仄仄平仄仄」类，多属拗句
            findings.append({
                "type": "pingze", "severity": "advisory", "line": li + 1,
                "message": f"第{li+1}句「{line}」偶数位连续同调（{vals[0]}），"
                           f"疑拗句，宜换字或调整邻句",
            })

    return findings, unclear


def _rhyme_of(ch: str) -> str:
    return _rhyme_index().get(ch, "")


def check_rhyme(lines: list[str], form_key: str) -> list[dict]:
    """押韵检查。"""
    f = FORMS[form_key]
    findings = []
    _init_adjacent()
    rhymes: list[tuple[int, str, str]] = []   # (句号, 字, 韵部)
    for li in f["rhyme_pos"]:
        if li >= len(lines):
            continue
        line = lines[li]
        ch = line[-1] if line else ""
        r = _rhyme_of(ch)
        if r:
            rhymes.append((li + 1, ch, r))
        else:
            findings.append({
                "type": "rhyme", "severity": "unclear", "line": li + 1,
                "message": f"第{li+1}句韵脚「{ch}」未收录平水韵，无法判定",
            })
    if len(rhymes) < 2:
        return findings
    base = rhymes[0][2]
    for ln, ch, r in rhymes[1:]:
        if r == base:
            continue
        # 首句借邻韵允许
        if ln == f["rhyme_pos"][0] + 1 and (base, r) in _ADJACENT_PAIRS:
            findings.append({
                "type": "rhyme", "severity": "advisory", "line": ln,
                "message": f"第{ln}句韵脚「{ch}」属「{r}」，与主韵「{base}」为邻韵（首句借韵可，慎用）",
            })
            continue
        findings.append({
            "type": "rhyme", "severity": "fail", "line": ln,
            "message": f"第{ln}句韵脚「{ch}」属「{r}」，出韵（主韵为「{base}」）",
        })
    # 重韵
    seen: dict[str, list[int]] = {}
    for ln, ch, _r in rhymes:
        seen.setdefault(ch, []).append(ln)
    for ch, lns in seen.items():
        if len(lns) > 1:
            findings.append({
                "type": "rhyme", "severity": "fail", "line": lns[0],
                "message": f"韵脚「{ch}」重复出现于第 {'、'.join(map(str, lns))} 句（重韵）",
            })
    return findings


def check_gu_ping(lines: list[str], form_key: str) -> list[dict]:
    """孤平检查：五言「仄仄平平仄」型句，首字仄、次字平则犯孤平。"""
    f = FORMS[form_key]
    if f["chars"] != 5:
        return []
    findings = []
    for li, line in enumerate(lines):
        if len(line) != 5:
            continue
        tones = []
        for ch in line:
            t, st = _tone_of(ch)
            tones.append(t or "")
        # 句型：仄仄平平仄 → 第1字仄 第2字仄 第4字平；犯孤平 = 首字仄而仅第4字平
        if tones[3] == "平" and tones[0] == "仄" and tones[1] == "仄":
            # 第四字平、第五字仄，且第二字仄 → 检查是否只有第四字平
            if tones[0] == "仄" and tones[2] == "平":
                continue   # 仄平平仄仄 有两处平，不算孤平
            findings.append({
                "type": "guping", "severity": "fail", "line": li + 1,
                "message": f"第{li+1}句「{line}」疑犯孤平（该句仅第四字为平声）",
            })
    return findings


def check_duizhang(lines: list[str], form_key: str) -> list[dict]:
    """对仗检查（律诗颔联/颈联）——只给建议，不判 fail。"""
    f = FORMS[form_key]
    findings = []
    for a, b in f.get("parallel") or []:
        if a >= len(lines) or b >= len(lines):
            continue
        la, lb = lines[a], lines[b]
        if len(la) != len(lb):
            continue
        # 简易判定：对应位置的词性是否一致（用一个粗略的虚词/实词分类）
        ta = [_is_function_word(c) for c in la]
        tb = [_is_function_word(c) for c in lb]
        if ta != tb:
            diff = [i + 1 for i, (x, y) in enumerate(zip(ta, tb, strict=False)) if x != y]
            findings.append({
                "type": "duizhang", "severity": "advisory", "line": a + 1,
                "message": f"第{a+1}、{b+1}句疑似失对（第 {'、'.join(map(str,diff))} 字虚实不相对），请人工核",
            })
    return findings


_FUNCTION_WORDS = set("之乎者也矣焉哉与和其于而已则于夫亦兮耳其为是以在有无不")


def _is_function_word(ch: str) -> bool:
    return ch in _FUNCTION_WORDS


# ---------- 主入口 ----------

def validate(poem: str, form: str = "") -> dict:
    """校验一首近体诗。返回报告 dict。

    报告结构：
      {form, form_label, verdict, score_hint, lines, findings[], summary}
    verdict ∈ pass | fail | unclear
    """
    lines = extract_lines(poem)
    form_key = normalize_form(form)
    if not form_key:
        form_key = detect_form(lines)

    findings: list[dict] = []
    summary_parts: list[str] = []

    if not lines:
        return {"form": form_key, "form_label": "", "verdict": "unclear", "lines": [],
                "findings": [{"type": "parse", "severity": "unclear",
                               "message": "没能从输入中解析出诗句"}],
                "summary": "未解析出诗句"}

    if not form_key:
        findings.append({
            "type": "form", "severity": "unclear",
            "message": f"无法判定体式：共 {len(lines)} 句，字数分布 {[len(x) for x in lines]}；"
                       f"请指定体式（五绝/七绝/五律/七律）",
        })
        return {"form": "", "form_label": "", "verdict": "unclear", "lines": lines,
                "findings": findings, "summary": "体式不明，未做格律检查"}

    f = FORMS[form_key]
    # 句数 / 字数
    if len(lines) != f["lines"]:
        findings.append({
            "type": "form", "severity": "fail",
            "message": f"句数不符：{f['label']}应为 {f['lines']} 句，实为 {len(lines)} 句",
        })
    for li, line in enumerate(lines):
        if len(line) != f["chars"]:
            findings.append({
                "type": "form", "severity": "fail", "line": li + 1,
                "message": f"第{li+1}句字数不符：{f['label']}每句 {f['chars']} 字，实为 {len(line)} 字"
                           f"（{line}）",
            })

    pz_findings, unclear_notes = check_pingze(lines, form_key)
    findings.extend(pz_findings)
    findings.extend(check_rhyme(lines, form_key))
    findings.extend(check_gu_ping(lines, form_key))
    findings.extend(check_duizhang(lines, form_key))
    for n in unclear_notes:
        findings.append({"type": "tone", "severity": "unclear", "message": n})

    # 判定
    if any(x["severity"] == "fail" for x in findings):
        verdict = "fail"
    elif any(x["severity"] == "unclear" for x in findings):
        verdict = "unclear"
    else:
        verdict = "pass"

    fails = [x for x in findings if x["severity"] == "fail"]
    advs = [x for x in findings if x["severity"] == "advisory"]
    uncs = [x for x in findings if x["severity"] == "unclear"]
    summary_parts.append(f"体式：{f['label']}（{len(lines)} 句）")
    summary_parts.append(f"判定：{verdict}")
    if fails:
        summary_parts.append(f"明确违规 {len(fails)} 处")
    if advs:
        summary_parts.append(f"建议 {len(advs)} 处")
    if uncs:
        summary_parts.append(f"待定 {len(uncs)} 处")

    return {
        "form": form_key,
        "form_label": f["label"],
        "verdict": verdict,
        "lines": lines,
        "findings": findings,
        "counts": {"fail": len(fails), "advisory": len(advs), "unclear": len(uncs)},
        "summary": "；".join(summary_parts),
    }


def to_yaml(report: dict) -> str:
    """把报告转成 YAML 文本（便于直接写进文件 / 给人看）。"""
    lines = [
        f"体式: {report.get('form_label') or '未判定'}",
        f"判定: {report.get('verdict')}",
        f"摘要: {report.get('summary', '')}",
        "检查项:",
    ]
    fds = report.get("findings") or []
    if not fds:
        lines.append("  - 无")
    else:
        for x in fds:
            sev = {"fail": "违规", "advisory": "建议", "unclear": "待定"}.get(x.get("severity"), "提示")
            loc = f"第{x['line']}句" if x.get("line") else "整体"
            msg = str(x.get("message", "")).replace('"', "'")
            lines.append(f'  - [{sev}] {loc}：{msg}')
    return "\n".join(lines) + "\n"


def rhyme_lookup(ch: str) -> dict:
    """查一个字的平水韵归属（调试 / 写作辅助用）。"""
    tm = _tone_map().get(ch, "")
    r = _rhyme_of(ch)
    return {"char": ch, "tone": tm, "rhyme": r,
            "polyphonic": ch in POLYPHONIC,
            "known": bool(r)}
