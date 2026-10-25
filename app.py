"""神笔文学创作台 主应用：聊天即开写（小说 / 剧本 / 诗词 / 散文）"""
import json, os, re, shutil, threading, uuid, time, secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bot import llm, workflow, writer, providers, version, distill, learn, agents, skeleton
from bot import medium as medium_mod, skillkit, script_writer, poetry_writer, poetry_checker
from bot import prose_writer, slop_checker, security, jobs, versions
from bot import export as export_mod
from bot.safeio import atomic_write_json, atomic_write_text, safe_join

ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIR = os.path.join(ROOT, "samples", "示例书-雨夜出租车")

@asynccontextmanager
async def _lifespan(_app):
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    yield

app = FastAPI(title="神笔文学创作台", lifespan=_lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=security.trusted_hosts())

@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    """只保护 /api/*：本机网页若没有启动时下发的一次性令牌，就不能调用写操作。"""
    if security.AUTH_ENABLED and request.url.path.startswith("/api/"):
        got = request.cookies.get(security.COOKIE_NAME) or request.headers.get("x-shenbi-token") or ""
        if not got or not secrets.compare_digest(got, security.TOKEN):
            return JSONResponse({"error": "未授权：请通过 http://127.0.0.1 打开首页获取访问令牌"},
                                status_code=401)
    return await call_next(request)

# ---------- 会话状态机 ----------
# state: idle → options → planning(忙) → title → writing(后台) → done
SESSIONS = {}
PROJECT_INDEX = {}  # key -> writer.Project（后台写作项目）

def _get_project(key):
    if not isinstance(key, str) or not key:
        return None   # 缺 key / 非 str → 交调用方 404，不再 500（os.path.join(dir, None) 会 TypeError）
    if key not in PROJECT_INDEX:
        try:
            pdir = writer._project_dir(key)
        except ValueError:
            return None
        if os.path.isdir(pdir):
            PROJECT_INDEX[key] = writer.Project(pdir, key)
    return PROJECT_INDEX.get(key)

def _chapter_path(proj, ch):
    """章节文件名只允许纯文件名，挡住计划文件被篡改后的 ../ 越界。"""
    name = os.path.basename((ch or {}).get("filePath") or "")
    if not name:
        return None
    return os.path.join(proj.path, name)

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

    # ---- 文体自动分流：用户直接写诗词/散文/剧本 指令时，不走小说流水线 ----
    if s["state"] == "idle":
        try:
            med = medium_mod.detect(msg)
        except Exception:
            med = "novel"
        if med in ("poetry", "prose", "script") and len(msg) >= 6:
            s["state"] = "routed"
            s["routed_medium"] = med
            from bot import prose_writer as _pw, poetry_writer as _pyw, script_writer as _sw
            hint = {
                "poetry": "🎵 识别为诗词创作。在左侧「诗词」面板里点「新建」，粘贴同样需求；或直接告诉我「诗词 七绝 写秋夜」，我用诗词流水线处理。",
                "prose": "✒️ 识别为散文创作。在左侧「散文」面板里点「新建」，粘贴同样需求；或直接告诉我「散文 写一段关于母亲的回忆」，我用散文流水线处理。",
                "script": "🎬 识别为剧本创作。在左侧「剧本」面板里点「新建」，粘贴同样需求；或直接告诉我「剧本 电影 写一个短片前提」，我用剧本流水线处理。",
            }[med]
            return hint + "\n\n（自动分流：此请求不进入小说流水线，避免文体错配。）"
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
    try:
        target = safe_join(writer.PROJECTS_DIR, prefix + new_name, label="目录名")
    except ValueError:
        return
    if target != proj.path and not os.path.exists(target):
        os.rename(proj.path, target)
        proj.path = target
        proj.name = new_name
        plan = proj.plan() or {}
        plan["novelName"] = new_name
        proj.save_plan(plan)

# ---------- 路由 ----------

@app.get("/")
def index(request: Request):
    """默认进入 v2 创作台；旧版保留在 /classic。"""
    if os.path.exists(os.path.join(ROOT, "static", "v2", "index.html")):
        return _serve_ui(os.path.join("v2", "index.html"))
    resp = FileResponse(os.path.join(ROOT, "static", "index.html"))
    resp.set_cookie(security.COOKIE_NAME, security.TOKEN, max_age=7 * 86400,
                    httponly=True, samesite="strict", path="/")
    return resp

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
        atomic_write_json(os.path.join(proj.path, "style_ref.json"),
                          {"name": style_id, "style_prompt": card.get("style_prompt", ""),
                           "summary": card.get("summary", "")})
    except Exception:
        pass


def _reference_block(ref_ids, query=""):
    """创作前上传的参考资料 → 带引用标记的注入块（模块缺失/未选时为空串）。"""
    if not ref_ids:
        return ""
    try:
        from bot import references as _refs
        return _refs.context_block(query or "", ref_ids, max_chars=4000)
    except Exception:
        return ""


