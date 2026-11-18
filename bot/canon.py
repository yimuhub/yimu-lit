"""正典体检（v4.40.0）：零 LLM 检查「书名/圣经/人物档案/状态库/章卡/正文」是否是同一本书。

真实事故不是文笔问题，而是事实源分裂：
  * 故事圣经写「沈默、陆青藤」，正文 900 章写的是「陆沉」，其余名字一次没出现；
  * 书名承诺「神棍 / 工业革命」，正文里这两个词出现 0 次；
  * 章卡冲突写「环境压力 / 时代洪流」，没有具体对手，写出来必然是流水账；
  * 300 章里 3 章冲突为空、只有 10 章提到真正的对手。

这些问题都能用字符串、计数和结构检查证明，不需要模型。本模块只做确定性检查，
输出 `25-正典体检.json` 与 `25-正典体检.md`，并给其它模块（章卡、改写、旧书整改）
提供结构化结论。不调用 LLM，不修改正文。
"""
from __future__ import annotations

import datetime
import json
import os
import re
from collections import Counter

from . import state as state_mod
from .safeio import atomic_write_json, atomic_write_text

REPORT_JSON = "25-正典体检.json"
REPORT_MD = "25-正典体检.md"
BIBLE_FILE = "09-故事圣经.md"
CHARACTER_FILE = "00-人物档案.md"
CARD_FILE = "02d-章卡.jsonl"

# 书名里常见的虚词/动词：切出「题材名词」而不是整句口号
_TITLE_SPLIT = re.compile(
    r"[:：·—\-|、，,。！？!?《》【】()（）\s]+|"
    r"(?:我在|在|的|之|与|和|从|到|把|被|将|让|给|对|向|为|是|有|就|都|也|又|还|"
    r"炸出|杀出|闯出|掀起|打造|开启|成为|重生|回到|带着|开局|全球|世界|"
    r"后我|以后|之后)"
)
_TITLE_STOP = {
    "我", "你", "他", "她", "它", "我们", "他们", "一个", "什么", "怎么", "这本", "本书",
    "小说", "故事", "全集", "完结", "最新", "全文",
}
_TITLE_PREFIXES = ("民国", "晚清", "清末", "大明", "大唐", "大宋", "大汉",
                   "宋朝", "明朝", "清朝", "现代", "都市", "末日", "星际")

_NAME_LINE = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:#{2,4}\s*)?(?:\*\*)?([\u4e00-\u9fffA-Za-z·]{2,12})(?:\*\*)?"
    r"\s*(?:[（(][^）)]{0,20}[）)])?\s*[:：\-—]\s*(.+)$"
)
_HEADING = re.compile(r"^\s*#{1,4}\s*(.+?)\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)、])\s+(.+)$")

_SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程"
    "嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧"
    "隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶"
)

_OPPONENT_MARK = ("敌", "反", "对手", "军阀", "反派", "鹰犬", "走狗", "仇", "敌手",
                  "压迫", "敌对", "对头", "势力")


def _pdir(proj) -> str:
    return os.path.abspath(getattr(proj, "path", "") or ".")


def _read(pdir: str, name: str, cap: int = 5_000_000) -> str:
    try:
        return open(os.path.join(pdir, name), encoding="utf-8", errors="replace").read()[:cap]
    except OSError:
        return ""


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def _plan(proj) -> dict:
    try:
        p = proj.plan() if hasattr(proj, "plan") else None
    except Exception:
        p = None
    if isinstance(p, dict) and p:
        return p
    return _read_json(os.path.join(_pdir(proj), "02-写作计划.json"), {}) or {}


