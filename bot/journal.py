"""任务档案（Task Journal）——断点续跑的「存档」，也是创作 Agent 的记忆底座。

## 为什么需要它

「继续」这件事以前是靠推断的：看计划里哪些章没写完，就当用户要接着写。
一旦中断发生在**章内**（写完 3 段被杀）或**另一类任务**里（正在批量重写第 12 章时暂停），
推断就会答错：要么把半章重写一遍（用户感知为「重新开始」），要么把重写任务丢在一边
去写新章。所以必须有一份**显式存档**：上次在做什么、做到哪、停在哪个阶段。

## 存什么

`<项目>/任务档案.json`：
```json
{
  "active": {
    "id": "t-20261102-103500-8f2a",
    "kind": "writing",            // writing / rewrite / score / deslop / export
    "title": "继续写《入赘三年》",
    "params": {"batch_size": "5"},
    "total": 100, "done": 40,     // 计数类进度
    "current": 41,                // 当前章号
    "stage": "writing",           // planning|writing|expanding|scoring|rewriting|verifying
    "note": "",
    "started_at": "...", "updated_at": "..."
  },
  "history": [ ...最近 20 条已结束任务... ],
  "memory":  [ ...最近 40 条事件/指令，供理解层回看... ]
}
```

## 纪律

- 只记**状态**，不记正文（正文在章节文件与 `.draft/` 草稿里）。
- 写盘用原子写；任何异常都不许把已有存档写坏（读写都 try/except，最差退化为空）。
- `active` 只允许有一条：新任务 `begin` 时把旧的挪进历史（标记 superseded）。
"""
from __future__ import annotations

import datetime
import json
import os
import random
import threading

from .safeio import atomic_write_json

FILE = "任务档案.json"
_MEM_MAX = 40
_HIST_MAX = 20
_LOCK = threading.Lock()

# 任务种类 → 中文名（回复与前端显示用）
KIND_LABEL = {
    "writing": "创作",
    "rewrite": "重写",
    "score": "评分",
    "deslop": "去味",
    "export": "导出",
    "plan": "规划",
}


def _path(pdir: str) -> str:
    return os.path.join(pdir, FILE)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load(pdir: str) -> dict:
    try:
        with open(_path(pdir), encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("bad journal")
    except Exception:
        d = {}
    d.setdefault("active", None)
    d.setdefault("history", [])
    d.setdefault("memory", [])
    d.setdefault("fails", {})
    return d


def _save(pdir: str, d: dict) -> None:
    try:
        atomic_write_json(_path(pdir), d)
    except Exception:
        pass


def _new_id() -> str:
    return "t-%s-%04x" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"),
                          random.randint(0, 0xFFFF))


# ---------------------------------------------------------------- 读写

def begin(pdir: str, kind: str, title: str, params: dict | None = None,
          total: int | None = None, note: str = "") -> dict:
    """开一个新任务；若已有未结束任务，先归档为 superseded。返回任务字典。"""
    with _LOCK:
        d = _load(pdir)
        old = d.get("active")
        if isinstance(old, dict) and old.get("id"):
            old.update({"status": "superseded", "updated_at": _now()})
            d["history"] = ([old] + d.get("history", []))[:_HIST_MAX]
        task = {
            "id": _new_id(),
            "kind": str(kind or "writing"),
            "title": str(title or "")[:80],
            "params": dict(params or {}),
            "total": int(total) if isinstance(total, int) else None,
            "done": 0,
            "current": None,
            "stage": "",
            "note": str(note or "")[:200],
            "status": "running",
            "started_at": _now(),
            "updated_at": _now(),
        }
        d["active"] = task
        _save(pdir, d)
        return task


def update(pdir: str, **fields) -> dict | None:
    """更新当前任务的字段（不存在则忽略）。"""
    with _LOCK:
        d = _load(pdir)
        t = d.get("active")
        if not isinstance(t, dict):
            return None
        for k, v in fields.items():
            if k in ("id", "started_at"):
                continue
            t[k] = v
        t["updated_at"] = _now()
        _save(pdir, d)
        return t


def progress(pdir: str, done: int | None = None, current: int | None = None,
             stage: str | None = None, total: int | None = None) -> dict | None:
    """记进度：已完成数量 / 当前章号 / 阶段。"""
    f = {}
    if done is not None:
        f["done"] = int(done)
    if current is not None:
        f["current"] = int(current)
    if stage:
        f["stage"] = str(stage)
    if total is not None:
        f["total"] = int(total)
    return update(pdir, **f) if f else current_task(pdir)