def _link_references(key, ref_ids):
    """把本次创建使用的参考关联到作品目录；失败不影响创作。"""
    if not key or not ref_ids:
        return
    try:
        from bot import references as _refs
        for rid in ref_ids:
            _refs.link_to_project(key, rid, "facts")
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
    # v2：长篇规模 / 写作模式 / 参考资料。字符串规模（1m/3m/5m）折算为章数目标。
    for k in ("scale", "writing_mode", "mode", "medium"):
        if payload.get(k):
            cfg[k if k != "writing_mode" else "mode"] = payload[k]
    if isinstance(cfg.get("scale"), str):
        try:
            from bot import longform as _lf
            _sc = _lf.resolve_scale({"scale": cfg["scale"], "chapters": total})
            cfg["scale"] = {"target_words": _sc["target_words"], "chapters": _sc["chapters"],
                            "volumes": _sc["volumes"], "label": payload["scale"]}
            if not payload.get("total_chapters"):
                total = _sc["chapters"]
                cfg["chapters"] = total
        except Exception:
            pass
    ref_ids = [str(x) for x in (payload.get("reference_ids") or []) if str(x).strip()][:40]
    if ref_ids:
        cfg["reference_ids"] = ref_ids
        cfg["_reference_block"] = _reference_block(
            ref_ids, " ".join(str(payload.get(k) or "") for k in ("title", "idea", "genre", "tone")))
    style_id = (payload.get("style_id") or "").strip()   # v0.3.4：蒸馏风格 id → 注入该书 craft
    # v1.2.2：/api/create 全程后台化——writer.create_project 内部有 5-8 次 LLM 调用（1-3 分钟），
    # 同步返回会把 async 事件循环卡死。这里只做轻量占位（写 02-写作计划.json 骨架 + 注册 PROJECT_INDEX），
    # 规划/书名候选全部后台线程跑；前端轮询 /api/status、/api/titles。
    import threading as _th
    from datetime import datetime as _dt
    base = f"{_dt.now().strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[/:*?"<>|\s]+', '', title or (payload.get('idea') or '未命名'))[:24]}" or f"bk_{_dt.now().strftime('%Y%m%d-%H%M%S')}"
    # 去掉路径分隔符/空白，书名不参与目录命名以外的用途
    try:
        pdir = safe_join(writer.PROJECTS_DIR, base, label="作品 key")
    except ValueError:
        return JSONResponse({"error": "书名包含非法字符"}, status_code=400)
    os.makedirs(pdir, exist_ok=True)
    skeleton_plan = {"version": 1, "novelName": title or (payload.get("idea") or "未命名"),
                     "projectPath": pdir, "status": "planning",
                     "chapters": [], "totalChapters": total, "totalWords": 0,
                     "config": {k: v for k, v in cfg.items() if k != "api_key"},
                     "meta": {"awaiting_title": bool(not title), "title_candidates": [],
                              "planning_running": True},
                     "createdAt": _dt.now().isoformat()}
    atomic_write_json(os.path.join(pdir, "02-写作计划.json"), skeleton_plan)
    _st = cfg.get("style") or {}
    learn.record_book(_st.get("题材", ""), _st.get("金手指", ""), _st.get("节奏", ""), _st.get("语言", ""))
    def _bg():
        plan_path = os.path.join(pdir, "02-写作计划.json")
        try:
            cfg2 = workflow.fill_defaults(cfg)   # 内部含 LLM 调用，必须留在后台线程
            n = int((cfg2.get("defaults") or {}).get("title_count", 8))
            if title:
                titles = [{"title": title, "technique": "指定书名", "why": "用户指定"}]
                cands = []
            else:
                cands = workflow.gen_titles(cfg2, n=n) or [{"title": "未命名", "technique": "", "why": ""}]
                titles = [{"title": (payload.get("idea") or "未命名")[:20], "technique": "占位", "why": ""}]
            # 关键：把 create_project 写到同一个骨架目录，避免“接口返回 key A、实际建了目录 B”
            proj = writer.create_project(cfg2, titles, pdir=pdir)
            _attach_style_ref(proj, style_id)
            _link_references(os.path.basename(proj.path), ref_ids)
            plan = proj.plan() or {}
            meta = plan.get("meta") or {}
            if title:
                plan["meta"] = meta
                proj.save_plan(plan)
                writer.start_writing(proj)
            else:
                meta["title_candidates"] = cands
                meta["awaiting_title"] = True
                meta["planning_running"] = False
                plan["meta"] = meta
                plan["status"] = "awaiting_title"
                proj.save_plan(plan)
        except Exception as e:
            print(f"[bg] 小说创建失败: {e}", flush=True)
            try:
                pl = json.load(open(plan_path, encoding="utf-8"))
                pl["status"] = "failed"; pl.setdefault("meta", {})["planning_running"] = False
                pl["meta"]["error"] = str(e)[:200]
                atomic_write_json(plan_path, pl)
            except Exception:
                pass
    _th.Thread(target=_bg, daemon=True).start()
    return {"project": base, "pid": base, "key": base,
            "awaiting_title": bool(not title), "async": True,
            "candidates": [] if title else [{"title": "候选生成中…", "technique": "", "why": ""}]}

