"""novel-bot 主应用：聊天即开写（深绑 「一幕·神笔」创作系统）"""
import json, os, re, threading, uuid
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from bot import llm, workflow, writer, providers, version

ROOT = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="novel-bot")

# ---------- 会话状态机 ----------
# state: idle → options → planning(忙) → title → writing(后台) → done
SESSIONS = {}
PROJECT_INDEX = {}  # key -> writer.Project（后台写作项目）

def _get_project(key):
    if key not in PROJECT_INDEX:
        pdir = os.path.join(writer.PROJECTS_DIR, key)
        if os.path.isdir(pdir):
            PROJECT_INDEX[key] = writer.Project(pdir, key)
    return PROJECT_INDEX.get(key)

def _pick_title(plan, picked):
    """按用户选择回写 novelName"""
    titles = plan.get("titles", [])
    if picked.isdigit() and 1 <= int(picked) <= len(titles):
        plan["novelName"] = re.sub(r'[\\/:*?"<>|\s]+', "", titles[int(picked) - 1]["title"])
    elif picked:
        plan["novelName"] = re.sub(r'[\\/:*?"?<>"|\s]+', "", picked)

def _start_bg(key):
    proj = _get_project(key)
    writer.start_writing(proj)

import sys
def _trace(tag, **kw):
    print(f"[TRACE] {tag} {kw}", file=sys.stderr, flush=True)

