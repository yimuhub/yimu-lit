"""一幕·YIMU 神笔文学创作台 主应用：聊天即开写（小说 / 剧本 / 诗词 / 散文）"""
import glob, json, os, re, shutil, threading, traceback, uuid, time, secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bot import llm, workflow, writer, providers, version, distill, learn, agents, skeleton
from bot import medium as medium_mod, skillkit, script_writer, poetry_writer, poetry_checker
from bot import prose_writer, slop_checker, security, jobs, versions, platforms
from bot import webai, webstats, weblib, webprompts, webbreakdown, webrank, webbench, webcanvas, webcover, webflow  # v5.2-5.8：增量路由（APIRouter）
from bot import thesis_writer
from bot import children_writer, news_writer, copy_writer, other_writer
from bot import chatsum
from bot import desktop_bridge
from bot import syslog as syslog_mod
from bot import steps as steps_mod
from bot import export as export_mod
from bot import purity, journal, express
from bot import livejob
from bot import audit as audit_mod
from bot import brief as brief_mod
from bot import chief as chief_mod
from bot.safeio import atomic_write_json, atomic_write_text, safe_join

# v4.28.0：双根路径——ROOT 语义保留为「资源根」（static/samples，桌面版随应用包只读）；
# 用户可写数据（config/projects/data…）一律走 bot.paths（桌面版在用户数据目录）。
from bot.paths import RESOURCE_ROOT as ROOT, DATA_ROOT as _DATA_ROOT, CONFIG_PATH as _CONFIG_PATH, DATA_ROOT
SAMPLE_DIR = os.path.join(ROOT, "samples", "示例书-雨夜出租车")

# 未绑定可用供应商时的统一兜底：v2 流式接口（bot/v2api.py）与旧版 /api/chat 说同一句话
_NO_MODEL_REPLY = ("您的消息已收到。由于未配置大模型，请配置模型。"
                   "在「设置 → 模型供应商」里绑定一个可用模型后，再发一次主题。")

# 打招呼/求助类闲聊不进主题提取（E2E 实测：「你好」被当成悬疑小说主题提取出整份配置）
_GREETING_RE = re.compile(
    r"(你好|您好|hi|hello|嗨|哈喽|在吗|在不在|你是谁|你叫什么|你能(做|干)(什么|啥)|"
    r"你都会(什么|啥)|怎么用|帮助|help|谢谢|多谢|感谢|辛苦了)[!！?？。~～\s]*", re.I)

_GREETING_REPLY = (
    "👋 你好！我是你的创作助手，支持四种文体：**小说 / 剧本 / 诗词 / 散文**。\n"
    "· 开新书：直接说主题（如「写一部悬疑小说」），或点左侧『新建作品』走向导；\n"
    "· 继续创作：打开作品后说「继续」「把第3章重写一下」「节奏再紧一点」；\n"
    "· 查进度：随时问「进度」；导出在作品面板的「导出」按钮。")

def _journal_pdirs() -> list[str]:
    """任务档案对账要的是完整目录；writer._project_dirs() 只给目录名。"""
    return [os.path.join(writer.PROJECTS_DIR, d) for d in writer._project_dirs()]


@asynccontextmanager
async def _lifespan(_app):
    syslog_mod.log_event("system", "服务启动", {"version": version.current(), "platform": f"{os.name}/{sys.platform}"},
                         f"服务启动（v{version.current()}）")
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    # v3.1.0：启动时后台恢复「校验中/放行中但线程已死」的书（服务重启遗留卡死）
    def _recover():
        try:
            n = writer.recover_all_stale()
            if n:
                print(f"[recover] 已救回 {n} 本陈旧卡死状态的书", flush=True)
        except Exception as e:
            print(f"[recover] 陈旧状态恢复失败：{e}", flush=True)
        # v4.23.0：任务档案启动对账——进程被上游断流/关机杀掉时来不及写「已暂停」，
        # 档案会永远停在 running，「继续」因此不肯接手。启动一次，全部改标为可续跑。
        try:
            # _project_dirs() 返回的是目录名，对账必须传完整路径；
            # 传名字会让任务档案读不到，僵尸任务永远停在 running。
            k = journal.reconcile_all(_journal_pdirs())
            if k:
                print(f"[recover] 已将 {k} 个中断任务标记为可继续", flush=True)
        except Exception as e:
            print(f"[recover] 任务档案对账失败：{e}", flush=True)
        # v4.26.1：总编辑开机孤儿扫描——服务重启杀掉了看护线程，重启后
        # 「计划说在写、实际没人在跑」的书此前只能等用户人工发现。
        # 现在启动 15 秒后扫描全部 in_progress（未暂停）的书，总编辑逐本接手
        # （ensure 后首检发现无引擎在跑 → 预算内自动断点恢复）。
        import time as _t
        _t.sleep(15)
        try:
            from bot import supervisor as _sv
            _sv.sweep_orphans()
        except Exception as e:
            print(f"[recover] 总编辑孤儿扫描失败：{e}", flush=True)
    threading.Thread(target=_recover, daemon=True).start()
    yield

app = FastAPI(title="一幕·YIMU 神笔文学创作台", lifespan=_lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=security.trusted_hosts())

@app.middleware("http")
async def _static_no_cache(request: Request, call_next):
    """v2.9.0：静态资源禁缓存。run.py 主打热更新，升级后浏览器必须立刻拿到新
    JS/CSS；Chromium 对 ES module 的启发式/内存缓存会长期复用旧模块，
    导致界面与后端版本错位（E2E 实测踩坑：升级后界面仍跑旧逻辑）。"""
    resp = await call_next(request)
    p = request.url.path
    if p.startswith("/v2/") or p in ("/classic",) or p.endswith((".js", ".css", ".html")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.middleware("http")
async def _auth_guard(request: Request, call_next):
    """只保护 /api/*：本机网页若没有启动时下发的一次性令牌，就不能调用写操作。"""
    if security.AUTH_ENABLED and request.url.path.startswith("/api/"):
        got = request.cookies.get(security.COOKIE_NAME) or request.headers.get("x-shenbi-token") or ""
        if not got or not secrets.compare_digest(got, security.TOKEN):
            return JSONResponse({"error": "未授权：请通过 http://127.0.0.1 打开首页获取访问令牌"},
                                status_code=401)
    return await call_next(request)


# v5.2.0：AI 辅助（幽灵字/快捷续写/调用账本/敏感词）与码字统计、灵感速记的增量路由。
# 独立 APIRouter 模块挂载，不进主路由表——见 bot/webai.py、bot/webstats.py。
app.include_router(webai.router)
app.include_router(webstats.router)
# v5.5.2：全书查找替换（bench）。
app.include_router(webbench.router)
# v5.6.0：资料画布（角色关系图/故事线思维导图）。
app.include_router(webcanvas.router)
# v5.7.0：AI 生成封面（图像模型配置与生成）。
app.include_router(webcover.router)
# v5.8.0：无限画布（创作工作流白板）。
app.include_router(webflow.router)
# v5.3.0：书架（封面/分组）与提示词开放编辑。挂载点必须在既有 /api/library/{key}
# 通配路由之前，否则 groups/cover 会被通配吞掉（weblib 硬约束）。
app.include_router(weblib.router)
app.include_router(webprompts.router)
# v5.4.0：拆书台与榜单风向、灵感补充端点。
app.include_router(webbreakdown.router)
app.include_router(webrank.router)

# ---------- 会话状态机 ----------
# state: idle → options → planning(忙) → title → writing(后台) → done
SESSIONS = {}
PROJECT_INDEX = {}  # key -> writer.Project（后台写作项目）

# v4.7.1 修复：会话表此前只增不减 —— 前端每次刷新/换标签都可能带来新的 session_id，
# 实测灌 500 个一次性会话全部常驻（无上限、无 TTL），而每个对象挂着 cfg/titles/digest
# 等大块数据，服务长期运行会稳定吃内存。下面给会话记录「最后活动时间」，超过上限时
# 回收最久未活动、且当前没有后台任务在跑的会话。被回收的会话由前端按 state=idle
# 重新绑定，与「服务重启后 SESSIONS 清空」的既有行为一致，不会丢已落盘的作品。
_SESSION_CAP = 256

def _sess_touch(s):
    try:
        s["_ts"] = time.time()
    except Exception:
        pass
    return s

def _sess_prune():
    try:
        if len(SESSIONS) <= _SESSION_CAP:
            return
        evictable = sorted((v.get("_ts") or 0, k) for k, v in SESSIONS.items()
                           if not v.get("busy") and v.get("state") not in ("planning", "writing"))
        for _, k in evictable[:len(SESSIONS) - _SESSION_CAP]:
            SESSIONS.pop(k, None)
    except Exception:
        pass

def _get_project(key):
    if not isinstance(key, str) or not key:
        return None   # 缺 key / 非 str → 交调用方 404，不再 500（os.path.join(dir, None) 会 TypeError）
    # v4.15.13：改书名后目录随之改名——旧 key 按别名映射跳新 key（否则旧标签页全部「项目不存在」）
    # v5.5：别名改为「先解析、且不再以旧目录存在为前提」。旧实现只有旧目录消失才跳新 key，
    # 而在途规划线程写盘会把旧目录原样复活——复活出来的幻影被当成事实源，会话绑着它
    # 读到 0 章空书，总编辑回答《未命名》（实测开书断层根因）。现在只要 rename_map
    # 有新家且新家在磁盘上，旧 key 一律落到新家；PROJECT_INDEX 也按 canonical key 建。
    if key not in PROJECT_INDEX:
        try:
            _rk = writer.resolve_key(key)
        except Exception:
            _rk = key
        if _rk and _rk != key:
            try:
                _rk_exists = os.path.isdir(writer._project_dir(_rk))
            except Exception:
                _rk_exists = False
            if _rk_exists:
                key = _rk
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

def chat(session_id, message, key=None, digest=None):
    s = SESSIONS.get(session_id) or {"id": session_id, "state": "idle", "cfg": None,
                                     "project": None, "titles": None, "busy": False}
    SESSIONS[session_id] = _sess_touch(s)
    _sess_prune()
    if digest:
        s["digest"] = digest   # v2.9.0：对话上下文压缩摘要，@Agent 群聊注入用
    msg = message.strip()
    _trace("chat-in", sid=session_id, state=s["state"], msg=msg[:40], busy=s.get("busy"))
    reply = []

    # ---- v2.3.0 修复「对话误起新书」：入口处绑定当前打开的作品。----
    # 服务重启后 SESSIONS 清空、state=idle，此前任何消息（「继续」「进度」）都会被
    # 当成新主题走 extract_config → 起一本全新小说。v2 每条消息带 key：
    # idle + key 有效 → 直接绑定本书（attached），绝不进新主题分支；
    # options/planning/title 等新建流程中途 → 保持原流程不动。
    # v5.5：绑定即规范化——旧 key 命中 rename 别名时直接绑 canonical key
    # （磁盘目录名），否则整个会话的 journal/事件/写作注册表都查旧 key。
    if key and s.get("state") == "idle" and _get_project(str(key)):
        _bproj = _get_project(str(key))
        _bkey = os.path.basename(_bproj.path) if _bproj is not None else str(key)
        s["project"] = _bkey or str(key)
        s["state"] = "attached"

    # ---- v2.4.0：文体自动分流 + NLU 意图理解（统一入口）----
    # 旧逻辑：idle 时检测到诗词/散文/剧本只返回提示，不进入创作
    # 新逻辑：用 NLU 识别用户意图，如果是创作类意图（start_writing/start_writing_all/resume_writing），
    #         则自动选择对应文体的创作流水线，不再只返回提示文字
    if s["state"] == "idle":
        try:
            med = medium_mod.detect(msg)
        except Exception:
            med = "novel"
        # 如果检测到非小说文体，先尝试 NLU 识别是否有明确创作意图
        if med in ("poetry", "prose", "script") and len(msg) >= 6:
            from bot.nlu import parse_intent as _nlu_pi
            _idle_nlu = _nlu_pi(msg, project_status={"state": "idle"})
            _idle_intent = _idle_nlu["intent"]
            # 如果是创作类意图，自动进入对应文体的创作流程（而非只返回提示）
            if _idle_intent in ("start_writing", "start_writing_all", "resume_writing"):
                # 设置 routed 状态和媒介类型，让后续流程走对应文体
                s["state"] = "routed"
                s["routed_medium"] = med
                # 继续往下走新作品触发逻辑（用对应文体创建）
                # 不 return，让下面的新作品提取逻辑接管
            else:
                # 非创作意图（如查询、修改），仍走提示引导
                hint = {
                    "poetry": "🎵 识别为诗词创作。请告诉我具体需求，例如「诗词 七绝 写秋夜」，我直接用诗词流水线处理。",
                    "prose": "✒️ 识别为散文创作。请告诉我具体需求，例如「散文 写一段关于母亲的回忆」，我直接用散文流水线处理。",
                    "script": "🎬 识别为剧本创作。请告诉我具体需求，例如「剧本 电影 写一个短片前提」，我直接用剧本流水线处理。",
                }[med]
                return hint + "\n\n（自动分流：此请求将使用对应文体流水线处理。直接说「开始创作」即可启动。）"
    # ---- 重复投递保护：同一会话的 LLM 重操作进行中，短消息/重复消息直接给状态提示 ----
    if s.get("busy"):
        if len(msg) <= 4 or s.get("state") in ("planning", "writing"):
            return "⏳ 正在处理中（规划/创作需 1-3 分钟），请稍候，或说「进度」查看状态。"
    # ---- 写作进行中 ----
    if s["state"] in ("writing", "done") and s.get("project"):
        proj = _get_project(s["project"])
        if proj:
            st = proj.status()
            # v4.34.0：只有此刻真的有引擎在跑才算「正在创作中」——账面 in_progress
            # 是历史状态（中断/待继续），completed 是成书，都不能再对用户说正在创作。
            if st.get("running") and st["state"] not in ("completed", "paused"):
                if re.search(r"进度|怎么样|状态|status", msg):
                    return f"📖 《{st['name']}》正在创作中：第 {st['done']}/{st['total']} 章，已写 {st['words']} 字。" \
                           f"完成前你可以随时问我「进度」。"
                # v4.37.0：创作中也能像跟总编辑说话一样下指令、聊稿、改要求，
                # 不再用「正在疯狂创作中」把作者挡在门外。
                return _chat_attached(s, proj, msg)
        # 已完成 → 允许新主题
    # ---- v2.3.0：会话已绑定本书（attached）：一切消息按「这本书」路由，绝不误起新书 ----
    if s["state"] == "attached" and s.get("project"):
        proj = _get_project(s["project"])
        if proj:
            _r = _chat_attached(s, proj, msg)
            # v5.5.2：直接开聊会话（请求无 key）的对话 v2api 不落盘（当时还没有 key）——
            # 这里补记进当前书，保住总编辑接「继续」「改设定」的对话上下文。
            # 带 key 的会话由 v2api 正常落盘，不重复记。
            if key is None and _r:
                _persist_unbound_turn(proj, msg, _r)
            return _r
        s["state"] = "idle"   # 目录已被删除 → 解绑，回落旧行为
        s["project"] = None
    # ---- 新作品触发（仅 idle；options 状态下重复丢主题 → 重显菜单，不重复提取）----
    # v2.3.0 保护：idle 但已有 project 的会话不得进新主题提取（attached 已在上面截获）。
    if s["state"] == "idle" and not s.get("project") and _GREETING_RE.fullmatch(msg):
        return _GREETING_REPLY
    # ---- v5.5.2：直接开聊建书（无绑定会话） ----
    # 作者在「直接开聊」入口说「我想写一本XX」——总编辑先真正理解：想写什么 → 抽 idea/偏好
    # → create_book 真的建书、会话绑定新书、后台规划自动定名开写。
    # 总编辑不可用/没接住（返回 None）→ 原样回落旧的主题提取流程，不改变既有行为。
    if s["state"] == "idle" and not s.get("project"):
        _direct = _chat_unbound(s, msg)
        if _direct is not None:
            return _direct
    if (s["state"] == "idle" and not s.get("project")) or re.search(r"^(写|帮我写|新|再|来)(一|个)?(部|本)?|新作品|新的小说|重新", msg):
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
            except providers.ProviderError:
                # 与 /api/chat/stream 的兜底同口径：没配模型不是「描述得不够具体」
                return _NO_MODEL_REPLY
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
        _trace("options-branch", msg=msg[:40], starts134=msg.startswith(("1", "2", "3", "4")))
        # v4.24.16 修复死分支：「2」（快车道）此前被挡在外层准入元组之外——
        # 内层 if msg.startswith("2") 永不可达，用户回复「2」只会得到「请回复 1/2/3/4」
        if msg.startswith(("1", "2", "3", "4")) or msg in ("确认", "快车道", "直接写", "随机"):
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
                except providers.ProviderError:
                    return _NO_MODEL_REPLY
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
                return chat(session_id, "3 " + msg, key)
            finally:
                s["busy"] = False
        return "请回复 1 / 2 / 3 / 4，或直接用文字告诉我要调整什么。"
    # ---- title 阶段 ----
    if s["state"] == "title" and s.get("project"):
        proj = _get_project(s["project"])
        plan = proj.plan() if proj else None
        if plan:
            _pick_title(plan, msg.strip())
            # v4.30.0：改走 _apply_title 统一路径——旧 _rename_project 不写 rename 别名、
            # 不刷新索引，改名后旧 key 写入会重建旧目录 → 一个作品两个对话
            res = _apply_title(s["project"], plan["novelName"])
            if not res.get("ok"):
                return f"⚠️ 书名应用失败：{res.get('error')}"
            s["project"] = res["key"]
            s["state"] = "writing"
            # v4.34.1：规划在途时 _apply_title 已置 title_picked（后台线程规划完自动
            # 开写），这里不得再抢跑 start_writing——否则双线程在同一本书上打架。
            if not res.get("started"):
                _start_bg(s["project"])
            return (f"✅ 书名《{res['name']}》确定，阶段3 疯狂创作已启动（{plan['totalChapters']}章）。\n"
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
    except providers.ProviderError:
        s["state"] = "options"
        return _NO_MODEL_REPLY + "\n配置完成后回复「重试」即可继续生成规划。"
    except Exception as e:
        s["state"] = "options"
        return f"⚠️ 规划生成失败：{e}\n回复「重试」再试一次，或调整配置。"


# ---------- v5.5.2：直接开聊建书（无绑定会话 → create_book → 绑定新书 → 规划开写） ----------

def _persist_unbound_turn(proj, user_msg, reply):
    """直接开聊会话（请求无 key）的对话轮次落进当前绑定书的 chat_log。

    v2api 只在请求带 key 时落盘；直聊建书后服务端会话已绑定新书，但前端仍以无 key
    直聊继续——不补记的话，总编辑读 chat_log 会漏掉建书后的这几轮（上下文断层）。
    带 key 的会话由 v2api 正常落盘，调用方（chat）已用 key is None 区分，不重复记。"""
    if proj is None:
        return
    try:
        from bot.v2api import _save_chat_msg as _scm
        if user_msg:
            _scm(proj, "user", str(user_msg))
        if reply:
            _scm(proj, "assistant", str(reply))
    except Exception:
        pass


def _chat_unbound(s, msg):
    """直接开聊（无绑定会话）：总编辑先理解，作者想写什么书就真的建书。

    返回回复文本（已处理）或 None（总编辑不可用/没接住 → 调用方回落旧的主题提取流程）。
    动作纪律：无绑定会话里只有 create_book 有标的——
    · 决策含 create_book → 执行建书并把会话绑到新书；
    · 没有任何动作（纯 discuss / 引导）→ 返回总编辑的回答；
    · 只有其他动作（write/rewrite… 在空会话没有标的，且不是纯讨论）→ 返回 None 走旧链路，
      不让幻觉动作吞掉消息。"""
    if s.get("project"):
        return None          # 已绑定：attached 分支处理，绝不在这里换绑新书（防误触发）
    try:
        _ctx = chief_mod.build_unbound_context(digest=s.get("digest") or "")
        decision = chief_mod.deliberate(msg, _ctx)
    except Exception:
        return None
    if not decision:
        return None
    acts = [a for a in (decision.get("actions") or []) if isinstance(a, dict)]
    _cb = next((a for a in acts if a.get("action") == "create_book"), None)
    reply = str(decision.get("reply") or "").strip()
    if _cb is None:
        if not acts and reply:
            return reply     # 纯讨论/引导（含 new_project 指引类决策）
        return None
    # 防重复投递：建书期间挡住同会话的连击短消息（与 options 阶段同一纪律）
    s["busy"] = True
    try:
        _res, _new_key = _create_book_from_chat(s, _cb.get("params") or {}, user_msg=msg)
    except Exception as e:
        _res, _new_key = f"⚠️ 建书启动失败（{e}）。请稍后再说一次，或用左侧『新建作品』向导。", None
    finally:
        s["busy"] = False
    if not _new_key:
        return _res
    return ((reply + "\n\n") if reply else "") + _res


def _create_book_from_chat(s, params, user_msg=""):
    """「直接开聊」建书执行器：一段话 → 真的建一本书 → 会话绑定 → 后台规划自动定名开写。

    建书与规划链**完全复用 /api/create**（骨架占位 + 后台 fill_defaults/gen_titles/
    create_project），并按「极速模式」约定收口：规划完成自动采用首个书名候选定名
    （writer.pick_title：改名 + record_rename 别名 + handoff 交接行），按 config.batch_size
    自动开写；书名候选列表照常落盘（meta.title_candidates / gen_titles 产物），
    「改名XX」随时可改。不在这里复制任何建书逻辑。

    params: {idea, title?, genre?/tone?/platform?/chapters?（或打包在 preferences 对象里）,
             batch_size?, medium?}
    返回 (回复文本, 新书 key 或 None)。key=None 表示没建成（含参数缺失/接口拒绝）。"""
    params = params if isinstance(params, dict) else {}
    prefs = params.get("preferences") if isinstance(params.get("preferences"), dict) else {}

    def _p(*keys):
        for k in keys:                      # 平铺参数优先，preferences 兜底
            v = params.get(k)
            if v not in (None, ""):
                return v
        for k in keys:
            v = prefs.get(k)
            if v not in (None, ""):
                return v
        return None

    idea = str(_p("idea") or "").strip()
    if not idea:
        return ("想写哪一类？给我一句话——主题、主角或一个画面就行"
                "（例如「写一本都市文，主角是会读心的心理咨询师」），我直接建书开写。"), None
    medium = str(_p("medium") or "novel").strip().lower() or "novel"
    if medium not in getattr(medium_mod, "MEDIUMS",
                             ("novel", "script", "poetry", "prose", "thesis",
                              "children", "news", "copy", "other")):
        medium = "novel"
    payload = {"idea": idea,
               # 直接开聊 = 极速约定：不拦 awaiting_title，规划完成自动采用首个候选定名并开写
               "express": "express",
               "medium": medium}
    _t = str(_p("title") or "").strip()
    if _t:
        payload["title"] = _t
    # 只透传字符串安全的参数：style 是向导专属的结构化对象（题材/金手指/…），
    # 总编辑给不了这个形状，透传字符串会让 /api/create 的 learn.record_book 崩。
    for _k in ("genre", "tone", "platform", "batch_size", "form", "perspective"):
        _v = _p(_k)
        if _v not in (None, ""):
            payload[_k] = str(_v)
    _chs = _p("chapters", "total_chapters")
    if _chs not in (None, ""):
        _m = re.search(r"\d+", str(_chs))
        if _m:
            payload["total_chapters"] = max(1, min(5000, int(_m.group())))
    # 会话里记录的供应商/模型选择（REST /api/chat 存过）随建书贯穿流水线
    prov = s.get("prov") or {}
    if prov.get("_provider_id"):
        payload["provider_id"] = prov["_provider_id"]
    if prov.get("_model"):
        payload["model"] = prov["_model"]
    try:
        res = api_create(payload)
    except Exception as e:
        return f"⚠️ 建书启动失败（{e}）。请稍后再说一次，或用左侧『新建作品』向导。", None
    if not isinstance(res, dict):
        # JSONResponse（书名非法等）→ 建书被拒，如实回报，不静默
        _err = "参数不合法"
        try:
            _err = (json.loads(res.body) or {}).get("error") or _err
        except Exception:
            pass
        return f"⚠️ 建书失败：{_err}", None
    new_key = str(res.get("key") or res.get("project") or "")
    if not new_key:
        return "⚠️ 建书失败：服务端没有返回新书的 key。", None
    # 会话重绑：占位 key 立即生效；规划完成 pick_title 改名后，旧 key 走 rename 别名
    # 自动落新家（_get_project 解析 + _chat_attached 的 canonical key 规范化）。
    s["project"] = new_key
    s["state"] = "attached"
    s.pop("titles", None)
    s.pop("cfg", None)
    _trace("direct-create", sid=s.get("id") or "", key=new_key, medium=medium,
           idea=str(idea)[:40])
    # 回复口径：骨架计划里的真实规划章数（此刻章卡未生成，规划完成后即 N 章 pending）
    total = 0
    genre_label = str(payload.get("genre") or "")
    try:
        _proj0 = _get_project(new_key)
        _plan0 = (_proj0.plan() or {}) if _proj0 else {}
        total = int(_plan0.get("totalChapters") or 0)
        if not genre_label:
            genre_label = str((_plan0.get("config") or {}).get("genre") or "")
    except Exception:
        pass
    _name = payload.get("title") or _short_title(idea)
    _med_label = {"novel": "小说", "script": "剧本", "poetry": "诗词", "prose": "散文",
                  "thesis": "论文", "children": "童书", "news": "新闻",
                  "copy": "文案", "other": "图文"}.get(medium, medium)
    _g = f"（{_med_label}{'·' + genre_label if genre_label else ''}）"
    _c = f"，规划 {total} 章" if total else ""
    reply = (f"📖 已建书《{_name}》{_g}{_c}：规划师正在生成人物档案/大纲/文风基准"
             f"（约 1-2 分钟），完成后自动采用首个书名候选定名并开写，进度在本对话全程汇报。\n"
             f"· 说「继续」推进写作；书名不满意随时说「改名XX」（书名候选已保留备选）。\n"
             f"· 这本书已绑定本对话，之后的每句话我都按它处理；也可以在左侧「作品」里随时打开。")
    # 这轮「无绑定对话」落进新书的 chat_log：作者的原话 + 本次回复。直聊请求到达时还没有
    # key，v2api 不会落盘；总编辑接「继续/改设定」、前端打开作品回放历史全靠这段留档。
    try:
        from bot.v2api import _save_chat_msg as _scm
        _p2 = _get_project(new_key)
        if _p2 is not None:
            if user_msg:
                _scm(_p2, "user", str(user_msg))
            _scm(_p2, "assistant", reply, {"type": "note", "direct_create": True, "key": new_key})
    except Exception:
        pass
    return reply, new_key


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

# ---------- v2.3.0：attached 会话（对话绑定本书）与标题体系 ----------

# attached 会话里判断「想开新书」：必须带宾语（部/本/作品/小说），裸「写」「重新」不算——
# 「第12章重新写一下」「写得更紧凑」是改本书，不是开新书。
_NEW_BOOK_PAT = (r"新作品|新的小说|新开一|开.{0,2}新书|起.{0,2}新书|再写一[部本]|来一[部本]|"
                 r"写一[部本]新|写[部本]新|重新写一[部本]|重新开一|换一[部本]新|另起一[部本]")
_CN_DIG = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

def _cn_to_int(s):
    """支持 一~九十九 / 数字字符串，够对话解析章号用。"""
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, b = s.split("十", 1)
        t = _CN_DIG.get(a, 1) if a else 1
        u = _CN_DIG.get(b, 0) if b else 0
        if a and a not in _CN_DIG or (b and b not in _CN_DIG):
            return None
        return t * 10 + u
    v = 0
    for ch in s:
        if ch not in _CN_DIG:
            return None
        v = v * 10 + _CN_DIG[ch]
    return v or None

def _parse_chapter_no(text):
    """从消息里解析「第N章」章号；没有返回 None。"""
    m = re.search(r"第\s*(\d{1,4})\s*章", text)
    if m:
        return int(m.group(1))
    m = re.search(r"第\s*([〇零一二两三四五六七八九十]{1,4})\s*章", text)
    if m:
        return _cn_to_int(m.group(1))
    return None

def _next_pending_chapter(plan):
    """plan 里下一个未完成章的章号（queue 回复文案用），取不到返回 None。"""
    for c in plan.get("chapters") or []:
        if str(c.get("status")) != "completed":
            no = c.get("chapterNumber")
            if isinstance(no, int) and no > 0:
                return no
    return None

def _clean_book_title(title):
    """书名清洗：与 _pick_title / create_project 同一套字符规则。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "", str(title or ""))

def _apply_title(key, title):
    """改名核心：清洗书名 → 目录改名（保留时间戳前缀）→ 02/02c 写回 → 索引与会话引用换新 key。

    返回 {ok, key, name}；目录不存在/重名/非法名一律 {ok: False, error} 不崩。"""
    title = _clean_book_title(title)
    if not title:
        return {"ok": False, "error": "书名不能为空"}
    proj = _get_project(key)
    if not proj:
        return {"ok": False, "error": "项目不存在或未加载，请刷新作品库"}
    if not os.path.isdir(proj.path):
        return {"ok": False, "error": "作品目录不存在"}
    base = os.path.basename(proj.path)
    _p = base.split("-", 2)
    prefix = "-".join(_p[:2]) + "-" if len(_p) >= 2 else base + "-"
    try:
        target = safe_join(writer.PROJECTS_DIR, prefix + title, label="目录名")
    except ValueError:
        return {"ok": False, "error": "书名不合法"}
    if target != proj.path and os.path.exists(target):
        return {"ok": False, "error": "已有同名作品，请先处理冲突"}
    if target != proj.path:
        os.rename(proj.path, target)
    new_key = os.path.basename(target)
    # v4.24.16 修复：目录改名后必须持久化别名——此前只更新内存 PROJECT_INDEX/SESSIONS，
    # 服务重启后所有持旧 key 的标签页/书签全部 404「项目不存在」
    # （writer.pick_title 的改名路径早就调 record_rename，这里补齐同款）。
    try:
        writer.record_rename(key, new_key)
    except Exception:
        pass
    proj.path = target
    proj.name = title
    # 计划文件改名：v3.3.0 修复——此前硬编码写 02-写作计划.json 的 novelName，
    # 剧本/诗词/散文改名完全无效。改走 writer._plan_of 按文体写各自的 *Name 字段。
    _med, _plan_fp, _plan_data = writer._plan_of(target)
    if _med and _plan_fp and isinstance(_plan_data, dict):
        _name_key = dict(getattr(writer, "_NAME_KEY", {})).get(_med, "novelName")
        _plan_data[_name_key] = title
        _plan_data["projectPath"] = target
        atomic_write_json(_plan_fp, _plan_data)
    else:
        plan = proj.plan() or {}
        plan["novelName"] = title
        plan["projectPath"] = target
        proj.save_plan(plan)
    # 02c-总规划.json：仅当已有 novelName/name 字段时同步
    cplan = os.path.join(target, "02c-总规划.json")
    if os.path.exists(cplan):
        try:
            d = json.load(open(cplan, encoding="utf-8"))
            if isinstance(d, dict):
                if "novelName" in d:
                    d["novelName"] = title
                if "name" in d:
                    d["name"] = title
                atomic_write_json(cplan, d)
        except Exception:
            pass
    # 内存索引 + 所有引用旧 key 的会话
    PROJECT_INDEX.pop(key, None)
    PROJECT_INDEX[new_key] = proj
    for sess in SESSIONS.values():
        if sess.get("project") == key:
            sess["project"] = new_key
    # v4.26.0 修复「选完书名就停」：采用书名走的是改名接口（右栏「采用」→ /api/title/set），
    # 此前只改目录名不启动创作（真正开写的 /api/pick-title 前端这条链路从不调用）——
    # 引导创建的书选完书名永远停在 awaiting_title。现在检测到待选书名状态：
    # 清标记 → 启动创作 → 对话流写入确认回复（总编辑随即接手汇报分工）。
    # v4.34.1 幻影双书根治：规划仍在途（/api/create 后台线程 planning_running=True）
    # 时绝不在这里抢跑开写——否则新写作线程与后台规划线程在同一本书上双规划打架，
    # 后台线程规划完还会把 awaiting_title 状态盖回来。改置 meta.title_picked，
    # 由后台线程规划完自动开写（app.py _bg 收尾已配合同款分支）。
    started = False
    _was_awaiting = False
    try:
        _med2, _fp2, _plan2 = writer._plan_of(target)
        if _plan2 and (_plan2.get("meta") or {}).get("awaiting_title"):
            _was_awaiting = True
            _meta2 = _plan2.setdefault("meta", {})
            _meta2["awaiting_title"] = False
            # v4.34.1：在途判据用 livejob（/api/create _bg 规划线程 begin/finish 登记）
            # ——骨架 meta.planning_running 会被 create_project 的整体写盘抹掉，不可靠。
            if livejob.is_active(key) or livejob.is_active(new_key):
                _meta2["title_picked"] = True
                atomic_write_json(_fp2, _plan2)
                started = True
            else:
                _plan2["status"] = "in_progress"
                atomic_write_json(_fp2, _plan2)
                started = writer.start_writing(proj, batch_size=((_plan2.get("config") or {}).get("batch_size")))
                try:
                    from bot.v2api import _save_chat_msg
                    _save_chat_msg(proj, "assistant",
                                   f"✅ 已收到，将以《{title}》进行创作，现在马上安排创作——总编辑已接手，"
                                   f"规划师/作家/审稿人各就各位，进度会在本对话全程汇报。",
                                   {"type": "note"})
                except Exception:
                    pass
    except Exception:
        pass
    # v5.5：交接语——换 key 之后新会话（或被跟随过来的旧会话）的第一份落盘上下文
    # 必须带全：书名 / 真实进度 / 下一步动作。总编辑读 chat_log 接「继续」全靠它，
    # 没有它就会出现《未命名》空书回答（实测开书断层）。规划仍在途（title_picked）
    # 与立即开写两条路径都要落这条交接。
    if _was_awaiting:
        try:
            from bot.v2api import _save_chat_msg
            _hp = writer._plan_of(target)[2] or {}
            _hc = _hp.get("chapters") or []
            _hd = writer.done_chapters(_hp, target) if _hc else 0
            _ht = len(_hc) or int(_hp.get("totalChapters") or 0)
            _hn = next((int(c.get("chapterNumber") or 0) for c in _hc
                        if str(c.get("status")) != "completed"), 0)
            _hnext = f"接下来从第 {_hn} 章接着写" if _hn else "规划就绪后立即开写第 1 章"
            _save_chat_msg(proj, "assistant",
                           f"📌 书名已确定为《{title}》，创作已在新会话继续。"
                           f"当前进度 {_hd}/{_ht} 章；{_hnext}。"
                           f"说「继续」我就按这个进度推进。",
                           {"type": "note", "handoff": True, "key": new_key})
        except Exception:
            pass
    return {"ok": True, "key": new_key, "name": title, "started": bool(started)}


def _retry_planning_bg(key: str) -> bool:
    """v5.5：规划中断自愈——同一目录重跑规划线程（不另起书、不换 key）。

    向导开书是「先落 0 章骨架、后台线程再补规划」；规划线程被 LLM 524/超时打断后，
    书会停在 failed / 空章卡。作者此时说「继续」绝不能再被当成空书处理：
    这里重启规划（gen_titles + create_project 写回同一 pdir），完成后按书名状态收口
    ——已选名/极速 → 直接开写；未选名 → awaiting_title 等选名。
    返回 True=已启动重跑线程。"""
    proj = _get_project(key)
    if not proj:
        return False
    plan = proj.plan() or {}
    if plan.get("chapters"):
        return False   # 已有章卡：不是规划中断，走正常写作路由
    cfg = dict(plan.get("config") or {})
    if not cfg:
        return False
    meta = plan.get("meta") or {}
    if meta.get("planning_running"):
        try:
            if livejob.is_active(os.path.basename(proj.path)):
                return False   # 真有规划在跑：不并发重建（与 _ensure_writable_plan 同一纪律）
        except Exception:
            return False
    bkey = os.path.basename(proj.path)
    name = _clean_book_title(plan.get("novelName") or proj.name) or "未命名"
    meta = plan.setdefault("meta", {})
    meta["planning_running"] = True
    meta.pop("error", None)
    plan["status"] = "planning"
    proj.save_plan(plan)
    proj.log("🔁 规划中断自愈：重新启动规划（同一本书，不换目录）")

    def _bg():
        livejob.begin(bkey, kind="planning", worker_label="规划师",
                      detail="重跑被中断的规划…")
        try:
            cfg2 = workflow.fill_defaults(cfg)
            titles = plan.get("titles") or [{"title": name, "technique": "恢复", "why": ""}]
            try:
                cands = workflow.gen_titles(cfg2, n=8)
                if cands:
                    titles = cands
            except Exception:
                pass
            writer.create_project(cfg2, titles, pdir=proj.path, name=name)
            p2 = proj.plan() or {}
            m2 = p2.setdefault("meta", {})
            m2["planning_running"] = False
            picked = m2.get("title_picked") or m2.get("selected_title")
            if picked:
                m2["awaiting_title"] = False
                p2["status"] = "in_progress"
                proj.save_plan(p2)
                proj.log("✅ 规划重跑完成（书名已定），自动开始创作")
                writer.start_writing(proj, batch_size=(p2.get("config") or {}).get("batch_size"))
            else:
                m2["awaiting_title"] = True
                m2["title_candidates"] = titles
                p2["status"] = "awaiting_title"
                proj.save_plan(p2)
                proj.log("✅ 规划重跑完成，等待作者选书名")
        except Exception as e:
            print(f"[retry-planning] 重跑失败: {e}", flush=True)
            try:
                p2 = proj.plan() or {}
                p2["status"] = "failed"
                m2 = p2.setdefault("meta", {})
                m2["planning_running"] = False
                m2["error"] = str(e)[:200]
                proj.save_plan(p2)
            except Exception:
                pass
        finally:
            try:
                livejob.finish(bkey, ok=True, detail="重规划线程结束")
            except Exception:
                pass

    threading.Thread(target=_bg, daemon=True).start()
    return True


def _planning_interrupted(proj) -> bool:
    """v5.5：判断一本书是否停在「规划被打断」状态（需要自愈而不是照常路由）。

    三种形态：
    · 账面 failed / meta.error（LLM 失败，/api/create _bg 的 except 落盘）；
    · status=planning/awaiting_title 但既没有 livejob 也没有 planning_running
      （规划线程没跑起来就死了）；
    · planning_running=True 但 livejob 已不在且计划文件 90 秒没动过
      （服务重启把规划线程杀掉，标志残留）。"""
    try:
        plan = proj.plan() or {}
        if plan.get("chapters"):
            return False
        meta = plan.get("meta") or {}
        status = str(plan.get("status") or "")
        if status == "failed" or meta.get("error"):
            return True
        live = False
        try:
            live = bool(livejob.is_active(os.path.basename(proj.path)))
        except Exception:
            live = False
        if status in ("planning", "awaiting_title"):
            if not meta.get("planning_running") and not live:
                return True
            if meta.get("planning_running") and not live:
                try:
                    fp = os.path.join(proj.path, "02-写作计划.json")
                    mt = os.path.getmtime(fp) if os.path.exists(fp) else 0
                    if time.time() - mt > 90:
                        return True
                except Exception:
                    pass
        return False
    except Exception:
        return False


def _title_progress_line(key) -> str:
    """v5.5：书名确认回复的进度交接尾巴（书已有章卡时给出真实进度与下一步）。"""
    try:
        p = _get_project(key) if key else None
        if p is None:
            return ""
        pl = p.plan() or {}
        chs = pl.get("chapters") or []
        if not chs:
            return "\n（规划就绪后自动开写第 1 章，说「继续」可随时推进。）"
        d = writer.done_chapters(pl, p.path)
        n = next((int(c.get("chapterNumber") or 0) for c in chs
                  if str(c.get("status")) != "completed"), 0)
        return f"\n（当前进度 {d}/{len(chs)} 章；说「继续」就从第 {n} 章接着写。）" if n \
            else f"\n（当前进度 {d}/{len(chs)} 章。）"
    except Exception:
        return ""


def _read_title_items(key):
    """读 05-标题候选.json 的 items（无文件返回 []）。"""
    try:
        pdir = writer._project_dir(key)
    except ValueError:
        return []
    fp = os.path.join(pdir, "05-标题候选.json")
    if not os.path.exists(fp):
        return []
    try:
        return (json.load(open(fp, encoding="utf-8")) or {}).get("items") or []
    except Exception:
        return []

def _generate_title_candidates(key, n=5):
    """基于本书生成候选标题并落盘 05-标题候选.json（与已有条目去重、封顶 20）。

    素材：plan 的 config/meta（题材/基调等）+ 大纲尾部 + plan_digest 进度摘要；
    出词复用 workflow.gen_titles(cfg, n=n)。失败返回 []。"""
    proj = _get_project(key)
    if not proj:
        return []
    # v3.3.0：拟题泛化到四文体——按 _plan_of 读各自计划，素材按文体取
    _med, _plan_fp, _plan = writer._plan_of(proj.path)
    if _med and _plan:
        plan = _plan
    else:
        plan = proj.plan() or {}
        _med = "novel"
    cfg = dict(plan.get("config") or {})
    cfg["medium"] = _med
    meta = plan.get("meta") or {}
    for k in ("genre", "tone", "theme", "idea", "conflict", "protagonist"):
        if not cfg.get(k) and meta.get(k):
            cfg[k] = meta[k]
    if not cfg.get("idea"):
        # 创意种子：小说取大纲尾部；剧本取创作圣经；诗词取题记/意象表；散文取 brief
        _seeds = {"novel": ("02-大纲.md", "01-大纲.md"),
                  "script": ("00-创作圣经.md",),
                  "poetry": ("00-题记.md", "01-意象表.md"),
                  "prose": ("00-brief.md", "01-素材.md")}.get(_med, ("02-大纲.md", "01-大纲.md"))
        for fn in _seeds:
            p = os.path.join(proj.path, fn)
            if os.path.exists(p):
                try:
                    tail = open(p, encoding="utf-8").read()[-900:].strip()
                except Exception:
                    tail = ""
                if tail:
                    cfg["idea"] = tail[-160:]
                break
    try:
        from bot import longform as _lf
        d = _lf.plan_digest(proj) or {}
        if d.get("summary"):
            cfg.setdefault("special", str(d["summary"]))
    except Exception:
        pass
    try:
        titles = workflow.gen_titles(cfg, n=n)
    except Exception:
        return []
    items = [t for t in (titles or []) if isinstance(t, dict) and t.get("title")]
    if not items:
        return []
    at = time.strftime("%Y-%m-%dT%H:%M:%S")
    for x in items:
        x.setdefault("at", at)
    fp = os.path.join(proj.path, "05-标题候选.json")
    old = []
    if os.path.exists(fp):
        try:
            old = (json.load(open(fp, encoding="utf-8")) or {}).get("items") or []
        except Exception:
            old = []
    merged, seen = [], set()
    for x in items + [o for o in old if isinstance(o, dict) and o.get("title")]:
        t = str(x.get("title"))
        if t in seen:
            continue
        seen.add(t)
        merged.append({"title": t, "technique": x.get("technique", ""),
                       "why": x.get("why", ""), "at": x.get("at") or at})
    merged = merged[:20]
    atomic_write_json(fp, {"updated_at": at, "items": merged})
    return items

def _chat_apply_title(s, key, title):
    res = _apply_title(key, title)
    if not res.get("ok"):
        return f"⚠️ 改名失败：{res.get('error') or '未知错误'}"
    s["project"] = res["key"]
    s["title_candidates"] = None
    return f"✅ 《{res['name']}》已改好。"

def _chat_title_cmd(s, key, msg):
    """chat 内标题命令：生成候选 / 采用N / 改名为Y（与 /api/title/* 同一套底层函数）。"""
    if (re.search(r"几个|候选|再想想|重新起|想想|起几个|来几个", msg)
            or re.fullmatch(r"(标题|书名|改标题|改书名|换标题|换书名|重命名|改名|换名)", msg)):
        items = _generate_title_candidates(key, n=5)
        if not items:
            return "⚠️ 标题候选生成失败（模型不可用或写作计划缺失），稍后再试。"
        s["title_candidates"] = items
        lines = [f"【{i + 1}】《{x.get('title', '')}》—— {x.get('technique', '')}，{x.get('why', '')}"
                 for i, x in enumerate(items[:5])]
        return ("📚 为本书拟了几组标题候选：\n" + "\n".join(lines)
                + "\n\n回复数字采用，或直接说「改名为Y」。")
    m = re.search(r"采用\s*(\d+)", msg)
    if m:
        i = int(m.group(1)) - 1
        items = s.get("title_candidates") or _read_title_items(key)
        if 0 <= i < len(items) and items[i].get("title"):
            return _chat_apply_title(s, key, items[i]["title"])
        return "⚠️ 没有这个编号的候选，先说「给我几个书名」生成一批。"
    m = re.search(r"(?:改名为|改名成|改名叫|改叫|换名为|换名成|换名叫|重命名为|重命名成|书名叫|小说名叫)\s*"
                  r"[《「\"']?([^》」\"'\s，。]+)", msg)
    if m and m.group(1):
        return _chat_apply_title(s, key, m.group(1))
    return "关于标题：说「给我几个书名」生成候选；「采用N」用第 N 个；或直接说「改名为Y」。"

def _chat_group(s, proj, msg, plan, st, name):
    """v2.6.1：@Agent 群聊模式。
    解析被 @ 的 Agent 列表，注入完整作品上下文（计划摘要、已写进度、人物设定），
    让每个被 @ 的 Agent 都能看到全部交流内容并有序回复。"""
    # 解析 @Agent名 列表（支持中文 Agent 名称）
    at_names = re.findall(r"@([^\s@]+)", msg)
    if not at_names:
        return "请在 @ 后输入要邀请的 Agent 名称。"
    # 去掉 @Agent 前缀，提取实际问题
    question = re.sub(r"@[^\s@]+\s*", "", msg).strip()
    if not question:
        question = "请汇报当前进度和遇到的问题。"

    # 构建作品上下文摘要
    # v3.2.0：剧本/诗词/散文的 @Agent 也用各自文体的上下文（此前全是小说口径空壳）
    _kb = os.path.basename(proj.path)
    _med = {"s_": "script", "p_": "poetry", "e_": "prose", "t_": "thesis",
            "c_": "children", "n_": "news", "k_": "copy", "o_": "other"}.get(_kb[:2])
    if _med:
        try:
            from bot import assist as _assist
            ctx_block = _assist._medium_context(proj, _kb, _med, plan if isinstance(plan, dict) else {})
            if question:
                ctx_block += f"\n\n【用户问题】\n{question}\n"
            context = ctx_block
            # 跳到统一的 LLM 调用段（下方小说上下文构建逻辑不适用）
            return _chat_group_llm(s, context, question, at_names, name)
        except Exception:
            pass
    chs = plan.get("chapters") or []
    done = writer.done_chapters(plan, proj.path)
    total = len(chs)
    chars = plan.get("characters") or plan.get("cast") or []
    char_summary = ""
    if chars:
        char_summary = "\n".join(
            f"  - {c.get('name','')}：{c.get('role','') or c.get('desc','')}"[:120]
            for c in chars[:10]
        )
    outline_tail = ""
    for fn in ("02-大纲.md", "01-大纲.md"):
        p = os.path.join(proj.path, fn)
        if os.path.exists(p):
            try:
                outline_tail = open(p, encoding="utf-8").read()[-600:].strip()
            except Exception:
                pass
            break

    context = (
        f"【作品上下文】\n"
        f"书名：{name}\n"
        f"进度：{done}/{total} 章已完成，已写 {st.get('words', 0)} 字\n"
        f"状态：{'创作中' if st.get('running') else '已暂停' if st.get('paused') else '在库'}\n"
    )
    if char_summary:
        context += f"主要角色：\n{char_summary}\n"
    if outline_tail:
        context += f"大纲尾部摘要：\n{outline_tail}\n"
    context += f"\n【用户问题】\n{question}\n"

    if s.get("digest"):
        context += f"\n对话进度摘要（自动压缩）：\n{s['digest'][:1500]}\n"
    return _chat_group_llm(s, context, question, at_names, name)


def _chat_group_llm(s, context, question, at_names, name):
    """@Agent 群聊的统一 LLM 调用段（v3.2.0：小说/剧本/诗词/散文共用）。"""
    # 构建 Agent 角色映射
    try:
        av = agents.bindings_view() or {}
        all_agents = av.get("agents") or []
    except Exception:
        all_agents = []
    agent_map = {}
    for a in all_agents:
        label = a.get("label") or ""
        agent_key = a.get("key") or a.get("agent") or ""
        stage = a.get("stage") or ""
        desc = a.get("desc") or ""
        if label:
            agent_map[label] = a
        if agent_key:
            agent_map[agent_key] = a

    # 为每个被 @ 的 Agent 构建回复
    replies = []
    for aname in at_names:
        a = agent_map.get(aname)
        if not a:
            # 模糊匹配
            for k, v in agent_map.items():
                if aname in k or k in aname:
                    a = v
                    aname = a.get("label") or k
                    break
        if not a:
            replies.append(f"【{aname}】未找到此 Agent，请检查名称。")
            continue

        label = a.get("label") or aname
        stage = a.get("stage") or ""
        desc = a.get("desc") or ""
        system_prompt = (
            f"你是《{name}》创作团队中的「{label}」岗位（{stage}）。"
            f"职责：{desc}。\n"
            f"你正在参与群聊讨论。其他 Agent 也在场，你们各自从自己的专业角度回复。"
            f"不要重复其他人的发言，直接给出你的专业意见和行动建议。\n"
            f"{context}"
        )
        try:
            resp = llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ], cfg={})   # v4.17.4 修复：_chat_group_llm 无 plan 上下文，此前引用未定义的 plan → @Agent 群聊偶发 NameError
            text = resp.strip() if isinstance(resp, str) else str(resp).strip()
            if text:
                replies.append(f"【{label}】{text}")
        except Exception as e:
            replies.append(f"【{label}】（暂时无法响应：{e}）")

    header = f"👥 群聊讨论（{len(at_names)} 位 Agent 参与）\n问题：{question}\n"
    return header + "\n\n".join(replies)


# ══════════ v4.37.0：总编辑工作单 / 结构提案执行器 ══════════
_MED_OF_PREFIX = {"s_": "script", "p_": "poetry", "e_": "prose", "t_": "thesis",
                  "c_": "children", "n_": "news", "k_": "copy", "o_": "other"}


def _med_of_key(key):
    return _MED_OF_PREFIX.get(str(key or "")[:2], "novel")


def _persist_brief(proj, brief_data, source_text, kind="directive"):
    """把总编辑翻译好的工作单落盘。失败返回 {}，不影响主链路。"""
    try:
        pdir = getattr(proj, "path", "") or ""
        return brief_mod.add(pdir, brief_data or {}, source_text=source_text, kind=kind) if pdir else {}
    except Exception:
        return {}


def _brief_for_rewrite(brief_data, chapters):
    """一次性改写要求不能悄悄变成全书长期政策：没给范围就锁到本次重写的章号上。"""
    b = dict(brief_data or {})
    _chs = sorted({int(x) for x in (chapters or []) if str(x).strip().isdigit()})
    if not b.get("scope"):
        b["scope"] = "chapters"
        b["chapters"] = _chs
    elif b.get("scope") == "chapters" and not b.get("chapters"):
        b["chapters"] = _chs
    return b


def _exec_directive(proj, key, msg, brief_data):
    b = dict(brief_data or {})
    b.setdefault("scope", "book")
    if not b.get("goal"):
        b["goal"] = msg[:400]
    if _med_of_key(key) != "novel":
        # 非小说没有章节队列，保存工作单后立刻走改稿通道
        _persist_brief(proj, b, msg)
        from bot import assist as _assist_mod
        try:
            res = _assist_mod.assist(key, _med_of_key(key), msg)
        except Exception as e:
            return f"⚠️ 改稿请求失败：{e}"
        if res and res.get("ok"):
            return res.get("text") or "已处理。"
        return (res or {}).get("text") or "这条要求我没能落到稿子上，请再说具体一点。"
    item = _persist_brief(proj, b, msg)
    if not item:
        if writer.queue_msg(key, msg):
            return "这条我记下了，会并入接下来的创作。"
        return "⚠️ 这条要求没能存下来（写作计划缺失？），请检查这本书是否完整。"
    scope, chs = item.get("scope"), item.get("chapters") or []
    if scope == "next":
        lines = brief_mod.render_lines(item)
        if writer.queue_msg(key, "；".join(lines) or msg, immediate=True):
            brief_mod.set_status(getattr(proj, "path", ""), item["id"], "done")
            return "记下了。下一章会按这个写，不用你再提醒。"
        return "记下了，但当前没法排进下一章（写作计划缺失），后续章节会按它执行。"
    if scope == "chapters":
        return (f"我记到第{'、'.join(map(str, chs))}章的工作单上了。"
                f"要我现在就按这个改这一{'章' if len(chs) == 1 else '几章'}吗？")
    return "记下了。从接下来这一章起，写作、改写、审稿都会按这个要求执行。"


def _exec_show_briefs(proj, key):
    try:
        items = brief_mod.open_items(getattr(proj, "path", "") or "")
    except Exception:
        items = []
    if not items:
        return "现在没有挂着的工作单。你直接把要求说出来，我会先理解，再翻译成团队能执行的指令。"
    lines = [f"📋 当前生效 {len(items)} 张工作单（新→旧）："]
    for i, x in enumerate(items[:12], 1):
        lines.append(f"{i}. {brief_mod.render_one(x)}")
    if len(items) > 12:
        lines.append(f"…另有 {len(items) - 12} 张较早的工作单。")
    return "\n".join(lines)


def _exec_revoke_briefs(proj, params):
    pdir = getattr(proj, "path", "") or ""
    try:
        items = brief_mod.open_items(pdir)
    except Exception:
        items = []
    if not items:
        return "现在没有生效中的工作单，不用撤。"
    target = None
    idx = params.get("index")
    if idx not in (None, ""):
        try:
            i = int(idx) - 1
            if 0 <= i < len(items):
                target = items[i]
        except (TypeError, ValueError):
            pass
    if target is None and params.get("chapter") not in (None, ""):
        try:
            ch = int(params["chapter"])
        except (TypeError, ValueError):
            ch = None
        if ch:
            target = next((x for x in items if x.get("scope") == "chapters"
                           and ch in (x.get("chapters") or [])), None)
            if target is None:
                return f"第{ch}章没有挂着章级工作单（全书要求不会被单章撤销）。"
    if target is None:
        target = items[0]
    if brief_mod.revoke(pdir, brief_ids=[target.get("id")]):
        return f"撤掉了这条工作单：{brief_mod.render_one(target)}。之后写作、改写、评分都不再按它执行。"
    return "⚠️ 撤销没成功（工作单文件可能被占用），请稍后再试。"


def _exec_plan_change(proj, key, msg, params, brief_data):
    if _med_of_key(key) != "novel":
        return "结构调整目前只支持小说；这类文体可以直接说「第几段改成什么」，我让改稿师处理。"
    if brief_data:
        _persist_brief(proj, brief_data, msg, kind="plan")
    scope = str(params.get("scope") or "window").strip().lower()
    if scope not in ("book", "volume", "arc", "window", "chapter"):
        scope = "window"
    instruction = str(params.get("instruction") or msg).strip()[:4000]
    try:
        from bot import longform as _lf
        prop = _lf.replan(proj, scope=scope, instruction=instruction, reason="author")
    except Exception as e:
        return f"⚠️ 结构重规划没跑起来：{e}"
    if not prop:
        return "⚠️ 这次没能生成结构提案，请稍后再试。"
    _what = prop.get("summary") or "结构调整"
    _aff = prop.get("affected_chapters") or []
    _range = f"第{min(_aff)}–{max(_aff)}章" if _aff else "后续章节"
    if prop.get("requires_past_rewrite"):
        return (f"我按你的意思拟了一版结构调整：{_what}（涉及{_range}，含已写章节）。"
                f"改动会动到成稿，先放在提案里——你说「按这个改」我再应用，并告诉你哪几章需要重写。")
    try:
        from bot import supervisor as _sup
        auto = _sup.autonomy_mode() == "auto"
    except Exception:
        auto = False
    if auto:
        try:
            res = _lf.apply_replan(proj, prop.get("id"), allow_past=False)
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        if res.get("ok"):
            return f"结构调整已落实：{_what}（{_range}）。后面的章节会按新结构推进。"
        return f"结构提案已生成：{_what}（{_range}），但自动应用没成功：{res.get('error')}。"
    return (f"我按你的意思拟了一版结构调整：{_what}（{_range}）。"
            f"提案已生成——你说「就按这个改」我就应用；不急着动就再放放。")


def _exec_apply_proposal(proj, params):
    try:
        from bot import longform as _lf
        props = _lf.list_proposals(proj, status="pending")
        if not props:
            return "现在没有待确认的结构提案。先说清你想怎么改，我来拟一版。"
        prop = props[0]
        res = _lf.apply_replan(proj, prop.get("id"), allow_past=bool(params.get("allow_past")))
        if res.get("ok") is False:
            err = str(res.get("error") or "")
            if "allow_past" in err:
                return ("这版提案会动到已经写完的章节。你确认要连成稿一起改的话，我就按它应用并安排重写；"
                        "只想改后面的章节就说「只改没写的」。")
            return f"⚠️ 提案没能应用：{err}"
        _aff = res.get("applied_cards") or []
        return ("✅ 已按提案调整结构"
                + (f"（涉及第{'、'.join(map(str, _aff[:12]))}章）" if _aff else "")
                + "。后面的写作会按新结构走。")
    except Exception as e:
        return f"⚠️ 应用提案失败：{e}"


def _exec_cancel_task(key, params):
    kind = str(params.get("kind") or "all").strip().lower()
    stopped = []
    if kind in ("rewrite", "all"):
        try:
            writer.cancel_rewrite(key)
            stopped.append("改写")
        except Exception:
            pass
    if not stopped:
        return "评分任务没有单独的停止开关；它跑完当前章就结束，不会改动正文。"
    return (f"已经让{'、'.join(stopped)}任务在下一个检查点停下——当前章可能还会写完，"
            f"之后的章节不会再开新轮。")


def _chat_attached(s, proj, msg):
    """attached 分支路由：待选书名/数字采纳 → @Agent → 总编辑理解并派活；
    总编辑不可用时回退标题规则/规划器/NLU/即时队列。"""
    key = s["project"]
    # v5.5：key 规范化——proj 可能已按 rename 别名落到新家（选书名 os.rename /
    # 极速自动定名）。磁盘目录名才是 canonical key；旧 key 仅作别名存续。
    # 不规范化的话，任务档案/创作事件/livejob/写作注册表全都查旧 key，
    # 总编辑把新书看成《未命名》空书（实测开书断层）。
    # v5.5.2：规范化放在 plan()/status() 读取之后——缓存住的 Project 实例要靠
    # 读盘触发 Project._follow_rename 懒跟随到新家；先读再规范化，改名后的
    # 第一轮消息就能拿到 canonical key（直聊建书的新书第一次自动定名即此场景）。
    plan = proj.plan() or {}
    st = proj.status()
    try:
        _ckey = os.path.basename(proj.path or "") if proj is not None else ""
    except Exception:
        _ckey = ""
    if _ckey and _ckey != key:
        key = _ckey
        s["project"] = _ckey
    if st.get("state") in ("missing", "", None):
        # 剧本/诗词/散文没有 02-写作计划.json：用全文体元数据补齐，
        # 否则「进度」会按小说口径回「第 0/0 章」这种死胡同回复。
        it = writer.library_item(key)
        if it:
            st["state"] = it.get("state") or st.get("state")
            st["name"] = it.get("name") or st.get("name")
            st["done"] = it.get("done", 0)
            st["total"] = it.get("total", 0)
            st["words"] = it.get("total_words", 0)
            st["running"] = bool(it.get("running"))
            st["paused"] = bool(it.get("paused"))
    name = st.get("name") or plan.get("novelName") or os.path.basename(proj.path)
    # 只信线程级 running（Project.running）。plan 的 state=in_progress 会跨重启残留，
    # 拿它当「活着」会让「继续」误报正在创作而不真正续写。
    running = bool(st.get("running"))
    # 候选给出后的数字回复 = 采用（认「2」「采用2」「选2」「#2」）
    m = re.fullmatch(r"(?:采用|选|用|#)?\s*(\d{1,2})\s*[#号]?", msg) if s.get("title_candidates") else None
    if m:
        return _chat_title_cmd(s, key, "采用" + m.group(1))
    # ── v2.6.1：@Agent 群聊模式 ──
    # 消息以 @ 开头时，解析被 @ 的 Agent 列表，注入完整作品上下文后调用 LLM 群聊
    if msg.startswith("@"):
        return _chat_group(s, proj, msg, plan, st, name)

    # v4.30.0：待选书名状态下，回复数字 / 「采用N」 / 「改名X」直接采纳——
    # 此前这些回复会被折进创作队列（书名永远采纳不了），或经旧目录重建出第二个对话
    if (plan.get("meta") or {}).get("awaiting_title"):
        m_pick = re.fullmatch(r"(?:采用|选|用|#)?\s*([1-9]\d?)\s*[#号]?", msg.strip())
        m_rename = re.match(r"^(?:改名|书名)(?:叫|为|改成|换成)?\s*[《『\"]?(.{1,60}?)[》』\"]?$", msg.strip())
        if m_rename and m_rename.group(1).strip():
            res = _apply_title(key, m_rename.group(1).strip())
            if res.get("ok"):
                return (f"✅ 已收到，将以《{res.get('name')}》进行创作，现在马上安排创作——"
                        f"总编辑已接手，进度在本对话全程汇报。{_title_progress_line(res.get('key'))}")
            return f"⚠️ 书名应用失败：{res.get('error')}"
        if m_pick:
            titles = plan.get("titles") or []
            idx = int(m_pick.group(1)) - 1
            if 0 <= idx < len(titles):
                t = str((titles[idx] or {}).get("title") or "").strip()
                if t:
                    res = _apply_title(key, t)
                    if res.get("ok"):
                        return (f"✅ 已收到，将以《{t}》进行创作，现在马上安排创作——"
                                f"总编辑已接手，规划师/作家/审稿人各就各位，进度在本对话全程汇报。"
                                f"{_title_progress_line(res.get('key'))}")
                    return f"⚠️ 书名应用失败：{res.get('error')}"
            return f"该书名序号不存在，请回复 1-{len(titles) or 5}，或直接说「改名为你想用的书名」。"

    # v4.22.0：意图分发抽成嵌套函数 _run —— 供「LLM 规划器」逐步调用（多步任务/任意表述）
    def _run(intent, params, brief_data=None):
        nonlocal key, proj, plan, st, name, running
        # v5.5：每步动作执行前重取当前绑定——书名确认（set_title/_apply_title）
        # 与「继续写作」常在同一次总编辑决策里（actions=[set_title, write]），
        # 改名换 key 后后续步骤必须落到新书：状态查询/继续/写作一律不得再看到
        # 旧 key 的空书或《未命名》。
        _cur_key = str(s.get("project") or "") or key
        if _cur_key and _cur_key != key:
            _p2 = _get_project(_cur_key)
            if _p2 is not None:
                key = os.path.basename(_p2.path) or _cur_key
                s["project"] = key
                proj = _p2
                plan = proj.plan() or plan
                st = proj.status()
                name = st.get("name") or plan.get("novelName") or key
                running = bool(st.get("running"))
        params = params or {}
        # v5.5.1：动作执行链停摆自检——批量写作线程已死但注册表残留（_RUNNING_BOOKS /
        # Project.running / mark_pipeline / livejob）时，running 口径永远是 True，
        # 「继续」被防重入拦死、界面永远「创作中」却没动静（实测《实习生掉马那天》）。
        # 总编辑 write / resume_task / 批量写等一切动作执行前先收口残留，再刷新状态口径。
        if str(key)[:2] not in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
            try:
                if writer.registry_selfcheck(key, proj):
                    st = proj.status()
                    running = bool(st.get("running"))
            except Exception:
                pass
        # v4.37.0：总编辑动作名与旧 planner/NLU 的 intent 名并存，先做等价映射
        if intent == "discuss":
            return None
        if intent == "read_chat":
            # v4.38.1：跨作品读取聊天记录——作者问「读一下某本书最后交流了什么」时，
            # 总编辑直接读真实 chat_log，不凭记忆编造。
            return chief_mod.read_last_chat(
                str(params.get("project") or ""), limit=params.get("limit") or 6,
                current_key=key, current_pdir=proj.path)
        # v5.5.2：防呆——create_book 只属于无绑定的「直接开聊」会话。已绑定书时一律拒绝
        # 并给指引，防止在书里说「我想写本XX」误触发，把当前会话悄悄换绑到一本新书。
        if intent == "create_book":
            return (f"当前对话绑定的是《{name}》，建书要在没有绑定作品的「直接开聊」里说"
                    f"（首页的『直接开聊』入口，或说一句「我想写一本XX」）。\n"
                    f"要继续《{name}》直接说「继续」或提要求；要另开新书也可以点左侧『新建作品』走向导。")
        if intent == "health_check":
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                return "体检目前针对小说章节；其他文体可以直接说「检查全文」走对应审校。"
            try:
                _rep = writer.book_health.preflight(proj, "作者手动体检")
            except Exception as e:
                return f"⚠️ 体检失败：{e}"
            _a = _rep.get("audit") or {}
            _rr = _rep.get("repair") or {}
            _fixed = len(_rr.get("repaired") or [])
            _left = {"字数不足": len(_a.get("short") or []),
                     "结尾截断": len(_a.get("truncated") or []),
                     "说明文字": len(_a.get("meta") or []),
                     "缺文件": len(_a.get("missing") or [])}
            _left = {k: v for k, v in _left.items() if v}
            _extra = []
            try:
                from bot import canon as _canon
                _cd = _rep.get("canon") or {}
                if _cd:
                    _extra.append(_canon.brief(_cd))
            except Exception:
                pass
            try:
                from bot import engine_check as _eng
                _ed = _eng.scan(proj)
                if _ed.get("counts", {}).get("high") or _ed.get("counts", {}).get("warn"):
                    _extra.append(_eng.brief(_ed))
            except Exception:
                pass
            _tail = ("\n" + "\n".join("· " + x for x in _extra)) if _extra else ""
            if not _fixed and not _left:
                return (f"✅ 《{name}》体检通过：{_a.get('total', 0)} 章正文完整，"
                        f"没有字数不足、截断或说明文字。" + _tail)
            _msg = f"🩺 《{name}》体检完成："
            if _fixed:
                _msg += f"已从版本库还原 {_fixed} 章；"
            if _left:
                _msg += "仍需处理 " + "、".join(f"{k} {v} 章" for k, v in _left.items()) + "。"
                _msg += "说「把字数不足和结尾截断的章节补好」我就按原情节逐章补足/收尾。"
            else:
                _msg += "其余章节完整。"
            return _msg + _tail

        if intent in ("retrofit_scan", "retrofit_rewrite"):
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                return "⚠️ 旧书整改目前只支持小说。"
            try:
                from bot import retrofit as _retro
                _rd = _retro.run(proj, quiet=True)
            except Exception as e:
                return f"⚠️ 旧书整改扫描失败：{e}"
            _top = (_rd.get("surgical") or [])
            _lines = [f"🧾 《{name}》旧书整改方案（已写 {_rd.get('chapters', 0)} 章 / "
                      f"约 {_rd.get('words', 0)} 字）",
                      f"· 书名：{( _rd.get('title_coverage') or 0):.0%} 的章提到书名关键词 → "
                      f"{_rd.get('title_decision')}。{_rd.get('title_fix')}",
                      f"· 建议先改 {len(_top)} 章（按顺序）："]
            for i, x in enumerate(_top[:10], 1):
                tier = {1: "①字数", 2: "②低分", 3: "③引擎", 4: "④文书", 5: "⑤章卡"}.get(x["tier"], "")
                _lines.append(f"  {i}. 第{x['n']}章 {tier}：{'；'.join((x.get('reasons') or [])[:2])[:70]}")
            _lines.append(f"完整清单见 {_retro.REPORT_MD}。")
            if intent == "retrofit_rewrite":
                _exec = [x["n"] for x in _top if x["tier"] != 5][:20]
                if not _exec:
                    return "\n".join(_lines) + "\n（没有可重写的章；⑤类请先补章卡规划。）"
                writer.supersede_rewrites(key)
                writer.clear_pause(key)
                writer.start_task(
                    key, "rewrite", f"旧书整改《{name}》（先改 {len(_exec)} 章）",
                    _exec,
                    batch=lambda todo: writer.run_parallel_rewrite(
                        key, chapters=todo, workers=3, max_rounds=2, target=75),
                    params={"workers": 3, "max_rounds": 2, "target": 75, "mode": "retrofit"})
                _lines.append(f"⚡ 已启动这 {len(_exec)} 章的外科手术式重写：每章改稿分必须高于原稿，"
                              f"否则自动回滚；全书不动。")
            else:
                _lines.append("要执行，点「旧书整改并改写」或说「按这份清单改」。")
            return "\n".join(_lines)

        if intent in ("report_status", "show_outline", "audit", "export", "new_project", "pause"):
            intent = {"report_status": "query_status", "show_outline": "query_outline",
                      "audit": "consistency_audit", "export": "export_book",
                      "new_project": "new_project", "pause": "pause_writing"}[intent]

        # 总编辑的「写」：count/mode 决定是接着写、批量写还是一次写完
        if intent == "write":
            _count = params.get("count")
            _mode = str(params.get("mode") or "").strip().lower()
            if _mode == "start":
                return _run("start_writing", {})
            if _mode == "resume_task":
                return _run("resume_task", {"batch_size": _count})
            # v5.8.0：协作模式（逐章确认）——「继续/采用」= 采用当前待确认章并写下一章
            if _mode == "collab_next":
                return _run("collab_next", {})
            _cs = str(_count if _count is not None else "").strip().lower()
            if _cs in ("", "none", "continue", "next"):
                return _run("resume_writing", {})
            if _cs in ("all", "全部", "所有", "0"):
                return _run("start_writing_all", {})
            _m = re.search(r"\d+", _cs)
            return _run("batch_writing", {"batch_size": _m.group() if _m else _cs})

        # v5.8.0：collab_mode —— 人机共同创作（逐章确认）开关
        if intent == "collab_mode":
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                return "⚠️ 逐章确认（协作模式）目前只支持小说；其他文体可逐章在对话里提修改意见。"
            _enabled = bool(params.get("enabled", True))
            plan = proj.plan() or {}
            if not plan.get("chapters"):
                return (f"⚠️ 《{name}》还没有写作计划，先等规划完成再设置创作模式"
                        f"（说「进度」可查看）。")
            plan.setdefault("meta", {})["collab_review"] = _enabled
            proj.save_plan(plan)
            proj.log("🤝 协作模式已开启：每章写完暂停等作者确认（说「继续」采用并写下一章）"
                     if _enabled else "⚡ 协作模式已关闭：回到自动连续写作，不再逐章停下")
            if _enabled:
                return (f"🤝 已开启《{name}》的逐章确认：以后每章写完都会停下来等你把关——"
                        f"说「继续」采用当前章并写下一章，「重写本章」重新生成，"
                        f"也可以去工作台改稿后说「采用继续」。设置页的「高级 → 创作模式」也能切换。")
            # 关闭时若正停在待确认章：视为作者放行，直接续写（「自动写」就是不想再停）
            _ac = writer.collab_awaiting_key(key)
            if _ac and not running and writer.resume_book(key):
                return (f"⚡ 已关闭《{name}》的逐章确认：回到自动连续写作。"
                        f"正停着的第 {_ac} 章视为已确认，直接继续往下写。")
            return f"⚡ 已关闭《{name}》的逐章确认：回到自动连续写作，不再逐章停下。"

        # v5.8.0：collab_next —— 协作模式「采用当前待确认章并继续写下一章」
        if intent == "collab_next":
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                return "⚠️ 逐章确认（协作模式）目前只支持小说。"
            if running:
                st2 = proj.status()
                return (f"📖 《{name}》正在创作中（第 {st2['done']}/{st2['total']} 章），"
                        f"无需确认；写完当前章后若仍开启逐章确认会自动停下等你把关。")
            _ac = writer.collab_awaiting_key(key)
            if not _ac:
                return (f"ℹ️ 《{name}》当前没有停在待确认的章节"
                        f"（协作模式未开启，或没有待把关的章）。"
                        f"说「开启逐章确认」可打开协作模式。")
            if not writer.resume_book(key):
                st2 = proj.status()
                if st2.get("state") == "completed":
                    return (f"✅ 第 {_ac} 章已按你确认采用——全书正文已齐"
                            f"（{st2['done']}/{st2['total']} 章），创作完成。")
                return f"⚠️ 无法继续《{name}》：写作计划缺失或已完本。"
            return (f"🤝 第 {_ac} 章已按你确认采用，继续第 {_ac + 1} 章…"
                    f"（协作模式仍开启：每章写完还会停下等你把关）")

        if intent == "rewrite_chapters":
            if _med_of_key(key) != "novel":
                return "逐章重写目前只支持小说；这类文体可以直接说「把第几段改成什么」，我让改稿师处理。"
            _raw_chs = params.get("chapters")
            if not isinstance(_raw_chs, list):
                _raw_chs = [_raw_chs] if _raw_chs not in (None, "") else []
            _chs = sorted({int(x) for x in _raw_chs if str(x).strip().isdigit()})
            if not _chs:
                _one = _parse_chapter_no(msg)
                _chs = [_one] if _one else []
            if not _chs:
                return "要改哪一章？告诉我章号，或者说说哪段不对劲，我来定位。"
            # v5.8.0：协作模式待确认章的重写（会话里说「重写本章/重写第 n 章」）——
            # 不用 clear_pause 抹掉待确认位，改由 begin/end_collab_rewrite 记住并恢复
            # （重写完成后仍停在本章待确认，不自动继续写下一章）。
            if not writer.begin_collab_rewrite(key):
                writer.clear_pause(key)
            _item = _persist_brief(proj, _brief_for_rewrite(brief_data, _chs), msg,
                                   kind="rewrite") if brief_data else {}
            _dims = brief_mod.render_lines(_item) if _item else [msg]
            if _start_rewrite(key, chapters=_chs, dimensions=_dims):
                if len(_chs) == 1:
                    return (f"已安排第{_chs[0]}章局部重写"
                            + ("，会按你刚说的验收标准逐条核对。" if _item else "，完成后状态 pane 可见。"))
                return (f"已安排第{'、'.join(map(str, _chs))}章重写"
                        + ("，会按你刚说的验收标准逐条核对。" if _item else "。"))
            return "⚠️ 重写任务启动失败，请稍后再试。"

        if intent == "rewrite_below":
            _below = params.get("below")
            try:
                _below = int(float(str(_below).replace("分", ""))) if _below not in (None, "") else None
            except (TypeError, ValueError):
                _below = None
            if _below:
                return _run("rewrite_low_score",
                            {"below": _below, "target": params.get("target")}, brief_data)
            return _run("rewrite_all_below", {}, brief_data)

        if intent == "rewrite_short":
            # v4.38.2：按「正文字数」重写短章——与按评分筛选彻底分开。
            # 最新指令优先：先让旧批量改写失效，再按当前磁盘字数重算清单并立即开跑。
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                return "⚠️ 按字数重写目前只支持小说；其他文体请直接说「把第几段写到多少字」。"
            _bw = params.get("below_words")
            try:
                _bw = int(float(str(_bw).replace("字", ""))) if _bw not in (None, "") else None
            except (TypeError, ValueError):
                _bw = None
            if not _bw:
                _m = (re.search(r"(?:低于|少于|不到|不足|小于)\s*(\d+)\s*字", msg)
                      or re.search(r"(\d+)\s*字(?:以下|以内|之下)", msg))
                _bw = int(_m.group(1)) if _m else 3000
            _bw = max(100, min(50000, int(_bw)))
            writer.supersede_rewrites(key)
            writer.clear_pause(key)
            _pf = {}
            try:
                _pf = writer.book_health.preflight(proj, "按字数重写", _bw)
            except Exception:
                pass
            _shorts = writer.short_chapters(proj, _bw)
            _cuts = list(((_pf.get("audit") or {}).get("truncated") or []))
            if not _shorts and not _cuts:
                _n_rep = len(((_pf.get("repair") or {}).get("repaired") or []))
                _extra = (f"另外已从版本库还原 {_n_rep} 章被写成说明文字/半句截断的原稿。"
                          if _n_rep else "")
                return f"✅ 按正文实测，没有少于 {_bw} 字或结尾截断的章节，无需重写。{_extra}"
            _chs = sorted({x["ch"] for x in _shorts} | {x["ch"] for x in _cuts})
            if brief_data:
                _persist_brief(proj, _brief_for_rewrite(brief_data, _chs), msg, kind="rewrite")
            jid = _rw_claim(key)
            writer.start_task(
                key, "rewrite", f"重写《{name}》字数不足/截断的章节", _chs,
                batch=lambda todo: _rw_short_batch(key, todo, _bw, jid),
                params={"mode": "expand_short", "below_words": int(_bw), "workers": 2})
            _head = "、".join(str(x) for x in _chs[:8])
            _desc = f"正文少于 {_bw} 字的有 {len(_shorts)} 章"
            if _cuts:
                _desc += f"，另有 {len(_cuts)} 章字数够但结尾截断"
            return (f"⚡ 已停掉之前的批量改写，只按你最新的要求执行：{_desc}（第 {_head}"
                    f"{'…' if len(_chs) > 8 else ''} 章）。"
                    f"现在按原情节逐章补足/收尾，写完评分复核、明显降分就还原原稿。"
                    f"这一批会自动跑完，不需要你说「继续」。")

        if intent == "score_chapters":
            _chs = params.get("chapters") or []
            if not isinstance(_chs, list):
                _chs = [_chs]
            _chs = sorted({int(x) for x in _chs if str(x).strip().isdigit()})
            if not _chs:
                return _run("score_all_unscored", {})
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                return "⚠️ 逐章评分暂支持小说；其他文体请在评分详情里处理。"
            writer.start_task(
                key, "score", f"给《{name}》第{'、'.join(map(str, _chs[:8]))}章评分", _chs,
                per_chapter=lambda n, i: writer.score_chapter(proj, n))
            return (f"📚 已开始给第{'、'.join(map(str, _chs[:8]))}章评分"
                    f"{'…' if len(_chs) > 8 else ''}，完成后章节卡会显示质量分。")

        if intent == "directive":
            return _exec_directive(proj, key, msg, brief_data)

        if intent == "revoke_briefs":
            return _exec_revoke_briefs(proj, params)

        if intent == "plan_change":
            return _exec_plan_change(proj, key, msg, params, brief_data)

        if intent == "apply_plan_proposal":
            return _exec_apply_proposal(proj, params)

        if intent == "cancel_task":
            return _exec_cancel_task(key, params)

        if intent == "show_briefs":
            return _exec_show_briefs(proj, key)

        # query_status → 进度回复
        if intent == "query_status":
            mark = ("正在创作中" if st.get("running") else
                    "已暂停" if st.get("paused") else
                    "已完结" if st.get("state") == "completed" else "在库待写")
            # 单位随文体：剧本=场、诗词=轮、散文=阶段、小说=章
            _unit = {"s_": "场", "p_": "轮", "e_": "阶段", "c_": "篇", "n_": "阶段", "k_": "阶段", "o_": "阶段"}.get(str(key)[:2], "章")   # v4.24.16：单文档引擎按阶段计
            return (f"📖 《{name}》{mark}：{st['done']}/{st['total']} {_unit}，已写 {st['words']} 字。"
                    f"可随时问「进度」，或直接下修改指令。")

        # set_title → 标题命令
        if intent == "set_title":
            _t = params.get("title")
            if not _t and (plan.get("meta") or {}).get("awaiting_title"):
                # v5.5：「作者没挑书名直接说继续」的兜底——总编辑拍板定名时若没给出
                # 具体书名（params.title 为空），用首个候选顶上，绝不让流程卡在
                # awaiting_title 空转（set_title 空参旧实现只会回一句标题引导话术）。
                _cands = plan.get("titles") or (plan.get("meta") or {}).get("title_candidates") or []
                for _c in _cands:
                    _ct = str((_c or {}).get("title") or "").strip()
                    if _ct:
                        _t = _ct
                        break
            return _chat_title_cmd(s, key, msg if not _t else f"改名叫{_t}")

        # rewrite_chapter → 局部重写通道
        if intent == "rewrite_chapter":
            no = params.get("chapter_number") or _parse_chapter_no(msg)
            if no:
                return _run("rewrite_chapters", {"chapters": [no]}, brief_data)

        # pause_writing → 暂停
        if intent == "pause_writing":
            _rw_on = bool(_REWRITING.get(key, {}).get("running")) \
                or bool(_PARA_RW_RUNNING.get(key, {}).get("running"))
            if writer.pause_book(key):
                _tail = "；正在跑的改写/补字会在本章结束后停下，剩余章节已存档（说「继续」接着做）" if _rw_on else ""
                return f"⏸ 已暂停《{name}》的创作{_tail}。"
            return f"⚠️ 无法暂停《{name}》。"

        # query_outline → 大纲
        if intent == "query_outline":
            chs = plan.get("chapters") or []
            if not chs:
                return f"📖 《{name}》暂无大纲。"
            lines = [f"📖 《{name}》目录（共 {len(chs)} 章）："]
            for c in chs[:30]:
                mark = "✅" if c.get("status") == "completed" else "⏳" if c.get("status") == "in_progress" else "⬜"
                lines.append(f"  {mark} 第{c.get('chapterNumber', '?')}章 {c.get('title', '')}")
            if len(chs) > 30:
                lines.append(f"  …（还有 {len(chs) - 30} 章）")
            return "\n".join(lines)

        # export_book → 导出
        if intent == "export_book":
            return f"📥 请在「作品」面板点击《{name}》的「导出」按钮，可导出 Markdown / EPUB / PDF。"

        # new_project → 引导
        if intent == "new_project":
            return (f"当前对话已绑定《{name}》。要改这本书直接说（例如「第12章节奏再紧一点」"
                    f"「给女主加一场反击」）；要开新书请用左侧『新建作品』。")

        # v4.22.2：resume_task ——「继续」的真正含义：接着上次没做完那件事，而不是重新开始
        if intent == "resume_task":
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                intent = "resume_writing"       # 非小说文体走各自引擎的断点恢复
            else:
                if running:
                    return (f"📖 《{name}》正在创作中：{st['done']}/{st['total']} 章，"
                            f"不用再点继续，说「进度」可随时查。")
                try:
                    r = writer.resume_task(key, batch_size=params.get("batch_size"))
                except Exception as e:
                    r = {"ok": False, "text": f"恢复失败：{e}"}
                if not r.get("ok"):
                    return (f"⚠️ 没能接着上次的任务继续（{r.get('text') or '无可用断点'}）。"
                            f"可以说「写下一章」另起一个写作任务。")
                return f"▶ {r.get('text')}。已恢复运行，工作台可见进度。"

        # start_writing_all / start_writing / resume_writing → 启动或继续写作
        # v4.17.3：rewrite_low_score——自然语言按分数阈值改写低分章（「低于65分的全部重写，目标80分」）
        if intent == "rewrite_low_score":
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                return f"⚠️ 按分数改写暂支持小说；其他文体请用评分详情里的「多岗位并行改写」。"
            _below = params.get("below") or 65
            _tgt = params.get("target") or max(int(_below) + 15, 80)
            _lows = [p["ch"] for p in writer.per_chapter_scores(proj)
                     if isinstance(p.get("score"), (int, float)) and p["score"] < int(_below)]
            if not _lows:
                return f"✅ 没有低于 {_below} 分的章节（逐章分最低见「评分详情」），无需改写。"
            if brief_data:
                _persist_brief(proj, _brief_for_rewrite(brief_data, _lows), msg, kind="rewrite")
            # v4.38.2：最新指令优先——不再因为旧 job 在跑就拒绝作者
            writer.supersede_rewrites(key)
            writer.clear_pause(key)
            jid = _rw_claim(key)
            # v4.23.0：登记任务档案（以前这里漏登记 → 「继续」认不出这个任务，
            # 会掉去 resume_book 开写新章，把正在改写的书打断）
            writer.start_task(
                key, "rewrite", f"重写《{name}》低于 {_below} 分的章节", _lows,
                batch=lambda todo: _rw_batch(key, todo, _PARA_RW_RUNNING, target=int(_tgt), job=jid),
                params={"below": int(_below), "target": int(_tgt)})
            return (f"⚡ 已启动按分改写：{_lows.__len__()} 章低于 {_below} 分"
                    f"（第 {_lows[:8]}{'…' if len(_lows) > 8 else ''} 章），{3} 个岗位并行改写，"
                    f"目标 {int(_tgt)} 分——每章最多 2 轮，改写分必须高于原分否则回滚。完成后逐章汇报。")

        # v4.40.0：ordered_review——「按顺序审：先字数、再 60 分以下、最后其他」一次跑完
        if intent == "ordered_review":
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                return "⚠️ 顺序审校目前只支持小说；其他文体请说「检查全文」走对应审校。"
            _below = params.get("below_words")
            try:
                _below = int(float(str(_below).replace("字", ""))) if _below not in (None, "") else None
            except (TypeError, ValueError):
                _below = None
            if not _below:
                _m = (re.search(r"(?:低于|少于|不到|不足|小于)\s*(\d+)\s*字", msg)
                      or re.search(r"(\d+)\s*字(?:以下|以内|之下)", msg))
                _below = int(_m.group(1)) if _m else int(
                    (proj.plan() or {}).get("minWordsPerChapter") or 3000)
            try:
                _low = int(params.get("low_below") or 60)
            except (TypeError, ValueError):
                _low = 60
            _prev_short = []
            try:
                _prev_short = sorted({x["ch"] for x in writer.short_chapters(proj, _below)})
            except Exception:
                pass
            _prev_low = [p["ch"] for p in writer.per_chapter_scores(proj)
                         if isinstance(p.get("score"), (int, float)) and p["score"] < _low]
            _prev_eng = []
            try:
                from bot import engine_check as _eng
                _ed = _eng.scan(proj)
                _prev_eng = sorted({int(f["chapter"]) for f in (_ed.get("findings") or [])
                                    if f.get("level") in ("high", "warn") and f.get("chapter")})
            except Exception:
                pass
            _todo = sorted(set(_prev_short) | set(_prev_low) | set(_prev_eng))
            if not _todo:
                return (f"✅ 顺序审校没有发现要改的章：没有少于 {_below} 字的章、"
                        f"没有低于 {_low} 分的章、故事引擎也没有高风险项。")
            writer.supersede_rewrites(key)
            writer.clear_pause(key)
            writer.start_task(
                key, "rewrite", f"按顺序审校《{name}》",
                _todo,
                batch=lambda todo: writer.run_ordered_review(
                    key, short_below=_below, low_below=_low,
                    target=max(_low + 15, 75)),
                params={"mode": "ordered", "below_words": int(_below),
                        "low_below": int(_low), "workers": 3,
                        "target": max(_low + 15, 75)})
            return (f"🧭 已按你定的顺序启动，且会一次跑完（不用再说「继续」）：\n"
                    f"① 字数不足/截断：{len(_prev_short)} 章（低于 {_below} 字）\n"
                    f"② 评分 <{_low}：{len(_prev_low)} 章\n"
                    f"③ 故事引擎/工艺：{len(_prev_eng)} 章（已排除前两步处理过的章）\n"
                    f"合计 {len(_todo)} 章，3 个岗位并行；每章改完必须比原稿分高，否则自动回滚原稿。")

        # v4.21.9：rewrite_all_below——「重写所有未达标的章节」（无章号/分数也准确理解）
        if intent == "rewrite_all_below":
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                return "⚠️ 批量重写暂支持小说；其他文体请在评分详情里逐章处理。"
            # v4.40.0：最新指令优先——不再让旧任务把作者挡在门外
            writer.supersede_rewrites(key)
            writer.clear_pause(key)
            _pass = writer._pass_line()
            _lows = [p["ch"] for p in writer.per_chapter_scores(proj)
                     if isinstance(p.get("score"), (int, float)) and p["score"] < _pass]
            if not _lows:
                return f"✅ 所有章节都达到 {_pass} 分达标线，没有需要重写的章节。"
            if brief_data:
                _persist_brief(proj, _brief_for_rewrite(brief_data, _lows), msg, kind="rewrite")
            writer.supersede_rewrites(key)
            writer.clear_pause(key)
            jid = _rw_claim(key)
            # v4.23.0：统一入口——登记 + 跑 + 逐章记进度 + 收尾，全部交给 start_task
            writer.start_task(
                key, "rewrite", f"重写《{name}》未达标章节", _lows,
                batch=lambda todo: _rw_batch(key, todo, _PARA_RW_RUNNING,
                                             workers=3, max_rounds=2, job=jid),
                params={"workers": 3, "max_rounds": 2, "pass_line": _pass})
            return (f"⚡ 已启动重写全部未达标章节：共 {len(_lows)} 章"
                    f"（第 {_lows[:8]}{'…' if len(_lows) > 8 else ''} 章，均低于达标线 {_pass} 分），"
                    f"3 个岗位并行定向重写——每章最多 2 轮，改写分必须高于原分否则回滚原稿。完成后逐章汇报。")

            # v4.24.4：consistency_audit——「检查一致性」「盘点一下坑」（巡检异步，报告写活动流与右栏）
        if intent == "consistency_audit":
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                return "⚠️ 一致性巡检暂支持小说。"
            def _audit_job():
                try:
                    audit_mod.run_audit(key, "manual")
                except Exception as e:
                    proj.log(f"⚠️ 巡检异常：{e}")
            threading.Thread(target=_audit_job, daemon=True).start()
            return "🔍 已启动一致性巡检：扫描全书矛盾 + 盘点超期未收的伏笔坑，审稿人仲裁后给出修复计划——完成后活动流会汇报，报告可在右栏查看。"

        # v4.21.9：score_all_unscored——「给所有未评分的章节进行评分」
        if intent == "score_all_unscored":
            _meta_sc = (proj.plan().get("meta") or {}).get("chapter_scores") or {}
            _unsc = [c["chapterNumber"] for c in (proj.plan().get("chapters") or [])
                     if c.get("status") == "completed" and not _meta_sc.get(str(c.get("chapterNumber")))]
            if not _unsc:
                return "✅ 所有章节都已有评分，无需补评。"
            # v4.23.0：统一入口——补评分跑到一半被打断，「继续」会接着评剩下的
            writer.start_task(
                key, "score", f"给《{name}》补评分", sorted(_unsc),
                per_chapter=lambda n, i: writer.score_chapter(proj, n))
            _head = _unsc[:8]
            return (f"📚 已开始为 {len(_unsc)} 个未评分章节评分"
                    f"（第 {_head}{'…' if len(_unsc) > 8 else ''} 章），完成后章节卡会自动显示质量分。")

        # v4.15.0：batch_writing 意图识别「再写N章 / 写完全部」——统一并入写作分支
        if intent == "batch_writing":
            _batch_param = params.get("batch_size") or "1"
            # 非小说：走各自引擎的断点恢复（带 batch_size）
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                # v4.33.4：改编剧不能走「按场批量写」——那条路径会把整集稿写成场次骨架
                if _is_adapted_key(key):
                    return (f"🎬 《{name}》是小说改编剧，按集推进：请点顶部的「继续」，"
                            "改编引擎会自动补齐没写或不合格的集（按场批量写已禁用）。")
                if writer.pipeline_running(key):
                    return f"📖 《{name}》正在创作中：说「继续」可随时查看进度。"
                import threading as _th
                from bot import (script_writer, poetry_writer, prose_writer, thesis_writer,
                                 children_writer, news_writer, copy_writer, other_writer)
                _mod = {"s_": script_writer, "p_": poetry_writer, "e_": prose_writer,
                        "t_": thesis_writer, "c_": children_writer,
                        "n_": news_writer, "k_": copy_writer, "o_": other_writer}[str(key)[:2]]
                _bs = str(_batch_param)
                _bs_label = "全部写完" if _bs.lower() in ("all", "0") else f"{_bs} 章"
                try:
                    if hasattr(_mod, "resume_project"):
                        _th.Thread(target=lambda: _mod.resume_project(key, batch_size=_bs), daemon=True).start()
                    else:
                        _th.Thread(target=lambda: _mod.resume_project(key), daemon=True).start()
                    return f"▶ 批量写《{name}》：本轮写 {_bs_label}，写完停在当前进度。"
                except Exception as e:
                    return f"⚠️ 无法批量写《{name}》：{e}"
            # 小说
            if not running and not writer.resume_book(key, batch_size=_batch_param):
                return f"⚠️ 无法批量写《{name}》：写作计划缺失或已完本。"
            if not writer.pipeline_running(key):
                writer.resume_book(key, batch_size=_batch_param)
            _bs = str(_batch_param)
            _bs_label = "全部写完" if _bs.lower() in ("all", "0") else f"{_bs} 章"
            return f"▶ 批量写《{name}》：本轮写 {_bs_label}，写完停在当前进度。"

        if intent in ("start_writing_all", "start_writing", "resume_writing"):
            if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
                # v4.33.4：改编剧只能按集续跑，别落到按场批量写
                if _is_adapted_key(key):
                    return (f"🎬 《{name}》是小说改编剧，按集推进：请点顶部的「继续」，"
                            "改编引擎会自动补齐没写或不合格的集。")
                # 非小说文体：走各自引擎的断点恢复（resume_book 只认小说计划）
                if writer.pipeline_running(key):
                    return f"📖 《{name}》正在创作中，进度见工作台运行日志。"
                import threading as _th
                from bot import (script_writer, poetry_writer, prose_writer, thesis_writer,
                                 children_writer, news_writer, copy_writer, other_writer)
                _mod = {"s_": script_writer, "p_": poetry_writer, "e_": prose_writer,
                        "t_": thesis_writer, "c_": children_writer,
                        "n_": news_writer, "k_": copy_writer, "o_": other_writer}[str(key)[:2]]
                # v4.15.7：start_writing_all（用户明确说全部写完）→ 全写，不落 config 默认逐章
                _bs = "all" if intent == "start_writing_all" else None
                _th.Thread(target=lambda: _mod.resume_project(key, batch_size=_bs), daemon=True).start()
                return f"▶ 恢复《{name}》的创作，工作台可见进度…"
            if running:
                st2 = proj.status()
                return (f"📖 《{name}》正在创作中：第 {st2['done']}/{st2['total']} 章，"
                        f"说「进度」随时可查。")
            # v4.34.2：start_writing_all（「写完全部」「一直写下去」「不要停」）必须把 all
            # 真正传给写作循环。此前这里只换文案不换参数——resume_book 拿不到 batch_size
            # 就回落到 plan.config.batch_size（开书时常见值是 1），于是界面写着「全章批量
            # 模式」，实际写完一章就按批停。文案与行为正好相反，用户以为指令没生效。
            _bs_all = "all" if intent == "start_writing_all" else None
            # v5.5.1：in_progress + 0 章完成 + plan_blocked 残留的书，「继续」要给出明确
            # 动作（自动修补章卡后直接开写），而不是空转或含糊其辞。
            _was_blocked = False
            try:
                _was_blocked = bool(((proj.plan() or {}).get("meta") or {}).get("plan_blocked"))
            except Exception:
                _was_blocked = False
            if writer.resume_book(key, batch_size=_bs_all):
                st2 = proj.status()
                if intent != "start_writing_all":
                    _bs = str((proj.plan().get("config") or {}).get("batch_size") or "1")
                    if _bs.lower() not in ("all", "0"):
                        _extra = f"（本批写 {_bs} 章后暂停；说「写完全部」或「一直写下去」可连续创作）"
                    else:
                        _extra = "（连续模式）"
                else:
                    _extra = "（全章批量模式，中途不停）"
                if _was_blocked:
                    _extra += ("\n上一轮因章卡缺目标/冲突停摆：本次继续会自动修补章卡"
                               "（确定性补全 + 规划师按拒收原因定向重生成，最多 2 轮），"
                               "两轮仍不合格会降级放行，不会再整批卡死。")
                return f"▶ 继续创作《{name}》（第 {st2['done']}/{st2['total']} 章）{_extra}…"
            return f"⚠️ 无法继续《{name}》：写作计划缺失或已完本。"

        return None   # 未命中任何分支

    # ══════════ v4.37.0：总编辑中枢 —— 先真正理解，再决定说还是做 ══════════
    # 作者只跟「一个人」说话：一句话里可能同时有聊天、评价、设定、改稿和进度询问。
    # 规则/规划器只作为模型不可用时的兜底，能读懂时不再靠关键词映射。
    def _exec_decision(decision):
        acts = [a for a in (decision.get("actions") or []) if a.get("action") != "discuss"]
        understanding = decision.get("understanding") or ""
        try:
            steps_mod.emit(key, "chief", "start" if acts else "done",
                           title="理解并安排" if acts else "理解你的话",
                           thinking=understanding,
                           output="→".join(a.get("action") for a in acts) if acts else "")
        except Exception:
            pass
        outs = []
        for a in acts:
            act, prm = a.get("action"), a.get("params") or {}
            brf = a.get("brief") if isinstance(a.get("brief"), dict) else prm.get("brief")
            try:
                r = _run(act, prm, brf)
            except Exception as e:
                r = f"⚠️ 这一步没做成（{act}）：{e}"
            if r:
                outs.append(r)
        try:
            steps_mod.emit(key, "chief", "done", title="已安排" if acts else "已回复",
                           thinking=understanding,
                           output="；".join(x[:80] for x in outs)[:400])
        except Exception:
            pass
        reply = str(decision.get("reply") or "").strip()
        if not outs:
            return reply
        return (reply + "\n\n" if reply else "") + "\n\n".join(outs)

    # ══════════ v5.5：规划中断自愈（awaiting_title / failed 状态机收口）══════════
    # 向导开书后规划线程被 LLM 524/超时打断 → 书停在 failed/空章卡。作者说「继续」
    # 必须重跑规划，而不是被总编辑当成《未命名》空书回答「没有可恢复的任务」。
    # 只拦短消息里的继续/重试类表达；书已有章卡时不拦（走正常写作路由）。
    if not (plan.get("chapters") or []) and _planning_interrupted(proj) \
            and len(msg.strip()) <= 24 and re.search(r"继续|接着|重试|retry", msg or "", re.I):
        if _retry_planning_bg(key):
            return ("⏳ 上一次规划被中断了（模型超时或失败），我已经重新启动规划师在同一本书上重跑规划，"
                    "书名与设定都不会丢。\n规划完成后会自动给出书名候选或直接开写；"
                    "稍等片刻再说「进度」即可查看。")

    # ══════════ v4.41.4：「继续」不再自动把低分章全部重写 ══════════
    # 作者实拍：全书已 100/100，点「继续」后系统自作主张按「<80 分」重写 81 章。
    # 「继续」的语义是「接着没做完的那件事」，不是「重新审一遍并提高分数」。
    # 已完成的书：如实说完成；有暂停的改写任务：接着那个任务；其余一律不动。
    _bare = re.sub(r"[\s。！？!?，,、~～…]+", "", msg or "")
    if _bare in ("继续", "接着来", "接着写", "继续写", "往下写", "接上", "继续吧", "接着来吧"):
        _total0 = int(st.get("total") or 0)
        _done0 = int(st.get("done") or 0)
        if _PARA_RW_RUNNING.get(key, {}).get("running") or _REWRITING.get(key, {}).get("running"):
            _p0 = writer._PARA_RW_PROGRESS.get(key) or {}
            return (f"⏳ 已有改写任务在跑（{_p0.get('done', 0)}/{_p0.get('total', 0)} 章），"
                    f"不用重复点继续；跑完会自动收尾。")
        try:
            from bot import journal as _jn
            _task0 = _jn.current_task(proj.path)
        except Exception:
            _task0 = None
        if isinstance(_task0, dict) and str(_task0.get("status") or "") in ("paused", "partial", "running"):
            _r0 = writer.resume_task(key)
            if _r0.get("ok"):
                return f"▶ 接着上次没做完的任务：{_r0.get('text')}"
        if _total0 and _done0 >= _total0:
            return (f"✅ 《{name}》全书已完成（{_done0}/{_total0} 章），没有待写的章节，"
                    f"所以不会自动重写任何一章。\n\n"
                    f"要交付：直接点「导出」即可——只需修硬伤（半句截断）后就能导原稿；\n"
                    f"要冲高分：在导出弹窗勾选「强制高分」，系统才会把低于达标线的章重写到达标。")
        # 书没写完：不在这里拦，交给正常写作路由（保留 plan.config 的每批章数等既有行为）

    _decision = None
    try:
        _hist = chief_mod.recent_history(proj.path)
        # v2api 在调用 chat() 前已把本条用户消息落盘；喂给总编辑时去掉重复的当前句
        if _hist and _hist[-1].get("role") == "user" \
                and str(_hist[-1].get("content") or "").strip() == msg:
            _hist = _hist[:-1]
        _cctx = chief_mod.build_context(proj, st, key,
                                        digest=s.get("digest") or "", history=_hist)
        _cctx["running_rewrite"] = bool(_REWRITING.get(key, {}).get("running")) \
            or bool(_PARA_RW_RUNNING.get(key, {}).get("running"))
        _decision = chief_mod.deliberate(msg, _cctx)
    except Exception:
        _decision = None
    if _decision:
        try:
            _trace("chief", sid=s.get("session", ""),
                   understanding=(_decision.get("understanding") or "")[:100],
                   actions=[a.get("action") for a in (_decision.get("actions") or [])],
                   conf=_decision.get("confidence"))
        except Exception:
            pass
        return _exec_decision(_decision)

    # ── 兜底（总编辑不可用）：旧的规则标题命令 ──
    if re.search(r"标题|书名|改名|换名|重命名", msg):
        return _chat_title_cmd(s, key, msg)

    # ══════════ v4.22.0：Agent 规划器 —— 先理解自然语言，再自主执行 ══════════
    # 不再依赖写死的句式规则：把「用户原话 + 真实项目状态」交给 LLM 规划器，
    # 它先理解意图，再输出可执行的多步计划（action + params），这里逐步执行。
    # 规划器不可用（无模型/断网/解析失败）时才回退到传统单意图理解（规则 + LLM 分类）。
    from bot.planner import plan_actions, build_context, ACTIONS as _PLAN_ACTIONS
    try:
        _pctx = build_context(proj, st, key)
    except Exception:
        _pctx = {"state": st.get("state") or "", "done": st.get("done") or 0,
                 "total": st.get("total") or 0, "pass_line": writer._pass_line()}
    _planobj = None
    try:
        _planobj = plan_actions(msg, _pctx)
    except Exception:
        _planobj = None
    _steps = (_planobj or {}).get("steps") or []
    _valid_steps = [stp for stp in _steps
                    if isinstance(stp, dict) and stp.get("action") in _PLAN_ACTIONS]
    if _valid_steps:
        try:
            _trace("plan", sid=s.get("session", ""), steps=[x.get("action") for x in _valid_steps],
                   understand=(_planobj or {}).get("understanding", "")[:80])
        except Exception:
            pass
        _outs = []
        try:
            from bot import steps as _steps_mod
            if len(_valid_steps) > 1:
                _steps_mod.emit(key, "planner", "start", title="理解并规划任务",
                                thinking=(_planobj or {}).get("understanding", ""),
                                output=f"{len(_valid_steps)} 步：" + " → ".join(x.get("title") or x["action"] for x in _valid_steps))
        except Exception:
            pass
        for _i, _stp in enumerate(_valid_steps, 1):
            _act = _stp.get("action")
            _prm = _stp.get("params") if isinstance(_stp.get("params"), dict) else {}
            try:
                _r = _run(_act, _prm)
            except Exception as _e:
                _r = f"⚠️ 第{_i}步（{_act}）执行异常：{_e}"
            if _r:
                if len(_valid_steps) > 1:
                    _outs.append(f"【第 {_i} 步 · {_stp.get('title') or _act}】{_r}")
                else:
                    _outs.append(_r)
        if _outs:
            _head = ""
            if len(_valid_steps) > 1 and (_planobj or {}).get("understanding"):
                _head = f"🧠 我的理解：{_planobj.get('understanding')}\n\n"
            return _head + "\n\n".join(_outs)
        # 全部步骤都返回 None（如纯创作建议）→ 落到 creative_hint 合并通道
    else:
        # ── 回退：传统单意图理解（规则 + LLM 分类）──
        from bot.nlu import parse_intent
        _nlu = parse_intent(msg, project_status={"state": st.get("state", "unknown")})
        _intent = _nlu["intent"]
        try:
            _trace("nlu", sid=s.get("session", ""), intent=_intent,
                   conf=_nlu["confidence"], src=_nlu["source"])
        except Exception:
            pass
        _r = _run(_intent, _nlu.get("params") or {})
        if _r is not None:
            return _r

    # set_style / add_constraint / creative_hint → 并入创作
    # 读者即时指令：存入队列，由写作线程在后续章节消费
    # v4.15.7：非小说文体没有 02-写作计划.json，queue_msg 必然失败——改走 assist 改稿/讨论通道
    if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
        from bot import assist as _assist_mod
        _med = {"s_": "script", "p_": "poetry", "e_": "prose", "t_": "thesis",
                "c_": "children", "n_": "news", "k_": "copy", "o_": "other"}.get(str(key)[:2], "script")
        try:
            _res = _assist_mod.assist(key, _med, msg)
        except Exception as e:
            return f"⚠️ 改稿请求失败：{e}"
        if _res and _res.get("ok"):
            return _res.get("text") or "已处理。"
        return _res.get("text") or f"⚠️ 这条要求没能处理（《{name}》{_med}）。"
    if not writer.queue_msg(key, msg):
        return f"⚠️ 这条要求没能并入《{name}》的创作（写作计划缺失？），可在『作品』里检查该书。"
    nxt = _next_pending_chapter(plan)
    _hint = ""
    _intent = locals().get("_intent", "")
    if _intent == "set_style":
        _hint = "（风格调整）"
    elif _intent == "add_constraint":
        _hint = "（追加设定）"
    return (f"已把这条要求并入创作{_hint}：第{nxt}章起生效。" if nxt
            else f"已把这条要求并入创作{_hint}。")

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
        _sess_touch(SESSIONS[sid]); _sess_prune()
    # 重试指令
    if message.strip() in ("重试", "retry") and SESSIONS.get(sid, {}).get("state") == "options":
        s = SESSIONS[sid]
        s["state"] = "options"
        r = _do_planning(s)
        return {"session_id": sid, "reply": r, "project": s.get("project")}
    reply = chat(sid, message, str(payload.get("key") or "") or None)
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
            syslog_mod.log_event("provider", "刷新模型清单", {"id": pid, "count": len(got)},
                                 f"从接口刷新供应商「{pid}」模型清单（{len(got)} 个）")
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
        syslog_mod.log_event("provider", "添加供应商",
                             {"id": p.get("id"), "base_url": p.get("base_url"), "models": (p.get("models") or [])[:8]},
                             f"添加供应商「{p.get('name') or p.get('id')}」（{len(p.get('models') or [])} 个模型）")
        return {"ok": True, "provider": p}
    except providers.ProviderError as e:
        return _prov_err(e)

@app.put("/api/providers/{pid}")
def api_provider_update(pid: str, payload: dict):
    try:
        p = providers.update_provider(pid, payload)
        fields = [k for k in payload if k != "api_key"]
        syslog_mod.log_event("provider", "修改供应商",
                             {"id": pid, "fields": fields},
                             f"修改供应商「{p.get('name') or pid}」：{'、'.join(fields) or '无字段变更'}"
                             + ("（密钥已更新）" if payload.get("api_key") else ""))
        return {"ok": True, "provider": p}
    except providers.ProviderError as e:
        return _prov_err(e)

@app.delete("/api/providers/{pid}")
def api_provider_delete(pid: str):
    try:
        r = providers.remove_provider(pid)
        syslog_mod.log_event("provider", "删除供应商", {"id": pid}, f"删除供应商「{pid}」")
        return {"ok": True, **r}
    except providers.ProviderError as e:
        return _prov_err(e)

@app.post("/api/providers/{pid}/default")
def api_provider_set_default(pid: str):
    try:
        r = providers.set_default(pid)
        syslog_mod.log_event("provider", "设为默认", {"id": pid}, f"把「{pid}」设为默认供应商")
        return {"ok": True, "default_provider": r}
    except providers.ProviderError as e:
        return _prov_err(e)

@app.post("/api/providers/{pid}/active")
def api_provider_set_active(pid: str, payload: dict):
    try:
        p = providers.set_active(pid, payload.get("active", True))
        on = payload.get("active", True)
        syslog_mod.log_event("provider", "启用/停用", {"id": pid, "active": bool(on)},
                             f"{'启用' if on else '停用'}供应商「{p.get('name') or pid}」")
        return {"ok": True, "provider": p}
    except providers.ProviderError as e:
        return _prov_err(e)

@app.post("/api/providers/{pid}/test")
def api_provider_test(pid: str, payload: dict = None):
    try:
        r = providers.test_provider(pid, probe_chat=(payload or {}).get("probe_chat", True))
        syslog_mod.log_event("provider", "测试连接",
                             {"id": pid, "ok": bool(r.get("ok", True)), "detail": str(r.get("detail") or "")[:120]},
                             f"测试供应商「{pid}」连接{'成功' if r.get('ok', True) else '失败'}：{str(r.get('detail') or '')[:80]}")
        return r
    except providers.ProviderError as e:
        return _prov_err(e)

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


# ---------- v2.1 B：按文体建项目（/api/create 分发与 /api/<medium>/create 共用同一套 cfg 组装） ----------

def _provider_into(cfg: dict, payload: dict) -> dict:
    if payload.get("provider_id"):
        cfg.setdefault("defaults", {})["provider_id"] = payload["provider_id"]
    if payload.get("model"):
        cfg.setdefault("defaults", {})["model"] = payload["model"]
    return cfg


def _express_into(cfg: dict, payload: dict) -> dict:
    """v4.24.0：把「极速模式」折成 cfg.express。

    两条界面入口等价：小说的「创作模式」卡片传 mode=express，其余文体传 express=express。
    只在为真时写键——老项目没有这个字段，统一写 False 只会让 02-写作计划.json 变脏。
    """
    if express.on({"express": payload.get("express"), "mode": payload.get("mode")}):
        cfg["express"] = True
    return cfg


def _short_title(idea: str, limit: int = 14) -> str:
    """极速模式隐藏书名时，用一个短、可命名的占位标题，不把整段简介当作品名。

    只做确定性切分：优先第一句/第一逗号；首段太短再借下一段前 10 字；仍过长则截断。
    引擎随后产出的正式标题会覆盖它（剧本/儿童/新闻等）。
    """
    text = re.sub(r"\s+", "", str(idea or "").strip())
    text = text.strip("「」“”‘’《》<>【】")
    if not text:
        return "未命名"
    parts = [p for p in re.split(r"[，,。；;：:！？!?\\n]", text) if p]
    cand = parts[0] if parts else text
    if len(cand) < 6 and len(parts) > 1:
        cand += parts[1][:10]
    cand = re.sub(r"[\\/:*?\"<>|]+", "", cand).strip("。！？!?，,、；;：:")
    return (cand[:limit] or "未命名")


def _style_prompt_of(style_id) -> str:
    """蒸馏风格卡的技法文本。剧本/诗词/散文没有 style_ref.json 注入点，折进素材说明里交给提示词。"""
    card = distill.get_style((style_id or "").strip())
    return (card or {}).get("style_prompt", "") or ""


def _refs_into(cfg: dict, payload: dict, query_keys: tuple) -> list:
    ref_ids = [str(x) for x in (payload.get("reference_ids") or []) if str(x).strip()][:40]
    if ref_ids:
        cfg["reference_ids"] = ref_ids
        cfg["_reference_block"] = _reference_block(
            ref_ids, " ".join(str(payload.get(k) or "") for k in query_keys))
    return ref_ids


def _spawn_project(medium: str, prefix: str, create_fn, safe_fn, default_name: str,
                   cfg: dict, ref_ids: list) -> dict:
    """占位 key 立即返回，规划放后台线程（引擎里是多次 LLM 调用，同步跑会卡死事件循环）。

    v4.23.2：先在**当前线程**把项目目录建出来，再启动后台规划。
    此前目录要等后台线程真正开始跑才 makedirs，而各引擎的 get_project 都是
    「目录不存在 → 返回 None」，于是前端拿到 key 立刻轮询 /api/*/status 会吃 404
    （提示「项目不存在或未加载，请刷新作品库」），网速快一点就必现。
    引擎 create_project 内部都是 os.makedirs(exist_ok=True)，预建目录不影响它。
    """
    import threading as _th
    from datetime import datetime as _dt
    # v5.2：极速模式隐藏标题时，不要把整段 idea 当作品名/目录名。
    # 用一个短占位标题；引擎生成正式标题后会回写计划。
    if medium != "novel" and not str(cfg.get("title") or "").strip():
        cfg["title"] = _short_title(cfg.get("idea") or "")
    display = (cfg.get("title") or cfg.get("idea") or default_name or "").strip()
    display = (display.splitlines()[0] if display else default_name)[:40] or default_name
    _name = safe_fn(cfg.get("title") or cfg.get("idea") or default_name)
    base = f"{prefix}{_dt.now().strftime('%Y%m%d-%H%M%S')}-{_name}"
    try:
        os.makedirs(os.path.join(writer.PROJECTS_DIR, base), exist_ok=True)
    except Exception as e:
        print(f"[spawn] 预建目录失败（{base}）：{e}", flush=True)

    def _bg():
        writer.mark_pipeline(base, True)   # v3.1.0：规划+写作全程「创作中」可见
        try:
            create_fn(cfg, base_key=base)
            _link_references(base, ref_ids)
        except Exception as e:
            # v4.23.2：只打 str(e) 时排查不到落点（曾见「No such file or directory: .tmp-xxx.part」
            # 完全看不出是哪一步），补完整堆栈。
            _tb = traceback.format_exc()
            print(f"[bg] {medium}创作失败: {e}\n{_tb}", flush=True)
            # 引擎在建项目时抛错（限流/空稿/JSON 失败）不能只留 status=planning 让前端永远转圈。
            try:
                _med, _fp, _pl = writer._plan_of(os.path.join(writer.PROJECTS_DIR, base))
                if _fp and isinstance(_pl, dict):
                    _pl["status"] = "failed"
                    _pm = _pl.setdefault("meta", {})
                    _pm["error"] = str(e)[:200]
                    _pm["stage"] = _pm.get("stage") or "create"
                    atomic_write_json(_fp, _pl)
            except Exception:
                pass
        finally:
            writer.mark_pipeline(base, False)

    _th.Thread(target=_bg, daemon=True).start()
    return {"key": base, "pid": base, "project": base, "medium": medium,
            "name": display, "status": "planning", "async": True}


def _create_script_project(payload: dict) -> dict:
    """剧本引擎读 form/entryPath/idea/notes/scenes/pages/episodes/title 与时代设定；
    向导里的题材、格式要求与蒸馏文风都折进 notes（script_writer.gen_premise 会读它）。"""
    cfg = dict(payload.get("cfg_overrides") or {})
    for k in ("title", "idea", "form", "entryPath", "scenes", "pages", "episodes",
              "setting_era", "setting_world",
              "batch_size", "penname"):   # v4.15.7：批量写作与笔名此前在剧本创建路径被丢弃
        if payload.get(k) is not None:
            cfg[k] = payload[k]
    if payload.get("agents"):
        cfg["agents"] = payload["agents"] if isinstance(payload["agents"], list) else [payload["agents"]]
    cfg.setdefault("form", "film")
    _express_into(cfg, payload)
    # 「跟随素材」不写具体时代，交给引擎从题材/标题/素材里推断，避免默认值压过用户意图。
    if str(cfg.get("setting_era") or "") in ("", "auto"):
        cfg.pop("setting_era", None)
    notes = [(payload.get("notes") or "").strip()]
    if payload.get("genre"):
        cfg["genre"] = payload["genre"]
        notes.append(f"题材类型：{payload['genre']}。")
    spec = payload.get("format_spec") or []
    if spec:
        notes.append("剧本格式要求：" + "、".join(str(x) for x in spec) + "。")
    sp = _style_prompt_of(payload.get("style_id"))
    if sp:
        notes.append("目标文风：" + sp)
    cfg["notes"] = "\n".join(x for x in notes if x)
    _provider_into(cfg, payload)
    ref_ids = _refs_into(cfg, payload, ("title", "idea", "form"))
    return _spawn_project("script", "s_", script_writer.create_project,
                          script_writer._safe, "未命名剧本", cfg, ref_ids)


def _create_poetry_project(payload: dict) -> dict:
    """诗词引擎读 form/title/idea/maxRounds；词牌与情感核心折进 idea（I 释读与 C 起草都从它起），
    格律开关与附产项由 poetry_writer 直接消费。"""
    cfg = dict(payload.get("cfg_overrides") or {})
    for k in ("title", "form", "cipai", "emotion", "metrical_check", "with_notes", "with_translation"):
        if payload.get(k) is not None:
            cfg[k] = payload[k]
    cfg.setdefault("form", "modern")
    _express_into(cfg, payload)
    try:
        rounds = int(payload.get("maxRounds") or payload.get("rounds") or 3)
    except (TypeError, ValueError):
        rounds = 3
    cfg["maxRounds"] = min(5, max(1, rounds))
    idea = [(payload.get("idea") or "").strip()]
    if cfg.get("cipai") and str(cfg.get("form")) in ("ci", "qu"):
        idea.append(f"词牌／曲牌：{cfg['cipai']}（句读与每句字数按该牌谱，不得自拟）。")
    if cfg.get("emotion"):
        idea.append(f"情感核心：{cfg['emotion']}。")
    sp = _style_prompt_of(payload.get("style_id"))
    if sp:
        idea.append("目标文风：" + sp)
    cfg["idea"] = "\n".join(x for x in idea if x)
    _provider_into(cfg, payload)
    ref_ids = _refs_into(cfg, payload, ("title", "idea", "form"))
    return _spawn_project("poetry", "p_", poetry_writer.create_project,
                          poetry_writer._safe, "无题", cfg, ref_ids)


def _create_prose_project(payload: dict) -> dict:
    """散文引擎按操作跑 B/S/D/A/P/F；受众、目的、语气、目标字数与去味深度、事实核验开关都写进 cfg，
    prose_writer 的 brief／素材／去味阶段直接消费。"""
    cfg = dict(payload.get("cfg_overrides") or {})
    for k in ("title", "idea", "genre", "operation", "sourceText", "audience", "purpose",
              "tone", "target_words", "slop_depth", "fact_check"):
        if payload.get(k) is not None:
            cfg[k] = payload[k]
    cfg.setdefault("operation", "write")
    _express_into(cfg, payload)
    sp = _style_prompt_of(payload.get("style_id"))
    if sp:
        cfg["idea"] = "\n".join(x for x in ((cfg.get("idea") or "").strip(), "目标文风：" + sp) if x)
    _provider_into(cfg, payload)
    ref_ids = _refs_into(cfg, payload, ("title", "idea", "genre"))
    return _spawn_project("prose", "e_", prose_writer.create_project,
                          prose_writer._safe, "未命名散文", cfg, ref_ids)


def _spawn_simple_writer(medium: str, mod, payload: dict) -> dict:
    """v4.10-4.12：children/news/copy 共用入口。
    v4.12.1 重大修复：此前在请求线程里同步跑 create_project（多次 LLM 调用）→ 请求超时。
    现在先落骨架目录 + 注册 PROJECT_INDEX + 立即返回 key，流水线放后台线程（与小说/剧本一致）。"""
    import threading as _th
    from datetime import datetime as _dt
    cfg = dict(payload.get("cfg_overrides") or {})
    for k, v in payload.items():
        if k not in ("medium", "cfg_overrides", "reference_ids", "provider_id", "model"):
            cfg[k] = v
    if payload.get("provider_id"):
        cfg.setdefault("defaults", {})["provider_id"] = payload["provider_id"]
    if payload.get("model"):
        cfg.setdefault("defaults", {})["model"] = payload["model"]

    prefix = {"children": "c_", "news": "n_", "copy": "k_", "other": "o_"}[medium]
    # v5.2：极速模式隐藏标题时用短占位标题，不再把整段 idea 当目录名。
    if not str(cfg.get("title") or "").strip():
        cfg["title"] = _short_title(cfg.get("idea") or "")
    display = (cfg.get("title") or cfg.get("idea") or "未命名").strip()[:40] or "未命名"
    _name = re.sub(r'[\/:*?"<>|\s]+', "", display)[:24] or "未命名"
    base = f"{prefix}{_dt.now().strftime('%Y%m%d-%H%M%S')}-{_name}"
    pdir = safe_join(mod.PROJECTS_DIR, base, label="作品 key")
    os.makedirs(pdir, exist_ok=True)

    def _bg():
        writer.mark_pipeline(base, True)
        try:
            mod.create_project({**cfg, "_base_key": base})
        except Exception as e:
            print(f"[bg] {medium} 创建失败: {e}", flush=True)
        finally:
            writer.mark_pipeline(base, False)

    _th.Thread(target=_bg, daemon=True).start()
    return {"key": base, "pid": base, "project": base, "medium": medium,
            "name": display, "status": "planning", "async": True}


def _create_thesis_project(payload: dict) -> dict:
    """v4.6.0：论文（degree/field/title/idea/target_words/cite_style/humanize）。"""
    cfg = dict(payload.get("cfg_overrides") or {})
    for k in ("title", "idea", "degree", "field", "target_words", "cite_style", "humanize"):
        if payload.get(k) is not None:
            cfg[k] = payload[k]
    _provider_into(cfg, payload)
    _express_into(cfg, payload)
    return _spawn_project("thesis", "t_", thesis_writer.create_project,
                          thesis_writer._safe, "未命名论文", cfg, [])


_MEDIUM_CREATORS = {
    "script": _create_script_project,
    "poetry": _create_poetry_project,
    "prose": _create_prose_project,
    "thesis": _create_thesis_project,
    "children": lambda payload: _spawn_simple_writer("children", children_writer, payload),
    "news": lambda payload: _spawn_simple_writer("news", news_writer, payload),
    "copy": lambda payload: _spawn_simple_writer("copy", copy_writer, payload),
    "other": lambda payload: _spawn_simple_writer("other", other_writer, payload),
}


@app.post("/api/create")
def api_create(payload: dict):
    """REST 直接开书；payload.medium 决定进哪条流水线（novel/script/poetry/prose）。

    小说：{title?, genre?, tone?, sells?, perspective?, idea?, total_chapters?, provider_id?, model?, cfg_overrides?}。
    带 title → 直接开写；不带 → 停在"选书名"（awaiting_title，不自动开写，返回 8 候选）。
    其余文体见 _MEDIUM_CREATORS。"""
    _med = (payload.get("medium") or "novel")
    _creator = _MEDIUM_CREATORS.get(_med)
    if _creator:
        return _creator(payload)

    title = (payload.get("title") or "").strip()
    overrides = payload.get("cfg_overrides") or {}
    cfg = _deep_merge(_load_base_cfg(), overrides)
    if payload.get("genre"):
        cfg["genre"] = payload["genre"]
    if payload.get("genre_key"):       # v2.1 B：前端带来的 skillkit craft key，题材名归一失败时用它兜底
        cfg["genre_key"] = payload["genre_key"]
    if payload.get("min_words_per_chapter"):   # v2.1 B：单章目标字数（写进 plan.minWordsPerChapter）
        try:
            _w = int(payload["min_words_per_chapter"])
            if 500 <= _w <= 20000:
                cfg["min_words_per_chapter"] = _w
        except (TypeError, ValueError):
            pass
    if payload.get("tone"):
        cfg["tone"] = payload["tone"]
    if payload.get("platform"):           # v2.1 P1：发布平台（bot/platforms.py 的 key），决定节奏/开局策略
        plat = platforms.find(payload["platform"])
        if plat:
            cfg["platform"] = plat["label"]
            cfg["platform_note"] = f"{plat['pace']}；{plat['hook']}"
    if payload.get("style"):           # v0.3 P2：题材×风格技能 {金手指,基调,节奏,语言,卷}
        cfg["style"] = payload["style"]
    if payload.get("sells"):
        cfg["sells"] = payload["sells"]
    if payload.get("perspective"):
        cfg["perspective"] = payload["perspective"]
    # v4.15.7：向导新字段白名单补齐——batch_size/penname/agents 此前在小说创建路径全部被丢弃
    if payload.get("batch_size"):
        cfg["batch_size"] = str(payload["batch_size"])
    if payload.get("penname"):
        cfg["penname"] = str(payload["penname"]).strip()[:40]
    if payload.get("agents"):
        cfg["agents"] = payload["agents"] if isinstance(payload["agents"], list) else [payload["agents"]]
    # v4.36.0：作品形态（serial 连载 / book 出版）。它决定章数、章长、节奏纪律、
    # 伏笔回收率与结局是否必须闭合；不写进 cfg 的话，后面所有形态相关判定都会回落到连载。
    if payload.get("form"):
        try:
            from bot import taste as _taste
            _fm = _taste.normalize(payload["form"])
            cfg["form"] = _fm
            _sp = _taste.spec(_fm, cfg)
            if not payload.get("min_words_per_chapter"):
                cfg["min_words_per_chapter"] = int(_sp["default_chapter_words"])
            # 出版型没显式选章数时，用形态默认章数（45），别把连载的 300 章套上去
            if not payload.get("total_chapters") and _fm == _taste.FORM_BOOK:
                payload = dict(payload)
                payload["total_chapters"] = int(_sp["default_chapters"])
                payload.pop("scale", None)
        except Exception:
            pass
    idea = (payload.get("idea") or "").strip()
    # v4.15.7 重大修复：原逻辑 title 覆盖 theme——书名「拟人E2E-百万字」这类标签
    # 会把用户的一句话简介挤掉，大纲阶段 LLM 拿着书名自由发挥（实测整个世界观跑偏成
    # 「拟人化E2E AI 系统」赛博文）。正确语义：theme=创意（idea），title 只做书名。
    if idea:
        cfg["theme"] = idea
    elif title:
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
    # v4.24.0：极速模式。界面上有两条入口——小说的「创作模式」卡片（mode=express）
    # 与其余文体的「创作节奏」开关（express=express）。两者等价，统一折成 cfg.express。
    _express_into(cfg, payload)
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
    _ts = _dt.now().strftime('%Y%m%d-%H%M%S')
    _express_mode = bool(cfg.get("express"))
    _provisional = _short_title(payload.get('idea') or title or '未命名')
    _safe_name = re.sub(r'[/:*?"<>|\s]+', '', title or _provisional)[:24]
    base = f"{_ts}-{_safe_name}" or f"bk_{_ts}"
    # 去掉路径分隔符/空白，书名不参与目录命名以外的用途
    try:
        pdir = safe_join(writer.PROJECTS_DIR, base, label="作品 key")
    except ValueError:
        return JSONResponse({"error": "书名包含非法字符"}, status_code=400)
    os.makedirs(pdir, exist_ok=True)
    skeleton_plan = {"version": 1, "novelName": title or _provisional,
                     "projectPath": pdir, "status": "planning",
                     "chapters": [], "totalChapters": total, "totalWords": 0,
                     "minWordsPerChapter": int(cfg.get("min_words_per_chapter") or 3000),
                     "config": {k: v for k, v in cfg.items() if k != "api_key"},
                     "meta": {"awaiting_title": bool(not title and not _express_mode),
                              "title_candidates": [],
                              "planning_running": True},
                     "createdAt": _dt.now().isoformat()}
    atomic_write_json(os.path.join(pdir, "02-写作计划.json"), skeleton_plan)
    _st = cfg.get("style") or {}
    learn.record_book(_st.get("题材", ""), _st.get("金手指", ""), _st.get("节奏", ""), _st.get("语言", ""))
    def _bg():
        plan_path = os.path.join(pdir, "02-写作计划.json")
        livejob.begin(base, kind="planning", worker_label="规划师",
                      detail="正在生成人物档案/大纲/文风…")   # v4.34.1：规划在途的可靠信号
        try:
            cfg2 = workflow.fill_defaults(cfg)   # 内部含 LLM 调用，必须留在后台线程
            n = int((cfg2.get("defaults") or {}).get("title_count", 8))
            if title:
                titles = [{"title": title, "technique": "指定书名", "why": "用户指定"}]
                cands = []
            else:
                cands = workflow.gen_titles(cfg2, n=n) or [{"title": _provisional, "technique": "", "why": "候选生成失败，沿用短标题"}]
                if _express_mode:
                    # v5.2：极速模式 = 一键直接开写。书名由引擎候选自动定第一个，
                    # 不再把用户拦在 awaiting_title 里等选名，也不把整段 idea 当书名。
                    titles = [{"title": (cands[0].get("title") or _provisional),
                               "technique": "极速自动选名", "why": "极速模式自动采用首个候选"}]
                else:
                    titles = [{"title": _provisional, "technique": "占位", "why": ""}]
            # 关键：把 create_project 写到同一个骨架目录，避免“接口返回 key A、实际建了目录 B”
            proj = writer.create_project(cfg2, titles, pdir=pdir)
            _attach_style_ref(proj, style_id)
            _link_references(os.path.basename(proj.path), ref_ids)
            plan = proj.plan() or {}
            meta = plan.get("meta") or {}
            if _express_mode and not title:
                # 极速路径：把占位目录改名为正式书名（旧 key 记别名，前端旧请求自动跳转），
                # 并立即开写。pick_title 会写入 novelName / selected_title。
                # v5.5.2：规划期间作者可能已抢先定名（直聊/极速模式下聊天里说「改名X」
                # → _apply_title 换 key）。create_project 的全新计划会把 novelName 与
                # meta 一并冲掉，唯一可靠的信号是当前目录名（create_project 的 _sync_home
                # 会跟随改名落盘）——目录已离开骨架占位名 = 作者已定名，沿用作者的选择，
                # 不再被首个候选覆盖（「书名随时可改」在规划在途时也成立）。
                _home = os.path.basename(proj.path or "")
                _user_name = _clean_book_title(_home.split("-", 2)[-1]) if _home else ""
                if _home != base and _user_name \
                        and _user_name != _clean_book_title(_provisional):
                    best = _user_name
                else:
                    best = str(titles[0].get("title") or _provisional)
                newkey = writer.pick_title(base, best) or base
                proj = writer.Project(writer._project_dir(newkey), newkey)
                plan = proj.plan() or {}
                meta = plan.get("meta") or {}
                meta["title_candidates"] = cands
                meta["title_picked"] = True
                meta["awaiting_title"] = False
                meta["planning_running"] = False
                plan["meta"] = meta
                proj.save_plan(plan)
                proj.log(f"✅ 极速模式：自动定名《{best}》并开始创作")
            elif title:
                plan["meta"] = meta
                proj.save_plan(plan)
                # v4.20.0：开书第一批也要遵守「一次写几张」设置（此前漏传 → 第一批直接全写完）
                writer.start_writing(proj, batch_size=(plan.get("config") or {}).get("batch_size"))
            else:
                # v4.34.1 幻影双书根治：规划期间用户可能已在对话里选完书名（目录已被
                # _apply_title rename 走）。跟随改名（Project._follow_rename）后这里
                # proj 已指向新目录；重读磁盘计划判断现状：
                #   · 书已开写（明确指令已生效）→ 绝不用 awaiting_title 覆盖真实状态；
                #   · 用户已选书名（meta.title_picked）→ 规划完直接开写，不再停留等待。
                plan = proj.plan() or plan
                meta = plan.get("meta") or {}
                if str(plan.get("status")) in ("in_progress", "writing", "validating", "completed") or \
                        (writer.done_chapters(plan, proj.path) if plan.get("chapters") else 0) > 0:
                    meta["planning_running"] = False
                    meta["awaiting_title"] = False
                    plan["meta"] = meta
                    proj.save_plan(plan)   # 只关掉规划标志，状态/章卡一概不碰
                elif meta.get("title_picked"):
                    meta["title_candidates"] = cands
                    meta["awaiting_title"] = False
                    meta["planning_running"] = False
                    plan["meta"] = meta
                    plan["status"] = "in_progress"
                    proj.save_plan(plan)
                    proj.log("✅ 规划就绪（书名已选定），自动开始创作")
                    writer.start_writing(proj, batch_size=(plan.get("config") or {}).get("batch_size"))
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
                # v5.5：失败落盘也要跟随改名——规划中途用户选完书名 os.rename 走了，
                # 原样写 pdir 会把旧目录复活成幻影（v4.34.1 一直在治的病根）。
                try:
                    plan_path = os.path.join(writer._project_dir(base), "02-写作计划.json")
                except Exception:
                    pass
                os.makedirs(os.path.dirname(plan_path), exist_ok=True)
                pl = json.load(open(plan_path, encoding="utf-8"))
                pl["status"] = "failed"; pl.setdefault("meta", {})["planning_running"] = False
                pl["meta"]["error"] = str(e)[:200]
                atomic_write_json(plan_path, pl)
            except Exception:
                pass
        finally:
            livejob.finish(base, ok=True, detail="规划线程结束")   # v4.34.1：收口登记
    _th.Thread(target=_bg, daemon=True).start()
    return {"project": base, "pid": base, "key": base,
            "awaiting_title": bool(not title and not _express_mode), "express": _express_mode,
            "async": True,
            "candidates": [] if (title or _express_mode) else [{"title": "候选生成中…", "technique": "", "why": ""}]}

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
_ZOMBIE_SWEPT = False   # v4.34.1：幻影目录清扫每进程一次（/api/library 触发）
_REWRITING = {}  # key -> {"running":bool,"job":id,"result":dict}
_REWRITE_JOBS = {}  # key -> 当前定向改写 job id（旧 job 收尾不得覆盖新 job 状态）

def _start_rewrite(key, chapters=None, dimensions=None, max_rounds=6, supersede=True):
    """v2.3.0：局部重写统一入口（/api/rewrite 端点与 attached 对话共用，不重复造轮子）。
    已在跑则返回 False；supersede=True 时让旧 job 失效、立刻按新指令开跑。
    v4.24.16：检查+置位加注册表互斥（与 /api/parallel-write 同款 TOCTOU 修复）。"""
    with _RUNNING_REG_LOCK:
        if _REWRITING.get(key, {}).get("running"):
            if not supersede:
                return False
            writer.supersede_rewrites(key)
        jid = uuid.uuid4().hex
        _REWRITE_JOBS[key] = jid
        _REWRITING[key] = {"running": True, "job": jid, "result": None}

    # v5.8.0：协作模式（逐章确认）——重写若发生在「第 n 章待作者确认」位上，
    # 先解除暂停位让重写能跑（run_guided_rewrite 的 is_paused 检查点会拦），
    # 重写收尾 end_collab_rewrite 恢复「仍停在本章待确认」：重写完不自动继续，
    # 新稿仍要作者拍板。不在待确认位时 begin 返回 0、end 直接短路，零副作用。
    writer.begin_collab_rewrite(key)

    def _job():
        _res = None
        try:
            _res = writer.run_guided_rewrite(key, chapters=chapters,
                                             dimensions=dimensions, max_rounds=max_rounds)
            if _REWRITE_JOBS.get(key) == jid:
                _REWRITING[key] = {"running": False, "job": jid, "result": _res}
        except Exception as e:
            if _REWRITE_JOBS.get(key) == jid:
                _REWRITING[key] = {"running": False, "job": jid, "result": {"error": str(e)}}
        finally:
            writer.end_collab_rewrite(key, _res)
    threading.Thread(target=_job, daemon=True).start()
    return True

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
    """{name, text?, urls?[], genre?, medium?}。粘贴 text 或抓 urls → 蒸馏成命名风格 → 存 styles/。

    medium 决定用哪套风格维度（小说/剧本/诗词/散文），空串按小说算。
    """
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
    return distill.distill(corpus, name, (payload.get("genre") or "").strip(), source,
                            cfg=payload.get("cfg"), medium=payload.get("medium") or "")

# ---------- v0.3.4：自我进化（学习报告）----------
@app.get("/api/learn")
def api_learn():
    """自我进化引擎报告：写过的题材分布、各题材偏好（金手指/节奏/语言）、平均成书质量。"""
    return learn.profile()
@app.get("/api/library")
def api_library(sort: str = "updated", order: str = "desc", genre: str = "",
                status: str = "", q: str = "", medium: str = "", view: str = "active"):
    """view=active|archived|trash 三视图；旧前端用 status=archived 的写法继续兼容。"""
    if status == "archived" and view == "active":
        view = "archived"
    if view not in ("active", "archived", "trash"):
        view = "active"

    # v4.34.1：清扫「改名后复活」的幻影目录（一个任务两本书的历史损伤）。
    # 每进程只扫一次；rename_map 里别名源目录与目标并存的残留按安全条件清理。
    global _ZOMBIE_SWEPT
    if not _ZOMBIE_SWEPT:
        _ZOMBIE_SWEPT = True
        try:
            writer.sweep_rename_zombies()
        except Exception:
            pass

    everything = writer.library_all(include_archived=True)
    if view == "archived":
        base = [i for i in everything if i.get("archived") and not i.get("trashed")]
    elif view == "trash":
        base = [i for i in everything if i.get("trashed")]
    else:
        base = [i for i in everything if not i.get("archived") and not i.get("trashed")]

    items = list(base)
    if genre:
        items = [i for i in items if i["genre"] == genre]
    if status and status != "archived":
        if status == "passing":
            items = [i for i in items if i.get("passing") is True]
        elif status == "unrated":
            items = [i for i in items if i.get("quality") is None]
        elif status != view:
            items = [i for i in items if i["state"] == status or i["status_label"] == status]
    if q:
        ql = q.lower()
        items = [i for i in items if ql in (i["name"] or "").lower() or ql in (i["genre"] or "").lower()]
    if medium:
        items = [i for i in items if (i.get("medium") or "novel") == medium]

    stats = writer.library_stats(base)
    counts = {
        "active": sum(1 for i in everything if not i.get("archived") and not i.get("trashed")),
        "archived": sum(1 for i in everything if i.get("archived") and not i.get("trashed")),
        "trash": sum(1 for i in everything if i.get("trashed")),
    }

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
    # v4.27.2：置顶项永远排最前（侧栏与作品库共用本接口）
    items.sort(key=lambda i: (not i.get("pinned"), i.get("pinned_at") or ""), reverse=False)
    items.sort(key=lambda i: not i.get("pinned"))   # 稳定排序：pinned 在前、其余保持原序
    facets = {"genres": sorted({i["genre"] for i in everything}),
              "mediums": sorted({i.get("medium") or "novel" for i in everything})}
    return {"items": items, "stats": stats, "counts": counts, "facets": facets,
            "sort": sort, "order": order,
            "filters": {"genre": genre, "status": status, "q": q, "medium": medium, "view": view}}

@app.get("/api/library/{key}")
def api_lib_item(key: str):
    """单作品元数据：前端冷启动只有 key，用它解析文体与进度。"""
    it = writer.library_item(key)
    if not it:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return it

@app.post("/api/library/{key}/pin")
def api_lib_pin(key: str, payload: dict = None):
    """v4.27.2：置顶/取消置顶。"""
    return writer.pin_book(key, bool((payload or {}).get("on", True)))

@app.post("/api/library/{key}/favorite")
def api_lib_favorite(key: str, payload: dict):
    return writer.set_meta(key, favorite=bool(payload.get("on", True)))

@app.post("/api/library/{key}/tags")
def api_lib_tags(key: str, payload: dict):
    return writer.set_meta(key, tags=payload.get("tags") or [])

@app.post("/api/library/{key}/archive")
def api_lib_archive(key: str, payload: dict):
    """归档 = 收进作品库「归档」页；与回收站互斥。"""
    return writer.archive_book(key, bool(payload.get("on", True)))

@app.post("/api/library/{key}/trash")
def api_lib_trash(key: str):
    """移入回收站：默认列表与侧栏都隐藏，可恢复。"""
    return writer.trash_book(key)

@app.post("/api/library/{key}/restore")
def api_lib_restore(key: str):
    return writer.restore_book(key)

def _after_book_gone(key: str):
    """v4.24.16：作品彻底删除后的运行期注册表清理——否则 PROJECT_INDEX 仍持有指向
    已删路径的 Project（缓存命中直接返回），queue_action 等写入路径会把已删目录
    连同空壳计划「复活」；活动流与并行/改写状态也要一并清。"""
    PROJECT_INDEX.pop(key, None)
    for reg in (_PARALLEL_RUNNING, _REWRITING, _RELEASE_RUNNING, _ONESHOT_RUNNING,
                _PARA_RW_RUNNING):
        try:
            reg.pop(key, None)
        except Exception:
            pass


@app.delete("/api/library/{key}/purge")
def api_lib_purge(key: str, confirm: str = ""):
    """彻底删除（不可逆）：confirm 必须与作品显示名完全一致。"""
    res = writer.purge_book(key, confirm)
    if not res.get("ok"):
        return JSONResponse(res, status_code=400)
    _after_book_gone(key)
    return res

@app.post("/api/library/{key}/rename")
def api_lib_rename(key: str, payload: dict):
    return writer.rename_book(key, (payload or {}).get("name", ""))

@app.delete("/api/library/{key}")
def api_lib_delete(key: str, hard: bool = False, confirm: str = ""):
    """默认软删=移入回收站；?hard=true 需同时给 confirm=作品名，才会彻底删除。"""
    if not hard:
        return writer.trash_book(key)
    res = writer.purge_book(key, confirm)
    if not res.get("ok"):
        return JSONResponse(res, status_code=400)
    _after_book_gone(key)
    return res

@app.post("/api/library/batch")
def api_lib_batch(payload: dict):
    keys = payload.get("keys") or []
    act = payload.get("action")
    out = []
    for k in keys:
        if act == "archive":
            r = writer.archive_book(k, True)
        elif act == "unarchive":
            r = writer.archive_book(k, False)
        elif act in ("trash", "delete"):
            r = writer.trash_book(k)
        elif act == "restore":
            r = writer.restore_book(k)
        elif act == "favorite":
            writer.set_meta(k, favorite=True)
            r = {"ok": True}
        elif act == "pin":
            import datetime as _dt
            writer.set_meta(k, pinned=True, pinned_at=_dt.datetime.now().isoformat())
            r = {"ok": True}
        elif act == "unpin":
            writer.set_meta(k, pinned=False)
            r = {"ok": True}
        elif act == "purge":
            r = {"ok": False, "note": "彻底删除需逐条确认作品名"}
        else:
            r = {"ok": False, "note": f"未知操作 {act}"}
        out.append({"key": k, **r})
    return {"ok": True, "action": act, "results": out}

@app.get("/api/compare")
def api_compare(keys: str):
    ks = [k for k in keys.split(",") if k][:4]
    return {"items": [writer.library_item(k) for k in ks]}

@app.post("/api/rewrite")
def api_rewrite(payload: dict):
    key = payload.get("key")
    proj = _get_project(key)
    if not proj:
        # 内存中未命中时尝试从磁盘加载
        try:
            pdir = writer._project_dir(key)
            if os.path.isdir(pdir):
                PROJECT_INDEX[key] = writer.Project(pdir, key)
                proj = PROJECT_INDEX[key]
        except ValueError:
            pass
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    if _REWRITING.get(key, {}).get("running"):
        return {"ok": True, "running": True, "msg": "重写进行中，请稍候…"}
    _probe = _model_probe_error(proj)
    if _probe:
        return JSONResponse({"error": _probe, "code": "provider_down"}, status_code=400)
    # v4.41.4：单章重写默认最多 2 轮（旧默认 6 轮，模型连接一坏就白烧 6 次）
    _rounds = max(1, min(int(payload.get("max_rounds") or 2), 3))
    _start_rewrite(key, chapters=payload.get("chapters"), dimensions=payload.get("dimensions"),
                   max_rounds=_rounds)
    return {"ok": True, "running": True, "msg": "🔁 审查后定向重写中…（最多 2 轮，模型异常会立即停手）"}

# ---------- v2.3.0：标题体系 + 一致性体检 ----------

@app.post("/api/title/set")
def api_title_set(payload: dict):
    key = str(payload.get("key") or "")
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    res = _apply_title(key, str(payload.get("title") or ""))
    if not res.get("ok"):
        return JSONResponse({"error": res.get("error") or "改名失败"}, status_code=400)
    return res

@app.post("/api/title/generate")
def api_title_generate(payload: dict):
    key = str(payload.get("key") or "")
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    try:
        n = min(max(int(payload.get("n") or 5), 1), 10)
    except (TypeError, ValueError):
        n = 5
    items = _generate_title_candidates(key, n=n)
    return {"ok": bool(items), "items": items}

@app.get("/api/title/candidates")
def api_title_candidates(key: str):
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return {"items": _read_title_items(key)}

# ---------- v4.15.11：一致性一键修复（全部一键修复） ----------

_FIXABLE_REWRITE_RE = re.compile(r"第(\d+)章")


@app.post("/api/providers/fetch-models")
def api_providers_fetch_models(payload: dict):
    """拉取供应商的可用模型列表（GET {base_url}/models）。
    添加/编辑供应商时，填完地址与密钥即可一键拉取。
    v4.19.1 修复：编辑已保存供应商时密钥框按设计不回显（留空=不修改），
    此前把空密钥直接发给接口导致 401，用户误以为密钥丢失。现在留空且带
    provider_id 时自动回落到该供应商已保存的密钥（明文不回显但服务端可复用）。"""
    import urllib.request as _ur
    base = str(payload.get("base_url") or "").strip().rstrip("/")
    key = str(payload.get("api_key") or "").strip()
    pid = str(payload.get("provider_id") or "").strip()
    used_saved_key = False
    if not key and pid:
        try:
            _p = providers._require_id(providers.load_data(), pid)
            key = providers.key_of(_p)
            used_saved_key = bool(key)
        except providers.ProviderError:
            pass
    if not base:
        return JSONResponse({"error": "请先填写接口地址"}, status_code=400)
    url = f"{base}/models"
    req = _ur.Request(url, headers={
        "Authorization": f"Bearer {key}" if key else "",
        "User-Agent": "yimu-lit",
        "Accept": "application/json"})
    try:
        with _ur.urlopen(req, timeout=12) as resp:
            data = json.load(resp)
        ids = [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]
        if not ids:
            # 兼容 {"models":[...]} 形态（Ollama 原生）
            ids = [str(m.get("name") or m.get("model")) for m in (data.get("models") or []) if (m.get("name") or m.get("model"))]
        return {"ok": True, "models": sorted(set(ids)), "count": len(set(ids)),
                "used_saved_key": used_saved_key}
    except Exception as e:
        return JSONResponse({"error": f"拉取失败（{type(e).__name__}）：请检查接口地址与密钥"}, status_code=502)


# ---------- v4.15.17：应用内一键自更新（弹窗确认 → 拉取 → 覆盖 → 重启） ----------

# 更新时覆盖的白名单（源码与对外文件）；用户数据（projects/config/providers/data/usage）一律不动
_UPDATE_SYNC = [
    "app.py", "run.py", "VERSION", "requirements.txt", "requirements-dev.txt",
    "pyproject.toml", "package.json", "config.example.json",
    "CHANGELOG.md", "README.md", "README_EN.md", "LICENSE", "NOTICE.md",
    "start.sh", "start.bat", "build.py",
    "bot", "static", "skillkit", "tests", "tools", "docs",
    ".github", "Dockerfile", "docker-compose.yml", ".dockerignore",
]

@app.post("/api/self-update")
def api_self_update():
    """一键自更新：下载 GitHub 最新 release 源码 → 白名单覆盖 → 返回需重启。
    v4.28.0：桌面版（PyInstaller 冻结包）源码覆盖无效——改走 overlay 更新：
    下载新版代码包解压到数据目录 runtime 层，重启进程即从新代码启动（v4.29.0）。"""
    if os.environ.get("YIMU_DESKTOP") == "1" or getattr(sys, "frozen", False):
        from bot import updater as _upd
        res = _upd.desktop_update()
        if res.get("updated"):
            res["need_restart"] = True
        return res
    import zipfile, io, shutil
    repo = "yimuhub/yimu-lit"
    token = None
    try:
        import subprocess as _sp
        _out = _sp.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n",
                       capture_output=True, text=True, timeout=8).stdout
        for _line in _out.splitlines():
            if _line.startswith("password="):
                token = _line.split("=", 1)[1]
    except Exception:
        pass
    # 1. 最新版本
    try:
        rel = _gh_fetch(f"https://api.github.com/repos/{repo}/releases/latest", token=token, timeout=12)
        remote = str(rel.get("tag_name") or "").lstrip("v")
    except Exception as e:
        return JSONResponse({"error": f"获取最新版本失败（{type(e).__name__}）"}, status_code=502)
    local = version.current()
    def _vk(v):
        try:
            return [int(x) for x in str(v).split(".")]
        except Exception:
            return [0]
    if not remote or _vk(remote) <= _vk(local):
        return {"ok": True, "updated": False, "version": local, "msg": f"已是最新版本 v{local}"}
    # 2. 下载 zipball（tag）
    try:
        url = f"https://api.github.com/repos/{repo}/zipball/v{remote}"
        blob = _gh_fetch(url, token=token, timeout=120, retries=2, binary=True)
    except Exception as e:
        return JSONResponse({"error": f"下载新版本失败（{type(e).__name__}）"}, status_code=502)
    # 3. 解压并白名单覆盖
    root = ROOT
    backup_dir = os.path.join(root, "data", "update_backup", f"v{local}-{int(time.time())}")
    replaced = 0
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = zf.namelist()
            if not names:
                return JSONResponse({"error": "压缩包为空"}, status_code=502)
            top = names[0].split("/")[0] + "/"
            # 先备份当前版本的关键源码（可回滚）
            os.makedirs(backup_dir, exist_ok=True)
            for item in _UPDATE_SYNC:
                src = os.path.join(root, item)
                if os.path.exists(src):
                    dst = os.path.join(backup_dir, item)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if os.path.isfile(src):
                        shutil.copy2(src, dst)
                    else:
                        shutil.copytree(src, dst, dirs_exist_ok=True)
            # 覆盖
            for item in _UPDATE_SYNC:
                prefix = top + item
                # v4.15.17 修复：文件/目录判断不能用扩展名（Dockerfile 等无扩展名文件
                # 会被当目录 → 对已存在文件 makedirs → FileExistsError）；按 zip 内子路径判断。
                is_dir = any(n.startswith(prefix + "/") for n in names)
                if is_dir:
                    os.makedirs(os.path.join(root, item), exist_ok=True)
                    for n in names:
                        if not n.startswith(prefix + "/") or n.endswith("/"):
                            continue
                        rel = n[len(top):]
                        target = os.path.join(root, rel)
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with zf.open(n) as fsrc, open(target, "wb") as fdst:
                            fdst.write(fsrc.read())
                    replaced += 1
                elif prefix in names:
                    with zf.open(prefix) as fsrc, open(os.path.join(root, item), "wb") as fdst:
                        fdst.write(fsrc.read())
                    replaced += 1
    except Exception as e:
        return JSONResponse({"error": f"解压覆盖失败（{type(e).__name__}），已保留原版本；备份在 {backup_dir}"}, status_code=500)
    new_version = version.current()
    return {"ok": True, "updated": True, "from": local, "to": new_version,
            "replaced": replaced, "need_restart": True,
            "msg": f"已更新 v{local} → v{new_version}（覆盖 {replaced} 项），需重启服务生效"}


@app.post("/api/restart")
def api_restart(request: Request):
    """重启服务（自更新后调用）。v4.15.18：-m uvicorn 方式（脚本路径方式会遮蔽标准库）+ 端口取自当前请求。
    v4.29.0：桌面版（冻结包/overlay）→ 重启整个应用进程（新进程带 YIMU_OVERLAY 加载新代码）。"""
    if os.environ.get("YIMU_DESKTOP") == "1" or getattr(sys, "frozen", False):
        from bot import updater as _upd
        syslog_mod.log_event("system", "桌面应用重启", {}, "自动更新后重启桌面进程")
        threading.Timer(0.8, _upd.desktop_restart).start()
        return {"ok": True, "msg": "应用正在重启，窗口将自动恢复；稍后自动刷新页面"}
    import sys as _sys, subprocess as _sp
    _port = request.url.port or int(os.environ.get("NOVEL_BOT_PORT") or 46821)
    syslog_mod.log_event("system", "重启服务", {"port": _port}, "手动重启服务")
    _uv = [ _sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(_port) ]
    # v4.15.18：新进程延迟 2.5 秒再绑定端口——旧进程 timer 0.6s 后退出，避免 address in use 竞争
    if _sys.platform == "win32":
        _sp.Popen(_uv, cwd=ROOT, creationflags=_sp.CREATE_NEW_PROCESS_GROUP | _sp.DETACHED_PROCESS)
    else:
        _sh = [ "/bin/bash", "-c", f"sleep 2.5; exec {' '.join(_uv)}" ]
        _sp.Popen(_sh, cwd=ROOT, start_new_session=True,
                  stdout=open(os.path.join(ROOT, "nb46831.log"), "ab"), stderr=_sp.STDOUT)
    threading.Timer(0.6, lambda: os._exit(0)).start()
    return {"ok": True, "msg": "服务正在重启，约 5 秒后自动恢复；页面稍后自动刷新"}


@app.get("/api/diagnostics")
def api_diagnostics():
    """v4.16.0：系统日志聚合——每本书的创作日志（走到哪/卡多久/最后发生了什么）、
    服务日志尾部、模型调用状态。供「设置→系统日志」展示与一键导出诊断包。"""
    import time as _t
    now = int(_t.time())
    books = []
    if os.path.isdir(writer.PROJECTS_DIR):
        for d in sorted(os.listdir(writer.PROJECTS_DIR), reverse=True)[:60]:
            pdir = os.path.join(writer.PROJECTS_DIR, d)
            if not os.path.isdir(pdir):
                continue
            med, pj, plan = writer._plan_of(pdir)
            if not med:
                continue
            log_path = os.path.join(pdir, "运行日志.txt")
            tail_lines, last_line, stalled = [], "", 0
            if os.path.exists(log_path):
                try:
                    with open(log_path, encoding="utf-8", errors="replace") as f:
                        tail_lines = [l.rstrip() for l in f.readlines()[-40:]]
                    if tail_lines:
                        last_line = tail_lines[-1]
                        m = re.match(r"\[(\d{2}:\d{2}:\d{2})\]", last_line)
                        # 卡住判断：日志文件 mtime 距今超 10 分钟且书未完结
                        age = int(now - os.path.getmtime(log_path))
                        state = (plan.get("status") or "unknown")
                        if state in ("in_progress", "writing", "planning") and age > 600:
                            stalled = age
                except Exception:
                    pass
            books.append({
                "key": d,
                "name": plan.get("novelName") or plan.get("scriptName") or plan.get("otherName")
                        or plan.get("bookName") or plan.get("newsName") or plan.get("copyName") or d,
                "medium": med,
                "state": plan.get("status") or "unknown",
                "done": writer.done_chapters(plan, pdir),
                "total": len(plan.get("chapters") or []),
                "stalled_seconds": stalled,
                "last_line": last_line[-160:],
                "tail": tail_lines,
            })
    # 服务端日志（nb*.log 取最新的）
    server_tail = []
    try:
        nb = sorted([f for f in os.listdir(ROOT) if f.startswith("nb") and f.endswith(".log")],
                    key=lambda f: os.path.getmtime(os.path.join(ROOT, f)))
        if nb:
            with open(os.path.join(ROOT, nb[-1]), encoding="utf-8", errors="replace") as f:
                server_tail = [l.rstrip() for l in f.readlines()[-60:]]
    except Exception:
        pass
    # 模型调用状态
    llm_state = {}
    try:
        from bot import llm as _llm
        llm_state = {"last_call_at": int(getattr(_llm, "_last_call", {}).get("ts") or 0),
                     "ago_seconds": int(now - getattr(_llm, "_last_call", {}).get("ts", 0)) if getattr(_llm, "_last_call", {}).get("ts") else None}
    except Exception:
        pass
    # v4.16.8：供应商配置脱敏摘要（一眼看出 key 未配/供应商未启用——401 排查无需猜）
    prov_summary = []
    try:
        pv = json.load(open(os.path.join(_DATA_ROOT, "providers.json"), encoding="utf-8"))
        for p in (pv.get("providers") or []):
            k = str(p.get("api_key") or "")
            prov_summary.append({"name": p.get("id") or p.get("name"), "base_url": p.get("base_url"),
                                 "has_key": bool(k), "key_len": len(k), "key_prefix": k[:6] + "***" if k else "",
                                 "timeout": p.get("timeout"), "models": (p.get("models") or [])[:5]})
        prov_summary.append({"default_provider": pv.get("default_provider")})
    except Exception:
        prov_summary = [{"error": "providers.json 读取失败"}]
    # v4.16.8：模型调用错误日志尾部（llm_errors.jsonl）
    llm_errors = []
    try:
        _el = os.path.join(ROOT, "data", "llm_errors.jsonl")
        if os.path.exists(_el):
            with open(_el, encoding="utf-8", errors="replace") as f:
                llm_errors = [l.rstrip() for l in f.readlines()[-40:]]
    except Exception:
        pass
    return {"ok": True, "now": now, "version": version.current(),
            "platform": f"{os.name}/{sys.platform}",
            "books": books, "server_tail": server_tail, "llm_state": llm_state,
            "providers": prov_summary, "llm_errors": llm_errors}


# ---------- v4.19.0：系统级操作日志（设置/供应商/技能/岗位/启停等系统侧流水） ----------

@app.get("/api/syslog")
def api_syslog(kind: str = "", q: str = "", limit: int = 300):
    """系统级操作日志：最新在前。kind 过滤一类；q 关键词过滤。"""
    return {"ok": True, "total": syslog_mod.count(), "items": syslog_mod.tail(limit=limit, kind=kind, q=q)}


@app.post("/api/syslog/clear")
def api_syslog_clear():
    # 先删文件、再重新落一条「清空」留痕（先记会被 clear 一起删掉）
    syslog_mod.clear()
    syslog_mod.log_event("system", "清空日志", None, "清空系统操作日志")
    return {"ok": True}


# ---------- v4.19.0：会话级创作日志（原有 运行日志.txt / chat_log.jsonl）的统一说明见 /api/diagnostics ----------

_PARA_RW_RUNNING: dict = {}
_RW_JOBS: dict = {}          # key -> 当前改写 job id（旧 job 收尾时不得清掉新 job 的运行标志）


def _rw_claim(key):
    """登记一次新的改写 job，返回 job id。"""
    jid = uuid.uuid4().hex
    _RW_JOBS[key] = jid
    _PARA_RW_RUNNING[key] = {"running": True, "job": jid, "started_at": int(time.time())}
    return jid


def _rw_release(key, jid, res):
    """只允许 job 自己收尾——防止被取代的旧 job 把新 job 标成「没在跑」。"""
    if _RW_JOBS.get(key) == jid:
        _PARA_RW_RUNNING[key] = {"running": False, "job": jid, "result": res}


def _rw_batch(key, todo, running_map, target=None, workers=3, max_rounds=2, job=None):
    """跑一批并行改写，并保证运行标志一定被清掉（异常时照样清，否则该书永远「在跑」）。"""
    try:
        res = writer.run_parallel_rewrite(key, chapters=todo, workers=workers,
                                          max_rounds=max_rounds, target=target)
        if job:
            _rw_release(key, job, res)
        else:
            running_map[key] = {"running": False, "result": res}
        return res
    except Exception as e:
        if job:
            _rw_release(key, job, {"error": str(e)})
        else:
            running_map[key] = {"running": False, "result": {"error": str(e)}}
        raise


def _rw_short_batch(key, todo, below_words, job):
    """按字数补字批次；job id 保证被新指令取代后不会误清新任务的运行标志。"""
    try:
        res = writer.run_short_expand(key, chapters=todo, below_words=int(below_words), workers=2)
        _rw_release(key, job, res)
        return res
    except Exception as e:
        _rw_release(key, job, {"error": str(e)})
        raise


def _rw_finish_batch(key, todo, target_words, job):
    """N03 截断补写专用批次（v4.41.4）。

    v4.41.1 用 expand_short_chapter 直接扩写，结果模型被要求「输出完整章节」，
    六七千字的章只回两千多字、结尾仍未收全，三轮全废。现在改为
    finish_truncated_chapter：从真实断点续写，只补结尾、不整章重写，
    并且只在结尾完整时保留——这是硬伤修复，不再拿评分当保留条件。
    """
    try:
        from concurrent.futures import ThreadPoolExecutor
        proj = writer.Project(writer._project_dir(key), key)

        def one(n):
            try:
                return writer.finish_truncated_chapter(proj, n, max_rounds=2)
            except Exception as e:
                return {"ch": n, "kept": False, "note": f"异常：{e}"}

        with ThreadPoolExecutor(max_workers=2) as ex:
            res = list(ex.map(one, todo))
        _rw_release(key, job, res)
        return res
    except Exception as e:
        _rw_release(key, job, {"error": str(e)})
        raise


def _model_probe_error(proj) -> str:
    """开大批量改写前先探一次模型连接（v4.41.4）。

    作者实拍：点「重新写」后每章都失败，日志全是 401 / timed out / 404，
    系统仍然一轮一轮重试——重试不会成功，只会白烧时间。这里在开跑前拦住。
    """
    try:
        from bot import providers as _pv
        cfg = (proj.plan() or {}).get("config") or {}
        _p, _m = _pv.resolve(cfg)
        r = _pv.test_provider(_p.get("id"), probe_chat=False)
        if r.get("ok"):
            return ""
        d = str(r.get("detail") or "")
        if "鉴权" in d or "无法连接" in d:
            return (f"模型连接不可用：{d}。请到「设置 → 模型与供应商」点「测试连接」修好后再重写；"
                    f"这次可以直接导出原稿，硬伤章留到连接恢复后再补。")
    except Exception:
        return ""
    return ""


@app.post("/api/rewrite-parallel")
def api_rewrite_parallel(payload: dict):
    """v4.17.2：多岗位并行改写——审稿问题一次性下发，每章独立改写。
    v4.23.0：登记任务档案，中途被打断也能「继续」接着改剩下的。
    v4.41.4：①没指定章号时只挑「硬伤章（半句截断）」，不再默认把 <80 分的章全部重写；
             ②开跑前探活模型，连接坏了立刻说清楚；③已有任务在跑时返回现状，不报失败。"""
    key = str(payload.get("key") or "")
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    key = os.path.basename(proj.path)
    if _PARA_RW_RUNNING.get(key, {}).get("running"):
        prog = writer._PARA_RW_PROGRESS.get(key) or {}
        return {"ok": True, "started": False, "running": True,
                "msg": (f"✅ 改写任务已经在跑（{prog.get('done', 0)}/{prog.get('total', 0)} 章），"
                        f"不用重复点；跑完会自动收尾。")}
    _probe = _model_probe_error(proj)
    if _probe:
        return JSONResponse({"error": _probe, "code": "provider_down"}, status_code=400)
    chapters = payload.get("chapters")
    workers = max(1, min(8, int(payload.get("workers") or 3)))
    _target = payload.get("target")
    if not chapters:
        # v4.41.4：作者显式要求冲高分（带 target，来自「强制高分」）才按分数选章；
        # 普通「重新写」只修硬伤（N03 半句截断），绝不默认把 <80 分的章全部重写。
        if _target:
            try:
                _pl_t = int(_target)
            except (TypeError, ValueError):
                _pl_t = writer._pass_line()
            chapters = [p["ch"] for p in writer.per_chapter_scores(proj)
                        if isinstance(p.get("score"), (int, float)) and p["score"] < _pl_t]
            if not chapters:
                return {"ok": True, "started": False,
                        "msg": f"✅ 所有已评分章节都达到 {_pl_t} 分，不需要再重写。"}
        else:
            try:
                from bot import delivery_gate as _dg, export as _ex
                _chs = _ex.novel_chapters(proj)
                _rep = _dg.check_delivery(_chs, title=proj.name, kind="novel")
                chapters = sorted({int(str(f.get("location") or "").replace("第", "").replace("章", ""))
                                   for f in (_rep.get("findings") or [])
                                   if f.get("code") == "N03" and str(f.get("location") or "").startswith("第")})
            except Exception:
                chapters = []
            if not chapters:
                return {"ok": True, "started": False,
                        "msg": ("✅ 没有需要重写的硬伤章。低分章节不再强制重写："
                                "直接导出即可；要冲高分请在导出时勾选「强制高分」。")}
    _PARA_RW_RUNNING[key] = {"running": True, "started_at": int(time.time())}
    writer.start_task(
        key, "rewrite", f"并行改写《{proj.name}》相关章节", chapters or [],
        batch=lambda todo: _rw_batch(key, todo, _PARA_RW_RUNNING,
                                     target=_target, workers=workers, max_rounds=1),
        params={"workers": workers, "max_rounds": 1,
                **({"target": _target} if _target else {})})
    return {"ok": True, "started": True, "workers": workers,
            "msg": f"⚡ 多岗位并行改写已启动：{len(chapters)} 章（同时 {workers} 个岗位）"}


@app.get("/api/rewrite-parallel/progress")
def api_rewrite_parallel_progress(key: str):
    st = _PARA_RW_RUNNING.get(key) or {}
    prog = writer._PARA_RW_PROGRESS.get(key) or {}
    return {"ok": True, **st, **{k: prog.get(k) for k in ("done", "total", "results") if k in prog},
            "rw_running": prog.get("running", False)}


@app.post("/api/export/finish")
def api_export_finish(payload: dict):
    """v4.41.0：导出被「半句截断（N03）」拦下时，走补写收尾而不是整章重写。

    整章重写会把已经合格的章节再写一遍，评分没提高就被回滚，于是导出永远
    看到同一份内容。截断章需要的是「接着写完整」，本端点对指定章直接调用
    expand_short_chapter：先还原版本库完整旧稿，没有旧稿才请模型续写收尾。
    """
    key = str(payload.get("key") or "")
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    key = os.path.basename(proj.path)
    if _PARA_RW_RUNNING.get(key, {}).get("running"):
        return {"ok": True, "started": False, "running": True,
                "msg": "已有补写/改写任务在跑，等它完成后会自动重试导出。"}
    _probe = _model_probe_error(proj)
    if _probe:
        return JSONResponse({"error": _probe, "code": "provider_down"}, status_code=400)
    chapters = payload.get("chapters")
    if not isinstance(chapters, list):
        chapters = []
    chapters = sorted({int(c) for c in chapters if str(c).strip().isdigit()})
    if not chapters:
        try:
            pf = writer.book_health.preflight(proj, "导出前补全截断")
            chapters = sorted({x["ch"] for x in (pf.get("audit") or {}).get("truncated", [])}
                              | set((pf.get("repair") or {}).get("needs_finish") or []))
        except Exception:
            chapters = []
    if not chapters:
        return {"ok": True, "started": False, "msg": "没有需要补写的截断章节"}
    writer.supersede_rewrites(key)
    writer.clear_pause(key)
    jid = _rw_claim(key)
    writer.start_task(
        key, "rewrite", f"补全《{proj.name}》导出前截断章节", chapters,
        batch=lambda todo: _rw_finish_batch(key, todo, writer.book_health.target_words(proj), jid),
        params={"mode": "finish_truncated", "workers": 2})
    return {"ok": True, "started": True, "chapters": chapters, "workers": 2,
            "msg": f"✍️ 正在补全 {len(chapters)} 个截断章节，完成后会自动重试导出"}


@app.get("/api/canon")
def api_canon(key: str, run: int = 1):
    """正典体检：书名承诺 / 人物登记 / 对手 / 章卡结构（零 token）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    try:
        from bot import canon
        data = canon.run(proj, quiet=True) if run else canon.scan(proj)
        return {"ok": True, "key": key, **data}
    except Exception as e:
        return JSONResponse({"error": f"正典体检失败：{e}"}, status_code=500)


@app.get("/api/engine-check")
def api_engine_check(key: str):
    """故事引擎对账：章卡字段 → 正文证据（零 token）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    try:
        from bot import engine_check
        data = engine_check.run(proj, quiet=True)
        return {"ok": True, "key": key, **data}
    except Exception as e:
        return JSONResponse({"error": f"故事引擎体检失败：{e}"}, status_code=500)


@app.get("/api/retrofit")
def api_retrofit(key: str):
    """旧书整改方案：先改哪 20 章 + 书名处置（零 token）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    try:
        from bot import retrofit
        data = retrofit.run(proj, quiet=True)
        return {"ok": True, "key": key, **data}
    except Exception as e:
        return JSONResponse({"error": f"旧书整改扫描失败：{e}"}, status_code=500)


@app.get("/api/chapter-scores")
def api_chapter_scores(key: str):
    """逐章评分列表（多岗位并行改写弹窗数据源）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return {"ok": True, "key": key, "scores": writer.per_chapter_scores(proj),
            "pass_line": writer._pass_line()}


# v4.24.11：key 前缀 → 各文体计划文件名（绑定岗位/技能这类「本书配置」必须读写
# 各自文体的计划，而不是往剧本目录里塞一份 02-写作计划.json）
_BOOK_PLAN_BY_PREFIX = {
    "s_": "剧本计划.json", "p_": "诗词计划.json", "e_": "散文计划.json",
    "t_": "论文计划.json", "c_": "儿童文学计划.json", "n_": "新闻稿计划.json",
    "k_": "文案计划.json", "o_": "其他创作计划.json",
}


def _read_book_plan(key: str) -> dict:
    """按 key 前缀读对应文体的计划文件；缺失/损坏返回 {}。"""
    import json as _json
    try:
        fp = os.path.join(writer._project_dir(key),
                          _BOOK_PLAN_BY_PREFIX.get(str(key)[:2], "02-写作计划.json"))
        with open(fp, encoding="utf-8") as f:
            d = _json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_book_plan(key: str, plan: dict) -> bool:
    """按 key 前缀写回对应文体的计划文件（原子写）。"""
    from bot.safeio import atomic_write_json as _aw
    try:
        fp = os.path.join(writer._project_dir(key),
                          _BOOK_PLAN_BY_PREFIX.get(str(key)[:2], "02-写作计划.json"))
        _aw(fp, plan)
        return True
    except Exception:
        return False


def _book_plan_exists(key: str) -> bool:
    try:
        return bool(key) and os.path.isdir(writer._project_dir(key))
    except Exception:
        return False


@app.get("/api/book-agents")
def api_book_agents(key: str = ""):
    """本书绑定的创作岗位（plan.config.agents）。v4.24.11：全文体通用——按前缀读各自计划文件。"""
    if not _book_plan_exists(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    plan = _read_book_plan(key)
    agents = (plan.get("config") or {}).get("agents") or []
    from bot import agents as _ag
    _merged = _ag._all_agents()   # v4.20：内置 + 自定义岗位都能正确显示 label/stage/desc
    return {"ok": True, "key": key, "agents": [
        {"agent": a, "label": _merged.get(a, {}).get("label", a),
         "stage": _merged.get(a, {}).get("stage", ""),
         "desc": _merged.get(a, {}).get("desc", "")} for a in agents if isinstance(a, str)]}


@app.post("/api/book-agents")
def api_book_agents_set(payload: dict):
    """设置本书创作岗位（真正参与创作：并行领章署名、写作日志展示）。
    v4.24.11：全文体通用——非小说写各自计划文件，不再误建 02-写作计划.json。"""
    key = str(payload.get("key") or "")
    if not _book_plan_exists(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    from bot import agents as _ag
    raw = payload.get("agents")
    if not isinstance(raw, list):
        return JSONResponse({"error": "agents 必须是数组"}, status_code=400)
    cleaned = []
    for a in raw:
        a = str(a or "").strip()
        # v4.20：is_known_agent 覆盖内置 + 自定义岗位（此前自定义岗位加不进本书名单）
        if a and (_ag.is_known_agent(a) or str(key)[:2] == "s_"):
            cleaned.append(a)
    # v4.24.12：读-改-写纳入按书串行原语——协作页签连点/双端并发改同一本书时不再互相覆盖；
    # 读失败时绝不整文件覆盖写（否则一次瞬时读错误就会把整本计划清成只剩 config.agents）。
    with writer._acq_book(key):
        fp = os.path.join(writer._project_dir(key),
                          _BOOK_PLAN_BY_PREFIX.get(str(key)[:2], "02-写作计划.json"))
        plan, read_ok = _read_book_plan(key), False
        try:
            with open(fp, encoding="utf-8") as f:
                import json as _json
                read_ok = isinstance(_json.load(f), dict)
        except Exception:
            read_ok = False
        if not read_ok:
            return JSONResponse({"error": "计划文件不可读，已拒绝写入（防止覆盖整本计划）"}, status_code=409)
        plan.setdefault("config", {})["agents"] = cleaned
        if not _write_book_plan(key, plan):
            return JSONResponse({"error": "计划文件写入失败"}, status_code=500)
    return {"ok": True, "key": key, "agents": cleaned}


def _build_diagnostics_text(payload: dict = None):
    """生成诊断包纯文本，返回 (text, scope)。

    v4.32.2：浏览器下载（/api/diagnostics/export）与桌面原生保存（/api/save
    kind=diagnostics）必须产出同一份内容，故把文本生成从路由里抽出来共用。
    payload 可选 {sections:[works|sysops|server|llm|providers], logs:bool}；
    sections 缺省=全部；logs=True 附日志全文（服务端 2000 行 / 每本运行日志
    800 行 / 系统操作日志 2000 条，截断防爆）。"""
    import datetime as _dt
    body = payload if isinstance(payload, dict) else {}
    sections = body.get("sections") if isinstance(body.get("sections"), list) else None
    want = set(sections) if sections else {"works", "sysops", "server", "llm", "providers"}
    logs_full = bool(body.get("logs"))
    d = api_diagnostics()
    lines = [f"# 一幕·YIMU 神笔 诊断包",
             f"- 导出时间: {_dt.datetime.now().isoformat(timespec='seconds')}",
             f"- 版本: {d['version']} | 平台: {d['platform']}",
             f"- 范围: {'、'.join(sorted(want)) or '（无）'}{' + 日志全文' if logs_full else ''}",
             f"- 模型最近调用: {d.get('llm_state')}", ""]

    if "providers" in want:
        lines.append("== 模型供应商配置（脱敏） ==")
        for p in (d.get("providers") or []):
            lines.append(json.dumps(p, ensure_ascii=False))
        lines.append("")

    if "llm" in want:
        if d.get("llm_errors"):
            lines.append("== 模型调用错误日志（尾40） ==")
            lines.extend(d["llm_errors"])
            lines.append("")

    if "works" in want:
        lines.append("== 各作品状态 ==")
        for b in d["books"]:
            flag = f"  ⚠️ 疑似卡住 {b['stalled_seconds']//60} 分钟" if b.get("stalled_seconds") else ""
            lines.append(f"[{b['name']}] {b['medium']} {b['state']} {b['done']}/{b['total']}{flag}")
            lines.append(f"  最后日志: {b['last_line']}")
        lines.append("")
        for b in d["books"]:
            lines.append(f"===== {b['name']} 运行日志（尾40行）=====")
            lines.extend(b["tail"])
            lines.append("")

    if "sysops" in want:
        lines.append("== 系统操作日志（最新在前，尾 500 条） ==")
        for e in syslog_mod.tail(limit=500):
            lines.append(f"[{e.get('time')}] [{e.get('kind')}] {e.get('summary') or e.get('action')}")
            det = e.get("detail")
            if det not in (None, {}, [], ""):
                lines.append(f"    详情: {json.dumps(det, ensure_ascii=False, default=str)[:400]}")
        lines.append("")

    if "server" in want and d.get("server_tail"):
        lines.append("== 服务端日志（尾60行） ==")
        lines.extend(d["server_tail"])
        lines.append("")

    # v4.20.0：日志全文（可读、截断防爆）
    if logs_full:
        try:
            nb = sorted([f for f in os.listdir(ROOT) if f.startswith("nb") and f.endswith(".log")],
                        key=lambda f: os.path.getmtime(os.path.join(ROOT, f)))
            if nb:
                with open(os.path.join(ROOT, nb[-1]), encoding="utf-8", errors="replace") as f:
                    srv_all = [l.rstrip() for l in f.readlines()[-2000:]]
                lines.append(f"== 服务端日志全文（{nb[-1]} 尾 2000 行） ==")
                lines.extend(srv_all)
                lines.append("")
        except Exception:
            pass
        for b in d["books"]:
            log_path = os.path.join(writer.PROJECTS_DIR, b["key"], "运行日志.txt")
            try:
                if os.path.exists(log_path):
                    with open(log_path, encoding="utf-8", errors="replace") as f:
                        full = [l.rstrip() for l in f.readlines()[-800:]]
                    lines.append(f"===== {b['name']} 运行日志全文（尾 800 行）=====")
                    lines.extend(full)
                    lines.append("")
            except Exception:
                continue
        lines.append("== 系统操作日志全文（最新在前，尾 2000 条） ==")
        for e in syslog_mod.tail(limit=2000):
            lines.append(f"[{e.get('time')}] [{e.get('kind')}] {e.get('summary') or e.get('action')}")
            det = e.get("detail")
            if det not in (None, {}, [], ""):
                lines.append(f"    详情: {json.dumps(det, ensure_ascii=False, default=str)[:400]}")
        lines.append("")

    scope = "all" if not sections else "-".join(sorted(want))
    return "\n".join(lines), scope


@app.post("/api/diagnostics/export")
def api_diagnostics_export(payload: dict = None):
    """浏览器端导出诊断包（纯文本，不含密钥/作品正文）。

    v4.32.2：桌面客户端不要走这里——webview 会把 attachment 响应当成页面导航。
    桌面统一走 /api/save（弹原生「存储」对话框，服务端写盘）。"""
    text, scope = _build_diagnostics_text(payload)
    return Response(text, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=yimu-lit-diagnostics-{scope}.txt"})


def _gh_fetch(url: str, token: str | None = None, timeout: int = 15, retries: int = 3, binary: bool = False):
    """v4.16.4：GitHub 请求重试——大陆网络到 api.github.com 间歇性抖动（实测 3 连发失败 1 次），
    此前单次失败直接报「联网错误」。binary=True 返回 bytes。"""
    import urllib.request as _ur
    last = None
    for i in range(retries):
        try:
            req = _ur.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "yimu-lit",
                                            **({"Authorization": f"Bearer {token}"} if token else {})})
            with _ur.urlopen(req, timeout=timeout) as r:
                return r.read() if binary else json.load(r)
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise last


@app.get("/api/check-update")
def api_check_update():
    """v4.15.12：检查 GitHub 仓库最新 release——前端版本号点击时调用（此前端点缺失 → 404「网络错误」）。
    GitHub 仓库是唯一更新来源：有新版提示拉取；无新版/网络不通都如实返回。"""
    local = version.current()
    repo = "yimuhub/yimu-lit"
    token = None
    try:
        import subprocess as _sp
        _out = _sp.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n",
                       capture_output=True, text=True, timeout=8).stdout
        for _line in _out.splitlines():
            if _line.startswith("password="):
                token = _line.split("=", 1)[1]
    except Exception:
        token = None
    try:
        data = _gh_fetch(f"https://api.github.com/repos/{repo}/releases/latest", token=token, timeout=12)
        remote = str(data.get("tag_name") or "").lstrip("v")
        def _vkey(v):
            try:
                return [int(x) for x in str(v).split(".")]
            except Exception:
                return [0]
        has_update = bool(remote) and _vkey(remote) > _vkey(local)
        return {"ok": True, "local": local, "remote": remote or "",
                "has_update": has_update,
                "release_url": data.get("html_url") or f"https://github.com/{repo}/releases",
                "notes": str(data.get("body") or "")[:400]}
    except Exception as e:
        return {"ok": False, "local": local, "remote": "", "has_update": False,
                "error": f"GitHub 访问失败（{type(e).__name__}）；当前版本 {local}"}


@app.post("/api/fix-consistency-all")
def api_fix_consistency_all(payload: dict):
    """全部一键修复：确定性修复批量执行；改稿类合并为一次定向重写（后台）。"""
    key = str(payload.get("key") or "")
    # v4.17.4：修复引用未定义的 _current_proj（缺 key 明确报错）
    proj = _get_project(key) if key else None
    if not proj:
        return JSONResponse({"error": "作品不存在或缺少 key"}, status_code=404)
    key = os.path.basename(proj.path)
    fp = os.path.join(proj.path, "22-规划体检.json")
    issues: list[str] = []
    handled: set = set()
    try:
        audit = json.load(open(fp, encoding="utf-8")) or {}
        issues = [str(x) for x in (audit.get("issues") or [])]
        handled = set(audit.get("handled") or [])
    except Exception:
        pass
    pending = [x for x in issues if x not in handled]
    if not pending:
        return {"ok": True, "key": key, "fixed_count": 0, "rewrite_started": False,
                "msg": "没有待修复的问题", "results": []}
    from bot import continuity as _cont
    results, rewrite_chapters, rewrite_dims = [], [], []
    fixed_count = 0
    for issue in pending:
        res = _cont.fix(proj.path, issue)   # 确定性修复 + 标记 handled
        if res.get("fixed"):
            fixed_count += 1
            results.append({"issue": issue, "fixed": True, "actions": res.get("actions") or []})
            continue
        m = _FIXABLE_REWRITE_RE.search(issue)
        plan = proj.plan() or {}
        no = int(m.group(1)) if m else max(
            [c.get("chapterNumber") or 0 for c in (plan.get("chapters") or [])
             if c.get("status") == "completed"] or [0])
        if no:
            rewrite_chapters.append(no)
            rewrite_dims.append(issue)
        results.append({"issue": issue, "fixed": False, "queued_rewrite": no})
    rewrite_started = False
    if rewrite_chapters:
        rewrite_started = _start_rewrite(key, chapters=sorted(set(rewrite_chapters)),
                                         dimensions=rewrite_dims, max_rounds=1)
    return {"ok": True, "key": key, "fixed_count": fixed_count,
            "rewrite_started": rewrite_started,
            "rewrite_chapters": sorted(set(rewrite_chapters)),
            "msg": (f"确定性修复 {fixed_count} 项" +
                    (f"；已启动 {len(set(rewrite_chapters))} 章 LLM 定向重写（后台）" if rewrite_chapters else "")),
            "results": results}


@app.post("/api/audit")
def api_audit(payload: dict):
    """确定性一致性体检：arc/volume/book 三档，结果落 22-规划体检.json 供状态 pane 复读。"""
    key = str(payload.get("key") or "")
    proj = _get_project(key)
    if not proj:
        # 内存中未命中时尝试从磁盘加载
        try:
            pdir = writer._project_dir(key)
            if os.path.isdir(pdir):
                PROJECT_INDEX[key] = writer.Project(pdir, key)
                proj = PROJECT_INDEX[key]
        except ValueError:
            pass
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    scope = str(payload.get("scope") or "book")
    from bot import longform as _lf
    if scope.startswith("arc:"):
        issues = _lf.audit_arc(proj, scope.split(":", 1)[1])
    elif scope.startswith("volume:"):
        issues = _lf.audit_volume(proj, scope.split(":", 1)[1])
    else:
        scope, issues = "book", _lf.audit_book(proj)
    at = time.strftime("%Y-%m-%dT%H:%M:%S")
    atomic_write_json(os.path.join(proj.path, "22-规划体检.json"),
                      {"at": at, "scope": scope, "issues": list(issues or [])[:50]})
    return {"ok": True, "at": at, "scope": scope, "issues": list(issues or [])[:50]}

@app.get("/api/job")
def api_job(key: str):
    """v4.33.1：当前作品的后台作业快照。

    点「继续」后前端立刻轮询这里：接口在起线程前就 `livejob.begin()`，所以
    200ms 内就能显示「谁在工作、做到哪一步、已经多久」，不再出现「点了没反应」。
    """
    row = livejob.get(key)
    if row and not row.get("finished") and float(row.get("elapsed") or 0) > 12:
        # 线程异常退出没走到 mark_pipeline 的兜底：项目确实不在跑就收口，别让前端永远转圈
        if not _medium_running(key):
            livejob.finish(key, ok=True, detail="本轮已结束")
            row = livejob.get(key)
    return {"ok": True, "key": key, "job": row}


_JOURNAL_STAGE_LABEL = {
    "planning": "规划中", "writing": "写作中", "expanding": "扩写中",
    "scoring": "评分中", "rewriting": "重写中", "deslopping": "去 AI 味",
    "verifying": "校验中", "reviewing": "全书终审中", "exporting": "导出中",
    "plan_blocked": "等补规划", "interrupted": "已中断",
}


def _journal_view(pdir: str) -> dict:
    """任务档案 → 前端状态条可直接显示的一句话（真实进度，不靠推断）。

    v4.38.0：切书回来时「现在到底在做什么」必须来自存档事实：
    journal 记录的是「哪个任务 / 第几章 / 哪个阶段 / 是否暂停」，
    前端状态条与 /api/book-live 共用这一份，避免各入口各说各话。
    """
    try:
        from bot import journal as _journal
        view = _journal.summary(pdir)
    except Exception:
        return {}
    act = view.get("active") or {}
    if not act:
        return {"journal": view}
    stage = str(act.get("stage") or "")
    stage_label = _JOURNAL_STAGE_LABEL.get(stage, stage)
    cur = act.get("current")
    paused = act.get("status") == "paused"
    label = act.get("label") or act.get("kind") or "创作"
    bits = [f"已暂停 · {label}" if paused else f"正在{label}"]
    if cur:
        bits.append(f"第 {cur} 章")
    if stage_label and stage_label not in label:
        bits.append(stage_label)
    if act.get("total"):
        bits.append(f"{act.get('done') or 0}/{act.get('total')}")
    activity = {
        "kind": act.get("kind"), "label": label, "title": act.get("title"),
        "stage": stage, "stage_label": stage_label,
        "current": cur, "done": act.get("done") or 0, "total": act.get("total"),
        "paused": paused, "status": act.get("status"),
        "text": " · ".join(bits),
    }
    return {"journal": view, "activity": activity}


@app.get("/api/book-live")
def api_book_live(key: str, request: Request):
    """v0.7 R4：对话区「本书在干什么」live 卡数据源（纯读、无 LLM、按书隔离）。
    聚合 阶段/当前章/进度/实时日志，前端 1.5s 轮询原地刷新（Hermes 式 live 工具步骤）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    st = proj.status()
    if st.get("state") in ("missing", "", "unknown"):
        # v4.13.0：非小说文体没有 02-写作计划.json，Project.status 误报 → 回退 library_item
        it = writer.library_item(key)
        if it:
            st["state"] = it.get("state") or "planning"
            st["name"] = it.get("name") or st.get("name")
            st["medium"] = it.get("medium")
            st["status_label"] = it.get("status_label")
            st["done"] = it.get("done", 0)
            st["total"] = it.get("total", 0)
            st["words"] = it.get("total_words", 0)
            st["running"] = bool(it.get("running"))
            # v4.24.12：非小说回退分支漏传 paused —— 「暂停中…」永远不出现，
            # 界面上就只剩「继续」，看不出线程其实还活着。
            st["paused"] = bool(it.get("paused"))
    plan = proj.plan() or {}
    chs = plan.get("chapters") or []
    inprog = [c.get("chapterNumber") for c in chs if c.get("status") == "in_progress"]
    # v4.38.0：当前章以任务档案为第一事实源——切书回来时计划里的 in_progress
    # 可能滞后一整轮，档案里的 current 才是「此刻正在写第几章」。
    jv = _journal_view(proj.path)
    act = jv.get("activity") or {}
    stream_info = steps_mod.stream_state(key, _q_int(request, "stream_offset"),
                                         _q_int(request, "stream_think_offset"))
    _act_ch = 0
    try:
        _act_ch = int(act.get("current") or 0)
    except (TypeError, ValueError):
        _act_ch = 0
    _stream_ch = int(stream_info.get("chapter") or 0) if stream_info.get("active") else 0
    cur_ch = (_act_ch or _stream_ch
              or (max(inprog) if inprog else min(st.get("done", 0) + 1, st.get("total", 0))))
    par = _PARALLEL_RUNNING.get(key, {}).get("running")
    rw = _REWRITING.get(key, {}).get("running")
    rel = _RELEASE_RUNNING.get(key, {}).get("running")
    osh = _ONESHOT_RUNNING.get(key, {}).get("running")
    state = st.get("state")
    if act.get("kind") == "review":
        phase = "reviewing"
    elif osh:
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
    _journal_running = bool(act.get("status") == "running")
    _running = bool(st.get("running") or par or rw or rel or osh or _journal_running
                    or livejob.is_active(key))
    _paused = bool(st.get("paused"))
    # v4.38.0：档案与按钮同源——档案已暂停就不再报 running，避免「已暂停」
    # 状态条和「创作中」按钮同时出现。
    if act.get("status") == "paused":
        _running, _paused = False, True
    elif act.get("status") == "running":
        _running, _paused = True, False
    return {
        "key": key, "name": st.get("name"), "phase": phase, "state": state,
        "current_chapter": cur_ch,
        # v4.38.0：活着的工作台状态（档案说真话）——前端状态条 + 「正在写第 N 章」
        "activity": act,
        "journal": jv.get("journal"),
        "stage": act.get("stage") or "",
        "progress": {"done": done, "total": total, "pct": round(done / total * 100, 1) if total else 0},
        "running": _running,
        "paused": _paused, "parallel": bool(par), "rewriting": bool(rw), "releasing": bool(rel),
        "live_log": live,
        "updated_at": int(time.time()),
        # v4.33.1：当前后台作业（谁在工作/做到哪一步）——前端据此渲染「正在工作」卡
        "job": livejob.get(key),
        # v4.21.0：创作活动流增量 + 当前流式步骤（对话内 Agent 逐条动作/思考/产出）
        # v4.24.16：stream_think_offset 透传——stream_state 早就设计了独立的思维链
        # 增量游标，但 HTTP 层从未接（B 支持的字段 A 不传），思维链每 1.5s 全量重发
        "steps": steps_mod.since(key, _q_int(request, "steps_cursor"), _q_int(request, "steps_limit")),
        "stream": stream_info,
    }


def _q_int(request: Request, name: str) -> int:
    try:
        return int(request.query_params.get(name) or 0)
    except (TypeError, ValueError):
        return 0


@app.post("/api/chat/note")
def api_chat_note(payload: dict):
    """v4.21.3：创作按钮（继续/暂停）的气泡永久入库——写入 chat_log.jsonl，
    刷新/重开后对话流原样回放。payload: {key, text, note}"""
    key = str(payload.get("key") or "")
    text = str(payload.get("text") or "").strip()[:60]
    note = str(payload.get("note") or "来自创作按钮").strip()[:30]
    if not key or not text:
        return JSONResponse({"error": "缺少 key 或 text"}, status_code=400)
    from bot.v2api import _save_chat_msg
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    _save_chat_msg(proj, "user", text, {"note": note})
    return {"ok": True}


@app.post("/api/express-toggle")
def api_express_toggle(payload: dict):
    """v4.22.2：已有书的极速开关（向导只在开书时能选，此端点让任何书随时切换）。

    v4.24.13：与「⚡ 极速续写」(POST /api/express-write) 合并成**同一个写入口**
    `express.set_plan_flag`。此前开关写 `config.express` + 顶层 `plan["express"]`，
    续写写 `config.express` + `config.mode`，两个入口各写一半，同一本书的字段会
    互相矛盾（顶层说开、config 说关），界面标签和实际行为也会分叉。

    **小说：运行中也能切，下一章生效。** 写作循环每章都重读 `config.express`
    （`_write_loop._live_express()`），所以切档只影响还没写的章：一章写到一半
    不会换档（换档会把同章的字数线与评分逻辑撕成两半）。开关是**动态**的——
    用户随时可以开启或关闭，不需要先暂停。
    非小说仍拒绝运行中切换：那 7 个引擎在启动时就把要跑的阶段算定了，
    中途改只会「标签变了、阶段没变」，宁可说清楚。

    非小说也支持：走各自引擎的 project（不能落到 `Project` 上——那会往
    剧本/诗词目录里写一份 02-写作计划.json）。
    """
    key = str(payload.get("key") or "")
    on = bool(payload.get("on"))
    running = False   # v4.24.15：回给前端，决定提示语是「立即」还是「下一章起」
    paused = False
    mod = _nonnovel_module(key)
    if mod is not None:
        try:
            proj = mod.get_project(key)
        except Exception:
            proj = None
        if not proj:
            return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
        plan = proj.plan() or {}
        if not plan:
            # 目录在、计划文件缺失（删过/损坏）：不能凭空 save 一份只有 express 的计划，
            # 那会把这本书的状态彻底弄脏，且引擎读不到任何创作字段。
            return JSONResponse({"error": "该作品的计划文件缺失，请先从作品库恢复"}, status_code=404)
        running = bool(writer.pipeline_running(key))
        if not running:
            try:
                st = proj.status() if callable(getattr(proj, "status", None)) else {}
                running = bool((st or {}).get("running"))
            except Exception:
                running = False
        if running:
            return {"ok": False, "express": express.plan_flag(plan), "running": True,
                    "msg": "该文体的写作按阶段一口气调度，运行中切换不会生效——先暂停再切。"}
        express.set_plan_flag(plan, on, sync_mode=False)
        try:
            proj.save_plan(plan)
        except Exception as e:
            return JSONResponse({"error": f"写入极速开关失败：{e}"}, status_code=500)
    else:
        proj = _get_project(key)
        if not proj:
            return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
        st = proj.status()
        # v4.24.15：小说**运行中也能切**。写作线程每章重读 config.express，
        # 切档只影响还没写的章；拦下来只会让用户在「正在写」时无法改速度。
        running = bool(st.get("running"))
        paused = bool(st.get("paused"))
        with proj.lock:
            plan = proj.plan() or {}
            if not plan:
                return JSONResponse({"error": "该作品的写作计划缺失，先从作品库恢复"}, status_code=404)
            express.set_plan_flag(plan, on)
            proj.save_plan(plan)

    writer._evt(key, "system", "done" if on else "warn",
                title="⚡ 极速模式" + ("开启" if on else "关闭"),
                output="后续章节不评分、不重写、字数验收降至60%" if on else "恢复正常验收标准")
    syslog_mod.log_event("system", "极速模式", {"key": key, "on": on},
                         f"{'开启' if on else '关闭'}极速模式（{key[:24]}）")
    _tail = "（当前这一章写完即生效，不会打断）" if running else ""
    return {"ok": True, "express": on, "running": running, "paused": paused,
            "msg": ("⚡ 极速模式已开启：后续章节不评分、不重写、字数验收降至 60%" + _tail if on
                    else "已关闭极速模式：后续章节恢复评分 + 90% 字数线" + _tail)}


@app.get("/api/consistency-audit")
def api_consistency_audit(key: str):
    """v4.24.4：读取最新一致性巡检报告（22-巡检报告.json）。"""
    return {"ok": True, "report": audit_mod.read_report(key)}


@app.post("/api/consistency-audit/run")
def api_consistency_audit_run(payload: dict):
    """v4.24.4：手动触发一致性巡检。
    v4.24.16 修复：docstring 写着「异步线程」实际是同步执行——run_audit 内含分钟级
    LLM 仲裁（审稿人岗 chat_json），请求线程会挂住数分钟占用 uvicorn worker。
    对齐 chat 意图路径：防重入检查 + 后台线程，接口立即返回。"""
    key = str((payload or {}).get("key") or "")
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    if audit_mod._RUNNING.get(key):
        return {"ok": True, "running": True, "msg": "巡检已在进行中，请稍候"}

    def _job():
        try:
            audit_mod.run_audit(key, "manual")
        except Exception as e:
            try:
                proj.log(f"⚠️ 巡检异常：{e}")
            except Exception:
                pass

    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "running": True, "msg": "已启动一致性巡检：完成后活动流会汇报，报告可在右栏查看。"}


@app.get("/api/chapter-tokens")
def api_chapter_tokens(key: str, no: int):
    """v4.22.1：本章 token 消耗明细（按阶段细分：正文/评分/去味/重写等）。
    数据源 usage.json by_chapter[no]（llm.set_chapter 归账）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    try:
        u = llm.read_usage(key)
    except Exception:
        u = {}
    cb = ((u.get("by_chapter") or {}).get(str(no)) or {})
    return {"ok": True, "no": no, "total": int(cb.get("total") or 0),
            "calls": int(cb.get("calls") or 0), "by_stage": cb.get("by_stage") or {}}


@app.get("/api/chapter-steps")
def api_chapter_steps(key: str, chapter: int = 0, limit: int = 400):
    """v4.21.0：本章创作档案——该章的全部创作事件（岗位/思考/产出，新→旧）。
    内存 ring + 历史 JSONL 去重合并，跨重启可查。"""
    if _get_project(key) is None:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return {"ok": True, "items": steps_mod.chapter_events(key, chapter, limit=limit)}

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
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
    # v2.3.0：段级改写也吃连续性上下文（人物/世界观/设定/线索），改一段不许跑偏
    cons = "全书约束：保持人物/设定一致，只改这一段"
    try:
        ctx = writer._build_ctx(proj, ch)
        cons += (f"\n人物：{(ctx.get('characters_text') or '')[:1200]}"
                 f"\n世界观/故事圣经：{(ctx.get('story_bible') or '')[:800]}"
                 f"\n设定词典（本章相关）：{(ctx.get('lore_relevant') or '')[:800]}"
                 f"\n线索账本（须兑现/不得乱揭）：{(ctx.get('clue_ledger') or '')[:600]}")
    except Exception:
        pass
    new = workflow.guided_rewrite(target, instr, cons, cfg)
    new = (new or "").strip()
    if new:
        versions.snapshot(proj.path, ch_no, body, label="局部改写前")
        atomic_write_text(fp, body.replace(target, new, 1))
        proj.log(f"✒️ 第{ch_no}章改写一段（{instr[:24]}…）")
    return {"ok": True, "chapter": ch_no, "orig": target, "new": new}

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
        # "卷" 已移除——由第二步「规模」决定，避免两处设置冲突（v4.12.9）
        # 默认技能（题材 → 推荐风格组合，可覆盖）
        "preset": {
            "都市": {"金手指": "马甲扮猪吃虎", "基调": "爽", "节奏": "快节奏(3章1转折)", "语言": "口语爽利"},
            "重生": {"金手指": "重生预知", "基调": "爽", "节奏": "快节奏(3章1转折)", "语言": "口语爽利"},
            "系统流": {"金手指": "系统面板", "基调": "燃", "节奏": "中节奏", "语言": "轻松幽默"},
            "玄幻": {"金手指": "悟性/顿悟", "基调": "热血", "节奏": "中节奏", "语言": "硬核细节"},
            "穿越": {"金手指": "重生预知", "基调": "轻松沙雕", "节奏": "慢热(前3章铺垫)", "语言": "文白夹杂"},
            "悬疑/刑侦": {"金手指": "无(纯剧情)", "基调": "暗黑复仇", "节奏": "中节奏", "语言": "硬核细节"},
            "古言/宫斗": {"金手指": "马甲扮猪吃虎", "基调": "爽", "节奏": "中节奏", "语言": "文白夹杂"},
            "快穿": {"金手指": "系统面板", "基调": "爽", "节奏": "快节奏(3章1转折)", "语言": "轻松幽默"},
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
    plat = platforms.payload()
    _ONB["platforms"] = plat["platforms"]
    _ONB["platforms_disclaimer"] = plat["disclaimer"]
    _ONB["by_medium"] = medium_mod.wizard_payload({
        "genres": _ONB["genres"], "tones": _ONB["tones"], "perspectives": _ONB["perspectives"],
        "sells": _ONB["sells"], "style_presets": list((_ONB["style"].get("preset") or {})),
        "platforms": [{"value": p["key"], "label": p["label"], "desc": p.get("note") or ""}
                      for p in plat["platforms"]],
        "script_forms": [{"value": k, "label": v["label"], "desc": f"目标 {v['target_pages']} {v['unit']}"}
                         for k, v in script_writer.FORMS.items() if k != "custom"],
        "script_entry_paths": list(script_writer.ENTRY_PATHS.items()),
        "poetry_forms": list(poetry_writer.FORMS.items()),
        "prose_genres": list(prose_writer.GENRES.items()),
        "prose_operations": list(prose_writer.OPERATIONS.items()),
    })
    # v4.15.1：注入笔名选项（所有文体向导的 penname 字段用）
    _pen = _load_pennames()
    _ONB["pennames"] = _pen   # v4.15.6：向导按文体默认选中笔名用（by_medium）
    _pen_opts = [{"value": c.get("name", ""), "label": c.get("name", "")}
                 for c in _pen.get("list", [])]
    for med_sections in _ONB["by_medium"].values():
        if med_sections.get("basic"):
            for f in med_sections["basic"]:
                if f.get("key") == "penname":
                    f["options"] = _pen_opts
    # v4.13.0：注入每个文体的专属 Agent 词表（标签从 agents.AGENTS 取）。
    # v4.15.2 修复：原按 basic[0] 下标注入，笔名字段插入后踩空 → 改按 key 查找。
    from bot import agents as _ag
    for med, agent_names in medium_mod.MEDIUM_AGENTS_LABELS.items():
        _opts = [{"value": n, "label": _ag.AGENTS.get(n, {}).get("label", n)}
                 for n in agent_names]
        for _f in _ONB["by_medium"][med]["basic"]:
            if _f.get("key") == "agents":
                _f["options"] = _opts
                break
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
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
        if not plan:
            # v4.7.1 修复：定时记录指向的书若「计划文件缺失或已损坏」（plan() 返回 None），
            # 原先直接 plan.get("status") 抛 AttributeError，被 _scheduler_loop 的
            # `except Exception: pass` 静默吞掉 —— 一条坏记录会拖死整轮调度，
            # 其它健康的定时书从此永不触发，且日志里看不到任何异常。
            s["last_note"] = "作品计划缺失或已损坏，跳过本次"; changed = True
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
        except Exception as e:
            # v4.7.1：原先是 `except Exception: pass`，调度器一旦异常停摆，日志里
            # 看不到任何痕迹，用户只表现为「定时写作怎么不跑了」。现在留一行可排查的告警。
            _trace("scheduler-error", err=repr(e)[:200])
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
            return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
            "call_gap": d.get("call_gap", 2), "max_global_agents": d.get("max_global_agents", 4),
            "chat_context_limit": chatsum.load_limit(),
            "chat_auto_compress": chatsum.compress_enabled(),
            "supervisor_autonomy": str(d.get("supervisor_autonomy") or "guarded"),
            "score_provider_id": d.get("score_provider_id", ""),
            "score_model": d.get("score_model", "")}


@app.post("/api/settings")
def api_settings_save(payload: dict):
    """收口关键开关：审稿强度、token 预算、Lorebook 检索、调用日志、对话压缩。"""
    cf = os.path.join(ROOT, "config.json")
    before = api_settings()   # v4.19.0：留存旧值，保存后对比差异写系统日志
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
    if "chat_context_limit" in payload:
        v = str(payload.get("chat_context_limit") or "").lower()
        if v not in chatsum.CONTEXT_LIMITS:
            return JSONResponse({"error": "chat_context_limit 只能是 200k/400k/500k/1m"}, status_code=400)
        d["chat_context_limit"] = v
    if "chat_auto_compress" in payload:
        d["chat_auto_compress"] = bool(payload.get("chat_auto_compress"))
    # v4.26.0：总编辑自动化程度（guarded=低自动：结构级重规划找用户确认；auto=全自动）
    if "supervisor_autonomy" in payload:
        v = str(payload.get("supervisor_autonomy") or "guarded").strip().lower()
        if v not in ("guarded", "auto"):
            return JSONResponse({"error": "supervisor_autonomy 只能是 guarded/auto"}, status_code=400)
        d["supervisor_autonomy"] = v
    # v4.18.0：评分专用模型（空 = 跟随创作模型）
    if "score_provider_id" in payload:
        d["score_provider_id"] = str(payload.get("score_provider_id") or "").strip()[:60]
    if "score_model" in payload:
        d["score_model"] = str(payload.get("score_model") or "").strip()[:80]
    atomic_write_json(cf, cfg)
    after = api_settings()
    changed = {k: {"旧值": before.get(k), "新值": after.get(k)}
               for k in payload if k in before and before.get(k) != after.get(k)}
    if changed:
        syslog_mod.log_event("settings", "修改设置", changed,
                             "修改设置：" + "、".join(changed.keys()))
    return api_settings()


# v4.15.1：作者笔名管理（全局默认 + 多笔名 + 按文体默认笔名）
_PENNAME_CFG_KEY = "pennames"


def _load_pennames() -> dict:
    cf = os.path.join(ROOT, "config.json")
    try:
        cfg = json.load(open(cf, encoding="utf-8")) if os.path.exists(cf) else {}
    except Exception:
        cfg = {}
    d = cfg.setdefault(_PENNAME_CFG_KEY, {})
    return {
        "list": d.get("list") or [],
        "default": d.get("default") or "",
        "by_medium": d.get("by_medium") or {},
    }


def _save_pennames(pennames: dict) -> dict:
    cf = os.path.join(ROOT, "config.json")
    try:
        cfg = json.load(open(cf, encoding="utf-8")) if os.path.exists(cf) else {}
    except Exception:
        cfg = {}
    cfg.setdefault(_PENNAME_CFG_KEY, {}).update(pennames or {})
    atomic_write_json(cf, cfg)
    syslog_mod.log_event("penname", "保存笔名",
                         {"count": len(pennames.get("list") or []), "default": pennames.get("default") or ""},
                         f"保存笔名配置（{len(pennames.get('list') or [])} 个笔名）")
    return _load_pennames()


@app.post("/api/pennames/save")
def api_pennames_save(payload: dict):
    """保存笔名配置：{list:[{name,desc,medium_default?}], default: name}"""
    list_raw = payload.get("list")
    if not isinstance(list_raw, list):
        return JSONResponse({"error": "list 必须是数组"}, status_code=400)
    cleaned = []
    for item in list_raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:40]
        if not name:
            continue
        cleaned.append({"name": name, "desc": str(item.get("desc") or "").strip()[:200],
                        "medium_default": str(item.get("medium_default") or "")})
    default = str(payload.get("default") or "").strip()[:40]
    # 默认笔名必须在列表内（或为空）
    if default and not any(c["name"] == default for c in cleaned):
        default = ""
    # v4.15.6：按文体默认笔名（{medium: name}，name 必须在列表内或为空）
    by_medium_raw = payload.get("by_medium")
    by_medium = {}
    if isinstance(by_medium_raw, dict):
        from bot import medium as _med_mod
        for med, name in by_medium_raw.items():
            med = str(med).strip()
            name = str(name or "").strip()[:40]
            if med in _med_mod.MEDIUMS and (not name or any(c["name"] == name for c in cleaned)):
                by_medium[med] = name
    saved = _save_pennames({"list": cleaned, "default": default, "by_medium": by_medium})
    return {"ok": True, **saved}


@app.get("/api/pennames")
def api_pennames():
    """获取笔名配置：{list:[{name,desc,medium_default?}], default: name}"""
    return _load_pennames()


@app.post("/api/pennames/delete")
def api_pennames_delete(payload: dict):
    name = str(payload.get("name") or "").strip()
    saved = _load_pennames()
    saved["list"] = [c for c in saved["list"] if c.get("name") != name]
    if saved.get("default") == name:
        saved["default"] = saved["list"][0]["name"] if saved["list"] else ""
    _save_pennames(saved)
    syslog_mod.log_event("penname", "删除笔名", {"name": name}, f"删除笔名「{name}」")
    return {"ok": True, **saved}

@app.get("/api/titles")
def api_titles(key: str):
    """只读返回书名候选；候选项由 /api/create 的后台规划生成，避免轮询时反复触发 LLM。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
    # v5.5：换 key 必须换会话——所有绑着旧 key 的会话立即重绑到新 key，
    # 否则用户选完书名还在旧会话里说话（writer.pick_title 只改磁盘不动会话）。
    if newbase and newbase != key:
        for _sess in SESSIONS.values():
            if _sess.get("project") == key:
                _sess["project"] = newbase
    return {"ok": True, "key": newbase, "title": title}

_RELEASE_RUNNING = {}
_RELEASE_LOCK = threading.Lock()  # v4.7.1：放行门禁去重锁（见 api_release 的 TOCTOU 修复）

@app.post("/api/release")
def api_release(payload: dict):
    key = payload.get("key")
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    # v4.7.1 修复 TOCTOU：原先「检查是否已在跑」和「置位 running」之间隔着 jobs.run()
    # （它立刻起后台线程），实测 8 个并发点击会全部通过检查、真的执行 8 次放行门禁
    # ——重复烧 token，且多个线程并发改写同一批章节文件。
    # 现在检查与置位在同一个锁临界区内完成，且置位先于任务启动。
    with _RELEASE_LOCK:
        if (_RELEASE_RUNNING.get(key) or {}).get("running"):
            return {"ok": True, "running": True, "msg": "放行门禁进行中…"}
        _RELEASE_RUNNING[key] = {"running": True, "started_at": int(time.time()), "job_id": None}

    def _run_gate():
        prev_started = (_RELEASE_RUNNING.get(key) or {}).get("started_at")
        try:
            res = writer.run_release_gate(key)
        except Exception as e:
            # v4.7.1：门禁本身抛异常时也必须解除标记，否则该书永久卡在「进行中」，
            # 之后每次点击都会被上面的检查短路，用户只能重启服务。
            with _RELEASE_LOCK:
                _RELEASE_RUNNING[key] = {"running": False, "started_at": prev_started,
                                         "result": {"error": str(e)[:300]}}
            raise
        with _RELEASE_LOCK:
            _RELEASE_RUNNING[key] = {"running": False, "started_at": prev_started, "result": res}
        return res

    try:
        job = jobs.run("release_gate", _run_gate, key=key, title="放行门禁")
    except Exception as e:
        with _RELEASE_LOCK:
            _RELEASE_RUNNING.pop(key, None)
        return JSONResponse({"error": f"放行任务启动失败：{e}"}, status_code=500)
    with _RELEASE_LOCK:
        if _RELEASE_RUNNING.get(key):
            _RELEASE_RUNNING[key]["job_id"] = job["id"]
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

# ---------- v0.4：暂停 / 继续 / 读者即时指令（像 Agent 聊天一样随时控）----------
def _nonnovel_set_paused(key, paused: bool) -> bool:
    """v4.20.0：非小说文体通用暂停位——直接写 plan.meta.paused，
    各引擎在步骤/章间检查该位后自停（剧本引擎原生支持，其余本次补齐）。
    v4.32.3：改为按 _nonnovel_module(key) 精确定位引擎。旧实现遍历 8 个引擎，
    而各引擎的 get_project() 大多只判断「目录存在」，第一个 script_writer 就会
    命中任意作品，并在小说目录里凭空写出一份只有 paused 的「剧本计划.json」。"""
    mod = _nonnovel_module(key)
    if mod is None:
        return False
    gp = getattr(mod, "get_project", None)
    if not callable(gp):
        return False
    try:
        proj = gp(key)
    except Exception:
        return False
    if proj is None:
        return False
    try:
        pl = proj.plan() or {}
    except Exception:
        return False
    if not pl:
        return False
    pl.setdefault("meta", {})["paused"] = bool(paused)
    if paused:
        if pl.get("status") in ("in_progress", "writing", "planning"):
            pl["status"] = "paused"
    elif pl.get("status") == "paused":
        pl["status"] = "in_progress"
    try:
        proj.save_plan(pl)
        return True
    except Exception:
        return False


# 兼容旧调用点；v4.32.3 起一律走 _nonnovel_module()（按内容判文体，不只认前缀）。
_NONNOVEL_PREFIXES = ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_")


def _is_nonnovel(key) -> bool:
    return _nonnovel_module(key) is not None


def _is_adapted_key(key) -> bool:
    """是不是「由小说全书改编」的短剧（有 adaptedFrom）。

    这类作品只能走 drama_adapter 的集级引擎：按场批量写会把整集稿写成
    场次骨架（用户实测第 21 集 6410 字被打成 483 字）。
    """
    try:
        sp = script_writer.get_project(key)
        if sp is None:
            return False
        return script_writer.is_adapted_drama(sp.plan() or {})
    except Exception:
        return False


# ---------------------------------------------------------------- v4.33.1 即时反馈
# 用户实拍：点「继续」后界面一动不动，30 秒后才开写，中途像卡死。根因是「点击
# 到第一个字」之间没有任何可观测事实。这里给所有文体一套统一的作业登记：
# 接口在起线程前先登记并写一条项目日志，前端据此立刻显示「谁在工作、做了什么」。

_JOB_WORKER = {
    "novel": ("writer", "小说引擎"),
    "script": ("script_writer", "剧本引擎"),
    "poetry": ("poetry_writer", "诗词引擎"),
    "prose": ("prose_writer", "散文引擎"),
    "thesis": ("thesis_writer", "论文引擎"),
    "children": ("children_writer", "儿童文学引擎"),
    "news": ("news_writer", "新闻稿引擎"),
    "copy": ("copy_writer", "文案引擎"),
    "other": ("other_writer", "创作引擎"),
}


def _job_worker(key) -> tuple[str, str]:
    """(worker_id, 中文名)：让前端能具体说「剧本引擎正在写第 3/50 集」。"""
    try:
        med = writer.medium_of_key(key)
    except Exception:
        med = ""
    return _JOB_WORKER.get(med, ("engine", "创作引擎"))


def _job_project(key):
    """按文体取项目对象（小说 Project 或各文体 Project）；用于写启动日志。"""
    try:
        med = writer.medium_of_key(key)
    except Exception:
        med = ""
    if med in ("", "novel"):
        p = _get_project(key)
        if p is not None:
            return p
    mod = _nonnovel_module(key)
    gp = getattr(mod, "get_project", None) if mod is not None else None
    if callable(gp):
        try:
            return gp(key)
        except Exception:
            return None
    return None


def _job_log(key, msg: str) -> None:
    """把「已收到指令」写进项目运行日志——前端日志带因此立刻有变化，不是死界面。"""
    p = _job_project(key)
    log = getattr(p, "log", None) if p is not None else None
    if callable(log):
        try:
            log(msg)
        except Exception:
            pass


def _job_start(key, *, kind: str, detail: str, worker: str = "",
               worker_label: str = "", total: int = 0, done: int = 0) -> dict:
    """登记作业 + 立即写日志。必须在起线程**之前**调用，保证接口返回即有事实。"""
    wid, wlabel = _job_worker(key)
    row = livejob.begin(key, kind=kind, worker=worker or wid,
                        worker_label=worker_label or wlabel,
                        detail=detail, total=total, done=done)
    _job_log(key, f"▶ {detail}")
    return row


def _job_fail(key, detail: str) -> dict:
    """失败也要留一条带结论的登记——前端据此说「没有可继续的任务」而不是静默。"""
    wid, wlabel = _job_worker(key)
    livejob.begin(key, kind="resume", worker=wid, worker_label=wlabel, detail=detail)
    return livejob.finish(key, ok=False, detail=detail)


def _job_done(key, detail: str) -> dict:
    """「没有活要干」也是一种结论：显式告诉用户，而不是让按钮空转。"""
    wid, wlabel = _job_worker(key)
    livejob.begin(key, kind="resume", worker=wid, worker_label=wlabel, detail=detail)
    return livejob.finish(key, ok=True, detail=detail)


def _job_payload(key, payload, *, started=None):
    """给业务返回体补上统一字段：started / job（前端一套 UI 适配所有文体）。"""
    if isinstance(payload, dict):
        payload.setdefault("key", key)
        row = livejob.get(key)
        if row:
            payload.setdefault("job", row)
        if started is not None:
            payload.setdefault("started", bool(started))
    return payload


def _medium_running(key) -> bool:
    """该项目此刻是否真的在跑（作业自愈用：线程异常退出时收口登记）。"""
    try:
        if writer.pipeline_running(key):
            return True
        med = writer.medium_of_key(key)
        if med in ("", "novel"):
            p = _get_project(key)
            return bool(p and p.status().get("running"))
        mod = _nonnovel_module(key)
        gp = getattr(mod, "get_project", None) if mod is not None else None
        if not callable(gp):
            return False
        p = gp(key)
        if p is None:
            return False
        st = p.status() or {}
        return bool(st.get("running"))
    except Exception:
        return False


@app.post("/api/pause")
def api_pause(payload: dict):
    key = payload.get("key")
    if _is_nonnovel(key):
        if not _nonnovel_set_paused(key, True):
            return JSONResponse({"error": "项目不存在或该文体不支持暂停"}, status_code=404)
        livejob.update(key, phase="pausing", detail="暂停请求已发出，当前步骤做完即停…")
        return _job_payload(key, {"ok": True, "msg": "⏸ 暂停请求已发出：当前步骤做完即停",
                "running": True, "paused": True, "state": "in_progress"})
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    ok = writer.pause_book(key)
    st = proj.status()
    if ok:
        livejob.update(key, phase="pausing", detail="暂停请求已发出，当前章节写完即停…")
    return _job_payload(key, {"ok": ok,
            "msg": "⏸ 已暂停（下一检查点停，停在当前进度）" if ok else "无法暂停",
            "running": st.get("running"), "paused": st.get("paused"), "state": st.get("state")})

@app.post("/api/resume")
def api_resume(payload: dict):
    key = payload.get("key")
    if _is_nonnovel(key):
        # v4.32.3：改编生成的剧本 key 是「<小说key>-短剧」，没有 s_ 前缀。
        # 旧版只认前缀 → 落到 writer.resume_task() 找小说任务档案 →
        # 「无法继续，没有未完成的任务」。现在按计划文件认文体，走剧本引擎断点恢复。
        _nonnovel_set_paused(key, False)
        return api_continue({"key": key})
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    # v4.22.2：「继续」按任务档案恢复上次那件事（写作/重写/补评分），而不是一律当写作
    try:
        r = writer.resume_task(key)
    except Exception as e:
        r = {"ok": False, "text": f"恢复失败：{e}"}
    st = proj.status()
    ok = bool(r.get("ok"))
    if ok:
        # v4.33.1：接口返回前就登记「谁在工作」并写项目日志——点下去界面立刻有反馈
        detail = f"{r.get('text') or '正在恢复创作'}…"
        _job_start(key, kind=str(r.get("kind") or "resume"), detail=detail,
                   total=int(st.get("total") or 0), done=int(st.get("done") or 0))
    else:
        _job_fail(key, f"无法继续：{r.get('text') or '无可用断点'}")
    return _job_payload(key, {"ok": ok,
            "msg": f"▶ {r.get('text')}…" if ok else f"无法继续（{r.get('text') or '无可用断点'}）",
            "kind": r.get("kind") or "",
            "running": st.get("running"), "paused": st.get("paused"), "state": st.get("state")},
            started=ok)

# ---------- v5.8.0：人机共同创作模式（逐章确认）——设置页「高级 → 创作模式」数据源 ----------

@app.get("/api/collab-review")
def api_collab_review_get(key: str):
    """读本书逐章确认开关与待确认位（纯读、零 LLM）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    plan = proj.plan() or {}
    meta = plan.get("meta") or {}
    return {"ok": True, "key": key,
            "enabled": bool(meta.get("collab_review")),
            "paused": bool(meta.get("paused")),
            "awaiting": writer.collab_awaiting_key(key),
            "state": plan.get("status") or "",
            "done": len([c for c in (plan.get("chapters") or [])
                         if c.get("status") == "completed"]),
            "total": len(plan.get("chapters") or [])}


@app.post("/api/collab-review")
def api_collab_review_set(payload: dict):
    """切换本书逐章确认（collab_review）。只落 plan.meta，不动运行线程——
    与极速开关同款动态语义：写作循环每章检查点实时读盘，下一章即生效。"""
    key = str(payload.get("key") or "")
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    enabled = bool(payload.get("enabled"))
    plan = proj.plan() or {}
    if not plan:
        return JSONResponse({"error": "该书还没有写作计划（等规划完成后再设置创作模式）"},
                            status_code=400)
    plan.setdefault("meta", {})["collab_review"] = enabled
    proj.save_plan(plan)
    proj.log("🤝 协作模式已开启：每章写完暂停等作者确认（说「继续」采用并写下一章）"
             if enabled else "⚡ 协作模式已关闭：回到自动连续写作，不再逐章停下")
    _awaiting = writer.collab_awaiting_key(key)
    _resumed = False
    if not enabled and _awaiting:
        st = proj.status()
        # 与总编辑 collab_mode(enabled=false) 同款语义：关闭 = 不想再停，
        # 正停着的待确认章视为放行，直接续写余下章节。
        if not st.get("running") and writer.resume_book(key):
            _awaiting = 0
            _resumed = True
    return {"ok": True, "key": key, "enabled": enabled, "awaiting": _awaiting,
            "resumed": _resumed,
            "msg": ("已关闭逐章确认：正停着的待确认章视为已确认，继续写作" if _resumed else
                    "已开启逐章确认：每章写完会停下等你把关" if enabled else
                    "已关闭逐章确认：回到自动连续写作")}


@app.post("/api/queue")
def api_queue(payload: dict):
    key = payload.get("key")
    text = (payload.get("text") or "").strip()
    immediate = bool(payload.get("immediate"))
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
        return JSONResponse({"ok": False, "error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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


@app.get("/api/feedback")
def api_feedback_list_compat(key: str = "", medium: str = "", stage: str = "", limit: int = 100):
    """v4.24.16 修复路由遮蔽：app.py 与 bot/v2api.py 各注册了一个 GET /api/feedback——
    FastAPI 先注册者赢，v2api 的列表版（items/summary/focus，evolution.js 的
    feedbackList 消费）永远不可达，前端只能拿到这里的 stats 形状靠容错降级。
    现在这里直接转发给 feedback 模块的完整实现；stats 形状保留在 v.notes 里向后兼容。"""
    from bot import feedback as _fb
    try:
        items = _fb.list_feedback(key, medium, stage, min(max(int(limit), 1), 500))
        summary = _fb.aggregate()
        return {"ok": True, "items": items, "summary": summary,
                "total": summary.get("total", 0), "up": summary.get("up", 0),
                "down": summary.get("down", 0), "issue": summary.get("issue", 0),
                "notes": items}
    except Exception:
        fb_path = os.path.join(writer.PROJECTS_DIR, "_feedback.json")
        if os.path.exists(fb_path):
            try:
                return json.load(open(fb_path, encoding="utf-8"))
            except Exception:
                pass
        return {"total": 0, "up": 0, "down": 0, "issue": 0, "notes": []}

@app.get("/api/status")
def api_status(key: str):
    _req_key = key
    # v5.5：改名别名跳转——选书名后旧 key 的请求解析到 canonical key，
    # 并随返回体带回 followed_from。注释里写了多年的「前端旧请求自动跳转」
    # 此前只做了一半（_get_project 兜底），前端从未接到跳转信号；
    # 现在这两个字段（key=canonical + followed_from=请求 key）就是跳转依据。
    try:
        _rk = writer.resolve_key(_req_key or "")
    except Exception:
        _rk = _req_key
    if _rk and _rk != _req_key:
        try:
            if os.path.isdir(writer._project_dir(_rk)):
                key = _rk
        except Exception:
            pass
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    # v3.1.0：validating/releasing 且线程已死 → 立即恢复，前端不再永远转圈
    try:
        writer.recover_stale_book(key, live_proj=proj)
    except Exception:
        pass
    st = proj.status()
    # v4.24.12：status 必须自带 key —— 前端 refreshWork 用它和作品库条目合并成 work。
    # 以前 key 只来自 library 条目，于是「刷新页面」时 /api/status 比 /api/library 先回
    # （单本快、聚合慢），work 就缺 key → 创作状态按钮（继续/暂停）整颗不渲染。
    st["key"] = key
    # v5.5：旧 key 请求命中别名时把跳转信息一并带回（canonical key 已在上面）。
    if key != _req_key:
        st["followed_from"] = _req_key
    if st.get("state") in ("missing", "", None):
        # 剧本/诗词/散文没有 02-写作计划.json，Project.status 会误报 missing；
        # 工作台 refreshWork 对当前作品调的就是本接口，必须回退到全文体元数据，
        # 否则打开非小说文体直接变死胡同。
        it = writer.library_item(key)
        if it:
            st["state"] = it.get("state") or "planning"
            st["name"] = it.get("name") or st.get("name")
            st["medium"] = it.get("medium")
            st["status_label"] = it.get("status_label")
            st["done"] = it.get("done", 0)
            st["total"] = it.get("total", 0)
            st["words"] = it.get("total_words", 0)
            st["running"] = bool(it.get("running"))
            st["paused"] = bool(it.get("paused"))
    # v4.33.1：作业登记在跑 = 创作中。有些引擎（尤其改编）不是通过 Session 的
    # proj.running 启动的，只信 status 会让按钮在真正写作时仍显示「继续」，
    # 用户以为没反应就反复点，反而起多条线程。
    # v4.34.0：不再把账面 state 强改成 in_progress——running（此刻有活）与
    # state（账面进度）是两回事；completed 的书即便撞上泄漏的 livejob 行
    # （已被活性超时兜底）也不该被改写成「创作中」。
    if livejob.is_active(key):
        st["running"] = True
    st["events"] = proj.events[-15:]
    st["path"] = proj.path
    st["has_book"] = os.path.exists(os.path.join(proj.path, f"成书-{st.get('name', key)}.md"))
    # v4.38.0：把任务档案的「此刻在做什么」随 status 一起下发——切书回来第一帧
    # 就有真实的「正在写第 N 章 / 全书终审中 / 已暂停」，不用等 book-live 轮询。
    _jv = _journal_view(proj.path)
    if _jv:
        st.update(_jv)
        _act = _jv.get("activity") or {}
        # v4.38.0：任务档案是「此刻」的最终事实——档案已暂停时，不再让陈旧的
        # running 位/livejob 把界面顶成「创作中」（文案与按钮必须同源）。
        if _act.get("status") == "paused":
            st["running"] = False
            st["paused"] = True
        elif _act.get("status") == "running":
            st["running"] = True
            st["paused"] = False
        if _act.get("current"):
            try:
                st["current_chapter"] = int(_act["current"])
            except (TypeError, ValueError):
                pass
    return st

# ---------- v4.24.12：未完成章节直接用极速模式续写 ----------

#: key 前缀 → 非小说引擎模块名。api_continue / api_express_write 共用这一份，
#: 免得加文体时漏改一处（漏改的后果是该文体的入口静默走到小说分支，行为完全不对）。
_NONNOVEL_MODS = {
    "s_": "script_writer", "p_": "poetry_writer", "e_": "prose_writer",
    "t_": "thesis_writer", "c_": "children_writer",
    "n_": "news_writer", "k_": "copy_writer", "o_": "other_writer",
}


_MODULES_BY_NAME = {
    "script_writer": script_writer, "poetry_writer": poetry_writer,
    "prose_writer": prose_writer, "thesis_writer": thesis_writer,
    "children_writer": children_writer, "news_writer": news_writer,
    "copy_writer": copy_writer, "other_writer": other_writer,
}
_MEDIUM_TO_MODULE = {"script": "script_writer", "poetry": "poetry_writer",
                     "prose": "prose_writer", "thesis": "thesis_writer",
                     "children": "children_writer", "news": "news_writer",
                     "copy": "copy_writer", "other": "other_writer"}


def _nonnovel_module(key):
    """取非小说引擎模块；小说 / 不存在的 key 返回 None。

    v4.32.3：先按 key 前缀（新建作品的命名约定），前缀不认时再按磁盘计划文件
    判定文体。小说转短剧生成的 key「<小说key>-短剧」没有 s_ 前缀，只认前缀会把
    它当小说，导致「继续」报「没有未完成的任务」、暂停/极速开关写错计划文件。
    """
    name = _NONNOVEL_MODS.get(str(key)[:2])
    if not name:
        med = writer.medium_of_key(key)
        if med and med != "novel":
            name = _MEDIUM_TO_MODULE.get(med)
    return _MODULES_BY_NAME.get(name) if name else None


@app.post("/api/express-write")
def api_express_write(payload: dict):
    """v4.24.12：未完成章节直接用极速模式续写。

    需求原文：「在极速模式和普通模式下还未完成的章节也可以直接采用极速模式进行创作」。

    **与「极速开关」的关系（v4.24.13 明确）**：两者共用同一个 `config.express`，
    不是两套并行逻辑。区别只有动作范围：
    - 极速开关（/api/express-toggle）：只改速度，不开写；
    - 极速续写（本接口）：改速度 **并** 立刻开写。
    因此「极速续写」会把这本书切成极速模式，写完后保持极速，直到用户点
    「切回标准」或关掉开关。前端菜单文案已按这个语义写明，不再暗示「只对这一批生效」。

    mode="express"（默认）：切极速 + 立即开写剩余章节（一口气写完）。
    mode="standard"：只切回标准，**不启动写作**。

    开关落在 plan.config.express（与 bot/express.on 同一口径），由
    express.set_plan_flag 单点写入；小说另有 config.mode（创作模式卡片）同步。
    """
    key = str(payload.get("key") or "").strip()
    mode = str(payload.get("mode") or "express").strip().lower()
    if not key:
        return JSONResponse({"error": "缺少作品 key"}, status_code=400)
    if mode not in ("express", "standard"):
        return JSONResponse({"error": "mode 只支持 express / standard"}, status_code=400)

    # ---- 非小说文体：走各自引擎的断点恢复，开关写进 plan.config（引擎自己读它）----
    mod = _nonnovel_module(key)
    if mod is not None:
        try:
            proj = mod.get_project(key)
        except Exception:
            proj = None
        if not proj:
            return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
        try:
            plan = proj.plan() or {}
        except Exception:
            plan = {}
        if not plan:
            return JSONResponse({"error": "该作品的计划文件缺失，请先从作品库恢复"}, status_code=404)
        # v4.24.13：运行中拒绝切速度（与 /api/express-toggle 同一口径）。
        # 引擎在启动时就算好了要跑哪些阶段，中途改开关只会让界面与实际分叉。
        if writer.pipeline_running(key):
            return {"ok": False, "express": express.plan_flag(plan), "running": True,
                    "msg": "正在创作中，先暂停再切换速度。"}
        # v4.24.13：先判「有没有活」，再改开关——已定稿的书不该被切成极速。
        if mode == "express" and str(plan.get("status") or "") == "completed":
            return {"ok": False, "express": express.plan_flag(plan), "running": False,
                    "msg": "该作品已定稿，没有待写的阶段。"}
        express.set_plan_flag(plan, mode == "express", sync_mode=False)
        # 只有「极速续写」才清暂停位（要立刻开写）；「切回标准」只改速度，
        # 不替用户解除暂停——否则界面显示在跑、实际没有线程，比不改更糟。
        if mode == "express":
            plan.setdefault("meta", {})["paused"] = False
            if plan.get("status") == "paused":
                plan["status"] = "in_progress"
        try:
            proj.save_plan(plan)
        except Exception as e:
            return JSONResponse({"error": f"写入极速开关失败：{e}"}, status_code=500)
        if mode == "standard":
            return {"ok": True, "mode": "standard", "express": False, "running": False,
                    "paused": bool((plan.get("meta") or {}).get("paused")),
                    "msg": "已切回标准模式：后续写作恢复逐章审校与打磨。"}
        # v4.33.6：改编剧走集级续跑，不能套常规剧本的「按场批量写」
        # （那条路会被守卫拒绝，用户点了两次只看到两行「已拒绝」）。
        if mod is script_writer:
            adapt = _resume_novel_to_drama(key)
            if adapt is not None:
                return adapt
        fn = getattr(mod, "resume_project", None)
        if not callable(fn):
            return JSONResponse({"error": "该文体暂不支持断点恢复"}, status_code=501)
        # v4.24.13：7 个引擎的 resume_project 是**同步**的（docstring 写明「调用方放后台线程」）。
        # 原实现在请求线程里直接调，一本书写完前 HTTP 都不返回 → 按钮像卡死、
        # 期间 running 还是 false（没走 mark_pipeline），用户再点一次就是两条流水线
        # 同时改同一批文件。这里与 /api/continue 对齐：后台线程 + 运行标记。
        import threading as _th

        def _bg():
            try:
                fn(key)
            except Exception as e:
                print(f"[bg] {mod.__name__} 极速续写失败: {e}", flush=True)
            finally:
                writer.mark_pipeline(key, False)

        writer.mark_pipeline(key, True)   # 先置标记，响应返回时前端就能看到「创作中」
        # v4.33.1：起线程前登记，点「极速续写」也立刻有「谁在工作」反馈
        _job_start(key, kind="express", detail="⚡ 极速续写启动，正在准备剩余的场次…")
        try:
            _th.Thread(target=_bg, daemon=True).start()
        except Exception as e:
            writer.mark_pipeline(key, False)
            livejob.finish(key, ok=False, detail=f"极速续写启动失败：{e}", error=str(e)[:300])
            return JSONResponse({"error": f"极速续写启动失败：{e}"}, status_code=500)
        return _job_payload(key, {"ok": True, "mode": "express", "express": True, "running": True,
                "paused": False,
                "msg": "⚡ 已切到极速模式，正在一口气写完剩余阶段（不评分、不重写）。"}, started=True)

    # ---- 小说 ----
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    try:
        plan = proj.plan()
    except Exception:
        plan = None
    if not plan:
        return JSONResponse({"error": "该作品还没有写作计划，先点「继续」生成一次"}, status_code=404)
    st = proj.status()
    _name = proj.name or key
    if st.get("running"):
        # v4.24.15：速度已经可以运行中切换（开关入口），这里拒绝的只是「再起一条写作线程」。
        return {"ok": False, "running": True, "paused": st.get("paused"), "state": st.get("state"),
                "msg": f"《{_name}》正在创作中——切速度用「⚡ 极速」开关（下一章起生效），不会再起一条写作线程。"}
    _chs = plan.get("chapters") or []
    # v4.32.2：按磁盘正文判定，避免把「正文齐备但被看护打回状态」的章
    # 当成待写章，启动极速续写后覆盖已有好稿、重复计费。
    _done = writer.done_chapters(plan, proj.path)
    _left = len(_chs) - _done
    if mode == "standard":
        express.set_plan_flag(plan, False)
        proj.save_plan(plan)
        return {"ok": True, "mode": "standard", "express": False, "running": False,
                "paused": bool((plan.get("meta") or {}).get("paused")),
                "state": plan.get("status"),
                "msg": "已切回标准模式：后续写作恢复逐章审校与打磨。"}
    # v4.24.13：先判「有没有活」，再改开关。原顺序会把一本已写完的书切成极速模式，
    # 然后回一句「没有待写的章节了」——操作失败了，状态却变了。
    if _left <= 0:
        _job_done(key, f"《{_name}》没有待写的章节了")
        return _job_payload(key, {"ok": False, "running": False, "paused": False,
                "state": plan.get("status"),
                "express": express.plan_flag(plan),
                "msg": f"《{_name}》没有待写的章节了。" if _chs else f"《{_name}》还没有章节规划。"},
                started=False)
    _prev_cfg = dict(plan.get("config") or {})   # 启动失败要原样退回去，别顺带改了速度
    express.set_plan_flag(plan, True)
    # 极速续写：清暂停位并开写。completed 的书也允许续写（规划在、章节没写完是常态），
    # 状态先改回进行中，否则 writer.resume_book 会以「该作品已完成」为由拒绝启动。
    plan.setdefault("meta", {})["paused"] = False
    if plan.get("status") in ("paused", "completed"):
        plan["status"] = "in_progress"
    proj.save_plan(plan)
    # v4.33.1：起线程前登记 + 写日志，点下去立刻显示「极速续写正在跑」
    _job_start(key, kind="express", detail=f"⚡ 极速续写：一口气写完剩余 {_left} 章…",
               total=len(_chs), done=_done)
    ok = writer.resume_book(key, batch_size="all")
    if not ok:
        # 启动失败（并发抢跑 / 线程已存在）：把速度开关退回原样。
        # 否则用户看到「无法启动」，书却已经被切成极速，下一次「继续」会被静默降级。
        livejob.finish(key, ok=False, detail="无法启动极速续写（该作品已在创作中）")
        try:
            _p2 = proj.plan() or {}
            _p2["config"] = _prev_cfg
            _p2["express"] = express.plan_flag(_p2)
            proj.save_plan(_p2)
        except Exception:
            pass
        return _job_payload(key, {"ok": False, "running": True, "paused": False, "state": st.get("state"),
                "express": express.plan_flag({"config": _prev_cfg}),
                "msg": "无法启动极速续写（该作品已在创作中）。"}, started=False)
    return _job_payload(key, {"ok": True, "mode": "express", "express": True, "running": True,
            "paused": False, "state": "in_progress",
            "msg": f"⚡ 已切到极速模式，正在一口气写完剩余 {_left} 章（不评分、不重写）。"},
            started=True)


@app.post("/api/continue")
def api_continue(payload: dict):
    key = payload.get("key")
    try:
        extra = max(0, int(payload.get("extra") or 0))
    except (ValueError, TypeError):
        extra = 0
    # v4.33.6：人已经手动点了「继续」，把总编辑的自动恢复断路器清零——
    # 否则 24h 内 3 次额度一旦用光，之后任何停滞都不会再自愈。
    if key:
        try:
            from bot import supervisor as _sv
            _sv.reset_resume_budget_by_key(key)
        except Exception:
            pass
    # ---- 非小说文体：走各自引擎的断点恢复（服务重启/线程中断后的唯一续跑通道）----
    # v4.24.12：前缀映射抽成 _nonnovel_module()，与 /api/express-write 共用一份。
    # 此前这里内联了一份，加文体时漏改一处 → 该文体的「继续」静默走小说分支。
    mod = _nonnovel_module(key)
    if mod is not None:
        import threading as _th
        # v4.32.3：小说改编出来的短剧有 adaptedFrom，必须走改编专用断点恢复
        # （续跑缺的集 / 补分镜），不能套常规剧本流水线把 12 集重写一遍。
        if mod is script_writer:
            adapt = _resume_novel_to_drama(key)
            if adapt is not None:
                return adapt
        fn = getattr(mod, "resume_project", None)
        if not callable(fn):
            return JSONResponse({"error": "该文体暂不支持断点恢复"}, status_code=501)
        # v4.24.16 修复：无防重入——对比 chat 意图路径与 /api/express-write 都有
        # pipeline_running 守卫，唯独这里没有；双击「继续」会起两条恢复流水线
        # 并发写同一本书的场次/阶段文件。
        if writer.pipeline_running(key):
            # v4.33.1：已经在跑也要刷新登记，前端才能显示「已在进行中」而不是无反应
            _job_start(key, kind="resume", detail="创作线程正在运行，进度见运行日志…")
            return _job_payload(key, {"ok": True, "msg": "该书正在创作中，进度见工作台运行日志。",
                    "running": True, "paused": False, "state": "in_progress"}, started=False)
        _nonnovel_set_paused(key, False)   # v4.20.0：继续前清掉暂停位（统一「继续=接着跑」语义）
        # v4.33.1：起线程前先登记 + 写日志——接口返回的同一秒，界面就能说「谁在工作」
        _job_start(key, kind="resume", detail="正在恢复创作，准备本批次内容…")
        def _bg():
            writer.mark_pipeline(key, True)   # v3.1.0：恢复期间「创作中」可见
            try:
                fn(key)
            except Exception as e:
                livejob.finish(key, ok=False, detail=f"恢复失败：{type(e).__name__}",
                               error=str(e)[:300])
                _job_log(key, f"💥 恢复失败：{type(e).__name__}: {e}")
                print(f"[bg] {mod.__name__} 断点恢复失败: {e}", flush=True)
            finally:
                writer.mark_pipeline(key, False)
        _th.Thread(target=_bg, daemon=True).start()
        return _job_payload(key, {"ok": True, "msg": "已从中断处恢复创作，工作台轮询可见进度。",
                "running": True, "paused": False, "state": "in_progress"}, started=True)
    proj = _get_project(key)
    if not proj:
        # 内存中未命中时尝试从磁盘加载
        try:
            pdir = writer._project_dir(key)
            if os.path.isdir(pdir):
                PROJECT_INDEX[key] = writer.Project(pdir, key)
                proj = PROJECT_INDEX[key]
        except ValueError:
            pass
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    st = proj.status()
    if st.get("running"):
        _job_start(key, kind="resume", detail="小说引擎正在写作，进度见运行日志…",
                   total=int(st.get("total") or 0), done=int(st.get("done") or 0))
        return _job_payload(key, {"ok": True, "msg": f"《{proj.name}》正在创作中，请稍候。",
                                  "running": True, "paused": False, "state": st.get("state")},
                            started=False)
    if extra > 0:
        # 续写：追加 extra 章（对已完成/未完成都适用），复用作品的默认供应商/模型
        writer.append_chapters(proj, extra)
        _job_start(key, kind="append", detail=f"正在续写 {extra} 章…",
                   total=int(st.get("total") or 0) + extra, done=int(st.get("done") or 0))
        writer.start_writing(proj, batch_size=extra)
        return _job_payload(key, {"ok": True,
                "msg": f"《{proj.name}》续写 {extra} 章中…（已完成 {st.get('done')}/{st.get('total')} 章）",
                "running": True, "paused": False, "state": "in_progress"}, started=True)
    if st.get("state") == "completed":
        _job_done(key, f"《{proj.name}》已完成，没有待写的章节")
        return _job_payload(key, {"ok": True,
                "msg": f"《{proj.name}》已完成。想加章请在作品上点「▶ 续写」。"}, started=False)
    # v4.20.0：按开书时设置的「一次写几张」（plan.config.batch_size）写，写完一批自动暂停；
    # 设了「全部写完」才一口气写到底。此前这里漏传批量 → 点一次继续就全书写完。
    _batch = (proj.plan().get("config") or {}).get("batch_size")
    _job_start(key, kind="resume", detail=f"正在继续写作《{proj.name}》…",
               total=int(st.get("total") or 0), done=int(st.get("done") or 0))
    writer.start_writing(proj, batch_size=_batch)
    return _job_payload(key, {"ok": True,
            "msg": f"继续写作《{proj.name}》…（本批 {_batch or '全部'} 章，写完自动暂停）",
            "running": True, "paused": False, "state": "in_progress"}, started=True)

@app.post("/api/branch")
def api_branch(payload: dict):
    """v2.9.0：从第 N 章分支出一本新书（复制 1..N 章与全部设定，从 N+1 章按新思路续写）。
    payload: {key, chapter}。原书完全不受影响；新书 key 加「-分支N」后缀。"""
    import shutil as _shutil
    from datetime import datetime as _dt
    key = str(payload.get("key") or "")
    try:
        ch = int(payload.get("chapter") or 0)
    except (TypeError, ValueError):
        ch = 0
    if not key or ch <= 0:
        return JSONResponse({"error": "缺少 key 或章节号"}, status_code=400)
    src = _get_project(key)
    if not src:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    # v4.24.16 修复：创作中的书直接 copytree 会产生撕裂副本（计划说完成、章节文件是半截）。
    # 与 /api/express-write 对运行中的口径一致：要求先暂停。
    if writer.pipeline_running(key):
        return JSONResponse({"error": "该书正在创作中，请先暂停再分支"}, status_code=409)
    plan = src.plan()
    if not plan:
        return JSONResponse({"error": "仅支持小说作品的分支"}, status_code=400)
    name = plan.get("novelName") or key
    safe_name = re.sub(r'[\\/:*?"<>|\s]+', "", f"{name}·分支{ch}")[:40] or f"分支{ch}"
    # 目录名带时间戳避免冲突；同秒重名递增后缀
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    n = 0
    while True:
        cand = f"{stamp}-{safe_name}{f'-{n}' if n else ''}"
        new_pdir = os.path.join(writer.PROJECTS_DIR, cand)
        if not os.path.exists(new_pdir):
            break
        n += 1
    try:
        _shutil.copytree(src.path, new_pdir)
    except Exception as e:
        return JSONResponse({"error": f"复制作品失败：{e}"}, status_code=500)

    # 新计划：截断到第 ch 章，之后全部回 pending，状态 in_progress 等待新思路
    plan_path = os.path.join(new_pdir, "02-写作计划.json")
    try:
        new_plan = json.load(open(plan_path, encoding="utf-8"))
        new_plan["novelName"] = f"{name}·分支{ch}"
        new_plan["meta"] = {k: v for k, v in (new_plan.get("meta") or {}).items()
                            if k in ("favorite", "tags", "agent_skills")}
        for c in new_plan.get("chapters") or []:
            if isinstance(c, dict) and isinstance(c.get("chapterNumber"), int) and c["chapterNumber"] > ch:
                c["status"] = "pending"
                c["wordCount"] = None
                c["wordCountPass"] = None
                c["retryCount"] = 0
                c.pop("tokens", None)
                c.pop("completedAt", None)
        new_plan["status"] = "in_progress"
        new_plan["branchedFrom"] = {"key": key, "chapter": ch,
                                    "at": _dt.now().isoformat(timespec="seconds")}
        atomic_write_json(plan_path, new_plan)
    except Exception as e:
        return JSONResponse({"error": f"重置分支计划失败：{e}"}, status_code=500)

    # 分支是对话的新起点：清掉原书聊天留档与候选书名等会话性文件
    for junk in ("chat_log.jsonl", "05-标题候选.json", "22-规划体检.json"):
        p = os.path.join(new_pdir, junk)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    for extra in os.listdir(new_pdir):
        if extra.startswith("chat-") and extra.endswith(".json"):
            try:
                os.remove(os.path.join(new_pdir, extra))
            except OSError:
                pass

    # 状态库裁剪到第 ch 章（时间线/最近章回退），best-effort
    try:
        stp = os.path.join(new_pdir, "18-状态库.json")
        if os.path.exists(stp):
            st = json.load(open(stp, encoding="utf-8")) or {}
            st["timeline"] = [t for t in (st.get("timeline") or [])
                              if isinstance(t, dict) and (t.get("chapter") or 0) <= ch]
            m = st.get("metrics") or {}
            m["last_chapter"] = ch
            st["metrics"] = m
            atomic_write_json(stp, st)
    except Exception:
        pass

    # 成书文件按 1..N 章重建（原成书含后续章节）
    new_proj = writer.Project(new_pdir, new_plan.get("novelName", safe_name))
    try:
        writer.rebuild_book(new_proj)
    except Exception:
        pass
    new_proj.log(f"🌿 已从《{name}》第{ch}章分支创建本书；第{ch + 1}章起按新思路创作。")
    PROJECT_INDEX[os.path.basename(new_pdir)] = new_proj
    return {"ok": True, "key": os.path.basename(new_pdir),
            "name": new_plan.get("novelName"), "from": key, "chapter": ch}


@app.get("/api/book-skills")
def api_book_skills(key: str):
    """v3.0.0：某本书的按岗位技能绑定 + 全局绑定（前端合并展示来源徽标）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    from bot import agents as _ag
    return {"ok": True, "key": key,
            "book": _ag.book_skill_bindings(proj.path),
            "global": _ag.get_skill_bindings()}


@app.post("/api/book-skills")
def api_book_skills_set(payload: dict):
    """v3.0.0：设置某本书某岗位的技能绑定（仅本书生效；空列表=清空回落全局）。"""
    key = str(payload.get("key") or "")
    agent = str(payload.get("agent") or "")
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    if agent not in agents.AGENTS and agent not in (agents.get_skill_bindings() or {}):
        return JSONResponse({"error": f"未知岗位：{agent}"}, status_code=400)
    try:
        binds = agents.set_book_skills(proj.path, agent, payload.get("skills") or [])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "key": key, "agent": agent, "book": binds}


@app.post("/api/chapter-names/backfill")
def api_chapter_names_backfill(payload: dict):
    """v3.1.0：老书一次性章名回填——把仍是「第N章」的已完成章用章节摘要批量起名（后台线程）。"""
    key = str(payload.get("key") or "")
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    import threading as _th

    def _run():
        # v4.1.0：一轮跑完所有默认标题章（每轮 1 次 LLM 批量调用，直到无默认章）
        try:
            total = 0
            while True:
                n = writer._backfill_chapter_names(proj, limit=60)
                total += n
                if n == 0:
                    break
            if total:
                print(f"[bg] 章名回填完成：共 {total} 章定名", flush=True)
        except Exception as e:
            print(f"[bg] 章名回填失败: {e}", flush=True)
    _th.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "章名回填已后台启动，每次最多处理 60 章；再点可继续。"}


@app.get("/api/jobs")
def api_jobs(key: str = "", kind: str = "", limit: int = 50):
    """后台任务表（排队/运行/完成/失败），支持按作品/类型过滤。"""
    return {"items": jobs.list_jobs(limit=limit, key=key, kind=kind)}


@app.get("/api/live/{key}")
def api_live(key: str):
    """SSE：把写作/审稿/门禁的进程日志与状态变化实时推给前端（替代高频轮询）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)

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

def _build_download(key: str):
    """拼装整本 Markdown，返回 (bytes, 书名)。桌面保存与浏览器下载共用。

    v4.22.1：一律以章节文件为准（章题 + 引子 + 正文），绝不回落 01-大纲.md；
    v4.32.2：原 /api/download 的主体抽到这里，避免两条导出链路产出不一致。"""
    proj = _get_project(key)
    if not proj:
        raise _ExportError("项目不存在或未加载，请刷新作品库", 404)
    plan = proj.plan() or {}
    if not (plan.get("chapters") or []) and not export_mod.is_script(proj):
        raise _ExportError("还没有可下载的正文")
    name = plan.get("novelName") or plan.get("scriptName") or proj.name
    text = export_mod.markdown(proj)
    if not (text or "").strip():
        raise _ExportError("还没有可下载的正文")
    meta = (plan.get("meta") or {})
    if "gate_rounds" in meta or meta.get("released") is not None:
        pl = int(((plan.get("config") or {}).get("defaults") or {}).get("validation", {}).get("pass_line", 90))
        fq = meta.get("final_quality")
        if meta.get("released"):
            text = f"> ✅ **已放行**（质量 {fq}/{pl}，达到放行线，可出书）\n\n" + text
        else:
            text = f"> ⚠️ **beta·未放行**（质量 {fq}/{pl}，未达放行线，仅供内部阅读）\n\n" + text
    return text.encode("utf-8"), name


@app.get("/api/download")
def api_download(key: str):
    """浏览器端下载整本（Markdown）；桌面端统一走 /api/save。"""
    from urllib.parse import quote
    try:
        data, name = _build_download(key)
    except _ExportError as e:
        return JSONResponse(e.body(), status_code=e.status)
    disp = f"attachment; filename=\"novel.md\"; filename*=UTF-8''{quote(name + '.md')}"
    return Response(data, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": disp})

class _ExportError(Exception):
    """导出失败。v4.35.0：带结构化 code/extra，前端据此弹「强制导出 / 重写低分章」
    ——此前只回一句 error 文案，用户看到「成书未过放行门禁」却没有任何出口，
    被闸门永久卡在导出页（《复仇太妃》实拍）。"""

    def __init__(self, msg, status=400, code="", extra=None):
        super().__init__(msg)
        self.msg = msg
        self.status = status
        self.code = code
        self.extra = extra or {}

    def body(self) -> dict:
        d = {"error": self.msg}
        if self.code:
            d["code"] = self.code
        d.update(self.extra)
        return d


def _delivery_checklist(plan: dict, chapters: list[dict], rep: dict, *, kind: str,
                        forced: bool = False, ranged: bool = False) -> list[dict]:
    """可执行的交付状态清单：每一项都有真实值，不是“已完成”三个字。

    v5 走查里 22-交付就绪报告只列 N01/N07 告警，不回答“这本书到底能不能交”。
    这里把商务交付必须核对的 8 项固化成机器可读清单，导出报告与 API 共用。
    """
    cfg = (plan.get("config") or {}) if isinstance(plan, dict) else {}
    meta = (plan.get("meta") or {}) if isinstance(plan, dict) else {}
    min_w = int(plan.get("minWordsPerChapter") or 0) or int(cfg.get("minWordsPerChapter") or 0) or 0
    # 终审有自己的全书线（默认 75），与逐章 validation.pass_line（80）不是一个尺；
    # 交付清单以终审实际使用的那条线为准，避免“61.5/80”与“61.5/75”两套结论。
    pass_line = int(((meta.get("final_review") or {}).get("book") or {}).get("pass_line")
                    or (cfg.get("validation") or {}).get("pass_line") or 80)
    fq = meta.get("final_quality")
    try:
        fq = float(fq) if fq is not None else None
    except (TypeError, ValueError):
        fq = None
    rows = []
    rows.append({"item": "章节完整", "ok": len(chapters) >= int(plan.get("totalChapters") or plan.get("chapterCount") or cfg.get("defaults", {}).get("chapters") or len(chapters)),
                 "value": f"{len(chapters)} 章", "detail": "计划章节全部落盘" if len(chapters) else "没有可导出章节"})
    if min_w:
        short = []
        for c in chapters:
            wc = purity._cn_len(c.get("text") or "")
            if wc < int(min_w * 0.7):
                short.append(f"第{c.get('n')}章 {wc}")
        rows.append({"item": "逐章字数", "ok": not short, "value": f"下限 {int(min_w*0.7)} 字",
                     "detail": "全部达标" if not short else "、".join(short[:8])})
    else:
        rows.append({"item": "逐章字数", "ok": True, "value": "未设下限", "detail": "作品未设单章下限"})
    rows.append({"item": "终审评分", "ok": (fq is not None and fq >= pass_line),
                 "value": f"{fq if fq is not None else '未评'} / {pass_line}",
                 "detail": "达到放行线" if (fq is not None and fq >= pass_line) else "低于放行线，只能作为初稿"})
    counts = (rep or {}).get("counts") or {}
    rows.append({"item": "交付禁止项", "ok": int(counts.get("block") or 0) == 0,
                 "value": f"{counts.get('block', 0)} 项阻断 / {counts.get('warn', 0)} 项告警",
                 "detail": "无硬阻断" if not counts.get("block") else "存在阻断，投稿会被拒"})
    # 标题/文件一致性（计划 filePath 是否等于规范名）
    try:
        from bot import chapter_identity as _ci
        bad = []
        for ch in (plan.get("chapters") or []):
            if not isinstance(ch, dict):
                continue
            fp = str(ch.get("filePath") or "")
            want = _ci.canonical_name(int(ch.get("chapterNumber") or 0), str(ch.get("title") or ""))
            if fp and os.path.basename(fp) != want:
                bad.append(f"第{ch.get('chapterNumber')}章")
        rows.append({"item": "标题/文件一致", "ok": not bad, "value": f"{len(bad)} 章漂移",
                     "detail": "计划=文件名=正文题头" if not bad else "、".join(bad[:8])})
    except Exception:
        rows.append({"item": "标题/文件一致", "ok": False, "value": "未校验", "detail": "identity 模块异常"})
    # 生产标记泄漏
    leak = 0
    for c in chapters:
        t = c.get("text") or ""
        if any(k in t for k in ("本章概要", "章节备注", "创作说明", "写作说明")):
            leak += 1
    rows.append({"item": "生产标记泄漏", "ok": leak == 0, "value": f"{leak} 章", "detail": "正文纯净" if not leak else "含生产小节，需清理"})
    # 平台规格（有配置才算）
    platform = str(cfg.get("platform") or plan.get("platform") or "")
    rows.append({"item": "平台规格", "ok": True, "value": platform or "通用",
                 "detail": "已绑定平台" if platform else "未绑定平台，按通用稿导出"})
    rows.append({"item": "导出模式", "ok": not forced,
                 "value": "强制内部稿" if forced else ("分章" if ranged else "整书"),
                 "detail": "force=1 仅供内部审阅，不能对外投稿" if forced else "按当前闸门口径导出"})
    return rows


def _checklist_markdown(rows: list[dict], verdict: str) -> str:
    out = ["", "## 交付状态清单（机器可读）", "",
           f"**结论：{verdict}**", "",
           "| 项 | 状态 | 实测 | 说明 |", "|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['item']} | {'通过' if r.get('ok') else '未通过'} | {r.get('value','')} | {r.get('detail','')} |")
    return "\n".join(out) + "\n"


def _export_busy_reason(key: str) -> str:
    """整书导出前必须没有写/审作业在跑。

    v5 走查实证：终审正在重写第 3 章（中间态 880 字）时点导出，闸门读到的是
    “1,218→880 的短章”，同一章随后回到 1,942 字——导出读到了撕裂的中间态。
    这里直接拒绝并给用户明确状态，不再赌“读的时候刚好写完”。
    """
    try:
        from bot import livejob as _lj
        row = _lj.get(key) or {}
        if row and not row.get("finished"):
            kind = str(row.get("kind") or "")
            if kind in ("planning", "resume", "review", "rewrite", "release", "export"):
                label = {"planning": "规划中", "resume": "创作中", "review": "终审中",
                         "rewrite": "改写中", "release": "放行重写中", "export": "导出中"}.get(kind, kind)
                return f"《{os.path.basename(str(key))}》正在{label}，请等本轮结束后再导出"
    except Exception:
        pass
    try:
        if writer.pipeline_running(key):
            return "该书正在写作/终审，章节处于中间态，暂不能导出"
    except Exception:
        pass
    return ""


def _export_build(key: str, fmt: str = "docx", frm: int = 0, to: int = 0, force: int = 0,
                  strict_high: int = 0, mode: str = ""):
    """构建导出产物（v4.32.0 从 GET /api/export 抽出，桌面保存对话框复用）。

    v4.41.4 交付口径：
    · 整书导出只拦「硬伤」（N03 半句截断/没写完），修完即可导原稿；
    · 评分低于达标线不再阻断，只把结果放进 notice 让界面提醒；
    · strict_high=1（作者勾选「强制高分」）才要求重写至 ≥ 达标线；
    · force=1 任何时候都能导出（不给理由，也不被硬伤拦住）。
    返回 (data, media, ext, title, notice:list[str])；错误抛 _ExportError。"""
    proj = _get_project(key)
    if not proj:
        raise _ExportError("项目不存在或未加载，请刷新作品库", 404)
    _busy = _export_busy_reason(key)
    if _busy:
        raise _ExportError(_busy, 409, "busy", {"state": "busy"})
    fmt = (fmt or "docx").lower()
    # v4.31.0：文体三分类——此前非小说一律按剧本取场次，散文/诗词/论文等
    # 单文档文体导出必 400（用户点导出即见裸 JSON 错误页）。
    is_script = os.path.exists(os.path.join(proj.path, script_writer.F_PLAN))
    # 注意排除小说自己的 02-写作计划.json，否则小说会被误判成单文档文体
    _plan_files = [f for f in glob.glob(os.path.join(proj.path, "*计划.json"))
                   if os.path.basename(f) != "02-写作计划.json"]
    _finals = sorted(glob.glob(os.path.join(proj.path, "04-*定稿.md")))
    _drafts = sorted(glob.glob(os.path.join(proj.path, "02-*稿.md")))
    is_mini = (not is_script) and bool(_plan_files)
    novel = not is_script and not is_mini
    plan = proj.plan() or {}
    sproj = None
    _clean = {}
    if novel:
        title = plan.get("novelName") or proj.name
        if not (frm or to):
            # v4.39.0：整书导出前统一体检——坏稿/半句稿先从版本库还原，
            # 保证交付文件里不会出现模型说明或半句截断。
            try:
                writer.book_health.preflight(proj, "导出前体检")
            except Exception:
                pass
        chapters = export_mod.novel_chapters(proj)
        # v4.41.0：闸门前先做确定性清理（标签/章题回显/章节号引用/排版残留）。
        # 这些是纯格式问题，整章重写既浪费 token，又会因「新稿分数没更高」被回滚，
        # 于是每次导出都看到同一批禁止项。能规则修掉的必须在这里修掉。
        try:
            from bot import export_repair as _export_repair
            _clean = _export_repair.repair(proj)
            # v5.1：导出前把章节身份对齐到计划标题（文件名/正文 H1 同一事实源）；
            # 这一步是确定性迁移，失败不阻断导出，只记录。
            try:
                # 只对已盖交付规格章的新作品做身份迁移；历史作品保持旧文件名，
                # 避免“打开旧书导出一次就批量改名”的不可预期副作用。
                if plan.get("deliverySpecVersion"):
                    from bot import chapter_identity as _identity
                    _identity.sync_project(proj, rename=True, persist=True)
            except Exception as _ie:
                proj.log(f"⚠️ 章节身份对齐失败：{_ie}")
            if _clean.get("changed"):
                chapters = export_mod.novel_chapters(proj)
            else:
                chapters = export_mod.novel_chapters(proj)
        except Exception as _e:
            _clean = {}
            try:
                proj.log(f"⚠️ 导出前确定性清理失败：{_e}")
            except Exception:
                pass
    elif is_script:
        sproj = script_writer.ScriptProject(proj.path, proj.name)
        plan = sproj.plan() or plan
        title = plan.get("scriptName") or proj.name
        # v5.2：场景不再冒充「集」；电影/短片/舞台按一整部分组，剧集才按集分组。
        chapters = [{"n": e["n"], "title": e["title"], "text": e["text"]}
                    for e in export_mod.script_episodes(sproj)]
    else:
        # 单文档文体（诗词/散文/论文/儿童文学/新闻稿/文案/其他创作）：
        # 计划里的 *Name 字段做标题，04-*定稿.md 优先、02-*稿.md 兜底。
        # 单文档文体一律读自己的计划（proj.plan() 是小说的 02-写作计划，这里可能
        # 返回 None 也可能返回空 dict，都不能作为标题来源）
        try:
            plan = json.load(open(_plan_files[0], encoding="utf-8"))
        except Exception:
            plan = {}
        title = re.sub(r"^\d{8}-\d{6}-", "", proj.name)   # 去掉目录名的时间戳前缀
        if isinstance(plan, dict):
            for _k, _v in plan.items():
                if _k.endswith("Name") and isinstance(_v, str) and _v.strip():
                    title = _v.strip()
                    break
        fpath = _finals[0] if _finals else (_drafts[0] if _drafts else None)
        doc_label = "定稿" if (_finals or not _drafts) else "初稿"
        ftext = ""
        if fpath:
            try:
                ftext = open(fpath, encoding="utf-8").read().strip()
            except Exception:
                ftext = ""
        chapters = [{"n": 1, "title": doc_label, "text": ftext}] if ftext else []
    if not chapters:
        raise _ExportError("还没有可导出的正文")
    # v5.0：投稿模式先过质量内核（事实源/主角/章卡/结构/终局证据）。
    # 老项目没有 v5 开关，保持原有口径；quick=显式只看内部稿。
    _v5_submit = False
    try:
        from bot import story_kernel as _v5
        _v5_on = bool(novel and _v5.enabled(proj))
        _mode = (mode or "").strip().lower()
        if _v5_on and not force and not (frm or to) and _mode != "quick":
            _v5_submit = True
            _v5_rep = _v5.check_book(proj, chapters)
            if not _v5_rep.get("ok"):
                _blocks = _v5_rep.get("blocks") or []
                raise _ExportError("v5 投稿闸门未通过：请先修质量内核阻断项", 409, "not_ready", {
                    "blocking": len(_blocks),
                    "findings": [{k: f.get(k) for k in ("code", "message", "chapter", "evidence")}
                                 for f in _blocks[:20]],
                })
    except _ExportError:
        raise
    except Exception as _e:
        try:
            proj.log(f"⚠️ v5 投稿闸门异常（按原口径继续）：{_e}")
        except Exception:
            pass
    # v4.33.7 统一交付闸门：所有导出格式共用同一份《交付就绪报告》。
    # 盖了交付规格章的整书导出 = enforcing（有禁止项即拒绝）；未盖章的历史作品、
    # 分章导出与显式 force=1 只出报告、不锁死取回。报告写到项目目录供复核。
    from bot import delivery_gate as _dg, delivery_spec as _ds
    _kind = "script" if is_script else ("novel" if novel else "mini")
    _declared = "novel_submission" if _v5_submit else (str(plan.get("deliveryProfile") or "").strip() or _ds.profile_for(_kind))
    if _kind == "script":
        # 电影/短片/舞台不是「短剧出片档」：用通用正文洁净档，避免 S04 集预算误杀。
        try:
            _form = str(plan.get("form") or "")
            if _form not in script_writer.SERIES_FORMS:
                _declared = "universal"
        except Exception:
            pass
    _stamped = bool(plan.get("deliverySpecVersion"))
    _enforce = "enforcing" if (_stamped and not (frm or to) and not force) else "advisory"
    _rep = _dg.check_delivery(chapters, title=title, kind=_kind, profile=_declared,
                         allowed_latin=writer.latin_allowlist(proj),
                         script_form=(str(plan.get("form") or "") if _kind == "script" else ""))
    _cl = _delivery_checklist(plan, chapters, _rep, kind=_kind,
                              forced=bool(force), ranged=bool(frm or to))
    _rep["checklist"] = _cl
    _all_ok = all(x.get("ok") for x in _cl)
    _rep["verdict"] = ("可投稿交付" if _all_ok and not force else
                       "内部稿（强制导出）" if force else
                       "初稿（存在未通过项）" if not _all_ok else "可交付")
    _rep["ready"] = bool(_all_ok and not force)
    # v4.36.0：报告开头先说标尺（形态 + 平台基准），再看禁止项。
    _taste_info = {}
    if _kind == "novel":
        try:
            from bot import taste as _taste
            _tcfg = plan.get("config") or {}
            _tform = _taste.form_of(_tcfg)
            _taste_info = {"form": _tform, "form_label": _taste.FORMS[_tform]["label"],
                           "benchmark": _taste.benchmark_note(
                               _tcfg.get("platform") or plan.get("platform") or "")}
            _rep.setdefault("notes", []).extend(_taste.report_notes(_tcfg))
            _rep.update(_taste_info)
        except Exception:
            _taste_info = {}
    try:
        with open(os.path.join(proj.path, "22-交付就绪报告.md"), "w", encoding="utf-8") as _f:
            _f.write(_dg.report_markdown(_rep))
            _f.write(_checklist_markdown(_cl, _rep["verdict"]))
    except OSError:
        pass
    _notice = []
    _hard_codes = ("N03",) if (novel and not force) else ()
    _ok, _msg = _dg.export_ready(_rep, enforcement=_enforce, block_codes=_hard_codes)
    if not _ok:
        # v4.35.0：禁止项明细随错误一起回给前端——用户要看到「哪几章、什么问题」。
        # v4.41.4：只列真正拦住导出的硬伤（N03）；其余同类问题只记录在报告里。
        _blocks = [f for f in (_rep.get("findings") or [])
                   if f.get("severity") == "block" and f.get("code") in _hard_codes]
        _others = [f for f in (_rep.get("findings") or [])
                   if f.get("severity") == "block" and f.get("code") not in _hard_codes]
        raise _ExportError(_msg, 409, "not_ready", {
            "blocking": len(_blocks),
            "findings": [{k: f.get(k) for k in ("code", "name", "location", "detail")}
                         for f in _blocks[:20]],
            "warnings": [{k: f.get(k) for k in ("code", "name", "location", "detail")}
                         for f in _others[:20]],
            "repaired": len(_clean.get("changed") or []),
            **_taste_info,
        })
    # v4.33.8 未达标不放行 → v4.41.4 改成「默认可导原稿、只提醒」：
    # 盖章作品评分低于达标线时不再拦住导出；只有作者勾选「强制高分」才要求重写到达标线。
    # force=1 任何时候都放行（带「未放行初稿」标注，避免冒充定稿）。
    _rel_block, _rel_msg = export_mod.release_export_block(plan)
    if _rel_block and _stamped and not (frm or to):
        _fq0 = (plan.get("meta") or {}).get("final_quality")
        _pl0 = ((plan.get("config") or {}).get("validation") or {}).get("pass_line") or 80
        try:
            _pl0 = int(_pl0)
        except (TypeError, ValueError):
            _pl0 = 80
        try:
            _below0 = len([x for x in writer.per_chapter_scores(proj)
                           if isinstance(x.get("score"), (int, float)) and x["score"] < _pl0])
        except Exception:
            _below0 = 0
        if strict_high and not force:
            raise _ExportError(
                f"已选择「强制高分」：本书最终质量 {_fq0 if _fq0 is not None else '?'} 分 < 达标线 {_pl0}，"
                f"需要先重写到 {_pl0} 分以上再导出；也可以取消勾选直接导出原稿。",
                409, "not_released", {
                    "final_quality": _fq0, "pass_line": _pl0, "below": _below0,
                    "chapters": len(chapters), "title": title, "strict_high": 1,
                    **_taste_info,
                })
        if force:
            title = f"{title}（未放行初稿{_fq0 if _fq0 is not None else ''}分）"
        else:
            _notice.append(
                f"本书最终质量 {_fq0 if _fq0 is not None else '?'} 分，低于达标线 {_pl0} 分"
                + (f"（{_below0} 章未达标）" if _below0 else "")
                + "；已按你的选择导出原稿。需要冲高分，可在导出时勾选「强制高分」再重写。")
    # v4.27.3：章节范围过滤（闭区间；0=不限）——docx/epub/print 都吃 chapters，
    # md 由过滤后的章手拼（markdown(proj) 只能整书）。
    ranged = bool(frm or to)
    if ranged:
        lo, hi = sorted((int(frm or 0), int(to or 10 ** 9)))
        chapters = [c for c in chapters if lo <= int(c.get("n") or 0) <= hi]
        if not chapters:
            raise _ExportError(f"第 {lo}-{hi} 章范围内还没有可导出的正文")
        title = f"{title}（第{lo}-{hi}章）"
    blocks = []
    if novel:
        for c in chapters:
            blocks.append((1, f"第{c['n']}章 {export_mod._strip_md(c['title'])}"))
            blocks.extend(export_mod._md_blocks(c["text"]))
    elif is_mini:
        for c in chapters:
            blocks.append((1, f"{export_mod._strip_md(c['title'])}"))
            blocks.extend(export_mod._md_blocks(c["text"]))
    # v4.15.3：署名回退链 —— 作品 meta.author → 向导笔名（plan.config.penname）→ 全局默认笔名
    author = ((plan.get("meta") or {}).get("author") or "").strip()
    if not author:
        author = str(((plan.get("config") or {}).get("penname")) or "").strip()
    if not author:
        author = str((_load_pennames().get("default")) or "").strip()

    if fmt in ("md", "markdown"):
        if ranged or not novel:
            # v4.31.0：非小说（剧本/单文档文体）也由章手拼——markdown(proj) 是小说专用
            _c0 = chapters[0] if len(chapters) == 1 else None
            _first0 = next((ln for ln in str((_c0 or {}).get("text", "")).splitlines() if ln.strip()), "") if _c0 else ""
            if _c0 and not ranged and _first0.startswith("# "):
                # 单文档正文自带 H1（定稿文件首行就是标题）→ 原文直出，不再叠加书名题头
                data = str(_c0.get("text", "")).encode("utf-8")
                media, ext = "text/markdown; charset=utf-8", "md"
                return data, media, ext, title, _notice
            md = [f"# {title}", ""]
            for c in chapters:
                head = f"第{c['n']}章 {c.get('title', '')}" if novel else str(c.get("title", "") or f"第{c['n']}节")
                _first = next((ln for ln in str(c.get("text", "")).splitlines() if ln.strip()), "")
                if novel or not _first.startswith("#"):
                    md.append(f"## {head}")
                    md.append("")
                md.append(c.get("text", ""))
                md.append("")
            data = "\n".join(md).encode("utf-8")
        else:
            # v4.35.0：force 导出会在书名后加「（未放行初稿x分）」，而 markdown(proj)
            # 自己取 plan.novelName 拼题头——两处不一致时以 title 为准，别让
            # 「未放行」标注只在 Word 里出现、Markdown 冒充定稿。
            _md = export_mod.markdown(proj)
            _origin = plan.get("novelName") or proj.name
            if title != _origin:
                _md = _md.replace(f"# {_origin}", f"# {title}", 1)
            data = _md.encode("utf-8")
        media, ext = "text/markdown; charset=utf-8", "md"
    elif fmt == "docx":
        if not blocks and chapters:
            # v4.12.16：剧本 docx 导出——此前 blocks 只在小说分支填充，剧本导出 Word 得到空文档
            for ch in chapters:
                blocks.extend([("h2", ch.get("title", "")), ("p", ch.get("text", ""))])
        data = export_mod.docx_bytes(title, (f"作者：{author}" if author else ""), blocks, author=author)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ext = "docx"
    elif fmt == "epub":
        data, media, ext = export_mod.epub_bytes(title, author, chapters), "application/epub+zip", "epub"
    elif fmt in ("print", "html"):
        data = export_mod.print_html(title, author, chapters).encode("utf-8")
        media, ext = "text/html; charset=utf-8", "html"
    elif fmt == "fountain" and is_script:
        data, media, ext = export_mod.fountain(sproj).encode("utf-8"), "text/plain; charset=utf-8", "fountain"
    elif fmt == "fdx" and is_script:
        data, media, ext = export_mod.fdx_bytes(sproj), "application/xml", "fdx"
    else:
        raise _ExportError(f"不支持的格式：{fmt}")
    return data, media, ext, title, _notice


def _export_settle(key: str, frm: int, to: int):
    """v4.34.0 状态规则：整本导出即「出书」——出书后必须彻底停止创作流程
    （写线程下一检查点自停、看护线程退出、作业登记收口、账面归位），
    不再显示「正在创作中」，避免 token 浪费与并发增加。
    章节范围导出（frm/to 任一非 0）不算出书，不影响创作。"""
    if int(frm or 0) or int(to or 0):
        return
    try:
        writer.stop_creation(key, reason="已出书（整本导出），创作流程已停止")
    except Exception:
        pass


@app.get("/api/delivery-status")
def api_delivery_status(key: str):
    """交付状态清单（只读）：前端在导出前就能看到“能不能交、差在哪”。

    v5.1 新增：旧流程只有点导出被 409 后才知道阻断项；这里把同一份
    delivery_gate 报告 + 8 项商务清单前置，不触发写盘、不调 LLM。
    """
    proj = _get_project(key)
    if not proj:
        raise _ExportError("项目不存在或未加载，请刷新作品库", 404)
    plan = proj.plan() or {}
    is_script = bool(plan.get("scenes"))
    chapters = export_mod.novel_chapters(proj) if not is_script else []
    kind = "script" if is_script else "novel"
    from bot import delivery_gate as _dg
    rep = _dg.check_delivery(chapters, title=proj.name, kind=kind,
                                       profile="novel_submission" if not is_script else None,
                                       allowed_latin=writer.latin_allowlist(proj))
    cl = _delivery_checklist(plan, chapters, rep, kind=kind)
    scores = writer.per_chapter_scores(proj) if not is_script else []
    return {"ok": True, "verdict": ("可投稿交付" if all(x.get("ok") for x in cl)
                                    else "初稿（存在未通过项）"),
            "counts": rep.get("counts") or {}, "checklist": cl,
            "scores": scores, "findings": (rep.get("findings") or [])[:60]}


@app.get("/api/export")
def api_export(key: str, fmt: str = "docx", frm: int = 0, to: int = 0, force: int = 0,
               strict_high: int = 0, mode: str = ""):
    """专业导出：md / docx / epub / print；剧本另支持 fountain / fdx。
    v4.27.3：frm/to 章节范围——侧栏「导出」弹窗可选整书或某几章（闭区间，0=不限）。
    v4.32.0：构建逻辑下沉 _export_build（桌面原生保存对话框复用同一套）。
    v4.41.4：strict_high=1 表示作者勾选「强制高分」；默认只修硬伤、评分仅提醒。"""
    from urllib.parse import quote
    try:
        data, media, ext, title, notice = _export_build(key, fmt, frm, to, force, strict_high, mode)
    except _ExportError as e:
        return JSONResponse(e.body(), status_code=e.status)
    _export_settle(key, frm, to)
    fname = re.sub(r'[\\/:*?"<>|]+', "", title)[:60] or "export"
    disp = f"attachment; filename=\"export.{ext}\"; filename*=UTF-8''{quote(fname + '.' + ext)}"
    headers = {"Content-Disposition": disp}
    if notice:
        headers["X-Export-Notice"] = quote(" | ".join(notice)[:600])
    return Response(data, media_type=media, headers=headers)


_SAVE_KINDS = ("export", "diagnostics", "download", "script")


def _safe_export_name(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "", str(title or ""))[:60] or "export"


def _save_payload(kind: str, payload: dict):
    """按导出类型产出 (bytes, 默认文件名, 扩展名, 提醒列表)。

    v4.32.2：整个系统「向外导出」只有一个生成入口——作品导出、诊断包/系统日志、
    整本 Markdown、剧本定稿全文。所有调用方最后都经过 _save_bytes_to_dialog，
    保证桌面端必定弹出「存储」对话框，绝不会把文件内容回灌进 webview。
    v4.41.4：返回 notice（如「评分低于达标线，已按原稿导出」），前端导出后可提示。"""
    p = payload if isinstance(payload, dict) else {}
    key = str(p.get("key") or "")
    if kind == "export":
        fmt = str(p.get("fmt") or "docx").lower()
        frm = int(p.get("frm") or 0)
        to = int(p.get("to") or 0)
        data, _media, ext, title, notice = _export_build(
            key, fmt, frm, to, int(p.get("force") or 0), int(p.get("strict_high") or 0),
            str(p.get("mode") or ""))
        _export_settle(key, frm, to)   # v4.34.0：整本导出即出书 → 停止创作流程
        return data, f"{_safe_export_name(title)}.{ext}", ext, notice
    if kind == "diagnostics":
        text, scope = _build_diagnostics_text(p)
        return text.encode("utf-8"), f"yimu-lit-诊断包-{scope}.txt", "txt", []
    if kind == "download":
        data, name = _build_download(key)
        return data, f"{_safe_export_name(name)}.md", "md", []
    if kind == "script":
        txt = script_writer.export_text(key)
        if not txt:
            raise _ExportError("项目不存在或未加载，请刷新作品库", 404)
        return txt.encode("utf-8"), f"{_safe_export_name(key)}.txt", "txt", []
    raise _ExportError(f"不支持的导出类型：{kind}")


def _save_bytes_to_dialog(data: bytes, default_name: str, ext: str):
    """弹原生「存储」对话框并写盘。取消 → {cancelled}；成功 → {saved: 绝对路径}。"""
    path = desktop_bridge.save_dialog(default_name)
    if not path:
        return {"cancelled": True}
    path = str(path)
    root, extn = os.path.splitext(path)
    if not extn:
        path = f"{root}.{ext}"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return {"saved": path}


@app.post("/api/save")
def api_save(payload: dict):
    """v4.32.2：全系统统一的导出保存入口。

    桌面 webview（WKWebView / WebView2）不支持 <a download>，blob 会变成导航到
    文件内容页——诊断包就是这样把用户困在一个没有返回键的窗口里。因此桌面端
    **一律**由服务端弹原生「存储」对话框写盘；网页端返回 desktop=False，
    前端走浏览器 blob 下载（浏览器自己的下载管理，不会导航）。"""
    kind = str((payload or {}).get("kind") or "")
    if kind not in _SAVE_KINDS:
        return JSONResponse({"error": f"不支持的导出类型：{kind or '（空）'}"}, status_code=400)
    if not desktop_bridge.is_desktop():
        return {"desktop": False}
    try:
        data, fname, ext, notice = _save_payload(kind, payload)
    except _ExportError as e:
        return JSONResponse(e.body(), status_code=e.status)
    out = _save_bytes_to_dialog(data, fname, ext)
    if notice:
        out["warnings"] = notice
    return out


@app.post("/api/export/save")
def api_export_save(payload: dict):
    """v4.32.0 作品导出端点（保留兼容旧客户端与既有测试）；v4.32.2 起内部委托
    给 _save_payload/_save_bytes_to_dialog，与 /api/save 是同一套写盘逻辑。"""
    if not desktop_bridge.is_desktop():
        return {"desktop": False}
    try:
        data, fname, ext, notice = _save_payload("export", payload)
    except _ExportError as e:
        return JSONResponse(e.body(), status_code=e.status)
    out = _save_bytes_to_dialog(data, fname, ext)
    if notice:
        out["warnings"] = notice
    return out

@app.get("/api/agents")
def api_agents():
    """v0.5 M4：多 Agent 宇宙 —— 各岗位当前模型绑定 + 可选模型树 + 全局并发上限。"""
    view = agents.view()
    # 分章齐写的默认并发（config.json defaults.chapter_workers），前端滑杆据此初始化。
    view["default_workers"] = max(1, min(8, int(writer._cfg_defaults().get("chapter_workers") or 5)))
    return view

@app.post("/api/agents/add")
def api_agents_add(payload: dict):
    """v2.6.0：添加自定义 Agent。
    payload: {name, label, desc, stage?, skills?}
    其中 name 可以是英文 key 或自然语言描述。
    如果只传 desc，用 LLM 自动生成 name/label/stage/skills。
    """
    from bot import agents as _ag
    try:
        desc = str(payload.get("desc") or "").strip()
        name = str(payload.get("name") or "").strip()
        label = str(payload.get("label") or "").strip()
        stage = str(payload.get("stage") or "writing").strip()
        skills = payload.get("skills")
        if not name and not desc:
            return JSONResponse({"error": "请提供 Agent 名称或描述"}, status_code=400)
        # 如果只传了自然语言描述，用 LLM 自动解析
        if not name and desc:
            try:
                from bot import llm as _llm
                prompt = f"""请根据以下描述创建一个创作 Agent，返回 JSON：
描述：{desc}
返回格式：{{"name": "english_key", "label": "中文名称", "stage": "planning|writing|review", "skills": []}}
注意：name 用英文下划线格式，label 用中文，stage 选最匹配的阶段。"""
                res = _llm.chat_json([{"role": "user", "content": prompt}], cfg=None)   # v4.17.4：此前引用未定义的 _cfg() → 自然语言建 Agent 必失败
                if isinstance(res, dict):
                    name = res.get("name", "")
                    label = res.get("label", desc[:8])
                    stage = res.get("stage", "writing")
                    skills = res.get("skills", [])
            except Exception:
                # LLM 不可用时，从描述生成简单 name
                import re as _re
                name = _re.sub(r"[^\w]", "_", desc[:20]).strip("_").lower() or "custom_agent"
                label = desc[:8]
                stage = "writing"
        result = _ag.add_agent(name, desc, stage=stage, skills=skills, label=label)
        syslog_mod.log_event("agent", "添加岗位",
                             {"name": name, "label": label or name, "stage": stage},
                             f"添加自定义岗位「{label or name}」")
        return {"ok": True, "agent": result, "name": name}
    except Exception as e:
        return JSONResponse({"error": f"添加 Agent 失败：{e}"}, status_code=500)


@app.post("/api/agents-set")
def api_agents_set(payload: dict):
    """v0.5 M4：给某子 Agent 单独配 provider/model（留空=回落 stage/默认）。"""
    am = agents.set_binding(payload.get("agent"), payload.get("provider_id"), payload.get("model"))
    _m = str(payload.get("model") or "").strip()
    _p = str(payload.get("provider_id") or "").strip()
    syslog_mod.log_event("agent", "岗位改模型",
                         {"agent": payload.get("agent"), "provider_id": _p, "model": _m},
                         f"岗位「{payload.get('agent')}」模型改为 {_m or _p or '阶段/默认'}")
    return {"ok": True, "agent_models": am}

@app.post("/api/agents-skills")
def api_agents_skills(payload: dict):
    """v2.1 P2：给某岗位绑定技能（skillkit 文件 name）；空列表=解绑。
    绑定后该岗位产出的注入会优先带上这些技能（skillkit.pick 的 boost 维度）。"""
    try:
        agents.set_skills(payload.get("agent"), payload.get("skills"))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    _n = len(payload.get("skills") or [])
    syslog_mod.log_event("agent", "岗位绑技能",
                         {"agent": payload.get("agent"), "count": _n, "skills": list(payload.get("skills") or [])[:10]},
                         f"岗位「{payload.get('agent')}」绑定 {_n} 个技能")
    return {"ok": True, "agent": payload.get("agent"), "skills": agents.skills_for(payload.get("agent"))}

@app.post("/api/agents-maxglobal")
def api_agents_maxglobal(payload: dict):
    """v0.5 M4：设全局并发上限（多 Agent/多书同时创作时的在飞上限）。"""
    _n = int(payload.get("n", 4))
    _v = agents.set_max_global(_n)
    syslog_mod.log_event("agent", "改并发上限", {"max_global_agents": _v}, f"全局并发上限改为 {_v}")
    return {"ok": True, "max_global_agents": _v}

@app.post("/api/agents-toggle")
def api_agents_toggle(payload: dict):
    """v4.20：启用/停用创作岗位（停用 = 流水线跳过该岗位环节；作家 writer 不可停用）。"""
    agent = str(payload.get("agent") or "").strip()
    enabled = bool(payload.get("enabled"))
    try:
        _disabled = agents.set_agent_enabled(agent, enabled)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    syslog_mod.log_event("agent", "岗位启停",
                         {"agent": agent, "enabled": enabled},
                         f"{'启用' if enabled else '停用'}岗位「{agent}」")
    return {"ok": True, "agent": agent, "enabled": enabled, "agent_disabled": _disabled}

@app.post("/api/agents/remove")
def api_agents_remove(payload: dict):
    """v4.20：删除自定义岗位（内置岗位只能停用不能删；模型/技能绑定与停用状态一并清除）。"""
    name = str(payload.get("name") or "").strip()
    try:
        agents.remove_custom_agent(name)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    syslog_mod.log_event("agent", "删除岗位", {"name": name}, f"删除自定义岗位「{name}」")
    return {"ok": True, "name": name}

@app.get("/api/global-model")
def api_global_model_get():
    """v4.20：全局默认模型（= default_provider + 其 default_model）。"""
    return {"ok": True, **providers.global_model()}

@app.post("/api/global-model")
def api_global_model_set(payload: dict):
    """v4.20：设全局默认模型（provider_id + model；校验供应商 active 且模型在其列表里）。POST 返回最新全局默认。"""
    try:
        providers.set_global_model(payload.get("provider_id"), payload.get("model"))
    except providers.ProviderError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    _g = providers.global_model()
    syslog_mod.log_event("provider", "设全局默认模型",
                         {"provider_id": _g.get("provider_id"), "model": _g.get("model")},
                         f"全局默认模型设为 {_g.get('provider_id') or '—'} · {_g.get('model') or '—'}")
    return {"ok": True, **_g}

# ---------- v0.6 B：并行章节创作（单本书内 N 个 Agent 分章齐写 + 总架构对齐） ----------
_PARALLEL_RUNNING = {}
# v4.24.16：运行标志注册表的检查-置位互斥（_PARALLEL_RUNNING/_REWRITING/_ONESHOT_RUNNING 共用）
_RUNNING_REG_LOCK = threading.Lock()

@app.post("/api/parallel-write")
def api_parallel_write(payload: dict):
    """v0.6 B：单本书内 N 个 worker 分章齐写（先熔炼总架构，写完一个立即取下一章）。后台线程跑，立即返回。"""
    key = payload.get("key")
    default_workers = max(1, min(8, int(writer._cfg_defaults().get("chapter_workers") or 5)))
    nworkers = max(1, min(8, int(payload.get("workers") or default_workers)))
    proj = _get_project(key)
    if not proj or not proj.plan():
        return JSONResponse({"ok": False, "error": "项目不存在或未创建"}, status_code=404)
    # v4.24.16 修复 TOCTOU：检查+置位之间无锁——两个并发请求都能过检查，
    # 同一本书起两条并行写作线程（v4.7.1 已给 /api/release 修过同款，这里补齐）。
    with _RUNNING_REG_LOCK:
        if _PARALLEL_RUNNING.get(key, {}).get("running"):
            return {"ok": True, "running": True, "msg": "该书并行创作进行中…"}
        _PARALLEL_RUNNING[key] = {"running": True, "result": None, "workers": nworkers}
    def _job():
        try:
            if key.startswith("s_"):
                # v4.16.7：剧本走多岗位并行分场齐写（场次结构独立引擎）
                # v4.24.12 修复：此前在后台线程里再起一个嵌套线程然后直接 return——
                # 内层线程跑完没人收尾，_PARALLEL_RUNNING[key] 永远 running=True，
                # /api/parallel-log 和书页活动阶段会永久误报「并行创作进行中」；
                # 且项目缺失时在线程里 return JSONResponse 无人接收，用户仍被告知「创作中」。
                # 现在 _job 本身就是后台线程，直接同步跑完并收尾。
                import script_writer as _sw
                sproj = _sw.get_project(key)
                if not sproj:
                    _PARALLEL_RUNNING[key] = {"running": False, "result": {"error": "剧本项目不存在"}, "workers": nworkers}
                    return
                _sw._write_loop_parallel_scenes(sproj, n_workers=nworkers, rounds=payload.get("rounds"))
                _PARALLEL_RUNNING[key] = {"running": False, "result": "ok", "workers": nworkers}
                return
            writer._write_loop_parallel(proj, n_workers=nworkers, rounds=payload.get("rounds"))
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
    # v4.24.16：检查+置位加注册表互斥（TOCTOU，同 /api/parallel-write）
    with _RUNNING_REG_LOCK:
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
            try: writer._write_loop_safe(proj)
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

@app.get("/api/skeleton")
def api_skeleton(key: str, regen: int = 0):
    """v0.9.0：该书『写作骨架』（17-写作骨架.md）——写到哪/多少/重点/为何/续写提示。
    纯文件聚合，不花 LLM；regen=1 立即重新生成（也可由写作/重写/放行自动刷新）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
    """写章节正文：统一走 book_health 门禁（校验→快照→落盘→同步字数）。"""
    ch, plan = _chapter_entry(proj, n)
    if ch is None:
        raise ValueError("章节不存在")
    fp = _chapter_path(proj, ch)
    old = open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
    res = writer.book_health.save_chapter(proj, n, text, label=label, kind="manual")
    if not res.get("ok"):
        raise ValueError(res.get("reason") or "正文写入被拒绝")
    if old != text:
        _capture_edit_feedback(proj, n, old, text)
    words = int(res.get("words") or 0)
    with proj.lock:
        latest = proj.plan() or plan
        chs = latest.get("chapters") or []
        if not (1 <= n <= len(chs)):
            raise ValueError("章节不存在")
        target = chs[n - 1]
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
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    ch, _plan = _chapter_entry(proj, n)
    if ch is None:
        return JSONResponse({"error": "章节不存在"}, status_code=404)
    fp = _chapter_path(proj, ch)
    text = open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
    return {"chapter": n, "title": ch.get("title") or "", "text": text,
            # v3.1.0：cleanText = 章题+引子+正文（剥本章概要/章节备注），复制与对外分享用；
            # text 保留原结构供编辑器与 PUT 回写
            "cleanText": writer._clean_chapter_text(text, fallback_title=ch.get("title") or ""),
            # v4.22.2：rawText —— 工作文件原文（含概要/备注/可能的历史污染），仅供「工作文件」视图核对
            "rawText": text,
            "words": ch.get("wordCount") or 0,
            "updatedAt": ch.get("editedAt") or "",
            "versions": len(versions.list_versions(proj.path, n))}


@app.put("/api/chapter")
def api_chapter_save(payload: dict):
    """手动编辑 / 自动保存：写正文 + 词数 + 重建成书 + 版本快照。"""
    proj = _get_project(payload.get("key"))
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    text = payload.get("text")
    if not isinstance(text, str):
        return JSONResponse({"error": "正文必须是字符串"}, status_code=400)
    if len(text) > 2_000_000:
        return JSONResponse({"error": "单章超过 200 万字符，请分段保存"}, status_code=413)
    # v4.22.2：编辑器默认只显示纯净正文（章题+引子+正文）。此时保存要把旧文件里的
    # 「本章概要 / 章节备注」补回去，否则工作文件会丢规划元信息；旧文件里的修改说明/
    # 评分等污染不会被回填。
    if str(payload.get("mode") or "") == "prose":
        try:
            _old = ""
            _ch0, _ = _chapter_entry(proj, payload.get("n"))
            _fp0 = _chapter_path(proj, _ch0) if _ch0 else ""
            if _fp0 and os.path.exists(_fp0):
                _old = open(_fp0, encoding="utf-8").read()
            if _old:
                text = purity.merge_meta(text, _old)
        except Exception:
            pass
    else:
        # 未声明 mode（旧客户端/接口调用）→ 按整文件处理，但必须去污染。
        # 本就干净的正文保持原样（不做无意义的尾换行改写，避免影响按字节比对的调用方）。
        try:
            _s = purity.sanitize_chapter(text)
            if _s.strip() != text.strip():
                text = _s
        except Exception:
            pass
    try:
        words = _apply_chapter_text(proj, payload.get("n"), text)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    # v5.1.0 边写边存的节流快照：距上次自动快照 ≥5 分钟或字数变化 ≥200 才落
    # （首版 ≥50 字、auto 类每章上限 20 版）。失败不影响保存主链路（降级不静默分叉）。
    try:
        versions.throttled_snapshot(proj.path, int(payload.get("n")), text, label="自动保存")
    except Exception:
        pass
    return {"ok": True, "chapter": int(payload.get("n")), "words": words,
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}


@app.get("/api/chapter/versions")
def api_chapter_versions(key: str, n: int):
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    ch, _ = _chapter_entry(proj, n)
    if ch is None:
        return JSONResponse({"error": "章节不存在"}, status_code=404)
    return {"chapter": n, "items": versions.list_versions(proj.path, n)}


@app.get("/api/chapter/version")
def api_chapter_version(key: str, n: int, id: str):
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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


# ---------- v5.1.0 写作台编辑器内核：新端点全部增量，不改既有路由语义 ----------

def _chapter_versions_restore_impl(payload: dict):
    """v5.1.0 恢复历史版本（新路径，与旧 /api/chapter/restore 并存）：
    恢复前先把当前稿存为 label=恢复前 的快照（versions.restore_version），
    再写盘目标版本；写盘仍走 _apply_chapter_text 的统一门禁（校验→快照→落盘→字数）。
    PUT/POST 双方法注册：前端按语义用 PUT，脚本/兼容习惯用 POST。"""
    proj = _get_project(payload.get("key"))
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    n = payload.get("n")
    ch, _ = _chapter_entry(proj, n)
    if ch is None:
        return JSONResponse({"error": "章节不存在"}, status_code=404)
    fp = _chapter_path(proj, ch)
    cur = open(fp, encoding="utf-8").read() if fp and os.path.exists(fp) else ""
    try:
        text = versions.restore_version(proj.path, n, payload.get("id"), cur)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        words = _apply_chapter_text(proj, n, text, label="恢复前")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return {"ok": True, "chapter": int(n), "words": words,
            "snapshot_before": "恢复前"}


@app.put("/api/chapter/versions/restore")
def api_chapter_versions_restore_put(payload: dict):
    return _chapter_versions_restore_impl(payload)


@app.post("/api/chapter/versions/restore")
def api_chapter_versions_restore_post(payload: dict):
    return _chapter_versions_restore_impl(payload)


@app.get("/api/selection-ai/actions")
def api_selection_ai_actions():
    """划词 AI 动作元信息（label/温度/选区上限）——前端气泡按它渲染，不硬编码。"""
    try:
        from bot import selection_ai
        return {"ok": True, "actions": selection_ai.selection_ai_meta()}
    except Exception as e:
        return JSONResponse({"error": f"动作表读取失败：{e}"}, status_code=500)


@app.post("/api/chapter/selection-ai")
async def api_chapter_selection_ai(request: Request):
    """v5.1.0 划词 AI（SSE 流式）。

    入参：{action, selection, before?, after?, title?, summary?, instruction?,
           persona?, key?, n?}——选区与前后文由前端从编辑器现算后传来，
    后端再按 220 字窗口钳制。出参（SSE）：start → delta*(增量) → done{text}；
    失败：error 帧 + done{ok:false}。**不落稿**：替换由前端在校验原选区未变后执行。
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
    payload = payload or {}
    from bot import selection_ai
    action = str(payload.get("action") or "")
    if action not in selection_ai.ACTIONS:
        return JSONResponse(
            {"error": f"未知动作：{action}（可用：{'/'.join(selection_ai.ACTIONS)}）"},
            status_code=400)
    selection = str(payload.get("selection") or "")
    if not selection.strip():
        return JSONResponse({"error": "选区为空，请先在正文里选中一段文字"}, status_code=400)
    key = str(payload.get("key") or "")
    n = payload.get("n")
    ctx = {
        "before": str(payload.get("before") or ""),
        "after": str(payload.get("after") or ""),
        "title": str(payload.get("title") or ""),
        "summary": str(payload.get("summary") or ""),
        "instruction": str(payload.get("instruction") or ""),
        "persona": str(payload.get("persona") or ""),
        "key": key,
        "chapter_no": n if isinstance(n, int) else None,
    }

    import asyncio as _asyncio
    import queue as _queue

    async def gen():
        t0 = time.time()
        yield _sse("start", {"action": action, "ts": int(t0)})
        q: _queue.Queue = _queue.Queue()
        box: list = []

        def _work():
            try:
                text = selection_ai.run_stream(
                    action, selection, on_delta=lambda piece: q.put(("delta", piece)), **ctx)
                box.append((True, text))
                q.put(("done", None))
            except BaseException as e:   # LLMError/入参错误都进 error 帧，绝不 500 中断流
                box.append((False, str(e)))
                q.put(("error", None))

        threading.Thread(target=_work, daemon=True).start()
        # 逐帧转发：线程队列 → SSE（delta 实时、done 带全文与耗时）
        while True:
            kind, _val = await _asyncio.to_thread(q.get)
            if kind == "delta":
                yield _sse("delta", {"text": _val})
            elif kind == "done":
                ok, text = box[0] if box else (False, "划词结果为空")
                if ok:
                    yield _sse("done", {"ok": True, "text": text, "action": action,
                                        "elapsed": round(time.time() - t0, 2)})
                else:
                    yield _sse("error", {"error": text})
                    yield _sse("done", {"ok": False, "error": text,
                                        "elapsed": round(time.time() - t0, 2)})
                return
            else:  # error
                err = box[0][1] if box else "划词调用失败"
                yield _sse("error", {"error": err})
                yield _sse("done", {"ok": False, "error": err,
                                    "elapsed": round(time.time() - t0, 2)})
                return

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

def _preview_pass_line(plan):
    try:
        return int(((plan.get("config") or {}).get("defaults") or {}).get("validation", {}).get("pass_line", 90))
    except (ValueError, TypeError):
        return 90

@app.get("/api/preview")
def api_preview(key: str):
    """预览：返回已写章节拼接（去噪后的正文）+ 章节清单 + 逐章评分。点开作品即可看内容。

    v4.22.1：不再优先读「成书-*.md」——成书是构建产物，可能是旧版清洗逻辑生成的
    （含本章概要/修改说明/评分表），也可能在重写后过期。预览一律以章节文件为准，
    每章都过 writer._clean_chapter_text（正文纯净域）：章题 + 章首引子 + 正文。
    """
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    plan = proj.plan() or {}
    book_path = os.path.join(proj.path, f"成书-{plan.get('novelName', '')}.md")
    has_book = os.path.exists(book_path)
    parts, chapters_full = [], []
    for ch in plan.get("chapters", []):
        fp = _chapter_path(proj, ch)
        if not fp or not os.path.exists(fp):
            continue
        try:
            clean = writer._clean_chapter_text(open(fp, encoding="utf-8").read())
        except OSError:
            continue
        if not clean.strip():
            continue
        parts.append(clean)
        # 逐章原文（对话区「逐章直播打字机」一章一条气泡）：同样只给去噪正文
        chapters_full.append({"n": ch.get("chapterNumber"), "title": ch.get("title"),
                              "status": ch.get("status"), "text": clean})
    text = "\n\n---\n\n".join(parts)
    chapters = [{"n": c.get("chapterNumber"), "title": c.get("title"),
                 "status": c.get("status"), "words": c.get("wordCount"),
                 "continuation": bool(c.get("continuation"))}
                for c in plan.get("chapters", [])]
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
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    fp = os.path.join(proj.path, "13-修订记录.md")
    return {"key": key, "text": open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""}

@app.get("/api/quality")
def api_quality(key: str):
    """返回 14-质量报告.md（确定性引擎 + 五角色 + 百分制达标判定）；无则空。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    fp = os.path.join(proj.path, "14-质量报告.md")
    return {"key": key, "text": open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""}

@app.get("/api/version")
async def api_version():
    # v4.32.0：desktop 标志——前端据此决定导出走原生保存对话框还是浏览器下载
    return {"version": version.current(), "desktop": desktop_bridge.is_desktop()}

# ---------- v1.0.0：文体路由 + 技能库（一幕·YIMU 神笔文学创作台） ----------

@app.get("/api/mediums")
async def api_mediums():
    """四种文体的注册表：阶段表 / 评估维度 / 目录规范，前端据此渲染选择器。"""
    return {"mediums": medium_mod.registry()}


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
    syslog_mod.log_event("skill", "添加技能",
                         {"name": f"custom-{name}", "file": os.path.relpath(fpath, ROOT), "medium": media, "stages": stages},
                         f"添加自定义技能「{title}」")
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
    syslog_mod.log_event("skill", "删除技能", {"name": f"custom-{safe}"}, f"删除自定义技能「{safe}」")
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
    """建剧本项目（与 /api/create + medium=script 同一条路径）。"""
    return _create_script_project(payload)


@app.get("/api/script/status")
async def api_script_status(key: str):
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return proj.status()


@app.get("/api/script/bible")
async def api_script_bible(key: str):
    """创作圣经（S0 引擎 + 圣经头）。"""
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return {"key": key, "text": script_writer.read_file_part(key, "bible")}


@app.post("/api/script/audit")
def api_script_audit(payload: dict):
    """v3.3.0：剧本一致性体检（人物在场/跳场/重复/时代串代）→ 22-剧本体检.json。"""
    key = str(payload.get("key") or "")
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    from bot import script_state
    report = script_state.audit_script(proj.path)
    script_state.save_audit(proj.path, report)
    return {"ok": True, "key": key, **report}


@app.get("/api/script/audit")
def api_script_audit_get(key: str):
    """读取上次剧本体检结果（22-剧本体检.json）。"""
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    from bot import script_state
    report = script_state.load_audit(proj.path)
    return {"ok": True, "key": key, **report}


def _script_doc_generated(proj, tab: str) -> str:
    """v4.33.5：文档不存在时，按正文实体现场生成一份（零模型调用）。

    以前人物/结构/修订/质量/分镜五个页签在改编剧上永远是空的——因为这些
    markdown 只由「通读修订/评分/分镜」之类的模型任务顺手写出，改编剧不走那条链。
    但正文里其实已有 `场1：外 部落广场 白天` 和 `林野：台词`，抽出来就是可用的底稿。
    """
    from bot import script_entities as se
    plan = (proj.plan() if proj else None) or {}
    ext = se.extract(proj.path, plan)
    if tab == "chars":
        return se.chars_md(ext, plan)
    if tab == "struct":
        return se.struct_md(ext, plan)
    if tab == "quality":
        return se.quality_md(proj.path, ext, plan)
    if tab == "revisions":
        return se.revisions_md(proj.path, ext, plan)
    if tab == "storyboard":
        return se.storyboard_md(proj.path, ext, plan)
    return ""


def _script_doc(key: str, part: str) -> dict:
    """读文档；文件为空就退回生成版，并在返回体里标注 generated。"""
    proj = script_writer.get_project(key)
    text = script_writer.read_file_part(key, part) or ""
    if text.strip():
        return {"text": text, "generated": False}
    try:
        gen = _script_doc_generated(proj, part)
    except Exception as e:
        print(f"[script] 生成 {part} 视图失败: {e.__class__.__name__}: {e}", flush=True)
        gen = ""
    return {"text": gen, "generated": bool(gen)}


@app.get("/api/script/chars")
async def api_script_chars(key: str):
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return {"key": key, **_script_doc(key, "chars")}


@app.get("/api/script/struct")
async def api_script_struct(key: str):
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return {"key": key, **_script_doc(key, "struct")}


def _script_scenes_or_reparse(proj, key=""):
    """v3.1.0：plan.scenes 为空但 03-场次清单.md 有内容时，现场解析并回填计划。
    旧剧本常因场次表格式解析不出而永远「没有场次」。返回 scenes 列表。"""
    plan = proj.plan() or {}
    scenes = plan.get("scenes") or []
    if scenes:
        return scenes
    try:
        md = script_writer.read_file_part(key or os.path.basename(proj.path), "scenes") or ""
    except Exception:
        md = ""
    parsed = script_writer.parse_scenes(md) if md else []
    if parsed:
        for i, sc in enumerate(parsed, 1):
            sc.setdefault("no", i)
            sc.setdefault("status", "pending")
            sc.setdefault("fileName", f"第{sc['no']:03d}场.md")
            sc.setdefault("wordCount", None)
        plan["scenes"] = parsed
        # v4.23.2：这里原先是 `try: save_plan(...); log(...) except Exception: pass`。
        # 两个问题：① 写盘失败完全无声，用户下次打开又要重解析一遍，日志里却什么都没留；
        # ② log 也在 try 里，写盘没成功也会记一条「已回填」——日志说做了，磁盘上没做。
        # 现在写盘失败打一条带异常类型的 stderr（接口仍返回解析结果，用户至少能看场次），
        # 只有真的落盘成功才记「已回填」。CI 上 test_script_scenes_reparse_fallback
        # 偶发「接口对、磁盘没有」，就是被这个 pass 盖住的。
        try:
            proj.save_plan(plan)
        except Exception as e:
            print(f"[script] 场次回填写盘失败（{key}）：{e.__class__.__name__}: {e}", flush=True)
        else:
            proj.log(f"🧭 场次清单重新解析成功：{len(parsed)} 场（原计划里缺失，已回填）")
        return parsed
    return []


@app.get("/api/script/scenes")
async def api_script_scenes(key: str):
    """场次清单（表格 + 解析后的结构化场次）。

    v4.33.5：改编剧的计划里一集就是一场，`place` 是占位「第 N 集」。
    这里返回**副本**并补上正文抽到的真实地点/出场人物，让场次卡片能显示
    「部落广场、木屋」而不是「第 1 集」；计划文件本身不动。
    """
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    scenes = _script_scenes_or_reparse(proj, key)
    try:
        from bot import script_entities as se
        ext = se.extract(proj.path, proj.plan() or {})
        if ext.get("summary", {}).get("scenes"):
            enriched = []
            for sc in scenes:
                row = dict(sc)
                ep = sc.get("no")
                ps = se.places_of(ext, ep)
                cast = se.cast_of(ext, ep)
                if ps:
                    row["places"] = ps
                    if not str(row.get("place") or "").strip() or \
                            re.match(r"^第\s*\d+\s*集", str(row.get("place") or "").strip()):
                        row["place"] = "、".join(ps[:3]) + (f" 等 {len(ps)} 处" if len(ps) > 3 else "")
                if cast:
                    row["cast"] = "、".join(cast[:6]) + ("…" if len(cast) > 6 else "")
                    row["sceneCount"] = len(((ext.get("episodes") or {}).get(str(ep)) or {}).get("scenes") or [])
                enriched.append(row)
            scenes = enriched
    except Exception:
        pass
    return {"key": key, "text": script_writer.read_file_part(key, "scenes"),
            "scenes": scenes}


@app.get("/api/script/scene")
async def api_script_scene(key: str, no: int):
    """单场正文。"""
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return {"key": key, "no": no, "text": script_writer.read_scene(key, no)}


@app.get("/api/script/preview")
async def api_script_preview(key: str):
    """整剧预览：按场次顺序一次返回全部场次的标题/状态/字数/正文（省掉逐场 30 次往返）。

    场次标题由引擎的 scene_heading() 合成，未写的场次也占位返回 text=""，
    前端据此直接排版占位。written 为实际有正文的场次数，total 为计划总场数。
    """
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    plan = proj.plan() or {}
    scenes = []
    for sc in _script_scenes_or_reparse(proj, key):
        no = sc.get("no")
        text = script_writer.read_scene(key, no) if no is not None else ""
        scenes.append({
            "no": no,
            "heading": script_writer.scene_heading(sc),
            "status": sc.get("status", ""),
            "words": sc.get("wordCount") or 0,
            "text": text,
        })
    return {"key": key, "name": plan.get("scriptName", proj.name),
            "scenes": scenes,
            "written": sum(1 for s in scenes if s["text"].strip()),
            "total": len(scenes)}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/script/check")
async def api_script_check(payload: dict):
    """世界观一致性体检：按计划里的时代设定扫圣经/场次表/正文，列出违规词与位置。"""
    key = payload.get("key")
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    from bot import script_checker
    if not hasattr(script_checker, "check_project"):
        return JSONResponse({"error": "当前版本不支持剧本体检"}, status_code=501)
    try:
        return script_checker.check_project(key)
    except Exception as e:
        return JSONResponse({"error": f"体检失败：{e}"}, status_code=500)


@app.post("/api/script/rewrite-scenes")
async def api_script_rewrite_scenes(payload: dict):
    """按设定重写指定场次：先快照旧正文，再把场次状态重置为 pending 后交给写作引擎。"""
    import threading as _th
    from datetime import datetime as _dt
    key = payload.get("key")
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    try:
        nos = sorted({int(n) for n in (payload.get("sceneNos") or [])})
    except (TypeError, ValueError):
        return JSONResponse({"error": "sceneNos 必须是场次号数组"}, status_code=400)
    if not nos:
        return JSONResponse({"error": "请选择要重写的场次"}, status_code=400)

    def _bg():
        try:
            plan = proj.plan() or {}
            ts = _dt.now().strftime("%Y%m%d-%H%M%S")
            ver_dir = os.path.join(proj.body_dir(), ".versions")
            os.makedirs(ver_dir, exist_ok=True)
            targets = []
            for sc in plan.get("scenes") or []:
                no = int(sc.get("no") or 0)
                if no not in nos:
                    continue
                old = script_writer.read_scene(key, no)
                if old.strip():
                    atomic_write_text(os.path.join(ver_dir, f"第{no:03d}场-{ts}.md"), old)
                sc["status"] = "pending"
                sc.pop("warn", None)
                targets.append(no)
            if targets:
                proj.save_plan(plan)
                script_writer.write_scenes(proj, only=targets)
        except Exception as e:
            print(f"[bg] 剧本按设定重写失败: {e}", flush=True)

    _th.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "async": True, "key": key, "scenes": nos}


# ---------- v2.2.1：通用副驾（剧本 / 诗词 / 散文 同一套引擎与留档） ----------

def _assist_medium(payload: dict) -> str:
    med = str(payload.get("medium") or "script").strip()
    return med if med in ("script", "poetry", "prose", "novel", "thesis", "children", "news", "copy", "other") else "script"


def _assist_project(medium: str, key: str):
    if medium == "poetry":
        return poetry_writer.get_project(key)
    if medium == "prose":
        return prose_writer.get_project(key)
    if medium == "novel":
        return _get_project(key)
    if medium in ("children", "news", "copy", "other"):
        return _get_project(key)   # v4.12.1：8 文体对话改稿
    return script_writer.get_project(key)


@app.post("/api/assist/stream")
async def api_assist_stream(request: Request):
    """副驾对话：讨论 / 定位改写。改写只返回 diff（patch），落盘走 apply。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
    return await _assist_stream_response(payload)


async def _assist_stream_response(payload: dict):
    import asyncio
    from bot import assist as assist_mod

    payload = payload or {}
    key = str(payload.get("key") or "")
    message = str(payload.get("message") or "").strip()
    medium = _assist_medium(payload)
    if not key or not message:
        return JSONResponse({"error": "缺少作品或消息"}, status_code=400)
    if not _assist_project(medium, key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    sid = str(payload.get("session_id") or uuid.uuid4().hex)
    focus = payload.get("focus") or {}
    history = payload.get("history") or []

    async def gen():
        t0 = time.time()
        yield _sse("start", {"session_id": sid, "ts": int(t0)})
        yield _sse("status", {"stage": "thinking", "text": "正在定位要改的段落…"})
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()
        box: list = []

        def _work():
            try:
                if message.startswith("@"):
                    # v3.2.0：对话改稿里也能 @Agent（任意文体，带岗位技能与文体上下文）
                    box.append((True, {"ok": True, "reply": assist_mod.agent_reply(
                        key, medium, message, history), "intent": {"kind": "discussion"}}))
                else:
                    box.append((True, assist_mod.assist(
                        key, medium, message, focus=focus, history_msgs=history)))
            except BaseException as e:
                box.append((False, e))
            finally:
                try:
                    loop.call_soon_threadsafe(ready.set)
                except RuntimeError:
                    pass

        threading.Thread(target=_work, daemon=True).start()
        while not ready.is_set():
            try:
                await asyncio.wait_for(ready.wait(), timeout=2.0)
            except TimeoutError:  # v4.7.1 Python<3.11 捕不到会让改稿流报错
                yield _sse("status", {"stage": "thinking", "text": "仍在推敲改法…"})

        ok, value = box[0] if box else (False, RuntimeError("改稿未返回结果"))
        if not ok:
            err = f"改稿失败：{value}"
            yield _sse("error", {"error": err})
            yield _sse("done", {"ok": False, "error": err, "elapsed": round(time.time() - t0, 2)})
            return
        res = value if isinstance(value, dict) else {"ok": False, "reply": str(value or "")}
        text = str(res.get("reply") or "")
        for i in range(0, len(text), 28):
            yield _sse("delta", {"text": text[i:i + 28]})
            await asyncio.sleep(0.01)
        intent = res.get("intent") or {"kind": "discussion"}
        patch = res.get("patch")
        if patch and medium == "script":
            patch = {**patch, "scene": patch.get("doc")}
        # 对话留档：用户这句与 Agent 这句都进作品目录，随作品走
        assist_mod.log_chat(key, medium, "user", message, kind=intent.get("kind"),
                            doc=intent.get("doc"), block=intent.get("block"))
        if text.strip():
            assist_mod.log_chat(key, medium, "assistant", text.strip(),
                                kind=intent.get("kind"), doc=intent.get("doc"),
                                block=intent.get("block"))
        # v3.2.0：同步进主对话留档（chat_log.jsonl，type=assist）——主对话上下文与
        # 压缩摘要都能看到改稿讨论，会话页回读时完整可见
        from bot.v2api import _save_chat_msg as _save_msg
        _sync_proj = _get_project(key)
        if _sync_proj:
            _save_msg(_sync_proj, "user", message, {"type": "assist", "medium": medium})
            if text.strip():
                _save_msg(_sync_proj, "assistant", text.strip(),
                          {"type": "assist", "medium": medium,
                           "kind": intent.get("kind"), "doc": intent.get("doc"),
                           "block": intent.get("block")})
        yield _sse("done", {
            "ok": bool(res.get("ok", True)),
            "text": text,
            "intent": intent,
            "patch": patch,
            "session_id": sid,
            "elapsed": round(time.time() - t0, 2),
        })

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/assist/apply")
async def api_assist_apply(payload: dict):
    """把 patch 落盘：只改目标段，其余内容逐字不动。"""
    from bot import assist as assist_mod

    medium = _assist_medium(payload)
    key = str(payload.get("key") or "")
    if not _assist_project(medium, key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    try:
        block = int(payload.get("block"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "缺少段号"}, status_code=400)
    res = assist_mod.apply_edit(key, medium, payload.get("doc"), block,
                                str(payload.get("base_hash") or ""),
                                str(payload.get("new_text") or ""))
    if not res.get("ok"):
        return JSONResponse(res, status_code=409 if res.get("code") == "stale" else 400)
    return res


@app.get("/api/assist/versions")
async def api_assist_versions(key: str, medium: str = "script", doc: str = ""):
    from bot import assist as assist_mod

    med = medium if medium in ("script", "poetry", "prose") else "script"
    if not _assist_project(med, key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return assist_mod.list_versions(key, med, doc)


@app.post("/api/assist/restore")
async def api_assist_restore(payload: dict):
    from bot import assist as assist_mod

    medium = _assist_medium(payload)
    key = str(payload.get("key") or "")
    if not _assist_project(medium, key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    res = assist_mod.restore_version(key, medium, payload.get("doc"),
                                      str(payload.get("version") or ""))
    if not res.get("ok"):
        return JSONResponse(res, status_code=400)
    return res


@app.get("/api/assist/history")
async def api_assist_history(key: str, medium: str = "script"):
    """对话历史 + 改稿记录：随作品留档，换浏览器也还在。"""
    from bot import assist as assist_mod

    med = medium if medium in ("script", "poetry", "prose") else "script"
    if not _assist_project(med, key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return assist_mod.history(key, med)


# ---------- 旧路径兼容：/api/script/assist/* 走同一套通用副驾 ----------

@app.post("/api/script/assist/stream")
async def api_script_assist_stream(request: Request):
    """剧本副驾（兼容入口，与 /api/assist/stream 同源）。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
    payload = dict(payload or {})
    payload["medium"] = "script"
    focus = payload.get("focus") or {}
    if isinstance(focus, dict) and focus.get("scene") is not None and focus.get("doc") is None:
        focus = {**focus, "doc": str(focus.get("scene"))}
    payload["focus"] = focus
    return await _assist_stream_response(payload)


@app.post("/api/script/assist/apply")
async def api_script_assist_apply(payload: dict):
    payload = dict(payload or {})
    payload["medium"] = "script"
    if payload.get("doc") is None and payload.get("scene") is not None:
        payload["doc"] = str(payload.get("scene"))
    return await api_assist_apply(payload)


@app.get("/api/script/assist/versions")
async def api_script_assist_versions(key: str, scene: int):
    from bot import assist as assist_mod

    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    res = assist_mod.list_versions(key, "script", str(scene))
    if not res.get("ok"):
        return res
    return {"ok": True, "items": res.get("items") or []}


@app.post("/api/script/assist/restore")
async def api_script_assist_restore(payload: dict):
    payload = dict(payload or {})
    payload["medium"] = "script"
    if payload.get("doc") is None and payload.get("scene") is not None:
        payload["doc"] = str(payload.get("scene"))
    return await api_assist_restore(payload)


@app.post("/api/script/write")
async def api_script_write(payload: dict):
    """按场次清单写正文。payload: {key, only?: [场次号]}
    v1.2.2：改后台线程（写 30 场是分钟级 LLM 任务，async 内同步跑会阻塞事件循环）。"""
    import threading as _th
    key = payload.get("key")
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return {"key": key, **_script_doc(key, "revisions")}


@app.get("/api/script/quality")
async def api_script_quality(key: str):
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return {"key": key, **_script_doc(key, "quality")}


@app.post("/api/script/revise")
async def api_script_revise(payload: dict):
    """S6 通读修订 + 评分。v1.2.2：后台线程。"""
    import threading as _th
    key = payload.get("key")
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    from urllib.parse import quote as _q
    return Response(txt, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=\"script.txt\"; filename*=UTF-8''{_q(key)}.txt"})


@app.post("/api/script/storyboard")
def api_script_storyboard(payload: dict):
    """v4.5.0：把已写场次拆成分镜头脚本（后台线程）。payload: {key, sceneNo?}"""
    import threading as _th
    key = str(payload.get("key") or "")
    scene_no = payload.get("sceneNo")
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    def _bg():
        try:
            script_writer.gen_storyboard(key, only_scene=int(scene_no) if scene_no else None)
        except Exception as e:
            print(f"[bg] 分镜生成失败: {e}", flush=True)
    _th.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "async": True, "msg": "分镜拆解已后台启动"}


@app.get("/api/script/storyboard")
async def api_script_storyboard_get(key: str):
    """v4.5.0：分镜表数据（结构化 JSON）。v4.33.5：未生成时给一份「差什么、怎么触发」的说明。"""
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return {"key": key, **_script_doc(key, "storyboard"),
            **script_writer.storyboard_data(key)}


@app.post("/api/novel-to-drama")
def api_novel_to_drama(payload: dict):
    """v4.33.0：小说一键转 AI 短剧/漫剧剧本——**全书**分集改编 + 自动分镜。

    payload:
      key                     原小说 key
      form                    short_drama | comic_drama
      chapters                [章号]，省略 = 全书所有已完成章节（不再截断成 12 章）
      chapters_per_episode    每集覆盖几章，1-6，默认 2
      target_episodes         目标集数（优先于 chapters_per_episode）
      rebuild                 已有新版蓝图时是否重新改编（覆盖旧稿，旧稿会归档）

    v4.32.3 及以前是 ``chs[:12]``，一本 100 章 / 25 万字的小说只改编前 12 章，
    产出 12「场」不到 1 万字，还有 42 字的残片被记成已完成。现在按全书章节
    先生成分集蓝图，集数由用户选的密度决定，原书不动。
    """
    import threading as _th
    from bot import drama_adapter as da

    src_key = str(payload.get("key") or "")
    form = str(payload.get("form") or "short_drama")
    if form not in ("short_drama", "comic_drama"):
        return JSONResponse({"error": "form 只能是 short_drama/comic_drama"}, status_code=400)
    proj = _get_project(src_key)
    if not proj:
        return JSONResponse({"error": "小说不存在"}, status_code=404)
    plan = proj.plan() or {}
    chs = [c for c in (plan.get("chapters") or [])
           if c.get("status") == "completed" and _chapter_path(proj, c)]
    want = payload.get("chapters")
    if isinstance(want, list) and want:
        want_set = {int(x) for x in want if str(x).isdigit()}
        chs = [c for c in chs if c.get("chapterNumber") in want_set] or chs[:1]
    if not chs:
        return JSONResponse({"error": "没有已完成可改编的章节"}, status_code=400)
    settings = {
        "chapters_per_episode": da.clamp_int(payload.get("chapters_per_episode"), 1, 6,
                                          da.DEFAULT_CHAPTERS_PER_EPISODE),
        "target_episodes": da.clamp_int(payload.get("target_episodes"), 1, da.MAX_EPISODES,
                                     len(chs)) if payload.get("target_episodes") else None,
        "min_chars": da.clamp_int(payload.get("min_chars"), 200, 5000, da.MIN_EPISODE_CHARS),
    }
    outline = da.group_chapters(chs, settings["chapters_per_episode"],
                                settings["target_episodes"])
    new_key_base = f"{src_key}-{'漫剧' if form == 'comic_drama' else '短剧'}"
    hint = {
        "key_hint": new_key_base,
        "name_hint": f"{plan.get('novelName') or src_key}·{'漫剧' if form == 'comic_drama' else '短剧'}",
        "chapter_count": len(chs), "episode_count": len(outline),
        "chapters_per_episode": settings["chapters_per_episode"],
    }
    # 已有新版蓝图的成品不静默覆盖；旧版 12 集剧本（无 adaptation.version）则自动升级
    existing = script_writer.get_project(new_key_base)
    old_plan = (existing.plan() if existing else None) or {}
    if old_plan and not payload.get("rebuild"):
        if (old_plan.get("adaptation") or {}).get("version") == da.ADAPTER_VERSION:
            return {"ok": True, "async": False, "already_exists": True,
                    "msg": f"《{old_plan.get('scriptName') or new_key_base}》已存在"
                           f"（{len(old_plan.get('scenes') or [])} 集）。可在作品库打开，"
                           "或勾选「重新改编全书」覆盖旧稿（旧稿会归档，不删除）。",
                    **hint}
    # v4.32.3：防重入——连点两次「转短剧」不能起两条线程写同一份计划/正文
    if _adapt_busy(new_key_base) or writer.pipeline_running(new_key_base) or not _adapt_claim(new_key_base):
        return {"ok": True, "async": True, "already_running": True,
                "msg": "这部短剧的改编任务已在后台运行，进度见工作台实时日志。", **hint}

    def _run():
        try:
            _novel_to_drama_impl(src_key, form, chs, new_key_base, plan, proj,
                                 resume=False, outline=outline, settings=settings)
        except Exception as e:
            livejob.finish(new_key_base, ok=False, detail=f"改编启动失败：{type(e).__name__}",
                           error=str(e)[:300])
            print(f"[bg] 小说转短剧失败: {e}", flush=True)
        finally:
            _adapt_release(new_key_base)
    # v4.33.1：先登记 + 写日志，再起线程；「查看剧本」弹窗和左侧会话立刻有状态
    _job_start(new_key_base, kind="adapt",
               detail=f"正在按全书 {len(chs)} 章生成 {len(outline)} 集分集蓝图…",
               total=len(outline), done=0)
    try:
        _th.Thread(target=_run, daemon=True).start()
    except Exception as e:
        _adapt_release(new_key_base)
        livejob.finish(new_key_base, ok=False, detail=f"改编启动失败：{e}", error=str(e)[:300])
        raise
    return _job_payload(new_key_base, {"ok": True, "async": True,
            "msg": f"改编已后台启动：全书 {len(chs)} 章 → {len(outline)} 集"
                   f"（每集约 {settings['chapters_per_episode']} 章），进度实时可见",
            **hint}, started=True)


# ---------------------------------------------------------------- 小说 → 短剧改编
_ADAPT_LOCK = threading.Lock()
_ADAPT_RUNNING: set = set()


def _adapt_claim(key) -> bool:
    """进程内防重入：同一部短剧只能有一条改编/续跑线程在写盘。"""
    with _ADAPT_LOCK:
        if key in _ADAPT_RUNNING:
            return False
        _ADAPT_RUNNING.add(key)
        return True


def _adapt_release(key) -> None:
    with _ADAPT_LOCK:
        _ADAPT_RUNNING.discard(key)


def _adapt_busy(key) -> bool:
    with _ADAPT_LOCK:
        return key in _ADAPT_RUNNING


def _adapt_scene_body_path(pdir, sc) -> str:
    no = int(sc.get("no") or 0)
    fn = str(sc.get("fileName") or f"第{no:03d}场.md")
    return os.path.join(pdir, script_writer.F_BODY, fn)


def _adapt_write_scene_list(pdir, name, scenes) -> None:
    """v4.33.0：清单以「集」为单位，并标出每集覆盖的原著章节与实测字数。

    旧版把 1 章当 1「场」，12 行清单却对应每集内部好几个场景，用户看到的
    「只有 12 场」就是这么来的。
    """
    md = [f"# {name} 分集清单\n",
          "> 每行一集；首列沿用「第N场」编号，以兼容剧本工作台的场次解析。\n",
          "| 场次 | 集 | 覆盖原著章节 | 集名 | 字数 | 状态 |",
          "| :--- | :--- | :--- | :--- | :--- | :--- |"]
    for sc in scenes:
        nums = sc.get("chapters") or []
        span = (f"第 {nums[0]}-{nums[-1]} 章" if len(nums) > 1
                else (f"第 {nums[0]} 章" if nums else "—"))
        state = {"completed": "已完成", "failed": "待重跑"}.get(sc.get("status"), "待改编")
        md.append(f"| 第{sc['no']}场 | 第{sc['no']}集 | {span} | "
                  f"{sc.get('title') or sc.get('place') or ''} | "
                  f"{sc.get('wordCount') or 0} | {state} |")
    script_writer._write(os.path.join(pdir, script_writer.F_SCENES), "\n".join(md) + "\n")


def _adapt_archive_body(pdir, sc) -> None:
    """把不合格的旧集正文归档而不是删除（旧版把 42 字残片当成品）。"""
    import shutil
    fp = _adapt_scene_body_path(pdir, sc)
    if not os.path.exists(fp):
        return
    dst_dir = os.path.join(pdir, script_writer.F_BODY, "旧版备份")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(fp))
    if os.path.exists(dst):
        dst = dst + "." + str(int(os.path.getmtime(fp))) + ".bak"
    try:
        shutil.move(fp, dst)
    except Exception:
        pass


def _adapt_archive_all_bodies(pdir) -> int:
    """旧版剧本升级：把全部旧集正文归档，避免把「1 章 1 集」的旧稿误当新蓝图成果。"""
    import shutil
    src_dir = os.path.join(pdir, script_writer.F_BODY)
    if not os.path.isdir(src_dir):
        return 0
    dst_dir = os.path.join(src_dir, "旧版备份")
    os.makedirs(dst_dir, exist_ok=True)
    moved = 0
    for fn in sorted(os.listdir(src_dir)):
        if not fn.endswith(".md"):
            continue
        src, dst = os.path.join(src_dir, fn), os.path.join(dst_dir, fn)
        if os.path.exists(dst):
            dst += "." + str(moved) + ".bak"
        try:
            shutil.move(src, dst)
            moved += 1
        except Exception:
            pass
    return moved


def _adapt_backup_plan(pdir) -> None:
    import shutil
    fp = os.path.join(pdir, script_writer.F_PLAN)
    if os.path.exists(fp):
        try:
            shutil.copy2(fp, fp + ".旧版备份.json")
        except Exception:
            pass


def _novel_to_drama_impl(src_key, form, chapters, new_key_base, src_plan,
                         proj=None, resume=False, outline=None, settings=None,
                         only_episodes=None):
    """小说 → 短剧**全书**改编（后台线程）。

    v4.33.0 重做：先按全书章节生成分集蓝图（1~6 章/集），提炼一次创作圣经，
    然后逐集带着「全书设定 + 本集大纲 + 上一集结尾 + 覆盖章节全文」写作；
    每集必须过质量门禁才落盘，残片自动重试、仍不合格归档重跑。
    """
    from datetime import datetime as _dt
    from bot import llm as _llm
    from bot import drama_adapter as da

    cfg = {
        "chapters_per_episode": da.DEFAULT_CHAPTERS_PER_EPISODE,
        "target_episodes": None,
        "min_chars": da.MIN_EPISODE_CHARS,
        "min_headings": da.MIN_SCENE_HEADINGS,
        "min_lines": da.MIN_DIALOGUE_LINES,
        "max_attempts": da.MAX_ATTEMPTS,
        "source_budget": da.SOURCE_BUDGET,
    }
    cfg.update({k: v for k, v in (settings or {}).items() if v is not None})
    limits = {"min_chars": cfg["min_chars"], "min_headings": cfg["min_headings"],
              "min_lines": cfg["min_lines"]}
    name = re.sub(r'[\/:*?"<>|\s]+', "",
                  f"{src_plan.get('novelName') or src_key}·{'漫剧' if form == 'comic_drama' else '短剧'}")[:40]
    pdir = safe_join(script_writer.PROJECTS_DIR, new_key_base, label="项目 key")
    os.makedirs(pdir, exist_ok=True)
    sp = script_writer.ScriptProject(pdir, name)
    if proj is None:
        proj = _get_project(src_key)
    if proj is None:
        sp.log("💥 无法读取原小说项目，改编失败")
        return {"ok": False, "error": "原小说项目不存在"}

    disk_plan = sp.plan()
    legacy = bool(disk_plan) and (disk_plan.get("adaptation") or {}).get("version") != da.ADAPTER_VERSION
    prev = disk_plan if resume else None
    if legacy:
        _adapt_backup_plan(pdir)
        moved = _adapt_archive_all_bodies(pdir)
        sp.log(f"♻️ 检测到旧版改编（只取前 12 章 / 1 章 1 集、含残片）；"
               f"已归档 {moved} 份旧稿与旧计划，按全书重新分集改编")
        prev = None
    name = (prev or {}).get("scriptName") or name
    sp = script_writer.ScriptProject(pdir, name)
    script_writer.llm.book(new_key_base, "planning")

    material = da.collect_material(proj)
    # v4.33.1：准备阶段也要可见——读素材/生成圣经可能 1-2 分钟，不能是黑箱
    livejob.update(new_key_base, phase="prepare",
                   detail=f"已读取 {len(material['summaries'])} 章摘要，正在生成分集蓝图…",
                   total=0, done=0)
    if outline is None:
        outline = da.group_chapters(chapters, cfg["chapters_per_episode"], cfg["target_episodes"])
    if not outline:
        sp.log("💥 没有可分集的章节，改编失败")
        livejob.finish(new_key_base, ok=False, detail="没有可分集的章节")
        return {"ok": False, "error": "没有可分集的章节"}
    form_label = script_writer.FORMS.get(form, {}).get("label", "AI短剧")
    novel_name = str(src_plan.get("novelName") or src_key)
    source_nos = [int(c.get("chapterNumber")) for c in chapters
                  if str(c.get("chapterNumber") or "").isdigit()]
    sp.log(f"🎬 全书改编《{novel_name}》：{len(source_nos)} 章 → {len(outline)} 集"
           f"（每集约 {cfg['chapters_per_episode']} 章）")

    # 创作圣经只生成一次，之后每集都带同一份人物/世界观，避免逐集独立裸调
    bible_fp = os.path.join(pdir, script_writer.F_BIBLE)
    bible = "" if legacy else da.read_text(bible_fp)
    if not bible.strip():
        livejob.update(new_key_base, phase="bible",
                       detail="正在提炼全书创作圣经（约 1-2 分钟，只需一次）…")
        sp.log("📖 正在提炼全书创作圣经（人物/世界观/全书弧线，约 1-2 分钟）…")
        bible = da.build_bible(_llm, material, novel_name, form_label, log=sp.log)
        script_writer._write(bible_fp, bible)
        sp.log("📖 创作圣经已就绪，后续每集都按同一份设定写")
    script_writer._write(os.path.join(pdir, script_writer.F_SEASON),
                         da.outline_brief(outline, material["summaries"]))

    plan_doc = prev or {
        "version": 1, "scriptName": name, "projectPath": pdir, "medium": "script",
        "form": form, "formLabel": form_label,
        "entryPath": "adapt", "targetPages": 30,
        "createdAt": _dt.now().isoformat(), "stages": {"S0": {"done": True}},
        "config": {"form": form, "title": name,
                   "idea": f"由小说《{novel_name}》全书改编"},
    }
    plan_doc["adaptedFrom"] = {"key": src_key, "chapters": source_nos,
                               "totalChapters": len(source_nos)}
    plan_doc["adaptation"] = {
        "version": da.ADAPTER_VERSION,
        "chaptersPerEpisode": cfg["chapters_per_episode"],
        "targetEpisodes": cfg["target_episodes"],
        "minChars": cfg["min_chars"],
        "totalEpisodes": len(outline),
        "blueprint": [{"no": ep["no"], "chapters": ep["chapters"]} for ep in outline],
    }
    # v4.33.9：改编保真清单——关键设定/名场面/人物结局/主线承诺，写完全集后逐项验收
    if not (prev or {}).get("adaptation", {}).get("fidelity"):
        try:
            _fid = da.build_fidelity(_llm, material, novel_name, log=sp.log)
            plan_doc["adaptation"]["fidelity"] = _fid
            sp.log(f"📜 改编保真清单就绪：{len(_fid)} 项必须保留的要素")
        except Exception as _e:
            sp.log(f"⚠️ 保真清单生成失败（不影响改编）：{_e}")
    # 一开工就把全书集数占位写进计划：工作台立刻是 0/N 而不是 0/0
    by_no = {}
    for sc in (plan_doc.get("scenes") or []):
        try:
            n = int(sc.get("no"))
        except (TypeError, ValueError):
            continue
        by_no[n] = dict(sc)
    for ep in outline:
        n = ep["no"]
        sc = by_no.get(n) or {"no": n, "status": "pending", "wordCount": 0}
        sc.update({"kind": "集", "place": f"第 {n} 集", "chapters": ep["chapters"],
                   "title": ep["title"], "fileName": f"第{n:03d}场.md"})
        ok, why = da.inspect_existing_body(_adapt_scene_body_path(pdir, sc), **limits)
        if not ok and os.path.exists(_adapt_scene_body_path(pdir, sc)):
            _adapt_archive_body(pdir, sc)
            sc.update({"status": "pending", "wordCount": 0, "error": why})
        elif sc.get("status") != "completed":
            sc["status"] = "pending"
        by_no[n] = sc
    plan_doc["scenes"] = [by_no[n] for n in sorted(by_no)]
    plan_doc["totalScenes"] = len(plan_doc["scenes"])
    plan_doc["episodes"] = len(plan_doc["scenes"])
    plan_doc["status"] = "writing"
    plan_doc.setdefault("meta", {})["paused"] = False
    sp.save_plan(plan_doc)
    _adapt_write_scene_list(pdir, name, plan_doc["scenes"])

    ch_by_no = {}
    for c in chapters:
        try:
            ch_by_no[int(c.get("chapterNumber"))] = c
        except (TypeError, ValueError):
            continue
    total_eps = len(outline)
    # 续跑时从「已完成」起算：断点续跑第 8 集不能把进度条从 7/50 打回 0/50
    finished_eps = sum(
        1 for sc in by_no.values()
        if sc.get("status") == "completed"
        and os.path.exists(_adapt_scene_body_path(pdir, sc)))
    # v4.34.0：mark_pipeline 挪进 try——此前它在 try 外，6317 前抛异常时
    # finally 不执行，_PIPELINE_COUNT 永久 ≥1、livejob 行永久 active，
    # 这本书从此钉死在「创作中」，/api/continue 防重入守卫也永远短路。
    try:
        writer.mark_pipeline(new_key_base, True)   # 全程「创作中」，不是只有分镜阶段
        livejob.update(new_key_base, phase="writing", total=total_eps, done=finished_eps,
                       detail=f"全书 {total_eps} 集，已完成 {finished_eps} 集，开始/继续逐集写作…")
        run_outline = ([ep for ep in outline if ep["no"] in set(only_episodes)]
                       if only_episodes is not None else outline)
        for ep in run_outline:
            n = ep["no"]
            sc = by_no.get(n)
            if sc is None:
                continue
            if (sp.plan() or {}).get("meta", {}).get("paused"):
                sp.log("⏸ 收到暂停请求，停在本集之前；剩余集保留待续，点「继续」接着改编")
                break
            fp = _adapt_scene_body_path(pdir, sc)
            ok, _ = da.inspect_existing_body(fp, **limits)
            if sc.get("status") == "completed" and ok:
                continue   # 断点续跑：已合格的集不重复烧 LLM
            rows = [ch_by_no[x] for x in ep["chapters"] if x in ch_by_no]
            source = da.episode_source_text(proj, rows, cfg["source_budget"])
            if not source.strip():
                sp.log(f"⚠️ 第 {n} 集读不到原著章节正文，保留待续")
                sc["status"] = "failed"
                sp.save_plan(plan_doc)
                continue
            beat = da.episode_beat_text(ep, material["summaries"])
            prev_fp = _adapt_scene_body_path(pdir, by_no[n - 1]) if n - 1 in by_no else ""
            prev_tail = da.previous_episode_tail(prev_fp) if prev_fp else ""
            span = (f"{ep['chapters'][0]}-{ep['chapters'][-1]}"
                    if len(ep["chapters"]) > 1 else str(ep["chapters"][0]))
            sp.log(f"🎬 第 {n}/{total_eps} 集（原著第 {span} 章）：剧本化…")
            livejob.update(new_key_base, phase="writing", total=total_eps, done=finished_eps,
                           detail=f"正在写第 {n}/{total_eps} 集（原著第 {span} 章，约 1-2 分钟）…")
            messages = da.build_episode_messages(
                bible=bible, ep=ep, total=len(outline), source=source, beat=beat,
                prev_tail=prev_tail, form_label=form_label, novel_name=novel_name,
                **limits)
            body, why, tries = da.generate_episode(
                _llm, messages, max_attempts=cfg["max_attempts"], **limits)
            if body:
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                script_writer._write(fp, body)
                sc.update({"status": "completed", "wordCount": len(body),
                           "attempts": tries, "error": ""})
                sc.pop("rewriting", None)   # v4.33.4：重写完成后清标记，恢复正常防回退
                sp.log(f"✅ 第 {n} 集完成：{len(body)} 字"
                       + (f"（第 {tries} 稿通过）" if tries > 1 else ""))
            else:
                sc.update({"status": "failed", "wordCount": 0, "error": why})
                sp.log(f"⚠️ 第 {n} 集未通过质量门禁：{why}；保留待续，点「继续」重跑")
            plan_doc["scenes"] = [by_no[k] for k in sorted(by_no)]
            sp.save_plan(plan_doc)                       # 每集实时落盘
            _adapt_write_scene_list(pdir, name, plan_doc["scenes"])
            # v4.33.3：原来成败都 +1，进度会比实际高；改成按落盘后的真实状态数
            finished_eps = sum(1 for x in by_no.values()
                               if x.get("status") == "completed")
            livejob.update(new_key_base, done=finished_eps, total=total_eps,
                           detail=f"已完成 {finished_eps}/{total_eps} 集，继续下一集…")

        pending = [sc for sc in by_no.values()
                   if sc.get("status") != "completed"
                   or not os.path.exists(_adapt_scene_body_path(pdir, sc))]
        done = len(by_no) - len(pending)
        if pending:
            plan_doc["status"] = "writing"
            plan_doc.setdefault("meta", {})["storyboard_done"] = False
            sp.save_plan(plan_doc)
            sp.log(f"⏸ 已完成 {done}/{len(by_no)} 集；还有 {len(pending)} 集未完成，点「继续」从断点接着改编")
            livejob.finish(new_key_base, ok=False,
                           detail=f"本轮完成 {done}/{len(by_no)} 集，还有 {len(pending)} 集待写")
            return {"ok": False, "done": done, "pending": len(pending)}

        sp.log(f"✅ 全书 {done} 集改编完成")
        storyboard_ok = False
        # 大书（>24 集）不在后台一次性跑几十次分镜调用，避免流水线卡死；
        # 正文已完成即可交付，分镜可在工作台「分镜」页签逐集生成或点「继续」补跑。
        if len(by_no) <= 24:
            try:
                sb = script_writer.gen_storyboard(new_key_base) or {}
                storyboard_ok = bool(sb.get("ok"))
                if not storyboard_ok:
                    sp.log(f"⚠️ 分镜未完成：{sb.get('error') or '未知原因'}（点「继续」可补跑）")
            except Exception as e:
                sp.log(f"⚠️ 分镜失败：{type(e).__name__}: {e}（点「继续」可补跑）")
        else:
            sp.log("ℹ️ 集数较多，分镜按集在工作台「分镜」页签生成；正文改编已全部完成")
        plan_doc = sp.plan() or plan_doc   # gen_storyboard 可能已写 meta，别覆盖
        plan_doc.setdefault("meta", {})["storyboard_done"] = bool(storyboard_ok)
        plan_doc["status"] = "completed"   # 正文完成即完成，不再被分镜阻塞
        plan_doc["scenes"] = [by_no[k] for k in sorted(by_no)]
        sp.save_plan(plan_doc)
        livejob.finish(new_key_base, ok=True, detail=f"全书 {done} 集改编完成")
        return {"ok": True, "done": done, "pending": 0}
    except Exception as e:
        livejob.finish(new_key_base, ok=False, detail=f"改编中断：{type(e).__name__}",
                       error=str(e)[:300])
        _job_log(new_key_base, f"💥 改编中断：{type(e).__name__}: {e}")
        raise
    finally:
        # 兜底：线程异常退出没走到显式 finish 时，不能让前端永远转圈
        if livejob.is_active(new_key_base):
            livejob.finish(new_key_base, ok=False, detail="改编线程已结束，点「继续」可续跑")
        writer.mark_pipeline(new_key_base, False)


def _storyboard_resume(key) -> None:
    """改编集数已齐、只差分镜时的补跑（用户点「继续」）。"""
    sp = script_writer.get_project(key)
    if sp is None:
        return
    writer.mark_pipeline(key, True)
    try:
        res = script_writer.gen_storyboard(key) or {}
        ok = bool(res.get("ok"))
        p = sp.plan() or {}
        p.setdefault("meta", {})["storyboard_done"] = ok
        p["status"] = "completed" if ok else "writing"
        sp.save_plan(p)
        sp.log("✅ 分镜表已补齐" if ok else f"⚠️ 分镜仍未完成：{res.get('error') or ''}（可再点继续）")
        livejob.finish(key, ok=ok,
                       detail="分镜表已补齐" if ok else f"分镜未完成：{res.get('error') or '可再点继续'}")
    except Exception as e:
        sp.log(f"⚠️ 分镜补跑失败：{type(e).__name__}: {e}")
        livejob.finish(key, ok=False, detail=f"分镜补跑失败：{type(e).__name__}", error=str(e)[:300])
    finally:
        writer.mark_pipeline(key, False)


def _blueprint_to_outline(blueprint: list, src_plan: dict) -> list[dict]:
    """把计划里存的精简蓝图还原成带章名的大纲对象。"""
    titles = {}
    for c in (src_plan.get("chapters") or []):
        try:
            titles[int(c.get("chapterNumber"))] = str(c.get("title") or "")
        except (TypeError, ValueError):
            continue
    out = []
    for ep in blueprint or []:
        try:
            no = int(ep.get("no"))
        except (TypeError, ValueError):
            continue
        nums = []
        for x in ep.get("chapters") or []:
            try:
                nums.append(int(x))
            except (TypeError, ValueError):
                continue
        if not nums:
            continue
        out.append({"no": no, "chapters": nums,
                    "titles": [titles.get(n) or f"第{n}章" for n in nums],
                    "title": titles.get(nums[0]) or f"第{nums[0]}章"})
    return sorted(out, key=lambda x: x["no"])


def _resume_novel_to_drama(key):
    """改编剧本的「继续」唯一入口；不是改编剧本返回 None 交给常规剧本引擎。

    v4.33.0：两种续跑
    * 旧版剧本（无 adaptation.version / 只覆盖前 12 章）：自动升级成全书蓝图，
      归档旧稿后按全书重跑，不再让用户对着「12 场」和 42 字残片点继续；
    * 新版剧本：按「状态 + 磁盘正文 + 质量门禁」对账，只重跑不合格的集，
      原书后来又写完的新章也会追加成新集；集齐了但分镜没生成就只补分镜。
    """
    from bot import drama_adapter as da

    sp = script_writer.get_project(key)
    if sp is None:
        return None
    plan = sp.plan() or {}
    src = plan.get("adaptedFrom") or {}
    src_key = str(src.get("key") or "")
    if not src_key:
        return None
    if _adapt_busy(key) or writer.pipeline_running(key):
        _job_start(key, kind="adapt", detail="改编任务正在后台运行，进度见运行日志…")
        return _job_payload(key, {"ok": True, "msg": "改编任务正在后台运行，进度见工作台实时日志。",
                "running": True, "paused": False, "state": "in_progress"}, started=False)
    src_proj = _get_project(src_key)
    if src_proj is None:
        _job_fail(key, "原小说项目已不存在，无法续跑改编")
        return _job_payload(key, {"ok": False, "msg": "原小说项目已不存在，无法续跑改编",
                "running": False, "paused": False, "state": "writing"}, started=False)
    src_plan = src_proj.plan() or {}
    all_ch = [c for c in (src_plan.get("chapters") or [])
              if c.get("status") == "completed" and _chapter_path(src_proj, c)]
    if not all_ch:
        _job_fail(key, "原书没有已完成的章节，无法续跑改编")
        return _job_payload(key, {"ok": False, "msg": "原书没有已完成的章节，无法续跑改编",
                "running": False, "paused": False, "state": "writing"}, started=False)
    adaptation = plan.get("adaptation") or {}
    legacy = adaptation.get("version") != da.ADAPTER_VERSION
    limits = {"min_chars": int(adaptation.get("minChars") or da.MIN_EPISODE_CHARS),
              "min_headings": da.MIN_SCENE_HEADINGS, "min_lines": da.MIN_DIALOGUE_LINES}
    missing: set = set()
    for sc in (plan.get("scenes") or []):
        if not isinstance(sc, dict):
            continue
        try:
            n = int(sc.get("no") or 0)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        ok, _why = da.inspect_existing_body(_adapt_scene_body_path(sp.path, sc), **limits)
        if sc.get("status") != "completed" or not ok:
            missing.add(n)

    if legacy:
        outline = da.group_chapters(all_ch, da.DEFAULT_CHAPTERS_PER_EPISODE, None)
        only = None                          # None = 全部集重跑
        msg = (f"检测到旧版改编（{len(plan.get('scenes') or [])} 集，未覆盖全书）。"
               f"已升级为全书 {len(outline)} 集蓝图，旧稿归档后重新改编")
    else:
        outline = _blueprint_to_outline(adaptation.get("blueprint"), src_plan)
        if not outline:
            outline = da.group_chapters(all_ch, da.DEFAULT_CHAPTERS_PER_EPISODE, None)
        covered = {n for ep in outline for n in ep["chapters"]}
        fresh = [c for c in all_ch if int(c.get("chapterNumber") or 0) not in covered]
        if fresh:
            extra = da.group_chapters(fresh, da.DEFAULT_CHAPTERS_PER_EPISODE, None)
            start = max((ep["no"] for ep in outline), default=0)
            for i, ep in enumerate(extra, 1):
                ep["no"] = start + i
                outline.append(ep)
                missing.add(ep["no"])
        only = sorted(missing)
        msg = (f"已从断点续跑改编：{len(only)} 集需要重写/补写" if only
               else "集数已齐，正在补跑分镜头拆解")

    storyboard_done = bool((plan.get("meta") or {}).get("storyboard_done"))
    if not legacy and not only and storyboard_done:
        if plan.get("status") != "completed":
            plan["status"] = "completed"
            sp.save_plan(plan)
        _job_done(key, "改编与分镜都已完成")
        return _job_payload(key, {"ok": True, "msg": "改编与分镜都已完成。", "running": False,
                "paused": False, "state": "completed"}, started=False)
    if not _adapt_claim(key):
        _job_start(key, kind="adapt", detail="改编任务正在后台运行，进度见运行日志…")
        return _job_payload(key, {"ok": True, "msg": "改编任务正在后台运行，进度见工作台实时日志。",
                "running": True, "paused": False, "state": "in_progress"}, started=False)

    # v4.33.1：起线程前就登记 + 写日志。准备素材 / 提炼创作圣经可能要 1-2 分钟，
    # 这段时间界面必须显示「正在准备全书素材」，而不是一片死寂。
    _job_start(key, kind="adapt",
               detail=("正在按全书重建分集蓝图…" if legacy
                       else (f"正在续跑改编：{len(only)} 集待写…" if only else "正在补跑分镜头拆解…")),
               total=(len(outline) if outline else 0),
               done=max(0, (len(outline) - len(only)) if outline and only else 0))

    def _run():
        try:
            if legacy or only:
                plan.setdefault("meta", {})["paused"] = False
                if plan.get("status") == "paused":
                    plan["status"] = "in_progress"
                sp.save_plan(plan)
                _novel_to_drama_impl(src_key, str(plan.get("form") or "short_drama"),
                                     all_ch, key, src_plan, src_proj, resume=True,
                                     outline=outline,
                                     settings={"min_chars": limits["min_chars"]},
                                     only_episodes=only)
            else:
                _storyboard_resume(key)
        except Exception as e:
            sp.log(f"💥 断点续跑失败：{type(e).__name__}: {e}")
            livejob.finish(key, ok=False, detail=f"续跑失败：{type(e).__name__}", error=str(e)[:300])
            print(f"[bg] 小说转短剧断点续跑失败: {e}", flush=True)
        finally:
            _adapt_release(key)
    threading.Thread(target=_run, daemon=True).start()
    return _job_payload(key, {"ok": True, "msg": msg, "running": True, "paused": False,
            "state": "in_progress"}, started=True)


def _adapt_resumer_for_engine(key):
    """v4.33.6：把改编续跑注册进剧本引擎。

    script_writer.resume_project 是「继续 / 极速续写 / 总编辑自动恢复」的共同落点，
    改编剧必须在这里就被接走，否则会掉进 write_scenes 的守卫，只留一行「已拒绝」。
    """
    return _resume_novel_to_drama(key) is not None


script_writer.set_adapt_resumer(_adapt_resumer_for_engine)



@app.get("/api/script/forms")
async def api_script_forms():
    """媒介形式与入口路径选项。"""
    return {"forms": [{"key": k, "label": v["label"], "target": v["target_pages"],
                       "unit": v["unit"]} for k, v in script_writer.FORMS.items()],
            "entryPaths": [{"key": k, "label": v} for k, v in script_writer.ENTRY_PATHS.items()]}


# ---------- v4.6.0：论文流水线 ----------

@app.get("/api/thesis/status")
async def api_thesis_status(key: str):
    proj = thesis_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return proj.status()


@app.get("/api/thesis/part")
async def api_thesis_part(key: str, part: str = "final"):
    """论文文档：outline/refs/report/final 或 正文章（如 part=ch1）。"""
    proj = thesis_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    mapping = {"outline": thesis_writer.F_OUTLINE, "refs": thesis_writer.F_REFS,
               "report": thesis_writer.F_REPORT, "final": thesis_writer.F_FINAL}
    if part in mapping:
        fp = os.path.join(proj.path, mapping[part])
        text = open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
    elif part.startswith("ch") and part[2:].isdigit():
        plan = proj.plan() or {}
        no = int(part[2:])
        ch = next((c for c in (plan.get("outline") or {}).get("chapters") or [] if int(c.get("no") or 0) == no), None)
        if not ch:
            return JSONResponse({"error": "章节不存在"}, status_code=404)
        fp = os.path.join(proj.path, thesis_writer.F_BODY_DIR,
                          f"{no:02d}-{thesis_writer._safe(ch.get('title'))}.md")
        text = open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
    else:
        return JSONResponse({"error": "未知 part"}, status_code=400)
    return {"key": key, "part": part, "text": text}


# v4.34.0：向导 AI 生成的题材描述——键为 skillkit 归一 key，覆盖 16 词题材表。
# 「悬疑/刑侦」「历史/民国」「现言」「古言/宫斗」等复合词经 _genre_key 归一后命中。
_AI_GENRE_DESCS = {
    "mystery": "悬疑推理，节奏紧凑，层层剥茧",
    "xuanhuan": "玄幻修真，宏大世界观，升级体系",
    "romance": "都市言情，细腻情感，人物羁绊",
    "scifi": "硬科幻设定，未来推演",
    "history": "历史/民国，权谋博弈",
    "urban": "都市生活，职场或情感",
    "wuxia": "武侠江湖，恩怨情仇",
    "horror": "恐怖/灵异，心理惊悚",
    "xianxia": "仙侠修仙，渡劫飞升",
    "military": "军旅战争，热血报国",
    "workplace": "职场行业，商战沉浮",
}


@app.post("/api/ai-generate")
def api_ai_generate(payload: dict):
    """v4.15.1：向导「AI 自动生成」——按体裁+模式+已选参数定制 prompt，随机种子保证每次不同。
    v4.34.0：修复第一步参数失效——①题材归一改走 skillkit._genre_key（此前 8 词旧词表
    精确匹配，16 词题材 13 个退化成「网络」）；②补读前端 style/style_id/genre_key，
    第一步选定的写作风格终于进第二步；③剧本模板补 {genre_desc} 槽位、form_map 与
    script_writer.FORMS 同源（此前有幽灵键 animation、缺 short/stage/opera/comic_drama）；
    ④随机维度块降级为「前提下的发散」，不再以「必须严格围绕」压制题材/基调/风格。"""
    medium = str(payload.get("medium") or "novel")
    field = str(payload.get("field") or "title")   # title 或 idea
    hint = str(payload.get("hint") or "")           # 用户已输入的线索
    # v4.15.1：从 payload 提取已选参数（题材/基调/视角/模式等），喂给 AI 定制生成
    genre = str(payload.get("genre") or "")
    genre_key = str(payload.get("genre_key") or "")
    tone = str(payload.get("tone") or "")
    perspective = str(payload.get("perspective") or "")
    mode = str(payload.get("mode") or "")
    ctype = str(payload.get("ctype") or "")
    ntype = str(payload.get("ntype") or "")
    ktype = str(payload.get("ktype") or "")
    template = str(payload.get("template") or "")
    form = str(payload.get("form") or "")
    # v4.34.0：第一步的写作风格（风格维度覆盖 + 蒸馏风格卡）
    style_obj = payload.get("style") if isinstance(payload.get("style"), dict) else {}
    style_id = str(payload.get("style_id") or "")

    # v4.34.0：题材描述表——键为 skillkit 归一 key（与向导下发 novel_genre_options 同源），
    # 未命中回落用户原文，绝不再退化成「网络」。
    def _genre_desc() -> str:
        g = genre or genre_key
        if not g:
            return ""
        try:
            key = skillkit._genre_key(g)
        except Exception:
            key = ""
        return _AI_GENRE_DESCS.get(key) or f"「{g}」题材"

    # 每个文体专属的生成指令模板（比旧版更细，结合体裁+模式）
    templates = {
        "novel": {
            "gen": "为这部{genre_desc}小说起一个有辨识度的书名，并写一句话故事概念（谁/想要什么/被什么挡住）",
        },
        "script": {
            "gen": "为这部{genre_desc}{form_desc}剧本起一个剧名，并写一句话故事概念（核心冲突 + 视觉钩子）",
            # v4.34.0：与 script_writer.FORMS 同源（电影长片/剧集/短片/舞台剧/戏曲/AI短剧/AI漫剧/自定义）
            "form_map": {k: v.get("label", k) for k, v in script_writer.FORMS.items()},
        },
        "poetry": {
            "gen": "为这首诗词起一个诗题，并写创作方向（意象 + 情感基调 + 韵脚建议）",
        },
        "prose": {
            "gen": "为这篇散文起一个标题，并写写作方向（核心意象 + 文体风格 + 篇幅建议）",
        },
        "thesis": {
            "gen": "为这篇论文起一个研究题目，并写研究方向（研究问题 + 方法 + 预期贡献）",
        },
        "children": {
            "gen": "为这篇{ctype}起一个标题，并写故事概念（主角 + 核心冲突 + 教育意义）",
            "ctype_map": {"fable": "寓言故事", "picture": "绘本故事", "scifi": "儿童科幻", "science": "科学故事", "bedtime": "睡前故事"},
        },
        "news": {
            "gen": "为这篇{ntype}起一个标题，并写核心事件概述（5W1H 精简版）",
            "ntype_map": {"product": "产品发布", "official": "官方公告", "event": "活动通稿", "corporate": "企业动态"},
        },
        "copy": {
            "gen": "为这个{ktype}起一个主题，并写创意方向（钩子 + 卖点 + 受众）",
            "ktype_map": {"voice": "口播稿", "planting": "种草笔记", "promo": "宣传文案", "marketing": "营销长文"},
        },
        "other": {
            "gen": "为这个{template}起一个标题，并写内容概要（核心主题 + 结构方向）",
            "template_map": {"script_kill": "剧本杀", "podcast": "播客脚本", "course": "课程稿", "speech": "演讲稿", "book_review": "拆书稿", "review": "影评"},
        },
    }

    tpl = templates.get(medium, templates["novel"])
    base = tpl["gen"]

    # 替换模板中的占位符
    if medium == "novel":
        base = base.replace("{genre_desc}", _genre_desc() or "网络")
    elif medium == "script":
        # v4.34.0：剧本题材此前被读入但模板无槽位，100% 丢失——现在拼回。
        base = base.replace("{genre_desc}", f"{genre}题材" if genre else "")
        base = base.replace("{form_desc}", tpl["form_map"].get(form, "影视剧本"))
    elif medium == "children":
        base = base.replace("{ctype}", tpl["ctype_map"].get(ctype, "儿童故事"))
    elif medium == "news":
        base = base.replace("{ntype}", tpl["ntype_map"].get(ntype, "新闻稿"))
    elif medium == "copy":
        base = base.replace("{ktype}", tpl["ktype_map"].get(ktype, "文案"))
    elif medium == "other":
        base = base.replace("{template}", tpl["template_map"].get(template, "其他内容"))

    # 已选参数附加到 prompt（不传时 AI 会自由发挥）
    extras = []
    if genre and medium not in ("novel", "script"):
        extras.append(f"题材：{genre}")
    if tone: extras.append(f"基调：{tone}")
    if perspective: extras.append(f"视角：{perspective}")
    if mode: extras.append(f"创作模式：{mode}")
    if style_obj:
        parts = [f"{k}：{v}" for k, v in style_obj.items() if str(v or "").strip()]
        if parts: extras.append("写作风格（必须遵循）：" + "，".join(parts))
    if style_id:
        sp = _style_prompt_of(style_id)
        if sp: extras.append(f"文风技法参考：{sp[:300]}")
    if extras:
        base += "。" + "，".join(extras)

    hint_p = f"\n用户已输入的线索（在此基础上发散，不要照抄）：{hint}" if hint.strip() else ""
    # v4.15.4：结构化随机——每次从维度池抽 2-3 个硬约束，强制输出方向不同。
    # 此前只拼一串数字种子，模型直接忽略 → 每次输出趋同。
    # v4.34.0：措辞从「必须严格围绕以下维度创作」降级为「前提下发散」——
    # 原措辞不含任何用户参数却优先级最高，题材/基调/风格全被它稀释。
    import random
    _DIMS = {
        "主角身份": ["落魄的外卖员", "退休刑警", "天才少女", "中年下岗工人", "海归精英", "乡村教师",
                  "夜班保安", "退役运动员", "小餐馆老板", "刚毕业的大学生", "单亲父亲", "直播主播"],
        "时代背景": ["九十年代小城", "近未来都市", "民国老上海", "当代一线城市", "架空古代王朝",
                  "末世废土", "深海科研站", "边陲小镇"],
        "核心冲突": ["身份错位引发的连环误会", "亲情与利益的撕扯", "一段被掩埋的旧案",
                  "阶层跃迁的代价", "信任崩塌与重建", "理想主义撞上现实规则"],
        "情绪基调": ["冷峻克制", "温暖治愈", "荒诞幽默", "悲怆厚重", "轻快明亮", "悬疑紧张"],
    }
    _picked = random.sample(list(_DIMS.keys()), k=3)
    _rand_block = "；".join(f"{k}可从「{random.choice(_DIMS[k])}」一类取材" for k in _picked)
    _seed = f"随机批次号：{random.randint(100000, 999999)}（同批次内容禁止重复）"

    out = llm.chat_json([{"role": "user", "content":
        f"{base}。{hint_p}\n"
        f"在上述题材、基调与风格的前提下展开创作，另用以下维度保证每次输出各不相同（不得与上述设定冲突）：{_rand_block}。\n{_seed}\n"
        '输出 JSON：{"title":"标题/题目","idea":"一句话概念或方向(50-100字)"}。只输出 JSON。'}],
        stage="planning", temperature=1.0, use_cache=False)
    return {"ok": True, "title": (out or {}).get("title", ""), "idea": (out or {}).get("idea", "")}


@app.post("/api/plan/regen")
def api_plan_regen(payload: dict):
    """v4.12.13：非小说重规划——清规划阶段→重跑（Agent 技能通过 book-skills 注入）。"""
    import threading as _th
    key = str(payload.get("key") or "")
    prefix_map = {"c_": "children_writer", "n_": "news_writer", "k_": "copy_writer",
                  "s_": "script_writer", "p_": "poetry_writer", "e_": "prose_writer",
                  "t_": "thesis_writer"}
    mod_name = prefix_map.get(str(key)[:2])
    if not mod_name:
        return JSONResponse({"error": "不支持的文体"}, status_code=400)
    import importlib
    mod = importlib.import_module(f"bot.{mod_name}")
    proj = mod.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    # 清规划阶段（保留正文/定稿）
    plan = proj.plan() or {}
    cs = plan.get("chapterStatus") or {}
    planning_codes = {"C1", "N1", "K1", "K2", "T1"}
    for code in planning_codes:
        cs.pop(code, None)
    plan["chapterStatus"] = cs
    plan["status"] = "in_progress"
    proj.save_plan(plan)
    proj.log("🔄 重规划：清规划阶段，重新生成…")
    def _bg():
        writer.mark_pipeline(key, True)
        try:
            mod.resume_project(key)
        except Exception as e:
            print(f"[bg] 重规划失败: {e}", flush=True)
        finally:
            writer.mark_pipeline(key, False)
    _th.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "msg": "重规划已后台启动"}


@app.get("/api/mini/status")
async def api_mini_status(key: str):
    """v4.12.1：children/news/copy 通用状态端点。"""
    for mod in (children_writer, news_writer, copy_writer, other_writer):
        proj = mod.get_project(key)
        if proj:
            return proj.status()
    return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)


@app.get("/api/mini/part")
async def api_mini_part(key: str, part: str = "final"):
    """v4.12.1：children/news/copy/other 通用文档端点。

    v4.33.5：补两个必然 404 的页签——儿童文学的「故事」是 02-故事/ 目录，
    文案的「钩子」是 01-钩子库.md（此前只有字面量、没有映射）。
    这两处以前点开就是「part 不存在」，用户看到的是永远空白。
    """
    for mod in (children_writer, news_writer, copy_writer, other_writer):
        proj = mod.get_project(key)
        if proj:
            # part 可以是 "outline"/"final" 或文件名
            cls = type(proj)
            mapping = {}
            if hasattr(cls, 'F_OUTLINE'):
                mapping["outline"] = cls.F_OUTLINE
            if hasattr(cls, 'FINAL_FILE'):
                mapping["final"] = cls.FINAL_FILE
            if hasattr(cls, 'F_REPORT'):
                mapping["report"] = cls.F_REPORT
            if hasattr(cls, 'F_INTERACTIVE'):
                mapping["interactive"] = cls.F_INTERACTIVE
            if hasattr(cls, 'F_TITLE_TAGS'):
                mapping["titles"] = cls.F_TITLE_TAGS
            if hasattr(cls, 'F_CHECK'):
                mapping["check"] = cls.F_CHECK
            if hasattr(cls, 'F_HOOKS'):
                mapping["hooks"] = cls.F_HOOKS
            # 「故事」页签：把 02-故事/ 下的章节按文件名顺序拼成一份可读正文
            if part == "story" and hasattr(cls, 'BODY_DIR'):
                body_dir = os.path.join(proj.path, cls.BODY_DIR)
                parts = []
                if os.path.isdir(body_dir):
                    for fn in sorted(os.listdir(body_dir)):
                        if fn.endswith(".md"):
                            try:
                                txt = open(os.path.join(body_dir, fn), encoding="utf-8").read()
                            except Exception:
                                continue
                            parts.append(f"## {fn[:-3]}\n\n{txt.strip()}")
                return {"key": key, "part": part, "text": "\n\n".join(parts)}
            fp = None
            known = part in mapping
            if known:
                fp = os.path.join(proj.path, mapping[part])
            elif part.endswith(".md"):
                cand = os.path.join(proj.path, cls.BODY_DIR if hasattr(cls, 'BODY_DIR') else "", part)
                if os.path.exists(cand):
                    fp = cand
                else:
                    alt = os.path.join(proj.path, part)
                    if os.path.exists(alt):
                        fp = alt
            if fp is None and not known:
                # part 键名不存在（且也不是实际存在的 .md 文件）→ 404，与其他文体 part 端点口径一致
                return JSONResponse({"error": "part 不存在"}, status_code=404)
            if fp and os.path.exists(fp):
                return {"key": key, "part": part, "text": open(fp, encoding="utf-8").read()}
            return {"key": key, "part": part, "text": ""}
    return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)


@app.post("/api/thesis/refs-check")
def api_thesis_refs_check(payload: dict):
    """引用核查（确定性）：引注 ↔ 文献表。"""
    key = str(payload.get("key") or "")
    proj = thesis_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    plan = proj.plan() or {}
    issues = thesis_writer.check_citations(plan, proj.path)
    return {"ok": True, "key": key, "issues": issues,
            "refs": plan.get("refs") or []}


# ---------- v1.0.0：诗词流水线 ----------

@app.get("/api/poetry/projects")
async def api_poetry_projects():
    return {"items": poetry_writer.discover_projects()}


@app.post("/api/poetry/create")
async def api_poetry_create(payload: dict):
    """建诗词项目（与 /api/create + medium=poetry 同一条路径）。"""
    return _create_poetry_project(payload)


@app.get("/api/poetry/status")
async def api_poetry_status(key: str):
    proj = poetry_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return proj.status()


@app.get("/api/poetry/part")
async def api_poetry_part(key: str, part: str = "final"):
    """读项目部分：note / imagery / draft / check / final"""
    if not poetry_writer.get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return {"key": key, "part": part, "text": poetry_writer.read_part(key, part)}


@app.post("/api/poetry/recheck")
async def api_poetry_recheck(payload: dict):
    """对当前草稿重新做格律校验。"""
    key = payload.get("key")
    if not poetry_writer.get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    try:
        return poetry_writer.recheck(key)
    except Exception as e:
        return JSONResponse({"error": f"校验失败：{e}"}, status_code=500)


@app.get("/api/poetry/forms")
async def api_poetry_forms():
    return {"forms": [{"key": k, "label": v} for k, v in poetry_writer.FORMS.items()]}


# ---------- v1.0.0：散文流水线 + 反 AI 腔检测 ----------

@app.get("/api/prose/projects")
async def api_prose_projects():
    return {"items": prose_writer.discover_projects()}


@app.post("/api/prose/create")
async def api_prose_create(payload: dict):
    """建散文项目（与 /api/create + medium=prose 同一条路径）。"""
    return _create_prose_project(payload)


@app.get("/api/prose/status")
async def api_prose_status(key: str):
    proj = prose_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
    return proj.status()


@app.get("/api/prose/part")
async def api_prose_part(key: str, part: str = "final"):
    """读项目部分：brief / material / draft / slop / final"""
    if not prose_writer.get_project(key):
        return JSONResponse({"error": "项目不存在或未加载，请刷新作品库"}, status_code=404)
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
    # v4.12.16：注入当前 VERSION 到 ?v= 参数（此前硬编码 290，版本错位）
    html = open(path, encoding="utf-8").read()
    ver = version.current()
    html = re.sub(r'\?v=\d+', f'?v={ver}', html)
    return Response(html, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


_V2_DIR = os.path.join(ROOT, "static", "v2")

# v2.9.0：热更新缓存击穿。浏览器对 ES module 的启发式缓存会在升级后长期复用旧模块
# （界面与后端版本错位，E2E 实测踩坑）。做法：JS 由本路由代为伺服，把所有相对
# import 重写为带当前 VERSION 查询参数——版本一升级，整张模块图的 URL 全部变化，
# 天然全量击穿；同时响应带 no-cache，模块图内永远走协商校验。
_V2_JS_IMPORT_RE = re.compile(
    r"(from\s+['\"]|\bimport\(\s*['\"])(\.{1,2}/[^'\"]+?\.js)(['\"])")

@app.get("/v2/js/{file_path:path}")
def v2_js_versioned(file_path: str):
    if not file_path or ".." in file_path or file_path.startswith("/"):
        return JSONResponse({"error": "not found"}, status_code=404)
    fp = os.path.join(_V2_DIR, "js", file_path)
    if not os.path.isfile(fp):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        txt = open(fp, encoding="utf-8").read()
    except OSError:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        ver = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip() or "dev"
    except OSError:
        ver = "dev"
    txt = _V2_JS_IMPORT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}?v={ver}{m.group(3)}", txt)
    return Response(txt, media_type="text/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})

if os.path.isdir(_V2_DIR):
    app.mount("/v2", StaticFiles(directory=_V2_DIR), name="v2-static")


@app.post("/api/export")
def api_export_post(payload: dict):
    """兼容 v2 前端的 POST 调用；实际导出仍复用 GET /api/export。"""
    return api_export(str(payload.get("key") or ""),
                      str(payload.get("format") or payload.get("fmt") or "docx"),
                      int(payload.get("frm") or 0), int(payload.get("to") or 0),
                      int(payload.get("force") or 0), int(payload.get("strict_high") or 0),
                      str(payload.get("mode") or ""))


from bot.v2api import register_v2_routes  # noqa: E402  (需在 _get_project/chat 定义后注册)

register_v2_routes(app, _get_project, chat,
                   session_project=lambda sid: (SESSIONS.get(sid) or {}).get("project"))

