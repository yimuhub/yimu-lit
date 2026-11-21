"""码字统计 + 灵感速记 HTTP 接口（v5.4.0）。

用 FastAPI APIRouter 收口，**不改 app.py 的既有内容**——主会话只需在 app.py 里加两行挂载：

    from bot import webstats
    app.include_router(webstats.router)

接口契约（与 static/v2/js/views/stats.js、views/welcome.js、core/writestats-client.js 对齐）：
  GET    /api/writestats?days=30[&month=YYYY-MM]   日明细 + 汇总 + 连续天数 + 目标 + 月视图
  POST   /api/writestats/session                   幂等覆盖当日「手写会话」槽位
  POST   /api/writestats/commit                    会话并入 manual（幂等，防崩溃重放双记）
  POST   /api/writestats/baseline                  AI 插字入账：并入当日 ai 槽 + 抬升章节基线
  POST   /api/writestats/goals                     设置手写/AI 日目标（月目标=日目标×当月天数）
  GET    /api/inspirations?limit=50                灵感列表（置顶优先，创建时间倒序）
  POST   /api/inspirations                         新增灵感（#标签 提取，300 字上限）
  DELETE /api/inspirations/{id}                    删除灵感
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import inspirations, writestats

router = APIRouter()


def _payload(payload: dict) -> dict:
    return payload if isinstance(payload, dict) else {}


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=str(message))


# ---------------------------------------------------------------- 码字统计

@router.get("/api/writestats")
def api_writestats(days: int = 30, month: str = ""):
    """统计总览：days 控制趋势窗口（1-400），month 控制日历月视图（缺省当月）。"""
    try:
        return writestats.report(days, month=month or None)
    except Exception as err:  # 统计是附加能力，读挂了给出可读错误而不是 500 堆栈
        raise _bad(f"统计读取失败：{err}") from err


@router.post("/api/writestats/session")
def api_writestats_session(payload: dict):
    """幂等覆盖写当日「手写会话」槽位。body: {date?, key, session, chars}"""
    p = _payload(payload)
    key = str(p.get("key") or "").strip()
    session = str(p.get("session") or "").strip()
    if not key:
        raise _bad("缺少作品 key")
    if not session:
        raise _bad("缺少会话 id")
    try:
        return writestats.put_session(
            p.get("date"), key, session, p.get("chars"),
        )
    except Exception as err:
        raise _bad(f"会话写入失败：{err}") from err


@router.post("/api/writestats/commit")
def api_writestats_commit(payload: dict):
    """会话定稿：槽位并入当日 manual。body: {date?, key, session}"""
    p = _payload(payload)
    key = str(p.get("key") or "").strip()
    session = str(p.get("session") or "").strip()
    if not key:
        raise _bad("缺少作品 key")
    if not session:
        raise _bad("缺少会话 id")
    try:
        return writestats.commit_session(p.get("date"), key, session)
    except Exception as err:
        raise _bad(f"会话并入失败：{err}") from err


@router.post("/api/writestats/baseline")
def api_writestats_baseline(payload: dict):
    """AI 插字入账：并入当日 ai 槽 + 抬升章节基线（手写/AI 不双记）。
    body: {date?, key, chapter, chars}（或 {count: 编辑器当前总字数} 走基线差口径）"""
    p = _payload(payload)
    key = str(p.get("key") or "").strip()
    if not key:
        raise _bad("缺少作品 key")
    try:
        count = p.get("count")
        if count is not None:
            base = writestats.raise_baseline(key, p.get("chapter"), count=count)
            delta = int(base.get("delta") or 0)
            if delta > 0:
                writestats.add_ai(p.get("date"), key, delta)
            return {"ok": True, "chars": delta, "base": base.get("base")}
        return writestats.record_ai_insert(
            p.get("date"), key, p.get("chapter"), p.get("chars"),
        )
    except Exception as err:
        raise _bad(f"AI 入账失败：{err}") from err


@router.post("/api/writestats/goals")
def api_writestats_goals(payload: dict):
    """设置手写/AI 日目标。body: {manual?, ai?}（0 表示不设目标）"""
    p = _payload(payload)
    if "manual" not in p and "ai" not in p:
        raise _bad("至少提供 manual 或 ai 之一")
    try:
        return writestats.set_goals(p.get("manual"), p.get("ai"))
    except Exception as err:
        raise _bad(f"目标保存失败：{err}") from err


# ---------------------------------------------------------------- 灵感速记

@router.get("/api/inspirations")
def api_inspirations(limit: int = 50):
    return {"ok": True, "items": inspirations.list_items(limit), "max_len": inspirations.MAX_LEN}


@router.post("/api/inspirations")
def api_inspirations_add(payload: dict):
    """新增灵感。body: {text}（#标签 语法自动提取；300 字上限）"""
    p = _payload(payload)
    try:
        item = inspirations.add(p.get("text"))
        return {"ok": True, "item": item}
    except ValueError as err:
        raise _bad(str(err)) from err
    except Exception as err:
        raise _bad(f"灵感保存失败：{err}") from err


@router.delete("/api/inspirations/{insp_id}")
def api_inspirations_delete(insp_id: str):
    ok = inspirations.delete(insp_id)
    if not ok:
        raise HTTPException(status_code=404, detail="灵感不存在或已删除")
    return {"ok": True}
