"""长篇内核：层次规划 + 章卡 + 滚动窗口 + 重规划提案 + 摘要抽取 + 确定性审计。

为什么需要它：旧 `02-写作计划.json` 是扁平章表，300 章以上无法表达"卷/弧/阶段目标"，
上下文只能整文件注入后随意截断。这里把规划拆成三层（总规划 02c / 章卡 02d / 权威状态 18），
让 100–500 万字（300–1500 章）的写作循环可以：
  * 只读未来 10–20 章的章卡；
  * 每章写后把事实增量并入状态库；
  * 每 10 章/弧边界/伏笔逾期时滚动重规划；
  * 重规划先出提案（02e），默认不碰已写章节。

项目目录下的数据文件：
  02c-总规划.json      顶层规划（KB 级，不随章节线性膨胀）
  02d-章卡.jsonl       每章一行章卡（按章号索引，避免整文件读写）
  02e-规划提案/<id>.json  未应用的重规划提案
  20-卷摘要.md         滚动卷级摘要
  21-章摘要.jsonl      每章一行摘要（含伏笔操作/质量信号）
  22-重规划记录.jsonl  提案/应用/驳回审计

LLM 只出现在 plan_book 命名、plan_window、replan、summarize_and_extract 四处；
所有调用都有 stage/max_tokens，返回空/坏结构一律走确定性兜底，绝不阻断写作。
"""
from __future__ import annotations

import copy
import datetime
import json
import math
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

from . import llm, state as state_mod
from .safeio import atomic_write_json, atomic_write_text, safe_join

PLAN_FILE = "02c-总规划.json"
CARD_FILE = "02d-章卡.jsonl"
PROPOSAL_DIR = "02e-规划提案"
REPLAN_LOG = "22-重规划记录.jsonl"
VOLUME_SUMMARY = "20-卷摘要.md"
CHAPTER_SUMMARY = "21-章摘要.jsonl"
FLAT_PLAN = "02-写作计划.json"

WORDS_PER_CHAPTER = 3333       # 100 万字≈300 章、500 万字≈1500 章
VOLUME_TARGET = 45             # 每卷 30–60 章，取中位
ARC_TARGET = 11                # 每弧 8–15 章

_CARD_FIELDS = ("title", "goal", "conflict", "turn", "payoff", "cliffhanger",
                "foreshadow_setup", "foreshadow_payoff", "cast", "locations", "word_target")
_MAX_PROPOSALS = 200


# ---------- 基础工具 ----------

def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _text(v, cap: int = 600) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            v = json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            v = str(v)
    return re.sub(r"\s+", " ", str(v)).strip()[:cap]


def _list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    return [] if v in (None, "") else [v]


def _str_list(v, cap: int = 80, limit: int = 20) -> list[str]:
    out, seen = [], set()
    for x in _list(v):
        s = _text(x, cap)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _clean_title(s, fallback: str = "") -> str:
    t = re.sub(r"[#*`>\[\]]", "", _text(s, 120)).strip()
    t = re.sub(r"\s+", " ", t)
    return t or fallback


def _safe_filename(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "", title or "")[:60]


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def _read_text(path: str, cap: int = 300000) -> str:
    try:
        return open(path, encoding="utf-8").read()[:cap]
    except OSError:
        return ""


def _pdir(proj) -> str:
    return os.path.abspath(getattr(proj, "path", "") or ".")


def _read_plan(proj) -> dict:
    p = _read_json(os.path.join(_pdir(proj), PLAN_FILE), {})
    return p if isinstance(p, dict) else {}


def _write_plan(proj, plan: dict) -> None:
    plan["updated_at"] = _now()
    atomic_write_json(os.path.join(_pdir(proj), PLAN_FILE), plan)


def _load_cards(proj) -> dict[int, dict]:
    path = os.path.join(_pdir(proj), CARD_FILE)
    cards: dict[int, dict] = {}
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return cards
    for line in lines:
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            card = _norm_card(obj)
            if card["n"] > 0:
                cards[card["n"]] = card
    return cards


def _write_cards(proj, cards: dict[int, dict]) -> None:
    lines = []
    for n in sorted(cards):
        card = _norm_card(cards[n], n)
        lines.append(json.dumps(card, ensure_ascii=False, separators=(",", ":")))
    atomic_write_text(os.path.join(_pdir(proj), CARD_FILE), "\n".join(lines) + ("\n" if lines else ""))


def _write_one_card_on_disk(proj, card: dict) -> None:
    """单章卡更新：内存有全部卡时整写；此函数仅作为兜底接口。"""
    cards = _load_cards(proj)
    cards[card["n"]] = _norm_card(card)
    _write_cards(proj, cards)


def _norm_status_card(v) -> str:
    s = _text(v, 20).lower()
    if s in ("completed", "done", "完成", "已写", "finished"):
        return "done"
    if s in ("writing", "in_progress", "进行中", "写作中"):
        return "writing"
    if s in ("changed", "已改"):
        return "changed"
    return "planned"


def _norm_card(raw, n: int = 0) -> dict:
    f = raw if isinstance(raw, dict) else {}
    num = _int(f.get("n") or f.get("chapterNumber") or n, n)
    return {
        "n": num,
        "volume": max(0, _int(f.get("volume"), 0)),
        "arc": max(0, _int(f.get("arc"), 0)),
        "title": _clean_title(f.get("title"), f"第{num}章" if num else ""),
        "goal": _text(f.get("goal"), 500),
        "conflict": _text(f.get("conflict"), 500),
        "turn": _text(f.get("turn"), 500),
        "payoff": _text(f.get("payoff"), 500),
        "cliffhanger": _text(f.get("cliffhanger"), 300),
        "foreshadow_setup": _str_list(f.get("foreshadow_setup"), 80, 12),
        "foreshadow_payoff": _str_list(f.get("foreshadow_payoff"), 80, 12),
        "cast": _str_list(f.get("cast"), 40, 16),
        "locations": _str_list(f.get("locations"), 40, 12),
        "word_target": max(0, _int(f.get("word_target"), 0)),
        "status": _norm_status_card(f.get("status")),
        "updated_at": _text(f.get("updated_at"), 40),
    }


def _empty_card(n: int, volume: int = 0, arc: int = 0) -> dict:
    return _norm_card({"n": n, "volume": volume, "arc": arc, "title": f"第{n}章",
                       "status": "planned"}, n)


# ---------- 规模与切分 ----------

SCALE_PRESETS = {
    "short": {"target_words": 300_000, "chapters": 100},
    "1m": {"target_words": 1_000_000, "chapters": 300},
    "3m": {"target_words": 3_000_000, "chapters": 900},
    "5m": {"target_words": 5_000_000, "chapters": 1500},
}