def chat(session_id, message):
    s = SESSIONS.get(session_id) or {"id": session_id, "state": "idle", "cfg": None,
                                     "project": None, "titles": None, "busy": False}
    SESSIONS[session_id] = s
    msg = message.strip()
    _trace("chat-in", sid=session_id, state=s["state"], msg=msg[:40], busy=s.get("busy"))
    reply = []

    # ---- 重复投递保护：同一会话的 LLM 重操作进行中，短消息/重复消息直接给状态提示 ----
    if s.get("busy"):
        if len(msg) <= 4 or s.get("state") in ("planning", "writing"):
            return "⏳ 正在处理中（规划/创作需 1-3 分钟），请稍候，或说「进度」查看状态。"
    # ---- 写作进行中 ----
    if s["state"] in ("writing", "done") and s.get("project"):
        proj = _get_project(s["project"])
        if proj:
            st = proj.status()
            if st.get("running") or st["state"] == "in_progress":
                if re.search(r"进度|继续|怎么样|状态|status", msg) or len(msg) < 6:
                    return f"📖 《{st['name']}》正在创作中：第 {st['done']}/{st['total']} 章，已写 {st['words']} 字。" \
                           f"完成前你可以随时问我「进度」。"
                return "正在疯狂创作中（阶段3 无需确认，技能规定一口气写完）。\n" \
                       f"当前进度 {st['done']}/{st['total']} 章，说「进度」查看，或发新主题开新作品。"
        # 已完成 → 允许新主题
    # ---- 新作品触发（仅 idle；options 状态下重复丢主题 → 重显菜单，不重复提取）----
    if s["state"] == "idle" or re.search(r"^(写|帮我写|新|再|来)(一|个)?(部|本)?|新作品|新的小说|重新", msg):
        if s["state"] == "options" and not re.search(r"^(写|帮我写|新|再|来)|新作品|新的小说|重新", msg):
            pass
        elif s["state"] == "options":
            # 重复的主题消息（投递重试）：状态未变，直接重显配置菜单
            return workflow.config_summary(s["cfg"]) + ("\n\n再选：【1】确认 【2】快车道 【3】再调整 【4】跳过直接写")
        elif s["state"] == "idle" or re.search(r"^(写|帮我写|新|再|来)|新作品|新的小说|重新", msg):
            # 阶段1：主题提取（快车道）——带上本次选择的供应商/模型（session 里 s['prov']）
            prov = s.get("prov") or {}
            try:
                cfg = workflow.extract_config(msg, cfg=prov)
            except Exception as e:
                return f"⚠️ 理解主题失败（{e}），请再描述具体一点。"
            s["cfg"] = dict(cfg)
            s["cfg"].update(prov)  # 让所选供应商/模型随 cfg 贯穿整条创作流水线
            s["state"] = "options"
            reply.append(workflow.config_summary(cfg))
            reply.append("\n接下来选一个：\n"
                        "【1】确认配置，开始规划\n"
                        "【2】快车道：其余设定随机智能生成，直接规划\n"
                        "【3】调整（直接告诉我要改什么，如：改成20章/主角换女性/基调轻松点）\n"
                        "【4】跳过问答直接写（全随机 + 立即开写，最少确认）")
            return "\n".join(reply)
    # ---- options 阶段 ----
    if s["state"] == "options" and s.get("cfg"):
        _trace("options-branch", msg=msg[:40], starts134=msg.startswith(("1", "3", "4")))
        if msg.startswith(("1", "3", "4")) or msg in ("确认", "快车道", "直接写", "随机"):
            cfg = s["cfg"]
            if msg.startswith("3") and len(msg) > 2:
                # 合并调整意见（带忙锁，防重复投递双跑 LLM）
                s["busy"] = True
                try:
                    merged = llm.chat_json([{"role": "system", "content":
                        "把用户的调整意见合并进小说配置 JSON，保持其他字段不变，只输出 JSON。"},
                        {"role": "user", "content": f"当前：{json.dumps(cfg, ensure_ascii=False)}\n调整：{msg}"}],
                        cfg=s.get("cfg"))
                    cfg.update(merged)
                    s["cfg"] = cfg
                    return workflow.config_summary(cfg) + "\n\n再选：【1】确认 【2】快车道 【3】再调整"
                finally:
                    s["busy"] = False
            if msg.startswith("4"):
                cfg = workflow.fill_defaults(cfg)
                s["state"] = "planning"
                s["busy"] = True
                try:
                    return _do_planning(s)
                finally:
                    s["busy"] = False
            if msg.startswith("2"):
                cfg = workflow.fill_defaults(cfg)
                s["state"] = "planning"
                s["busy"] = True
                try:
                    return _do_planning(s)
                finally:
                    s["busy"] = False
            # msg=1：已有信息直接规划（缺省字段用默认）
            s["state"] = "planning"
            s["busy"] = True
            try:
                return _do_planning(s)
            finally:
                s["busy"] = False
        if s.get("cfg") and len(msg) > 2:
            # 当作调整意见（忙锁防重复投递）
            s["busy"] = True
            try:
                return chat(session_id, "3 " + msg)
            finally:
                s["busy"] = False
        return "请回复 1 / 2 / 3 / 4，或直接用文字告诉我要调整什么。"
    # ---- title 阶段 ----
    if s["state"] == "title" and s.get("project"):
        proj = _get_project(s["project"])
        plan = proj.plan() if proj else None
        if plan:
            _pick_title(plan, msg.strip())
            # 按最终书名对齐目录
            _rename_project(proj, plan["novelName"])
            proj.save_plan(plan)
            s["project"] = os.path.basename(proj.path)
            s["state"] = "writing"
            _start_bg(s["project"])
            return (f"✅ 书名《{plan['novelName']}》确定，阶段3 疯狂创作已启动（{plan['totalChapters']}章）。\n"
                    f"技能规定创作全程无需确认，我边写边报进度。完成后可在右侧「作品」里下载成书。")
    # ---- 兜底（按当前状态给上下文提示，不再一律退回首页文案）----
    if s.get("state") == "planning":
        return "⏳ 规划已启动（人物档案/大纲/文风/书名生成中，约 1-2 分钟），稍后会自动给出书名选择。"
    if s.get("state") in ("title", "writing", "done") and s.get("project"):
        proj = _get_project(s["project"])
        st = proj.status() if proj else {}
        return f"当前《{st.get('name', s['project'])}》：{st.get('done', 0)}/{st.get('total', 0)} 章（{st.get('state', '?')}）。\n" \
               f"说「进度」查看，或发新主题开新作品。"
    if s.get("state") == "idle":
        return "直接告诉我要写什么主题的小说，例如：\n「写一部悬疑小说，退休刑警被诬陷杀人」\n" \
               "「帮我写穿越文，女主，宫廷，20章」"
    return "请回复对应编号，或发新主题开始新作品。"

def _do_planning(s):
    """阶段2：生成四个规划文件（耗时 1-2 分钟）"""
    cfg = s["cfg"]
    try:
        titles = workflow.gen_titles(cfg)
        proj = writer.create_project(cfg, titles)
        s["project"] = os.path.basename(proj.path)
        s["titles"] = titles
        s["state"] = "title"
        t = [f"【{i+1}】《{x['title']}》—— {x.get('technique','')}，{x.get('why','')}"
             for i, x in enumerate(titles[:5])]
        return ("📐 规划完成（人物档案/大纲/文风基准/写作计划 已生成）。\n"
                "请选书名（回复编号，或直接输入你想要的书名）：\n" + "\n".join(t))
    except Exception as e:
        s["state"] = "options"
        return f"⚠️ 规划生成失败：{e}\n回复「重试」再试一次，或调整配置。"

def _rename_project(proj, new_name):
    """按最终书名调整目录后缀（保留时间戳前缀）"""
    base = os.path.basename(proj.path)
    prefix = base.split("-", 1)[0] + "-"
    target = os.path.join(writer.PROJECTS_DIR, prefix + new_name)
    if target != proj.path and not os.path.exists(target):
        os.rename(proj.path, target)
        proj.path = target
        proj.name = new_name
        plan = proj.plan()
        plan["novelName"] = new_name
        proj.save_plan(plan)

