"""v5.2.0 webai 路由（AI 辅助写作的后端面）：FastAPI APIRouter，由 app.py 两行挂载。

    from bot.webai import router as webai_router
    app.include_router(webai_router)

端点：
  POST /api/ghost            幽灵字短补全（JSON，一次性；服务端 8s 间隔门 → 429 + 冷却秒数）
  POST /api/quickwrite       快捷续写（SSE：start → delta* → done{text}；失败 error + done{ok:false}）
  GET  /api/ai-ledger        AI 调用账本：聚合（总次数/成功/失败/token 求和）+ 场景/状态/日期/
                             关键词筛选 + 明细分页（读 llm_calls.jsonl 流式解析）
  GET/POST /api/sensitive-words  敏感词词库读写（data/sensitive-words.txt，每行一词，# 注释）

约定：
  · 复用 bot/llm.py 现有封装与记账（scene 中文场景标签），不新增网络层；
  · 产出清洗走 purity.strip_fragment + selection_ai.clean_result（纯净域口径）；
  · 明细只含账本既有字段（时间/场景/模型/状态/token/耗时/错误摘要）——llm.py
    字段白名单绝不落正文，账本面板不展示输入输出原文。
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import ghost, llm
from .paths import DATA_ROOT
from .safeio import atomic_write_text

router = APIRouter()

_LEDGER_PAGE_DEFAULT = 20
_LEDGER_PAGE_MAX = 100

SENSITIVE_PATH = os.path.join(DATA_ROOT, "data", "sensitive-words.txt")

# 内置基础词表（与仓库 data/sensitive-words.txt 同源；文件缺失时自动播种）。
# 检测本身是纯前端字符串匹配（bench.js 状态栏徽标），不调 LLM；作者可在文件里自由增删。
_DEFAULT_SENSITIVE_TEXT = """\
# 一幕·YIMU 神笔 · 敏感词基础词库（v5.2.0）
# 每行一词；# 开头的行为注释；写作台状态栏徽标按本地字符串匹配计数，不调用任何模型。
# 用途：网文平台过审自查。词库完全在本机使用与编辑，不会上传。
枪械买卖
毒品交易
制毒方法
开锁教程
炸弹制作
自杀教程
赌博网站
网络赌博
色情服务
援交
裸聊
嫖娼
卖淫
性奴
强奸
轮奸
幼女
乱伦
血腥虐杀
碎尸
肢解
剥皮
虐童
邪教
传销组织
诈骗话术
洗钱
高利贷逼债
恐怖袭击
劫机
爆炸袭击
投毒
灭门
"""


def _sse(event: str, data: dict) -> str:
    """SSE 帧（与 app.py /api/chapter/selection-ai 同格式：event + data 两行 + 空行）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _clamp_text(text: str, cap: int = ghost.MAX_TEXT_CHARS) -> str:
    """提交正文钳制：超长只保留离光标最近的 cap 个字符（上下文质量不受影响）。"""
    t = (text or "").replace("\r\n", "\n")
    return t[-cap:] if len(t) > cap else t


def _chapter_no_of(payload: dict):
    n = payload.get("n")
    return n if isinstance(n, int) and n > 0 else None


# ---------------------------------------------------------------- 幽灵字（JSON 短补全）

@router.post("/api/ghost")
def api_ghost(payload: dict):
    """幽灵字短补全。入参 {key, text, title?, summary?, mode?, n?}；
    出参 {ok, text, mode, label, chars, temperature}；请求过快 → 429 {error, cooldown}。"""
    payload = payload or {}
    key = str(payload.get("key") or "")
    try:
        ghost.check_rate(key)
    except ghost.GhostRateLimited as e:
        return JSONResponse({"error": str(e), "cooldown": e.cooldown}, status_code=429)
    text = _clamp_text(str(payload.get("text") or ""))
    try:
        r = ghost.run(text, key=key,
                      title=str(payload.get("title") or ""),
                      summary=str(payload.get("summary") or ""),
                      mode=payload.get("mode") or None,
                      chapter_no=_chapter_no_of(payload))
    except llm.LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:   # 幽灵字是附加能力：任何异常都转成可读错误，不 500 中断写作台
        return JSONResponse({"error": f"幽灵字调用失败：{e}"}, status_code=502)
    return {"ok": True, **r}