def set_params(pdir: str, **kv) -> dict | None:
    """把几个键合并进当前任务的 params（不覆盖其它键）。

    不能直接 update(params=...) ——那会把 mark_chapter_done 写进去的
    done_chapters 一起冲掉，续跑时又变成从头做。
    """
    if not kv:
        return current_task(pdir)
    with _LOCK:
        d = _load(pdir)
        t = d.get("active")
        if not isinstance(t, dict):
            return None
        params = t.get("params") if isinstance(t.get("params"), dict) else {}
        params.update(kv)
        t["params"] = params
        t["updated_at"] = _now()
        _save(pdir, d)
        return t


# ---------------------------------------------------------------- 失败计数
# 放在档案根上，不放在 active 任务里：每次「继续」都会 begin 一个新任务，
# 计数若挂在任务上会每次清零，同一章就能无限重试、这本书永远收不了尾。

def bump_fail(pdir: str, n) -> int:
    """某一章又失败了一次，返回累计次数。"""
    try:
        k = str(int(n))
    except (TypeError, ValueError):
        return 0
    with _LOCK:
        d = _load(pdir)
        fails = d.get("fails") if isinstance(d.get("fails"), dict) else {}
        fails[k] = int(fails.get(k) or 0) + 1
        if len(fails) > 500:
            fails = dict(list(fails.items())[-500:])
        d["fails"] = fails
        _save(pdir, d)
        return fails[k]


def fail_count(pdir: str, n) -> int:
    d = _load(pdir)
    fails = d.get("fails") if isinstance(d.get("fails"), dict) else {}
    try:
        return int(fails.get(str(int(n))) or 0)
    except (TypeError, ValueError):
        return 0


def clear_fail(pdir: str, n=None) -> None:
    """某章做成了就清掉它的失败记录（单章传 n；传 None 清全部）。"""
    with _LOCK:
        d = _load(pdir)
        if n is None:
            d["fails"] = {}
        else:
            fails = d.get("fails") if isinstance(d.get("fails"), dict) else {}
            try:
                fails.pop(str(int(n)), None)
            except (TypeError, ValueError):
                pass
            d["fails"] = fails
        _save(pdir, d)


def mark_chapter_done(pdir: str, n, done: int | None = None,
                      stage: str | None = None, total: int | None = None) -> dict | None:
    """按**章号**记完成，而不是按完成个数记。

    为什么不能只记 done 计数：并行重写是 3 个线程同时跑，第 7 章可能比第 5 章先完成。
    只记「已完成 3 章」的话，续跑时按 `todo[3:]` 切片会跳过没做完的章、重做已完成的章
    —— 用户看到的就是「又改了一遍」和「那章怎么没改」。
    所以把做完的章号列出来，续跑时取差集，顺序无关。
    """
    try:
        ni = int(n)
    except (TypeError, ValueError):
        return None
    with _LOCK:
        d = _load(pdir)
        t = d.get("active")
        if not isinstance(t, dict):
            return None
        params = t.get("params") if isinstance(t.get("params"), dict) else {}
        dc = []
        for x in params.get("done_chapters") or []:
            try:
                dc.append(int(x))
            except (TypeError, ValueError):
                continue
        if ni not in dc:
            dc.append(ni)
        params["done_chapters"] = dc
        t["params"] = params
        t["done"] = len(dc) if done is None else int(done)
        t["current"] = ni
        if stage:
            t["stage"] = str(stage)
        if total is not None:
            t["total"] = int(total)
        t["updated_at"] = _now()
        _save(pdir, d)
        return t


def finish(pdir: str, status: str = "done", note: str = "") -> dict | None:
    """结束当前任务（done/paused/aborted/failed），挪进历史。"""
    with _LOCK:
        d = _load(pdir)
        t = d.get("active")
        if not isinstance(t, dict):
            return None
        t["status"] = str(status or "done")
        t["updated_at"] = _now()
        if note:
            t["note"] = str(note)[:200]
        d["history"] = ([t] + d.get("history", []))[:_HIST_MAX]
        d["active"] = None
        _save(pdir, d)
        return t


def current_task(pdir: str) -> dict | None:
    """取当前未结束任务；已完成/已放弃的不算。"""
    d = _load(pdir)
    t = d.get("active")
    if isinstance(t, dict) and t.get("status") in ("running", "paused"):
        return t
    return None


