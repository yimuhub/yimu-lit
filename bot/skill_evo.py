"""候选技能、版本、晋升与回滚（技能自我进化的「审批闸门」）。

职责：
1. 把「用户反馈 + 改稿差异」变成候选技能草稿（只写候选区，绝不自动上线）；
2. 用固定评测集比较「现有技能 vs 候选技能」，给出可复现的晋升建议；
3. 人工批准后，把候选写入 ``skillkit/_evolved/``，保留旧版本以便随时回滚。

数据布局：
- 候选：``data/skill_candidates/<candidate_id>/skill.md`` + ``meta.json``
        （候选不在 skillkit 里，未审批内容永远不会被注入 prompt）
- 版本：``data/skill_versions/<skill_name>/<vN>.json``（内容、diff、来源、评测、时间）
- 索引：``data/skill_candidates/index.json``（可随时从目录重建）
- 上线：``skillkit/_evolved/<skill_name>.md``（frontmatter 标 evolved: true）
- 审计：``data/skill_evo_audit.jsonl``

安全边界：
- 技能名必须匹配 ``^[a-z0-9][a-z0-9-]{2,60}$``；所有落盘路径经 safe_join，禁止 .. 与分隔符；
- 内容必须过 ``validate_skill``（frontmatter、大小、危险片段、高重复）；
- ``promote`` 绝不自动调用：只能由 API / 人工显式触发，且默认要求评测通过。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
UTC = timezone.utc  # v4.7.1 兼容 Python 3.9/3.10（datetime.UTC 自 3.11 起才有；与 timezone.utc 等价）

from . import eval as eval_mod
from . import feedback
from . import llm
from . import medium as medium_mod
from . import skillkit
from .safeio import atomic_write_text, safe_join

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
CAND_DIR = os.path.join(DATA_DIR, "skill_candidates")
VERS_DIR = os.path.join(DATA_DIR, "skill_versions")
#: 上线的进化技能目录；与 skillkit 索引目录保持一致，测试可 monkeypatch。
EVOLVED_DIR = os.path.join(skillkit.SKILLKIT_DIR, "_evolved")
AUDIT_PATH = os.path.join(DATA_DIR, "skill_evo_audit.jsonl")

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
_VERSION_RE = re.compile(r"^v(\d+)$")

MIN_BYTES = 512
MAX_BYTES = 20 * 1024
REQUIRED_FIELDS = ("name", "title", "medium", "stages", "kind", "weight", "license", "source")
VALID_KINDS = ("method", "template", "data", "case")

# 提示注入 / 可执行 / 外链：候选技能里一律不允许
_DANGER: list[tuple[str, str]] = [
    (r"忽略(?:以上|上面|之前|先前|前面)(?:的)?(?:所有|全部)?(?:指令|提示|规则|要求)", "提示注入：试图覆盖上文指令"),
    (r"ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions", "提示注入：ignore previous instructions"),
    (r"(?:输出|打印|泄露|展示|reveal|print|leak).{0,12}(?:系统提示|system\s*prompt|开发者消息)", "提示注入：索取系统提示"),
    (r"curl\s*[^\n|]{0,120}\|\s*(?:sh|bash)", "可执行命令：curl 管道执行"),
    (r"\brm\s+-rf\b|:\(\)\s*\{", "可执行危险命令"),
    (r"<\s*script|<\s*iframe|javascript\s*:", "内嵌脚本 / 可执行标记"),
    (r"https?://", "外链：技能内容不得夹带外部链接"),
]

_LOCK = threading.Lock()
_TOGGLES_PATH = os.path.join(DATA_DIR, "skill_toggles.json")


def _load_toggles() -> dict:
    """读取已禁用技能列表 {skill_name: False}"""
    try:
        with open(_TOGGLES_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_toggles(toggles: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    atomic_write_text(_TOGGLES_PATH, json.dumps(toggles, ensure_ascii=False, indent=2))


def is_disabled(skill_name: str) -> bool:
    """检查某技能是否被手动禁用"""
    return not _load_toggles().get(skill_name, True)


def toggle(skill_name: str, enabled: bool) -> dict:
    """启用/禁用一个技能。禁用后的技能不会被 stage_inject 注入 prompt。

    参数：
        skill_name: 技能名
        enabled: True=启用, False=禁用

    返回：{"ok": True, "enabled": bool, "name": str}
    """
    name = str(skill_name or "").strip()
    if not name:
        return {"ok": False, "error": "缺少技能名"}
    with _LOCK:
        toggles = _load_toggles()
        if enabled:
            toggles.pop(name, None)
        else:
            toggles[name] = False
        _save_toggles(toggles)
    return {"ok": True, "enabled": enabled, "name": name}


# ---------------------------------------------------------------- 小工具

def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _err(msg: str, **extra) -> dict:
    return {"ok": False, "error": msg, **extra}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _split_fm(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text or "")
    if not m:
        return {}, text or ""
    return skillkit._parse_frontmatter(text), text[m.end():]


def _as_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
        return [x.strip() for x in re.split(r"[,，;；\s]+", value) if x.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()]


def _render_fm(fm: dict, body: str) -> str:
    """统一序列化 frontmatter：数组用 [a, b]，布尔用 true/false（加载器可解析）。"""
    order = ("name", "title", "medium", "stages", "kind", "weight", "license",
             "source", "evolved")
    keys = [k for k in order if k in fm] + [k for k in fm if k not in order]
    lines = ["---"]
    for key in keys:
        val = fm[key]
        if isinstance(val, bool):
            val = "true" if val else "false"
        elif isinstance(val, (list, tuple)):
            clean = [str(x).replace(",", "，") for x in val]
            val = "[" + ", ".join(clean) + "]"
        lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (body or "").lstrip("\n")


def _skill_codes(media: list[str]) -> set[str]:
    codes: set[str] = set()
    for med in media:
        if med == "all":
            for key in medium_mod.MEDIUMS:
                codes.update(medium_mod.all_stage_codes(key))
        elif medium_mod.is_medium(med):
            codes.update(medium_mod.all_stage_codes(med))
    # 戏曲主路径之外还有板腔体 X0–X6 / 曲牌体 Q0–Q6（medium.py 注释中约定）
    if "script" in media or "all" in media:
        codes.update(f"X{i}" for i in range(7))
        codes.update(f"Q{i}" for i in range(7))
    return codes


def _audit(event: dict) -> None:
    """追加审计行；审计失败不影响主流程（但不能静默到完全没有）。"""
    rec = {"ts": _now(), **event}
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(AUDIT_PATH)), exist_ok=True)
        with _LOCK:
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
    except OSError:
        pass


# ---------------------------------------------------------------- 校验

def validate_skill(text: str) -> dict:
    """候选技能准入检查：结构、字段、大小、危险片段、与现有技能的重复度。"""
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "errors": ["内容为空"], "warnings": [], "meta": {}}
    raw = text.encode("utf-8")
    errors: list[str] = []
    warnings: list[str] = []
    if len(raw) < MIN_BYTES:
        errors.append(f"文件过小：{len(raw)} 字节（下限 {MIN_BYTES}）")
    if len(raw) > MAX_BYTES:
        errors.append(f"文件过大：{len(raw)} 字节（上限 {MAX_BYTES}）")

    if not _FM_RE.match(text):
        errors.append("缺少 frontmatter（--- 包裹的 YAML 头）")
        fm: dict = {}
        body = text
    else:
        fm = skillkit._parse_frontmatter(text)
        body = text[_FM_RE.match(text).end():]
    if not body.strip():
        errors.append("正文为空")

    for field in REQUIRED_FIELDS:
        if field not in fm or fm.get(field) in ("", None, []):
            errors.append(f"frontmatter 缺字段：{field}")

    name = str(fm.get("name") or "").strip()
    if name and not SKILL_NAME_RE.match(name):
        errors.append("name 必须匹配 ^[a-z0-9][a-z0-9-]{2,60}$")

    media = _as_list(fm.get("medium") or fm.get("media"))
    if not media:
        errors.append("medium 不能为空")
    bad_media = [m for m in media if m != "all" and not medium_mod.is_medium(m)]
    if bad_media:
        errors.append("medium 非法：" + "、".join(bad_media))

    stages = _as_list(fm.get("stages") or fm.get("stage"))
    allowed = _skill_codes(media)
    bad_stages = [s for s in stages if allowed and s not in allowed]
    if bad_stages:
        errors.append("stages 含未知阶段：" + "、".join(bad_stages))

    kind = str(fm.get("kind") or "")
    if kind and kind not in VALID_KINDS:
        errors.append(f"kind 非法：{kind}")
    try:
        weight = int(fm.get("weight"))
        if weight < 1 or weight > 5:
            errors.append("weight 必须在 1–5")
    except (TypeError, ValueError):
        errors.append("weight 必须是 1–5 的整数")

    for pattern, label in _DANGER:
        if re.search(pattern, text, re.I):
            errors.append("内容含危险片段：" + label)

    # 与现有高权重技能高度重复：只警告，由人工判断是否换角度重写
    top = sorted(skillkit.index(), key=lambda s: -s.weight)[:3]
    new_body = re.sub(r"\s+", "", body)
    for sf in top:
        old_body = re.sub(r"\s+", "", _split_fm(sf.text)[1])
        if len(old_body) < 200 or len(new_body) < 200:
            continue
        try:
            ratio = difflib.SequenceMatcher(None, old_body, new_body, autojunk=False).ratio()
        except Exception:
            ratio = 0.0
        if ratio > 0.9:
            warnings.append(f"与现有技能「{sf.name}」重复度 {ratio:.2f}，建议换角度重写")
            break
    if name and any(sf.name == name for sf in skillkit.index()):
        warnings.append("同名技能已存在：晋升后将覆盖原文件，旧版本会先存档")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "meta": {
            "name": name,
            "title": str(fm.get("title") or ""),
            "medium": media,
            "stages": stages,
            "kind": kind,
            "weight": fm.get("weight"),
            "license": str(fm.get("license") or ""),
            "source": str(fm.get("source") or ""),
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()[:32],
        },
    }


# ---------------------------------------------------------------- 候选

def _cand_path(cid: str, leaf: str) -> str:
    if not _ID_RE.match(str(cid or "")):
        raise ValueError("候选 id 非法")
    return safe_join(CAND_DIR, cid, label="候选 id") if leaf == "" else \
        safe_join(safe_join(CAND_DIR, cid, label="候选 id"), leaf, label="文件名")


def _read_meta(cid: str) -> dict | None:
    try:
        with open(_cand_path(cid, "meta.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _write_meta(cid: str, meta: dict) -> None:
    atomic_write_text(_cand_path(cid, "meta.json"),
                      json.dumps(meta, ensure_ascii=False, indent=2))


def _rebuild_index() -> dict:
    """从目录重建索引，避免并发追加把索引写乱。"""
    with _LOCK:
        items: list[dict] = []
        try:
            names = [n for n in os.listdir(CAND_DIR)
                     if os.path.isdir(os.path.join(CAND_DIR, n)) and _ID_RE.match(n)]
        except OSError:
            names = []
        for cid in names:
            meta = _read_meta(cid)
            if meta:
                items.append(meta)
        items.sort(key=lambda m: str(m.get("created_at") or ""), reverse=True)
        payload = {"updated_at": _now(), "count": len(items), "candidates": items}
        try:
            os.makedirs(CAND_DIR, exist_ok=True)
            atomic_write_text(os.path.join(CAND_DIR, "index.json"),
                              json.dumps(payload, ensure_ascii=False, indent=2))
        except OSError:
            pass
    return payload


def list_candidates(status: str = "pending") -> list[dict]:
    """列出候选；status 为空串返回全部。索引缺失时自动重建。"""
    try:
        with open(os.path.join(CAND_DIR, "index.json"), encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = _rebuild_index()["candidates"]
    except (OSError, ValueError, TypeError):
        items = _rebuild_index()["candidates"]
    if status:
        items = [m for m in items if m.get("status") == status]
    return [json.loads(json.dumps(m, ensure_ascii=False)) for m in items]


def get_candidate(candidate_id: str) -> dict:
    if not _ID_RE.match(str(candidate_id or "")):
        return _err("候选 id 非法")
    meta = _read_meta(candidate_id)
    if not meta:
        return _err("候选不存在")
    try:
        with open(_cand_path(candidate_id, "skill.md"), encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return _err("候选内容缺失")
    return {**meta, "content": content, "ok": True}


def propose(name: str, medium, stages, title: str, content: str,
            reason: str = "", source_feedback_ids=None, cfg=None) -> dict:
    """校验并落盘一个候选；同 name 且内容相同则拒绝重复。不会进入 skillkit。"""
    name = str(name or "").strip()
    if not SKILL_NAME_RE.match(name):
        return _err("技能名非法：只允许小写字母、数字、连字符，3–61 位")
    media = _as_list(medium)
    if not media:
        return _err("medium 不能为空")
    bad = [m for m in media if m != "all" and not medium_mod.is_medium(m)]
    if bad:
        return _err("medium 非法：" + "、".join(bad))
    stage_list = _as_list(stages)
    body = content if isinstance(content, str) else ""
    if not body.strip():
        return _err("content 不能为空")

    fm_src, body_src = _split_fm(body)
    body_src = body_src.strip()
    if not body_src:
        return _err("content 正文不能为空")
    fm = dict(fm_src)
    fm.update({
        "name": name,
        "title": str(title or fm.get("title") or name),
        "medium": media,
        "stages": stage_list,
        "kind": str(fm.get("kind") or "method"),
        "weight": fm.get("weight") or 4,
        "license": "自研",
        "source": str(fm.get("source") or "反馈进化候选"),
    })
    fm.pop("evolved", None)
    text = _render_fm(fm, body_src)

    check = validate_skill(text)
    if not check["ok"]:
        return _err("校验失败", errors=check["errors"], warnings=check["warnings"])
    digest = _hash(text)
    for meta in list_candidates(status=""):
        if meta.get("name") != name:
            continue
        if meta.get("content_hash") == digest:
            return _err("重复候选：已存在内容相同的候选",
                        candidate_id=meta.get("candidate_id"))

    cid = secrets.token_hex(6)
    created = _now()
    meta = {
        "candidate_id": cid,
        "name": name,
        "title": fm["title"],
        "medium": media,
        "stages": stage_list,
        "kind": fm["kind"],
        "weight": int(fm["weight"]) if str(fm["weight"]).isdigit() else 4,
        "status": "pending",
        "created_at": created,
        "updated_at": created,
        "reason": str(reason or "")[:2000],
        "source_feedback_ids": _as_list(source_feedback_ids)[:50],
        "content_hash": digest,
        "content_bytes": len(text.encode("utf-8")),
        "evaluation": None,
        "audit": [{"ts": created, "action": "propose", "actor": "system"}],
    }
    try:
        os.makedirs(safe_join(CAND_DIR, cid, label="候选 id"), exist_ok=True)
        atomic_write_text(_cand_path(cid, "skill.md"), text)
        _write_meta(cid, meta)
    except (OSError, ValueError) as exc:
        return _err(f"候选写入失败：{exc.__class__.__name__}")
    _rebuild_index()
    _audit({"action": "propose", "candidate_id": cid, "name": name, "medium": media})
    return {"ok": True, "candidate_id": cid, "meta": meta, "warnings": check["warnings"]}


def reject(candidate_id: str, note: str = "") -> dict:
    meta = _read_meta(candidate_id)
    if not meta:
        return _err("候选不存在")
    if meta.get("status") != "pending":
        return _err(f"只有 pending 候选可驳回（当前 {meta.get('status')}）")
    meta["status"] = "rejected"
    meta["rejected_at"] = _now()
    meta["reject_note"] = str(note or "")[:2000]
    meta.setdefault("audit", []).append(
        {"ts": _now(), "action": "reject", "actor": "user", "note": meta["reject_note"]})
    _write_meta(candidate_id, meta)
    _rebuild_index()
    _audit({"action": "reject", "candidate_id": candidate_id, "name": meta.get("name")})
    return {"ok": True, "candidate_id": candidate_id, "status": "rejected"}


# ---------------------------------------------------------------- 版本

def _versions(skill_name: str) -> list[dict]:
    if not SKILL_NAME_RE.match(str(skill_name or "")):
        return []
    base = safe_join(VERS_DIR, skill_name, label="技能名")
    try:
        names = [n for n in os.listdir(base) if n.endswith(".json")]
    except OSError:
        return []
    out = []
    for name in names:
        try:
            with open(os.path.join(base, name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    out.sort(key=lambda d: int(str(d.get("version", "v0")).lstrip("v") or 0))
    return out


def _next_version(skill_name: str) -> str:
    nums = []
    for d in _versions(skill_name):
        m = _VERSION_RE.match(str(d.get("version") or ""))
        if m:
            nums.append(int(m.group(1)))
    return f"v{max(nums) + 1 if nums else 1}"


def _save_version(skill_name: str, content: str, *, source: str,
                  evaluation=None, candidate_id: str = "", approver: str = "") -> dict:
    version = _next_version(skill_name)
    previous = _versions(skill_name)
    old = previous[-1].get("content", "") if previous else ""
    diff = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), content.splitlines(keepends=True),
        fromfile="previous", tofile=version, n=2,
    ))[:20000]
    rec = {
        "skill_name": skill_name,
        "version": version,
        "created_at": _now(),
        "source": source[:200],
        "candidate_id": candidate_id,
        "approver": str(approver or "")[:80],
        "content_hash": _hash(content),
        "content": content,
        "diff": diff,
        "evaluation": evaluation,
    }
    base = safe_join(VERS_DIR, skill_name, label="技能名")
    os.makedirs(base, exist_ok=True)
    atomic_write_text(os.path.join(base, f"{version}.json"),
                      json.dumps(rec, ensure_ascii=False, indent=2))
    return rec


def history(skill_name: str = "") -> list[dict]:
    """版本历史（不含完整正文，只给预览），按时间倒序。"""
    names = [skill_name] if skill_name else []
    if not names:
        try:
            names = [n for n in os.listdir(VERS_DIR)
                     if os.path.isdir(os.path.join(VERS_DIR, n)) and SKILL_NAME_RE.match(n)]
        except OSError:
            return []
    out = []
    for name in names:
        for rec in _versions(name):
            content = rec.get("content") or ""
            out.append({
                "skill_name": rec.get("skill_name") or name,
                "version": rec.get("version"),
                "created_at": rec.get("created_at"),
                "source": rec.get("source"),
                "candidate_id": rec.get("candidate_id"),
                "approver": rec.get("approver"),
                "content_bytes": len(content.encode("utf-8")),
                "preview": content[:300],
                "diff_preview": (rec.get("diff") or "")[:500],
                "evaluation": rec.get("evaluation"),
            })
    out.sort(key=lambda d: str(d.get("created_at") or ""), reverse=True)
    return out


def _promoted_text(content: str, version: str) -> str:
    fm, body = _split_fm(content)
    fm.update({
        "evolved": True,
        "license": "自研",
        "source": f"反馈进化 {version}",
    })
    return _render_fm(fm, body)


def promote(candidate_id: str, force: bool = False, approver: str = "user") -> dict:
    """人工批准后上线：存旧版本 → 写 _evolved → reload → 标记 promoted。"""
    meta = _read_meta(candidate_id)
    if not meta:
        return _err("候选不存在")
    if meta.get("status") != "pending":
        return _err(f"只有 pending 候选可晋升（当前 {meta.get('status')}）")
    ev = meta.get("evaluation") or {}
    if not force and not ev.get("recommend"):
        return _err("评测未通过，不能自动晋升；如确认要上线请显式 force=true",
                    evaluation=ev)
    name = str(meta.get("name") or "")
    if not SKILL_NAME_RE.match(name):
        return _err("技能名非法，拒绝写入 _evolved")
    try:
        with open(_cand_path(candidate_id, "skill.md"), encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return _err("候选内容缺失")

    target = safe_join(EVOLVED_DIR, name + ".md", label="技能文件名")
    os.makedirs(EVOLVED_DIR, exist_ok=True)
    next_v = _next_version(name)
    if os.path.exists(target):
        try:
            with open(target, encoding="utf-8") as f:
                old = f.read()
            _save_version(name, old, source="晋升前快照",
                          evaluation=ev, candidate_id=candidate_id, approver=approver)
        except OSError:
            pass
        next_v = _next_version(name)
    version = next_v
    text = _promoted_text(content, version)
    try:
        _save_version(name, text, source=f"晋升：{candidate_id}",
                      evaluation=ev, candidate_id=candidate_id, approver=approver)
        atomic_write_text(target, text)
    except OSError as exc:
        return _err(f"晋升写盘失败：{exc.__class__.__name__}")

    try:
        skillkit.reload()
    except Exception as exc:  # 索引失败不能把已写文件“假装”成成功
        return _err(f"已写入但索引重载失败：{exc.__class__.__name__}", path=target)

    meta["status"] = "promoted"
    meta["promoted_at"] = _now()
    meta["target"] = target
    meta["version"] = version
    meta.setdefault("audit", []).append({
        "ts": _now(), "action": "promote", "actor": str(approver or "user")[:80],
        "force": bool(force), "version": version,
    })
    _write_meta(candidate_id, meta)
    _rebuild_index()
    _audit({"action": "promote", "candidate_id": candidate_id, "name": name,
            "version": version, "approver": approver, "force": bool(force)})
    return {"ok": True, "candidate_id": candidate_id, "skill_name": name,
            "version": version, "path": target, "forced": bool(force)}


def rollback(skill_name: str, version: str = "") -> dict:
    """恢复指定/上一版本；没有可恢复版本时移除 _evolved 文件。"""
    if not SKILL_NAME_RE.match(str(skill_name or "")):
        return _err("技能名非法")
    versions = _versions(skill_name)
    picked: dict | None = None
    if version:
        for rec in versions:
            if str(rec.get("version")) == str(version):
                picked = rec
                break
        if picked is None:
            return _err(f"版本不存在：{version}")
    elif len(versions) >= 2:
        picked = versions[-2]  # 当前版本的上一版（晋升时已把旧稿存档）
    target = safe_join(EVOLVED_DIR, skill_name + ".md", label="技能文件名")
    removed = False
    restored = ""
    if picked is not None:
        text = str(picked.get("content") or "")
        if not text.strip():
            return _err("目标版本内容为空")
        os.makedirs(EVOLVED_DIR, exist_ok=True)
        atomic_write_text(target, text)
        restored = str(picked.get("version") or "")
    else:
        if os.path.exists(target):
            try:
                os.remove(target)
                removed = True
            except OSError as exc:
                return _err(f"移除失败：{exc.__class__.__name__}")
    try:
        skillkit.reload()
    except Exception as exc:
        return _err(f"索引重载失败：{exc.__class__.__name__}")
    _audit({"action": "rollback", "name": skill_name,
            "version": restored, "removed": removed})
    return {"ok": True, "skill_name": skill_name, "version": restored,
            "restored": bool(restored), "removed": removed}


# ---------------------------------------------------------------- 评测

def evaluate_candidate(candidate_id: str, baseline_skill_name: str = "", cfg=None) -> dict:
    """用固定评测集比较候选与现有同名技能；结果写回 meta，但不改 status。"""
    cand = get_candidate(candidate_id)
    if not cand.get("ok"):
        return cand
    name = cand.get("name") or ""
    base_name = baseline_skill_name or name
    base = skillkit.by_name(base_name) if base_name else None
    baseline_text = base.text if base else ""
    result = eval_mod.compare(baseline_text, cand["content"], cfg=cfg)
    run = {
        "kind": "skill_candidate",
        "id": f"cand-{candidate_id}",
        "candidate_id": candidate_id,
        "skill_name": name,
        "baseline_skill": base.relpath if base else "",
        **result,
    }
    path = eval_mod.save_run(run)
    meta = _read_meta(candidate_id) or cand
    meta["evaluation"] = {
        "baseline_avg": result["baseline_avg"],
        "candidate_avg": result["candidate_avg"],
        "delta": result["delta"],
        "win": result["win"],
        "tie": result["tie"],
        "loss": result["loss"],
        "worst_drop": result["worst_drop"],
        "recommend": result["recommend"],
        "reason": result["reason"],
        "fallback": result["fallback"],
        "evaluated_at": _now(),
        "run_path": path,
    }
    meta["updated_at"] = _now()
    meta.setdefault("audit", []).append(
        {"ts": _now(), "action": "evaluate", "actor": "system",
         "recommend": result["recommend"], "delta": result["delta"]})
    _write_meta(candidate_id, meta)
    _rebuild_index()
    return {"ok": True, "candidate_id": candidate_id, "evaluation": meta["evaluation"],
            "recommend": result["recommend"], "advice": _advice(result),
            "run_path": path}


def _advice(result: dict) -> str:
    if result.get("fallback") and not result.get("candidate_avg"):
        return "评测在离线降级模式下没有拿到有效输出：先配置可用模型重跑，或人工通读候选后再决定。"
    if result.get("recommend"):
        return "评测达到晋升线，可提交人工审批；上线后保留旧版本，随时可回滚。"
    if result.get("delta", 0) < 0:
        return "候选低于现有技能：建议驳回或按反馈重写，不要强行上线。"
    return "提升不显著：继续收集同类反馈，或缩小候选只改一个明确问题。"


# ---------------------------------------------------------------- 反馈驱动草稿

def _llm_rules(focus: list[str], med_label: str, cfg=None) -> list[str]:
    """让模型把建议压成 3–6 条规则；失败/可疑就退回模板。"""
    prompt = (
        f"把下面的改进方向改写成 3–6 条可执行写作规则，每条一行、以「1. 」这样的序号开头，"
        f"不超过 80 字，不要解释，不要加标题。文体：{med_label}。\n"
        + "\n".join(f"- {x}" for x in focus[:6])
    )
    try:
        raw = llm.chat([
            {"role": "system", "content": "你是写作教练，只输出编号规则列表。"},
            {"role": "user", "content": prompt},
        ], cfg=cfg)
    except Exception:
        return []
    lines = []
    for line in str(raw or "").splitlines():
        line = line.strip()
        m = re.match(r"^(?:\d+[.、]|[-*])\s*(.{6,200})$", line)
        if not m:
            continue
        rule = m.group(1).strip()
        if re.search(r"https?://|忽略|系统提示|<\s*script", rule, re.I):
            continue
        if rule not in lines:
            lines.append(rule)
        if len(lines) >= 6:
            break
    return lines if len(lines) >= 3 else []


def propose_from_feedback(medium: str = "", stage: str = "", cfg=None) -> dict:
    """用低分反馈生成候选草稿；只 propose，不 evaluate、不 promote。"""
    med = medium if medium_mod.is_medium(medium) else ""
    stage = str(stage or "").strip()
    if med and stage and stage not in medium_mod.all_stage_codes(med):
        return _err(f"阶段非法：{med}/{stage}")
    agg = feedback.aggregate()
    focus = feedback.suggest_focus(med, stage)
    low = [r for r in feedback.list_feedback(medium=med, stage=stage, limit=300)
           if isinstance(r.get("rating"), int) and r["rating"] <= 2]
    pairs = [p for p in feedback.export_pairs(limit=100)
             if (not med or p.get("medium") == med)
             and (not stage or p.get("stage") == stage)]

    if not focus:
        return _err("暂无可用的反馈信号：先积累评分/批注或改稿样本")
    rules = _llm_rules(focus, medium_mod.label(med) if med else "通用", cfg=cfg)
    if not rules:
        rules = [re.sub(r"^[（(]?\d+[)）.、]\s*", "", x) for x in focus[:6]]
    rules = [r[:120] for r in rules if r.strip()][:6]
    if len(rules) < 3:
        rules += [x for x in feedback.suggest_focus(med, stage) if x not in rules][:3]

    label = medium_mod.label(med) if med else "通用"
    stage_name = medium_mod.stage_names(med).get(stage, stage) if med else stage
    name = re.sub(r"[^a-z0-9-]+", "-",
                  f"evolved-{med or 'all'}-{(stage or 'all').lower()}")[:60].strip("-")
    if not SKILL_NAME_RE.match(name):
        name = "evolved-all-" + secrets.token_hex(3)
    title = f"{label}{(' · ' + stage_name) if stage_name else ''} · 反馈驱动修订要点"

    total = agg.get("total") or 0
    low_rate = agg.get("low_rating_rate") or 0
    body = [
        f"# {title}",
        "",
        f"> 生成依据：{total} 条反馈，低分 {len(low)} 条，改稿样本 {len(pairs)} 组。"
        "本文件是候选草稿，未经人工审批不会注入任何写作流程。",
        "",
        "## 一、本阶段先做什么",
        "",
    ]
    for i, rule in enumerate(rules, 1):
        body.append(f"{i}. {rule}")
    body += [
        "",
        "## 二、执行顺序（照做，不要跳步）",
        "",
        "1. 写前：把本阶段目标压缩成一句「主角要什么、谁/什么在挡、失败代价是什么」。",
        "2. 写中：每完成一个自然段就回看一次规则清单，命中问题先改结构再改词句。",
        "3. 写后：用下面的自检表逐项打勾；任何一项为「否」都先修该项，不做整体重写。",
        "",
        "## 三、交付前自检表",
        "",
        "- [ ] 是否出现了具体、可验证的信息（动作/数量/专名/身体感受），而不是形容词结论？",
        "- [ ] 是否删掉了不改变局面的段落、重复桥段与解释性总结？",
        "- [ ] 人物选择是否由「此刻最想要 / 最怕失去」驱动，而不是为推进剧情降智？",
        "- [ ] 结尾是否留下新事实、新代价或新问题，而不是情绪感叹？",
        "- [ ] 是否逐条核对了本阶段的硬约束（字数/格式/格律/场景头等）？",
        "",
        "## 四、反例（出现即重写）",
        "",
        "- 用一段总结告诉读者「刚才发生了什么」，而不是让读者自己看见。",
        "- 为了煽情临时改变人物性格，或让对手突然变蠢。",
        "- 同一章里重复使用同一个爽点/比喻/句式，只加大音量不改变代价。",
        "",
        "## 五、来源",
        "",
        f"- 反馈总量：{total}；低分率：{round(float(low_rate) * 100, 1)}%；"
        f"有实质差异的改稿配对：{len(pairs)}。",
        "- 规则由确定性聚合 + 可选模型润色生成；人工审批后才可能进入 `_evolved`。",
    ]
    content = "\n".join(body) + "\n"
    source_ids = [r.get("id") for r in low if r.get("id")][:50]
    reason = (f"聚合 {total} 条反馈、{len(low)} 条低分、{len(pairs)} 组改稿样本；"
              f"聚焦问题：" + "；".join(x[:40] for x in focus[:3]))
    return propose(name, med or "all", [stage] if stage else [], title, content,
                   reason=reason, source_feedback_ids=source_ids, cfg=cfg)
