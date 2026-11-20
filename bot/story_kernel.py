"""v5.0 长篇质量内核：事实源 + 故事契约 + 章卡契约 + 写前/写后闸门。

设计目标
--------
把“写完再体检”改成“写前有契约、写中有拦截、写后有追责”。只对启用
``config.v5_kernel`` 的新项目生效；老项目没有该开关，行为完全不变。

核心文件（都只在新项目里生成，不改老作品）：
  00-作品事实源.json   唯一事实源：书名/主角/核心人物/终局契约/红线
  01-故事契约.json     读者承诺/终局证据/人物弧/反派阶梯
  v5-质量账本.jsonl    每次写前/写后闸门的判断记录（可回原文核对）

纯确定性、零 LLM；所有检查都能给出章节号与证据，不靠模型自评。
"""
from __future__ import annotations

import datetime
import json
import os
import re

from .safeio import atomic_write_json, atomic_write_text

FACTS_FILE = "00-作品事实源.json"
CONTRACT_FILE = "01-故事契约.json"
LEDGER_FILE = "v5-质量账本.jsonl"
V5_FLAG = "v5_kernel"

SCENE_TYPES = ("谈判", "核验", "动作", "揭示", "关系", "余波", "调查", "舆论", "家庭", "独处")
_PAUSE_TAGS = ("没有回答", "没有接话", "几秒", "停了一下", "顿了一下", "沉默")
_GENERIC_CONFLICT = ("局势", "环境", "时代压力", "命运", "所有人", "外界")


# ---------------------------------------------------------------- 基础读取

def _pdir(proj) -> str:
    return os.path.abspath(getattr(proj, "path", "") or "")


def _plan(proj) -> dict:
    try:
        p = proj.plan() if hasattr(proj, "plan") else None
        return p if isinstance(p, dict) else {}
    except Exception:
        return {}


def _cfg(proj) -> dict:
    c = (_plan(proj).get("config") or {})
    return c if isinstance(c, dict) else {}


def _read(path: str, cap: int = 2_000_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[:cap]
    except OSError:
        return ""


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, (dict, list)) else default
    except Exception:
        return default


def _cn(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def _text(v, cap: int = 400) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:cap]


def _norm_title(s: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(s or ""))


def enabled(proj) -> bool:
    """只对显式开启 v5 的项目生效；老项目不受影响。"""
    try:
        if bool(_cfg(proj).get(V5_FLAG)):
            return True
        return os.path.exists(os.path.join(_pdir(proj), FACTS_FILE))
    except Exception:
        return False


def facts_path(proj) -> str:
    return os.path.join(_pdir(proj), FACTS_FILE)


def contract_path(proj) -> str:
    return os.path.join(_pdir(proj), CONTRACT_FILE)


def load_facts(proj) -> dict:
    d = _read_json(facts_path(proj), {})
    return d if isinstance(d, dict) else {}


def load_contract(proj) -> dict:
    d = _read_json(contract_path(proj), {})
    return d if isinstance(d, dict) else {}


# ---------------------------------------------------------------- 人物档案解析

def _parse_cast(text: str) -> list[dict]:
    """从 00-人物档案.md 的 `## 人物：X` 小节抽结构化人物卡。"""
    out = []
    if not text:
        return out
    blocks = re.split(r"(?m)^##\s*人物[：:]\s*", text)
    for block in blocks[1:]:
        lines = block.splitlines()
        if not lines:
            continue
        name = _text(lines[0], 40)
        if not name or len(name) > 12:
            continue
        fields = {}
        for ln in lines[1:]:
            m = re.match(r"^\s*-\s*([^：:]{1,24})[：:]\s*(.*)$", ln)
            if m:
                fields[_text(m.group(1), 30)] = _text(m.group(2), 600)
        rec = {
            "name": name,
            "role": fields.get("身份/资源") or fields.get("身份") or "",
            "personality": fields.get("性格内核") or "",
            "want": fields.get("想要 Want") or fields.get("想要") or "",
            "need": fields.get("欠缺 Need") or fields.get("欠缺") or "",
            "flaw": fields.get("致命缺陷") or "",
            "voice": fields.get("说话方式") or "",
            "relations": fields.get("关系/立场") or "",
            "knowledge": fields.get("知情范围") or "",
            "arc": fields.get("弧光") or "",
            "mark": fields.get("标志细节") or "",
            "aliases": [],
            "first_chapter": 1,
            "exit_chapter": 0,
        }
        out.append(rec)
    return out


def _title_motifs(title: str) -> list[str]:
    try:
        from . import canon
        return canon.title_motifs(title) or []
    except Exception:
        return []


