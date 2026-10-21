"""神笔文学创作台 主应用：聊天即开写（小说 / 剧本 / 诗词 / 散文）"""
import json, os, re, threading, uuid, time
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from bot import llm, workflow, writer, providers, version, distill, learn, agents, skeleton
from bot import medium as medium_mod, skillkit, script_writer, poetry_writer, poetry_checker

ROOT = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="一幕·YIMU 神笔文学创作台")

# ---------- 会话状态机 ----------
# state: idle → options → planning(忙) → title → writing(后台) → done
SESSIONS = {}
PROJECT_INDEX = {}  # key -> writer.Project（后台写作项目）

def _get_project(key):
    if not isinstance(key, str) or not key:
        return None   # 缺 key / 非 str → 交调用方 404，不再 500（os.path.join(dir, None) 会 TypeError）
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
    _p = base.split("-", 2)
    prefix = "-".join(_p[:2]) + "-" if len(_p) >= 2 else base + "-"
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

def _load_base_cfg():
    p = os.path.join(ROOT, "config.json")
    try:
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
    except Exception:
        return {}

def _deep_merge(base, over):
    import copy
    if not isinstance(base, dict) or not isinstance(over, dict):
        return over if over is not None else base
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _deep_merge(out.get(k, {}), v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out

def _attach_style_ref(proj, style_id):
    """v0.3.4：把蒸馏风格卡写进该书 style_ref.json，供 _build_ctx 注入 craft。"""
    if not style_id:
        return
    card = distill.get_style(style_id)
    if not card:
        return
    try:
        open(os.path.join(proj.path, "style_ref.json"), "w", encoding="utf-8").write(
            json.dumps({"name": style_id, "style_prompt": card.get("style_prompt", ""),
                        "summary": card.get("summary", "")}, ensure_ascii=False, indent=2))
    except Exception:
        pass

@app.post("/api/create")
def api_create(payload: dict):
    """REST 直接开书。{title?, genre?, tone?, sells?, perspective?, idea?, total_chapters?, provider_id?, model?, cfg_overrides?}。
    带 title → 直接开写；不带 → 停在"选书名"（awaiting_title，不自动开写，返回 8 候选）。"""
    title = (payload.get("title") or "").strip()
    overrides = payload.get("cfg_overrides") or {}
    cfg = _deep_merge(_load_base_cfg(), overrides)
    if payload.get("genre"):
        cfg["genre"] = payload["genre"]
    if payload.get("tone"):
        cfg["tone"] = payload["tone"]
    if payload.get("style"):           # v0.3 P2：题材×风格技能 {金手指,基调,节奏,语言,卷}
        cfg["style"] = payload["style"]
    if payload.get("sells"):
        cfg["sells"] = payload["sells"]
    if payload.get("perspective"):
        cfg["perspective"] = payload["perspective"]
    idea = (payload.get("idea") or "").strip()
    if idea:
        cfg["theme"] = idea
    if title:
        cfg["theme"] = title
    if payload.get("provider_id"):
        cfg.setdefault("defaults", {})["provider_id"] = payload["provider_id"]
    if payload.get("model"):
        cfg.setdefault("defaults", {})["model"] = payload["model"]
    total = int(payload.get("total_chapters") or cfg.get("chapters") or (cfg.get("defaults") or {}).get("total_chapters") or 10)
    cfg["chapters"] = total
    style_id = (payload.get("style_id") or "").strip()   # v0.3.4：蒸馏风格 id → 注入该书 craft
    cfg = workflow.fill_defaults(cfg)
    try:
        n = int((cfg.get("defaults") or {}).get("title_count", 8))
        if title:
            titles = [{"title": title, "technique": "指定书名", "why": "用户指定"}] + workflow.gen_titles(cfg, n=n)
            proj = writer.create_project(cfg, titles)
            _attach_style_ref(proj, style_id)
            _st = cfg.get("style") or {}
            learn.record_book(_st.get("题材", ""), _st.get("金手指", ""), _st.get("节奏", ""), _st.get("语言", ""))
            writer.start_writing(proj)
            base = os.path.basename(proj.path)
            return {"project": base, "pid": base, "key": base, "awaiting_title": False}
        cands = workflow.gen_titles(cfg, n=n) or [{"title": "未命名", "technique": "", "why": ""}]
        proj = writer.create_project(cfg, cands)
        _attach_style_ref(proj, style_id)
        _st = cfg.get("style") or {}
        learn.record_book(_st.get("题材", ""), _st.get("金手指", ""), _st.get("节奏", ""), _st.get("语言", ""))
        plan = proj.plan(); meta = plan.get("meta") or {}
        meta["title_candidates"] = cands; meta["awaiting_title"] = True
        plan["meta"] = meta; plan["status"] = "awaiting_title"; proj.save_plan(plan)
        base = os.path.basename(proj.path)
        return {"project": base, "pid": base, "key": base, "awaiting_title": True, "candidates": cands}
    except Exception as e:
        return JSONResponse({"error": f"创建失败：{e}"}, status_code=500)

# ---------- v0.2：作品库管理 + 审查后重写 ----------
_REWRITING = {}  # key -> {"running":bool,"result":dict}

# ---------- v0.3.4：风格蒸馏引擎（粘贴文本 / 网址抓取 → 风格库）----------
@app.get("/api/styles")
def api_styles():
    return {"items": distill.list_styles()}

@app.get("/api/styles/{name}")
def api_style(name: str):
    c = distill.get_style(name)
    return c if c else JSONResponse({"error": "风格不存在"}, status_code=404)

@app.delete("/api/styles/{name}")
def api_style_delete(name: str):
    return distill.delete_style(name)

@app.post("/api/distill")
def api_distill(payload: dict):
    """{name, text?, urls?[], genre?}。粘贴 text 或抓 urls → 蒸馏成命名风格 → 存 styles/。"""
    name = re.sub(r'[\\/:*?"<>|\s]+', "", (payload.get("name") or "").strip())[:40]
    if not name:
        return JSONResponse({"error": "请给这个风格起个名字"}, status_code=400)
    corpus = (payload.get("text") or "").strip()
    urls = payload.get("urls") or []
    source = "粘贴文本"
    if not corpus and urls:
        parts = [t for t in (distill.fetch_url_text(u) for u in urls[:5]) if t]
        corpus = "\n".join(parts)
        source = "网址抓取: " + " ".join(str(u) for u in urls[:5])
        if len(corpus) < 300:
            return {"ok": False, "note": "网址抓到的正文太少（可能是 JS 渲染站或被反爬挡了）。请改用「粘贴文本」：把这部小说的几段内容复制进来再蒸馏。"}
    return distill.distill(corpus, name, (payload.get("genre") or "").strip(), source, cfg=payload.get("cfg"))

# ---------- v0.3.4：自我进化（学习报告）----------
@app.get("/api/learn")
def api_learn():
    """自我进化引擎报告：写过的题材分布、各题材偏好（金手指/节奏/语言）、平均成书质量。"""
    return learn.profile()
@app.get("/api/library")
def api_library(sort: str = "updated", order: str = "desc", genre: str = "",
                status: str = "", q: str = "", include_archived: str = "false"):
    if status == "archived":
        base = writer.library_all(include_archived=True)
        items = [i for i in base if i["archived"]]
    else:
        base = writer.library_all()          # 默认不含回收站
        items = base
        if genre:
            items = [i for i in items if i["genre"] == genre]
        if status == "passing":
            items = [i for i in items if i.get("passing") is True]
        elif status == "unrated":
            items = [i for i in items if i.get("quality") is None]
        elif status:
            items = [i for i in items if i["state"] == status or i["status_label"] == status]
        if q:
            ql = q.lower()
            items = [i for i in items if ql in (i["name"] or "").lower() or ql in (i["genre"] or "").lower()]
    stats = writer.library_stats(base)
    def _val(i):
        s = sort
        if s in ("created", "created_at"):
            return i.get("created_at") or ""
        if s in ("updated", "updated_at"):
            return i.get("updated_at") or i.get("created_at") or ""
        if s == "genre":
            return i.get("genre") or ""
        if s in ("progress", "completion"):
            return i.get("progress") or 0
        if s == "quality":
            return (i.get("quality") if i.get("quality") is not None else -1)
        if s in ("words", "total_words"):
            return i.get("total_words") or 0
        if s in ("chapters", "total"):
            return i.get("total") or 0
        if s == "status":
            return str(i.get("state"))
        return i.get("updated_at") or i.get("created_at") or ""
    items = sorted(items, key=_val, reverse=(order != "asc"))
    facets = {"genres": sorted({i["genre"] for i in base})}
    return {"items": items, "stats": stats, "facets": facets, "sort": sort, "order": order,
            "filters": {"genre": genre, "status": status, "q": q}}

@app.post("/api/library/{key}/favorite")
def api_lib_favorite(key: str, payload: dict):
    return writer.set_meta(key, favorite=bool(payload.get("on", True)))

@app.post("/api/library/{key}/tags")
def api_lib_tags(key: str, payload: dict):
    return writer.set_meta(key, tags=payload.get("tags") or [])

@app.post("/api/library/{key}/archive")
def api_lib_archive(key: str, payload: dict):
    on = bool(payload.get("on", True))
    writer.set_meta(key, archived=on)
    return {"ok": True, "archived": on}

@app.post("/api/library/{key}/rename")
def api_lib_rename(key: str, payload: dict):
    return writer.rename_book(key, (payload or {}).get("name", ""))

@app.delete("/api/library/{key}")
def api_lib_delete(key: str, hard: bool = False):
    """v0.3.3：软删=归档(可恢复)；?hard=true 彻底删除（删目录+清定时，不可逆）。"""
    return writer.delete_book(key, hard=bool(hard))

@app.post("/api/library/batch")
def api_lib_batch(payload: dict):
    keys = payload.get("keys") or []
    act = payload.get("action")
    out = []
    for k in keys:
        if act in ("archive", "delete"):
            writer.set_meta(k, archived=True); out.append({"key": k, "ok": True, "note": "已移入回收站"})
        elif act == "restore":
            writer.set_meta(k, archived=False); out.append({"key": k, "ok": True, "note": "已恢复"})
        elif act == "favorite":
            writer.set_meta(k, favorite=True); out.append({"key": k, "ok": True})
        else:
            out.append({"key": k, "ok": False, "note": f"未知操作 {act}"})
    return {"ok": True, "action": act, "results": out}

@app.get("/api/compare")
def api_compare(keys: str):
    ks = [k for k in keys.split(",") if k][:4]
    return {"items": [writer.library_item(k) for k in ks]}

@app.post("/api/rewrite-plan")
def api_rewrite_plan(payload: dict):
    key = payload.get("key")
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    cfg = (proj.plan() or {}).get("config") or {}
    brief = writer.rewrite_brief(proj, cfg)
    return brief

@app.post("/api/rewrite")
def api_rewrite(payload: dict):
    key = payload.get("key")
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    if _REWRITING.get(key, {}).get("running"):
        return {"ok": True, "running": True, "msg": "重写进行中，请稍候…"}
    _REWRITING[key] = {"running": True, "result": None}
    def _job():
        try:
            res = writer.run_guided_rewrite(key, chapters=payload.get("chapters"),
                                             dimensions=payload.get("dimensions"),
                                             max_rounds=int(payload.get("max_rounds") or 6))
            _REWRITING[key] = {"running": False, "result": res}
        except Exception as e:
            _REWRITING[key] = {"running": False, "result": {"error": str(e)}}
    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "running": True, "msg": "🔁 审查后定向重写中…（约 2-5 分钟，把要求书逐条带进目标章）"}

