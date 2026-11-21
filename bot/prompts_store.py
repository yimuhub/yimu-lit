"""提示词全开放存储（v5.3.0，方案 docs/系统化升级方案-v5.1 §7.2，全部自研）。

两层目录 + 代码兜底：
  1. prompts/            —— 仓库内置默认（随代码入库，只读）；
  2. data/prompts/       —— 用户覆盖层（/data/ 已 gitignore；「恢复默认」= 删用户文件）；
  3. 代码默认值          —— 调用方以 default= 原字符串传入，作为最终兜底与指纹来源。

文件格式（每组一个 md，组名=文件名）：
    ---
    id: selection-ai          # 组 id（英文 kebab）
    label: 划词润色           # 中文显示名（内置文件记录；代码注册值优先）
    base: 3f9a2b1c4d5e        # 指纹 = 该组代码默认值规范序列化的 sha1 前 12 位
    ---
    <!-- 组说明注释（不进入提示词） -->

    ## 槽名                   # 一个 `## ` 段落 = 一个槽
    温度：0.10                 # 可选段首行：覆盖该槽温度（0-2，步长 0.05）
    槽文本……                   # 其余正文 = 槽文本；{{变量}} 由调用方替换

原则：
  · text()/temperature() 永不抛异常——md 语法错、文件缺失、槽不存在一律
    静默回退下一层并 stderr 警告，绝不让创作任务失败（红线：提示词可改坏，出口闸门不变）；
  · mtime 缓存：文件未变不重读，开发热更新友好；
  · 线程安全：注册表与冻结快照由锁保护，批量任务用 freeze()/thaw() 快照生效版本
    （任务启动时冻结 → 进行中的批次不受中途编辑影响，线程结束时解冻）；
  · JSON 输出形状也作为槽放进 md：用户改坏导致解析失败由调用方既有兜底接住，
    删用户文件即恢复。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading

from .paths import data, resource

# ---------------------------------------------------------------- 路径

USER_DIR = data("prompts")


def _builtin_dir() -> str:
    """内置 prompts 目录：打包资源优先，缺省回落源码仓库目录（开发/测试同源）。"""
    p = resource("prompts")
    if os.path.isdir(p):
        return p
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")


def builtin_path(group: str) -> str:
    return os.path.join(_builtin_dir(), f"{group}.md")


def user_path(group: str) -> str:
    return os.path.join(USER_DIR, f"{group}.md")


# ---------------------------------------------------------------- 警告（等价 console.warn：只提醒，不抛错）

def _warn(msg: str) -> None:
    try:
        print(f"[prompts_store] {msg}", file=sys.stderr)
    except Exception:
        pass


# ---------------------------------------------------------------- md 解析（纯函数，前后端同一格式）

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_TEMP_LINE_RE = re.compile(r"^温度\s*[:：]\s*(-?\d+(?:\.\d+)?)\s*$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def parse_prompt_md(raw: str) -> dict:
    """解析组 md：frontmatter + `## 槽` 段落 + 段首温度行。

    返回 {"id","label","base","slots":{槽名:{"text","temp"}},"errors":[...]}。
    解析尽量宽容：错的地方记进 errors，能解析的部分照常返回。
    槽正文若自身包含 `## ` 开头的行（如输出格式段），用 ``` 围栏整段包裹——
    围栏内的行不当作槽标题，解析时剥掉外围围栏行，正文逐字保留。
    """
    out = {"id": "", "label": "", "base": "", "slots": {}, "errors": []}
    s = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    m = _FRONTMATTER_RE.match(s)
    if m:
        for line in m.group(1).split("\n"):
            kv = line.split(":", 1)
            if len(kv) != 2:
                continue
            k, v = kv[0].strip().lower(), kv[1].strip()
            if k in ("id", "label", "base"):
                out[k] = v
        s = s[m.end():]
    else:
        out["errors"].append("缺少 frontmatter（--- 块）")
    # 注释块剥离（组说明/编辑规则不进入提示词）
    s = _COMMENT_RE.sub("", s)
    # 切槽：`## 槽名` 到下一个 `## ` 之前（``` 围栏内的 ## 不是标题）
    cur, buf, in_fence = "", [], False
    for line in s.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            if cur:
                buf.append(line)
            continue
        if not in_fence:
            hm = _HEADING_RE.match(line)
            if hm:
                if cur:
                    out["slots"][cur] = _finish_slot("\n".join(buf), out["errors"], cur)
                cur, buf = hm.group(1), []
                continue
        if cur:
            buf.append(line)
        elif line.strip() and not line.startswith("#"):
            out["errors"].append(f"frontmatter 之外的游离文本：{line.strip()[:30]}")
    if cur:
        out["slots"][cur] = _finish_slot("\n".join(buf), out["errors"], cur)
    return out