def _default_logline(title: str, protagonist: str, deuteragonist: str, genre: str) -> str:
    who = protagonist or "主角"
    other = deuteragonist or "对手"
    return (f"《{title}》是一部{genre or '长篇'}：{who}在被当众否定之后，"
            f"必须在不暴露全部底牌的前提下，与{other}争夺主动权，并最终为一次选择付出代价。")


def _default_theme() -> dict:
    return {
        "question": "一个人被当众否定之后，是靠证明自己赢回尊重，还是靠承担代价守住选择？",
        "answer": "真正的尊重不是靠隐藏身份换来的，而是靠一次次可被验证的选择与代价换来的。",
    }


def ensure_project(proj, cfg: dict | None = None, *, name: str = "",
                   characters_text: str = "", outline_text: str = "") -> dict:
    """给新项目生成唯一事实源与故事契约；已存在则原样返回，绝不覆盖。"""
    pdir = _pdir(proj)
    if not pdir:
        return {}
    if os.path.exists(facts_path(proj)):
        return load_facts(proj)
    plan = _plan(proj)
    cfg = cfg if isinstance(cfg, dict) else _cfg(proj)
    title = _text(name or plan.get("novelName") or getattr(proj, "name", "") or "未命名", 80)
    total = max(1, int(plan.get("totalChapters") or cfg.get("chapters") or 1))
    per = max(1500, int(plan.get("minWordsPerChapter") or cfg.get("min_words_per_chapter") or 3000))
    genre = _text(cfg.get("genre") or cfg.get("genre_key") or "长篇", 40)

    characters_text = characters_text or _read(os.path.join(pdir, "00-人物档案.md"))
    cast = _parse_cast(characters_text)
    protagonist = _text(cfg.get("protagonist") or cfg.get("lead") or "", 40)
    deuteragonist = _text(cfg.get("deuteragonist") or cfg.get("female_lead") or "", 40)
    if not cast:
        cast = [{"name": protagonist or "主角", "role": "主角", "voice": "", "want": "",
                 "need": "", "flaw": "", "relations": "", "aliases": [], "first_chapter": 1,
                 "exit_chapter": 0}]
    if not protagonist:
        protagonist = cast[0]["name"]
    if not deuteragonist and len(cast) > 1:
        deuteragonist = cast[1]["name"]
    for nm in (protagonist, deuteragonist):
        if nm and not any(c["name"] == nm for c in cast):
            cast.insert(0, {"name": nm, "role": "主角" if nm == protagonist else "主要角色",
                            "voice": "", "want": "", "need": "", "flaw": "",
                            "relations": "", "aliases": [], "first_chapter": 1,
                            "exit_chapter": 0})

    logline = _text(cfg.get("logline") or cfg.get("idea") or cfg.get("premise"), 600)
    if len(logline) < 20:
        logline = _default_logline(title, protagonist, deuteragonist, genre)
    theme = _default_theme()
    if isinstance(cfg.get("theme"), dict):
        theme.update({k: _text(v, 300) for k, v in cfg["theme"].items() if v})

    motifs = _title_motifs(title)
    promises = []
    for i, m in enumerate(motifs[:3], 1):
        promises.append({
            "id": f"P{i}",
            "text": f"书名/主题承诺「{m}」必须在正文里作为事件兑现，不能只出现在标题。",
            "keywords": [m],
            "first_hit": 3,
            "window": 10,
            "final_hit": total,
        })
    if len(promises) < 2:
        promises.append({
            "id": f"P{len(promises) + 1}",
            "text": "主线目标必须每 10 章至少推进一次，且至少有一次不可逆变化。",
            "keywords": [],
            "first_hit": 5, "window": 10, "final_hit": total,
        })
    if len(promises) < 3:
        promises.append({
            "id": f"P{len(promises) + 1}",
            "text": "主要对手必须每 10 章升级一次资源、权限或代价，不能原地重复。",
            "keywords": [],
            "first_hit": 5, "window": 10, "final_hit": total,
        })
    promises.append({
        "id": f"P{len(promises) + 1}",
        "text": "情感/关系线必须每 10 章有一次实质变化，终局落定。",
        "keywords": ["选择", "婚礼", "在一起", "领证"],
        "first_hit": 5,
        "window": 10,
        "final_hit": total,
    })
    ending = [
        {"id": "E1", "text": "主角的核心目标有明确结果",
         "keywords": [protagonist] if protagonist else [], "evidence_chapter": 0, "status": "open"},
        {"id": "E2", "text": "主要对手败亡、退出或失去关键资源",
         "keywords": [deuteragonist] if deuteragonist else [], "evidence_chapter": 0, "status": "open"},
        {"id": "E3", "text": "核心谜题/旧案揭开并有结论",
         "keywords": ["真相", "旧案", "证据", "揭开", "幕后"], "evidence_chapter": 0, "status": "open"},
        {"id": "E4", "text": "男女主关系落定（选择/婚礼/共同承担）",
         "keywords": ["婚礼", "在一起", "领证", "选择", "共同承担"], "evidence_chapter": 0, "status": "open"},
        {"id": "E5", "text": "书名承诺在正文中作为事件兑现",
         "keywords": motifs[:3], "evidence_chapter": 0, "status": "open"},
    ]
    # 反派阶梯：从人物档案里挑身份含对手/反派/竞争/总裁/董事长的角色，最多 4 级
    ladder = []
    for c in cast:
        role = str(c.get("role") or "")
        if any(k in role for k in ("反派", "对手", "竞争", "敌", "总裁", "董事长", "老板")):
            ladder.append({"level": len(ladder) + 1, "name": c["name"], "goal": "",
                           "resource": role[:80], "weakness": "", "defeat_chapter": 0})
        if len(ladder) >= 4:
            break
    if not ladder and len(cast) > 1:
        ladder = [{"level": 1, "name": c["name"], "goal": "", "resource": str(c.get("role") or "")[:80],
                   "weakness": "", "defeat_chapter": 0} for c in cast[1:4]]

    facts = {
        "version": 5,
        "status": "seed",
        "title": title,
        "aliases": [],
        "genre": genre,
        "platform": _text(cfg.get("platform") or "", 40),
        "target_chapters": total,
        "target_words_per_chapter": per,
        "logline": logline,
        "theme_question": theme["question"],
        "theme_answer": theme["answer"],
        "pov": _text(cfg.get("perspective") or "第三人称有限视角", 80),
        "protagonist": protagonist,
        "deuteragonist": deuteragonist,
        "core_cast": cast,
        "antagonist_ladder": ladder,
        "world_rules": _listify(cfg.get("world_rules")) or (
            ["现代都市，无超自然力量"] if any(k in genre for k in ("都市", "情感", "豪门")) else []),
        "timeline_origin": "第1章",
        "locations": _listify(cfg.get("locations")),
        "ending_contract": ending,
        "red_lines": [
            "主角不得连续 2 章无主动行动",
            "同一场景类型不得连续 3 章",
            "重大胜利必须在 2 章内付出具体代价",
            "最后 5 章不得开新的核心谜",
            "书名承诺不得只出现在标题",
        ],
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    contract = {
        "version": 5,
        "title": title,
        "logline": logline,
        "theme": theme,
        "promises": promises,
        "ending_contract": ending,
        "character_arcs": [{
            "name": c["name"], "start_lie": c.get("flaw") or "", "want": c.get("want") or "",
            "need": c.get("need") or "", "break_point": "", "final_choice": "",
        } for c in cast[:8]],
        "antagonist_ladder": ladder,
        "volume_turns": [],
        "red_lines": facts["red_lines"],
        "updated_at": facts["updated_at"],
    }
    atomic_write_json(facts_path(proj), facts)
    atomic_write_json(contract_path(proj), contract)
    return facts


def _listify(v) -> list:
    if isinstance(v, list):
        return [_text(x, 120) for x in v if _text(x, 120)]
    if isinstance(v, str) and v.strip():
        return [x.strip() for x in re.split(r"[、,，;；\n]", v) if x.strip()]
    return []


# ---------------------------------------------------------------- 事实源体检

def fact_lint(proj) -> list[dict]:
    out = []
    facts = load_facts(proj)
    if not facts:
        return [_f("block", "V5F00", "缺少 00-作品事实源.json；v5 项目不能开写", "")]
    plan = _plan(proj)
    title = _text(facts.get("title"), 80)
    plan_title = _text(plan.get("novelName") or plan.get("title"), 80)
    if title and plan_title and _norm_title(title) != _norm_title(plan_title):
        out.append(_f("block", "V5F01", f"事实源书名「{title}」与计划书名「{plan_title}」不一致", ""))

    bible = _read(os.path.join(_pdir(proj), "09-故事圣经.md"), 3000)
    first = ""
    for ln in bible.splitlines():
        if ln.strip().startswith("#"):
            first = ln.strip().lstrip("#").strip()
            break
    if first and first not in ("故事圣经", "故事圣经 ") and title:
        if title not in first and first not in title:
            out.append(_f("block", "V5F02",
                          f"故事圣经标题「{first}」与事实源书名「{title}」不是同一本书", ""))

    outline = _read(os.path.join(_pdir(proj), "01-大纲.md"), 4000)
    m = re.search(r"总章数\s*[|｜]\s*(\d{1,4})", outline)
    if m:
        n = int(m.group(1))
        total = int(facts.get("target_chapters") or 0)
        if total >= 60 and n and abs(n - total) > 5:
            out.append(_f("block", "V5F03", f"大纲写 {n} 章，事实源写 {total} 章", ""))

    cast = facts.get("core_cast") or []
    names = {str(c.get("name") or "").strip() for c in cast if isinstance(c, dict)}
    prof = _read(os.path.join(_pdir(proj), "00-人物档案.md"), 20000)
    pnames = set(re.findall(r"(?m)^##\s*人物[：:]\s*([^\n]{1,12})", prof))
    missing = sorted(x.strip() for x in pnames if x.strip() and x.strip() not in names)
    if missing:
        out.append(_f("block", "V5F04",
                      f"人物档案有、事实源没登记：{'、'.join(missing[:6])}", ""))
    extra = sorted(x for x in names if x and x not in pnames) if pnames else []
    if extra:
        out.append(_f("warn", "V5F05",
                      f"事实源有、人物档案没写：{'、'.join(extra[:6])}", ""))

    p = str(facts.get("protagonist") or "").strip()
    if not p or p not in names:
        out.append(_f("block", "V5F06", "事实源缺少主角，或主角不在 core_cast 里", ""))
    if len([c for c in cast if isinstance(c, dict) and c.get("name")]) < 2:
        out.append(_f("warn", "V5F07", "核心人物少于 2 人，冲突会退化成独角戏", ""))

    contract = load_contract(proj)
    if len(contract.get("promises") or []) < 3:
        out.append(_f("block", "V5F08", "故事契约少于 3 条读者承诺", ""))
    if len(contract.get("ending_contract") or facts.get("ending_contract") or []) < 5:
        out.append(_f("block", "V5F09", "终局契约少于 5 条", ""))
    if len(_text(facts.get("logline"), 600)) < 20:
        out.append(_f("warn", "V5F10", "一句话故事太短，无法约束主线", ""))
    return out


def _f(level: str, code: str, message: str, evidence: str = "", chapter: int = 0) -> dict:
    return {"level": level, "code": code, "message": message,
            "evidence": _text(evidence, 160), "chapter": int(chapter or 0)}


# ---------------------------------------------------------------- 章卡体检

def _jaccard(a: str, b: str, n: int = 3) -> float:
    def grams(s):
        s = re.sub(r"\s", "", s or "")
        return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}
    A, B = grams(a), grams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / max(1, len(A | B))