@app.get("/api/rewrite-progress")
def api_rewrite_progress(key: str):
    """v0.7 R2：重写 job 逐章实时进度（前端进度板轮询；无 job 时返回空结构）。"""
    p = writer.rewrite_progress(key)
    return p or {"running": False, "total": 0, "done": 0, "cancelled": False, "per_chapter": [], "result": None}

@app.post("/api/rewrite-cancel")
def api_rewrite_cancel(payload: dict):
    """v0.7 R2：取消该书重写（下一检查点停，已写章保留）。"""
    key = payload.get("key")
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    writer.cancel_rewrite(key)
    _REWRITING[key] = {"running": False, "result": {"cancelled": True}}
    return {"ok": True, "msg": "已请求取消（下一检查点停，已写章保留）"}

@app.get("/api/book-live")
def api_book_live(key: str):
    """v0.7 R4：对话区「本书在干什么」live 卡数据源（纯读、无 LLM、按书隔离）。
    聚合 阶段/当前章/进度/实时日志，前端 1.5s 轮询原地刷新（Hermes 式 live 工具步骤）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    st = proj.status()
    plan = proj.plan() or {}
    chs = plan.get("chapters") or []
    inprog = [c.get("chapterNumber") for c in chs if c.get("status") == "in_progress"]
    cur_ch = (max(inprog) if inprog else min(st.get("done", 0) + 1, st.get("total", 0)))
    par = _PARALLEL_RUNNING.get(key, {}).get("running")
    rw = _REWRITING.get(key, {}).get("running")
    rel = _RELEASE_RUNNING.get(key, {}).get("running")
    osh = _ONESHOT_RUNNING.get(key, {}).get("running")
    state = st.get("state")
    if osh:
        phase = "one_shot"
    elif par:
        phase = "parallel"
    elif rw:
        phase = "rewriting"
    elif rel or state in ("validating", "releasing"):
        phase = "releasing"
    elif st.get("running") and state in ("in_progress", "unknown"):
        phase = "writing"
    elif state == "completed":
        phase = "done"
    elif state == "failed":
        phase = "failed"
    else:
        phase = "idle"
    total = st.get("total", 0) or 0
    done = st.get("done", 0) or 0
    # R4：live_log 优先持久化 运行日志.txt（跨重启可用），无则内存 events
    live = []
    try:
        lf = os.path.join(proj.path, "运行日志.txt")
        if os.path.exists(lf):
            live = open(lf, encoding="utf-8").read().splitlines()[-8:]
    except Exception:
        live = []
    if not live:
        live = (proj.events or [])[-8:]
    return {
        "key": key, "name": st.get("name"), "phase": phase, "state": state,
        "current_chapter": cur_ch,
        "progress": {"done": done, "total": total, "pct": round(done / total * 100, 1) if total else 0},
        "running": bool(st.get("running") or par or rw or rel),
        "paused": st.get("paused"), "parallel": bool(par), "rewriting": bool(rw), "releasing": bool(rel),
        "live_log": live,
        "updated_at": int(time.time()),
    }

@app.post("/api/cmd")
def api_cmd(payload: dict):
    """v0.5 M3：把中间台自然语言指令解析成结构化动作，前端照做（改写某段/某章/全书/看评分/聊天）。"""
    key = payload.get("key"); text = (payload.get("text") or "").strip()
    out = {"key": key, "text": text}
    if not text:
        out["action"] = "chat"; return out
    mch = re.search(r"第\s*(\d+)\s*章", text)
    chap = int(mch.group(1)) if mch else 0
    if re.search(r"全书|整本|全部.{0,4}重写|重写全书|重写整本", text):
        out["action"] = "rewrite_all"
    elif re.search(r"看评分|评分表|各章评分|每章.{0,3}分|评分怎么样", text):
        out["action"] = "scores"
    elif re.search(r"段", text) and chap:
        out["action"] = "rewrite_section"; out["chapter"] = chap
    elif chap:
        out["action"] = "rewrite_chapter"; out["chapter"] = chap
    elif re.search(r"重写|改写", text):
        out["action"] = "rewrite_default"
    else:
        out["action"] = "chat"
    return out

@app.post("/api/rewrite-section")
def api_rewrite_section(payload: dict):
    """v0.5 M3：改写某一段（正文里的一个自然段）。match=定位文本片段 或 para=第N段；instruction=改写要求。"""
    key = payload.get("key")
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    ch_no = int(payload.get("chapter") or 0)
    match = (payload.get("match") or "").strip()
    para = int(payload.get("para") or 0)
    instr = payload.get("instruction") or "把这一段写得更抓人，去掉 AI 腔，加细节与张力"
    plan = proj.plan()
    ch = next((c for c in plan.get("chapters", []) if c.get("chapterNumber") == ch_no), None)
    if not ch:
        return JSONResponse({"error": f"第{ch_no}章不存在"}, status_code=404)
    fp = os.path.join(proj.path, ch["filePath"])
    body = open(fp, encoding="utf-8").read()
    mm = re.search(r"(##\s*正文[^\n]*\n)(.*?)(\n##|\Z)", body, re.S)
    sec = mm.group(2) if mm else body
    paras = [p for p in re.split(r"\n\s*\n", sec) if p.strip()]
    target = None
    if match:
        target = next((p for p in paras if match in p), None)
    elif 1 <= para <= len(paras):
        target = paras[para - 1]
    if not target:
        return JSONResponse({"error": "未定位到该段（试试给更多定位文字或段落序号）"}, status_code=404)
    cfg = plan.get("config") or {}
    new = workflow.guided_rewrite(target, instr, "全书约束：保持人物/设定一致，只改这一段", cfg)
    new = (new or "").strip()
    if new:
        open(fp, "w", encoding="utf-8").write(body.replace(target, new, 1))
        proj.log(f"✒️ 第{ch_no}章改写一段（{instr[:24]}…）")
    return {"ok": True, "chapter": ch_no, "orig": target, "new": new}

@app.get("/api/rewrite-log")
def api_rewrite_log(key: str):
    p = os.path.join(writer.PROJECTS_DIR, key, "16-重写记录.md")
    w = _REWRITING.get(key, {})
    return {"running": bool(w.get("running")), "result": w.get("result"),
            "text": open(p, encoding="utf-8").read() if os.path.exists(p) else ""}

# ---------- v0.2.1：开书向导 / 书名选 / 放行门禁 ----------
# v0.3 P2：题材×风格技能目录（16 题材 × 风格维度：金手指/基调/视角/节奏/语言/卷）
_ONB = {
    "genres": ["都市", "玄幻", "重生", "系统流", "穿越", "历史/民国", "军旅", "悬疑/刑侦", "末日", "科幻", "游戏", "职场", "现言", "古言/宫斗", "快穿", "恐怖/灵异"],
    "tones": ["爽文", "虐", "轻松", "暗黑", "复仇", "燃"],
    "sells": ["打脸", "逆袭", "反转", "金手指", "系统", "复仇", "升级", "打怪", "甜宠", "燃"],
    "perspectives": ["男主", "女主", "不限", "第一人称"],
    "chapter_options": [3, 5, 10, 20, 50],
    # v0.3 P2 风格技能维度（每项可填"自定义"）
    "style": {
        "金手指": ["系统面板", "重生预知", "空间/储物", "马甲扮猪吃虎", "悟性/顿悟", "无(纯剧情)", "自定义"],
        "基调": ["爽", "虐心", "热血", "轻松沙雕", "暗黑复仇", "治愈"],
        "节奏": ["快节奏(3章1转折)", "中节奏", "慢热(前3章铺垫)", "自定义"],
        "语言": ["口语爽利", "文白夹杂", "硬核细节", "轻松幽默", "克制内敛", "自定义"],
        "卷": ["单卷(10章)", "多卷(3卷各10)", "长篇连载", "自定义"],
        # 默认技能（题材 → 推荐风格组合，可覆盖）
        "preset": {
            "都市": {"金手指": "马甲扮猪吃虎", "基调": "爽", "节奏": "快节奏(3章1转折)", "语言": "口语爽利", "卷": "多卷(3卷各10)"},
            "重生": {"金手指": "重生预知", "基调": "爽", "节奏": "快节奏(3章1转折)", "语言": "口语爽利", "卷": "多卷(3卷各10)"},
            "系统流": {"金手指": "系统面板", "基调": "燃", "节奏": "中节奏", "语言": "轻松幽默", "卷": "多卷(3卷各10)"},
            "玄幻": {"金手指": "悟性/顿悟", "基调": "热血", "节奏": "中节奏", "语言": "硬核细节", "卷": "多卷(3卷各10)"},
            "穿越": {"金手指": "重生预知", "基调": "轻松沙雕", "节奏": "慢热(前3章铺垫)", "语言": "文白夹杂", "卷": "多卷(3卷各10)"},
            "悬疑/刑侦": {"金手指": "无(纯剧情)", "基调": "暗黑复仇", "节奏": "中节奏", "语言": "硬核细节", "卷": "单卷(10章)"},
            "古言/宫斗": {"金手指": "马甲扮猪吃虎", "基调": "爽", "节奏": "中节奏", "语言": "文白夹杂", "卷": "多卷(3卷各10)"},
            "快穿": {"金手指": "系统面板", "基调": "爽", "节奏": "快节奏(3章1转折)", "语言": "轻松幽默", "卷": "单卷(10章)"},
        },
    },
}

@app.get("/api/onboarding")
def api_onboarding():
    d = writer._cfg_defaults()
    _ONB["defaults"] = {"release_gate": d.get("release_gate", True),
                        "pass_line": (d.get("validation") or {}).get("pass_line", 90),
                        "max_release_rounds": d.get("max_release_rounds", 3),
                        "title_count": d.get("title_count", 8),
                        "p5_pipeline": d.get("p5_pipeline", True)}
    return _ONB

def _plan_genre(proj):
    """从 02-写作计划.json 安全取 genre（brief/meta/顶层 任一），不存在返回 ''。"""
    plan = proj.plan() or {}
    brief = plan.get("brief") or {}
    meta = plan.get("meta") or {}
    return brief.get("genre") or plan.get("genre") or meta.get("genre") or ""

@app.get("/api/style")
def api_style_get(key: str):
    """v0.3 P2：取某书的题材×风格技能（style.json）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    p = os.path.join(proj.path, "style.json")
    if os.path.exists(p):
        try:
            return {"key": key, "style": json.load(open(p, encoding="utf-8"))}
        except Exception:
            pass
    return {"key": key, "style": {"genre": _plan_genre(proj), "style": {}}}