# ---------- 新手指引：加载内置示例书 ----------
@app.post("/api/sample")
def api_sample():
    """把内置示例书复制成一本可编辑的作品，让新用户不配置模型也能先看到成品。"""
    src = os.path.realpath(SAMPLE_DIR)
    if not os.path.isdir(src):
        return JSONResponse({"error": "内置示例书缺失，请重新安装完整发行包"}, status_code=404)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = f"{ts}-示例-雨夜出租车"
    dest = ""
    for i in range(20):
        key = base if i == 0 else f"{base}-{i + 1}"
        cand = os.path.join(writer.PROJECTS_DIR, key)
        if not os.path.exists(cand):
            dest, final_key = cand, key
            break
    if not dest:
        final_key = f"{base}-{uuid.uuid4().hex[:6]}"
        dest = os.path.join(writer.PROJECTS_DIR, final_key)
    try:
        shutil.copytree(src, dest)
        pj = os.path.join(dest, "02-写作计划.json")
        plan = json.load(open(pj, encoding="utf-8"))
        plan["projectPath"] = dest
        plan["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        meta = plan.setdefault("meta", {})
        meta["sample"] = True
        meta["sample_loaded_at"] = plan["updatedAt"]
        atomic_write_json(pj, plan)
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        return JSONResponse({"error": f"加载示例书失败：{e}"}, status_code=500)
    return {"ok": True, "key": final_key, "name": plan.get("novelName", "雨夜出租车"),
            "chapters": len(plan.get("chapters") or [])}

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
    fp = _chapter_path(proj, ch)
    if not fp or not os.path.exists(fp):
        return JSONResponse({"error": "章节文件不存在"}, status_code=404)
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
        versions.snapshot(proj.path, ch_no, body, label="局部改写前")
        atomic_write_text(fp, body.replace(target, new, 1))
        proj.log(f"✒️ 第{ch_no}章改写一段（{instr[:24]}…）")
    return {"ok": True, "chapter": ch_no, "orig": target, "new": new}

@app.get("/api/rewrite-log")
def api_rewrite_log(key: str):
    try:
        p = os.path.join(writer._project_dir(key), "16-重写记录.md")
    except ValueError:
        return JSONResponse({"error": "作品 key 非法"}, status_code=400)
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
    atomic_write_json(os.path.join(proj.path, "style.json"), st)
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
        u = llm.read_usage(os.path.basename(proj.path))
        used, cap = int(u.get("total_tokens") or 0), llm.budget_tokens()
        return {"key": key, "usage": u, "global": g, "budget": cap,
                "remaining": (max(0, cap - used) if cap else None), "exceeded": bool(cap and used >= cap)}
    return {"key": "", "usage": {}, "global": g, "budget": llm.budget_tokens()}


@app.get("/api/llm-calls")
def api_llm_calls(limit: int = 50):
    """结构化 LLM 调用日志（不含 prompt/密钥），用于排查慢调用与花费。"""
    return {"items": llm.recent_calls(limit)}


@app.get("/api/settings")
def api_settings():
    d = llm._global_defaults()
    val = d.get("validation") or {}
    mode = "off" if val.get("enabled") is False else ("auto" if val.get("mode") == "autofix" else "report")
    return {"quality_mode": mode, "token_budget": llm.budget_tokens(),
            "lorebook": bool(d.get("lorebook", True)), "llm_trace": bool(d.get("llm_trace", True)),
            "call_gap": d.get("call_gap", 2), "max_global_agents": d.get("max_global_agents", 4)}


@app.post("/api/settings")
def api_settings_save(payload: dict):
    """收口关键开关：审稿强度、token 预算、Lorebook 检索、调用日志。"""
    cf = os.path.join(ROOT, "config.json")
    try:
        cfg = json.load(open(cf, encoding="utf-8")) if os.path.exists(cf) else {}
    except Exception:
        cfg = {}
    d = cfg.setdefault("defaults", {})
    if "quality_mode" in payload:
        qm = str(payload.get("quality_mode"))
        if qm not in ("off", "report", "auto"):
            return JSONResponse({"error": "quality_mode 只能是 off/report/auto"}, status_code=400)
        v = d.setdefault("validation", {})
        v["enabled"] = qm != "off"
        v["mode"] = "autofix" if qm == "auto" else "report"
    if "token_budget" in payload:
        try:
            d["token_budget"] = max(0, int(payload.get("token_budget") or 0))
        except (TypeError, ValueError):
            return JSONResponse({"error": "token_budget 必须是整数"}, status_code=400)
    if "lorebook" in payload:
        d["lorebook"] = bool(payload.get("lorebook"))
    if "llm_trace" in payload:
        d["llm_trace"] = bool(payload.get("llm_trace"))
    atomic_write_json(cf, cfg)
    return api_settings()

@app.get("/api/titles")
def api_titles(key: str):
    """只读返回书名候选；候选项由 /api/create 的后台规划生成，避免轮询时反复触发 LLM。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    plan = proj.plan() or {}
    meta = plan.get("meta") or {}
    return {"key": key, "candidates": meta.get("title_candidates") or [],
            "selected": meta.get("selected_title"),
            "planning_running": bool(meta.get("planning_running")),
            "status": plan.get("status"), "error": meta.get("error")}

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
    if (_RELEASE_RUNNING.get(key) or {}).get("running"):
        return {"ok": True, "running": True, "msg": "放行门禁进行中…"}
    def _run_gate():
        res = writer.run_release_gate(key)
        _RELEASE_RUNNING[key] = {"running": False, "result": res}
        return res
    job = jobs.run("release_gate", _run_gate, key=key, title="放行门禁")
    _RELEASE_RUNNING[key] = {"running": True, "started_at": int(time.time()), "job_id": job["id"]}
    return {"ok": True, "running": True, "msg": "🔓 放行门禁中（自动带要求重写到 ≥90，约几分钟）…"}

@app.get("/api/release-log")
def api_release_log(key: str):
    try:
        p = os.path.join(writer._project_dir(key), "14-质量报告.md")
    except ValueError:
        return JSONResponse({"error": "作品 key 非法"}, status_code=400)
    r = _RELEASE_RUNNING.get(key) or {}
    running = bool(r.get("running"))
    res = r.get("result")
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
    try:
        return os.path.join(writer._project_dir(key), "chat.json")
    except ValueError:
        return None


@app.get("/api/bkchat")
def api_bkchat_get(key: str = ""):
    if not key:
        return {"messages": []}
    p = _bkchat_path(key)
    if not p:
        return JSONResponse({"error": "作品 key 非法"}, status_code=400)
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
    # 采用前端传来的 ts（前端拿它标反馈按钮）；缺失/异常时才用服务器时间
    now_ms = int(time.time() * 1000)
    try:
        ts = int(payload.get("ts") or 0)
    except (ValueError, TypeError):
        ts = 0
    if ts <= 0 or ts > now_ms + 86400_000:
        ts = now_ms
    while any(m.get("ts") == ts for m in msgs):
        ts += 1
    msgs.append({"who": who, "text": text, "ts": ts})
    # 每本书只保留最近 400 条，防止无限膨胀
    os.makedirs(os.path.dirname(p), exist_ok=True)
    atomic_write_text(p, json.dumps(msgs[-400:], ensure_ascii=False))
    return {"ok": True, "count": len(msgs), "ts": ts}


@app.post("/api/bkchat/{key}/feedback")
def api_bkchat_feedback(key: str, payload: dict):
    """v1.3：对某条 AI 回复打分（DSH 式 👍/👎/有问题），写入 chat.json 对应消息 +
    汇入反馈账本（设置→学习报告展示，自我改进素材）。
    payload: {ts, rating: "up"|"down"|"issue", note?}"""
    p = _bkchat_path(key)
    if not p:
        return JSONResponse({"error": "作品 key 非法"}, status_code=400)
    if not os.path.exists(p):
        return JSONResponse({"error": "该作品还没有对话记录"}, status_code=404)
    ts = int(payload.get("ts") or 0)
    rating = payload.get("rating")
    if rating not in ("up", "down", "issue"):
        return JSONResponse({"error": "rating 必须是 up/down/issue"}, status_code=400)
    try:
        msgs = json.loads(open(p, encoding="utf-8").read())
    except Exception:
        return JSONResponse({"error": "对话记录损坏"}, status_code=500)
    hit = None
    for m in msgs:
        if m.get("ts") == ts and m.get("who") == "bot":
            m["feedback"] = {"rating": rating, "note": (payload.get("note") or "").strip()[:500],
                             "at": int(time.time() * 1000)}
            hit = m
            break
    if not hit:
        return JSONResponse({"error": "未找到该条回复"}, status_code=404)
    atomic_write_text(p, json.dumps(msgs[-400:], ensure_ascii=False))
    try:
        fb_path = os.path.join(writer.PROJECTS_DIR, "_feedback.json")
        fb = json.load(open(fb_path, encoding="utf-8")) if os.path.exists(fb_path) else {"total": 0, "up": 0, "down": 0, "issue": 0, "notes": []}
        fb["total"] = int(fb.get("total") or 0) + 1
        fb[rating] = int(fb.get(rating) or 0) + 1
        if payload.get("note"):
            fb.setdefault("notes", []).append({"key": key, "note": (payload.get("note") or "")[:200],
                                               "rating": rating, "at": int(time.time() * 1000)})
            fb["notes"] = fb["notes"][-50:]
        atomic_write_json(fb_path, fb, indent=1)
    except Exception:
        pass
    return {"ok": True, "rating": rating}


@app.post("/api/bkchat/{key}/branch")
def api_bkchat_branch(key: str, payload: dict):
    """v1.3：从某条消息分支（DSH 式）：以该消息为界另存分支文件，主对话保留并标记。
    payload: {ts}"""
    p = _bkchat_path(key)
    if not p:
        return JSONResponse({"error": "作品 key 非法"}, status_code=400)
    if not os.path.exists(p):
        return JSONResponse({"error": "该作品还没有对话记录"}, status_code=404)
    ts = int(payload.get("ts") or 0)
    try:
        msgs = json.loads(open(p, encoding="utf-8").read())
    except Exception:
        return JSONResponse({"error": "对话记录损坏"}, status_code=500)
    idx = next((i for i, m in enumerate(msgs) if m.get("ts") == ts), None)
    if idx is None:
        return JSONResponse({"error": "未找到该条消息"}, status_code=404)
    branch_id = f"b{int(time.time())}"
    bp = os.path.join(os.path.dirname(p), f"chat-{branch_id}.json")
    os.makedirs(os.path.dirname(bp), exist_ok=True)
    atomic_write_text(bp, json.dumps(msgs[:idx + 1], ensure_ascii=False))
    for m in msgs:
        if m.get("ts") == ts:
            m["branched"] = branch_id
            break
    atomic_write_text(p, json.dumps(msgs[-400:], ensure_ascii=False))
    return {"ok": True, "branch_id": branch_id, "messages": idx + 1}


@app.get("/api/bkchat/{key}/branches")
def api_bkchat_branches(key: str):
    try:
        pdir = writer._project_dir(key)
    except ValueError:
        return {"branches": []}
    out = []
    if os.path.isdir(pdir):
        for f in sorted(os.listdir(pdir)):
            if f.startswith("chat-b") and f.endswith(".json"):
                bid = f[5:-5]
                try:
                    n = len(json.loads(open(os.path.join(pdir, f), encoding="utf-8").read()))
                except Exception:
                    n = 0
                out.append({"branch_id": bid, "messages": n})
    return {"branches": out}


@app.get("/api/bkchat/{key}/branch/{bid}")
def api_bkchat_branch_get(key: str, bid: str):
    if not re.fullmatch(r"b\d{1,20}", bid or ""):
        return JSONResponse({"error": "分支 id 非法"}, status_code=400)
    try:
        bp = os.path.join(writer._project_dir(key), f"chat-{bid}.json")
    except ValueError:
        return JSONResponse({"error": "作品 key 非法"}, status_code=400)
    if not os.path.isfile(bp):
        return JSONResponse({"error": "分支不存在"}, status_code=404)
    return {"branch_id": bid, "messages": json.loads(open(bp, encoding="utf-8").read())}


@app.get("/api/feedback")
def api_feedback_stats():
    """反馈总账（设置→学习报告用）。"""
    fb_path = os.path.join(writer.PROJECTS_DIR, "_feedback.json")
    if os.path.exists(fb_path):
        try:
            return json.load(open(fb_path, encoding="utf-8"))
        except Exception:
            pass
    return {"total": 0, "up": 0, "down": 0, "issue": 0, "notes": []}

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

@app.get("/api/jobs")
def api_jobs(key: str = "", kind: str = "", limit: int = 50):
    """后台任务表（排队/运行/完成/失败），支持按作品/类型过滤。"""
    return {"items": jobs.list_jobs(limit=limit, key=key, kind=kind)}


@app.get("/api/live/{key}")
def api_live(key: str):
    """SSE：把写作/审稿/门禁的进程日志与状态变化实时推给前端（替代高频轮询）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)

    def _active():
        for table in (_RELEASE_RUNNING, _ONESHOT_RUNNING, _PARALLEL_RUNNING, _REWRITING):
            v = table.get(key)
            if isinstance(v, dict) and v.get("running"):
                return True
        return False

    def _gen():
        import time as _t
        # 连接建立时先补发最近日志（进程重启后内存事件为空，文件里仍有历史）
        try:
            hist = open(os.path.join(proj.path, "运行日志.txt"), encoding="utf-8").read().splitlines()[-50:]
        except Exception:
            hist = []
        for line in hist:
            yield "event: log\ndata: " + json.dumps({"line": line}, ensure_ascii=False) + "\n\n"
        seen = len(list(proj.events))
        last = None
        deadline = _t.time() + 1800   # 单连接最长 30 分钟，前端会自动重连
        while _t.time() < deadline:
            evs = list(proj.events)
            for e in evs[seen:]:
                yield "event: log\ndata: " + json.dumps({"line": e}, ensure_ascii=False) + "\n\n"
            seen = len(evs)
            st = proj.status()
            snap = {"state": st.get("state"), "done": st.get("done"), "total": st.get("total"),
                    "words": st.get("words"), "running": st.get("running")}
            if snap != last:
                last = snap
                yield "event: status\ndata: " + json.dumps(snap, ensure_ascii=False) + "\n\n"
            if snap["state"] in ("completed", "failed", "awaiting_title") and not snap["running"] and not _active():
                yield "event: done\ndata: " + json.dumps(snap, ensure_ascii=False) + "\n\n"
                break
            yield ": ping\n\n"
            _t.sleep(1)

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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