def card_lint(proj, card: dict, no: int = 0, prev_cards: dict | None = None) -> list[dict]:
    out = []
    c = card if isinstance(card, dict) else {}
    n = int(no or c.get("n") or 0)
    facts = load_facts(proj)
    protagonist = str(facts.get("protagonist") or "").strip()

    goal = str(c.get("goal") or "")
    if any(k in goal for k in ("**场景分镜", "场景分镜", "核心事件展开**", "**伏笔")):
        out.append(_f("block", "V5C01", "goal 是原始大纲倾倒，不是一句话目标；必须把 bullets 移到 beats", "", n))
    if len(goal) > 240:
        out.append(_f("block", "V5C02", f"goal 过长（{len(goal)} 字），本章没有单一可验收目标", goal[:80], n))
    for key, code, label in (("conflict", "V5C03", "核心冲突"), ("turn", "V5C04", "转折"),
                             ("payoff", "V5C05", "本章兑现"), ("cliffhanger", "V5C06", "章末钩子")):
        if not str(c.get(key) or "").strip():
            out.append(_f("block", code, f"章卡缺少{label}", "", n))
    conflict = str(c.get("conflict") or "")
    if conflict and any(k in conflict for k in _GENERIC_CONFLICT) and len(conflict) < 80:
        out.append(_f("warn", "V5C07", "核心冲突太抽象，缺少具名对手/具体阻力", conflict[:80], n))

    scenes = c.get("scenes") or []
    if len(scenes) < 3:
        out.append(_f("block", "V5C08", f"场景少于 3 场（当前 {len(scenes)}）", "", n))
    for i, s in enumerate(scenes[:8], 1):
        if not isinstance(s, dict):
            out.append(_f("block", "V5C09", f"第{i}场不是结构化场景", "", n))
            continue
        missing = [k for k in ("want", "obstacle", "choice", "cost", "outcome") if not str(s.get(k) or "").strip()]
        if missing:
            out.append(_f("block", "V5C10", f"第{i}场缺少：{'、'.join(missing)}", "", n))

    cast = [str(x).strip() for x in (c.get("cast") or []) if str(x).strip()]
    if len(cast) < 2:
        out.append(_f("block", "V5C11", f"cast 少于 2 人（当前 {len(cast)}）", "", n))
    if protagonist and cast and protagonist not in cast:
        out.append(_f("block", "V5C12", f"主角「{protagonist}」不在本章 cast 里", "", n))
    if not c.get("locations"):
        out.append(_f("block", "V5C13", "本章没有地点（locations 为空）", "", n))
    if not str(c.get("protagonist_action") or "").strip():
        out.append(_f("block", "V5C14", "缺少 protagonist_action（主角本章主动做了什么）", "", n))
    threads = c.get("threads_advance") or []
    if not threads:
        out.append(_f("block", "V5C15", "缺少 threads_advance（本章推进了哪条线）", "", n))
    elif not any(isinstance(t, dict) and t.get("irreversible") for t in threads):
        out.append(_f("block", "V5C16", "threads_advance 里没有 irreversible=true 的不可逆变化", "", n))
    stype = str(c.get("scene_type") or "").strip()
    if stype not in SCENE_TYPES:
        out.append(_f("block", "V5C17", f"scene_type「{stype or '空'}」不在类型库", "", n))
    if not str(c.get("story_time") or "").strip():
        out.append(_f("block", "V5C18", "缺少 story_time（故事内时间）", "", n))
    wt = int(c.get("word_target") or 0)
    target = int(facts.get("target_words_per_chapter") or 3000)
    if wt and (wt < int(target * 0.8) or wt > int(target * 1.3)):
        out.append(_f("warn", "V5C19", f"word_target={wt}，与目标 {target} 偏差过大", "", n))
    if str(c.get("title") or "").strip() in ("", f"第{n}章"):
        out.append(_f("warn", "V5C20", "章标题还是默认编号", "", n))

    if isinstance(prev_cards, dict):
        prev = [prev_cards.get(n - k) for k in (1, 2) if isinstance(prev_cards.get(n - k), dict)]
        if stype and len(prev) == 2 and all(str(x.get("scene_type") or "") == stype for x in prev):
            out.append(_f("block", "V5C21", f"连续 3 章同一场景类型「{stype}」，触发结构循环", "", n))
        if goal:
            for x in prev[:1]:
                if _jaccard(goal, str(x.get("goal") or "")) > 0.6:
                    out.append(_f("warn", "V5C22", "本章目标与上一章高度相似", goal[:80], n))
    return out