@app.put("/api/style")
def api_style_put(payload: dict):
    """v0.3 P2：改某书的题材×风格技能 → 写 style.json（后续 craft 注入用）。"""
    key = payload.get("key") or ""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    st = {"genre": _plan_genre(proj) or payload.get("genre") or "",
          "style": payload.get("style") or {}}
    open(os.path.join(proj.path, "style.json"), "w", encoding="utf-8").write(json.dumps(st, ensure_ascii=False, indent=2))
    return {"ok": True, "style": st}

# ---------- v0.3 P3：定时任务 调度器 + /api/schedule（默认关；每 60s 巡检到期批次） ----------
@app.get("/api/schedule")
def api_sched_list():
    return {"schedules": writer.load_schedules()}

@app.post("/api/schedule")
def api_sched_set(payload: dict):
    key = payload.get("key") or ""
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    d = writer.load_schedules(); now = time.time()
    cur = d.get(key, {})
    enabled = bool(payload.get("enabled", cur.get("enabled", False)))
    iv = int(payload.get("interval_days", cur.get("interval_days", 7)))
    per = int(payload.get("per_run", cur.get("per_run", 10)))
    d[key] = {"enabled": enabled, "interval_days": iv, "per_run": per,
              "next_run": (now + iv * 86400) if enabled else cur.get("next_run"),
              "last_run": cur.get("last_run"), "last_note": cur.get("last_note")}
    writer.save_schedules(d)
    return {"ok": True, "schedules": d}

