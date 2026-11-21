"""拆书工作台 HTTP 接口（v5.4.0，docs/系统化升级方案 8.1）。

用 FastAPI APIRouter 收口，**不改 app.py**——主会话在认证中间件之后加两行挂载
（与 webai/webstats/weblib 同位置）：

    from bot import webbreakdown
    app.include_router(webbreakdown.router)

接口契约（与 static/v2/js/views/breakdown.js 对齐；拆书对象是外部文本，纯净域：
响应里的原文只用于工作台对照展示，入账本的手法卡不带原文片段——见 bot/breakdown.py）：
  POST   /api/breakdown/upload                     multipart（file + 可选 title）→ 切章并建项目
  POST   /api/breakdown/paste                      {title, text} → 切章并建项目
  GET    /api/breakdowns                           项目列表（摘要，不含正文）
  GET    /api/breakdown/{id}                       项目全量（章节段落 + analysis + 报告）
  GET    /api/breakdown/{id}/progress              进度轮询（轻量）
  POST   /api/breakdown/{id}/run                   启动逐章拆解后台任务（线程）
  POST   /api/breakdown/{id}/chapter/{n}/retry     重试单个失败章（同步单次调用）
  POST   /api/breakdown/{id}/report                全书报告（同步单次调用）
  POST   /api/breakdown/{id}/resplit               整体重切（清空章节状态与分析）
  POST   /api/breakdown/{id}/adopt-techniques      手法入账本 {indices:[...], projectKey?}
  DELETE /api/breakdown/{id}                       删除项目
"""
from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import breakdown, ingest

router = APIRouter()


def _bad(message: str, status: int = 400):
    return JSONResponse({"ok": False, "error": str(message), "detail": str(message)},
                        status_code=status)


def _preview_of(bd: dict) -> dict:
    """切章预览：卷/章数 + 前 12 章标题与字数（预览页展示用）。"""
    chapters = bd.get("chapters") or []
    return {"id": bd["id"], "title": bd.get("title"), "chars": bd.get("chars"),
            "chapterCount": len(chapters),
            "volumeCount": len(bd.get("volumes") or []),
            "volumes": bd.get("volumes") or [],
            "warnings": bd.get("warnings") or [],
            "preview": [{"no": c["no"], "num": c.get("num"), "title": c.get("title"),
                         "volume": c.get("volume"), "chars": c.get("chars"),
                         "golden": bool(c.get("golden"))}
                        for c in chapters[:12]],
            "status": bd.get("status")}


@router.get("/api/breakdowns")
def api_list():
    return {"ok": True, "projects": breakdown.list_projects()}


@router.post("/api/breakdown/upload")
async def api_upload(request: Request):
    """上传整本书（multipart：file + 可选 title）。安全校验完全沿用 bot/ingest.py：
    扩展名白名单 / 30MB 大小上限 / 文件头粗校验 / EPUB 解压总量上限。"""
    ctype = (request.headers.get("content-type") or "").lower()
    if not ctype.startswith("multipart/"):
        return _bad("请用 multipart/form-data 上传文件")
    try:
        form = await request.form()
    except Exception:
        return _bad("上传组件未就绪（需要 python-multipart）", 503)
    up = form.get("file")
    if up is None or not hasattr(up, "read"):
        return _bad("缺少文件，请选择 txt / md / docx / epub 等格式的整本文件")
    filename = str(getattr(up, "filename", "") or "upload.txt")
    ext = os.path.splitext(ingest.safe_filename(filename))[1].lower()
    if ext not in ingest.ALLOWED_EXTS:
        return _bad(f"不支持的格式：{ext or '（无扩展名）'}；支持 "
                    + " ".join(ingest.ALLOWED_EXTS))
    # 与 ingest.MAX_BYTES 对齐的读取上限（多读 1 字节用于超限判定）
    try:
        data = await up.read(ingest.MAX_BYTES + 1)
    except Exception as e:
        return _bad(f"读取上传文件失败：{e}")
    if len(data) > ingest.MAX_BYTES:
        return _bad(f"文件超过 {ingest.MAX_BYTES // (1024 * 1024)}MB 上限", 413)

    tmpdir = tempfile.mkdtemp(prefix="bd-upload-")
    path = ""
    try:
        path = os.path.join(tmpdir, ingest.safe_filename(filename))
        with open(path, "wb") as f:
            f.write(data)
        res = ingest.extract_text(path, filename)
    except Exception as e:
        return _bad(f"文件解析失败：{e}")
    finally:
        try:
            if path and os.path.exists(path):
                os.unlink(path)
            os.rmdir(tmpdir)
        except OSError:
            pass
    if not res.get("ok"):
        return _bad(str(res.get("error") or "文件解析失败"),
                    413 if "上限" in str(res.get("error")) else 400)
    title = str(form.get("title") or "").strip() or os.path.splitext(
        ingest.safe_filename(filename))[0]
    created = breakdown.create_project(title, res.get("text", ""), source="upload",
                                       filename=filename)
    if not created.get("ok"):
        return _bad(str(created.get("error") or "创建拆书项目失败"))
    return {"ok": True, **_preview_of(created["project"]),
            "extractWarnings": res.get("warnings") or []}