# ---------------------------------------------------------------- 写前 / 写后

def preflight(proj, card: dict, no: int = 0, cfg: dict | None = None) -> dict:
    if not enabled(proj):
        return {"ok": True, "blocks": [], "warns": [], "directives": []}
    facts = ensure_project(proj, cfg or _cfg(proj))
    if not facts:
        return {"ok": False, "blocks": [_f("block", "V5F00", "事实源为空", "")],
                "warns": [], "directives": ["[V5F00] 先补 00-作品事实源.json"]}
    try:
        from . import longform
        cards = longform.load_cards(proj)
    except Exception:
        cards = {}
    card = card if isinstance(card, dict) and card else cards.get(int(no or 0), {})
    if not card:
        try:
            from . import longform
            card = longform.build_chapter_card(proj, int(no or 1), cfg or _cfg(proj))
        except Exception:
            card = {}
    findings = fact_lint(proj) + card_lint(proj, card, no, cards)
    blocks = [f for f in findings if f["level"] == "block"]
    warns = [f for f in findings if f["level"] == "warn"]
    directives = [f"[{f['code']}] {f['message']}" for f in blocks]
    _ledger(proj, {"stage": "preflight", "chapter": int(no or 0),
                   "blocks": blocks, "warns": warns})
    return {"ok": not blocks, "blocks": blocks, "warns": warns, "directives": directives}


