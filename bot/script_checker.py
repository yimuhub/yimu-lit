"""剧本时代/世界观一致性检查器（v2.2.0 · P3）

缺陷背景：剧本引擎早先不读创作圣经与时代设定，场次正文会整段串到别的时代
（典型：古代喜剧写出手机、医院）。本模块提供**确定性**（纯词表、零 LLM）的
时代判定与串代扫描，供 script_writer 在 S4 场次表与 S5 正文落盘前拦截。

口径：
  - guess_era 只做古/今词表命中数投票；判不出返回 ""，调用方据此不做拦截（避免误伤）。
  - forbidden_terms 按时代返回「不该出现」的词：古代 → 现代词表；现代 → 古代词表；
    民国 → 强现代/近未来词表（民国可以有汽车、电话，但不能有手机、扫码、AI）；
    近未来/架空/自定义/"" → 空表（虚拟世界规则由用户的世界观自定）。
  - scan 合并同一 term 的出现次数，并给出首次命中前后 18 字的 snippet，便于反馈重写。
"""
from __future__ import annotations

import json
import os
import re

ERA_LABELS = {
    "ancient": "古代",
    "republic": "民国",
    "modern": "当代",
    "future": "近未来",
    "fantasy": "架空",
    "custom": "自定义",
}

# 现代/当代词表：覆盖器物、通讯、交通、职业、商业与网络生活。
# 顺序即 _world_block 的列举顺序（前 20 个直接进提示词）。
MODERN_TERMS = [
    "手机", "微信", "电脑", "网络", "视频", "直播", "咖啡", "医院", "地铁", "公交",
    "高铁", "汽车", "飞机", "电视", "空调", "电梯", "公司", "老板", "合同", "律师",
    "警察", "电话", "相机", "外卖", "超市", "小区", "公寓", "办公室", "会议室", "CEO",
    "KPI", "APP", "快递", "程序员", "共享单车", "网约车",
    # 补充：常见的强现代器物与网络生活（排在后面，不挤占提示词前 20）
    "互联网", "二维码", "扫码", "支付宝", "银行卡", "信用卡", "火车", "出租车",
    "冰箱", "洗衣机", "微波炉", "商场", "奶茶", "网红", "朋友圈", "抖音", "微博",
    "短信", "充电", "笔记本电脑", "平板电脑",
]

# 古代词表：器物、身份、场所、科举与官衙。
ANCIENT_TERMS = [
    "马车", "轿子", "客栈", "衙门", "官府", "银两", "铜钱", "令牌", "更鼓", "灯笼",
    "里正", "捕快", "丫鬟", "小厮", "娘子", "相公", "掌柜", "店小二", "酒楼", "茶楼",
    "布庄", "县衙", "京城", "科举", "秀才", "举人", "县太爷", "官差", "马厩",
    # 补充：高频古代场景词，帮助 guess_era 在短文本上也可判定
    "驿站", "镖局", "当铺", "朝堂", "陛下", "皇上", "太监", "轿夫",
]

# 民国仍可有汽车/电话/咖啡，故只拦「近未来 + 强现代」物种。
STRONG_MODERN_TERMS = [
    "手机", "智能手机", "微信", "电脑", "互联网", "网络", "视频", "直播", "短视频",
    "外卖", "快递", "共享单车", "网约车", "高铁", "地铁", "扫码", "二维码",
    "移动支付", "支付宝", "微信支付", "人工智能", "AI", "5G", "元宇宙", "无人机",
    "电商", "网购", "APP", "小程序", "直播带货", "电动汽车", "平板电脑", "网红",
    "抖音", "微博", "KPI", "CEO",
]


def _spans(text_low: str, term: str) -> list[int]:
    """返回 term 在 text_low 中的全部起点；纯 ASCII 词加词边界，避免 AI 命中 said 这类误伤。"""
    t = term.lower()
    if t.isascii() and t.isalpha():
        pat = re.compile(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])")
    else:
        pat = re.compile(re.escape(t))
    return [m.start() for m in pat.finditer(text_low)]


def _count(text: str, term: str) -> int:
    return len(_spans(text.lower(), term))