# ---------- 路由 ----------

@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "static", "index.html"))

@app.post("/api/chat")
def api_chat(payload: dict):
    sid = payload.get("session_id") or str(uuid.uuid4())
    message = payload.get("message", "")
    if not message:
        return JSONResponse({"session_id": sid, "reply": "说个主题，我帮你写小说。"})
    # 记录本次选择的供应商/模型到 session（供新作品 / 调整 LLM 调用使用）
    prov = {}
    if payload.get("provider_id"):
        prov["_provider_id"] = str(payload["provider_id"])
    if payload.get("model"):
        prov["_model"] = str(payload["model"])
    if prov:
        SESSIONS.setdefault(sid, {"id": sid, "state": "idle", "cfg": None,
                                   "project": None, "titles": None, "busy": False})["prov"] = prov
    # 重试指令
    if message.strip() in ("重试", "retry") and SESSIONS.get(sid, {}).get("state") == "options":
        s = SESSIONS[sid]
        s["state"] = "options"
        r = _do_planning(s)
        return {"session_id": sid, "reply": r, "project": s.get("project")}
    reply = chat(sid, message)
    return {"session_id": sid, "reply": reply, "project": SESSIONS[sid].get("project")}

# ---------- 模型供应商管理（多供应商绑定 / 按任务选模型） ----------

def _prov_err(e):
    return JSONResponse({"error": str(e)}, status_code=400)

@app.get("/api/providers")
def api_providers_list():
    try:
        d = providers.list_providers()
        d["key_ok"] = any(p.get("has_key") for p in d.get("providers", []))
        return d
    except providers.ProviderError as e:
        return _prov_err(e)

@app.get("/api/providers/{pid}/models")
def api_provider_models(pid: str):
    try:
        return {"id": pid, "models": providers.get_models(pid)}
    except providers.ProviderError as e:
        return _prov_err(e)

@app.post("/api/providers/{pid}/models")
def api_provider_fetch_models(pid: str):
    """拉 /models 刷新该供应商模型清单；拉不到（None）则保留现有清单。"""
    try:
        got = providers.fetch_models(pid)
        if got:
            providers.update_provider(pid, {"models": got})
            return {"ok": True, "models": got, "from_api": True}
        return {"ok": False, "models": providers.get_models(pid), "from_api": False,
                "msg": "该供应商未开放 /models，请手动填写模型清单"}
    except providers.ProviderError as e:
        return _prov_err(e)

@app.get("/api/model_options")
def api_model_options():
    """给创作选择器的 供应商->模型 树（只列 active 供应商）。"""
    return {"default_provider": providers.load_data().get("default_provider"),
            "options": providers.active_model_options()}

@app.post("/api/providers")
def api_provider_add(payload: dict):
    try:
        p = providers.add_provider(payload)
        return {"ok": True, "provider": p}
    except providers.ProviderError as e:
        return _prov_err(e)

@app.put("/api/providers/{pid}")
def api_provider_update(pid: str, payload: dict):
    try:
        p = providers.update_provider(pid, payload)
        return {"ok": True, "provider": p}
    except providers.ProviderError as e:
        return _prov_err(e)

@app.delete("/api/providers/{pid}")
def api_provider_delete(pid: str):
    try:
        return {"ok": True, **providers.remove_provider(pid)}
    except providers.ProviderError as e:
        return _prov_err(e)

@app.post("/api/providers/{pid}/default")
def api_provider_set_default(pid: str):
    try:
        return {"ok": True, "default_provider": providers.set_default(pid)}
    except providers.ProviderError as e:
        return _prov_err(e)

@app.post("/api/providers/{pid}/active")
def api_provider_set_active(pid: str, payload: dict):
    try:
        p = providers.set_active(pid, payload.get("active", True))
        return {"ok": True, "provider": p}
    except providers.ProviderError as e:
        return _prov_err(e)

@app.post("/api/providers/{pid}/test")
def api_provider_test(pid: str, payload: dict = None):
    try:
        return providers.test_provider(pid, probe_chat=(payload or {}).get("probe_chat", True))
    except providers.ProviderError as e:
        return _prov_err(e)

@app.get("/api/debug_sessions")
def api_debug_sessions():
    return {k: {kk: vv for kk, vv in v.items() if kk != "cfg"} | {"cfg_keys": list((v.get("cfg") or {}).keys())}
            for k, v in SESSIONS.items()}

@app.get("/api/projects")
def api_projects():
    return writer.discover_projects()