def _dialogue(text: str) -> dict[str, list[str]]:
    pat = re.compile(r"([\u4e00-\u9fff]{2,4})(?:说|问|道|答|笑|开口|低声|沉声|声音|看着她|看着他)"
                     r"[^“”]{0,20}“([^”]{1,220})”")
    out: dict[str, list[str]] = {}
    for m in pat.finditer(text or ""):
        name, line = m.group(1), m.group(2)
        out.setdefault(name, []).append(line)
    return out


def _voice_similarity(proj, text: str) -> list[dict]:
    facts = load_facts(proj)
    cast = [c for c in (facts.get("core_cast") or []) if isinstance(c, dict)]
    names = {str(c.get("name") or "") for c in cast}
    dlg = {k: v for k, v in _dialogue(text).items() if k in names and len(v) >= 8}
    out = []
    keys = list(dlg)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = keys[i], keys[j]
            la = sum(_cn(x) for x in dlg[a]) / max(1, len(dlg[a]))
            lb = sum(_cn(x) for x in dlg[b]) / max(1, len(dlg[b]))
            qa = sum(1 for x in dlg[a] if "？" in x or "?" in x) / max(1, len(dlg[a]))
            qb = sum(1 for x in dlg[b] if "？" in x or "?" in x) / max(1, len(dlg[b]))
            oa = {x[:3] for x in dlg[a]}
            ob = {x[:3] for x in dlg[b]}
            overlap = len(oa & ob) / max(1, len(oa | ob))
            if abs(la - lb) <= 2 and abs(qa - qb) <= 0.1 and overlap >= 0.35:
                out.append(_f("warn", "V5Q04",
                              f"「{a}」与「{b}」台词声音太像：平均 {la:.0f}/{lb:.0f} 字，"
                              f"问句率 {qa:.0%}/{qb:.0%}，开头重合 {overlap:.0%}", ""))
    return out