def _finish_slot(body: str, errors: list, slot: str) -> dict:
    """槽正文收口：段首温度行 → temp；外围 ``` 围栏剥壳；其余为文本。"""
    temp = None
    lines = body.strip("\n").split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines):
        tm = _TEMP_LINE_RE.match(lines[idx].strip())
        if tm:
            try:
                temp = max(0.0, min(2.0, float(tm.group(1))))
            except ValueError:
                errors.append(f"槽「{slot}」温度行无法解析：{lines[idx].strip()}")
            idx += 1
    # 剥外围围栏（首尾各一条 ``` 行）：正文含 ## 小节标记时的包裹壳
    rest = lines[idx:]
    while rest and not rest[0].strip():
        rest.pop(0)
    while rest and not rest[-1].strip():
        rest.pop()
    if len(rest) >= 2 and rest[0].strip().startswith("```") and rest[-1].strip().startswith("```"):
        rest = rest[1:-1]
    text = "\n".join(rest).strip()
    if not text and temp is None:
        errors.append(f"槽「{slot}」为空")
    return {"text": text, "temp": temp}


def slot_vars(text: str) -> list:
    """提取槽文本里的 {{变量}}（去重保序）。"""
    seen, out = set(), []
    for v in re.findall(r"\{\{\s*([^{}]+?)\s*\}\}", str(text or "")):
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ---------------------------------------------------------------- 组注册表（代码默认值与元数据的家）

_GROUPS: dict[str, dict] = {}
_GROUPS_LOCK = threading.RLock()


def register(group: str, label: str, slots: dict, desc: str = "") -> None:
    """接线模块 import 时登记本组代码默认值与槽元数据（幂等，可整组覆盖）。

    slots: {槽名: {"default": 原字符串模板, "label": 中文名, "temp": 缺省温度或 None,
                   "vars": [变量名], "desc": 槽说明}}
    """
    clean = {}
    for name, meta in (slots or {}).items():
        meta = dict(meta or {})
        d = meta.get("default")
        clean[str(name)] = {
            "default": "" if d is None else str(d),
            "label": str(meta.get("label") or name),
            "temp": (None if meta.get("temp") is None else float(meta.get("temp"))),
            "vars": list(meta.get("vars") or []),
            "desc": str(meta.get("desc") or ""),
        }
    with _GROUPS_LOCK:
        _GROUPS[group] = {"label": str(label or group), "desc": str(desc or ""),
                          "slots": clean, "registered": True}


def groups() -> list:
    """全部已知组 id（注册表 ∪ 内置 md frontmatter）。"""
    ids = set(_GROUPS)
    bd = _builtin_dir()
    try:
        for fn in sorted(os.listdir(bd)):
            if fn.endswith(".md"):
                ids.add(fn[:-3])
    except OSError:
        pass
    return sorted(ids)


def group_meta(group: str) -> dict | None:
    with _GROUPS_LOCK:
        g = _GROUPS.get(group)
        if g:
            return dict(g)
    return None


