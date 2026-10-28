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


def _read(pdir, name):
    p = os.path.join(pdir, name)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def _scene_body(pdir, sc):
    fn = os.path.basename(str(sc.get("fileName") or "").strip())
    if not fn:
        return ""
    return _read(pdir, os.path.join("04-正文", fn))


def _cn_name_re():
    return re.compile(r"(?:人物|角色|出场)[:：]?\s*([^\n，,。;；]{1,30})")


def build_state(pdir: str) -> dict:
    """登记人物表 / 场景表。人物来源优先级：场次表 cast 列 > 创作圣经。"""
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
        if place:
            loc = locations.setdefault(place, {"scenes": []})
            if sc.get("no") and sc["no"] not in loc["scenes"]:
                loc["scenes"].append(sc.get("no"))
    # 圣经里的人物小传段落（补全没上场次表的）
    bible = _read(pdir, "00-创作圣经.md")
    for m in re.finditer(r"^\s*[-*·]?\s*[*【]?\s*([\u4e00-\u9fa5]{2,6})[*】]?\s*[:：]", bible, re.M):
        nm = m.group(1).strip()
        if nm and nm not in characters and not any(k in nm for k in ("场景", "时间", "时代", "主题", "类型")):
            characters.setdefault(nm, {"first_scene": None, "scenes": [], "from": "bible"})
    state = {
        "characters": characters,
        "locations": locations,
        "metrics": {"scenes": len(scenes),
                    "written": sum(1 for sc in scenes if sc.get("status") == "completed")},
        "timeline": [{"scene": sc.get("no"), "place": sc.get("place"), "time": sc.get("time")}
                     for sc in scenes[:500]],
    }
    try:
        atomic_write_json(os.path.join(pdir, STATE_FILE), state)
    except Exception:
        pass
    return state


def load_state(pdir: str) -> dict:
    try:
        return json.load(open(os.path.join(pdir, STATE_FILE), encoding="utf-8")) or {}
    except Exception:
        return {}


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
    for a, b in zip(nos, nos[1:]):
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

    return {"at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "scope": "script", "issues": issues[:50],
            "stats": {"scenes": len(scenes), "written": state.get("metrics", {}).get("written", 0),
                      "checked": checked, "characters": len(characters),
                      "locations": len(state.get("locations") or {})}}


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