@router.post("/api/breakdown/paste")
def api_paste(payload: dict):
    """粘贴整本文本。body: {title, text}"""
    p = payload if isinstance(payload, dict) else {}
    text = str(p.get("text") or "")
    if len(text.strip()) < 20:
        return _bad("正文太短（少于 20 字），请粘贴整本文本")
    title = str(p.get("title") or "").strip()
    created = breakdown.create_project(title, text, source="paste")
    if not created.get("ok"):
        return _bad(str(created.get("error") or "创建拆书项目失败"))
    return {"ok": True, **_preview_of(created["project"])}


@router.get("/api/breakdown/{bd_id}")
def api_get(bd_id: str):
    """项目全量：章节段落 + analysis + 全书报告 + 人物聚合 + 已入账手法。"""
    try:
        breakdown._bd_path(bd_id)
    except ValueError:
        return _bad("拆书项目 id 非法")
    bd = breakdown.load_project(bd_id)
    if not bd:
        return _bad("拆书项目不存在", 404)
    bd["running"] = breakdown.is_running(bd_id)
    bd["progress"] = breakdown._progress_of(bd)
    return {"ok": True, "project": bd}


@router.get("/api/breakdown/{bd_id}/progress")
def api_progress(bd_id: str):
    """进度轮询端点（轻量：不含正文与报告）。"""
    try:
        breakdown._bd_path(bd_id)
    except ValueError:
        return _bad("拆书项目 id 非法")
    bd = breakdown.load_project(bd_id)
    if not bd:
        return _bad("拆书项目不存在", 404)
    running = breakdown.is_running(bd_id)
    return {"ok": True, "id": bd_id, "status": "running" if running else bd.get("status"),
            "running": running, "progress": breakdown._progress_of(bd)}


@router.post("/api/breakdown/{bd_id}/run")
def api_run(bd_id: str):
    """启动逐章拆解后台任务（守护线程 + 进度落盘，见 bot/breakdown.run_breakdown）。"""
    res = breakdown.run_breakdown(bd_id)
    if not res.get("ok"):
        note = str(res.get("note") or "启动失败")
        bd = breakdown.load_project(bd_id)
        return _bad(note, 404 if not bd else 409)
    return res


@router.post("/api/breakdown/{bd_id}/chapter/{no}/retry")
def api_retry(bd_id: str, no: int):
    """重试单个失败章（同步；进行中返回 409）。"""
    res = breakdown.retry_chapter(bd_id, no)
    if not res.get("ok"):
        return _bad(str(res.get("note") or "重试失败"),
                    404 if "不存在" in str(res.get("note")) else 409)
    return res


@router.post("/api/breakdown/{bd_id}/report")
def api_report(bd_id: str):
    """全书报告（同步一次 LLM 调用；证据逐字校验在 bot/breakdown.build_report）。"""
    res = breakdown.build_report(bd_id)
    if not res.get("ok"):
        note = str(res.get("note") or "报告生成失败")
        bd = breakdown.load_project(bd_id)
        return _bad(note, 404 if not bd else 409)
    return res


@router.post("/api/breakdown/{bd_id}/resplit")
def api_resplit(bd_id: str):
    """整体重切（清空章节状态与分析）。"""
    res = breakdown.resplit_project(bd_id)
    if not res.get("ok"):
        note = str(res.get("note") or "重切失败")
        bd = breakdown.load_project(bd_id)
        return _bad(note, 404 if not bd else 409)
    bd = breakdown.load_project(bd_id)
    return {"ok": True, **_preview_of(bd)}


@router.post("/api/breakdown/{bd_id}/adopt-techniques")
def api_adopt(bd_id: str, payload: dict):
    """手法入账本。body: {indices: [报告 techniques 下标], projectKey?: 入账到真实作品}"""
    p = payload if isinstance(payload, dict) else {}
    indices = p.get("indices") or p.get("index") or []
    if not isinstance(indices, list):
        return _bad("indices 必须是下标数组")
    res = breakdown.adopt_techniques(bd_id, indices, str(p.get("projectKey") or ""))
    if not res.get("ok"):
        return _bad(str(res.get("note") or "入账失败"))
    return res


@router.delete("/api/breakdown/{bd_id}")
def api_delete(bd_id: str):
    res = breakdown.delete_project(bd_id)
    if not res.get("ok"):
        note = str(res.get("note") or "删除失败")
        return _bad(note, 409 if "进行中" in note else 400)
    return res
