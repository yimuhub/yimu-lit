"""v2 API 聚合层（M6 整合）。

把长篇内核 / 参考资料 / 反馈进化 / 固定评测 暴露为稳定 HTTP 接口，同时保持旧版
`app.py` 不改也能启动：所有新模块都在函数内延迟导入，缺失时返回 501 而不是让
应用 import 失败。这样并行开发期间旧功能与测试始终可用。

约定：
- 成功返回 JSON 可序列化 dict；失败返回 `{"error": "中文原因"}` 与非 2xx。
- 不在这里做业务逻辑；只做参数校验、项目定位、结果整形与错误映射。
- 不打印用户正文、密钥或绝对路径。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import threading
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mod(name: str):
    """延迟导入 bot.<name>；返回 (模块, 错误响应)。"""
    try:
        return importlib.import_module(f"bot.{name}"), None
    except Exception as e:
        return None, JSONResponse(
            {"error": f"{name} 模块尚未就绪：{e}", "code": "module_unavailable"},
            status_code=501,
        )


def _bad(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)


# 各文体的「写正文」阶段码（bot/medium.py 的 stages）。技能文件的 stages 用的是这些码，
# 前端传来的泛化值（WRITE / 空）需要先落地成具体码，否则 pick 只会返回通用技能。
_WRITE_STAGE = {"novel": "P2", "script": "S5", "poetry": "C", "prose": "D"}


def _resolve_stage(stage: str, med: str) -> str:
    """把前端传来的阶段值落地成该文体真实存在的阶段码。

    空值或泛化的 WRITE/WRITING → 该文体「写正文」的阶段；已是合法码则原样保留；
    无法识别时也回落到写正文阶段（技能文件的 stages 里查不到非法码会静默漏掉题材技能）。
    """
    s = str(stage or "").strip().upper()
    write = _WRITE_STAGE.get(med, "P2")
    if not s or s in ("WRITE", "WRITING", "WRITER"):
        return write
    try:
        from bot import medium as medium_mod
        if s in medium_mod.all_stage_codes(med):
            return s
    except Exception:
        return s
    return write


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _read_jsonl(path: str, limit: int = 5000) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
                if len(out) >= limit:
                    break
    except Exception:
        return []
    return out


def _project(get_project, key):
    """定位项目；返回 (proj, None) 或 (None, 响应)。"""
    if not isinstance(key, str) or not key:
        return None, _bad("缺少作品 key", 400)
    proj = get_project(key)
    if not proj:
        return None, _bad("作品不存在", 404)
    return proj, None


def _cfg():
    try:
        from bot import llm
        return llm._global_defaults()
    except Exception:
        return {}


def _persist_learning(summary) -> None:
    """把反馈聚合快照写进学习档案，供下次选题/风格建议使用；失败不影响接口。"""
    try:
        from bot import learn
        learn.record_feedback_summary(summary)
    except Exception:
        pass


def _norm_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# 会话流：等待期心跳间隔（秒）与「没有任何可用供应商」时的友好兜底文案。
_HEARTBEAT_SECS = 2.0
_NO_PROVIDER_TEXT = "您的消息已收到。由于未配置大模型，请配置模型。"


def _waiting_text(elapsed: float) -> str:
    """等待心跳文案：<8s 普通思考，>=8s 深入思考，>=60s 追加网关慢提示（单行）。"""
    n = int(elapsed)
    if elapsed >= 60:
        return f"模型正在深入思考（已等待 {n}s），网关较慢，可点停止后稍后重试"
    if elapsed >= 8:
        return f"模型正在深入思考（已等待 {n}s）"
    return f"模型正在思考中（已等待 {n}s）"


def _provider_ready() -> bool:
    """开跑前探测「有没有可用供应商」；探测自身出错时按有处理，避免掩盖真实错误。"""
    try:
        from bot import providers
        return bool(providers.usable())
    except Exception:
        return True


def _plan_view(proj, plan: dict) -> dict:
    """把 02c 总规划与 02d 章卡合成前端树；结构不匹配时原样返回。"""
    if not isinstance(plan, dict):
        return {"volumes": []}
    cards = _read_jsonl(os.path.join(proj.path, "02d-章卡.jsonl"), limit=20000)
    by_no = {}
    for c in cards:
        try:
            by_no[int(c.get("n"))] = c
        except Exception:
            continue
    sc = plan.get("scale")
    if isinstance(sc, dict):
        words = int(sc.get("target_words") or 0)
        chs = int(sc.get("chapters") or 0)
        plan["scale_label"] = (f"{words / 10000:g} 万字 · {chs} 章" if words and chs
                               else (sc.get("label") or ""))
    volumes = plan.get("volumes")
    if not isinstance(volumes, list):
        return plan
    for v in volumes:
        for a in (v.get("arcs") or []):
            nums = [n for n in (a.get("chapters") or []) if isinstance(n, int)]
            if not nums and a.get("start_chapter") and a.get("end_chapter"):
                try:
                    nums = list(range(int(a["start_chapter"]), int(a["end_chapter"]) + 1))
                except Exception:
                    nums = []
            # 用章卡里的 volume/arc 归属补全（总规划只存了卷弧标题时）
            if not nums:
                vno, ano = v.get("no"), a.get("no")
                nums = [n for n, c in by_no.items()
                        if c.get("volume") == vno and c.get("arc") == ano]
            a["chapters"] = sorted(set(nums))
            a["chapter_cards"] = [by_no.get(n, {"n": n, "status": "planned"}) for n in a["chapters"]]
    return plan


def _attach_digest(proj, lf, plan: dict) -> dict:
    try:
        d = lf.plan_digest(proj)
        return d if isinstance(d, dict) else {}
    except Exception:
        pass
    chapters = plan.get("chapters") or []
    written = sum(1 for c in chapters if c.get("status") == "completed")
    return {"chapters_written": written, "chapters_planned": len(chapters)}


def register_v2_routes(app, get_project, chat_func=None):
    """在 app 上注册 v2 接口。由 app.py 在 `_get_project` 定义后调用。"""

    # ------------------------------------------------------------ 能力探测
    @app.get("/api/capabilities")
    def api_capabilities():
        feats = {"classic_ui": os.path.exists(os.path.join(ROOT, "static", "index.html")),
                 "v2_ui": os.path.exists(os.path.join(ROOT, "static", "v2", "index.html")),
                 "chat_stream": bool(chat_func)}
        for name, modname in (("longform", "longform"), ("references", "references"),
                              ("feedback", "feedback"), ("skill_evolution", "skill_evo"),
                              ("eval", "eval")):
            try:
                importlib.import_module(f"bot.{modname}")
                feats[name] = True
            except Exception:
                feats[name] = False
        try:
            from bot import medium as medium_mod
            media = [{"key": k, "label": v.get("label"), "icon": v.get("icon"),
                      "desc": v.get("desc"), "stages": [s[0] for s in v.get("stages", [])]}
                     for k, v in medium_mod.MEDIUMS.items()]
        except Exception:
            media = []
        return {"ok": True, "features": feats, "mediums": media,
                "time": int(time.time())}

    # ------------------------------------------------------------ 长篇规划
    @app.get("/api/plan")
    def api_plan(key: str = ""):
        proj, err = _project(get_project, key)
        if err:
            return err
        lf, err = _mod("longform")
        if err:
            return err
        try:
            result = lf.ensure_plan(proj)
            plan = result if isinstance(result, dict) else None
            if not plan:
                plan = _read_json(os.path.join(proj.path, "02c-总规划.json"), {})
            plan = _plan_view(proj, plan)
            proposals = lf.list_proposals(proj)
            if isinstance(proposals, dict):
                proposals = proposals.get("items") or proposals.get("proposals") or []
            return {"ok": True, "key": key, "plan": plan,
                    "digest": _attach_digest(proj, lf, plan),
                    "proposals": _norm_list(proposals)}
        except Exception as e:
            return _bad(f"读取规划失败：{e}", 500)

    @app.post("/api/plan/ensure")
    def api_plan_ensure(payload: dict):
        return api_plan(str(payload.get("key") or ""))

    @app.post("/api/plan/replan")
    def api_plan_replan(payload: dict):
        proj, err = _project(get_project, payload.get("key"))
        if err:
            return err
        lf, err = _mod("longform")
        if err:
            return err
        scope = str(payload.get("scope") or "window")
        if scope not in ("book", "volume", "arc", "window", "chapter"):
            return _bad("scope 只能是 book/volume/arc/window/chapter")
        try:
            proposal = lf.replan(proj, scope=scope,
                                 instruction=str(payload.get("instruction") or "")[:4000],
                                 reason=str(payload.get("reason") or "manual"))
            return {"ok": True, "proposal": proposal}
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"生成重规划提案失败：{e}", 500)

    @app.post("/api/plan/multi")
    def api_plan_multi(payload: dict):
        """v2.1 P2：多岗位同时规划 —— 规划师与总编各出一份提案，前端并排对比后选一份应用。"""
        proj, err = _project(get_project, payload.get("key"))
        if err:
            return err
        lf, err = _mod("longform")
        if err:
            return err
        scope = str(payload.get("scope") or "window")
        if scope not in ("book", "volume", "arc", "window", "chapter"):
            return _bad("scope 只能是 book/volume/arc/window/chapter")
        roles = payload.get("roles")
        if not isinstance(roles, list):
            roles = None
        try:
            fn = getattr(lf, "multi_replan", None)
            if not callable(fn):
                return _bad("多角色规划尚未就绪", 501)
            cards = fn(proj, scope=scope,
                       instruction=str(payload.get("instruction") or "")[:4000],
                       roles=roles)
            return {"ok": True, "key": payload.get("key"), "scope": scope, "cards": cards or []}
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"多角色规划失败：{e}", 500)

    @app.post("/api/plan/apply")
    def api_plan_apply(payload: dict):
        proj, err = _project(get_project, payload.get("key"))
        if err:
            return err
        lf, err = _mod("longform")
        if err:
            return err
        pid = str(payload.get("proposal_id") or "")
        if not pid:
            return _bad("缺少 proposal_id")
        try:
            res = lf.apply_replan(proj, pid, allow_past=bool(payload.get("allow_past")))
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "应用失败"))
            return {"ok": True, "result": res}
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"应用提案失败：{e}", 500)

    @app.post("/api/plan/reject")
    def api_plan_reject(payload: dict):
        proj, err = _project(get_project, payload.get("key"))
        if err:
            return err
        lf, err = _mod("longform")
        if err:
            return err
        try:
            return {"ok": True, "result": lf.reject_replan(
                proj, str(payload.get("proposal_id") or ""),
                str(payload.get("note") or ""))}
        except Exception as e:
            return _bad(f"驳回失败：{e}", 500)

    @app.post("/api/plan/window")
    def api_plan_window(payload: dict):
        proj, err = _project(get_project, payload.get("key"))
        if err:
            return err
        lf, err = _mod("longform")
        if err:
            return err
        try:
            start = int(payload.get("start") or 0)
            end = int(payload.get("end") or 0)
            return {"ok": True, "result": lf.plan_window(proj, start, end)}
        except Exception as e:
            return _bad(f"滚动规划失败：{e}", 500)

    # ------------------------------------------------------------ 状态库
    @app.get("/api/state")
    def api_state(key: str = "", q: str = ""):
        proj, err = _project(get_project, key)
        if err:
            return err
        stmod, err = _mod("state")
        if err:
            return err
        try:
            st = stmod.load(proj.path) or {}
            plan = proj.plan() or {}
            total = len(plan.get("chapters") or [])
            done = sum(1 for c in (plan.get("chapters") or []) if c.get("status") == "completed")
            cur = min(done + 1, total or done + 1)
            try:
                issues = stmod.validate(st)
            except Exception:
                issues = []
            try:
                due = stmod.foreshadow_due(st, cur)
            except Exception:
                due = {}
            chars = [{"name": k, **v} for k, v in (st.get("characters") or {}).items()]
            return {"ok": True, "key": key, "state": st,
                    "current_chapter": cur,
                    "chapters": {"done": done, "total": total},
                    "characters": chars,
                    "locations": [{"name": k, **v} for k, v in (st.get("locations") or {}).items()],
                    "items": [{"name": k, **v} for k, v in (st.get("items") or {}).items()],
                    "factions": [{"name": k, **v} for k, v in (st.get("factions") or {}).items()],
                    "threads": _norm_list(st.get("threads")),
                    "foreshadowing": [{"id": k, **v} for k, v in (st.get("foreshadowing") or {}).items()],
                    "timeline": _norm_list(st.get("timeline"))[-30:],
                    "issues": issues, "foreshadow_due": due,
                    "excerpt": stmod.excerpt(st, q, 6000)}
        except Exception as e:
            return _bad(f"读取状态库失败：{e}", 500)

    # ------------------------------------------------------------ 参考资料
    @app.get("/api/references")
    def api_refs(key: str = ""):
        refs, err = _mod("references")
        if err:
            return err
        try:
            res = refs.list_refs(key)
            if isinstance(res, list):
                res = {"items": res}
            return {"ok": True, **(res or {})}
        except Exception as e:
            return _bad(f"读取参考资料失败：{e}", 500)

    @app.post("/api/references/upload")
    async def api_refs_upload(request: Request):
        refs, err = _mod("references")
        if err:
            return err
        try:
            form = await request.form()
        except Exception:
            return _bad("上传组件未就绪（需要 python-multipart）", 503)
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            return _bad("缺少文件")
        try:
            data = await up.read(30 * 1024 * 1024 + 1)
        except Exception as e:
            return _bad(f"读取上传文件失败：{e}")
        if len(data) > 30 * 1024 * 1024:
            return _bad("文件超过 30MB 上限", 413)
        try:
            res = refs.add_upload(
                data,
                filename=str(form.get("filename") or getattr(up, "filename", "") or "upload.txt"),
                title=str(form.get("title") or ""),
                usage_mode=str(form.get("usage_mode") or "facts"),
                copyright_confirmed=str(form.get("copyright_confirmed") or "true").lower()
                in ("1", "true", "yes", "on"),
                cfg=_cfg(),
            )
        except Exception as e:
            return _bad(f"解析参考资料失败：{e}", 500)
        if isinstance(res, dict) and res.get("ok") is False:
            return _bad(str(res.get("error") or "入库失败"))
        rid = (res or {}).get("ref_id") or ((res or {}).get("ref") or {}).get("id")
        pkey = str(form.get("project_key") or "")
        if pkey and rid:
            try:
                refs.link_to_project(pkey, rid, str(form.get("usage_mode") or "facts"))
            except Exception:
                pass
        return {"ok": True, "item": (res or {}).get("ref") or (res or {}).get("item") or res,
                "ref_id": rid}

    @app.post("/api/references/text")
    def api_refs_text(payload: dict):
        refs, err = _mod("references")
        if err:
            return err
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return _bad("缺少参考资料正文")
        try:
            res = refs.add_text(text, str(payload.get("title") or "未命名资料"),
                                kind=str(payload.get("kind") or ""),
                                usage_mode=str(payload.get("usage_mode") or "facts"),
                                copyright_confirmed=bool(payload.get("copyright_confirmed", True)),
                                cfg=_cfg())
        except Exception as e:
            return _bad(f"保存参考资料失败：{e}", 500)
        if isinstance(res, dict) and res.get("ok") is False:
            return _bad(str(res.get("error") or "入库失败"))
        rid = (res or {}).get("ref_id") or ((res or {}).get("ref") or {}).get("id")
        pkey = str(payload.get("project_key") or "")
        if pkey and rid:
            try:
                refs.link_to_project(pkey, rid, str(payload.get("usage_mode") or "facts"))
            except Exception:
                pass
        return {"ok": True, "item": (res or {}).get("ref") or (res or {}).get("item") or res,
                "ref_id": rid}

    @app.post("/api/references/url")
    def api_refs_url(payload: dict):
        refs, err = _mod("references")
        if err:
            return err
        url = str(payload.get("url") or "")
        if not url:
            return _bad("缺少网址")
        try:
            res = refs.add_url(url, title=str(payload.get("title") or ""),
                               usage_mode=str(payload.get("usage_mode") or "facts"),
                               copyright_confirmed=bool(payload.get("copyright_confirmed", True)),
                               cfg=_cfg())
        except Exception as e:
            return _bad(f"抓取参考资料失败：{e}", 500)
        if isinstance(res, dict) and res.get("ok") is False:
            return _bad(str(res.get("error") or "入库失败"))
        rid = (res or {}).get("ref_id") or ((res or {}).get("ref") or {}).get("id")
        pkey = str(payload.get("project_key") or "")
        if pkey and rid:
            try:
                refs.link_to_project(pkey, rid, str(payload.get("usage_mode") or "facts"))
            except Exception:
                pass
        return {"ok": True, "item": (res or {}).get("ref") or (res or {}).get("item") or res,
                "ref_id": rid}

    @app.patch("/api/references/{ref_id}")
    def api_refs_patch(ref_id: str, payload: dict):
        refs, err = _mod("references")
        if err:
            return err
        try:
            if payload.get("usage_mode") is not None:
                res = refs.set_usage(ref_id, str(payload["usage_mode"]))
                if isinstance(res, dict) and res.get("ok") is False:
                    return _bad(str(res.get("error") or "更新失败"))
            if any(k in payload for k in ("title", "tags", "copyright_confirmed")):
                res = refs.update_meta(ref_id, title=payload.get("title"),
                                       tags=payload.get("tags"),
                                       copyright_confirmed=payload.get("copyright_confirmed"))
                if isinstance(res, dict) and res.get("ok") is False:
                    return _bad(str(res.get("error") or "更新失败"))
            return {"ok": True, "item": refs.get_ref(ref_id)}
        except Exception as e:
            return _bad(f"更新参考资料失败：{e}", 500)

    @app.delete("/api/references/{ref_id}")
    def api_refs_delete(ref_id: str):
        refs, err = _mod("references")
        if err:
            return err
        try:
            res = refs.delete_ref(ref_id)
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "删除失败"))
            return {"ok": True, "result": res}
        except Exception as e:
            return _bad(f"删除参考资料失败：{e}", 500)

    @app.post("/api/references/{ref_id}/analyze")
    def api_refs_analyze(ref_id: str):
        refs, err = _mod("references")
        if err:
            return err
        try:
            res = refs.analyze_ref(ref_id, cfg=_cfg())
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "分析失败"))
            return {"ok": True, "analysis": res}
        except Exception as e:
            return _bad(f"分析参考资料失败：{e}", 500)

    @app.get("/api/references/{ref_id}/chunks")
    def api_refs_chunks(ref_id: str, offset: int = 0, limit: int = 20):
        refs, err = _mod("references")
        if err:
            return err
        try:
            return {"ok": True, **refs.read_chunks(ref_id, offset, min(max(limit, 1), 50))}
        except Exception as e:
            return _bad(f"读取切片失败：{e}", 500)

    @app.get("/api/references/search")
    def api_refs_search(key: str = "", q: str = "", k: int = 6):
        refs, err = _mod("references")
        if err:
            return err
        if not q.strip():
            return {"ok": True, "items": []}
        try:
            ids = None
            if key:
                ids = refs.project_ref_ids(key)
                if not ids:
                    return {"ok": True, "items": []}
            items = refs.retrieve(q, ids, k=min(max(int(k), 1), 20))
            return {"ok": True, "items": items,
                    "block": refs.context_block(q, ids, 4000, k=min(max(int(k), 1), 20))}
        except Exception as e:
            return _bad(f"检索失败：{e}", 500)

    @app.post("/api/references/link")
    def api_refs_link(payload: dict):
        refs, err = _mod("references")
        if err:
            return err
        proj, perr = _project(get_project, payload.get("key"))
        if perr:
            return perr
        try:
            return {"ok": True, "result": refs.link_to_project(
                os.path.basename(proj.path), str(payload.get("ref_id") or ""),
                str(payload.get("usage_mode") or "facts"))}
        except Exception as e:
            return _bad(f"关联失败：{e}", 500)

    @app.post("/api/references/unlink")
    def api_refs_unlink(payload: dict):
        refs, err = _mod("references")
        if err:
            return err
        proj, perr = _project(get_project, payload.get("key"))
        if perr:
            return perr
        try:
            return {"ok": True, "result": refs.unlink_from_project(
                os.path.basename(proj.path), str(payload.get("ref_id") or ""))}
        except Exception as e:
            return _bad(f"取消关联失败：{e}", 500)

    @app.post("/api/references/overlap")
    def api_refs_overlap(payload: dict):
        refs, err = _mod("references")
        if err:
            return err
        text = payload.get("text")
        if not isinstance(text, str):
            return _bad("缺少待检文本")
        try:
            ids = payload.get("ref_ids")
            if ids is None and payload.get("key"):
                ids = refs.project_ref_ids(str(payload["key"]))
            return {"ok": True, **refs.overlap_check(text, ids)}
        except Exception as e:
            return _bad(f"查重失败：{e}", 500)

    # ------------------------------------------------------------ 反馈
    @app.post("/api/feedback")
    def api_feedback_add(payload: dict):
        fb, err = _mod("feedback")
        if err:
            return err
        try:
            if payload.get("before") is not None and payload.get("after") is not None:
                rec = fb.capture_edit(str(payload.get("project_key") or ""),
                                      payload.get("chapter_no"),
                                      payload.get("before"), payload.get("after"),
                                      rating=payload.get("rating"),
                                      tags=payload.get("tags") or [],
                                      note=payload.get("note") or "")
            else:
                rec = fb.record(payload)
            if isinstance(rec, dict) and rec.get("ok") is False:
                return _bad(str(rec.get("error") or "记录失败"))
            return {"ok": True, "record": rec}
        except Exception as e:
            return _bad(f"记录反馈失败：{e}", 500)

    @app.get("/api/feedback")
    def api_feedback_list(key: str = "", medium: str = "", stage: str = "", limit: int = 100):
        fb, err = _mod("feedback")
        if err:
            return err
        try:
            items = fb.list_feedback(key, medium, stage, min(max(limit, 1), 500))
            summary = fb.aggregate()
            _persist_learning(summary)
            return {"ok": True, "items": _norm_list(items),
                    "summary": summary,
                    "focus": fb.suggest_focus(medium, stage)}
        except Exception as e:
            return _bad(f"读取反馈失败：{e}", 500)

    @app.get("/api/feedback/summary")
    def api_feedback_summary():
        fb, err = _mod("feedback")
        if err:
            return err
        try:
            summary = fb.aggregate()
            _persist_learning(summary)
            return {"ok": True, "summary": summary}
        except Exception as e:
            return _bad(f"汇总反馈失败：{e}", 500)

    # ------------------------------------------------------------ 技能与进化
    @app.get("/api/skills")
    def api_skills(medium: str = "", stage: str = ""):
        try:
            from bot import skillkit
            return {"ok": True, "items": skillkit.list_skills(medium, stage),
                    "stats": skillkit.stats()}
        except Exception as e:
            return _bad(f"读取技能库失败：{e}", 500)

    @app.get("/api/skills/tree")
    def api_skills_tree(medium: str = ""):
        """技能库导航树：文体 → 题材分组（前端 #/skills 左栏）。"""
        try:
            from bot import skillkit
            fn = getattr(skillkit, "tree", None)
            if not fn:
                return {"ok": True, "tree": []}
            return {"ok": True, "tree": fn(medium or None)}
        except Exception as e:
            return _bad(f"读取技能树失败：{e}", 500)

    @app.get("/api/skills/file")
    def api_skills_file(path: str = ""):
        """按相对路径读取单个技能文件（frontmatter + 正文）；越界路径返回 400。"""
        try:
            from bot import skillkit
            return {"ok": True, **skillkit.read_skill(path)}
        except ValueError as e:
            return _bad(str(e), 400)
        except Exception as e:
            return _bad(f"读取技能文件失败：{e}", 500)

    @app.get("/api/skills/applicable")
    def api_skills_applicable(key: str = "", stage: str = ""):
        """某本书在当前题材下会被注入的技能清单 + 注入预览。"""
        proj, err = _project(get_project, key)
        if err:
            return err
        try:
            from bot import medium as medium_mod
            from bot import skillkit
            style = _read_json(os.path.join(proj.path, "style.json"), {}) or {}
            plan = proj.plan() or {}
            brief = plan.get("brief") or {}
            meta = plan.get("meta") or {}
            med = str(plan.get("medium") or meta.get("medium") or style.get("medium") or "novel")
            if not medium_mod.is_medium(med):
                med = "novel"
            st = _resolve_stage(stage, med)
            genre = str(style.get("genre") or brief.get("genre") or meta.get("genre") or plan.get("genre") or "")
            picked = skillkit.pick(med, st, genre=genre)
            items = [{"relpath": sf.relpath, "name": sf.name, "title": sf.title,
                      "kind": sf.kind, "weight": sf.weight, "genre": sf.genre or "_index",
                      "evolved": sf.evolved} for sf in picked]
            preview = ""
            try:
                preview = skillkit.stage_inject(st, med, genre=genre, max_bytes=6000)
            except Exception:
                pass
            return {"ok": True, "key": key, "medium": med, "genre": genre,
                    "stage": st, "items": items, "injected_preview": preview}
        except Exception as e:
            return _bad(f"读取本书技能失败：{e}", 500)

    @app.get("/api/skills/validate")
    def api_skills_validate():
        try:
            from bot import skillkit
            fn = getattr(skillkit, "validate_all", None)
            return {"ok": True, "result": fn() if fn else {"note": "当前版本无全库校验"}}
        except Exception as e:
            return _bad(f"校验技能库失败：{e}", 500)

    @app.get("/api/skills/evolution")
    def api_skills_evolution(status: str = ""):
        se, err = _mod("skill_evo")
        if err:
            return err
        out = {"ok": True}
        try:
            out["candidates"] = _norm_list(se.list_candidates(status or "pending"))
        except Exception as e:
            out["candidates"] = []
            out["candidates_error"] = str(e)
        try:
            out["history"] = _norm_list(se.history())
        except Exception:
            out["history"] = []
        try:
            fb, _ = _mod("feedback")
            out["feedback"] = fb.aggregate() if fb else {}
        except Exception:
            out["feedback"] = {}
        try:
            from bot import skillkit
            out["stats"] = skillkit.stats()
        except Exception:
            out["stats"] = {}
        return out

    @app.post("/api/skills/evolution/propose")
    def api_skills_propose(payload: dict):
        se, err = _mod("skill_evo")
        if err:
            return err
        try:
            if payload.get("from_feedback"):
                res = se.propose_from_feedback(str(payload.get("medium") or ""),
                                               str(payload.get("stage") or ""), cfg=_cfg())
            else:
                res = se.propose(str(payload.get("name") or ""),
                                 str(payload.get("medium") or ""),
                                 payload.get("stages") or [],
                                 str(payload.get("title") or ""),
                                 str(payload.get("content") or ""),
                                 reason=str(payload.get("reason") or ""),
                                 cfg=_cfg())
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "生成候选失败"))
            return {"ok": True, "candidate": res}
        except Exception as e:
            return _bad(f"生成候选失败：{e}", 500)

    @app.post("/api/skills/evolution/evaluate")
    def api_skills_evaluate(payload: dict):
        se, err = _mod("skill_evo")
        if err:
            return err
        try:
            res = se.evaluate_candidate(str(payload.get("id") or ""),
                                        str(payload.get("baseline") or ""), cfg=_cfg())
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "评测失败"))
            return {"ok": True, "result": res}
        except Exception as e:
            return _bad(f"评测失败：{e}", 500)

    @app.post("/api/skills/evolution/promote")
    def api_skills_promote(payload: dict):
        se, err = _mod("skill_evo")
        if err:
            return err
        try:
            res = se.promote(str(payload.get("id") or ""), force=bool(payload.get("force")),
                             approver=str(payload.get("approver") or "user"))
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "晋升失败"))
            return {"ok": True, "result": res}
        except Exception as e:
            return _bad(f"晋升失败：{e}", 500)

    @app.post("/api/skills/evolution/reject")
    def api_skills_reject(payload: dict):
        se, err = _mod("skill_evo")
        if err:
            return err
        try:
            return {"ok": True, "result": se.reject(str(payload.get("id") or ""),
                                                    str(payload.get("note") or ""))}
        except Exception as e:
            return _bad(f"驳回失败：{e}", 500)

    @app.post("/api/skills/evolution/rollback")
    def api_skills_rollback(payload: dict):
        se, err = _mod("skill_evo")
        if err:
            return err
        try:
            res = se.rollback(str(payload.get("name") or ""), str(payload.get("version") or ""))
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "回滚失败"))
            return {"ok": True, "result": res}
        except Exception as e:
            return _bad(f"回滚失败：{e}", 500)

    # ------------------------------------------------------------ 固定评测
    @app.get("/api/eval/cases")
    def api_eval_cases(medium: str = ""):
        ev, err = _mod("eval")
        if err:
            return err
        try:
            return {"ok": True, "items": ev.list_cases(medium)}
        except Exception as e:
            return _bad(f"读取评测集失败：{e}", 500)

    @app.get("/api/eval/runs")
    def api_eval_runs(limit: int = 20):
        ev, err = _mod("eval")
        if err:
            return err
        try:
            return {"ok": True, "items": ev.list_runs(min(max(limit, 1), 100))}
        except Exception as e:
            return _bad(f"读取评测记录失败：{e}", 500)

    @app.post("/api/eval/compare")
    def api_eval_compare(payload: dict):
        ev, err = _mod("eval")
        if err:
            return err
        try:
            res = ev.compare(str(payload.get("baseline") or ""),
                             str(payload.get("candidate") or ""),
                             payload.get("case_ids"), cfg=_cfg())
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "评测失败"))
            return {"ok": True, "result": res}
        except Exception as e:
            return _bad(f"评测失败：{e}", 500)

    # ------------------------------------------------------------ 会话流式
    @app.post("/api/chat/stream")
    async def api_chat_stream(request: Request):
        if not chat_func:
            return _bad("流式接口未启用", 501)
        try:
            payload = await request.json()
        except Exception:
            return _bad("请求体必须是 JSON")
        message = str(payload.get("message") or "")
        if not message.strip():
            return _bad("消息不能为空")
        sid = str(payload.get("session_id") or uuid.uuid4().hex)

        async def gen():
            t0 = time.time()
            yield _sse("start", {"session_id": sid, "ts": int(t0)})

            # 没有任何可用供应商：消息照收，直接给友好兜底；绝不进 chat_func
            # （不碰网络、不推进会话状态机）。
            if not _provider_ready():
                yield _sse("status", {"stage": "thinking", "text": "正在检查模型配置…"})
                yield _sse("delta", {"text": _NO_PROVIDER_TEXT})
                yield _sse("done", {"ok": True, "fallback": True, "text": _NO_PROVIDER_TEXT,
                                    "session_id": sid, "elapsed": round(time.time() - t0, 2)})
                return

            yield _sse("status", {"stage": "thinking", "text": "正在理解你的需求…"})

            # chat_func 是同步阻塞的（一轮可能几分钟），丢到 daemon 线程里跑，
            # 主协程每 ~2s 发一次心跳。用 Event 而不是 wait_for(queue.get())：
            # Event 的状态不会被取消吞掉，边界时刻完成也不会丢结果。
            loop = asyncio.get_running_loop()
            ready = asyncio.Event()
            box: list = []

            def _work():
                try:
                    box.append((True, chat_func(sid, message)))
                except BaseException as e:  # 原样带回事件循环，转成 error 帧
                    box.append((False, e))
                finally:
                    try:
                        loop.call_soon_threadsafe(ready.set)
                    except RuntimeError:
                        pass  # 事件循环已关闭（客户端早退）：结果丢弃，线程自然结束

            worker = threading.Thread(target=_work, name="v2-chat-worker", daemon=True)
            worker.start()

            while not ready.is_set():
                try:
                    await asyncio.wait_for(ready.wait(), timeout=_HEARTBEAT_SECS)
                except TimeoutError:
                    yield _sse("status", {"stage": "thinking",
                                          "text": _waiting_text(time.time() - t0)})

            ok, value = box[0] if box else (False, RuntimeError("会话处理未返回结果"))
            if not ok:
                err = f"处理失败：{value}"
                yield _sse("error", {"error": err})
                yield _sse("done", {"ok": False, "error": err,
                                    "elapsed": round(time.time() - t0, 2)})
                return
            text = value if isinstance(value, str) else str(value or "")
            # 旧状态机返回整段文本；按小段推送，让前端有真实的逐字阅读感。
            for i in range(0, len(text), 28):
                yield _sse("delta", {"text": text[i:i + 28]})
                await asyncio.sleep(0.012)
            yield _sse("done", {"ok": True, "text": text, "session_id": sid,
                                "elapsed": round(time.time() - t0, 2)})

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    return app