def _scheduler_tick():
    d = writer.load_schedules(); now = time.time(); changed = False
    for key, s in list(d.items()):
        if not s.get("enabled"):
            continue
        if s.get("next_run") and now < float(s["next_run"]):
            continue
        proj = _get_project(key)
        if not proj:
            continue
        try:
            plan = proj.plan()
        except Exception:
            continue
        if plan.get("status") == "completed":
            s["enabled"] = False; s["last_note"] = "已成书，自动停用"; changed = True; continue
        if getattr(proj, "running", False):
            continue
        n = int(s.get("per_run", 10))
        if writer.start_writing_batch(proj, n):
            s["next_run"] = now + int(s.get("interval_days", 7)) * 86400
            s["last_run"] = now; s["last_note"] = f"⏱ 定时写 {n} 章"; changed = True
    if changed:
        writer.save_schedules(d)

def _scheduler_loop():
    while True:
        try:
            _scheduler_tick()
        except Exception:
            pass
        time.sleep(60)

@app.on_event("startup")
def _start_scheduler():
    threading.Thread(target=_scheduler_loop, daemon=True).start()

# ---------- v0.3 P4：模型规则引擎 /api/model_rules ----------
@app.get("/api/model_rules")
def api_model_rules():
    from bot import model_rules
    try:
        prov, model = providers.resolve()
    except Exception:
        prov, model = {}, ""
    prov = dict(prov or {}); prov.setdefault("model", model)
    rules = model_rules.effective_rules(prov, model_rules.load_overrides())
    return {"vendor": model_rules.detect_vendor(prov.get("base_url", "")), "rules": rules,
            "provider": {"id": prov.get("id"), "name": prov.get("name"), "model": prov.get("model")},
            "overrides": model_rules.load_overrides()}

