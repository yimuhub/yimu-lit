"""后台任务表：把“丢线程 + 内存 dict”升级为可查询、可持久化的 job。

用途：批量分发、放行门禁等跨分钟任务需要在前端/运维侧看到「排队/运行/完成/失败」，
进程重启后也应留下最近记录。任务本身仍在进程内线程跑（单机工具不引入外部队列），
这里只负责状态与结果的可观测性。
"""
from __future__ import annotations

import datetime
import os
import threading
import uuid

from .safeio import atomic_write_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS_FILE = os.path.join(ROOT, "jobs.json")
_MAX_JOBS = 200
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    import json
    try:
        d = json.load(open(JOBS_FILE, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _trim(d: dict) -> dict:
    if len(d) <= _MAX_JOBS:
        return d
    keep = sorted(d.values(), key=lambda j: j.get("created_at", ""))[-_MAX_JOBS:]
    return {j["id"]: j for j in keep}


def create(kind: str, key: str = "", title: str = "", **extra) -> dict:
    jid = f"{datetime.datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
    job = {"id": jid, "kind": kind, "key": key, "title": title,
           "status": "queued", "created_at": _now(), "updated_at": _now()}
    job.update(extra)
    with _LOCK:
        d = _load()
        d[jid] = job
        atomic_write_json(JOBS_FILE, _trim(d))
    return job


def update(jid: str, **fields) -> dict | None:
    with _LOCK:
        d = _load()
        job = d.get(jid)
        if not job:
            return None
        job.update(fields)
        job["updated_at"] = _now()
        atomic_write_json(JOBS_FILE, d)
        return job


def get(jid: str) -> dict | None:
    with _LOCK:
        return _load().get(jid)


def list_jobs(limit: int = 50, key: str = "", kind: str = "") -> list[dict]:
    with _LOCK:
        items = list(_load().values())
    if key:
        items = [j for j in items if j.get("key") == key]
    if kind:
        items = [j for j in items if j.get("kind") == kind]
    items.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return items[:max(1, min(200, int(limit or 50)))]


def run(kind: str, fn, key: str = "", title: str = "", **extra) -> dict:
    """登记并后台执行；立即返回 job 记录（含 id）。fn 的返回值写入 result。"""
    job = create(kind, key=key, title=title, **extra)
    jid = job["id"]

    def _runner():
        update(jid, status="running", started_at=_now())
        try:
            res = fn()
            update(jid, status="done", result=res, finished_at=_now())
        except Exception as e:
            update(jid, status="failed", error=str(e)[:500], finished_at=_now())

    threading.Thread(target=_runner, daemon=True).start()
    return job
