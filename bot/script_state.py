"""剧本一致性引擎（v3.3.0，借鉴小说的 18-状态库 / continuity 体系）。

零 LLM 成本、可复现：
  · 状态库：从 剧本计划.json（场次/人物）+ 创作圣经 登记人物表、场景表 → 18-剧本状态库.json；
  · 逐场体检：人物在场校验（场次表声明 vs 正文出现）、场景跳号/重复、时代串代（复用
    script_checker 的词表扫描），结果落 22-剧本体检.json 供右栏体检区展示。

设计原则与 bot/continuity.py 一致：每一项检查失败都降级为跳过，绝不 crash。
"""
import json
import os
import re

from .safeio import atomic_write_json

STATE_FILE = "18-剧本状态库.json"
AUDIT_FILE = "22-剧本体检.json"
# 改编剧的场次是「集」，place 只能填占位；这种值不是地点，不能进地点表
PLACEHOLDER_PLACE = re.compile(r"^(第\s*\d+\s*[集场]|第\s*\d+\s*集.*|场景\s*\d+|\d+)$")


def _read(pdir, name):
    p = os.path.join(pdir, name)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def _scene_body(pdir, sc):
    fn = os.path.basename(str(sc.get("fileName") or "").strip())
    if not fn:
        return ""
    return _read(pdir, os.path.join("04-正文", fn))


def build_state(pdir: str) -> dict:
    """登记人物表 / 场景表。人物来源优先级：场次表 cast 列 > 正文对白 > 创作圣经。

    v4.33.5：小说改编剧在计划里「一集」就是一个 scene，`place` 只能填占位的
    「第 N 集」、`cast` 为空——于是状态库的地点成了 50 条「第1集…第50集」，
    人物表 0 条，工作台的人物/结构页签永远空白。
    现在计划里缺的（或只有占位的）一律用正文抽取的实体补上：正文里
    `场1：外 部落广场 白天` 与 `林野：台词` 是比计划更可信的底稿。
    """
    plan = {}
    try:
        plan = json.load(open(os.path.join(pdir, "剧本计划.json"), encoding="utf-8"))
    except Exception:
        pass
    scenes = plan.get("scenes") or []
    characters: dict[str, dict] = {}
    locations: dict[str, dict] = {}
    for sc in scenes:
        cast = str(sc.get("cast") or "")
        for nm in re.split(r"[、，,/\s]+", cast):
            nm = nm.strip()
            if not nm or nm in ("无", "无对白", "群演"):
                continue
            c = characters.setdefault(nm, {"first_scene": sc.get("no"), "scenes": []})
            if sc.get("no") and sc["no"] not in c["scenes"]:
                c["scenes"].append(sc.get("no"))
        place = str(sc.get("place") or "").strip()
        if place and not PLACEHOLDER_PLACE.match(place):
            loc = locations.setdefault(place, {"scenes": []})
            if sc.get("no") and sc["no"] not in loc["scenes"]:
                loc["scenes"].append(sc.get("no"))
    # 圣经里的人物小传段落（补全没上场次表的）
    bible = _read(pdir, "00-创作圣经.md")
    for m in re.finditer(r"^\s*[-*·]?\s*[*【]?\s*([\u4e00-\u9fa5]{2,6})[*】]?\s*[:：]", bible, re.M):
        nm = m.group(1).strip()
        if nm and nm not in characters and not any(k in nm for k in ("场景", "时间", "时代", "主题", "类型")):
            characters.setdefault(nm, {"first_scene": None, "scenes": [], "from": "bible"})
    # v4.33.5：正文抽取补位——改编剧是唯一真相；常规剧本则是与正文对账的底稿
    extracted: dict = {}
    try:
        from . import script_entities as _se
        extracted = _se.extract(pdir, plan)
        for nm, c in (extracted.get("characters") or {}).items():
            if nm in characters:
                continue
            characters[nm] = {"first_scene": c.get("first"),
                              "scenes": list(c.get("episodes") or []),
                              "from": "body", "lines": c.get("lines") or 0}
        for nm, loc in (extracted.get("locations") or {}).items():
            if nm in locations:
                continue
            locations[nm] = {"scenes": list(loc.get("episodes") or []),
                             "count": loc.get("count") or 0, "from": "body"}
    except Exception:
        extracted = {}
    state = {
        "characters": characters,
        "locations": locations,
        "metrics": {"scenes": len(scenes),
                    "written": sum(1 for sc in scenes if sc.get("status") == "completed"),
                    "sceneCount": (extracted.get("summary") or {}).get("scenes", 0)},
        "timeline": [{"scene": sc.get("no"), "place": sc.get("place"), "time": sc.get("time")}
                     for sc in scenes[:500]],
        "entities": {"episodes": len(extracted.get("episodes") or {}),
                     "scenes": (extracted.get("summary") or {}).get("scenes", 0),
                     "fromBody": bool(extracted)},
    }
    try:
        atomic_write_json(os.path.join(pdir, STATE_FILE), state)
    except Exception:
        pass
    return state