@app.post("/api/model_rules")
def api_model_rules_set(payload: dict):
    """通用/自由配置：存 override（context/max_tokens/rpm/tpm/backoff_base/call_gap/extra_body）。"""
    from bot import model_rules
    ovr = model_rules.load_overrides()
    ovr.update({k: v for k, v in payload.items() if v not in (None, "")})
    model_rules.save_overrides(ovr)
    return {"ok": True, "overrides": ovr}

@app.get("/api/usage")
def api_usage(key: str = ""):
    """v0.3.x：token 账本。带 key=单本；不带=全局合计。"""
    gpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_global.json")
    g = {}
    if os.path.exists(gpath):
        try:
            g = json.load(open(gpath, encoding="utf-8"))
        except Exception:
            g = {}
    if key:
        proj = _get_project(key)
        if not proj:
            return JSONResponse({"error": "项目不存在"}, status_code=404)
        p = os.path.join(proj.path, "usage.json")
        u = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"total_tokens": 0, "calls": 0, "total_prompt": 0, "total_completion": 0}
        return {"key": key, "usage": u, "global": g}
    return {"key": "", "usage": {}, "global": g}

@app.get("/api/titles")
def api_titles(key: str):
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    plan = proj.plan() or {}
    cands = (plan.get("meta") or {}).get("title_candidates") or []
    if not cands:
        cands = workflow.gen_titles(plan.get("config") or {}, int(writer._cfg_defaults().get("title_count", 8)))
    return {"key": key, "candidates": cands, "selected": (plan.get("meta") or {}).get("selected_title")}

@app.post("/api/pick-title")
def api_pick_title(payload: dict):
    key = payload.get("key"); title = (payload.get("title") or "").strip()
    proj = _get_project(key)
    if not proj or not title:
        return JSONResponse({"error": "项目或书名无效"}, status_code=400)
    newbase = writer.pick_title(key, title)
    PROJECT_INDEX.pop(key, None)
    return {"ok": True, "key": newbase, "title": title}

_RELEASE_RUNNING = {}

@app.post("/api/release")
def api_release(payload: dict):
    key = payload.get("key")
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    if _RELEASE_RUNNING.get(key):
        return {"ok": True, "running": True, "msg": "放行门禁进行中…"}
    _RELEASE_RUNNING[key] = True
    def _job():
        try:
            res = writer.run_release_gate(key)
            _RELEASE_RUNNING[key] = res
        except Exception as e:
            _RELEASE_RUNNING[key] = {"error": str(e)}
    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "running": True, "msg": "🔓 放行门禁中（自动带要求重写到 ≥90，约几分钟）…"}

@app.get("/api/release-log")
def api_release_log(key: str):
    p = os.path.join(writer.PROJECTS_DIR, key, "14-质量报告.md")
    r = _RELEASE_RUNNING.get(key)
    running = isinstance(r, bool) and r
    res = (r if (isinstance(r, dict) and "before" in r) else None)
    gate = ""
    if os.path.exists(p):
        t = open(p, encoding="utf-8").read()
        i = t.find("## 放行判定")
        gate = t[i:] if i >= 0 else ""
    return {"running": running, "result": res, "gate": gate}

@app.post("/api/release-cancel")
def api_release_cancel(payload: dict):
    return writer.cancel_release_gate(payload.get("key"))

# ---------- v0.4：暂停 / 继续 / 读者即时指令（像 Agent 聊天一样随时控）----------
@app.post("/api/pause")
def api_pause(payload: dict):
    key = payload.get("key")
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    ok = writer.pause_book(key)
    return {"ok": ok, "msg": "⏸ 已暂停（下一检查点停，停在当前进度）" if ok else "无法暂停"}

@app.post("/api/resume")
def api_resume(payload: dict):
    key = payload.get("key")
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    ok = writer.resume_book(key)
    return {"ok": ok, "msg": "▶ 继续写作中（接着写剩余章节）…" if ok else "无法继续"}

@app.post("/api/queue")
def api_queue(payload: dict):
    key = payload.get("key")
    text = (payload.get("text") or "").strip()
    immediate = bool(payload.get("immediate"))
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    ok = writer.queue_msg(key, text, immediate)
    return {"ok": ok, "items": writer.pending_queue(key)}

@app.get("/api/queue")
def api_queue_get(key: str):
    return {"items": writer.pending_queue(key)}


@app.post("/api/queue/action")
def api_queue_action(payload: dict):
    key = payload.get("key")
    try:
        index = int(payload.get("index", 0))
    except (ValueError, TypeError):
        index = 0
    action = payload.get("action", "")
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    items = writer.queue_action(key, index, action)
    return {"ok": True, "items": items, "msg": "已移除" if action == "remove" else "已标记立即生效"}


# ===== v0.4.1：每本书独立的对话记录（会话隔离，点击书只显示它自己的聊天）=====
def _bkchat_path(key: str):
    return os.path.join(writer.PROJECTS_DIR, key, "chat.json")


@app.get("/api/bkchat")
def api_bkchat_get(key: str = ""):
    if not key:
        return {"messages": []}
    p = _bkchat_path(key)
    if not os.path.exists(p):
        return {"messages": []}
    try:
        return {"messages": json.loads(open(p, encoding="utf-8").read())}
    except Exception:
        return {"messages": []}