def last_finished(pdir: str, kinds=("writing", "rewrite", "score")) -> dict | None:
    """最近一条已结束任务（「上次那个」的指代对象）。"""
    d = _load(pdir)
    for t in d.get("history", []):
        if isinstance(t, dict) and t.get("kind") in kinds:
            return t
    return None


# ---------------------------------------------------------------- 记忆

def remember(pdir: str, kind: str, text: str) -> None:
    """记一条事件/指令（供理解层回看「刚刚发生了什么」）。"""
    text = str(text or "").strip()
    if not text:
        return
    with _LOCK:
        d = _load(pdir)
        mem = d.get("memory") or []
        mem.append({"at": _now(), "kind": str(kind or "event"), "text": text[:200]})
        d["memory"] = mem[-_MEM_MAX:]
        _save(pdir, d)


def memory(pdir: str, n: int = 8) -> list:
    """最近 n 条记忆（时间正序）。"""
    d = _load(pdir)
    mem = d.get("memory") or []
    return mem[-n:]


def recall_text(pdir: str, n: int = 6) -> str:
    """把「当前任务 + 最近事件」压成一段给理解层看的中文。"""
    d = _load(pdir)
    lines = []
    t = d.get("active")
    if isinstance(t, dict) and t.get("status") in ("running", "paused"):
        label = KIND_LABEL.get(t.get("kind") or "", t.get("kind") or "")
        seg = [f"进行中的任务：{label}「{t.get('title') or ''}」"]
        if t.get("total"):
            seg.append(f"进度 {t.get('done') or 0}/{t.get('total')}")
        if t.get("current"):
            seg.append(f"当前第 {t.get('current')} 章")
        if t.get("stage"):
            seg.append(f"阶段 {t.get('stage')}")
        if t.get("note"):
            seg.append(f"备注 {t.get('note')}")
        lines.append("；".join(seg))
    else:
        lines.append("进行中的任务：无（上一件已结束）")
    last = last_finished(pdir)
    if isinstance(last, dict):
        label = KIND_LABEL.get(last.get("kind") or "", last.get("kind") or "")
        lines.append(f"最近一次结束的任务：{label}「{last.get('title') or ''}」"
                     f"（{last.get('status')}，{last.get('updated_at') or ''}）")
    mem = d.get("memory") or []
    if mem:
        for m in mem[-n:]:
            lines.append(f"- [{m.get('at', '')}] {m.get('kind', '')}：{m.get('text', '')}")
    return "\n".join(lines) if lines else "（无记忆）"


def summary(pdir: str) -> dict:
    """给前端/接口用的结构化摘要。"""
    d = _load(pdir)
    t = d.get("active")
    active = None
    if isinstance(t, dict) and t.get("status") in ("running", "paused"):
        active = {
            "kind": t.get("kind"),
            "label": KIND_LABEL.get(t.get("kind") or "", t.get("kind") or ""),
            "title": t.get("title"),
            "done": t.get("done") or 0,
            "total": t.get("total"),
            "current": t.get("current"),
            "stage": t.get("stage"),
            "status": t.get("status"),
            "updated_at": t.get("updated_at"),
        }
    return {"active": active, "memory": (d.get("memory") or [])[-10:]}


# ---------------------------------------------------------------- 启动对账

def reconcile(pdir: str, note: str = "进程退出导致中断（可点「继续」接着做）") -> bool:
    """把「仍标 running、其实早已没有线程」的任务改标为 paused。

    场景：上游断流、进程被杀、机器重启。进程退出时来不及写 paused，
    档案就永远停在 running——「继续」据此判断会以为任务还在跑而不肯接手，
    用户点一百次也动不了。启动时对账一次，把这类僵尸任务转成可续跑的 paused。
    """
    with _LOCK:
        d = _load(pdir)
        t = d.get("active")
        if not isinstance(t, dict) or t.get("status") != "running":
            return False
        t["status"] = "paused"
        t["note"] = str(note)[:200]
        t["updated_at"] = _now()
        d["active"] = t
        _save(pdir, d)
        return True


def reconcile_all(pdirs) -> int:
    """对一批项目做启动对账，返回改标的任务数。"""
    n = 0
    for p in pdirs or []:
        try:
            if reconcile(p):
                n += 1
        except Exception:
            continue
    return n