# ---------------------------------------------------------------- 快捷续写（SSE 流式）

@router.post("/api/quickwrite")
async def api_quickwrite(request: Request):
    """快捷续写（SSE 流式逐字）：入参 {key, text, title?, summary?, n?}。
    帧：start → delta*(增量原文) → done{ok, text=清洗后全文}；失败 error + done{ok:false}。
    清洗只能对全文做——前端先按增量渲染，done 帧以后端清洗文本对账（不一致就地替换）。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
    payload = payload or {}
    key = str(payload.get("key") or "")
    text = _clamp_text(str(payload.get("text") or ""))
    title = str(payload.get("title") or "")
    summary = str(payload.get("summary") or "")
    chapter_no = _chapter_no_of(payload)

    async def gen():
        t0 = time.time()
        yield _sse("start", {"ts": int(t0), "key": key})
        q: queue.Queue = queue.Queue()
        box: list = []

        def _work():
            try:
                full = ghost.run_stream(text, key=key, title=title, summary=summary,
                                        chapter_no=chapter_no,
                                        on_delta=lambda piece: q.put(("delta", piece)))
                box.append((True, full))
                q.put(("done", None))
            except BaseException as e:   # LLMError/入参错误都进 error 帧，绝不 500 中断流
                box.append((False, str(e)))
                q.put(("error", None))

        threading.Thread(target=_work, daemon=True).start()
        # 逐帧转发：线程队列 → SSE（与划词 AI 路由同款接线）
        while True:
            kind, _val = await asyncio.to_thread(q.get)
            if kind == "delta":
                yield _sse("delta", {"text": _val})
            elif kind == "done":
                ok, full = box[0] if box else (False, "续写结果为空")
                if ok:
                    yield _sse("done", {"ok": True, "text": full,
                                        "chars": len(full),
                                        "elapsed": round(time.time() - t0, 2)})
                else:
                    yield _sse("error", {"error": full})
                    yield _sse("done", {"ok": False, "error": full,
                                        "elapsed": round(time.time() - t0, 2)})
                return
            else:  # error
                err = box[0][1] if box else "续写调用失败"
                yield _sse("error", {"error": err})
                yield _sse("done", {"ok": False, "error": err,
                                    "elapsed": round(time.time() - t0, 2)})
                return

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- AI 调用账本

def _ledger_path() -> str:
    """账本文件单一来源：bot/llm.py 的 _TRACE_FILE（DATA_ROOT/llm_calls.jsonl）。"""
    return llm._TRACE_FILE


def _ledger_records():
    """流式解析账本（append 序 = 时间序）；坏行跳过，绝不因单行损坏丢整本账。"""
    try:
        with open(_ledger_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def _match(rec: dict, *, scene: str, status: str, date_from: str, date_to: str, q: str) -> bool:
    """账本筛选：场景精确 / 状态 / 日期（YYYY-MM-DD 闭区间）/ 关键词（全字段子串）。"""
    if scene and str(rec.get("scene") or "") != scene:
        return False
    if status == "ok" and not rec.get("ok"):
        return False
    if status == "failed" and rec.get("ok"):
        return False
    ts = str(rec.get("ts") or "")
    day = ts[:10]
    if date_from and day and day < date_from:
        return False
    if date_to and day and day > date_to:
        return False
    if q:
        blob = json.dumps(rec, ensure_ascii=False).lower()
        if q.lower() not in blob:
            return False
    return True


@router.get("/api/ai-ledger")
def api_ai_ledger(scene: str = "", status: str = "", date_from: str = "",
                  date_to: str = "", q: str = "", page: int = 1, page_size: int = _LEDGER_PAGE_DEFAULT):
    """AI 调用账本：聚合 + 筛选 + 明细分页。
    聚合口径（对筛选后的全量记录）：total=总次数，ok=成功，failed=失败，
    tokens=total_tokens 求和（缺 token 的记录按 0 计，不做字数估算）；
    by_scene=按场景标签计数；scenes=全量去重场景（供筛选下拉）。"""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(_LEDGER_PAGE_MAX, max(1, int(page_size)))
    except (TypeError, ValueError):
        page_size = _LEDGER_PAGE_DEFAULT

    matched: list[dict] = []
    stats = {"total": 0, "ok": 0, "failed": 0, "tokens": 0,
             "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0}
    by_scene: dict[str, int] = {}
    scenes: list[str] = []
    for rec in _ledger_records():
        s = str(rec.get("scene") or "")
        if s and s not in scenes:
            scenes.append(s)
        if not _match(rec, scene=scene, status=status, date_from=date_from,
                      date_to=date_to, q=q):
            continue
        matched.append(rec)
        stats["total"] += 1
        if rec.get("ok"):
            stats["ok"] += 1
        else:
            stats["failed"] += 1
        try:
            stats["tokens"] += int(rec.get("total_tokens") or 0)
            stats["prompt_tokens"] += int(rec.get("prompt_tokens") or 0)
            stats["completion_tokens"] += int(rec.get("completion_tokens") or 0)
            stats["latency_ms"] += int(rec.get("latency_ms") or 0)
        except (TypeError, ValueError):
            pass
        by_scene[s or "（未标注）"] = by_scene.get(s or "（未标注）", 0) + 1

    # 明细：append 序 = 时间正序，倒序输出（最新在前）
    total = len(matched)
    start = (page - 1) * page_size
    items = list(reversed(matched))[start:start + page_size]
    return {
        "ok": True,
        "stats": stats,
        "by_scene": by_scene,
        "scenes": sorted(scenes),
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


# ---------------------------------------------------------------- 敏感词词库

def _parse_sensitive(text: str) -> list[str]:
    """词库解析：每行一词，# 注释行与空行跳过；去重保序。"""
    out, seen = [], set()
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _load_sensitive(create: bool = True) -> list[str]:
    """读词库；文件缺失时用内置基础词表播种（桌面首启开箱即用）。"""
    try:
        with open(SENSITIVE_PATH, encoding="utf-8") as f:
            text = f.read()
        return _parse_sensitive(text)
    except OSError:
        pass
    if create:
        try:
            os.makedirs(os.path.dirname(SENSITIVE_PATH), exist_ok=True)
            atomic_write_text(SENSITIVE_PATH, _DEFAULT_SENSITIVE_TEXT)
            return _parse_sensitive(_DEFAULT_SENSITIVE_TEXT)
        except Exception:
            return _parse_sensitive(_DEFAULT_SENSITIVE_TEXT)
    return []