def audit_script(pdir: str) -> dict:
    """逐场体检。返回 {at, scope:'script', issues:[...], stats:{...}}。"""
    from . import script_checker
    plan = {}
    try:
        plan = json.load(open(os.path.join(pdir, "剧本计划.json"), encoding="utf-8"))
    except Exception:
        pass
    state = build_state(pdir)
    scenes = plan.get("scenes") or []
    issues: list[str] = []

    # 1) 场次号连续性（跳号/重复）
    nos = [sc.get("no") for sc in scenes if isinstance(sc.get("no"), int)]
    if nos and nos != sorted(nos):
        issues.append("场次清单顺序与场次号不一致")
    # 相邻两场比较差值，长度天然差 1，显式 strict=False 表明是刻意为之
    for a, b in zip(nos, nos[1:], strict=False):
        if b - a > 1:
            issues.append(f"第{a}场与第{b}场之间缺号（跳场）")
    if len(nos) != len(set(nos)):
        issues.append("存在重复场次号")

    # 2) 人物在场校验：场次表声明的角色没在正文出现（写好的场才查）
    characters = state.get("characters") or {}
    checked = 0
    for sc in scenes:
        if sc.get("status") != "completed":
            continue
        body = _scene_body(pdir, sc)
        if not body.strip():
            continue
        checked += 1
        cast = [nm.strip() for nm in re.split(r"[、，,/\s]+", str(sc.get("cast") or "")) if nm.strip()]
        for nm in cast:
            if nm and nm not in body:
                issues.append(f"第{sc.get('no')}场声明出场「{nm}」但正文未出现")
        # 3) 时代串代（复用词表扫描）
        setting = (plan.get("meta") or {}).get("setting") or {}
        era = setting.get("era") or ""
        if era:
            try:
                hits = script_checker.scan(body, era)
                if hits:
                    terms = "、".join(sorted({h["term"] for h in hits}))[:60]
                    issues.append(f"第{sc.get('no')}场时代串代（{era}）：{terms}")
            except Exception:
                pass

    # 4) 正文写了但场次表缺失
    body_dir = os.path.join(pdir, "04-正文")
    if os.path.isdir(body_dir):
        listed = {os.path.basename(str(sc.get("fileName") or "")) for sc in scenes}
        for fn in sorted(os.listdir(body_dir)):
            if fn.endswith(".md") and fn not in listed:
                issues.append(f"正文文件 {fn} 不在场次清单里")

    # 5) v4.33.5 正文结构体检（改编剧的场次表是空的，上面 1/2 两条查不出问题）
    try:
        from . import script_entities as _se
        ext = _se.extract(pdir, plan)
        for ep in (ext.get("summary") or {}).get("no_scene_heading") or []:
            issues.append(f"第{ep}集正文没有可识别的场标题（`场N：内外 地点 时间`），无法核对场景一致性")
        # 单场零对白：纯动作场是合理的，但连续多场没有台词通常是写坏了
        silent: dict[int, int] = {}
        for s in ext.get("scenes") or []:
            if not s.get("lines"):
                silent[s.get("episode")] = silent.get(s.get("episode"), 0) + 1
        for ep, n in silent.items():
            if n >= 2:
                issues.append(f"第{ep}集有 {n} 场没有台词，建议确认是否写漏了对白")
        # 主要人物断层：台词数前 5 的角色，连续 8 集以上不出现
        # （系统/旁白这类非人声不参与——它们本来就只在需要时出现）
        NON_HUMAN = {"系统", "系统提示音", "旁白", "画外音", "声音", "提示音"}
        tops = sorted((ext.get("characters") or {}).items(),
                      key=lambda kv: -(kv[1].get("lines") or 0))[:5]
        total_ep = max((int(k) for k in (ext.get("episodes") or {}) if str(k).isdigit()),
                       default=0)
        for nm, c in tops:
            if nm in NON_HUMAN:
                continue
            eps = c.get("episodes") or []
            if len(eps) < 2 or total_ep < 12:
                continue
            gap, prev = 0, eps[0]
            worst = None
            for e in eps[1:] + [total_ep]:
                if e - prev - 1 > gap:
                    gap, worst = e - prev - 1, (prev, e)
                prev = e
            if gap >= 8 and worst:
                issues.append(f"主要人物「{nm}」在第{worst[0]}–{worst[1]}集之间连续 {gap} 集未出场")
        # 同一地点写了多个名字：长名字以短名字开头（古营核心 / 古营核心大厅），
        # 会让人物-地点一致性对不上号。只认前缀，避免「广场 / 部落广场」这种
        # 通名误报——那本来就不是同一个地点。
        loc_names = sorted((ext.get("locations") or {}).keys(), key=len)
        fuzzy: list[tuple[str, str]] = []
        for i, short in enumerate(loc_names):
            if len(short) < 3:
                continue
            for long in loc_names[i + 1:]:
                if short != long and long.startswith(short) and (short, long) not in fuzzy:
                    fuzzy.append((short, long))
        if fuzzy:
            issues.append("同一地点有多种写法（建议统一）：" + "；".join(
                f"{a} / {b}" for a, b in fuzzy[:4]))
    except Exception:
        pass

    return {"at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "scope": "script", "issues": issues[:50],
            "stats": {"scenes": len(scenes), "written": state.get("metrics", {}).get("written", 0),
                      "checked": checked, "characters": len(characters),
                      "locations": len(state.get("locations") or {}),
                      "bodyScenes": state.get("metrics", {}).get("sceneCount", 0)}}


def save_audit(pdir: str, report: dict) -> None:
    try:
        atomic_write_json(os.path.join(pdir, AUDIT_FILE), report)
    except Exception:
        pass


def load_audit(pdir: str) -> dict:
    try:
        return json.load(open(os.path.join(pdir, AUDIT_FILE), encoding="utf-8")) or {}
    except Exception:
        return {}