def post_gate(proj, no: int, text: str, card: dict | None = None, cfg: dict | None = None) -> dict:
    if not enabled(proj):
        return {"ok": True, "blocks": [], "warns": []}
    facts = load_facts(proj) or ensure_project(proj, cfg or _cfg(proj))
    blocks, warns = [], []
    body = text or ""
    target = int(facts.get("target_words_per_chapter") or 3000)
    w = _cn(body)
    if w and (w < int(target * 0.7) or w > int(target * 1.4)):
        blocks.append(_f("block", "V5Q01", f"本章 {w} 汉字，目标 {target}（允许 70%–140%）", "", no))
    p = str(facts.get("protagonist") or "").strip()
    if p and p not in body:
        blocks.append(_f("block", "V5Q02", f"主角「{p}」在本章 0 次出现", "", no))
    tags = sum(body.count(t) for t in _PAUSE_TAGS)
    density = tags / max(0.001, w / 1000)
    if density > 5:
        blocks.append(_f("block", "V5Q03",
                         f"情绪停顿标签 {tags} 次（{density:.1f}/千字），超过 5/千字", "", no))
    elif density > 2:
        warns.append(_f("warn", "V5Q03",
                        f"情绪停顿标签 {tags} 次（{density:.1f}/千字），建议 ≤2/千字", "", no))
    warns.extend(_voice_similarity(proj, body))
    if card:
        for f in card_lint(proj, card, no):
            (blocks if f["level"] == "block" else warns).append(f)
    motifs = [str(x) for x in (facts.get("ending_contract") or [{}])[-1].get("keywords") or []]
    if motifs and no >= 3 and not any(m and m in body for m in motifs):
        warns.append(_f("warn", "V5Q05", "本章没有出现任何书名承诺词（可以是事件，不必是字面）", "", no))
    _ledger(proj, {"stage": "post", "chapter": int(no or 0), "blocks": blocks, "warns": warns})
    return {"ok": not blocks, "blocks": blocks, "warns": warns}


# ---------------------------------------------------------------- 结构体检 / 终审

def _chapter_text(proj, n: int) -> str:
    plan = _plan(proj)
    pdir = _pdir(proj)
    for c in plan.get("chapters") or []:
        if int(c.get("chapterNumber") or 0) == int(n):
            fp = os.path.join(pdir, str(c.get("filePath") or ""))
            if os.path.isfile(fp):
                return _read(fp, 200000)
    # 回退：不同版本的作品用「第1章…」「第01章…」两种命名，按章号前缀找正文。
    try:
        for fn in os.listdir(pdir):
            if not fn.endswith(".md") or "章" not in fn:
                continue
            m = re.match(r"^第0*(\d+)章", fn)
            if m and int(m.group(1)) == int(n):
                return _read(os.path.join(pdir, fn), 200000)
    except OSError:
        pass
    return ""


def _summaries(proj) -> dict[int, str]:
    pdir = _pdir(proj)
    fp = os.path.join(pdir, "21-章摘要.jsonl")
    out = {}
    for line in _read(fp, 500000).splitlines():
        try:
            d = json.loads(line)
            n = int(d.get("n") or 0)
            if n:
                out[n] = str(d.get("summary") or "")
        except Exception:
            continue
    if out:
        return out
    # 兼容只写 11-章节摘要.md 的项目：## 第01章 标题 / - 摘要：...
    md = _read(os.path.join(pdir, "11-章节摘要.md"), 500000)
    cur = 0
    for line in md.splitlines():
        m = re.match(r"^##\s*第0*(\d+)章", line.strip())
        if m:
            cur = int(m.group(1))
            continue
        if cur and line.strip().startswith("- 摘要："):
            out[cur] = line.split("：", 1)[-1].strip()
    return out


def _save_structure_alert(proj, end: int, issues: list) -> None:
    pl = _plan(proj)
    pl.setdefault("meta", {})["v5_structure_alert"] = {
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "upto": end, "issues": issues[:10],
    }
    if hasattr(proj, "save_plan"):
        proj.save_plan(pl)


