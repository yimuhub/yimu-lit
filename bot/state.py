"""权威状态库（canonical state）：长篇连续性的单一事实源。

为什么需要它：旧实现把人物/线索状态散落在 00/09/10/11/12 等 Markdown 里，
每章写前整文件注入、写后追加一段自然语言，既无法精确判断"谁此刻知道什么"，
也无法在 300–1500 章后做确定性校验。这里把状态收敛成结构化 JSON，
写入走 delta + 审计日志，读侧按"本章相关"挑选后给 LLM。

项目目录下的数据文件：
  18-状态库.json      权威状态（人物/地点/物品/势力/规则/线索/伏笔/时间线/章摘要/指标）
  18b-状态日志.jsonl  每次 delta 一行审计；同时用于同章同 source 的幂等判断
  19-伏笔账本.json    面向旧 UI/读者的伏笔账本（与 state.foreshadowing 同步）

设计纪律：
  * 所有公开函数对坏输入返回安全默认值，不抛异常；
  * merge_state_delta 是纯函数，先 deepcopy 再合并，非法 delta 不破坏原状态；
  * apply_delta 用进程内锁 + fcntl 文件锁保护读改写，支持并行章节写作。
"""
from __future__ import annotations

import contextlib
import copy
import datetime
import json
import os
import re
import threading

from .safeio import atomic_write_json, atomic_write_text

STATE_FILE = "18-状态库.json"
LOG_FILE = "18b-状态日志.jsonl"
LEDGER_FILE = "19-伏笔账本.json"
_LOG_KEEP = 20000          # 日志超过此规模时截断，避免无限膨胀
_LOG_SCAN = 8000           # 幂等判断只看最近若干行（旧记录已无审计价值）
_MAX_ISSUES = 200          # validate 输出的问题上限，防止坏数据刷屏

_LOCK_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


# ---------- 基础工具 ----------

def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _int(v, default: int = 0) -> int:
    """宽容取整：字符串数字、浮点、None 都安全。"""
    try:
        if isinstance(v, bool):
            return int(v)
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _str(v, cap: int = 2000) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        try:
            v = json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            v = str(v)
    return str(v).strip()[:cap]


def _list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    if v in (None, ""):
        return []
    return [v]