def resolve_scale(cfg: dict, flat: dict | None = None) -> dict:
    """把用户配置/旧计划折算成 {target_words, chapters, volumes}，全部有安全默认。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    raw_scale = cfg.get("scale")
    preset = SCALE_PRESETS.get(str(raw_scale).strip().lower()) if isinstance(raw_scale, str) else None
    sc = raw_scale if isinstance(raw_scale, dict) else (preset or {})
    target = _int(sc.get("target_words") or cfg.get("target_words") or cfg.get("targetWords"), 0)
    chapters = _int(sc.get("chapters") or cfg.get("chapters") or cfg.get("totalChapters"), 0)
    flat = flat if isinstance(flat, dict) else {}
    if not chapters:
        chapters = _int(flat.get("totalChapters"), 0) or len(flat.get("chapters") or [])
    if not target and chapters:
        target = chapters * WORDS_PER_CHAPTER
    if not chapters and target:
        chapters = max(1, round(target / WORDS_PER_CHAPTER))
    if not chapters:
        chapters = 10
    if not target:
        target = chapters * WORDS_PER_CHAPTER
    chapters = max(1, min(20000, chapters))
    target = max(chapters * 300, target)
    vols = _int(sc.get("volumes") or cfg.get("volumes"), 0)
    return {"target_words": target, "chapters": chapters,
            "volumes": vols or len(_split_ranges(chapters, VOLUME_TARGET, 30, 60))}


def _split_ranges(total: int, target: int, min_size: int, max_size: int) -> list[tuple[int, int]]:
    """把 1..total 均分成尽量均匀的区间，尽量落在 [min_size, max_size]。"""
    total = max(1, _int(total, 1))
    n = max(1, round(total / max(1, target)))

    def _size(k: int) -> int:
        return math.ceil(total / k)

    while _size(n) > max_size:
        n += 1
    while n > 1 and _size(n) < min_size and _size(n - 1) <= max_size:
        n -= 1
    base, rem = divmod(total, n)
    ranges, pos = [], 1
    for i in range(n):
        size = base + (1 if i < rem else 0)
        if size <= 0:
            continue
        ranges.append((pos, pos + size - 1))
        pos += size
    return ranges or [(1, total)]


_CN = "零一二三四五六七八九"


def _cn_no(n: int) -> str:
    n = _int(n, 0)
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN[n]
    if n < 20:
        return "十" + (_CN[n % 10] if n % 10 else "")
    if n < 100:
        return _CN[n // 10] + "十" + (_CN[n % 10] if n % 10 else "")
    if n < 1000:
        rest = n % 100
        head = _CN[n // 100] + "百"
        if rest == 0:
            return head
        return head + ("零" if rest < 10 else "") + _cn_no(rest)
    return str(n)


_VOL_WORDS = [
    "起势", "入局", "暗涌", "裂变", "远征", "迷局", "争锋", "转折", "低谷", "反攻",
    "登临", "风雷", "危局", "破晓", "归墟", "余烬", "新章", "长夜", "曙光", "潮涌",
    "逆流", "孤城", "星火", "深渊", "彼岸", "天堑", "残局", "旧梦", "远山", "风起",
    "云涌", "归途", "破阵", "重光", "无声", "惊雷", "落子", "终局", "余波", "回响",
]
_ARC_WORDS = ["破局", "试探", "交锋", "转机", "危机", "反击", "真相", "抉择", "收束", "余波", "暗线", "合流"]

# v2.5.0: 多套题材化弧模板——每本书根据题材选择不同的弧名序列，不再千篇一律
_ARC_TEMPLATES = {
    "fantasy": ["觉醒", "试炼", "结盟", "异变", "鏖战", "觉醒", "破围", "代价", "逆转", "飞升", "暗涌", "收束"],
    "urban":   ["初遇", "试探", "入局", "暗流", "对峙", "抉择", "撕裂", "弥合", "真相", "释然", "余温", "归途"],
    "mystery": ["迷雾", "线索", "误导", "逼近", "反转", "困局", "破壁", "拼图", "真相", "清算", "余波", "回响"],
    "romance": ["邂逅", "心动", "试探", "误会", "靠近", "甜蜜", "裂痕", "放手", "挽回", "坦诚", "相依", "圆满"],
    "history": ["起兵", "布局", "征伐", "权谋", "变局", "险胜", "扩张", "内乱", "中兴", "鼎盛", "衰落", "回响"],
    "scifi":   ["发现", "探索", "接触", "异变", "危机", "抉择", "对抗", "牺牲", "突破", "重构", "新序", "余烬"],
    "default": ["破局", "试探", "交锋", "转机", "危机", "反击", "真相", "抉择", "收束", "余波", "暗线", "合流"],
}

# v2.5.0: 多套题材化卷名模板
_VOL_TEMPLATES = {
    "fantasy": ["天命", "风云", "龙渊", "烽火", "帝业", "逆天", "苍穹", "归墟", "劫火", "涅槃"],
    "urban":   ["初入", "浮沉", "暗涌", "博弈", "风云", "蜕变", "逆行", "归途", "余烬", "重生"],
    "mystery": ["迷局", "暗线", "拼图", "疑云", "深渊", "破晓", "真相", "清算", "余音", "落幕"],
    "romance": ["初春", "盛夏", "秋意", "冬暖", "重逢", "心语", "承诺", "同行", "余生", "圆满"],
    "history": ["起势", "逐鹿", "鼎革", "治世", "乱世", "中兴", "鼎盛", "风雨", "残阳", "回望"],
    "scifi":   ["启航", "星海", "接触", "裂变", "暗物质", "奇点", "维度", "坍缩", "新生", "余烬"],
    "default": ["起势", "入局", "暗涌", "裂变", "远征", "迷局", "争锋", "转折", "低谷", "反攻"],
}


def _arc_words_for(cfg: dict) -> list:
    """v2.5.0: 根据题材/类型选择弧名模板，避免所有作品千篇一律。"""
    genre = ""
    if isinstance(cfg, dict):
        genre = str(cfg.get("genre") or cfg.get("type") or cfg.get("category") or "").lower()
    for key in _ARC_TEMPLATES:
        if key in genre:
            return _ARC_TEMPLATES[key]
    return _ARC_TEMPLATES["default"]


def _vol_words_for(cfg: dict) -> list:
    """v2.5.0: 根据题材/类型选择卷名模板。"""
    genre = ""
    if isinstance(cfg, dict):
        genre = str(cfg.get("genre") or cfg.get("type") or cfg.get("category") or "").lower()
    for key in _VOL_TEMPLATES:
        if key in genre:
            return _VOL_TEMPLATES[key]
    return _VOL_TEMPLATES["default"]
_STAGES = [(0.0, "开局铺陈"), (0.15, "试炼升级"), (0.35, "全面对抗"),
           (0.55, "至暗低谷"), (0.72, "绝地反攻"), (0.88, "终局收束")]


def _stage_label(ratio: float) -> str:
    label = _STAGES[0][1]
    for edge, name in _STAGES:
        if ratio >= edge:
            label = name
    return label


def _base_context(cfg: dict, existing: dict) -> dict:
    """规划里的事实性字段只从用户配置/旧规划继承，不凭空编造。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    existing = existing if isinstance(existing, dict) else {}
    return {
        "premise": _text(cfg.get("idea") or cfg.get("premise") or existing.get("premise"), 600),
        "theme": _text(cfg.get("theme") or existing.get("theme"), 300),
        "promise": _text(cfg.get("conflict") or cfg.get("promise") or existing.get("promise"), 600),
        "style_anchor": _text(cfg.get("style_anchor") or cfg.get("tone") or existing.get("style_anchor"), 600),
        "mode": _text(cfg.get("mode") or existing.get("mode") or "outline", 20),
    }


def _build_structure(total: int, cfg: dict, existing: dict | None = None) -> dict:
    """确定性生成三层结构：卷（30–60 章）→ 弧（8–15 章）→ 章号列表。"""
    existing = existing if isinstance(existing, dict) else {}
    ctx = _base_context(cfg, existing)
    old_vols = {_int(v.get("no"), 0): v for v in _list(existing.get("volumes"))
                if isinstance(v, dict)}
    old_arcs = {}
    for v in old_vols.values():
        for a in _list(v.get("arcs")):
            if isinstance(a, dict):
                old_arcs[_int(a.get("no"), 0)] = a

    volumes = []
    arc_no = 0
    for vno, (start, end) in enumerate(_split_ranges(total, VOLUME_TARGET, 30, 60), 1):
        ratio = (start - 1) / max(1, total)
        stage = _stage_label(ratio)
        old_v = old_vols.get(vno) or {}
        _vol_words = _vol_words_for(cfg)
        vtitle = _clean_title(old_v.get("title"), f"第{_cn_no(vno)}卷·{_vol_words[(vno - 1) % len(_vol_words)]}")
        vpurpose = _text(old_v.get("purpose"), 300) or stage
        vgoal = _text(old_v.get("goal"), 600) or (
            f"完成{stage}阶段的结构目标（第{start}–{end}章）")
        if ctx["theme"] and not _text(old_v.get("goal"), 600):
            vgoal = f"围绕「{ctx['theme']}」完成{stage}阶段的结构目标（第{start}–{end}章）"
        arcs = []
        for local_no, (astart, aend) in enumerate(_split_ranges(end - start + 1, ARC_TARGET, 8, 15), 1):
            arc_no += 1
            old_a = old_arcs.get(arc_no) or {}
            _arc_words = _arc_words_for(cfg)
            atitle = _clean_title(old_a.get("title"),
                                  f"第{arc_no}弧·{_arc_words[(local_no - 1) % len(_arc_words)]}")
            agoal = _text(old_a.get("goal"), 600) or (
                f"承接「{vtitle}」，完成本弧的小高潮与章末钩子（第{start + astart - 1}–{start + aend - 1}章）")
            arcs.append({
                "no": arc_no, "local_no": local_no, "title": atitle, "goal": agoal,
                "start_chapter": start + astart - 1, "end_chapter": start + aend - 1,
                "chapters": list(range(start + astart - 1, start + aend)),
            })
        volumes.append({
            "no": vno, "title": vtitle, "purpose": vpurpose, "goal": vgoal,
            "start_chapter": start, "end_chapter": end,
            "word_target": (end - start + 1) * WORDS_PER_CHAPTER,
            "arcs": arcs, "payoffs": _str_list(old_v.get("payoffs"), 120, 10),
        })

    plan = {
        "version": 2,
        "scale": {"target_words": total * WORDS_PER_CHAPTER, "chapters": total,
                  "volumes": len(volumes)},
        **ctx,
        "volumes": volumes,
        "digest": {
            "chapters": total,
            "volumes": len(volumes),
            "arcs": arc_no,
            "target_words": total * WORDS_PER_CHAPTER,
            "words_per_chapter": WORDS_PER_CHAPTER,
        },
        "updated_at": _now(),
    }
    # cfg 里的明确规模优先于推导值（例如用户指定 100 万字/300 章）
    sc = cfg.get("scale") if isinstance(cfg, dict) and isinstance(cfg.get("scale"), dict) else {}
    if sc.get("target_words"):
        plan["scale"]["target_words"] = _int(sc["target_words"], plan["scale"]["target_words"])
        plan["digest"]["target_words"] = plan["scale"]["target_words"]
    return plan


def _find_volume(plan: dict, n: int) -> dict | None:
    for v in _list(plan.get("volumes")):
        if isinstance(v, dict) and _int(v.get("start_chapter"), 0) <= n <= _int(v.get("end_chapter"), 0):
            return v
    return None


def _find_arc(plan: dict, n: int) -> tuple[dict | None, dict | None]:
    for v in _list(plan.get("volumes")):
        if not isinstance(v, dict):
            continue
        for a in _list(v.get("arcs")):
            if isinstance(a, dict) and _int(a.get("start_chapter"), 0) <= n <= _int(a.get("end_chapter"), 0):
                return v, a
    return None, None


def _all_arcs(plan: dict) -> list[dict]:
    out = []
    for v in _list(plan.get("volumes")):
        if isinstance(v, dict):
            for a in _list(v.get("arcs")):
                if isinstance(a, dict):
                    out.append(a)
    return out


# ---------- LLM 辅助 ----------

def _llm_on(cfg) -> bool:
    cfg = cfg if isinstance(cfg, dict) else {}
    return cfg.get("longform_llm", True) is not False and cfg.get("no_llm", False) is not True


def _parse_json_text(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("{"):] if "{" in s else s
        s = s[:s.rfind("}") + 1] if "}" in s else s
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        i, j = s.find("{"), s.rfind("}")
        if not (0 <= i < j):
            return {}
        try:
            obj = json.loads(s[i:j + 1])
        except (ValueError, TypeError):
            return {}
    return obj if isinstance(obj, dict) else {}