@app.post("/api/bkchat")
def api_bkchat_post(payload: dict):
    key = payload.get("key", ""); who = payload.get("who", "user")
    text = (payload.get("text") or "").strip()
    if not key or not text:
        return {"ok": False}
    if not _get_project(key):
        return JSONResponse({"ok": False, "error": "项目不存在"}, status_code=404)
    p = _bkchat_path(key)
    msgs = []
    if os.path.exists(p):
        try:
            msgs = json.loads(open(p, encoding="utf-8").read())
        except Exception:
            msgs = []
    msgs.append({"who": who, "text": text, "ts": int(time.time() * 1000)})
    # 每本书只保留最近 400 条，防止无限膨胀
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(json.dumps(msgs[-400:], ensure_ascii=False))
    return {"ok": True, "count": len(msgs)}

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
    text = open(book, encoding="utf-8").read()
    meta = (plan.get("meta") or {})
    if "gate_rounds" in meta or meta.get("released") is not None:
        pl = int(((plan.get("config") or {}).get("defaults") or {}).get("validation", {}).get("pass_line", 90))
        fq = meta.get("final_quality")
        if meta.get("released"):
            text = f"> ✅ **已放行**（质量 {fq}/{pl}，达到放行线，可出书）\n\n" + text
        else:
            text = f"> ⚠️ **beta·未放行**（质量 {fq}/{pl}，未达放行线，仅供内部阅读）\n\n" + text
    from urllib.parse import quote
    fname = os.path.basename(book)
    disp = f"attachment; filename=\"novel.md\"; filename*=UTF-8''{quote(fname)}"
    return Response(text.encode("utf-8"), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": disp})

@app.get("/api/agents")
def api_agents():
    """v0.5 M4：多 Agent 宇宙 —— 各岗位当前模型绑定 + 可选模型树 + 全局并发上限。"""
    return agents.view()

@app.post("/api/agents-set")
def api_agents_set(payload: dict):
    """v0.5 M4：给某子 Agent 单独配 provider/model（留空=回落 stage/默认）。"""
    am = agents.set_binding(payload.get("agent"), payload.get("provider_id"), payload.get("model"))
    return {"ok": True, "agent_models": am}

@app.post("/api/agents-maxglobal")
def api_agents_maxglobal(payload: dict):
    """v0.5 M4：设全局并发上限（多 Agent/多书同时创作时的在飞上限）。"""
    return {"ok": True, "max_global_agents": agents.set_max_global(int(payload.get("n", 4)))}

@app.post("/api/agents-dispatch")
def api_agents_dispatch(payload: dict):
    """v0.5 M4：批量并发创作多本书（每书一线程同步跑完，LLM 并发受 ConcurrencyGuard 限流排队）。"""
    d = payload
    keys = d.get("keys") or []
    if not keys:
        return JSONResponse({"ok": False, "error": "请至少选一本书"}, status_code=400)
    def _get(k):
        try:
            p = writer.Project(writer._project_dir(k), k)
        except Exception:
            p = None
        if not p or not p.plan():
            raise Exception(f"《{k}》不存在或未创建")
        return p
    res = agents.dispatch_write(keys[:8], _get, lambda p: writer._write_loop(p))
    return {"ok": True, "results": res}

# ---------- v0.6 B：并行章节创作（单本书内 N 个 Agent 分章齐写 + 总架构对齐） ----------
_PARALLEL_RUNNING = {}

@app.post("/api/parallel-write")
def api_parallel_write(payload: dict):
    """v0.6 B：单本书内 N 个 worker 分章齐写（先熔炼总架构，写完一个立即取下一章）。后台线程跑，立即返回。"""
    key = payload.get("key")
    nworkers = max(1, min(8, int(payload.get("workers") or 5)))
    proj = _get_project(key)
    if not proj or not proj.plan():
        return JSONResponse({"ok": False, "error": "项目不存在或未创建"}, status_code=404)
    if _PARALLEL_RUNNING.get(key, {}).get("running"):
        return {"ok": True, "running": True, "msg": "该书并行创作进行中…"}
    _PARALLEL_RUNNING[key] = {"running": True, "result": None, "workers": nworkers}
    def _job():
        try:
            writer._write_loop_parallel(proj, n_workers=nworkers)
            _PARALLEL_RUNNING[key] = {"running": False, "result": "ok", "workers": nworkers}
        except Exception as e:
            _PARALLEL_RUNNING[key] = {"running": False, "result": {"error": str(e)}, "workers": nworkers}
    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "running": True, "workers": nworkers,
            "msg": f"⚡ 并行创作中：先熔炼总架构，{nworkers} 个 Agent 分章齐写（写完一个立即取下一章）"}

@app.get("/api/parallel-log")
def api_parallel_log(key: str):
    """v0.6 B：该书并行创作进度（是否仍在跑 + 结果）。"""
    return {"running": bool(_PARALLEL_RUNNING.get(key, {}).get("running")),
            "result": _PARALLEL_RUNNING.get(key, {}).get("result")}

# ---------- v0.7 R5：一键完稿（小白）+ 工作室总览（重度） ----------
_ONESHOT_RUNNING = {}

@app.post("/api/one-shot")
def api_one_shot(payload: dict):
    """v0.7 R5 一键完稿：熔炼总架构 → 写剩余章节 → 逐章评分 → 不达标自动重写(保留更好守护) → 放行门禁。
    全程后台跑，实时进度看 /api/book-live「本书在干什么」活动卡。auto_rewrite/auto_release 可关。"""
    key = payload.get("key"); proj = _get_project(key)
    if not proj or not proj.plan():
        return JSONResponse({"ok": False, "error": "项目不存在或未创建"}, status_code=404)
    if _ONESHOT_RUNNING.get(key, {}).get("running"):
        return {"ok": True, "running": True, "msg": "✨ 一键完稿进行中（看对话区本书活动卡）"}
    auto_rw = bool(payload.get("auto_rewrite", True)); auto_rel = bool(payload.get("auto_release", True))
    _ONESHOT_RUNNING[key] = {"running": True}
    def _job():
        try:
            writer.one_shot_begin(key)   # v0.7：期间 成书 不再二次自动门控
            proj.log("✨ 一键完稿 ① 熔炼总架构…")
            try: writer.ensure_blueprint(proj)
            except Exception as e: proj.log("⚠️ 总架构熔炼失败(继续)：" + str(e))
            proj.log("✨ ② 写剩余章节…")
            try: writer._write_loop(proj)
            except Exception as e: proj.log("⚠️ 写章节中断：" + str(e))
            proj.log("✨ ③ 逐章评分…")
            for c in (proj.plan().get("chapters") or []):
                try: writer.score_chapter(proj, c.get("chapterNumber"))
                except Exception: pass
            below = [x["ch"] for x in writer.per_chapter_scores(proj) if x.get("status") == "below"]
            if auto_rw and below:
                proj.log(f"✨ ④ 不达标自动重写（{len(below)} 章，含『保留更好』守护）…")
                try: writer.run_guided_rewrite(key, chapters=below, max_rounds=2)
                except Exception as e: proj.log("⚠️ 自动重写中断：" + str(e))
            elif auto_rw:
                proj.log("✨ ④ 全部达标，跳过自动重写")
            if auto_rel:
                proj.log("✨ ⑤ 放行门禁…")
                try: writer.run_release_gate(key)
                except Exception as e: proj.log("⚠️ 放行门禁中断：" + str(e))
            _ONESHOT_RUNNING[key] = {"running": False, "result": "ok"}
            proj.log("✅ 一键完稿完成")
        except Exception as e:
            _ONESHOT_RUNNING[key] = {"running": False, "result": str(e)}
            proj.log("⚠️ 一键完稿中断：" + str(e))
        finally:
            writer.one_shot_end(key)   # v0.7：释放驱动标记（无论成败，恢复成书自动门控）
    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "running": True,
            "msg": "✨ 一键完稿启动：熔炼→写剩余→逐章评分→不达标自动重写→放行门禁（全程看对话区「本书在干什么」活动卡）"}