@app.get("/api/export")
def api_export(key: str, fmt: str = "docx"):
    """专业导出：md / docx / epub / print；剧本另支持 fountain / fdx。"""
    from urllib.parse import quote
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    fmt = (fmt or "docx").lower()
    novel = not os.path.exists(os.path.join(proj.path, script_writer.F_PLAN))
    plan = proj.plan() or {}
    if novel:
        title = plan.get("novelName") or proj.name
        chapters = export_mod.novel_chapters(proj)
        blocks = []
        for c in chapters:
            blocks.append((1, f"第{c['n']}章 {export_mod._strip_md(c['title'])}"))
            blocks.extend(export_mod._md_blocks(c["text"]))
    else:
        sproj = script_writer.ScriptProject(proj.path, proj.name)
        plan = sproj.plan() or plan
        title = plan.get("scriptName") or proj.name
        chapters = [{"n": sc["no"], "title": sc["heading"], "text": sc["text"]}
                    for sc in export_mod.script_scenes(sproj)]
    if not chapters:
        return JSONResponse({"error": "还没有可导出的正文"}, status_code=400)
    author = ((plan.get("meta") or {}).get("author") or "").strip()

    if fmt in ("md", "markdown"):
        data, media, ext = export_mod.markdown(proj).encode("utf-8"), "text/markdown; charset=utf-8", "md"
    elif fmt == "docx":
        data = export_mod.docx_bytes(title, author, blocks)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ext = "docx"
    elif fmt == "epub":
        data, media, ext = export_mod.epub_bytes(title, author, chapters), "application/epub+zip", "epub"
    elif fmt in ("print", "html"):
        data = export_mod.print_html(title, author, chapters).encode("utf-8")
        media, ext = "text/html; charset=utf-8", "html"
    elif fmt == "fountain" and not novel:
        data, media, ext = export_mod.fountain(sproj).encode("utf-8"), "text/plain; charset=utf-8", "fountain"
    elif fmt == "fdx" and not novel:
        data, media, ext = export_mod.fdx_bytes(sproj), "application/xml", "fdx"
    else:
        return JSONResponse({"error": f"不支持的格式：{fmt}"}, status_code=400)

    fname = re.sub(r'[\\/:*?"<>|]+', "", title)[:60] or "export"
    disp = f"attachment; filename=\"export.{ext}\"; filename*=UTF-8''{quote(fname + '.' + ext)}"
    return Response(data, media_type=media, headers={"Content-Disposition": disp})

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
    keys = [k for k in keys if isinstance(k, str) and k][:8]
    job = jobs.run("dispatch", lambda: agents.dispatch_write(keys, _get, lambda p: writer._write_loop(p)),
                   key=",".join(keys), title="批量并发创作", keys=keys)
    # 兼容旧前端：立即返回已受理（不再同步等所有书跑完）
    return {"ok": True, "async": True, "job_id": job["id"],
            "results": {k: {"ok": True, "message": "已进入后台队列"} for k in keys}}

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