@router.get("/api/sensitive-words")
def api_sensitive_words_get():
    """词库读取：{ok, items, count, path}。文件缺失自动播种内置基础词表。"""
    items = _load_sensitive(create=True)
    return {"ok": True, "items": items, "count": len(items), "path": SENSITIVE_PATH}


@router.post("/api/sensitive-words")
def api_sensitive_words_save(payload: dict):
    """词库保存：{text}（整份文件内容）或 {words: [..]}（词数组）。
    统一按「每行一词 + # 注释」规范化后原子写盘；返回 {ok, items, count}。"""
    payload = payload or {}
    if isinstance(payload.get("words"), list):
        text = "\n".join(str(w).strip() for w in payload["words"] if str(w).strip())
    else:
        text = str(payload.get("text") or "")
    items = _parse_sensitive(text)
    if not items:
        return JSONResponse({"error": "词库不能为空（至少保留一个词）"}, status_code=400)
    try:
        os.makedirs(os.path.dirname(SENSITIVE_PATH), exist_ok=True)
        atomic_write_text(SENSITIVE_PATH, "\n".join(items) + "\n")
    except Exception as e:
        return JSONResponse({"error": f"词库写盘失败：{e}"}, status_code=500)
    return {"ok": True, "items": items, "count": len(items), "path": SENSITIVE_PATH}