def _uniq(items) -> list:
    seen, out = set(), []
    for x in _list(items):
        s = _str(x, 500)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _pairs(obj):
    """把 dict / list[dict] 统一成 (名称, 字段 dict) 序列。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            name = _str(k, 80)
            if not name:
                continue
            if isinstance(v, dict):
                yield name, v
            elif isinstance(v, str):
                yield name, {"notes": v}
            else:
                yield name, {}
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                name = _str(item.get("name") or item.get("id") or item.get("title"), 80)
                if name:
                    fields = {k: v for k, v in item.items()
                              if k not in ("name", "id", "title")}
                    yield name, fields
            elif isinstance(item, str) and item.strip():
                yield item.strip()[:80], {}


def _relation_map(v) -> dict:
    if not isinstance(v, dict):
        return {}
    return {_str(k, 60): _str(val, 300) for k, val in v.items() if _str(k, 60)}


def _norm_status(v) -> str:
    """伏笔状态归一：兼容旧账本里的中文/英文写法。"""
    s = _str(v, 20).lower()
    if not s:
        return "open"
    if any(k in s for k in ("paid", "done", "closed", "回收", "兑现", "已解")):
        return "paid"
    if any(k in s for k in ("drop", "cut", "abandon", "放弃", "废弃")):
        return "dropped"
    return "open"


def _chapter_key(n) -> str:
    return str(_int(n, 0))


# ---------- 规范化 ----------

def empty_state() -> dict:
    return {
        "version": 1,
        "characters": {},
        "locations": {},
        "items": {},
        "factions": {},
        "rules": [],
        "threads": [],
        "foreshadowing": {},
        "timeline": [],
        "chapter_summaries": {},
        "metrics": {"words": 0, "chapters": 0, "last_chapter": 0, "updated_at": ""},
        "issues": [],
    }


def _norm_character(raw, chapter: int = 0) -> dict:
    f = raw if isinstance(raw, dict) else {}
    first = _int(f.get("first_seen"), chapter)
    last = _int(f.get("last_seen"), chapter)
    out = {
        "aliases": _uniq(f.get("aliases")),
        "status": _str(f.get("status"), 60),
        "location": _str(f.get("location"), 80),
        "goal": _str(f.get("goal"), 300),
        "motivation": _str(f.get("motivation"), 300),
        "knowledge": _uniq(f.get("knowledge")),
        "relations": _relation_map(f.get("relations")),
        "arc": _str(f.get("arc"), 300),
        "first_seen": first,
        "last_seen": last,
        "notes": _str(f.get("notes"), 600),
    }
    for k, v in f.items():
        if k not in out and not isinstance(v, (dict, list)):
            out[k] = v
    return out


def _norm_location(raw, chapter: int = 0) -> dict:
    f = raw if isinstance(raw, dict) else {}
    return {
        "aliases": _uniq(f.get("aliases")),
        "status": _str(f.get("status"), 60),
        "description": _str(f.get("description") or f.get("notes"), 600),
        "first_seen": _int(f.get("first_seen"), chapter),
        "last_seen": _int(f.get("last_seen"), chapter),
    }


def _norm_foreshadow(raw, chapter: int = 0) -> dict:
    f = raw if isinstance(raw, dict) else {}
    setup = _int(f.get("setup_chapter"), chapter)
    due_from = _int(f.get("due_from"), 0)
    due_to = _int(f.get("due_to"), 0)
    return {
        "description": _str(f.get("description") or f.get("name") or f.get("text"), 600),
        "setup_chapter": setup,
        "due_from": due_from,
        "due_to": due_to,
        "status": _norm_status(f.get("status")),
        "payoff_chapter": _int(f.get("payoff_chapter"), 0),
        "importance": _str(f.get("importance"), 20),
        "notes": _str(f.get("notes"), 600),
    }


def _norm_timeline_item(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    ch = _int(raw.get("chapter"), 0)
    if ch <= 0:
        return None
    return {
        "chapter": ch,
        "time": _str(raw.get("time"), 80),
        "event": _str(raw.get("event") or raw.get("summary"), 600),
        "location": _str(raw.get("location"), 80),
        "characters": _uniq(raw.get("characters")),
    }


def _norm_summary(raw) -> dict:
    f = raw if isinstance(raw, dict) else {"summary": _str(raw, 1200)}
    return {
        "summary": _str(f.get("summary"), 2000),
        "beats": _uniq(f.get("beats")),
        "promises": _uniq(f.get("promises")),
        "state_changed": bool(f.get("state_changed")),
    }


def _norm_issue(raw, chapter: int = 0) -> dict:
    if isinstance(raw, dict):
        return {
            "id": _str(raw.get("id"), 80),
            "type": _str(raw.get("type"), 60),
            "detail": _str(raw.get("detail") or raw.get("message"), 600),
            "chapter": _int(raw.get("chapter"), chapter),
            "status": _str(raw.get("status") or "open", 20),
        }
    return {"id": "", "type": "note", "detail": _str(raw, 600),
            "chapter": chapter, "status": "open"}


def _normalize_state(raw) -> dict:
    """把任意输入修成合法状态；未知字段保留但只做浅层清洗。"""
    st = empty_state()
    if not isinstance(raw, dict):
        return st
    for name, fields in _pairs(raw.get("characters")):
        st["characters"][name] = _norm_character(fields)
    for name, fields in _pairs(raw.get("locations")):
        st["locations"][name] = _norm_location(fields)
    for name, fields in _pairs(raw.get("items")):
        st["items"][name] = fields if isinstance(fields, dict) else {}
    for name, fields in _pairs(raw.get("factions")):
        st["factions"][name] = fields if isinstance(fields, dict) else {}
    st["rules"] = _uniq(raw.get("rules"))
    st["threads"] = [t for t in _list(raw.get("threads")) if isinstance(t, dict)]
    for fid, fields in _pairs(raw.get("foreshadowing")):
        st["foreshadowing"][fid] = _norm_foreshadow(fields)
    for item in _list(raw.get("timeline")):
        it = _norm_timeline_item(item)
        if it:
            st["timeline"].append(it)
    for key, val in _pairs(raw.get("chapter_summaries")):
        n = _int(key, 0)
        if n > 0:
            st["chapter_summaries"][str(n)] = _norm_summary(val)
    m = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    st["metrics"] = {
        "words": _int(m.get("words"), 0),
        "chapters": _int(m.get("chapters"), 0),
        "last_chapter": _int(m.get("last_chapter"), 0),
        "updated_at": _str(m.get("updated_at"), 40),
    }
    st["issues"] = [_norm_issue(x) for x in _list(raw.get("issues"))][:_MAX_ISSUES]
    return st


# ---------- 读写 ----------

def _path(pdir: str, name: str) -> str:
    """项目内固定文件名，全部来自本模块常量，不接受外部原始字符串。"""
    return os.path.join(os.path.abspath(pdir), name)


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def exists(pdir: str) -> bool:
    try:
        return os.path.isfile(_path(pdir, STATE_FILE))
    except Exception:
        return False


def load(pdir: str) -> dict:
    """读权威状态；文件缺失/损坏返回空状态（绝不抛）。"""
    try:
        raw = _read_json(_path(pdir, STATE_FILE), None)
    except Exception:
        raw = None
    st = _normalize_state(raw)
    # 旧项目可能只有 19-伏笔账本：把尚未进入权威状态的伏笔并进来（只补缺，不覆盖）。
    try:
        led = _read_json(_path(pdir, LEDGER_FILE), None)
        items = None
        if isinstance(led, dict):
            items = led.get("items") or led.get("foreshadowing")
        elif isinstance(led, list):
            items = led
        for fid, fields in _pairs(items):
            if fid not in st["foreshadowing"]:
                st["foreshadowing"][fid] = _norm_foreshadow(fields)
    except Exception:
        pass
    return st


def _sync_ledger(pdir: str, state: dict) -> None:
    """把权威伏笔同步到 19-伏笔账本.json；旧账本独有字段保留（旧 UI 可能依赖）。"""
    try:
        old = _read_json(_path(pdir, LEDGER_FILE), {})
        old_items = {}
        if isinstance(old, dict):
            raw = old.get("items") or old.get("foreshadowing") or {}
            for fid, fields in _pairs(raw):
                old_items[fid] = fields if isinstance(fields, dict) else {}
        elif isinstance(old, list):
            for fid, fields in _pairs(old):
                old_items[fid] = fields if isinstance(fields, dict) else {}
        merged = dict(old_items)
        for fid, fields in state.get("foreshadowing", {}).items():
            item = dict(old_items.get(fid) or {})
            item.update(fields)
            merged[fid] = item
        atomic_write_json(_path(pdir, LEDGER_FILE),
                          {"version": 1, "updated_at": _now(), "items": merged})
    except Exception:
        pass


def save(pdir: str, state: dict) -> dict:
    """规范化后原子落盘，并同步伏笔账本。返回实际写入的状态。"""
    st = _normalize_state(state)
    st["metrics"]["updated_at"] = _now()
    try:
        atomic_write_json(_path(pdir, STATE_FILE), st)
    except Exception:
        return st
    _sync_ledger(pdir, st)
    return st


# ---------- 锁 ----------

def _lock_for(pdir: str) -> threading.RLock:
    key = os.path.abspath(pdir)
    with _LOCK_GUARD:
        lk = _LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[key] = lk
        return lk


@contextlib.contextmanager
def _state_lock(pdir: str):
    """进程内 RLock + 跨进程 fcntl 文件锁；fcntl 不可用时退化为纯线程锁。"""
    with _lock_for(pdir):
        fh = None
        try:
            try:
                import fcntl  # POSIX only
            except ImportError:
                fcntl = None
            if fcntl is not None:
                fh = open(_path(pdir, ".18-state.lock"), "a+", encoding="utf-8")
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fh is not None:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    fh.close()
                except Exception:
                    pass


# ---------- 日志 / 幂等 ----------

def _log_tail(pdir: str, limit: int = _LOG_SCAN) -> list[dict]:
    p = _path(pdir, LOG_FILE)
    try:
        lines = open(p, encoding="utf-8").read().splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _already_applied(pdir: str, chapter_no: int, source: str) -> bool:
    for rec in reversed(_log_tail(pdir)):
        if (_int(rec.get("chapter"), -1) == chapter_no
                and _str(rec.get("source"), 80) == source
                and rec.get("result") == "applied"):
            return True
    return False


def _append_log(pdir: str, rec: dict) -> None:
    """追加审计行；失败不阻断主流程。超长时截断最旧部分。"""
    try:
        p = _path(pdir, LOG_FILE)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # 控制文件规模（读一次行数；日志本身很小，代价可接受）
        try:
            if os.path.getsize(p) > 4_000_000:
                lines = open(p, encoding="utf-8").read().splitlines()[-_LOG_KEEP:]
                atomic_write_text(p, "\n".join(lines) + "\n")
        except OSError:
            pass
    except Exception:
        pass


# ---------- 纯合并 ----------

def _merge_character(cur: dict, fields: dict, chapter: int) -> dict:
    out = copy.deepcopy(cur) if isinstance(cur, dict) else {}
    for k, v in fields.items():
        if k in ("aliases", "knowledge"):
            out[k] = _uniq(list(_list(out.get(k))) + list(_list(v)))
        elif k == "relations":
            rel = dict(out.get("relations") or {})
            rel.update(_relation_map(v))
            out["relations"] = rel
        elif k in ("first_seen", "last_seen"):
            out[k] = _int(v, chapter)
        elif k in ("status", "location", "goal", "motivation", "arc", "notes"):
            out[k] = _str(v, 600)
        else:
            out[k] = v
    if chapter > 0:
        out["first_seen"] = out.get("first_seen") or chapter
        out["last_seen"] = max(_int(out.get("last_seen"), 0), chapter)
    return _norm_character(out, chapter)


def _merge_foreshadow(cur: dict, fields: dict, chapter: int) -> dict:
    out = copy.deepcopy(cur) if isinstance(cur, dict) else {}
    for k, v in fields.items():
        if k == "status":
            out["status"] = _norm_status(v)
        elif k in ("setup_chapter", "due_from", "due_to", "payoff_chapter"):
            out[k] = _int(v, out.get(k) or 0)
        else:
            out[k] = _str(v, 600)
    if chapter > 0 and not out.get("setup_chapter"):
        out["setup_chapter"] = chapter
    return _norm_foreshadow(out, chapter)


def merge_state_delta(state: dict, delta: dict) -> dict:
    """纯函数：返回合并后的新状态，绝不修改传入对象。"""
    base = _normalize_state(copy.deepcopy(state) if isinstance(state, dict) else {})
    if not isinstance(delta, dict):
        return base
    chapter = _int(delta.get("chapter") or delta.get("_chapter"), 0)

    for name, fields in _pairs(delta.get("characters")):
        base["characters"][name] = _merge_character(
            base["characters"].get(name) or {}, fields, chapter)

    for name, fields in _pairs(delta.get("locations")):
        cur = copy.deepcopy(base["locations"].get(name) or {})
        for k, v in fields.items():
            if k in ("first_seen", "last_seen"):
                cur[k] = _int(v, chapter)
            elif k == "aliases":
                cur[k] = _uniq(list(_list(cur.get(k))) + list(_list(v)))
            else:
                cur[k] = _str(v, 600)
        if chapter > 0:
            cur["first_seen"] = cur.get("first_seen") or chapter
            cur["last_seen"] = max(_int(cur.get("last_seen"), 0), chapter)
        base["locations"][name] = _norm_location(cur, chapter)

    for section in ("items", "factions"):
        for name, fields in _pairs(delta.get(section)):
            cur = dict(base[section].get(name) or {})
            cur.update(fields if isinstance(fields, dict) else {})
            base[section][name] = cur

    base["rules"] = _uniq(list(base["rules"]) + list(_list(delta.get("rules"))))

    # 线索/线程：按 id 合并，缺 id 的用描述当 id
    for item in _list(delta.get("threads")):
        if not isinstance(item, dict):
            if isinstance(item, str) and item.strip():
                item = {"id": item.strip()[:40], "description": item.strip()[:300]}
            else:
                continue
        tid = _str(item.get("id") or item.get("name") or item.get("description"), 60)
        if not tid:
            continue
        cur = None
        for t in base["threads"]:
            if _str(t.get("id") or t.get("name"), 60) == tid:
                cur = t
                break
        if cur is None:
            cur = {"id": tid}
            base["threads"].append(cur)
        for k, v in item.items():
            if k not in ("id", "name"):
                cur[k] = _str(v, 600) if not isinstance(v, (dict, list)) else v

    for fid, fields in _pairs(delta.get("foreshadowing")):
        base["foreshadowing"][fid] = _merge_foreshadow(
            base["foreshadowing"].get(fid) or {}, fields, chapter)

    for item in _list(delta.get("timeline")):
        it = _norm_timeline_item(item)
        if not it:
            continue
        dup = any(t["chapter"] == it["chapter"] and t["event"] == it["event"]
                  for t in base["timeline"])
        if not dup:
            base["timeline"].append(it)
    base["timeline"].sort(key=lambda t: _int(t.get("chapter"), 0))
    if len(base["timeline"]) > 20000:
        base["timeline"] = base["timeline"][-20000:]

    for key, val in _pairs(delta.get("chapter_summaries")):
        n = _int(key, 0)
        if n <= 0:
            continue
        cur = dict(base["chapter_summaries"].get(str(n)) or {})
        cur.update(_norm_summary(val))
        base["chapter_summaries"][str(n)] = cur

    for item in _list(delta.get("issues")):
        iss = _norm_issue(item, chapter)
        if not iss["detail"]:
            continue
        if not any(_str(x.get("detail"), 600) == iss["detail"] for x in base["issues"]):
            base["issues"].append(iss)
    base["issues"] = base["issues"][:_MAX_ISSUES]

    m = delta.get("metrics") if isinstance(delta.get("metrics"), dict) else {}
    if m:
        if m.get("words") is not None:
            base["metrics"]["words"] = _int(m.get("words"), 0)
        if m.get("chapters") is not None:
            base["metrics"]["chapters"] = _int(m.get("chapters"), 0)
        if m.get("last_chapter") is not None:
            base["metrics"]["last_chapter"] = _int(m.get("last_chapter"), 0)
    base["metrics"]["chapters"] = max(base["metrics"]["chapters"],
                                      len(base["chapter_summaries"]))
    base["metrics"]["last_chapter"] = max(
        base["metrics"]["last_chapter"], chapter,
        max((_int(k) for k in base["chapter_summaries"]), default=0),
        max((_int(t.get("chapter")) for t in base["timeline"]), default=0))
    return base


def apply_delta(pdir: str, chapter_no: int, delta: dict, source: str = "writer") -> dict:
    """合并一次状态增量并落盘。

    幂等：同一 chapter + source 只应用一次（读 18b 日志判定），
    重复调用直接返回当前状态，不重复追加 timeline/threads。
    """
    n = _int(chapter_no, 0)
    src = _str(source, 80) or "writer"
    src = re.sub(r"[^\w\u4e00-\u9fff.-]", "", src)[:40] or "writer"
    if n <= 0 or n > 20000:
        _append_log(pdir, {"ts": _now(), "chapter": n, "source": src, "delta": delta,
                           "result": "rejected", "reason": "章号越界"})
        return load(pdir)
    if not isinstance(delta, dict):
        _append_log(pdir, {"ts": _now(), "chapter": n, "source": src, "delta": delta,
                           "result": "rejected", "reason": "delta 不是对象"})
        return load(pdir)

    with _state_lock(pdir):
        cur = load(pdir)
        if _already_applied(pdir, n, src):
            return cur
        # 把章号写进 delta：人物 first_seen/last_seen、时间线都依赖它
        work = dict(delta)
        work["chapter"] = n
        try:
            new = merge_state_delta(cur, work)
        except Exception as e:  # 极端坏数据：拒绝并保留原状态
            _append_log(pdir, {"ts": _now(), "chapter": n, "source": src, "delta": delta,
                               "result": "rejected", "reason": f"合并失败：{e}"})
            return cur
        try:
            saved = save(pdir, new)
        except Exception as e:
            _append_log(pdir, {"ts": _now(), "chapter": n, "source": src, "delta": delta,
                               "result": "rejected", "reason": f"写盘失败：{e}"})
            return cur
        _append_log(pdir, {"ts": _now(), "chapter": n, "source": src, "delta": delta,
                           "result": "applied", "reason": ""})
        return saved


# ---------- 便捷写入口（就地修改并返回 state） ----------

def upsert_character(state: dict, name: str, **fields) -> dict:
    if not isinstance(state, dict):
        state = empty_state()
    name = _str(name, 80)
    if not name:
        return state
    chapter = _int(fields.pop("chapter", 0), 0)
    state.setdefault("characters", {})
    state["characters"][name] = _merge_character(
        state["characters"].get(name) or {}, fields, chapter)
    return state


def upsert_foreshadow(state: dict, id: str, **fields) -> dict:
    if not isinstance(state, dict):
        state = empty_state()
    fid = _str(id, 80)
    if not fid:
        return state
    chapter = _int(fields.pop("chapter", 0), 0)
    state.setdefault("foreshadowing", {})
    state["foreshadowing"][fid] = _merge_foreshadow(
        state["foreshadowing"].get(fid) or {}, fields, chapter)
    return state


def close_issue(state: dict, issue_id, note: str = "") -> dict:
    """按 id（或序号）关闭一个 issue；找不到也安全返回。"""
    if not isinstance(state, dict):
        return state
    issues = state.get("issues")
    if not isinstance(issues, list):
        return state
    target = _str(issue_id, 80)
    idx = _int(issue_id, -1)
    for i, iss in enumerate(issues):
        if not isinstance(iss, dict):
            continue
        if (target and _str(iss.get("id"), 80) == target) or i == idx:
            iss["status"] = "closed"
            if note:
                iss["detail"] = (_str(iss.get("detail"), 500) + f"（处理：{note[:200]}）")[:600]
            break
    return state


# ---------- 校验 ----------

def validate(state: dict) -> list[str]:
    """确定性结构校验：返回中文问题列表，不抛异常。"""
    issues: list[str] = []
    try:
        st = _normalize_state(state)
    except Exception:
        return ["状态库无法解析"]

    chars = st["characters"]
    locations = st["locations"]
    for name, c in chars.items():
        for other in c.get("relations", {}):
            if other and other not in chars:
                issues.append(f"人物「{name}」的关系引用了不存在的人物「{other}」")
        loc = c.get("location")
        if loc and locations and loc not in locations:
            issues.append(f"人物「{name}」所在地点「{loc}」未登记")
        if c.get("first_seen") and c.get("last_seen") and c["first_seen"] > c["last_seen"]:
            issues.append(f"人物「{name}」首次出场晚于最近出场")

    for fid, f in st["foreshadowing"].items():
        due_from, due_to = f.get("due_from", 0), f.get("due_to", 0)
        if due_from and due_to and due_from > due_to:
            issues.append(f"伏笔「{fid}」的兑现区间反转（due_from={due_from} > due_to={due_to}）")
        setup = f.get("setup_chapter", 0)
        if setup and due_to and setup > due_to:
            issues.append(f"伏笔「{fid}」埋设晚于计划兑现（第{setup}章 > 第{due_to}章）")
        if f.get("status") == "paid" and not f.get("payoff_chapter"):
            issues.append(f"伏笔「{fid}」已标记兑现但缺少兑现章号")

    last_ch = 0
    for key in st["chapter_summaries"]:
        n = _int(key, 0)
        if n <= 0:
            issues.append(f"章节摘要存在非法章号：{key!r}")
        last_ch = max(last_ch, n)
    if st["metrics"]["last_chapter"] and last_ch and st["metrics"]["last_chapter"] < last_ch:
        issues.append(f"状态指标 last_chapter={st['metrics']['last_chapter']} 落后于已登记摘要（第{last_ch}章）")

    prev = 0
    for item in st["timeline"]:
        ch = _int(item.get("chapter"), 0)
        if ch <= 0:
            issues.append("时间线存在非法章号条目")
            continue
        if ch < prev:
            issues.append(f"时间线顺序错乱：第{ch}章排在第{prev}章之后")
        prev = max(prev, ch)

    for t in st["threads"]:
        fid = _str(t.get("foreshadow_id"), 80)
        if fid and fid not in st["foreshadowing"]:
            issues.append(f"线索「{_str(t.get('id') or t.get('name'), 40)}」引用了不存在的伏笔「{fid}」")

    return issues[:_MAX_ISSUES]


# ---------- 伏笔到期 ----------

def foreshadow_due(state: dict, chapter_no: int, lookahead: int = 15) -> dict:
    """按章号把未兑现伏笔分成：本期到期 / 已逾期 / 临近。"""
    st = _normalize_state(state)
    ch = max(1, _int(chapter_no, 1))
    la = max(0, _int(lookahead, 15))
    due, overdue, upcoming = [], [], []
    for fid, f in st["foreshadowing"].items():
        if f.get("status") != "open":
            continue
        item = dict(f)
        item["id"] = fid
        due_from = _int(f.get("due_from"), 0)
        due_to = _int(f.get("due_to"), 0)
        if not due_from and not due_to:
            continue
        if not due_from:
            due_from = due_to
        if not due_to:
            due_to = due_from
        if due_to < ch:
            overdue.append(item)
        elif due_from <= ch <= due_to:
            due.append(item)
        elif ch < due_from <= ch + la:
            upcoming.append(item)
    key = lambda x: (_int(x.get("due_to"), 999999), _str(x.get("id"), 40))
    return {"due": sorted(due, key=key), "overdue": sorted(overdue, key=key),
            "upcoming": sorted(upcoming, key=key)}


# ---------- 截断 ----------

def clip_text(text: str, limit: int, marker: str = "……（已压缩）") -> str:
    """按段落/句子边界截断，避免把一句话切成半截；返回长度 <= limit。"""
    text = text or ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    allow = max(10, limit - len(marker) - 1)
    cut = text.rfind("\n\n", 0, allow)
    if cut < allow * 0.4:
        cut = text.rfind("\n", 0, allow)
    if cut < allow * 0.4:
        cut = -1
        for p in "。！？；!?;":
            pos = text.rfind(p, 0, allow)
            if pos > cut:
                cut = pos + 1
        if cut < allow * 0.4:
            cut = allow
    return text[:cut].rstrip() + "\n" + marker


# ---------- 给 LLM 的相关性摘录 ----------

def _bigrams(text: str) -> set[str]:
    s = re.sub(r"[\s，。！？,.!?:：；;、“”‘’（）()《》\-]+", "", text or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _score(query_bg: set[str], *texts) -> int:
    if not query_bg:
        return 0
    blob = " ".join(_str(t, 500) for t in texts if t)
    return len(query_bg & _bigrams(blob))


def excerpt(state: dict, query: str = "", max_chars: int = 6000, focus_chars: int = None) -> str:
    """给 LLM 的状态摘要：本章相关人物/地点/伏笔优先，其余压缩概览。"""
    st = _normalize_state(state)
    limit = max(500, _int(max_chars, 6000))
    focus_limit = max(400, _int(focus_chars, max(1200, limit // 2)))
    qb = _bigrams(query)
    latest = st["metrics"].get("last_chapter", 0)

    def recency(n) -> int:
        n = _int(n, 0)
        return max(0, 30 - max(0, latest - n)) if latest else 0

    scored = []
    for name, c in st["characters"].items():
        sc = _score(qb, name, c.get("status"), c.get("goal"), c.get("notes"), *c.get("aliases", []))
        scored.append((sc + recency(c.get("last_seen")), name, c))
    scored.sort(key=lambda x: (-x[0], -_int(x[2].get("last_seen"))))
    rel_chars = [x for x in scored if x[0] > 0][:6] or scored[:4]

    # 详情：相关人物 + 相关地点 + 伏笔 + 最近事件
    detail = ["【本章相关人物】"]
    if rel_chars:
        for _sc, name, c in rel_chars:
            bits = []
            if c.get("aliases"):
                bits.append("别名：" + "、".join(c["aliases"][:3]))
            for k, label in (("status", "状态"), ("location", "位置"), ("goal", "目标"),
                             ("motivation", "动机"), ("arc", "人物弧")):
                if c.get(k):
                    bits.append(f"{label}：{c[k]}")
            if c.get("relations"):
                rel = "、".join(f"{k}→{v}" for k, v in list(c["relations"].items())[:4])
                bits.append("关系：" + rel)
            if c.get("knowledge"):
                bits.append("已知：" + "；".join(c["knowledge"][-3:]))
            detail.append(f"- {name}：" + ("；".join(bits) or "（无更多档案）"))
    else:
        detail.append("- （尚未登记人物状态）")

    rel_locs = []
    loc_names = {c.get("location") for _, _, c in rel_chars if c.get("location")}
    for name, loc in st["locations"].items():
        sc = _score(qb, name, loc.get("description")) + (3 if name in loc_names else 0)
        if sc > 0 or name in loc_names:
            rel_locs.append((sc, name, loc))
    rel_locs.sort(key=lambda x: -x[0])
    if rel_locs:
        detail.append("【相关地点】")
        for _sc, name, loc in rel_locs[:4]:
            detail.append(f"- {name}：{(loc.get('description') or loc.get('status') or '')[:160]}")

    open_fs = []
    for fid, f in st["foreshadowing"].items():
        if f.get("status") != "open":
            continue
        sc = _score(qb, fid, f.get("description"))
        due = _int(f.get("due_to"), 0)
        urgency = 0
        if due and latest:
            urgency = 5 if due <= latest else max(0, 5 - (due - latest) // 5)
        open_fs.append((sc * 2 + urgency, fid, f))
    open_fs.sort(key=lambda x: -x[0])
    if open_fs:
        detail.append("【未兑现伏笔（按相关/紧急排序）】")
        for _sc, fid, f in open_fs[:8]:
            span = ""
            if f.get("due_from") or f.get("due_to"):
                span = f"（计划第{f.get('due_from') or '?'}–{f.get('due_to') or '?'}章兑现）"
            detail.append(f"- [{fid}] {f.get('description') or '（无描述）'}{span}")

    if st["timeline"]:
        detail.append("【最近事件】")
        for it in st["timeline"][-5:]:
            detail.append(f"- 第{it['chapter']}章 {it.get('time','')} {it.get('event','')[:120]}")

    detail_text = "\n".join(detail)
    detail_text = clip_text(detail_text, focus_limit)

    # 压缩概览：让 LLM 知道还有谁/多少伏笔，但不占用正文预算
    overview = [f"【全局概览】人物 {len(st['characters'])} 名 / 地点 {len(st['locations'])} 处 / "
                f"未兑现伏笔 {sum(1 for f in st['foreshadowing'].values() if f.get('status') == 'open')} 条 / "
                f"已写至第{latest}章"]
    names = [n for _s, n, _c in scored[:20]]
    if names:
        overview.append("- 其他人物：" + "、".join(names))
    items = sorted(st["chapter_summaries"].items(), key=lambda kv: _int(kv[0]))
    for key, sm in items[-2:]:
        if sm.get("summary"):
            overview.append(f"- 第{key}章摘要：{sm['summary'][:180]}")
    overview_text = "\n".join(overview)
    remaining = max(0, limit - len(detail_text) - 1)
    if remaining > 100:
        overview_text = clip_text(overview_text, remaining)
        return detail_text + "\n" + overview_text
    return clip_text(detail_text, limit)


# ---------- 旧项目迁移 ----------

def _read(pdir: str, name: str, cap: int = 200000) -> str:
    try:
        return open(os.path.join(pdir, name), encoding="utf-8").read()[:cap]
    except OSError:
        return ""


def _summary_from_text(text: str) -> str:
    """优先取『本章概要』，否则取正文第一个有意义的段落（不编造）。"""
    if not text:
        return ""
    m = re.search(r"##\s*本章概要\s*\n(.*?)(?=\n##\s|\Z)", text, re.S)
    if m:
        body = re.sub(r"[#>*`]", "", m.group(1)).strip()
        if body:
            return body[:600]
    lines = []
    for ln in text.splitlines():
        s = ln.strip().lstrip("#").strip()
        if not s or s.startswith(("本章概要", "章首引子", "章节备注")):
            continue
        lines.append(s)
        if sum(len(x) for x in lines) >= 300:
            break
    body = " ".join(lines)
    if len(body) > 300:
        cut = max(body.rfind("。", 0, 300), body.rfind("！", 0, 300), body.rfind("？", 0, 300))
        body = body[:cut + 1] if cut > 80 else body[:300]
    return body[:600]


def _parse_clue_ledger(text: str) -> dict:
    """尽力解析 10-线索账本 Markdown 表格；解析不出返回空（不阻断迁移）。"""
    out = {}
    for line in (text or "").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [re.sub(r"[*`]", "", c).strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0]:
            continue
        if cells[0].startswith(("ID", "---", "线索", "编号")) or set(cells[0]) <= {"-", " "}:
            continue
        fid = cells[0][:40]
        desc = cells[2] if len(cells) > 2 and cells[2] else (cells[1] if len(cells) > 1 else "")
        nums = [int(x) for x in re.findall(r"\d+", " ".join(cells[1:]))]
        due = int(re.findall(r"\d+", cells[5])[0]) if len(cells) > 5 and re.findall(r"\d+", cells[5]) else (
            nums[1] if len(nums) > 1 else (nums[0] if nums else 0))
        status = _norm_status(cells[-1]) if cells else "open"
        out[fid] = _norm_foreshadow({
            "description": desc[:300], "setup_chapter": nums[0] if nums else 0,
            "due_from": due, "due_to": due, "status": status,
        })
    return out


def rebuild_from_project(pdir: str, plan: dict, chapter_texts=None) -> dict:
    """从旧项目文件尽力重建权威状态；解析失败也返回可用空状态并落盘。"""
    st = empty_state()
    plan = plan if isinstance(plan, dict) else {}
    chapters = plan.get("chapters") if isinstance(plan.get("chapters"), list) else []

    chars_text = _read(pdir, "00-人物档案.md")
    names = []
    for m in re.finditer(r"(?:^|\n)\s*#{1,6}\s*人物[:：]\s*([^\n，,。:：]{1,12})", chars_text):
        names.append(m.group(1).strip())
    for m in re.finditer(r"主角[:：]\s*\**([^*\n，,。;；]{1,10})", chars_text):
        names.append(m.group(1).strip())
    for name in _uniq(names)[:30]:
        st["characters"][name] = _norm_character({"status": "已登记"}, 0)

    st["foreshadowing"].update(_parse_clue_ledger(_read(pdir, "10-线索账本.md")))

    texts = {}
    if isinstance(chapter_texts, dict):
        texts = {_int(k): v for k, v in chapter_texts.items() if _int(k) > 0}
    elif isinstance(chapter_texts, (list, tuple)):
        for i, t in enumerate(chapter_texts, 1):
            texts[i] = t
    total_words = 0
    last_ch = 0
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        n = _int(ch.get("chapterNumber"), 0)
        if n <= 0:
            continue
        text = texts.get(n)
        if text is None:
            fp = ch.get("filePath")
            if isinstance(fp, str) and fp:
                text = _read(pdir, fp, 200000)
        if not text and ch.get("status") not in ("completed", "done"):
            continue
        summary = _summary_from_text(text or "")
        words = _int(ch.get("wordCount"), 0) or len(re.findall(r"[\u4e00-\u9fff]", text or ""))
        if summary or words:
            st["chapter_summaries"][str(n)] = _norm_summary({
                "summary": summary, "beats": [], "state_changed": False})
            st["timeline"].append({"chapter": n, "time": "", "event": summary[:120],
                                   "location": "", "characters": []})
            total_words += words
            last_ch = max(last_ch, n)
    st["timeline"].sort(key=lambda x: x["chapter"])
    st["metrics"].update({"words": total_words, "chapters": len(st["chapter_summaries"]),
                          "last_chapter": last_ch, "updated_at": _now()})
    try:
        save(pdir, st)
    except Exception:
        pass
    return st


def migrate_from_markdown(pdir: str) -> dict:
    """旧项目迁移入口：读 02-写作计划 + 00/10 Markdown，重建状态库。"""
    plan = _read_json(_path(pdir, "02-写作计划.json"), {})
    return rebuild_from_project(pdir, plan if isinstance(plan, dict) else {})