def _chapter_entry(proj, n):
    """按 1-based 章序取章节记录与最新计划；越界返回 (None, plan)。"""
    plan = proj.plan() or {}
    chs = plan.get("chapters") or []
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None, plan
    if n < 1 or n > len(chs):
        return None, plan
    return chs[n - 1], plan


_EDIT_FB_AT: dict[tuple, float] = {}


def _project_medium(proj) -> str:
    cfg = (proj.plan() or {}).get("config") or {}
    med = str(cfg.get("medium") or "").strip()
    if medium_mod.is_medium(med):
        return med
    for name, mod in (("script", script_writer), ("poetry", poetry_writer), ("prose", prose_writer)):
        if os.path.exists(os.path.join(proj.path, mod.F_PLAN)):
            return name
    return "novel"


def _capture_edit_feedback(proj, n: int, before: str, after: str) -> None:
    """把改稿差异沉淀为反馈（去抖 90 秒/章），供技能自我蒸馏。"""
    if not before or not after or before == after:
        return
    if abs(len(after) - len(before)) < 20 and before[:120] == after[:120]:
        return
    key = (proj.path, int(n))
    now = time.time()
    if now - _EDIT_FB_AT.get(key, 0) < 90:
        return
    _EDIT_FB_AT[key] = now
    try:
        from bot import feedback
        feedback.capture_edit(os.path.basename(proj.path), n, before, after,
                              medium=_project_medium(proj), stage="edit",
                              tags=["manual_edit"])
    except Exception:
        pass