def fingerprint(group: str) -> str | None:
    """组指纹 = 代码默认值规范序列化的 sha1 前 12 位（未注册组返回 None）。"""
    g = group_meta(group)
    if not g or not g["slots"]:
        return None
    canon = json.dumps(
        {k: {"text": v["default"], "temp": v["temp"]} for k, v in sorted(g["slots"].items())},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- md 读取（mtime 缓存，线程安全）

_MD_CACHE: dict = {}
_MD_CACHE_LOCK = threading.Lock()
_MD_INVALID = object()      # 「解析失败」哨兵：坏文件也缓存（同 mtime 不重复告警）


def _load_layer(path: str) -> dict | None:
    """读一层 md（内置/用户）。文件缺失 → None；解析失败 → None + 警告（每版本只警告一次）。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    with _MD_CACHE_LOCK:
        hit = _MD_CACHE.get(key)
    if hit is not None:
        return None if hit is _MD_INVALID else hit
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as err:
        _warn(f"读取失败 {path}：{err}")
        return None
    parsed = parse_prompt_md(raw)
    hard_errors = [e for e in parsed["errors"] if "frontmatter" in e]
    if parsed["errors"]:
        _warn(f"{path} 解析告警：{'；'.join(parsed['errors'][:4])}")
    if hard_errors:
        # frontmatter 缺失/残缺 = md 语法坏 → 整文件视为无效，回退下一层
        parsed = None
    with _MD_CACHE_LOCK:
        # 只保留有限缓存，防长期驻留膨胀
        if len(_MD_CACHE) > 256:
            _MD_CACHE.clear()
        _MD_CACHE[key] = _MD_INVALID if parsed is None else parsed
    return parsed


def _slot_from(path: str, slot: str) -> dict | None:
    parsed = _load_layer(path)
    if not parsed:
        return None
    ent = parsed["slots"].get(slot)
    if ent and (ent["text"] or ent["temp"] is not None):
        return ent
    return None


# ---------------------------------------------------------------- 冻结快照（批量任务运行中不受中途编辑影响）

_PINNED: dict | None = None          # {(group, slot): {"text": str|None, "temp": float|None}}
_PINNED_REF = 0
_PINNED_LOCK = threading.Lock()


def _resolve_layers(group: str, slot: str) -> dict:
    """文件两层解析：{"text": str|None, "temp": float|None}（不含代码默认）。"""
    text, temp = None, None
    ent = _slot_from(user_path(group), slot)
    if ent:
        text, temp = ent["text"], ent["temp"]
    else:
        ent = _slot_from(builtin_path(group), slot)
        if ent:
            text, temp = ent["text"], ent["temp"]
    return {"text": text, "temp": temp}


def snapshot() -> dict:
    """抓取当前文件层生效版本（{(group, slot): {"text","temp"}}），供冻结/诊断。"""
    out = {}
    for group in groups():
        meta = group_meta(group)
        names = set((meta or {}).get("slots") or {})
        parsed_b = _load_md_safe(builtin_path(group))
        parsed_u = _load_md_safe(user_path(group))
        for p in (parsed_b, parsed_u):
            if p:
                names.update(p["slots"].keys())
        for slot in sorted(names):
            out[(group, slot)] = _resolve_layers(group, slot)
    return out


def _load_md_safe(path: str) -> dict | None:
    try:
        return _load_layer(path)
    except Exception as err:      # 快照绝不抛错
        _warn(f"快照读取 {path} 失败：{err}")
        return None


def freeze() -> None:
    """批量任务启动：冻结当前生效版本（引用计数，可嵌套）。冻结后 text/temperature
    只认快照——运行中的批次不受中途编辑影响；下次任务启动自动用上最新版。"""
    global _PINNED, _PINNED_REF
    with _PINNED_LOCK:
        if _PINNED_REF == 0:
            try:
                _PINNED = snapshot()
            except Exception as err:
                _warn(f"冻结快照失败（按未冻结处理）：{err}")
                _PINNED = None
        _PINNED_REF += 1


def thaw() -> None:
    """批量任务结束：解冻（配对调用；多余调用无害）。"""
    global _PINNED, _PINNED_REF
    with _PINNED_LOCK:
        if _PINNED_REF > 0:
            _PINNED_REF -= 1
        if _PINNED_REF == 0:
            _PINNED = None


def frozen() -> bool:
    with _PINNED_LOCK:
        return _PINNED is not None


# ---------------------------------------------------------------- 对外主入口

def text(group: str, slot: str, default: str) -> str:
    """槽文本：冻结快照 → 用户层 → 内置层 → 代码默认。永不抛错。"""
    try:
        with _PINNED_LOCK:
            pin = _PINNED
        if pin is not None:
            ent = pin.get((group, slot))
            if ent and ent.get("text"):
                return ent["text"]
            return default      # 冻结时刻该槽未被文件覆盖 → 沿用代码默认，忽略期间新增覆盖
        ent = _resolve_layers(group, slot)
        if ent["text"]:
            return ent["text"]
    except Exception as err:    # 任何装载问题都不许打断创作
        _warn(f"text({group}.{slot}) 回退代码默认：{err}")
    return default


def temperature(group: str, slot: str, default: float) -> float:
    """槽温度：用户层 → 内置层 → 代码默认。永不抛错。"""
    try:
        with _PINNED_LOCK:
            pin = _PINNED
        if pin is not None:
            ent = pin.get((group, slot))
            if ent and ent.get("temp") is not None:
                return float(ent["temp"])
            return float(default)
        ent = _resolve_layers(group, slot)
        if ent["temp"] is not None:
            return float(ent["temp"])
    except Exception as err:
        _warn(f"temperature({group}.{slot}) 回退代码默认：{err}")
    try:
        return float(default)
    except (TypeError, ValueError):
        return 0.7


def fill(template: str, **vars_) -> str:
    """替换 {{变量}}（显式传参替换，不用 str.format——槽里有 JSON 花括号）。"""
    s = str(template or "")
    for k, v in (vars_ or {}).items():
        s = s.replace("{{" + str(k) + "}}", "" if v is None else str(v))
    return s


# ---------------------------------------------------------------- 用户文件维护（编辑页后端）

def user_md_raw(group: str) -> str | None:
    """用户层原始 md（不存在返回 None）。"""
    try:
        with open(user_path(group), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def builtin_parsed(group: str) -> dict | None:
    """内置层解析结果（含 id/label/base/slots）。文件缺失或语法坏 → None。"""
    return _load_md_safe(builtin_path(group))


def user_parsed(group: str) -> dict | None:
    """用户层解析结果（含 id/label/base/slots）。文件缺失 → None；语法坏 → None + 警告。"""
    return _load_md_safe(user_path(group))


def builtin_md_raw(group: str) -> str | None:
    try:
        with open(builtin_path(group), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def validate_user_md(group: str, raw: str) -> tuple[dict, list]:
    """校验用户层 md：槽名必须存在于该组（注册表 ∪ 内置 md）。返回 (parsed, errors)。

    frontmatter 缺失不拒绝——save_user_md 会归一补齐；其余解析告警照常拒绝。
    """
    parsed = parse_prompt_md(raw)
    errors = [e for e in parsed["errors"] if not e.startswith("缺少 frontmatter")]
    known = set(((group_meta(group) or {}).get("slots")) or {})
    b = _load_md_safe(builtin_path(group))
    if b:
        known.update(b["slots"].keys())
    if parsed["id"] and parsed["id"] != group:
        errors.append(f"frontmatter id={parsed['id']} 与组 {group} 不一致")
    for name in parsed["slots"]:
        if name not in known:
            errors.append(f"未知槽「{name}」（该组可用槽：{'/'.join(sorted(known)) or '（组不存在）'}）")
    if not known:
        errors.append(f"组「{group}」不存在或没有可用槽")
    return parsed, errors


def save_user_md(group: str, raw: str) -> dict:
    """保存用户层 md：校验通过后归一 frontmatter（id + 当前指纹）并原子落盘。"""
    parsed, errors = validate_user_md(group, raw)
    if errors:
        return {"ok": False, "errors": errors}
    body = str(raw or "").replace("\r\n", "\n")
    m = _FRONTMATTER_RE.match(body)
    rest = body[m.end():] if m else body
    fp = fingerprint(group) or ""
    label = parsed.get("label") or ((group_meta(group) or {}).get("label") if group_meta(group) else "") or group
    # frontmatter 归一：id/label/base 与当前状态对齐（用户正文原样保留）
    head = f"---\nid: {group}\nlabel: {label}\nbase: {fp}\n---\n"
    out = head + rest.lstrip("\n")
    os.makedirs(USER_DIR, exist_ok=True)
    from .safeio import atomic_write_text
    atomic_write_text(user_path(group), out)
    with _MD_CACHE_LOCK:
        _MD_CACHE.clear()      # 简单起见：写盘后整体失效缓存
    return {"ok": True, "path": user_path(group), "base": fp}


def delete_user_md(group: str) -> bool:
    """恢复默认 = 删用户覆盖文件。"""
    try:
        os.remove(user_path(group))
        with _MD_CACHE_LOCK:
            _MD_CACHE.clear()
        return True
    except FileNotFoundError:
        return False
    except OSError as err:
        _warn(f"删除用户层失败 {group}：{err}")
        return False


def adopt(group: str) -> dict:
    """采用新版默认：把用户文件 frontmatter base 重写为当前指纹（保留用户槽文本）。"""
    raw = user_md_raw(group)
    if raw is None:
        return {"ok": False, "errors": ["该组没有用户覆盖文件，无需采用"]}
    fp = fingerprint(group) or ""
    m = _FRONTMATTER_RE.match(raw.replace("\r\n", "\n"))
    rest = raw[m.end():] if m else raw
    parsed = parse_prompt_md(raw)
    label = parsed.get("label") or ((group_meta(group) or {}).get("label") if group_meta(group) else "") or group
    head = f"---\nid: {group}\nlabel: {label}\nbase: {fp}\n---\n"
    from .safeio import atomic_write_text
    os.makedirs(USER_DIR, exist_ok=True)
    atomic_write_text(user_path(group), head + rest.lstrip("\n"))
    with _MD_CACHE_LOCK:
        _MD_CACHE.clear()
    return {"ok": True, "base": fp}