def guess_era(text: str) -> str:
    """按古/今词表命中数投票。平票或全未命中返回 ""（不猜）。"""
    s = text or ""
    if not s.strip():
        return ""
    low = s.lower()
    ancient = sum(len(_spans(low, t)) for t in ANCIENT_TERMS)
    modern = sum(len(_spans(low, t)) for t in MODERN_TERMS)
    if ancient > modern:
        return "ancient"
    if modern > ancient:
        return "modern"
    return ""


def forbidden_terms(era: str) -> list[str]:
    """该时代禁止出现的词；未知/虚拟时代返回空表（不做拦截，避免误伤）。"""
    era = (era or "").strip()
    if era == "ancient":
        return list(MODERN_TERMS)
    if era == "republic":
        return list(STRONG_MODERN_TERMS)
    if era == "modern":
        return list(ANCIENT_TERMS)
    return []


def scan(text: str, era: str) -> list[dict]:
    """扫描违规词：同一 term 合并计数，snippet 取首次命中前后 18 字。"""
    s = text or ""
    if not s.strip():
        return []
    low = s.lower()
    hits: list[tuple[int, dict]] = []
    for term in forbidden_terms(era):
        pos = _spans(low, term)
        if not pos:
            continue
        first = pos[0]
        snippet = s[max(0, first - 18): first + len(term) + 18].replace("\n", " ")
        hits.append((first, {"term": term, "count": len(pos), "snippet": snippet}))
    hits.sort(key=lambda x: x[0])
    return [h for _, h in hits]


def check_text(text: str, era: str) -> dict:
    """单段文本校验：{"ok", "issues", "era"}。era 为空时直接放行。"""
    era = (era or "").strip()
    issues = scan(text, era) if era else []
    return {"ok": not issues, "issues": issues, "era": era}


def check_project(key: str) -> dict:
    """整项目体检：读计划 / 场次清单 / 圣经 / 各场正文，逐场标出串代。"""
    from . import script_writer as sw  # 延迟导入：script_writer 依赖本模块，避免循环导入

    pdir = sw.project_dir(key)
    empty = {"ok": False, "era": "", "items": [],
             "summary": {"scenes": 0, "flagged": 0, "terms": {}}}
    if not os.path.isdir(pdir):
        return {**empty, "error": "项目不存在"}
    try:
        plan = json.load(open(os.path.join(pdir, sw.F_PLAN), encoding="utf-8"))
    except Exception:
        plan = {}

    bible = sw._read(os.path.join(pdir, sw.F_BIBLE))
    scenes_md = sw._read(os.path.join(pdir, sw.F_SCENES))
    setting = (plan.get("meta") or {}).get("setting") or {}
    era = (setting.get("era") or "").strip()
    if not era:
        # 旧项目没有 meta.setting：沿用写作引擎同一套判定（标题/题材优先，再词表投票），
        # 否则圣经里现代词一多，会把古代剧误判成当代而漏报。
        cfg = dict(plan.get("config") or {})
        cfg.setdefault("title", plan.get("scriptName") or "")
        era = sw._era_of(cfg) or guess_era(bible + "\n" + scenes_md)

    items: list[dict] = []
    terms: dict[str, int] = {}
    for idx, sc in enumerate(plan.get("scenes") or [], 1):
        try:
            no = int(sc.get("no") or idx)
        except (TypeError, ValueError):
            no = idx
        fn = sc.get("fileName") or f"第{no:03d}场.md"
        fn = os.path.basename(str(fn).strip()) or f"第{no:03d}场.md"
        body = sw._read(os.path.join(pdir, sw.F_BODY, fn))
        issues = scan(body, era) if era else []
        for it in issues:
            terms[it["term"]] = terms.get(it["term"], 0) + it["count"]
        items.append({
            "scene": no, "fileName": fn, "issues": issues,
            "words": len(re.findall(r"[\u4e00-\u9fff]", body)),
        })

    flagged = sum(1 for it in items if it["issues"])
    return {
        "ok": flagged == 0, "era": era, "items": items,
        "summary": {"scenes": len(items), "flagged": flagged, "terms": terms},
    }