def _apply_chapter_text(proj, n, text: str, label: str = "编辑前") -> int:
    """写章节正文：先留版本快照，再原子落盘，最后同步词数与成书。返回汉字数。"""
    ch, plan = _chapter_entry(proj, n)
    if ch is None:
        raise ValueError("章节不存在")
    fp = _chapter_path(proj, ch)
    old = open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
    if old != text:
        versions.snapshot(proj.path, n, old, label=label)
        atomic_write_text(fp, text)
        _capture_edit_feedback(proj, n, old, text)
    words = len(re.findall(r"[\u4e00-\u9fff]", text))
    with proj.lock:
        latest = proj.plan() or plan
        chs = latest.get("chapters") or []
        if not (1 <= n <= len(chs)):
            raise ValueError("章节不存在")
        target = chs[n - 1]
        target["wordCount"] = words
        target["editedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if text.strip() and target.get("status") != "completed":
            target["status"] = "completed"
        latest["totalWords"] = sum(int(c.get("wordCount") or 0) for c in chs)
        proj.save_plan(latest)
    try:
        writer.rebuild_book(proj)
    except Exception:
        pass
    return words


@app.get("/api/chapter")
def api_chapter(key: str, n: int):
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    ch, _plan = _chapter_entry(proj, n)
    if ch is None:
        return JSONResponse({"error": "章节不存在"}, status_code=404)
    fp = _chapter_path(proj, ch)
    text = open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
    return {"chapter": n, "title": ch.get("title") or "", "text": text,
            "words": ch.get("wordCount") or 0,
            "updatedAt": ch.get("editedAt") or "",
            "versions": len(versions.list_versions(proj.path, n))}


@app.put("/api/chapter")
def api_chapter_save(payload: dict):
    """手动编辑 / 自动保存：写正文 + 词数 + 重建成书 + 版本快照。"""
    proj = _get_project(payload.get("key"))
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    text = payload.get("text")
    if not isinstance(text, str):
        return JSONResponse({"error": "正文必须是字符串"}, status_code=400)
    if len(text) > 2_000_000:
        return JSONResponse({"error": "单章超过 200 万字符，请分段保存"}, status_code=413)
    try:
        words = _apply_chapter_text(proj, payload.get("n"), text)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return {"ok": True, "chapter": int(payload.get("n")), "words": words,
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}


@app.get("/api/chapter/versions")
def api_chapter_versions(key: str, n: int):
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    ch, _ = _chapter_entry(proj, n)
    if ch is None:
        return JSONResponse({"error": "章节不存在"}, status_code=404)
    return {"chapter": n, "items": versions.list_versions(proj.path, n)}


@app.get("/api/chapter/version")
def api_chapter_version(key: str, n: int, id: str):
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    try:
        text = versions.read_version(proj.path, n, id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"chapter": n, "id": id, "text": text}


@app.get("/api/chapter/diff")
def api_chapter_diff(key: str, n: int, id: str):
    """历史版本 → 当前正文 的 unified diff。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    ch, _ = _chapter_entry(proj, n)
    if ch is None:
        return JSONResponse({"error": "章节不存在"}, status_code=404)
    try:
        old = versions.read_version(proj.path, n, id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    fp = _chapter_path(proj, ch)
    cur = open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
    return {"diff": versions.diff_text(old, cur)}


@app.post("/api/chapter/restore")
def api_chapter_restore(payload: dict):
    """回滚到某个历史版本（当前内容会先自动存一份快照，可再滚回来）。"""
    proj = _get_project(payload.get("key"))
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    n = payload.get("n")
    try:
        text = versions.read_version(proj.path, n, payload.get("id"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        words = _apply_chapter_text(proj, n, text, label="回滚前")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return {"ok": True, "chapter": int(n), "words": words}

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
            fp = _chapter_path(proj, ch)
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
        fp = _chapter_path(proj, c)
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
        c["evidence"] = rec.get("evidence") or []
    per_chapter = [{"ch": int(k), "score": v.get("score"), "weak": v.get("weak", []),
                    "status": v.get("status", "unscored"), "verdict": v.get("verdict", ""),
                    "evidence": v.get("evidence") or []}
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


@app.post("/api/skillkit/add")
async def api_skillkit_add(payload: dict):
    """自定义添加技能文件（写到 skillkit/custom/ 并热重载索引）。
    payload: {name, title?, medium: [..], stages: [..], kind?, weight?, content}
    content 为 markdown 正文（可含自己的 frontmatter；缺省字段自动补）。"""
    name = re.sub(r"[^\w\-]+", "-", (payload.get("name") or "").strip()).strip("-")
    if not name:
        return JSONResponse({"error": "技能名不能为空"}, status_code=400)
    content = (payload.get("content") or "").strip()
    if len(content) < 20:
        return JSONResponse({"error": "正文太短（至少 20 字）"}, status_code=400)
    media = payload.get("medium") or ["all"]
    if not isinstance(media, list):
        media = [str(media)]
    media = [m for m in media if medium_mod.is_medium(m) or m == "all"] or ["all"]
    stages = [str(s).upper() for s in (payload.get("stages") or []) if s]
    kind = payload.get("kind") if payload.get("kind") in ("method", "template", "data") else "method"
    weight = max(1, min(9, int(payload.get("weight") or 3)))
    title = (payload.get("title") or name).strip()[:60]
    fm = ["---", f"name: custom-{name}", f"title: {title}",
          f"medium: [{', '.join(media)}]",
          f"stages: [{', '.join(stages)}]" if stages else "stages: []",
          f"kind: {kind}", f"weight: {weight}",
          "license: 用户自定义", "source: 用户在本机添加", "---", ""]
    cdir = os.path.join(skillkit.EXTERNAL_DIR if os.path.isdir(skillkit.EXTERNAL_DIR)
                        else os.path.join(ROOT, "skillkit"), "custom")
    # custom 目录放 skillkit/ 下（gitignore 不含它，用户技能随产品走）；EXTERNAL 是第三方语料
    cdir = os.path.join(ROOT, "skillkit", "custom")
    os.makedirs(cdir, exist_ok=True)
    fpath = os.path.join(cdir, f"{name}.md")
    with open(fpath, "w", encoding="utf-8") as f:
        f.write("\n".join(fm) + "\n" + content + "\n")
    skillkit.reload()
    return {"ok": True, "path": os.path.relpath(fpath, ROOT), "name": f"custom-{name}"}


@app.delete("/api/skillkit/custom/{name}")
async def api_skillkit_del(name: str):
    """删除自定义技能（仅限 skillkit/custom/ 目录）。"""
    safe = re.sub(r"[^\w\-]+", "", name)
    fpath = os.path.join(ROOT, "skillkit", "custom", f"{safe}.md")
    if not os.path.isfile(fpath):
        return JSONResponse({"error": "自定义技能不存在"}, status_code=404)
    os.remove(fpath)
    skillkit.reload()
    return {"ok": True}


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
    ref_ids = [str(x) for x in (payload.get("reference_ids") or []) if str(x).strip()][:40]
    if ref_ids:
        cfg["reference_ids"] = ref_ids
        cfg["_reference_block"] = _reference_block(
            ref_ids, " ".join(str(payload.get(k) or "") for k in ("title", "idea", "form")))
    if payload.get("provider_id"):
        cfg.setdefault("defaults", {})["provider_id"] = payload["provider_id"]
    if payload.get("model"):
        cfg.setdefault("defaults", {})["model"] = payload["model"]
    # v1.2.1：规划放后台线程（async 端点里同步跑 LLM 会阻塞事件循环 → 全站请求卡死、无法并发多创作）
    import threading as _th
    from datetime import datetime as _dt
    _name = script_writer._safe(cfg.get("title") or cfg.get("idea") or "未命名剧本")
    base = f"s_{_dt.now().strftime('%Y%m%d-%H%M%S')}-{_name}"
    def _bg():
        try:
            script_writer.create_project(cfg, base_key=base)
            _link_references(base, ref_ids)
        except Exception as e:
            print(f"[bg] 剧本规划失败: {e}", flush=True)
    _th.Thread(target=_bg, daemon=True).start()
    return {"key": base, "pid": base, "project": base, "medium": "script", "async": True}


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
    """按场次清单写正文。payload: {key, only?: [场次号]}
    v1.2.2：改后台线程（写 30 场是分钟级 LLM 任务，async 内同步跑会阻塞事件循环）。"""
    import threading as _th
    key = payload.get("key")
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    only = payload.get("only")
    if not only and payload.get("sceneNo"):
        only = [int(payload["sceneNo"])]
    def _bg():
        try:
            script_writer.write_scenes(proj, only=only)
        except Exception as e:
            print(f"[bg] 剧本写作失败: {e}", flush=True)
    _th.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "async": True, "key": key, "msg": "写作已后台启动，工作台轮询可见进度"}


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
    """S6 通读修订 + 评分。v1.2.2：后台线程。"""
    import threading as _th
    key = payload.get("key")
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    def _bg():
        try:
            script_writer.run_revision(proj)
        except Exception as e:
            print(f"[bg] 剧本修订失败: {e}", flush=True)
    _th.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "async": True, "key": key, "msg": "修订已后台启动"}


@app.get("/api/script/export")
async def api_script_export(key: str):
    """S7 导出定稿全文（text/plain）。"""
    txt = script_writer.export_text(key)
    if not txt:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    from urllib.parse import quote as _q
    return Response(txt, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=\"script.txt\"; filename*=UTF-8''{_q(key)}.txt"})


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
    ref_ids = [str(x) for x in (payload.get("reference_ids") or []) if str(x).strip()][:40]
    if ref_ids:
        cfg["reference_ids"] = ref_ids
        cfg["_reference_block"] = _reference_block(
            ref_ids, " ".join(str(payload.get(k) or "") for k in ("title", "idea", "form")))
    import threading as _th
    from datetime import datetime as _dt
    _name = poetry_writer._safe(cfg.get("title") or cfg.get("idea") or "无题")
    base = f"p_{_dt.now().strftime('%Y%m%d-%H%M%S')}-{_name}"
    def _bg():
        try:
            poetry_writer.create_project(cfg, base_key=base)
            _link_references(base, ref_ids)
        except Exception as e:
            print(f"[bg] 诗词创作失败: {e}", flush=True)
    _th.Thread(target=_bg, daemon=True).start()
    return {"key": base, "pid": base, "project": base, "medium": "poetry", "async": True}


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


# ---------- v1.0.0：散文流水线 + 反 AI 腔检测 ----------

@app.get("/api/prose/projects")
async def api_prose_projects():
    return {"items": prose_writer.discover_projects()}


@app.post("/api/prose/create")
async def api_prose_create(payload: dict):
    """建散文项目。payload: {title?, idea?, genre?, operation?, sourceText?, cfg_overrides?}"""
    cfg = dict(payload.get("cfg_overrides") or {})
    for k in ("title", "idea", "genre", "operation", "sourceText"):
        if payload.get(k) is not None:
            cfg[k] = payload[k]
    cfg.setdefault("operation", "write")
    ref_ids = [str(x) for x in (payload.get("reference_ids") or []) if str(x).strip()][:40]
    if ref_ids:
        cfg["reference_ids"] = ref_ids
        cfg["_reference_block"] = _reference_block(
            ref_ids, " ".join(str(payload.get(k) or "") for k in ("title", "idea", "genre")))
    import threading as _th
    from datetime import datetime as _dt
    _name = prose_writer._safe(cfg.get("title") or cfg.get("idea") or "未命名散文")
    base = f"e_{_dt.now().strftime('%Y%m%d-%H%M%S')}-{_name}"
    def _bg():
        try:
            prose_writer.create_project(cfg, base_key=base)
            _link_references(base, ref_ids)
        except Exception as e:
            print(f"[bg] 散文创作失败: {e}", flush=True)
    _th.Thread(target=_bg, daemon=True).start()
    return {"key": base, "pid": base, "project": base, "medium": "prose", "async": True}


@app.get("/api/prose/status")
async def api_prose_status(key: str):
    proj = prose_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return proj.status()


@app.get("/api/prose/part")
async def api_prose_part(key: str, part: str = "final"):
    """读项目部分：brief / material / draft / slop / final"""
    if not prose_writer.get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return {"key": key, "part": part, "text": prose_writer.read_part(key, part)}


@app.get("/api/prose/operations")
async def api_prose_operations():
    return {"operations": [{"key": k, "label": v} for k, v in prose_writer.OPERATIONS.items()],
            "genres": [{"key": k, "label": v} for k, v in prose_writer.GENRES.items()]}


@app.post("/api/slop/scan")
async def api_slop_scan(payload: dict):
    """反 AI 腔扫描（纯规则，不调 LLM）。payload: {text}"""
    text = payload.get("text") or ""
    rep = slop_checker.scan(text)
    rep["yaml"] = slop_checker.report_yaml(rep)
    return rep


# ---------------------------------------------------------------- v2.0 界面与接口
# 默认 `/` 进入 v2；旧版保留在 `/classic`。静态资源由挂载提供。
# 新内核模块全部在 bot/v2api.py 内延迟导入，缺失时返回 501，旧功能不受影响。
def _serve_ui(filename: str):
    resp = FileResponse(os.path.join(ROOT, "static", filename))
    resp.set_cookie(security.COOKIE_NAME, security.TOKEN, max_age=7 * 86400,
                    httponly=True, samesite="strict", path="/")
    return resp


@app.get("/classic")
def classic(request: Request):
    return _serve_ui("index.html")


@app.get("/v2")
def v2_root():
    return RedirectResponse("/v2/", status_code=307)


@app.get("/v2/")
def v2_index():
    path = os.path.join(ROOT, "static", "v2", "index.html")
    if not os.path.exists(path):
        return JSONResponse({"error": "v2 界面尚未构建，请先使用 /classic"},
                            status_code=404)
    return _serve_ui(os.path.join("v2", "index.html"))


_V2_DIR = os.path.join(ROOT, "static", "v2")
if os.path.isdir(_V2_DIR):
    app.mount("/v2", StaticFiles(directory=_V2_DIR), name="v2-static")


@app.post("/api/export")
def api_export_post(payload: dict):
    """兼容 v2 前端的 POST 调用；实际导出仍复用 GET /api/export。"""
    return api_export(str(payload.get("key") or ""),
                      str(payload.get("format") or payload.get("fmt") or "docx"))


from bot.v2api import register_v2_routes  # noqa: E402  (需在 _get_project/chat 定义后注册)

register_v2_routes(app, _get_project, chat)

