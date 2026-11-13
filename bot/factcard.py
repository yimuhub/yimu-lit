"""本章事实卡（v4.36.0）：写之前把「这一章的事实边界」钉死。

要解决的问题
------------
真实项目里最有代表性的事故是：人物档案写的是「沈知微」，正文写的是「沈清漪」，
第 1–8 章全中；第 63 章把李承乾叫成「四哥」；第 68 章人物从城外一步跨进宫里。
这些都不是文笔问题，是**写的时候没有人告诉模型「此刻的事实是什么」**。

此前的做法是「写完再查」：抽取 delta → 合并状态 → 复审冲突。
但抽取本身也要模型，模型同样会漏；而且错误已经进了正文，查出来只能重写。
事实卡把这一步前移：**写之前**给出一份确定性生成的边界清单，成本为零。

三个边界
--------
1. 谁能出场：已死 / 尚未登场 / 已离场的名字列成禁出场名单；
2. 该兑现什么：到期与逾期的伏笔必须在本章处理，不再是「写完了才提醒」；
3. 能新增多少：单章最多引入 N 个新专名——这一条直接压制「每章起一堆新名字」的
   稀释行为，也是账本膨胀的源头之一。

写后校验
--------
`validate()` 做确定性回查：已死人物是否出场、未登场人物是否提前出现、
主角名是否发生同姓漂移。所有结论都带原文证据，可回原文核对。

纯函数、零 LLM。
"""
from __future__ import annotations

import os
import re

from . import state as state_mod

# 死亡/退场语义（从自由文本 status 里识别；状态库没有独立的生死字段）
_DEAD = ("已死", "死亡", "身亡", "阵亡", "殁", "被杀", "自尽", "殉", "伏诛", "斩首", "死去")
_LEFT = ("离京", "离开", "远走", "流放", "外放", "失踪", "退隐", "幽禁")
_ALIVE = ("在世", "健在", "存活", "活着")

# 常见姓氏：只对「高频出现的姓」做漂移检测，避免把「沈」「李」之外的普通词当人名
_SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程"
    "嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧"
    "隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶"
    "幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴鬱胥能苍双闻"
    "莘党翟谭贡劳逄姬申扶堵冉宰郦雍卻璩桑桂濮牛寿通边扈燕冀郏浦尚农温"
    "别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡"
    "国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾"
    "毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
)


def _pdir(proj) -> str:
    return os.path.abspath(getattr(proj, "path", "") or "")


def _is_dead(status: str) -> bool:
    s = status or ""
    if any(w in s for w in _ALIVE):
        return False
    return any(w in s for w in _DEAD)


def _is_gone(status: str) -> bool:
    return any(w in (status or "") for w in _LEFT)


def _names_of(name: str, rec: dict) -> list:
    out = [name]
    for a in (rec.get("aliases") or []):
        a = str(a).strip()
        if 1 < len(a) <= 8:
            out.append(a)
    return out