def _cn(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def book_title(proj) -> str:
    plan = _plan(proj)
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    for v in (plan.get("novelName"), meta.get("selected_title"), plan.get("title")):
        s = str(v or "").strip()
        if s:
            return s
    base = os.path.basename(_pdir(proj))
    parts = base.split("-", 2)
    return parts[2] if len(parts) >= 3 else base


def title_motifs(title: str) -> list[str]:
    """从书名切出可被正文兑现的题材名词（如「神棍」「桥头」「工业革命」）。"""
    raw = str(title or "")
    out, seen = [], set()

    def _add(p: str):
        p = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", p or "").strip()
        if len(p) < 2 or p in _TITLE_STOP:
            return
        if p not in seen:
            seen.add(p)
            out.append(p[:12])

    for part in _TITLE_SPLIT.split(raw):
        _add(part)
        # 「民国神棍」这类带时代/地域前缀的，前缀本身不是要兑现的题材，
        # 真正会出现在正文里的是后半截，必须单独登记，否则永远判「未兑现」。
        for pre in _TITLE_PREFIXES:
            if part.startswith(pre) and len(part) - len(pre) >= 2:
                _add(part[len(pre):])
                break
    # 书名不带标点时，_TITLE_SPLIT 可能只切出一整句；再按 2–4 字滑窗补名词
    if len(out) <= 1 and len(raw) >= 6:
        chunks = re.findall(r"[\u4e00-\u9fff]{2,4}", raw)
        for c in chunks:
            if c not in seen and c not in _TITLE_STOP:
                seen.add(c)
                out.append(c)
    return out[:10]


def _section_map(text: str) -> dict:
    out, cur = {}, "（开头）"
    for line in (text or "").splitlines():
        m = _HEADING.match(line)
        if m:
            cur = re.sub(r"[*_`#]", "", m.group(1)).strip()[:40] or cur
            out.setdefault(cur, [])
            continue
        out.setdefault(cur, []).append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _names_from_text(text: str, cap: int = 40) -> list[str]:
    """从人物档案/圣经里抽取登记名：优先列表行「- 沈默：...」，其次「### 沈默」。"""
    out, seen = [], set()
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        cand = ""
        m = _NAME_LINE.match(s)
        if m:
            cand = m.group(1)
        else:
            h = _HEADING.match(s)
            if h:
                cand = re.sub(r"[（(].*?[）)]", "", h.group(1)).strip()
        cand = state_mod._clean_person_name(cand)
        if (2 <= len(cand) <= 12 and state_mod._is_person_key(cand)
                and cand not in seen and not any(x in cand for x in ("人物", "关系", "设定", "故事", "主题"))):
            seen.add(cand)
            out.append(cand)
        if len(out) >= cap:
            break
    return out


def _bible_names(text: str) -> dict:
    """只从「人物/角色/主角/反派/配角」相关小节取名字，避免把主题词当人物。"""
    secs = _section_map(text)
    names = {}
    for title, body in secs.items():
        if not any(k in title for k in ("人物", "角色", "主角", "反派", "配角", "群像", "关系")):
            continue
        for n in _names_from_text(body):
            names.setdefault(n, title)
    return names


def _load_cards(pdir: str) -> list[dict]:
    path = os.path.join(pdir, CARD_FILE)
    out = []
    try:
        for line in open(path, encoding="utf-8", errors="replace").read().splitlines():
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                out.append(obj)
    except OSError:
        pass
    return out


def _chapters(proj) -> list[dict]:
    plan = _plan(proj)
    rows = plan.get("chapters") if isinstance(plan.get("chapters"), list) else []
    out = []
    for ch in rows:
        n = int(ch.get("chapterNumber") or 0)
        fp = ch.get("filePath") or ""
        if not n or not fp:
            continue
        path = os.path.join(_pdir(proj), fp)
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        out.append({"n": n, "title": ch.get("title") or "", "text": text})
    return out


def _mentions(text: str, names: list[str]) -> dict:
    return {n: text.count(n) for n in names if n and len(n) >= 2}


def _name_candidates(text: str, limit: int = 12) -> list[tuple[str, int]]:
    """正文里高频的「姓+1/2 字」候选，用来发现未登记但实际在用的名字。"""
    c = Counter()
    # 用前瞻取重叠窗口：贪婪匹配会把「陆沉走进」吃成三字词，主角名就永远发现不了。
    # 二字、三字各扫一遍，否则二字名一定被三字窗口吞掉。
    for size in (2, 3):
        pat = re.compile(r"(?=([\u4e00-\u9fff]{%d}))" % size)
        for m in pat.finditer(text or ""):
            w = m.group(1)
            if w[0] in _SURNAMES:
                c[w] += 1
    for w in list(c):
        if len(w) == 3 and c.get(w[:2], 0) > c[w]:
            # 「陆沉走」只是「陆沉」后面缀了一个动词，不是人名
            del c[w]
    return [(w, k) for w, k in c.most_common(limit * 3) if k >= 5][:limit]


def _alias_index(state: dict) -> dict:
    out = {}
    for name, rec in (state.get("characters") or {}).items():
        rec = rec if isinstance(rec, dict) else {}
        for a in [name] + list(rec.get("aliases") or []):
            a = str(a or "").strip()
            if a and len(a) >= 2:
                out[a] = name
    return out


def _opponents(state: dict, characters_file: str, bible: str) -> list[dict]:
    out, seen = [], set()

    def _add(name, source, status=""):
        nm = state_mod._clean_person_name(name)
        if not nm or nm in seen or not state_mod._is_person_key(nm):
            return
        seen.add(nm)
        out.append({"name": nm, "source": source, "status": status[:60]})

    for name, rec in (state.get("opponents") or {}).items():
        if isinstance(rec, dict):
            _add(name, "状态库·对手表", str(rec.get("status") or rec.get("goal") or ""))
    for name, rec in (state.get("characters") or {}).items():
        rec = rec if isinstance(rec, dict) else {}
        blob = " ".join(str(rec.get(k) or "") for k in ("status", "role", "goal", "arc", "notes"))
        if any(w in blob for w in _OPPONENT_MARK):
            _add(name, "状态库·人物", blob)
    for name in _names_from_text(characters_file, cap=80):
        # 档案里只凭「对手/反派」段落判断，避免把所有配角都算对手
        secs = _section_map(characters_file)
        for title, body in secs.items():
            if any(k in title for k in ("对手", "反派", "敌人", "敌对")):
                if name in body:
                    _add(name, f"人物档案·{title}")
    for title, body in _section_map(bible).items():
        if any(k in title for k in ("对手", "反派", "敌人", "敌对")):
            for name in _names_from_text(body):
                _add(name, f"圣经·{title}")
    return out


def scan(proj) -> dict:
    pdir = _pdir(proj)
    title = book_title(proj)
    motifs = title_motifs(title)
    bible_text = _read(pdir, BIBLE_FILE)
    char_text = _read(pdir, CHARACTER_FILE)
    try:
        state = state_mod.load(pdir)
    except Exception:
        state = state_mod.empty_state()
    cards = _load_cards(pdir)
    chapters = _chapters(proj)

    bible_names = _bible_names(bible_text)
    char_names = _names_from_text(char_text)
    state_names = [n for n in (state.get("characters") or {})
                   if state_mod._is_person_key(state_mod._clean_person_name(n))]
    alias = _alias_index(state)

    chapter_text = "\n".join(c["text"] for c in chapters)
    book_mentions = _mentions(chapter_text, list(dict.fromkeys(
        state_names + char_names + list(bible_names) + list(alias))))

    # 登记名是否在正文真实存在（按状态库别名归并）
    registry = {}
    for nm in state_names:
        registry.setdefault(nm, {"name": nm, "sources": ["状态库"], "aliases": []})
    for nm in char_names:
        registry.setdefault(nm, {"name": nm, "sources": [], "aliases": []})["sources"].append("人物档案")
    for nm in bible_names:
        registry.setdefault(nm, {"name": nm, "sources": [], "aliases": []})["sources"].append("故事圣经")
    for nm, rec in registry.items():
        aliases = alias.get(nm, nm)
        rec["aliases"] = sorted({a for a, main in alias.items() if main == aliases and a != nm})
        rec["mentions"] = sum(book_mentions.get(x, 0) for x in [nm] + rec["aliases"])
        rec["chapters"] = sum(1 for c in chapters
                              if any(x in c["text"] for x in [nm] + rec["aliases"]))
    missing = [r for r in registry.values() if r["mentions"] == 0]
    used = [r for r in registry.values() if r["mentions"] > 0]
    used.sort(key=lambda r: -r["mentions"])

    # 正文高频、登记表里没有的名字（典型：主角写成了另一个名字）
    registry_all = set(registry) | set(alias) | set(bible_names) | set(char_names)
    candidates = [(w, k) for w, k in _name_candidates(chapter_text)
                  if w not in registry_all and not any(w in r for r in registry_all)]
    unregistered = [{"name": w, "mentions": k} for w, k in candidates[:10]]

    motif_stats = []
    for m in motifs:
        cnt = chapter_text.count(m)
        chs = sum(1 for c in chapters if m in c["text"])
        motif_stats.append({"motif": m, "mentions": cnt, "chapters": chs,
                            "ratio": round(chs / max(1, len(chapters)), 3)})
    missing_motifs = [x for x in motif_stats if x["mentions"] == 0]
    title_coverage = (round(sum(1 for x in motif_stats if x["mentions"] > 0) / len(motif_stats), 3)
                      if motif_stats else 0.0)

    opponents = _opponents(state, char_text, bible_text)
    for op in opponents:
        op["mentions"] = chapter_text.count(op["name"])
        op["recent_chapters"] = sum(1 for c in chapters[-10:] if op["name"] in c["text"])
    named_opponents = [x for x in opponents if x["mentions"] > 0]

    # 章卡结构
    empty_conflict = [int(c.get("n") or 0) for c in cards
                      if not str(c.get("conflict") or "").strip()]
    empty_goal = [int(c.get("n") or 0) for c in cards if not str(c.get("goal") or "").strip()]
    no_named = []
    for c in cards:
        text = " ".join([str(c.get("conflict") or ""), str(c.get("goal") or "")])
        if not any(n in text for n in registry_all):
            no_named.append(int(c.get("n") or 0))
    cast_miss = []
    for c in cards:
        cast = [str(x) for x in (c.get("cast") or []) if x]
        if cast and not any(x in registry_all for x in cast):
            cast_miss.append(int(c.get("n") or 0))

    issues = []
    if missing_motifs:
        issues.append({
            "level": "high", "code": "K01",
            "message": "书名承诺在正文中零兑现：" + "、".join(x["motif"] for x in missing_motifs),
            "detail": "读者点进来是因为书名里的这几个词；正文一次没写，等于货不对板。",
        })
    if missing:
        issues.append({
            "level": "high", "code": "K02",
            "message": f"{len(missing)} 个登记人物在正文中从未出现：" + "、".join(r["name"] for r in missing[:8]),
            "detail": "圣经/档案与正文是两套人物表；写前事实卡会把不存在的人当禁出场，越写越乱。",
        })
    if unregistered:
        issues.append({
            "level": "warn", "code": "K03",
            "message": "正文高频但未登记的名字：" + "、".join(f"{x['name']}×{x['mentions']}" for x in unregistered[:6]),
            "detail": "这些名字可能是真正的主角/对手，却没有进入状态库与章卡。",
        })
    if empty_conflict:
        issues.append({
            "level": "high", "code": "K04",
            "message": f"{len(empty_conflict)} 张章卡没有冲突：" + "、".join(str(x) for x in empty_conflict[:10]),
            "detail": "没有对手/阻力的章卡，写出来必然是无事件流水账。",
        })
    if no_named:
        issues.append({
            "level": "warn", "code": "K05",
            "message": f"{len(no_named)}/{len(cards)} 张章卡的冲突里没有任何已登记人物",
            "detail": "冲突写「时代压力/环境/局势」而不是具体的人，等于没有可写的对手行动。",
        })
    if not named_opponents and chapters:
        issues.append({
            "level": "high", "code": "K06",
            "message": "全书没有可追踪的具名对手",
            "detail": "对手没有名字、没有目标、没有行动，冲突只能靠环境和巧合推动。",
        })
    elif chapters and all(x.get("recent_chapters", 0) == 0 for x in named_opponents):
        issues.append({
            "level": "warn", "code": "K07",
            "message": "最近 10 章没有任何具名对手出场",
            "detail": "长线对手长期消失，读者会感觉主线断了。",
        })

    decision = "keep"
    if title_coverage < 0.5 and missing_motifs:
        decision = "rename_or_strengthen"
    elif title_coverage < 0.5:
        decision = "strengthen_title_motifs"

    return {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "title": title,
        "title_motifs": motif_stats,
        "title_coverage": title_coverage,
        "title_decision": decision,
        "chapters": len(chapters),
        "book_chars": _cn(chapter_text),
        "registry": sorted(registry.values(), key=lambda r: -r["mentions"]),
        "missing_registry": missing,
        "unregistered_names": unregistered,
        "opponents": opponents,
        "named_opponents": named_opponents,
        "cards": {
            "total": len(cards),
            "empty_goal": empty_goal,
            "empty_conflict": empty_conflict,
            "no_named_actor": no_named,
            "cast_not_in_registry": cast_miss,
        },
        "issues": issues,
        "counts": {
            "high": sum(1 for x in issues if x["level"] == "high"),
            "warn": sum(1 for x in issues if x["level"] == "warn"),
        },
    }


def render_report(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    L = ["# 正典体检（零 token）", ""]
    L.append(f"- 时间：{data.get('at', '')}")
    L.append(f"- 书名：**{data.get('title', '')}**")
    L.append(f"- 正文：{data.get('chapters', 0)} 章 / {data.get('book_chars', 0)} 汉字")
    L.append(f"- 结论：**{data.get('counts', {}).get('high', 0)} 项高风险 · "
             f"{data.get('counts', {}).get('warn', 0)} 项告警**")
    L.append("")
    L.append("## 1) 书名承诺兑现")
    if data.get("title_motifs"):
        L.append("| 书名关键词 | 出现次数 | 覆盖章数 | 覆盖率 |")
        L.append("|---|---|---|---|")
        for x in data["title_motifs"]:
            L.append(f"| {x['motif']} | {x['mentions']} | {x['chapters']} | {x['ratio']:.0%} |")
    else:
        L.append("（书名未切出可检查的关键词）")
    decision = data.get("title_decision")
    L.append("")
    L.append("**处置建议**：" + {
        "rename_or_strengthen": "书名关键词基本没兑现。二选一：① 按正文真实内容改名；② 把关键词写回主线（需要重写，不是加几句口号）。",
        "strengthen_title_motifs": "书名关键词覆盖不足，建议在后续章卡里把它们变成可追踪的目标/对手/事件。",
        "keep": "书名与正文基本一致，保持。",
    }.get(decision, decision or ""))
    L.append("")
    L.append("## 2) 人物登记与正文对账")
    L.append("| 人物 | 登记来源 | 正文提及 | 出现章数 | 别名 |")
    L.append("|---|---|---|---|---|")
    for r in (data.get("registry") or [])[:20]:
        L.append(f"| {r['name']} | {'、'.join(r.get('sources') or []) or '—'} | {r.get('mentions', 0)} "
                 f"| {r.get('chapters', 0)} | {'、'.join(r.get('aliases') or []) or '—'} |")
    if data.get("unregistered_names"):
        L.append("")
        L.append("**正文高频、但未登记的名字**：" + "、".join(
            f"{x['name']}×{x['mentions']}" for x in data["unregistered_names"][:10]))
    L.append("")
    L.append("## 3) 对手清单")
    if data.get("opponents"):
        for x in data["opponents"][:12]:
            L.append(f"- **{x['name']}**（{x['source']}）：正文 {x.get('mentions', 0)} 次，"
                     f"最近 10 章 {x.get('recent_chapters', 0)} 次")
    else:
        L.append("（没有登记任何具名对手）")
    L.append("")
    L.append("## 4) 章卡结构")
    c = data.get("cards") or {}
    L.append(f"- 章卡 {c.get('total', 0)} 张")
    if c.get("empty_goal"):
        L.append(f"- 无目标：第 {'、'.join(str(x) for x in c['empty_goal'][:20])} 章")
    if c.get("empty_conflict"):
        L.append(f"- 无冲突：第 {'、'.join(str(x) for x in c['empty_conflict'][:20])} 章")
    if c.get("no_named_actor"):
        L.append(f"- 冲突无具名人物：{len(c['no_named_actor'])} 张")
    L.append("")
    L.append("## 5) 问题清单")
    if not data.get("issues"):
        L.append("未发现正典级问题。")
    for x in data.get("issues") or []:
        L.append(f"- **[{x.get('level')} · {x.get('code')}]** {x.get('message')}")
        if x.get("detail"):
            L.append(f"  {x['detail']}")
    L.append("")
    L.append("_本报告只做确定性对账，不判断文笔；所有结论都可回到原文核对。_")
    return "\n".join(L)


def run(proj, quiet: bool = False) -> dict:
    data = scan(proj)
    try:
        pdir = _pdir(proj)
        atomic_write_json(os.path.join(pdir, REPORT_JSON), data)
        atomic_write_text(os.path.join(pdir, REPORT_MD), render_report(data) + "\n")
    except Exception:
        pass
    if not quiet:
        try:
            proj.log(f"🧾 正典体检：{data['counts']['high']} 项高风险、"
                     f"{data['counts']['warn']} 项告警（书名覆盖 {data['title_coverage']:.0%}）")
        except Exception:
            pass
    return data


def brief(data: dict) -> str:
    """给对话/体检回执的一行摘要。"""
    if not isinstance(data, dict):
        return ""
    c = data.get("counts") or {}
    return (f"正典体检：书名关键词覆盖 {data.get('title_coverage', 0):.0%}，"
            f"{c.get('high', 0)} 项高风险、{c.get('warn', 0)} 项告警；"
            f"详见 {REPORT_JSON}")


# ---------- 圣经 → 状态库（让事实不再只活在 L4 文本里） ----------

_TABLE_SECTIONS = {
    "title": ("标题承诺", "书名承诺"),
    "opponent": ("对手行动",),
    "emotion": ("情感线",),
    "humanity": ("人性命题",),
}
_CHAPTER_RE = re.compile(r"第?\s*(\d{1,4})\s*章")


def _tables(text: str) -> list[dict]:
    """抽取 markdown 表格：返回 [{heading, header, rows}]。"""
    out, cur_head, header, rows = [], "", None, []
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = _HEADING.match(line)
        if m:
            if header and rows:
                out.append({"heading": cur_head, "header": header, "rows": rows})
            cur_head = re.sub(r"[*_`#]", "", m.group(1)).strip()[:40]
            header, rows = None, []
            continue
        if not line.startswith("|"):
            if header and rows:
                out.append({"heading": cur_head, "header": header, "rows": rows})
                header, rows = None, []
            continue
        cells = [c.strip().strip("*_`") for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
            continue
        if header is None:
            header = cells
        elif any(cells):
            rows.append(cells)
    if header and rows:
        out.append({"heading": cur_head, "header": header, "rows": rows})
    return out


def _section_kind(heading: str) -> str:
    h = heading or ""
    for kind, keys in _TABLE_SECTIONS.items():
        if any(k in h for k in keys):
            return kind
    return ""


def _col(header: list, *keys) -> int:
    for i, h in enumerate(header):
        if any(k in (h or "") for k in keys):
            return i
    return -1


def _cell(row: list, i: int) -> str:
    return str(row[i]).strip() if 0 <= i < len(row) else ""


def _first_chapter(text: str) -> int:
    m = _CHAPTER_RE.search(text or "")
    return int(m.group(1)) if m else 0


def seed_state(proj, chapters: list[dict] | None = None) -> dict:
    """把故事圣经的四张表（标题承诺/对手行动/情感线/人性命题）确定性地写进状态库。

    为什么必须进状态库：故事圣经在上下文里是 L4 层，只有 2000 字预算，常常整段被
    裁掉；状态库的 excerpt 优先级更高，且可以被 factcard / 章卡 / 改写直接引用。
    只在对应表为空时注入，绝不覆盖已经抽取出来的真实事实。

    对手只在正文里真的出现过才登记——否则圣经里那套「另一个人名宇宙」会再次污染状态。
    返回 {"promises": n, "opponents": n, "humanity": n, "skipped": reason}。
    """
    pdir = _pdir(proj)
    try:
        st = state_mod.load(pdir)
    except Exception:
        return {"promises": 0, "opponents": 0, "humanity": 0, "skipped": "state-unavailable"}
    bible = _read(pdir, BIBLE_FILE)
    if not bible.strip():
        return {"promises": 0, "opponents": 0, "humanity": 0, "skipped": "no-bible"}
    chapters = chapters if isinstance(chapters, list) else _chapters(proj)
    body = "\n".join(str(c.get("text") or "") for c in chapters)
    motifs = title_motifs(book_title(proj))

    tables = _tables(bible)
    added = {"promises": 0, "opponents": 0, "humanity": 0, "skipped": ""}
    now = datetime.date.today().isoformat()

    if not (st.get("promises") or {}):
        for motif in motifs:
            key = f"title:{motif}"
            st["promises"][key] = {
                "text": f"书名承诺「{motif}」必须在正文靠具体事件兑现",
                "kind": "title", "status": "open", "importance": "A",
                "notes": f"seed:{now}"}
            added["promises"] += 1
        for tb in tables:
            if _section_kind(tb["heading"]) != "title":
                continue
            h = tb["header"]
            i_kw = _col(h, "关键词", "承诺", "母题", "书名", "标题")
            i_why = _col(h, "兑现", "事件", "方式", "做法")
            i_from = _col(h, "最早", "起始", "开始")
            i_to = _col(h, "最晚", "截止", "结束")
            for row in tb["rows"]:
                kw = _cell(row, i_kw)
                if not kw:
                    continue
                key = f"title:{kw[:40]}"
                if key in st["promises"]:
                    continue
                st["promises"][key] = {
                    "text": f"书名承诺「{kw}」" + (f"：{_cell(row, i_why)}" if i_why >= 0 else ""),
                    "kind": "title", "status": "open", "importance": "A",
                    "due_from": _first_chapter(_cell(row, i_from)) if i_from >= 0 else 0,
                    "due_to": _first_chapter(_cell(row, i_to)) if i_to >= 0 else 0,
                    "notes": f"seed:{now}"}
                added["promises"] += 1

    if not (st.get("opponents") or {}):
        for tb in tables:
            if _section_kind(tb["heading"]) != "opponent":
                continue
            h = tb["header"]
            i_name = _col(h, "对手", "人物", "名字", "谁")
            i_goal = _col(h, "要什么", "目标", "目的", "诉求")
            i_press = _col(h, "施压", "手段", "做法", "方法")
            i_first = _col(h, "首次", "第一次", "出手", "章")
            for row in tb["rows"]:
                name = re.sub(r"[（(].*?[）)]", "", _cell(row, i_name)).strip()
                if not (2 <= len(name) <= 12):
                    continue
                if body and name not in body:
                    continue           # 圣经里的幽灵人物，不进状态
                st["opponents"][name] = {
                    "name": name,
                    "goal": _cell(row, i_goal) if i_goal >= 0 else "",
                    "pressure": _cell(row, i_press) if i_press >= 0 else "",
                    "status": "active",
                    "first_seen": _first_chapter(_cell(row, i_first)) if i_first >= 0 else 0,
                    "notes": f"seed:{now}"}
                added["opponents"] += 1

    if not (st.get("humanity") or []):
        for tb in tables:
            if _section_kind(tb["heading"]) != "humanity":
                continue
            h = tb["header"]
            i_q = _col(h, "问题", "命题", "两难", "困境")
            i_c = _col(h, "选择", "做法")
            i_cost = _col(h, "代价", "失去")
            for row in tb["rows"]:
                q = _cell(row, i_q)
                if not q:
                    continue
                st["humanity"].append({
                    "chapter": _first_chapter(_cell(row, i_c)) or 1,
                    "dilemma": q, "choice": _cell(row, i_c),
                    "cost": _cell(row, i_cost), "value": "",
                    "evidence": f"seed:{now}"})
                added["humanity"] += 1

    if added["promises"] or added["opponents"] or added["humanity"]:
        try:
            state_mod.save(pdir, st)
        except Exception:
            added["skipped"] = "save-failed"
    return added