def structure_check(proj, upto: int | None = None, window: int = 10) -> dict:
    if not enabled(proj):
        return {"ok": True, "issues": [], "should_replan": False}
    plan = _plan(proj)
    try:
        from . import longform
        cards = longform.load_cards(proj)
    except Exception:
        cards = {}
    total = int(plan.get("totalChapters") or (max(cards) if cards else 0) or 0)
    end = max(1, min(int(upto or total or 1), total or int(upto or 1)))
    start = max(1, end - int(window) + 1)
    issues = []
    scene_types = [str((cards.get(n) or {}).get("scene_type") or "") for n in range(start, end + 1)]
    for i in range(2, len(scene_types)):
        if scene_types[i] and scene_types[i] == scene_types[i - 1] == scene_types[i - 2]:
            issues.append(_f("block", "V5S01", f"第{start+i-2}–{start+i}章连续 3 章同一场景「{scene_types[i]}」", "", start + i))

    p = str((load_facts(proj) or {}).get("protagonist") or "").strip()
    present = sum(1 for n in range(start, end + 1) if p and p in _chapter_text(proj, n))
    if p and present < int((end - start + 1) * 0.8):
        issues.append(_f("block", "V5S02", f"近 {end-start+1} 章主角只在场 {present} 章（<80%）", "", end))

    irr = 0
    for n in range(start, end + 1):
        for t in ((cards.get(n) or {}).get("threads_advance") or []):
            if isinstance(t, dict) and t.get("irreversible"):
                irr += 1
    if irr < max(1, (end - start + 1) // 3):
        issues.append(_f("block", "V5S03", f"近 {end-start+1} 章只有 {irr} 处不可逆变化（<每 3 章 1 处）", "", end))

    sums = _summaries(proj)
    js = []
    for n in range(start + 1, end + 1):
        if n in sums and n - 1 in sums:
            js.append(_jaccard(sums[n], sums[n - 1], 4))
    if js and sum(js) / len(js) > 0.25:
        issues.append(_f("warn", "V5S04", f"相邻章摘要平均重合 {sum(js)/len(js):.2f}（>0.25）", "", end))

    contract = load_contract(proj)
    for pr in (contract.get("promises") or [])[:5]:
        kws = [str(x) for x in (pr.get("keywords") or []) if str(x)]
        if kws and not any(any(k in (sums.get(n) or "") for k in kws) for n in range(start, end + 1)):
            issues.append(_f("warn", "V5S05", f"承诺「{pr.get('id')}」近 {window} 章未 hit", "", end))

    blocks = [x for x in issues if x["level"] == "block"]
    should = bool(blocks)
    if should:
        try:
            lock = getattr(proj, "lock", None)
            if lock:
                with lock:
                    _save_structure_alert(proj, end, issues)
            else:
                _save_structure_alert(proj, end, issues)
        except Exception:
            pass
    _ledger(proj, {"stage": "structure", "chapter": end, "blocks": blocks,
                   "warns": [x for x in issues if x["level"] == "warn"], "metrics": {
                       "scene_types": scene_types, "protagonist_present": present,
                       "irreversible": irr, "summary_jaccard": round(sum(js) / len(js), 3) if js else 0}})
    return {"ok": not blocks, "issues": issues, "should_replan": should,
            "metrics": {"window": [start, end], "protagonist_present": present,
                        "irreversible": irr, "scene_types": scene_types}}


def ending_evidence(proj, chapters: list[dict]) -> dict:
    contract = load_contract(proj)
    facts = load_facts(proj)
    items = contract.get("ending_contract") or facts.get("ending_contract") or []
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        ev = int(it.get("evidence_chapter") or 0)
        ch = next((c for c in chapters if int(c.get("n") or 0) == ev), None)
        status, quote = "missing", ""
        if ev and ch:
            status = "verified"
            quote = _text(ch.get("text"), 120)
        elif it.get("keywords"):
            kws = [str(x) for x in it.get("keywords") or [] if str(x)]
            for c in chapters[-10:]:
                if any(k in (c.get("text") or "") for k in kws):
                    status = "candidate"
                    quote = _text(c.get("text"), 120)
                    break
        rows.append({"id": it.get("id"), "text": it.get("text"),
                     "evidence_chapter": ev, "status": status, "quote": quote})
    # 终局不是“每一条都必须字面命中”，但至少 3 条要有正文证据/候选证据；
    # 少于 3 条视为终局没有真正落地，阻止投稿导出。
    hit = [r for r in rows if r["status"] in ("verified", "candidate")]
    missing = [r for r in rows if r["status"] == "missing"]
    need = min(3, len(rows))
    return {"rows": rows, "missing": missing, "hit": len(hit), "need": need,
            "ok": bool(rows) and len(hit) >= need}


def check_book(proj, chapters: list[dict]) -> dict:
    if not enabled(proj):
        return {"ok": True, "blocks": [], "warns": [], "skipped": True}
    blocks = [x for x in fact_lint(proj) if x["level"] == "block"]
    warns = [x for x in fact_lint(proj) if x["level"] == "warn"]
    try:
        from . import longform
        cards = longform.load_cards(proj)
    except Exception:
        cards = {}
    for c in chapters or []:
        n = int(c.get("n") or 0)
        if not n:
            continue
        g = post_gate(proj, n, c.get("text") or "", cards.get(n), _cfg(proj))
        blocks.extend(g["blocks"])
        warns.extend(g["warns"])
    st = structure_check(proj, upto=max((int(c.get("n") or 0) for c in chapters or [{"n": 1}]), default=1))
    blocks.extend([x for x in st.get("issues") or [] if x["level"] == "block"])
    warns.extend([x for x in st.get("issues") or [] if x["level"] == "warn"])
    end = ending_evidence(proj, chapters)
    if not end.get("ok"):
        blocks.append(_f("block", "V5E01",
                         f"终局证据不足：{end.get('hit', 0)}/{end.get('need', 3)} 条命中"
                         f"（缺：{'、'.join(str(r.get('id')) for r in end.get('missing') or [])}）", "", 0))
    _ledger(proj, {"stage": "book", "chapter": 0, "blocks": blocks, "warns": warns,
                   "ending": end})
    return {"ok": not blocks, "blocks": blocks, "warns": warns,
            "ending": end, "structure": st}


# ---------------------------------------------------------------- 上下文 / 账本

def context_block(proj, card: dict | None = None) -> str:
    if not enabled(proj):
        return ""
    facts = load_facts(proj)
    contract = load_contract(proj)
    if not facts:
        return ""
    lines = ["【v5 作品事实源（优先于旧圣经/旧人物档案）】"]
    lines.append(f"- 书名：{facts.get('title')}｜题材：{facts.get('genre')}｜目标：{facts.get('target_chapters')} 章 × {facts.get('target_words_per_chapter')} 字")
    lines.append(f"- 一句话故事：{_text(facts.get('logline'), 240)}")
    lines.append(f"- 主角：{facts.get('protagonist')}｜主要角色：{facts.get('deuteragonist') or '—'}")
    ladder = facts.get("antagonist_ladder") or []
    if ladder:
        lines.append("- 反派阶梯：" + "；".join(f"{x.get('level')}.{x.get('name')}" for x in ladder[:4] if x.get("name")))
    ps = contract.get("promises") or []
    if ps:
        lines.append("- 本书承诺：" + "；".join(f"[{p.get('id')}] {p.get('text')}" for p in ps[:4]))
    ending = contract.get("ending_contract") or []
    if ending:
        lines.append("- 终局必须发生：" + "；".join(str(x.get("text") or "") for x in ending[:5]))
    if card:
        bits = []
        for k, label in (("project_type", "场景类型"), ("scene_type", "场景类型"),
                         ("story_time", "故事时间"), ("protagonist_action", "主角行动"),
                         ("relationship_delta", "关系变化"), ("cliffhanger", "章末钩子")):
            v = _text(card.get(k), 120)
            if v:
                bits.append(f"{label}：{v}")
        if bits:
            lines.append("- 本章契约：" + "；".join(bits))
    cast = facts.get("core_cast") or []
    focus = [str(x) for x in (card or {}).get("cast") or [] if str(x)]
    voices = []
    for c in cast:
        if not isinstance(c, dict):
            continue
        if focus and c.get("name") not in focus:
            continue
        v = _text(c.get("voice"), 160)
        if v:
            voices.append(f"{c.get('name')}：{v}")
        if len(voices) >= 4:
            break
    if voices:
        lines.append("- 人物声音卡（台词必须能盲听出是谁）：" + "；".join(voices))
    lines.append("- 红线：" + "；".join(str(x) for x in (facts.get("red_lines") or [])[:5]))
    return "\n".join(lines)[:2600]


def _ledger(proj, rec: dict) -> None:
    try:
        p = os.path.join(_pdir(proj), LEDGER_FILE)
        rec = dict(rec)
        rec.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def report_markdown(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    lines = ["# v5.0 质量内核报告", ""]
    blocks = data.get("blocks") or []
    warns = data.get("warns") or []
    lines.append(f"- 结论：{'通过' if data.get('ok') else '未通过'}｜阻断 {len(blocks)}｜告警 {len(warns)}")
    if blocks:
        lines += ["", "## 阻断项", ""]
        for f in blocks[:40]:
            lines.append(f"- [{f.get('code')}] 第{f.get('chapter') or '—'}章：{f.get('message')}")
    end = data.get("ending") or {}
    if end.get("rows"):
        lines += ["", "## 终局证据", "", "| 编号 | 承诺 | 证据章 | 状态 |", "| :--- | :--- | :--- | :--- |"]
        for r in end["rows"]:
            lines.append(f"| {r.get('id')} | {str(r.get('text'))[:60]} | {r.get('evidence_chapter') or '—'} | {r.get('status')} |")
    return "\n".join(lines) + "\n"