def _llm_json(system: str, user: str, cfg, stage: str, max_tokens: int = 2000,
              agent: str | None = None) -> dict:
    """一次严格 JSON 调用；空返回/异常/坏结构一律 {}，调用方走确定性兜底。

    agent：子 Agent 岗位名（planner/editor…）；传入后该次调用按岗位绑定的
    模型/技能解析（providers.agent_resolve），多角色同时规划靠它把不同岗位挂到不同模型。
    """
    if not _llm_on(cfg):
        return {}
    try:
        raw = llm.chat([{"role": "system", "content": system},
                        {"role": "user", "content": user}],
                       json_mode=True, temperature=0.4, max_tokens=max_tokens,
                       cfg=cfg, stage=stage, agent=agent)
    except Exception:
        raw = ""
    out = _parse_json_text(raw)
    if out:
        return out
    # 兼容测试/宿主只 stub chat_json 的情况；仅在 chat 返回空时多试一次
    if not raw:
        try:
            out = llm.chat_json([{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                                cfg=cfg, stage=stage, agent=agent)
        except Exception:
            return {}
    return out if isinstance(out, dict) else {}


def _naming_pass(plan: dict, cfg: dict, references=None) -> None:
    """一次调用补充卷名/卷目标/主题命名；失败时保留模板化中文命名。"""
    volumes = plan.get("volumes") or []
    if not volumes:
        return
    refs = ""
    if references:
        refs = _text(json.dumps(references, ensure_ascii=False) if not isinstance(references, str)
                     else references, 800)
    user = (
        f"全书 {plan['scale']['chapters']} 章，分 {len(volumes)} 卷（卷区间已定，不要改章数）。\n"
        f"梗概：{plan.get('premise') or '（未提供）'}\n主题：{plan.get('theme') or '（未提供）'}\n"
        f"参考材料：{refs or '（无）'}\n"
        "请为每卷起一个 2–6 字的中文卷名，并给出一句可执行的卷目标；只输出 JSON："
        '{"premise":"","theme":"","promise":"","style_anchor":"",'
        '"volumes":[{"no":1,"title":"","purpose":"","goal":""}]}。'
        "不确定的字段留空，不要编造用户未提供的事实。"
    )
    out = _llm_json("你是长篇小说结构规划师，只输出严格 JSON。", user, cfg, "longform_plan", 1800)
    if not out:
        return
    for key in ("premise", "theme", "promise", "style_anchor"):
        if not plan.get(key) and _text(out.get(key), 600):
            plan[key] = _text(out.get(key), 600)
    by_no = {_int(v.get("no"), 0): v for v in volumes if isinstance(v, dict)}
    for item in _list(out.get("volumes")):
        if not isinstance(item, dict):
            continue
        v = by_no.get(_int(item.get("no"), 0))
        if not v:
            continue
        title = _clean_title(item.get("title"))
        if title and len(title) <= 20:
            v["title"] = title if title.startswith("第") else f"第{_cn_no(v['no'])}卷·{title}"
        for key in ("purpose", "goal"):
            if _text(item.get(key), 300):
                v[key] = _text(item.get(key), 600)


# ---------- 旧计划 / 大纲读取 ----------

def _outline_rows(proj) -> dict[int, dict]:
    """从 01-大纲.md 抽每章标题与首个规划段（迁移旧项目时保留原意）。"""
    text = _read_text(os.path.join(_pdir(proj), "01-大纲.md"))
    rows: dict[int, dict] = {}
    for m in re.finditer(r"^\|\s*第(\d+)章\s*\|\s*([^|]*)\|", text, re.M):
        n = _int(m.group(1), 0)
        if n > 0:
            rows.setdefault(n, {})["title"] = _clean_title(m.group(2))
    # v3.1.0：LLM 表格首列常写「01」「1」（非「第N章」），同样解析，救回章名
    for m in re.finditer(r"^\|\s*(\d{1,4})\s*\|\s*([^|\n]+)\|", text, re.M):
        n = _int(m.group(1), 0)
        if n > 0 and not rows.get(n, {}).get("title"):
            t = _clean_title(m.group(2))
            if t:
                rows.setdefault(n, {})["title"] = t
    for m in re.finditer(r"^#{2,4}\s*第(\d+)章[^\n]*\n(.*?)(?=\n#{2,4}\s*第|\n##\s|\Z)", text, re.M | re.S):
        n = _int(m.group(1), 0)
        if n > 0:
            body = re.sub(r"^[#>*`\-\s]+", "", m.group(2)).strip()
            if body:
                rows.setdefault(n, {})["goal"] = _text(body, 400)
    return rows


def _flat_chapters(proj) -> list[dict]:
    plan = proj.plan() if hasattr(proj, "plan") else None
    if not isinstance(plan, dict):
        plan = _read_json(os.path.join(_pdir(proj), FLAT_PLAN), {})
    chs = plan.get("chapters") if isinstance(plan, dict) else None
    return [c for c in _list(chs) if isinstance(c, dict)]


def _flat_config(proj) -> dict:
    plan = proj.plan() if hasattr(proj, "plan") else None
    if not isinstance(plan, dict):
        plan = _read_json(os.path.join(_pdir(proj), FLAT_PLAN), {})
    cfg = plan.get("config") if isinstance(plan, dict) else None
    return cfg if isinstance(cfg, dict) else {}


# ---------- 迁移 / 生成 ----------

def migrate_flat_plan(proj, cfg=None) -> dict:
    """幂等迁移：扁平 02-写作计划 → 02c/02d；不覆盖已完成章节的标题/状态/字数。"""
    existing = _read_plan(proj)
    if existing.get("volumes"):
        return existing
    flat = proj.plan() if hasattr(proj, "plan") else None
    flat = flat if isinstance(flat, dict) else _read_json(os.path.join(_pdir(proj), FLAT_PLAN), {})
    flat = flat if isinstance(flat, dict) else {}
    cfg2 = cfg if isinstance(cfg, dict) and cfg else (flat.get("config") or {})
    total = max(resolve_scale(cfg2, flat)["chapters"], len(_list(flat.get("chapters"))),
                _int(flat.get("totalChapters"), 0))
    plan = _build_structure(total, cfg2, existing)
    _naming_pass(plan, cfg2)

    cards = _load_cards(proj)
    outline = _outline_rows(proj)
    for ch in _flat_chapters(proj):
        n = _int(ch.get("chapterNumber"), 0)
        if n <= 0:
            continue
        row = outline.get(n) or {}
        old = cards.get(n) or {}
        title = _clean_title(ch.get("title") or old.get("title") or row.get("title"), f"第{n}章")
        card = _empty_card(n)
        card.update(old)
        card["title"] = title
        if not card.get("goal") and row.get("goal"):
            card["goal"] = row["goal"]
        card["status"] = _norm_status_card(ch.get("status") or old.get("status"))
        cards[n] = card
    for n in range(1, total + 1):
        if n not in cards:
            row = outline.get(n) or {}
            card = _empty_card(n)
            card["title"] = _clean_title(row.get("title"), f"第{n}章")
            card["goal"] = _text(row.get("goal"), 400)
            cards[n] = card
    _fill_card_positions(plan, cards)
    _write_plan(proj, plan)
    _write_cards(proj, cards)
    # 旧项目已有成章但还没有状态库：用确定性解析补一份，避免续写从零开始。
    try:
        if not os.path.isfile(os.path.join(_pdir(proj), "18-状态库.json")):
            state_mod.rebuild_from_project(_pdir(proj), flat)
    except Exception:
        pass
    _log("已迁移旧扁平计划 → 分层规划（%d 章）" % total, proj)
    return plan


def ensure_plan(proj, cfg=None) -> dict:
    """确保 02c 存在：已有直接返回；否则从扁平计划迁移/生成。不覆盖已完成章节。"""
    plan = _read_plan(proj)
    if plan.get("volumes"):
        return plan
    return migrate_flat_plan(proj, cfg)


def _fill_card_positions(plan: dict, cards: dict[int, dict]) -> None:
    """只补 volume/arc/word_target 这类结构字段，不碰标题/目标/状态。"""
    for n, card in cards.items():
        v = _find_volume(plan, n)
        if v:
            card["volume"] = _int(v.get("no"), 0)
        _v, a = _find_arc(plan, n)
        if a:
            card["arc"] = _int(a.get("no"), 0)
        if not card.get("word_target"):
            card["word_target"] = WORDS_PER_CHAPTER
        if not card.get("title"):
            card["title"] = f"第{n}章"
        card.setdefault("updated_at", "")


def _default_goal(plan: dict, n: int) -> str:
    v, a = _find_arc(plan, n)
    if a:
        start = _int(a.get("start_chapter"), n)
        end = _int(a.get("end_chapter"), n)
        return f"{_text(a.get('goal'), 300)}；本章为弧内第{n - start + 1}/{end - start + 1}章"
    if v:
        return f"{_text(v.get('goal'), 300)}；本章推进本卷目标"
    return ""


def plan_book(proj, cfg, references=None) -> dict:
    """生成/刷新总规划与全部章卡；返回给前端/日志的 digest。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    flat = proj.plan() if hasattr(proj, "plan") else None
    flat = flat if isinstance(flat, dict) else _read_json(os.path.join(_pdir(proj), FLAT_PLAN), {})
    flat = flat if isinstance(flat, dict) else {}
    cfg2 = cfg or (flat.get("config") if isinstance(flat.get("config"), dict) else {})
    existing = _read_plan(proj)
    old_cards = _load_cards(proj)
    naming_cfg = {**(_flat_config(proj)), **cfg2} if isinstance(cfg2, dict) else _flat_config(proj)

    scale = resolve_scale(cfg2, flat)
    total = max(scale["chapters"], len(_list(flat.get("chapters"))),
                _int(flat.get("totalChapters"), 0),
                _int((existing.get("scale") or {}).get("chapters"), 0),
                max(old_cards, default=0))
    plan = _build_structure(total, cfg2, existing)
    if references:
        ids = []
        summaries = []
        for r in _list(references):
            if isinstance(r, dict):
                rid = _text(r.get("id") or r.get("title"), 80)
                if rid:
                    summaries.append({
                        "id": rid,
                        "title": _text(r.get("title") or rid, 120),
                        "excerpt": _text(r.get("excerpt") or r.get("summary"), 900),
                    })
            else:
                rid = _text(r, 80)
            if rid:
                ids.append(rid)
        if ids:
            plan["reference_ids"] = ids[:30]
            plan["references"] = summaries[:8]
    _naming_pass(plan, naming_cfg, references)

    cards = dict(old_cards)
    outline = _outline_rows(proj)
    titles = {}
    for ch in _flat_chapters(proj):
        n = _int(ch.get("chapterNumber"), 0)
        title = _clean_title(ch.get("title"))
        if n > 0 and title:
            titles[n] = title
    for n in range(1, total + 1):
        card = cards.get(n)
        row = outline.get(n) or {}
        if card and card.get("status") == "done":
            # 已写章节卡原样保留（只补结构字段）
            cards[n] = card
            continue
        if not card:
            card = _empty_card(n, _int((_find_volume(plan, n) or {}).get("no"), 0),
                               _int((_find_arc(plan, n)[1] or {}).get("no"), 0))
        if not card.get("title") or card["title"] == f"第{n}章":
            card["title"] = titles.get(n) or _clean_title(row.get("title"), f"第{n}章")
        if not card.get("goal"):
            card["goal"] = _text(row.get("goal"), 400) or _default_goal(plan, n)
        cards[n] = card
    # 丢弃超出新规模的尾部卡（不会删已写章节）
    for n in list(cards):
        if n > total and cards[n].get("status") != "done":
            cards.pop(n, None)
    _fill_card_positions(plan, cards)
    _write_plan(proj, plan)
    _write_cards(proj, cards)
    if hasattr(proj, "save_plan"):
        plan_to_flat(proj)
    _log(f"已生成分层规划：{total} 章 / {len(plan['volumes'])} 卷", proj)
    return plan_digest(proj)


def plan_window(proj, start, end, reason: str = "rolling") -> dict:
    """滚动规划未来窗口章卡；不改已完成章卡。LLM 为空时只做确定性补全。"""
    plan = ensure_plan(proj)
    total = max(1, _int((plan.get("scale") or {}).get("chapters"), 1))
    s, e = _int(start, 1), _int(end, 1)
    if s > e:
        s, e = e, s
    s = max(1, min(total, s))
    e = max(s, min(total, e))
    cards = _load_cards(proj)
    st = state_mod.load(_pdir(proj))
    focus = [cards[n] for n in range(s, e + 1) if n in cards and cards[n].get("status") != "done"]
    if not focus:
        return {"ok": True, "start": s, "end": e, "updated": 0, "source": "none",
                "reason": reason, "cards": []}

    cfg = _flat_config(proj)
    recent = _recent_summaries(proj, 5, before=s)
    due = state_mod.foreshadow_due(st, s, lookahead=max(15, e - s + 1))
    v, a = _find_arc(plan, s)
    llm_cards = {}
    if _llm_on(cfg):
        due_lines = [f"[{x.get('id')}] {x.get('description','')[:80]}（第{x.get('due_from')}–{x.get('due_to')}章）"
                     for x in due["overdue"] + due["due"] + due["upcoming"]][:10]
        user = (
            f"全书 {total} 章，当前规划第 {s}–{e} 章。\n"
            f"卷目标：{_text((v or {}).get('goal'), 300)}\n弧目标：{_text((a or {}).get('goal'), 300)}\n"
            f"最近摘要：\n" + "\n".join(f"- 第{n}章：{t[:160]}" for n, t in recent) + "\n"
            f"相关伏笔：\n" + ("\n".join(f"- {x}" for x in due_lines) or "- 无") + "\n"
            f"本次原因：{reason}\n"
            '只输出 JSON：{"cards":[{"n":章号,"title":"","goal":"","conflict":"","turn":"",'
            '"payoff":"","cliffhanger":"","foreshadow_setup":[],"foreshadow_payoff":[],'
            '"cast":[],"locations":[]}]}。只规划给定章号，不确定的字段留空。'
        )
        out = _llm_json("你是长篇网文的分卷细纲师，只输出严格 JSON。", user, cfg,
                        "longform_window", 4000)
        for item in _list(out.get("cards")):
            if not isinstance(item, dict):
                continue
            n = _int(item.get("n"), 0)
            if s <= n <= e:
                llm_cards[n] = item

    updated = 0
    for n in range(s, e + 1):
        card = cards.get(n) or _empty_card(n)
        if card.get("status") == "done":
            continue
        before = json.dumps(card, ensure_ascii=False, sort_keys=True)
        item = llm_cards.get(n) or {}
        for key in _CARD_FIELDS:
            if key not in item:
                continue
            val = item.get(key)
            if key in ("foreshadow_setup", "foreshadow_payoff", "cast", "locations"):
                val = _str_list(val, 80, 16)
                if val:
                    card[key] = val
            elif key == "word_target":
                num = _int(val, 0)
                if num > 0:
                    card[key] = max(500, min(20000, num))
            else:
                txt = _text(val, 500)
                if txt:
                    card[key] = _clean_title(txt, txt) if key == "title" else txt
        if not card.get("goal"):
            card["goal"] = _default_goal(plan, n)
        if not card.get("title"):
            card["title"] = f"第{n}章"
        if json.dumps(card, ensure_ascii=False, sort_keys=True) != before:
            card["updated_at"] = _now()
            updated += 1
        cards[n] = card
    _fill_card_positions(plan, cards)
    _write_cards(proj, cards)
    return {"ok": True, "start": s, "end": e, "updated": updated,
            "source": "llm" if llm_cards else "deterministic",
            "reason": reason, "cards": [cards[n]["title"] for n in range(s, e + 1) if n in cards]}


# ---------- 重规划提案 ----------

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _proposal_dir(proj) -> str:
    return os.path.join(_pdir(proj), PROPOSAL_DIR)


def _done_boundary(cards: dict[int, dict]) -> int:
    return max((n for n, c in cards.items() if c.get("status") == "done"), default=0)


def _scope_range(proj, scope: str, next_no: int, cards: dict[int, dict]) -> tuple[list[int], dict]:
    plan = _read_plan(proj)
    total = max(1, _int((plan.get("scale") or {}).get("chapters"),
                        max(cards, default=next_no)))
    if scope == "book":
        return list(range(1, total + 1)), {"total": total}
    if scope == "volume":
        v = _find_volume(plan, next_no)
        if v:
            return list(range(_int(v.get("start_chapter"), next_no),
                              _int(v.get("end_chapter"), next_no) + 1)), {"volume": v.get("no")}
        return [next_no], {}
    if scope == "arc":
        _v, a = _find_arc(plan, next_no)
        if a:
            return list(range(_int(a.get("start_chapter"), next_no),
                              _int(a.get("end_chapter"), next_no) + 1)), {"arc": a.get("no")}
        return [next_no], {}
    if scope == "chapter":
        return [next_no], {}
    return list(range(next_no, min(total, next_no + 14) + 1)), {}


# ---------- 多角色同时规划（v2.1 P2） ----------

# 岗位 → 视角提示。系统默认文案保持旧版「总编」措辞，只有显式指定岗位时才换口吻，
# 这样历史调用与测试的行为不变。
MULTI_ROLES = {
    "planner": {
        "label": "规划师",
        "system": "你是长篇网文的规划师，负责把主线推进、钩子密度与爽点节拍排进章卡，只输出严格 JSON。",
        "angle": "你的视角：主线推进与追读钩子——优先保证每章有明确目标、章尾有钩子、爽点节拍不断档。",
    },
    "editor": {
        "label": "总编",
        "system": "你是长篇网文的总编，负责重规划提案，只输出严格 JSON。",
        "angle": "你的视角：一致性与风险——优先核对伏笔回收、人物动机、时间线与结构断裂，指出并修正隐患。",
    },
}


def _replan_system(agent: str) -> str:
    role = MULTI_ROLES.get(agent or "")
    return role["system"] if role else "你是长篇网文的总编，负责重规划提案，只输出严格 JSON。"


def multi_replan(proj, scope: str = "window", instruction: str = "",
                 roles: tuple | list | None = None) -> list[dict]:
    """两个（或多个）岗位同时出一份规划提案，返回对比卡数据。

    每个岗位各写一份 02e 提案（互不覆盖），前端并排展示后由用户选一份应用——
    「多 Agent 同时规划、不同 Agent 干不同的事」的落点。单个岗位失败只标记该岗位，
    不影响其它岗位产出的提案。
    """
    keys = [r for r in (roles or ("planner", "editor")) if r in MULTI_ROLES]
    keys = list(dict.fromkeys(keys))[:3] or ["planner"]
    base = _text(instruction, 1000)

    def one(agent: str) -> dict:
        role = MULTI_ROLES[agent]
        ask = f"{base}\n{role['angle']}".strip() if base else role["angle"]
        try:
            p = replan(proj, scope=scope, instruction=ask,
                       reason=f"multi:{agent}", agent=agent, role=role["label"])
            return {"agent": agent, "label": role["label"], "ok": True,
                    "proposal_id": p.get("id"), "summary": p.get("summary") or "",
                    "diff": p.get("diff") or [], "scope": p.get("scope"),
                    "affected_chapters": p.get("affected_chapters") or [],
                    "requires_past_rewrite": bool(p.get("requires_past_rewrite")),
                    "status": p.get("status") or "pending", "created_at": p.get("created_at")}
        except Exception as e:
            return {"agent": agent, "label": role["label"], "ok": False, "error": str(e)}

    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        return list(pool.map(one, keys))


def replan(proj, scope: str = "window", instruction: str = "", reason: str = "manual",
           agent: str = "", role: str = "") -> dict:
    """生成重规划提案（写 02e），**不修改** 02c/02d。

    agent/role（v2.1 P2）：多角色同时规划时传入岗位名与角色标签，提案里带上它们，
    前端才能把「规划师版 / 总编版」并排成对比卡；不传则与旧行为完全一致。
    """
    scope = scope if scope in ("book", "volume", "arc", "window", "chapter") else "window"
    instruction = _text(instruction, 1000)
    reason = _text(reason, 200) or "manual"
    plan = ensure_plan(proj)
    cards = _load_cards(proj)
    total = max(1, _int((plan.get("scale") or {}).get("chapters"), max(cards, default=1)))
    done = _done_boundary(cards)
    next_no = min(total, done + 1) if done < total else total
    affected, target = _scope_range(proj, scope, next_no, cards)
    affected = [n for n in affected if 1 <= n <= total]
    requires_past = any(n <= done for n in affected)

    # LLM 只出草案；确定性 diff 始终可用
    draft_cards, volume_updates, llm_summary, llm_diff = {}, [], "", []
    cfg = _flat_config(proj)
    if _llm_on(cfg):
        v, a = _find_arc(plan, next_no)
        sample = [cards[n] for n in affected[:20] if n in cards]
        user = (
            f"重规划范围：{scope}（第{min(affected) if affected else next_no}–"
            f"{max(affected) if affected else next_no}章，共{len(affected)}章）。\n"
            f"原因：{reason}\n作者指令：{instruction or '（无）'}\n"
            f"卷目标：{_text((v or {}).get('goal'), 300)}\n弧目标：{_text((a or {}).get('goal'), 300)}\n"
            f"现有章卡（节选）：{_text(json.dumps(sample, ensure_ascii=False), 6000)}\n"
            '只输出 JSON：{"summary":"","diff":["中文变更说明"],'
            '"volume_updates":[{"no":1,"title":"","purpose":"","goal":""}],'
            '"cards":[{"n":章号,"title":"","goal":"","conflict":"","turn":"","payoff":"",'
            '"cliffhanger":""}]}。不要输出与范围无关的章。'
        )
        out = _llm_json(_replan_system(agent), user, cfg,
                        "longform_replan", 3500, agent=agent or None)
        llm_summary = _text(out.get("summary"), 600)
        llm_diff = _str_list(out.get("diff"), 200, 10)
        for item in _list(out.get("volume_updates")):
            if isinstance(item, dict):
                volume_updates.append(item)
        for item in _list(out.get("cards")):
            if isinstance(item, dict):
                n = _int(item.get("n"), 0)
                if n in affected:
                    draft_cards[n] = item

    if not draft_cards and scope in ("window", "chapter", "arc", "volume"):
        # 确定性草案：只改目标描述以承载指令，不编造情节
        for n in affected:
            card = cards.get(n)
            if not card or card.get("status") == "done":
                continue
            draft = {"n": n}
            if instruction:
                base = card.get("goal") or _default_goal(plan, n)
                draft["goal"] = _text(f"{base}；重规划方向：{instruction}", 500)
            else:
                draft["goal"] = _text(_default_goal(plan, n), 500)
            draft_cards[n] = draft

    diff = []
    diff.append(f"范围：{scope}，影响第{min(affected) if affected else next_no}–"
                f"{max(affected) if affected else next_no}章（{len(affected)} 章）")
    if instruction:
        diff.append(f"作者指令：{instruction}")
    for n in affected[:12]:
        old = cards.get(n) or {}
        new = draft_cards.get(n) or {}
        if new.get("goal") and new.get("goal") != old.get("goal"):
            diff.append(f"第{n}章目标：{old.get('goal') or '（空）'} → {new.get('goal')}")
        if new.get("title") and new.get("title") != old.get("title"):
            diff.append(f"第{n}章标题：{old.get('title') or '（空）'} → {new.get('title')}")
    for vu in volume_updates[:6]:
        diff.append(f"第{_int(vu.get('no'), 0)}卷结构调整："
                    f"{_text(vu.get('title') or vu.get('goal') or vu.get('purpose'), 120)}")
    if requires_past:
        diff.append(f"注意：范围覆盖已写章节（截至第{done}章），应用时默认拒绝，需 allow_past")
    diff.extend(llm_diff)
    diff = _str_list(diff, 300, 30)

    pid = f"{scope}-{time.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
    proposal = {
        "id": pid,
        "scope": scope,
        "reason": reason,
        "instruction": instruction,
        "agent": _text(agent, 40),
        "role": _text(role, 40),
        "created_at": _now(),
        "status": "pending",
        "summary": llm_summary or (diff[0] if diff else f"{scope} 重规划"),
        "diff": diff,
        "affected_chapters": affected,
        "requires_past_rewrite": requires_past,
        "target": target,
        "changes": {"cards": [draft_cards[n] for n in sorted(draft_cards)],
                    "volume_updates": volume_updates},
    }
    os.makedirs(_proposal_dir(proj), exist_ok=True)
    atomic_write_json(os.path.join(_proposal_dir(proj), f"{pid}.json"), proposal)
    _append_replan_log(proj, {"ts": _now(), "action": "proposed", "proposal_id": pid,
                              "scope": scope, "reason": reason,
                              "affected_chapters": affected[:50],
                              "requires_past_rewrite": requires_past,
                              "summary": proposal["summary"]})
    return proposal


def _read_proposal(proj, proposal_id: str) -> dict | None:
    pid = str(proposal_id or "")
    if not _ID_RE.match(pid):
        return None
    try:
        path = safe_join(_proposal_dir(proj), f"{pid}.json", label="提案 id")
    except ValueError:
        return None
    data = _read_json(path, None)
    if not isinstance(data, dict):
        return None
    data.setdefault("id", pid)
    return data


def _write_proposal(proj, proposal: dict) -> None:
    pid = str(proposal.get("id") or "")
    if not _ID_RE.match(pid):
        return
    os.makedirs(_proposal_dir(proj), exist_ok=True)
    atomic_write_json(os.path.join(_proposal_dir(proj), f"{pid}.json"), proposal)


def apply_replan(proj, proposal_id: str, allow_past: bool = False) -> dict:
    """应用提案：只改未来章卡与规划结构；影响已写章节的提案默认拒绝。"""
    proposal = _read_proposal(proj, proposal_id)
    if not proposal:
        return {"ok": False, "error": "提案不存在或 id 非法"}
    if proposal.get("status") not in (None, "pending"):
        return {"ok": False, "error": f"提案状态为 {proposal.get('status')}，不可重复应用"}
    if proposal.get("requires_past_rewrite") and not allow_past:
        return {"ok": False, "error": "该提案影响已写章节，需 allow_past=true 才能应用"}

    plan = ensure_plan(proj)
    cards = _load_cards(proj)
    done = _done_boundary(cards)
    scope = proposal.get("scope") if proposal.get("scope") in (
        "book", "volume", "arc", "window", "chapter") else "window"
    affected = {_int(n) for n in _list(proposal.get("affected_chapters")) if _int(n) > 0}
    changes = proposal.get("changes") if isinstance(proposal.get("changes"), dict) else {}

    structural = 0
    for vu in _list(changes.get("volume_updates")):
        if not isinstance(vu, dict):
            continue
        vno = _int(vu.get("no"), 0)
        for v in _list(plan.get("volumes")):
            if isinstance(v, dict) and _int(v.get("no"), 0) == vno:
                for key, cap in (("title", 40), ("purpose", 300), ("goal", 600)):
                    if _text(vu.get(key), cap):
                        v[key] = _clean_title(vu[key]) if key == "title" else _text(vu[key], cap)
                structural += 1
                break

    applied_cards = []
    for item in _list(changes.get("cards")):
        if not isinstance(item, dict):
            continue
        n = _int(item.get("n"), 0)
        if n <= 0 or (affected and n not in affected):
            continue
        card = cards.get(n)
        if not card:
            continue
        if card.get("status") == "done" and not allow_past:
            continue
        if n <= done and not allow_past:
            continue
        for key in _CARD_FIELDS:
            if key not in item:
                continue
            val = item.get(key)
            if key in ("foreshadow_setup", "foreshadow_payoff", "cast", "locations"):
                cleaned = _str_list(val, 80, 16)
                if cleaned:
                    card[key] = cleaned
            elif key == "word_target":
                num = _int(val, 0)
                if num > 0:
                    card[key] = max(500, min(20000, num))
            else:
                txt = _text(val, 500)
                if txt:
                    card[key] = _clean_title(txt, txt) if key == "title" else txt
        card["updated_at"] = _now()
        applied_cards.append(n)

    # 结构变化落盘；章卡只写未来（allow_past 时允许覆盖过去卡字段，但状态仍是 done）
    _fill_card_positions(plan, cards)
    _write_plan(proj, plan)
    _write_cards(proj, cards)
    if hasattr(proj, "save_plan"):
        plan_to_flat(proj)

    proposal["status"] = "applied"
    proposal["applied_at"] = _now()
    proposal["allow_past"] = bool(allow_past)
    _write_proposal(proj, proposal)
    _append_replan_log(proj, {"ts": _now(), "action": "applied", "proposal_id": proposal.get("id"),
                              "scope": scope, "allow_past": bool(allow_past),
                              "affected_cards": applied_cards[:100],
                              "structural_updates": structural,
                              "summary": proposal.get("summary", "")})
    return {"ok": True, "proposal_id": proposal.get("id"), "scope": scope,
            "applied_cards": applied_cards, "structural_updates": structural,
            "digest": plan_digest(proj)}


def reject_replan(proj, proposal_id: str, note: str = "") -> dict:
    proposal = _read_proposal(proj, proposal_id)
    if not proposal:
        return {"ok": False, "error": "提案不存在或 id 非法"}
    if proposal.get("status") not in (None, "pending"):
        return {"ok": False, "error": f"提案状态为 {proposal.get('status')}，不可驳回"}
    proposal["status"] = "rejected"
    proposal["rejected_at"] = _now()
    proposal["note"] = _text(note, 500)
    _write_proposal(proj, proposal)
    _append_replan_log(proj, {"ts": _now(), "action": "rejected", "proposal_id": proposal.get("id"),
                              "scope": proposal.get("scope"), "note": _text(note, 500)})
    return {"ok": True, "proposal_id": proposal.get("id"), "status": "rejected"}


def list_proposals(proj, status: str | None = None) -> list[dict]:
    d = _proposal_dir(proj)
    try:
        names = [x for x in os.listdir(d) if x.endswith(".json")][-_MAX_PROPOSALS:]
    except OSError:
        return []
    out = []
    for name in names:
        data = _read_json(os.path.join(d, name), None)
        if not isinstance(data, dict):
            continue
        if status and data.get("status") != status:
            continue
        out.append({
            "id": data.get("id") or name[:-5],
            "scope": data.get("scope"),
            "reason": data.get("reason"),
            "instruction": data.get("instruction"),
            "agent": data.get("agent") or "",
            "role": data.get("role") or "",
            "status": data.get("status"),
            "summary": data.get("summary"),
            "diff": data.get("diff") or [],
            "affected_chapters": data.get("affected_chapters") or [],
            "requires_past_rewrite": bool(data.get("requires_past_rewrite")),
            "created_at": data.get("created_at"),
            "applied_at": data.get("applied_at"),
        })
    out.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return out


def _append_replan_log(proj, rec: dict) -> None:
    """审计日志追加。多岗位同时规划时会有两个线程并发写同一文件，
    所以能拿到 proj.lock 就上锁，避免半行 JSON。"""
    try:
        path = os.path.join(_pdir(proj), REPLAN_LOG)
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
    except (OSError, TypeError, ValueError):
        return
    lock = getattr(proj, "lock", None)
    try:
        if lock is None:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        else:
            with lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
    except OSError:
        pass


# ---------- 触发判断 ----------

def _recent_summaries(proj, k: int = 5, before: int | None = None) -> list[tuple[int, str]]:
    rows = _load_chapter_summaries(proj)
    picked = []
    for n in sorted(rows, reverse=True):
        if before is not None and n >= before:
            continue
        sm = rows[n].get("summary") if isinstance(rows[n], dict) else ""
        if sm:
            picked.append((n, sm))
        if len(picked) >= k:
            break
    return list(reversed(picked))


def _load_chapter_summaries(proj) -> dict[int, dict]:
    path = os.path.join(_pdir(proj), CHAPTER_SUMMARY)
    out: dict[int, dict] = {}
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            n = _int(obj.get("n"), 0)
            if n > 0:
                out[n] = obj
    return out


def should_replan(proj, next_chapter_no: int, cfg=None) -> tuple[bool, str]:
    """每 10 章 / 弧边界 / 卷边界 / 伏笔逾期≥3 / 连续低分时返回 (True, 原因)。"""
    cfg = cfg if isinstance(cfg, dict) else _flat_config(proj)
    lf = cfg.get("longform") if isinstance(cfg.get("longform"), dict) else {}
    n = max(1, _int(next_chapter_no, 1))
    interval = max(2, _int(cfg.get("replan_interval") or lf.get("replan_every")
                           or cfg.get("replan_every"), 10))
    if n > 1 and (n - 1) % interval == 0:
        return True, f"已推进 {n - 1} 章，进入每 {interval} 章的滚动重规划"
    plan = _read_plan(proj)
    v, a = _find_arc(plan, n)
    if v and n > 1 and _int(v.get("start_chapter"), 0) == n:
        return True, f"第{n}章进入第{v.get('no')}卷「{v.get('title') or ''}」"
    if a and n > 1 and _int(a.get("start_chapter"), 0) == n:
        return True, f"第{n}章进入第{a.get('no')}弧「{a.get('title') or ''}」"
    st = state_mod.load(_pdir(proj))
    overdue = state_mod.foreshadow_due(st, max(1, n - 1))["overdue"]
    if len(overdue) >= 3:
        return True, f"已有 {len(overdue)} 条伏笔逾期（如「{overdue[0].get('id')}」），需要重排兑现窗口"
    low_line = _int(cfg.get("low_score"), 60)
    rows = _load_chapter_summaries(proj)
    scores = []
    for k in sorted(rows, reverse=True)[:3]:
        sig = rows[k].get("quality_signals") if isinstance(rows[k], dict) else None
        if isinstance(sig, dict):
            sc = _int(sig.get("score") or sig.get("total"), -1)
            if sc >= 0:
                scores.append(sc)
    if len(scores) >= 3 and all(s < low_line for s in scores):
        return True, f"最近 3 章连续低分（{', '.join(map(str, scores))} < {low_line}），需要调整节奏"
    return False, ""


# ---------- 章卡合并 ----------

def build_chapter_card(proj, ch_no: int, cfg=None) -> dict:
    """把章卡 + 卷/弧目标 + 状态相关人物/伏笔 + 最近摘要 + 参考材料合并给 context 用。"""
    n = max(1, _int(ch_no, 1))
    plan = _read_plan(proj)
    cards = _load_cards(proj)
    card = cards.get(n)
    if not card:
        # 旧项目还没有章卡：从扁平计划/大纲兜底，保证 context 仍有"本章章卡"
        flat = next((c for c in _flat_chapters(proj)
                     if _int(c.get("chapterNumber"), 0) == n), {})
        row = _outline_rows(proj).get(n) or {}
        card = _empty_card(n)
        card["title"] = _clean_title(flat.get("title") or row.get("title"), f"第{n}章")
        card["goal"] = _text(row.get("goal"), 400)
        v, a = _find_arc(plan, n)
        if v:
            card["volume"] = _int(v.get("no"), 0)
        if a:
            card["arc"] = _int(a.get("no"), 0)
        if not card.get("goal"):
            card["goal"] = _default_goal(plan, n)
    v, a = _find_arc(plan, n)
    if not v:
        v = _find_volume(plan, n)
    st = state_mod.load(_pdir(proj))
    cast = list(card.get("cast") or [])
    focus_chars = []
    for name in cast:
        c = st["characters"].get(name)
        if c:
            focus_chars.append({"name": name, **{k: c.get(k) for k in
                                                 ("status", "location", "goal", "knowledge", "relations")}})
    if not focus_chars:
        for name, c in list(st["characters"].items())[:6]:
            focus_chars.append({"name": name, **{k: c.get(k) for k in
                                                 ("status", "location", "goal")}})
    due = state_mod.foreshadow_due(st, n, lookahead=15)
    fs_ids = set(card.get("foreshadow_setup") or []) | set(card.get("foreshadow_payoff") or [])
    focus_fs = []
    for bucket in (due["overdue"], due["due"], due["upcoming"]):
        for f in bucket:
            if f.get("id") in fs_ids or len(focus_fs) < 6:
                focus_fs.append(f)
    recent = _recent_summaries(proj, 5, before=n)
    refs = _references_summary(proj, cfg)
    return {
        "n": n,
        "title": card.get("title") or f"第{n}章",
        "volume": card.get("volume") or _int((v or {}).get("no"), 0),
        "volume_title": _text((v or {}).get("title"), 80),
        "volume_goal": _text((v or {}).get("goal"), 400),
        "arc": card.get("arc") or _int((a or {}).get("no"), 0),
        "arc_title": _text((a or {}).get("title"), 80),
        "arc_goal": _text((a or {}).get("goal"), 400),
        "goal": card.get("goal"),
        "conflict": card.get("conflict"),
        "turn": card.get("turn"),
        "payoff": card.get("payoff"),
        "cliffhanger": card.get("cliffhanger"),
        "foreshadow_setup": list(card.get("foreshadow_setup") or []),
        "foreshadow_payoff": list(card.get("foreshadow_payoff") or []),
        "cast": cast,
        "locations": list(card.get("locations") or []),
        "word_target": _int(card.get("word_target"), WORDS_PER_CHAPTER),
        "status": card.get("status"),
        "state_focus": {"characters": focus_chars, "foreshadowing": focus_fs[:8]},
        "foreshadow_due": due,
        "recent_summaries": recent,
        "references": refs,
    }


def render_card(card: dict) -> str:
    """把章卡渲染成给 LLM 的中文段落（context.chapter_plan 用）。"""
    if not isinstance(card, dict):
        return ""
    lines = [f"【本章章卡】第{card.get('n')}章 · {card.get('title') or ''}"]
    if card.get("volume"):
        lines.append(f"- 卷：第{card['volume']}卷 {card.get('volume_title') or ''}｜{card.get('volume_goal') or ''}")
    if card.get("arc"):
        lines.append(f"- 弧：第{card['arc']}弧 {card.get('arc_title') or ''}｜{card.get('arc_goal') or ''}")
    for key, label in (("goal", "本章目标"), ("conflict", "核心冲突"), ("turn", "转折"),
                       ("payoff", "本章兑现"), ("cliffhanger", "章末钩子")):
        if card.get(key):
            lines.append(f"- {label}：{card[key]}")
    if card.get("foreshadow_setup"):
        lines.append("- 需埋设伏笔：" + "、".join(card["foreshadow_setup"]))
    if card.get("foreshadow_payoff"):
        lines.append("- 需兑现伏笔：" + "、".join(card["foreshadow_payoff"]))
    if card.get("cast"):
        lines.append("- 出场人物：" + "、".join(card["cast"]))
    if card.get("locations"):
        lines.append("- 场景地点：" + "、".join(card["locations"]))
    if card.get("word_target"):
        lines.append(f"- 目标字数：{card['word_target']}")
    return "\n".join(lines)


def _references_summary(proj, cfg=None) -> list[dict]:
    """参考材料摘要：调用方可通过 cfg["references"] 传入；只保留标题+短摘录。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    refs = cfg.get("references")
    if not refs:
        plan = _read_plan(proj)
        refs = plan.get("references")
    out = []
    for i, r in enumerate(_list(refs), 1):
        if isinstance(r, dict):
            out.append({"id": _text(r.get("id") or f"R{i}", 20),
                        "title": _text(r.get("title") or r.get("name"), 120),
                        "excerpt": _text(r.get("excerpt") or r.get("summary") or r.get("text"), 400)})
        else:
            out.append({"id": f"R{i}", "title": "", "excerpt": _text(r, 400)})
        if len(out) >= 8:
            break
    return out


# ---------- 摘要抽取 ----------

def _fallback_summary(text: str) -> tuple[str, list[str]]:
    """确定性兜底：只摘录正文/本章概要，不推断、不编造事实。"""
    src = text or ""
    m = re.search(r"##\s*本章概要\s*\n(.*?)(?=\n##\s|\Z)", src, re.S)
    if m:
        src = m.group(1)
    plain = re.sub(r"\n##\s*(章首引子|章节备注).*?(?=\n##\s|\Z)", "\n", src, flags=re.S)
    lines = []
    for ln in plain.splitlines():
        s = ln.strip().lstrip("#").strip()
        if not s or s.startswith(("|", "-", ">", "_")):
            continue
        lines.append(s)
        if len(" ".join(lines)) >= 320:
            break
    body = re.sub(r"[#>*`]", "", " ".join(lines)).strip()
    if not body:
        return "", []
    cut = -1
    for p in "。！？!?":
        cut = max(cut, body.find(p, 60, 300))
    summary = body[:cut + 1] if cut > 60 else body[:300]
    beats = []
    start = 0
    for m2 in re.finditer(r"[。！？!?]", body[:600]):
        seg = body[start:m2.end()].strip()
        if len(seg) >= 8:
            beats.append(seg[:160])
        start = m2.end()
        if len(beats) >= 3:
            break
    return summary[:600], beats


def _apply_foreshadow_ops(delta: dict, ops: list, n: int) -> None:
    for op in ops:
        if not isinstance(op, dict):
            continue
        fid = _text(op.get("id") or op.get("foreshadow_id") or op.get("name"), 60)
        if not fid:
            continue
        action = _text(op.get("action") or op.get("op"), 20).lower()
        fields = {}
        if _text(op.get("description"), 600):
            fields["description"] = _text(op.get("description"), 600)
        for key in ("due_from", "due_to", "importance", "notes"):
            if op.get(key) not in (None, ""):
                fields[key] = op.get(key)
        if action in ("pay", "paid", "payoff", "回收", "兑现", "close"):
            fields["status"] = "paid"
            fields["payoff_chapter"] = _int(op.get("payoff_chapter"), n)
        elif action in ("drop", "dropped", "abandon", "放弃", "废弃"):
            fields["status"] = "dropped"
        elif action in ("setup", "open", "plant", "埋设", "新增"):
            fields["status"] = "open"
            fields.setdefault("setup_chapter", n)
        delta.setdefault("foreshadowing", {})[fid] = fields
        delta.setdefault("foreshadow_ops", []).append({
            "id": fid, "action": action or "update", "fields": fields})


def summarize_and_extract(proj, ch_no: int, text: str, cfg=None) -> dict:
    """一次 LLM 提取章摘要 + 状态 delta + 伏笔操作；失败走确定性兜底，绝不阻断成章。"""
    n = max(1, _int(ch_no, 1))
    text = text if isinstance(text, str) else ""
    cfg2 = cfg if isinstance(cfg, dict) and cfg else _flat_config(proj)
    plan = _read_plan(proj)
    cards = _load_cards(proj)
    card = cards.get(n) or {}
    v, a = _find_arc(plan, n)

    out = {}
    if text.strip() and _llm_on(cfg2):
        system = ("你是长篇小说的连续性编辑。只从给定正文提取事实，不补充、不推断未出现的设定。"
                  "只输出严格 JSON。")
        user = (
            f"第{n}章《{card.get('title') or f'第{n}章'}》。\n"
            f"卷目标：{_text((v or {}).get('goal'), 200)}\n弧目标：{_text((a or {}).get('goal'), 200)}\n"
            "请提取：summary(200-400字，只写本章发生的事)、beats(3-6条情节节拍)、"
            "state_delta(人物/地点/物品/势力的状态变化，只写正文明确出现的)、"
            "foreshadow_ops(伏笔埋设/兑现/放弃；action=setup|pay|drop，id 自拟稳定短名)、"
            "issues(本章暴露的连续性问题)、quality_signals(score 0-100、hook、pacing)。\n"
            "JSON 结构：{\"summary\":\"\",\"beats\":[],\"state_delta\":{\"characters\":{},"
            "\"locations\":{},\"foreshadowing\":{},\"timeline\":[]},"
            "\"foreshadow_ops\":[],\"issues\":[],\"quality_signals\":{}}\n"
            f"正文：\n{text[:10000]}"
        )
        out = _llm_json(system, user, cfg2, "longform_summary", 2200)

    fallback = False
    summary = _text(out.get("summary"), 1200)
    beats = _str_list(out.get("beats"), 160, 8)
    if not summary:
        summary, auto_beats = _fallback_summary(text)
        fallback = True
        if not beats:
            beats = auto_beats
    if not summary:
        summary = f"（第{n}章正文为空或无法解析，未提取到事实摘要）"
        fallback = True
    state_delta = out.get("state_delta") if isinstance(out.get("state_delta"), dict) else {}
    state_delta = copy.deepcopy(state_delta)
    ops = [x for x in _list(out.get("foreshadow_ops")) if isinstance(x, dict)][:20]
    issues = _str_list(out.get("issues"), 300, 8)
    quality = out.get("quality_signals") if isinstance(out.get("quality_signals"), dict) else {}
    quality = dict(quality)
    quality.setdefault("score", 0)
    quality["fallback"] = fallback
    quality["chars"] = len(re.findall(r"[\u4e00-\u9fff]", text))

    state_delta.setdefault("chapter", n)
    _apply_foreshadow_ops(state_delta, ops, n)
    state_delta.setdefault("chapter_summaries", {})[str(n)] = {
        "summary": summary, "beats": beats, "promises": [],
        "state_changed": bool(state_delta.get("characters") or state_delta.get("foreshadowing")),
    }
    if not state_delta.get("timeline"):
        state_delta["timeline"] = [{
            "chapter": n, "time": "", "event": summary[:120],
            "location": (card.get("locations") or [""])[0],
            "characters": list(card.get("cast") or [])[:8],
        }]
    for detail in issues:
        state_delta.setdefault("issues", []).append(
            {"type": "continuity", "detail": detail, "chapter": n, "status": "open"})
    state_delta.setdefault("metrics", {"last_chapter": n, "chapters": n})
    state_delta["metrics"]["last_chapter"] = max(n, _int(state_delta["metrics"].get("last_chapter"), n))

    applied = True
    try:
        state_mod.apply_delta(_pdir(proj), n, state_delta, source="summarize")
    except Exception:
        applied = False

    record = {
        "n": n,
        "title": card.get("title") or f"第{n}章",
        "summary": summary,
        "beats": beats,
        "state_changed": bool(state_delta.get("characters") or state_delta.get("foreshadowing")),
        "foreshadow_ops": ops,
        "issues": issues,
        "quality_signals": quality,
        "fallback": fallback,
        "updated_at": _now(),
    }
    _upsert_summary_line(proj, n, record)

    # 更新章卡：状态 done，记录字数；不碰已写章卡的目标内容
    if card:
        card["status"] = "done"
        card["updated_at"] = _now()
        if not card.get("word_target"):
            card["word_target"] = WORDS_PER_CHAPTER
        cards[n] = card
        _write_cards(proj, cards)
    else:
        card = _empty_card(n)
        card["status"] = "done"
        card["updated_at"] = _now()
        cards[n] = card
        _write_cards(proj, cards)

    _write_volume_summary(proj, plan, cards)
    return {"n": n, "summary": summary, "beats": beats, "state_delta": state_delta,
            "foreshadow_ops": ops, "issues": issues, "quality_signals": quality,
            "fallback": fallback, "applied": applied}


def _upsert_summary_line(proj, n: int, record: dict) -> None:
    path = os.path.join(_pdir(proj), CHAPTER_SUMMARY)
    rows: dict[int, dict] = {}
    try:
        for line in open(path, encoding="utf-8").read().splitlines():
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict) and _int(obj.get("n"), 0) > 0:
                rows[_int(obj["n"])] = obj
    except OSError:
        pass
    rows[n] = record
    lines = [json.dumps(rows[k], ensure_ascii=False, separators=(",", ":")) for k in sorted(rows)]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def _write_volume_summary(proj, plan: dict, cards: dict[int, dict]) -> None:
    """滚动卷摘要：近期卷逐章一句话，旧卷压缩为一行（保持文件小而可读）。"""
    rows = _load_chapter_summaries(proj)
    lines = ["# 卷摘要（自动滚动生成，供长程上下文）", ""]
    volumes = [v for v in _list(plan.get("volumes")) if isinstance(v, dict)]
    done_vols = []
    for v in volumes:
        start, end = _int(v.get("start_chapter"), 0), _int(v.get("end_chapter"), 0)
        done = [n for n in range(start, end + 1) if cards.get(n, {}).get("status") == "done"]
        if done:
            done_vols.append((v, done))
    for v, done in done_vols[:-3]:
        lines.append(f"- 第{v.get('no')}卷「{v.get('title') or ''}」第{_int(v.get('start_chapter'),0)}–"
                     f"{_int(v.get('end_chapter'),0)}章，已完成 {len(done)} 章："
                     f"{_text((rows.get(done[-1]) or {}).get('summary'), 120)}")
    for v, done in done_vols[-3:]:
        lines.append("")
        lines.append(f"## 第{v.get('no')}卷「{v.get('title') or ''}」（第{_int(v.get('start_chapter'),0)}–"
                     f"{_int(v.get('end_chapter'),0)}章）")
        if v.get("goal"):
            lines.append(f"- 卷目标：{_text(v['goal'], 300)}")
        for n in done:
            sm = (rows.get(n) or {}).get("summary") or ""
            lines.append(f"- 第{n}章：{_text(sm, 160)}")
    atomic_write_text(os.path.join(_pdir(proj), VOLUME_SUMMARY), "\n".join(lines) + "\n")


# ---------- 确定性审计 ----------

def _card_range(cards: dict[int, dict], start: int, end: int) -> list[dict]:
    return [cards[n] for n in range(start, end + 1) if n in cards]


def _overdue_in_range(st: dict, start: int, end: int) -> list[str]:
    out = []
    for f in state_mod.foreshadow_due(st, max(1, end))["overdue"]:
        due_to = _int(f.get("due_to"), 0)
        if due_to and due_to >= start:
            out.append(f"伏笔「{f.get('id')}」计划第{due_to}章兑现，截至第{end}章仍未回收")
    return out


def _absence_issues(st: dict, start: int, end: int, limit: int = 5) -> list[str]:
    out = []
    for name, c in st.get("characters", {}).items():
        last = _int(c.get("last_seen"), 0)
        if last and last < start and end - last >= 15:
            out.append(f"人物「{name}」已连续 {end - last} 章未出场（最近第{last}章）")
        if len(out) >= limit:
            break
    return out


def audit_arc(proj, arc_no) -> list[str]:
    """单弧确定性检查：章卡缺失/目标未覆盖/伏笔逾期/人物失踪/节奏单调。"""
    arc_id = _int(arc_no, -1)
    plan = _read_plan(proj)
    arcs = _all_arcs(plan)
    arc = next((a for a in arcs if _int(a.get("no"), 0) == arc_id), None)
    if not arc:
        return [f"未找到第{arc_no}弧（规划缺失或弧号越界）"]
    start, end = _int(arc.get("start_chapter"), 1), _int(arc.get("end_chapter"), 1)
    cards = _load_cards(proj)
    have = _card_range(cards, start, end)
    issues = []
    missing = [n for n in range(start, end + 1) if n not in cards]
    if missing:
        issues.append(f"第{arc_id}弧缺少 {len(missing)} 张章卡（如第{missing[0]}章）")
    if have:
        empty_goal = sum(1 for c in have if not c.get("goal"))
        if empty_goal:
            issues.append(f"第{arc_id}弧有 {empty_goal}/{len(have)} 章章卡没有目标")
        goals = {c.get("goal") for c in have if c.get("goal")}
        if len(have) > 3 and len(goals) <= 1:
            issues.append(f"第{arc_id}弧章卡目标高度重复，节奏可能单调")
        no_hook = sum(1 for c in have if not c.get("cliffhanger"))
        if len(have) > 3 and no_hook > len(have) * 0.6:
            issues.append(f"第{arc_id}弧 {no_hook}/{len(have)} 章没有章末钩子")
    st = state_mod.load(_pdir(proj))
    issues.extend(_overdue_in_range(st, start, end))
    issues.extend(_absence_issues(st, start, end))
    return issues[:40]


def audit_volume(proj, volume_no) -> list[str]:
    vno = _int(volume_no, -1)
    plan = _read_plan(proj)
    vol = next((v for v in _list(plan.get("volumes"))
                if isinstance(v, dict) and _int(v.get("no"), 0) == vno), None)
    if not vol:
        return [f"未找到第{volume_no}卷（规划缺失或卷号越界）"]
    start, end = _int(vol.get("start_chapter"), 1), _int(vol.get("end_chapter"), 1)
    cards = _load_cards(proj)
    have = _card_range(cards, start, end)
    issues = []
    missing = [n for n in range(start, end + 1) if n not in cards]
    if missing:
        issues.append(f"第{vno}卷缺少 {len(missing)} 张章卡（如第{missing[0]}章）")
    for a in _list(vol.get("arcs")):
        if isinstance(a, dict) and not _text(a.get("goal"), 400):
            issues.append(f"第{a.get('no')}弧缺少弧目标")
    # 字数偏差：只看已完成章的平均字数
    flat = {_int(c.get("chapterNumber"), 0): c for c in _flat_chapters(proj)}
    words = [_int((flat.get(n) or {}).get("wordCount"), 0) for n in range(start, end + 1)
             if cards.get(n, {}).get("status") == "done"]
    words = [w for w in words if w > 0]
    target = _int((have[0] if have else {}).get("word_target"), WORDS_PER_CHAPTER)
    if len(words) >= 3:
        avg = sum(words) / len(words)
        if avg < target * 0.8:
            issues.append(f"第{vno}卷平均 {int(avg)} 字/章，低于目标 {target} 的 80%")
        elif avg > target * 1.6:
            issues.append(f"第{vno}卷平均 {int(avg)} 字/章，超过目标 {target} 的 160%")
    st = state_mod.load(_pdir(proj))
    issues.extend(_overdue_in_range(st, start, end))
    issues.extend(_absence_issues(st, start, end))
    return issues[:50]


def audit_book(proj) -> list[str]:
    plan = _read_plan(proj)
    if not plan.get("volumes"):
        return ["尚未生成分层总规划（02c-总规划.json）"]
    total = max(1, _int((plan.get("scale") or {}).get("chapters"), 1))
    cards = _load_cards(proj)
    issues = []
    pos = 1
    for v in _list(plan.get("volumes")):
        if not isinstance(v, dict):
            continue
        start, end = _int(v.get("start_chapter"), 0), _int(v.get("end_chapter"), 0)
        if start != pos:
            issues.append(f"卷区间不连续：第{v.get('no')}卷应从第{pos}章开始，实际第{start}章")
        pos = end + 1
    if pos - 1 != total:
        issues.append(f"卷区间覆盖到第{pos - 1}章，与总章数 {total} 不一致")
    missing = [n for n in range(1, total + 1) if n not in cards]
    if missing:
        issues.append(f"全书缺少 {len(missing)} 张章卡（如第{missing[0]}章）")
    done = _done_boundary(cards)
    st = state_mod.load(_pdir(proj))
    overdue = state_mod.foreshadow_due(st, max(1, done))["overdue"]
    if overdue:
        issues.append(f"截至第{done}章有 {len(overdue)} 条伏笔逾期（如「{overdue[0].get('id')}」）")
    issues.extend(state_mod.validate(st)[:5])
    # 节奏单调：连续 10 章目标完全相同
    goals = [(n, _text(cards[n].get("goal"), 200)) for n in sorted(cards) if cards[n].get("goal")]
    run, prev = 0, None
    for _n, g in goals:
        run = run + 1 if g == prev else 1
        prev = g
        if run == 10:
            issues.append("检测到连续 10 章章卡目标完全相同，节奏可能单调")
            break
    return issues[:60]


# ---------- 反向摘要 / digest / 兼容投影 ----------

def reverse_outline(proj, upto_chapter=None) -> str:
    """纯 Python 生成"当前故事到哪儿了"，给作者与重规划用。"""
    plan = _read_plan(proj)
    cards = _load_cards(proj)
    st = state_mod.load(_pdir(proj))
    done = _done_boundary(cards)
    upto = min(done, _int(upto_chapter, done) or done) if done else _int(upto_chapter, 0)
    rows = _load_chapter_summaries(proj)
    lines = [f"# 当前故事进度（截至第{upto}章）", ""]
    if plan.get("volumes"):
        v, a = _find_arc(plan, max(1, upto + 1))
        lines.append("## 结构位置")
        if v:
            lines.append(f"- 第{v.get('no')}卷「{v.get('title') or ''}」：{_text(v.get('goal'), 240)}")
        if a:
            lines.append(f"- 第{a.get('no')}弧「{a.get('title') or ''}」：{_text(a.get('goal'), 240)}")
    if upto:
        lines.append("")
        lines.append("## 最近 5 章")
        for n in range(max(1, upto - 4), upto + 1):
            title = (cards.get(n) or {}).get("title") or f"第{n}章"
            sm = (rows.get(n) or {}).get("summary") or ""
            lines.append(f"- 第{n}章「{title}」：{_text(sm, 200)}")
        active = []
        for name, c in st["characters"].items():
            if _int(c.get("last_seen"), 0) >= upto - 10:
                active.append(f"{name}（{c.get('status') or '在场'}，{c.get('location') or '未知'}）")
        if active:
            lines.append("")
            lines.append("## 当前活跃人物")
            lines.append("- " + "；".join(active[:8]))
    due = state_mod.foreshadow_due(st, max(1, upto), lookahead=20)
    open_fs = due["overdue"] + due["due"] + due["upcoming"]
    if open_fs:
        lines.append("")
        lines.append("## 未兑现伏笔")
        for f in open_fs[:8]:
            lines.append(f"- [{f.get('id')}] {_text(f.get('description'), 160)}"
                         f"（第{f.get('due_from') or '?'}–{f.get('due_to') or '?'}章）")
    nxt = cards.get(upto + 1)
    if nxt:
        lines.append("")
        lines.append("## 下一章")
        lines.append(render_card(nxt))
    text = "\n".join(lines)
    return text[:6000]


def plan_digest(proj) -> dict:
    """给前端/日志的小摘要；不读正文文件，只看 02c/02d/18。"""
    plan = _read_plan(proj)
    cards = _load_cards(proj)
    st = state_mod.load(_pdir(proj))
    scale = plan.get("scale") if isinstance(plan.get("scale"), dict) else {}
    total = _int(scale.get("chapters"), max(cards, default=0))
    if not total and hasattr(proj, "plan"):
        flat = proj.plan() or {}
        total = _int(flat.get("totalChapters"), len(flat.get("chapters") or []))
    written = sum(1 for c in cards.values() if c.get("status") == "done")
    done = _done_boundary(cards)
    next_no = min(total, done + 1) if total else done + 1
    v, a = _find_arc(plan, max(1, next_no))
    if not v:
        v = _find_volume(plan, max(1, next_no))
    flat_words = 0
    for c in _flat_chapters(proj):
        if _norm_status_card(c.get("status")) == "done":
            flat_words += _int(c.get("wordCount"), 0)
    target = _int(scale.get("target_words"), total * WORDS_PER_CHAPTER)
    if not flat_words:
        flat_words = _int(st["metrics"].get("words"), 0)
    due = state_mod.foreshadow_due(st, max(1, done), lookahead=15)
    open_n = sum(1 for f in st["foreshadowing"].values() if f.get("status") == "open")
    paid_n = sum(1 for f in st["foreshadowing"].values() if f.get("status") == "paid")
    issues = state_mod.validate(st)[:5]
    if len(due["overdue"]) >= 3:
        issues.append(f"{len(due['overdue'])} 条伏笔逾期未兑现")
    audit = _read_json(os.path.join(_pdir(proj), "22-规划体检.json"), {})
    audit = audit if isinstance(audit, dict) else {}
    return {
        "scale": {"target_words": target, "chapters": total,
                  "volumes": _int(scale.get("volumes"), len(_list(plan.get("volumes"))))},
        "mode": plan.get("mode") or "outline",
        "volumes": len(_list(plan.get("volumes"))),
        "arcs": len(_all_arcs(plan)),
        "chapters_planned": len(cards) or total,
        "chapters_written": written,
        "current_volume": {"no": _int((v or {}).get("no"), 0),
                           "title": _text((v or {}).get("title"), 80)} if v else None,
        "current_arc": {"no": _int((a or {}).get("no"), 0),
                        "title": _text((a or {}).get("title"), 80)} if a else None,
        "target_words": target,
        "word_progress": round(min(1.0, flat_words / target), 4) if target else 0.0,
        "foreshadow": {"open": open_n, "overdue": len(due["overdue"]), "paid": paid_n},
        "issues": issues[:8],
        "summary": (f"已写 {written}/{total} 章 · {flat_words}/{target} 字"
                    + (f" · 当前第{v.get('no')}卷《{v.get('title')}》" if v else "")
                    + (f" 第{a.get('no')}弧" if a else "")),
        "last_audit": {"at": audit.get("at"), "scope": audit.get("scope"),
                       "issues": list(audit.get("issues") or [])[:8]} if audit else None,
    }


def plan_to_flat(proj) -> dict:
    """把层次章卡投影回旧 plan["chapters"]：只补未来章，不删除/不改已写章。"""
    cards = _load_cards(proj)
    plan = proj.plan() if hasattr(proj, "plan") else None
    plan = plan if isinstance(plan, dict) else _read_json(os.path.join(_pdir(proj), FLAT_PLAN), {})
    plan = plan if isinstance(plan, dict) else {}
    if not cards:
        return plan
    chapters = [c for c in _list(plan.get("chapters")) if isinstance(c, dict)]
    by_no = {_int(c.get("chapterNumber"), 0): c for c in chapters}
    total = max(_int(plan.get("totalChapters"), 0), len(chapters), max(cards))
    for n in sorted(cards):
        card = cards[n]
        cur = by_no.get(n)
        if cur is not None:
            status = _norm_status_card(cur.get("status"))
            if status in ("done", "writing"):
                # 已写/正在写章节：一个字都不动（文件路径、标题、字数、状态）
                continue
            if card.get("title"):
                cur["title"] = card["title"]
            cur["status"] = "pending" if card.get("status") in ("planned", "changed") else cur.get("status", "pending")
            cur["filePath"] = cur.get("filePath") or f"第{n:02d}章-{_safe_filename(card.get('title')) or '未命名'}.md"
            by_no[n] = cur
            continue
        title = card.get("title") or f"第{n}章"
        entry = {
            "chapterNumber": n,
            "title": title,
            "filePath": f"第{n:02d}章-{_safe_filename(title) or '未命名'}.md",
            "status": "completed" if card.get("status") == "done" else (
                "in_progress" if card.get("status") == "writing" else "pending"),
            "wordCount": None, "wordCountPass": None, "retryCount": 0,
        }
        if card.get("status") == "done":
            entry["wordCount"] = _int(card.get("word_count"), 0) or None
        chapters.append(entry)
        by_no[n] = entry
    plan["chapters"] = sorted(chapters, key=lambda c: _int(c.get("chapterNumber"), 0))
    plan["totalChapters"] = max(total, len(plan["chapters"]))
    plan.setdefault("novelName", getattr(proj, "name", "") or "未命名")
    plan.setdefault("minWordsPerChapter", WORDS_PER_CHAPTER)
    plan.setdefault("version", 1)
    plan.setdefault("status", "pending")
    plan.setdefault("writingMode", "serial")
    plan.setdefault("config", _flat_config(proj))
    plan.setdefault("titles", [])
    if hasattr(proj, "save_plan"):
        proj.save_plan(plan)
    return plan


# ---------- 日志 ----------

def _log(msg: str, proj=None) -> None:
    """只记录关键规划事件，不逐章刷屏。"""
    try:
        if proj is not None and hasattr(proj, "log"):
            proj.log(msg)
    except Exception:
        pass