@app.get("/api/studio-overview")
def api_studio_overview():
    """v0.7 R5 工作室总览（重度）：所有书 mini 进度 + 运行态（并行/重写/放行/一键），像任务看板。"""
    items = []
    for it in writer.library_all():
        k = it.get("key")
        items.append({
            "key": k, "name": it.get("name"), "state": it.get("state"),
            "done": it.get("done"), "total": it.get("total"), "quality": it.get("quality"),
            "archived": it.get("archived"),
            "running": bool(_PARALLEL_RUNNING.get(k, {}).get("running") or _REWRITING.get(k, {}).get("running")
                          or _RELEASE_RUNNING.get(k, {}).get("running") or _ONESHOT_RUNNING.get(k, {}).get("running")),
        })
    active = [i for i in items if i["running"]]
    return {"books": items, "active_count": len(active)}

@app.get("/api/blueprint")
def api_blueprint(key: str):
    """v0.6 B：取某书『总架构』（02b-总架构）；未生成则给规则式即时预览（不落盘）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    jpath = os.path.join(proj.path, "02b-总架构.json")
    mdpath = os.path.join(proj.path, "02b-总架构.md")
    bp = None
    if os.path.exists(jpath):
        try:
            bp = json.load(open(jpath, encoding="utf-8"))
        except Exception:
            bp = None
    out = {"key": key, "exists": bool(bp), "blueprint": bp,
           "md": open(mdpath, encoding="utf-8").read() if os.path.exists(mdpath) else ""}
    if not bp:
        try:
            out["blueprint"] = writer._rule_blueprint(proj, proj.plan() or {})
        except Exception:
            out["blueprint"] = None
    return out

@app.get("/api/skeleton")
def api_skeleton(key: str, regen: int = 0):
    """v0.9.0：该书『写作骨架』（17-写作骨架.md）——写到哪/多少/重点/为何/续写提示。
    纯文件聚合，不花 LLM；regen=1 立即重新生成（也可由写作/重写/放行自动刷新）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    if regen:
        try:
            skeleton.build(key)
        except Exception:
            pass
    md = skeleton.read(key, cap=8000)
    meta = (writer.library_item(key, with_quality=False) or {})
    return {"key": key, "exists": bool(md.strip()), "md": md,
            "updated": meta.get("skeleton") or (proj.plan() or {}).get("meta", {}).get("skeleton"),
            "skeleton_file": "17-写作骨架.md"}

@app.post("/api/skeleton")
def api_skeleton_regen(payload: dict):
    """v0.9.0：手动刷新写作骨架。"""
    key = payload.get("key")
    if not key or not _get_project(key):
        return JSONResponse({"ok": False, "error": "项目不存在"}, status_code=404)
    try:
        p = skeleton.build(key)
        return {"ok": True, "key": key, "path": p, "md": skeleton.read(key, cap=8000) if p else ""}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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

def _preview_pass_line(plan):
    try:
        return int(((plan.get("config") or {}).get("defaults") or {}).get("validation", {}).get("pass_line", 90))
    except (ValueError, TypeError):
        return 90

@app.get("/api/preview")
def api_preview(key: str):
    """预览：返回成书（或已写章节拼接）+ 章节清单 + 逐章评分。点开作品即可看内容。"""
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
    # v0.7.x：逐章原文（按章序，供对话区「逐章直播打字机」一章一条气泡）——纯读文件，不烧 LLM
    chapters_full = []
    for c in plan.get("chapters", []):
        fp = os.path.join(proj.path, c.get("filePath", ""))
        if fp and os.path.exists(fp):
            try:
                chapters_full.append({"n": c.get("chapterNumber"), "title": c.get("title"),
                                      "status": c.get("status"), "text": open(fp, encoding="utf-8").read()})
            except Exception:
                pass
    # v0.5 M1：逐章评分（meta.chapter_scores）挂到每章 + 单独字段
    cs = (plan.get("meta") or {}).get("chapter_scores", {}) or {}
    pl = _preview_pass_line(plan)
    for c in chapters:
        rec = cs.get(str(c["n"])) or {}
        c["score"] = rec.get("score"); c["weak"] = rec.get("weak", []); c["ch_status"] = rec.get("status", "unscored")
    per_chapter = [{"ch": int(k), "score": v.get("score"), "weak": v.get("weak", []),
                    "status": v.get("status", "unscored"), "verdict": v.get("verdict", "")}
                   for k, v in sorted(cs.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 9999)]
    return {"key": key, "name": plan.get("novelName", key), "has_book": has_book,
            "total_words": plan.get("totalWords", 0), "text": text, "chapters": chapters,
            "chapters_full": chapters_full, "per_chapter": per_chapter, "pass_line": pl}

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

# ---------- v1.0.0：文体路由 + 技能库（神笔文学创作台） ----------

@app.get("/api/mediums")
async def api_mediums():
    """四种文体的注册表：阶段表 / 评估维度 / 目录规范，前端据此渲染选择器。"""
    return {"mediums": medium_mod.registry()}


@app.get("/api/medium/detect")
async def api_medium_detect(text: str = ""):
    """从一段文本判定文体（给对话入口用）。"""
    m = medium_mod.detect(text)
    return {"medium": m, "label": medium_mod.label(m) if m else "", "icon": medium_mod.icon(m) if m else ""}


@app.get("/api/skillkit/stats")
async def api_skillkit_stats():
    """技能库统计：文件数 / 体量 / 分布。"""
    return skillkit.stats()


@app.get("/api/skillkit/list")
async def api_skillkit_list(medium: str = "", stage: str = ""):
    """技能清单（可按文体 / 阶段过滤）。"""
    return {"skills": skillkit.list_skills(medium, stage)}