@app.get("/api/status")
def api_status(key: str):
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    st = proj.status()
    st["events"] = proj.events[-15:]
    st["path"] = proj.path
    st["has_book"] = os.path.exists(os.path.join(proj.path, f"成书-{st.get('name', key)}.md"))
    return st

@app.post("/api/continue")
def api_continue(payload: dict):
    key = payload.get("key")
    try:
        extra = max(0, int(payload.get("extra") or 0))
    except (ValueError, TypeError):
        extra = 0
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    st = proj.status()
    if st.get("running"):
        return {"ok": True, "msg": f"《{proj.name}》正在创作中，请稍候。"}
    if extra > 0:
        # 续写：追加 extra 章（对已完成/未完成都适用），复用作品的默认供应商/模型
        writer.append_chapters(proj, extra)
        writer.start_writing(proj)
        return {"ok": True, "msg": f"《{proj.name}》续写 {extra} 章中…（已完成 {st.get('done')}/{st.get('total')} 章）"}
    if st.get("state") == "completed":
        return {"ok": True, "msg": f"《{proj.name}》已完成。想加章请在作品上点「▶ 续写」。"}
    writer.start_writing(proj)
    return {"ok": True, "msg": f"继续写作《{proj.name}》…"}

@app.get("/api/download")
def api_download(key: str):
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    plan = proj.plan() or {}
    book = os.path.join(proj.path, f"成书-{plan.get('novelName','')}.md") if plan.get("novelName") else ""
    if not os.path.exists(book):
        book = os.path.join(proj.path, "01-大纲.md")
    data = open(book, "rb").read()
    from urllib.parse import quote
    fname = os.path.basename(book)
    disp = f"attachment; filename=\"novel.md\"; filename*=UTF-8''{quote(fname)}"
    return Response(data, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": disp})

@app.get("/api/chapter")
def api_chapter(key: str, n: int):
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    plan = proj.plan()
    if not plan or n < 1 or n > len(plan["chapters"]):
        return JSONResponse({"error": "章节不存在"}, status_code=404)
    fp = os.path.join(proj.path, plan["chapters"][n - 1]["filePath"])
    if not os.path.exists(fp):
        return {"chapter": n, "text": ""}
    return {"chapter": n, "text": open(fp, encoding="utf-8").read()}

@app.get("/api/preview")
def api_preview(key: str):
    """预览：返回成书（或已写章节拼接）+ 章节清单。点开作品即可看内容。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    plan = proj.plan() or {}
    book_path = os.path.join(proj.path, f"成书-{plan.get('novelName', '')}.md")
    has_book = os.path.exists(book_path)
    if has_book:
        text = open(book_path, encoding="utf-8").read()
    else:
        parts = []
        for ch in plan.get("chapters", []):
            fp = os.path.join(proj.path, ch.get("filePath", ""))
            if os.path.exists(fp):
                parts.append(open(fp, encoding="utf-8").read())
        text = "\n\n---\n\n".join(parts)
    chapters = [{"n": c.get("chapterNumber"), "title": c.get("title"),
                 "status": c.get("status"), "words": c.get("wordCount"),
                 "continuation": bool(c.get("continuation"))}
                for c in plan.get("chapters", [])]
    return {"key": key, "name": plan.get("novelName", key), "has_book": has_book,
            "total_words": plan.get("totalWords", 0), "text": text, "chapters": chapters}

# ---------- 分层审稿（五角色 + 百分制）/ 版本 ----------
@app.post("/api/validate")
async def api_validate(payload: dict):
    key = payload.get("key")
    mode = str(payload.get("mode", "report"))
    max_rounds = int(payload.get("max_rounds", 2))
    proj = _get_project(key)
    if not proj:
        raise HTTPException(404, "项目不存在")
    st = proj.status()
    if st.get("state") not in ("idle", "completed", "failed"):
        raise HTTPException(409, "项目正在创作中，请完成后再审稿")
    def _job():
        writer.validate_project(proj, mode=mode, max_rounds=max_rounds)
    threading.Thread(target=_job, daemon=True).start()
    return {"status": "started", "key": key, "mode": mode}

@app.get("/api/review")
def api_review(key: str):
    """返回 13-修订记录.md（五角色审校产物）；无则空。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    fp = os.path.join(proj.path, "13-修订记录.md")
    return {"key": key, "text": open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""}

@app.get("/api/quality")
def api_quality(key: str):
    """返回 14-质量报告.md（确定性引擎 + 五角色 + 百分制达标判定）；无则空。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    fp = os.path.join(proj.path, "14-质量报告.md")
    return {"key": key, "text": open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""}

@app.get("/api/version")
async def api_version():
    return {"version": version.VE}
