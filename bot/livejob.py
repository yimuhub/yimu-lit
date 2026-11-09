"""实时作业看板（v4.33.1）：让「点击 → 立刻有反馈」成为后端契约。

用户实拍：点「继续」后界面一动不动，等 30 秒以上才看到第一条日志，中途完全
不知道系统是在工作还是卡死。根因不在引擎，而在**启动瞬间没有事实**：

* 后端先做同步准备（读素材、生成创作圣经、扫描几十集正文），HTTP 响应迟迟不回；
* 真正开写后日志只写文件，前端日志带用「数组长度」判断是否有新行，永远不更新；
* 小说 / 剧本 / 诗词 / 论文……每个文体的续跑入口各自返回不同字段，
  前端无法用一套 UI 告诉用户「谁在工作」。

本模块只做一件事：给每个正在跑的后台作业一份**可查询的、带时间戳的登记**。
任何入口在起线程之前先 ``begin()``，引擎在阶段切换时 ``update()``，收尾 ``finish()``。
前端轮询 ``/api/job``，点下去 200ms 内就能显示「谁在做什么、已经做了多久」，
即使模型 2 分钟才吐第一个字，用户也看得见它在动。

与 ``bot/jobs.py`` 的分工：那个是持久化的批量任务账本（按 uuid 索引、落 jobs.json）；
这里是**当前正在跑什么**的实时快照（按作品 key 索引、纯内存、进程重启即清空）。
历史进度仍以项目计划与运行日志为准，不需要持久化。
"""
from __future__ import annotations

import os
import threading
import time

_LOCK = threading.RLock()
_ACTIVE: dict[str, dict] = {}
# 完成后保留一小段时间，供前端最后一次轮询拿到「已完成 / 失败原因」再自行收起
_FINISHED_TTL = 180


def _norm(key) -> str:
    return os.path.basename(str(key or "").strip())


def begin(key, *, kind: str = "resume", worker: str = "", worker_label: str = "",
          detail: str = "", phase: str = "starting", total: int = 0,
          done: int = 0) -> dict:
    """登记一个刚启动的作业；重复调用刷新时间与说明，不新开一条。"""
    k = _norm(key)
    if not k:
        return {}
    now = time.time()
    with _LOCK:
        row = _ACTIVE.get(k) or {}
        if row.get("finished"):
            row = {}          # 上一轮已结束：新作业重新计时，不继承旧进度
        row.update({
            "key": k,
            "kind": kind or row.get("kind") or "resume",
            "worker": worker or row.get("worker") or "",
            "worker_label": worker_label or row.get("worker_label") or "创作引擎",
            "detail": detail or row.get("detail") or "正在启动…",
            "phase": phase or row.get("phase") or "starting",
            "total": int(total or row.get("total") or 0),
            "done": int(done or row.get("done") or 0),
            "started_at": row.get("started_at") or now,
            "updated_at": now,
            "finished": False,
            "ok": None,
            "error": "",
        })
        _ACTIVE[k] = row
        return dict(row)


def update(key, **fields) -> dict:
    """更新阶段 / 进度。作业不存在时静默返回空（不代表失败）。"""
    k = _norm(key)
    if not k:
        return {}
    with _LOCK:
        row = _ACTIVE.get(k)
        if not row:
            return {}
        for name in ("detail", "phase", "worker", "worker_label"):
            if fields.get(name):
                row[name] = str(fields[name])
        for name in ("total", "done"):
            if fields.get(name) is not None:
                try:
                    row[name] = int(fields[name])
                except (TypeError, ValueError):
                    pass
        row["updated_at"] = time.time()
        return dict(row)


def finish(key, ok: bool = True, detail: str = "", error: str = "") -> dict:
    """标记作业结束。保留记录一段时间，让前端能读到结论。"""
    k = _norm(key)
    if not k:
        return {}
    now = time.time()
    with _LOCK:
        row = _ACTIVE.get(k)
        if not row:
            return {}
        row.update({
            "finished": True,
            "ok": bool(ok),
            "phase": "done" if ok else "failed",
            "detail": detail or row.get("detail") or ("已完成" if ok else "未完成"),
            "error": "" if ok else (error or detail or "未完成"),
            "updated_at": now,
            "ended_at": now,
        })
        return dict(row)


def get(key) -> dict:
    """当前作业快照；没有则返回空 dict。过期记录顺手清理。"""
    k = _norm(key)
    if not k:
        return {}
    now = time.time()
    with _LOCK:
        row = _ACTIVE.get(k)
        if not row:
            return {}
        if row.get("finished") and now - float(row.get("ended_at") or 0) > _FINISHED_TTL:
            _ACTIVE.pop(k, None)
            return {}
        out = dict(row)
    out["elapsed"] = round(max(0.0, now - float(out.get("started_at") or now)), 1)
    return out


def clear(key) -> None:
    with _LOCK:
        _ACTIVE.pop(_norm(key), None)


def is_active(key) -> bool:
    row = get(key)
    return bool(row) and not row.get("finished")


def snapshot() -> list[dict]:
    """全部未结束作业（诊断用）。"""
    with _LOCK:
        keys = [k for k, r in _ACTIVE.items() if not r.get("finished")]
    return [get(k) for k in keys]


def reset_for_tests() -> None:
    with _LOCK:
        _ACTIVE.clear()