@app.get("/api/skillkit/preview")
async def api_skillkit_preview(stage: str, medium: str = "novel", genre: str = "",
                               include_case: bool = False, max_bytes: int = 26000):
    """预览某阶段的最小上下文注入内容（调试用：不调 LLM）。"""
    if not medium_mod.is_medium(medium):
        return JSONResponse({"error": f"未知文体：{medium}"}, status_code=400)
    ctx = skillkit.stage_inject(stage, medium, include_case=include_case,
                                max_bytes=max_bytes, genre=genre)
    return {"stage": stage, "medium": medium, "genre": genre,
            "bytes": len(ctx), "context": ctx}


# ---------- v1.0.0：剧本流水线 ----------

@app.get("/api/script/projects")
async def api_script_projects():
    """剧本项目列表。"""
    return {"items": script_writer.discover_projects()}


@app.post("/api/script/create")
async def api_script_create(payload: dict):
    """建剧本项目。payload: {title?, idea?, form?, entryPath?, scenes?, pages?, episodes?, notes?, cfg_overrides?}"""
    cfg = dict(payload.get("cfg_overrides") or {})
    for k in ("title", "idea", "form", "entryPath", "scenes", "pages", "episodes", "notes"):
        if payload.get(k) is not None:
            cfg[k] = payload[k]
    cfg.setdefault("form", "film")
    if payload.get("provider_id"):
        cfg.setdefault("defaults", {})["provider_id"] = payload["provider_id"]
    if payload.get("model"):
        cfg.setdefault("defaults", {})["model"] = payload["model"]
    try:
        proj = script_writer.create_project(cfg)
    except Exception as e:
        return JSONResponse({"error": f"创建失败：{e}"}, status_code=500)
    base = os.path.basename(proj.path)
    return {"key": base, "pid": base, "project": base, "medium": "script",
            "status": proj.status()}


@app.get("/api/script/status")
async def api_script_status(key: str):
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return proj.status()


@app.get("/api/script/bible")
async def api_script_bible(key: str):
    """创作圣经（S0 引擎 + 圣经头）。"""
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return {"key": key, "text": script_writer.read_file_part(key, "bible")}


@app.get("/api/script/chars")
async def api_script_chars(key: str):
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return {"key": key, "text": script_writer.read_file_part(key, "chars")}


@app.get("/api/script/struct")
async def api_script_struct(key: str):
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return {"key": key, "text": script_writer.read_file_part(key, "struct")}


@app.get("/api/script/scenes")
async def api_script_scenes(key: str):
    """场次清单（表格 + 解析后的结构化场次）。"""
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    plan = proj.plan() or {}
    return {"key": key, "text": script_writer.read_file_part(key, "scenes"),
            "scenes": plan.get("scenes") or []}


@app.get("/api/script/scene")
async def api_script_scene(key: str, no: int):
    """单场正文。"""
    return {"key": key, "no": no, "text": script_writer.read_scene(key, no)}


@app.post("/api/script/write")
async def api_script_write(payload: dict):
    """按场次清单写正文。payload: {key, only?: [场次号]}"""
    key = payload.get("key")
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    try:
        return script_writer.write_scenes(proj, only=payload.get("only"))
    except Exception as e:
        return JSONResponse({"error": f"写作失败：{e}"}, status_code=500)


@app.get("/api/script/revisions")
async def api_script_revisions(key: str):
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return {"key": key, "text": script_writer.read_file_part(key, "revisions")}


@app.get("/api/script/quality")
async def api_script_quality(key: str):
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return {"key": key, "text": script_writer.read_file_part(key, "quality")}


@app.post("/api/script/revise")
async def api_script_revise(payload: dict):
    """S6 通读修订 + 评分。"""
    key = payload.get("key")
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    try:
        return script_writer.run_revision(proj)
    except Exception as e:
        return JSONResponse({"error": f"修订失败：{e}"}, status_code=500)


@app.get("/api/script/export")
async def api_script_export(key: str):
    """S7 导出定稿全文（text/plain）。"""
    txt = script_writer.export_text(key)
    if not txt:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return Response(txt, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{key}.txt"'})


@app.get("/api/script/forms")
async def api_script_forms():
    """媒介形式与入口路径选项。"""
    return {"forms": [{"key": k, "label": v["label"], "target": v["target_pages"],
                       "unit": v["unit"]} for k, v in script_writer.FORMS.items()],
            "entryPaths": [{"key": k, "label": v} for k, v in script_writer.ENTRY_PATHS.items()]}


# ---------- v1.0.0：诗词流水线 ----------

@app.get("/api/poetry/projects")
async def api_poetry_projects():
    return {"items": poetry_writer.discover_projects()}


@app.post("/api/poetry/create")
async def api_poetry_create(payload: dict):
    """建诗词项目。payload: {title?, idea?, form?, rounds?, cfg_overrides?}"""
    cfg = dict(payload.get("cfg_overrides") or {})
    for k in ("title", "idea", "form", "rounds"):
        if payload.get(k) is not None:
            cfg[k] = payload[k]
    cfg.setdefault("form", "modern")
    try:
        proj = poetry_writer.create_project(cfg)
    except Exception as e:
        return JSONResponse({"error": f"创建失败：{e}"}, status_code=500)
    base = os.path.basename(proj.path)
    return {"key": base, "pid": base, "project": base, "medium": "poetry",
            "status": proj.status()}


@app.get("/api/poetry/status")
async def api_poetry_status(key: str):
    proj = poetry_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return proj.status()


@app.get("/api/poetry/part")
async def api_poetry_part(key: str, part: str = "final"):
    """读项目部分：note / imagery / draft / check / final"""
    if not poetry_writer.get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return {"key": key, "part": part, "text": poetry_writer.read_part(key, part)}


@app.post("/api/poetry/recheck")
async def api_poetry_recheck(payload: dict):
    """对当前草稿重新做格律校验。"""
    key = payload.get("key")
    if not poetry_writer.get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    try:
        return poetry_writer.recheck(key)
    except Exception as e:
        return JSONResponse({"error": f"校验失败：{e}"}, status_code=500)


@app.get("/api/poetry/rhyme")
async def api_poetry_rhyme(ch: str = ""):
    """查字的平水韵（韵部 + 平仄）。"""
    return poetry_writer.rhyme_info(ch)


@app.get("/api/poetry/check")
async def api_poetry_check(poem: str = "", form: str = ""):
    """纯校验（不建项目）：给一段诗，返回格律报告。"""
    return poetry_checker.validate(poem, form)


@app.get("/api/poetry/forms")
async def api_poetry_forms():
    return {"forms": [{"key": k, "label": v} for k, v in poetry_writer.FORMS.items()]}