def build(proj, no: int, cfg=None, card: dict | None = None) -> dict:
    """生成本章事实卡（确定性）。任何读取失败都降级为空卡，不阻断写作。

    `card` 可由调用方传入已算好的章卡，避免与 context.build 重复计算。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    n = max(1, int(no or 1))
    form = str(cfg.get("form") or "serial")
    new_name_limit = 1 if form.lower().startswith("serial") else 2

    st = {}
    try:
        st = state_mod.load(_pdir(proj))
    except Exception:
        st = {}

    chars = st.get("characters") or {}
    allowed, forbidden, table = [], [], {}
    for name, rec in chars.items():
        rec = rec if isinstance(rec, dict) else {}
        nm = state_mod._clean_person_name(name)
        if not state_mod._is_person_key(nm):
            continue
        status = str(rec.get("status") or "")
        aliases = _names_of(nm, rec)
        # 注意：**不用 first_seen 判断「尚未登场」**。状态库的 first_seen 记录的是
        # 「第一次被写进账本」的章号，不是「正文里第一次出场」——一个第 30 章就出场、
        # 却在第 100 章才被抽取进状态的配角，first_seen 会是 100。用它做禁出场判定，
        # 会把主角本人在第 1 章判成「第 100 章才登场」。
        if _is_dead(status):
            forbidden.append({"name": nm, "reason": f"已退场（{status[:16]}）", "names": aliases})
        elif _is_gone(status):
            forbidden.append({"name": nm, "reason": f"当前不在场（{status[:16]}）", "names": aliases})
        elif any(w in status for w in ("未登场", "尚未出场", "未出场", "未出场")):
            forbidden.append({"name": nm, "reason": "尚未登场", "names": aliases})
        else:
            allowed.append(nm)
            if aliases[1:]:
                table[nm] = {"aliases": aliases[1:], "status": status[:24],
                             "location": str(rec.get("location") or "")[:16]}

    # 章卡点名要出场的人：从禁出场名单里移除，但保留提示（作者要为此写交代）
    card_chars = []
    c = card if isinstance(card, dict) else {}
    try:
        if not c:
            from . import longform
            c = longform.build_chapter_card(proj, n, cfg) or {}
        card_chars = [str(x) for x in (c.get("characters") or []) if x]
        focus = ((c.get("state_focus") or {}).get("characters") or [])
        card_chars += [str(x.get("name")) for x in focus if isinstance(x, dict) and x.get("name")]
    except Exception:
        c = {}
    if card_chars:
        names_set = set(card_chars)
        kept = []
        for f in forbidden:
            if f["name"] in names_set:
                f = dict(f)
                f["reason"] += "，但章卡要求出场——必须写明「如何重新出现」"
                kept.append(f)
            else:
                kept.append(f)
        forbidden = kept

    # 到期 / 逾期伏笔
    due = {"due": [], "overdue": [], "upcoming": []}
    try:
        due = state_mod.foreshadow_due(st, n)
    except Exception:
        pass
    must_pay = [
        {"id": f.get("id"), "desc": str(f.get("description") or "")[:60],
         "due_to": f.get("due_to")}
        for f in (due.get("overdue") or []) + (due.get("due") or [])
    ][:8]

    items = []
    for nm, rec in list((st.get("items") or {}).items())[:12]:
        rec = rec if isinstance(rec, dict) else {}
        holder = str(rec.get("holder") or rec.get("owner") or "")
        if holder:
            items.append({"name": nm, "holder": holder})

    return {
        "chapter": n,
        "form": form,
        "allowed": allowed[:24],
        "forbidden": forbidden[:16],
        "alias_table": table,
        "must_pay": must_pay,
        "items": items,
        "new_name_limit": new_name_limit,
        "focus": card_chars[:8],
        "card": c if isinstance(c, dict) else {},
    }


def render(fc: dict) -> str:
    """把事实卡渲染成写前注入的文本块；空卡返回空串（不占预算）。"""
    if not isinstance(fc, dict) or not fc:
        return ""
    n = fc.get("chapter")
    lines = [f"【本章事实卡（第{n}章，写前必读；与本卡冲突即为错稿）】"]

    if fc.get("allowed"):
        lines.append("- 可出场人物：" + "、".join(fc["allowed"][:16]))
    if fc.get("forbidden"):
        bits = [f"{f['name']}（{f['reason']}）" for f in fc["forbidden"][:10]]
        lines.append("- **禁出场**：" + "；".join(bits)
                     + "。除非本章明确写了他们如何重新出现，否则一个字都不能提他们在场。")
    tab = fc.get("alias_table") or {}
    if tab:
        rows = []
        for name, info in list(tab.items())[:8]:
            al = "、".join(info.get("aliases") or [])
            rows.append(f"{al}＝{name}")
        if rows:
            lines.append("- 称谓对照（写错即穿帮）：" + "；".join(rows)
                         + "。同一人物全章只用一种称呼，不要混用。")
    if fc.get("must_pay"):
        bits = [f"[{m.get('id')}] {m.get('desc')}（计划第{m.get('due_to')}章）"
                for m in fc["must_pay"][:6]]
        lines.append("- 本章必须处理或明确推迟的伏笔：" + "；".join(bits))
    if fc.get("items"):
        bits = [f"{i['name']}→{i['holder']}" for i in fc["items"][:8]]
        lines.append("- 关键物品当前持有人：" + "；".join(bits) + "。物品不能凭空易手。")
    limit = fc.get("new_name_limit")
    if limit:
        lines.append(f"- 本章最多新增 {limit} 个有名字的新角色/新专名。"
                     "能用「一个内侍」「那名校尉」指代的，就不要起名字。")
    lines.append("- 事实优先级：本卡 > 章卡 > 你的记忆。卡里说不在场就是不在场。")
    return "\n".join(lines)


# ---------- 写后回查 ----------

def _find_names(text: str, names: list) -> list:
    hits = []
    for nm in names:
        if not nm or len(nm) < 2:
            continue
        if nm in text:
            hits.append(nm)
    return hits


def _edit_le1(a: str, b: str) -> bool:
    """两字符串编辑距离是否 ≤1（同长度则允许一处替换）。"""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b, strict=False) if x != y) <= 1
    if len(a) > len(b):
        a, b = b, a
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            diff += 1
            j += 1
            if diff > 1:
                return False
    return True


def validate(fc: dict, text: str, n: int = 0) -> list:
    """写后回查：已死/未登场人物是否出场、主角名是否同姓漂移。

    返回 [{level, code, message, evidence}]，全部可回原文核对。
    """
    out = []
    if not isinstance(fc, dict) or not text:
        return out
    body = text or ""

    for f in (fc.get("forbidden") or []):
        if "章卡要求出场" in str(f.get("reason") or ""):
            continue
        hits = _find_names(body, f.get("names") or [f.get("name")])
        if hits:
            ev = ""
            m = re.search(re.escape(hits[0]), body)
            if m:
                ev = body[max(0, m.start() - 24):m.end() + 24].replace("\n", " ")
            out.append({"level": "block", "code": "F01",
                        "message": f"禁出场人物「{hits[0]}」出现在正文（{f.get('reason')}）",
                        "evidence": ev[:90]})

    # 主角名漂移：取正文出现最多的 3 个「姓+名」，与登记名比对
    out.extend(_name_drift(fc, body))
    return out


def _name_drift(fc: dict, body: str) -> list:
    """同姓近名漂移检测：抓「沈知微 / 沈清漪」这类一处之差的错名。"""
    allowed = set(fc.get("allowed") or [])
    for info in (fc.get("alias_table") or {}).values():
        allowed.update(info.get("aliases") or [])
    if not allowed:
        return []
    # 正文里的候选：姓 + 1~2 字
    cand = {}
    for m in re.finditer(r"[\u4e00-\u9fff]{2,3}", body):
        w = m.group(0)
        if w[0] not in _SURNAMES:
            continue
        cand[w] = cand.get(w, 0) + 1
    # 只对「出现 ≥5 次」的高频名做锚点，避免把路人当主角
    anchors = sorted((w for w, c in cand.items() if c >= 5 and w not in allowed),
                     key=lambda w: -cand[w])[:3]
    out = []
    for a in anchors:
        for name in allowed:
            if len(name) != len(a) or a[0] != name[0]:
                continue
            if _edit_le1(a, name):
                continue
            # 同姓、同长度、差两个字：疑似把主角名写成了另一个名字
            if sum(1 for x, y in zip(a, name, strict=False) if x != y) >= 2 and cand.get(a, 0) >= 2:
                m = re.search(re.escape(a), body)
                ev = body[max(0, m.start() - 20):m.end() + 20].replace("\n", " ") if m else ""
                out.append({"level": "warn", "code": "F02",
                            "message": f"疑似人名漂移：正文出现「{a}」（{cand[a]} 次），"
                                       f"登记名是「{name}」",
                            "evidence": ev[:90]})
                break
    return out


def report(proj, chapters, cfg=None) -> dict:
    """全书事实回查：逐章跑 validate，汇总成可读结论（零 token）。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    findings = []
    for ch in chapters or []:
        n = int(ch.get("n") or 0)
        try:
            fc = build(proj, n, cfg)
            for f in validate(fc, ch.get("text") or "", n):
                f["chapter"] = n
                findings.append(f)
        except Exception:
            continue
    blocks = [f for f in findings if f["level"] == "block"]
    return {
        "findings": findings,
        "counts": {"block": len(blocks), "warn": len(findings) - len(blocks)},
        "blocked_chapters": sorted({f["chapter"] for f in blocks}),
    }
