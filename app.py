"""一幕·YIMU 神笔文学创作台 主应用：聊天即开写（小说 / 剧本 / 诗词 / 散文）"""
import json, os, re, shutil, threading, uuid, time, secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bot import llm, workflow, writer, providers, version, distill, learn, agents, skeleton
from bot import medium as medium_mod, skillkit, script_writer, poetry_writer, poetry_checker
from bot import prose_writer, slop_checker, security, jobs, versions, platforms
from bot import thesis_writer
from bot import children_writer, news_writer, copy_writer, other_writer
from bot import chatsum
from bot import export as export_mod
from bot.safeio import atomic_write_json, atomic_write_text, safe_join

ROOT = os.path.dirname(os.path.abspath(__file__))
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

@asynccontextmanager
async def _lifespan(_app):
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    # v3.1.0：启动时后台恢复「校验中/放行中但线程已死」的书（服务重启遗留卡死）
    def _recover():
        try:
            n = writer.recover_all_stale()
            if n:
                print(f"[recover] 已救回 {n} 本陈旧卡死状态的书", flush=True)
        except Exception as e:
            print(f"[recover] 陈旧状态恢复失败：{e}", flush=True)
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
    if key not in PROJECT_INDEX:
        try:
            _exists = os.path.isdir(writer._project_dir(key))
        except Exception:
            _exists = False   # 非法路径 key（如 ../secrets）按原逻辑 404，不因别名检查抛 500
        if not _exists:
            _rk = writer.resolve_key(key)
            if _rk != key:
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
    if key and s.get("state") == "idle" and _get_project(str(key)):
        s["project"] = str(key)
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
                from bot import prose_writer as _pw, poetry_writer as _pyw, script_writer as _sw
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
            if st.get("running") or st["state"] == "in_progress":
                if re.search(r"进度|继续|怎么样|状态|status", msg) or len(msg) < 6:
                    return f"📖 《{st['name']}》正在创作中：第 {st['done']}/{st['total']} 章，已写 {st['words']} 字。" \
                           f"完成前你可以随时问我「进度」。"
                return "正在疯狂创作中（阶段3 无需确认，技能规定一口气写完）。\n" \
                       f"当前进度 {st['done']}/{st['total']} 章，说「进度」查看，或发新主题开新作品。"
        # 已完成 → 允许新主题
    # ---- v2.3.0：会话已绑定本书（attached）：一切消息按「这本书」路由，绝不误起新书 ----
    if s["state"] == "attached" and s.get("project"):
        proj = _get_project(s["project"])
        if proj:
            return _chat_attached(s, proj, msg)
        s["state"] = "idle"   # 目录已被删除 → 解绑，回落旧行为
        s["project"] = None
    # ---- 新作品触发（仅 idle；options 状态下重复丢主题 → 重显菜单，不重复提取）----
    # v2.3.0 保护：idle 但已有 project 的会话不得进新主题提取（attached 已在上面截获）。
    if s["state"] == "idle" and not s.get("project") and _GREETING_RE.fullmatch(msg):
        return _GREETING_REPLY
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
    except providers.ProviderError:
        s["state"] = "options"
        return _NO_MODEL_REPLY + "\n配置完成后回复「重试」即可继续生成规划。"
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
        return {"ok": False, "error": "项目不存在"}
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
    proj.path = target
    proj.name = title
    # 计划文件改名：v3.3.0 修复——此前硬编码写 02-写作计划.json 的 novelName，
    # 剧本/诗词/散文改名完全无效。改走 writer._plan_of 按文体写各自的 *Name 字段。
    _med, _plan_fp, _plan_data = writer._plan_of(target)
    if _med and _plan_fp and isinstance(_plan_data, dict):
        _name_key = {"script": "scriptName", "poetry": "poemName", "prose": "proseName"}.get(_med, "novelName")
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
    return {"ok": True, "key": new_key, "name": title}

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
    done = sum(1 for c in chs if c.get("status") == "completed")
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
            ], cfg=plan.get("config") or {})
            text = resp.strip() if isinstance(resp, str) else str(resp).strip()
            if text:
                replies.append(f"【{label}】{text}")
        except Exception as e:
            replies.append(f"【{label}】（暂时无法响应：{e}）")

    header = f"👥 群聊讨论（{len(at_names)} 位 Agent 参与）\n问题：{question}\n"
    return header + "\n\n".join(replies)

def _chat_attached(s, proj, msg):
    """attached 分支路由：标题 > 继续 > 进度/状态 > 第N章局部重写 > 新建意图引导 > 即时指令。
    章号重写必须排在新建意图之前：「第12章重新写一下」是改本书，不是开新书。"""
    key = s["project"]
    plan = proj.plan() or {}
    st = proj.status()
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
    # 1) 标题命令
    if re.search(r"标题|书名|改名|换名|重命名", msg):
        return _chat_title_cmd(s, key, msg)
    # ── v2.6.1：@Agent 群聊模式 ──
    # 消息以 @ 开头时，解析被 @ 的 Agent 列表，注入完整作品上下文后调用 LLM 群聊
    if msg.startswith("@"):
        return _chat_group(s, proj, msg, plan, st, name)

    # ── v2.4.0：NLU 自然语言意图理解（替换原步骤 2-6 的硬编码正则）──
    # 任意自然语言均可被解析为结构化意图，再路由到对应操作
    from bot.nlu import parse_intent, format_intent_result
    _nlu_status = {"state": st.get("state", "unknown")}
    _nlu = parse_intent(msg, project_status=_nlu_status)
    _intent = _nlu["intent"]
    try:
        _trace("nlu", sid=s.get("session", ""), intent=_intent, conf=_nlu["confidence"], src=_nlu["source"])
    except Exception:
        pass

    # query_status → 进度回复
    if _intent == "query_status":
        mark = ("正在创作中" if st.get("running") else
                "已暂停" if st.get("paused") else
                "已完结" if st.get("state") == "completed" else "在库待写")
        # 单位随文体：剧本=场、诗词=轮、散文=阶段、小说=章
        _unit = {"s_": "场", "p_": "轮", "e_": "阶段"}.get(str(key)[:2], "章")
        return (f"📖 《{name}》{mark}：{st['done']}/{st['total']} {_unit}，已写 {st['words']} 字。"
                f"可随时问「进度」，或直接下修改指令。")

    # set_title → 标题命令
    if _intent == "set_title":
        _t = _nlu["params"].get("title")
        return _chat_title_cmd(s, key, msg if not _t else f"改名叫{_t}")

    # rewrite_chapter → 局部重写通道
    if _intent == "rewrite_chapter":
        no = _nlu["params"].get("chapter_number") or _parse_chapter_no(msg)
        if no:
            if _REWRITING.get(key, {}).get("running"):
                return f"🔁 《{name}》已有重写在进行中，请稍候（状态 pane 可看进度）。"
            if _start_rewrite(key, chapters=[no], dimensions=[msg]):
                return f"已安排第{no}章局部重写，完成后状态 pane 可见。"
            return "⚠️ 重写任务启动失败，请稍后再试。"

    # pause_writing → 暂停
    if _intent == "pause_writing":
        if writer.pause_book(key):
            return f"⏸ 已暂停《{name}》的创作，说「继续」即可恢复。"
        return f"⚠️ 无法暂停《{name}》。"

    # query_outline → 大纲
    if _intent == "query_outline":
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
    if _intent == "export_book":
        return f"📥 请在「作品」面板点击《{name}》的「导出」按钮，可导出 Markdown / EPUB / PDF。"

    # new_project → 引导
    if _intent == "new_project":
        return (f"当前对话已绑定《{name}》。要改这本书直接说（例如「第12章节奏再紧一点」"
                f"「给女主加一场反击」）；要开新书请用左侧『新建作品』。")

    # start_writing_all / start_writing / resume_writing → 启动或继续写作
    # v4.15.0：batch_writing 意图识别「再写N章 / 写完全部」——统一并入写作分支
    if _intent == "batch_writing":
        _batch_param = _nlu["params"].get("batch_size") or "1"
        # 非小说：走各自引擎的断点恢复（带 batch_size）
        if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
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

    if _intent in ("start_writing_all", "start_writing", "resume_writing"):
        if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
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
            _bs = "all" if _intent == "start_writing_all" else None
            _th.Thread(target=lambda: _mod.resume_project(key, batch_size=_bs), daemon=True).start()
            return f"▶ 恢复《{name}》的创作，工作台可见进度…"
        if running:
            st2 = proj.status()
            return (f"📖 《{name}》正在创作中：第 {st2['done']}/{st2['total']} 章，"
                    f"说「进度」随时可查。")
        if writer.resume_book(key):
            st2 = proj.status()
            if _intent != "start_writing_all":
                _bs = str((proj.plan().get("config") or {}).get("batch_size") or "1")
                if _bs.lower() not in ("all", "0"):
                    _extra = f"（本批写 {_bs} 章后暂停；说「写完全部」或「一直写下去」可连续创作）"
                else:
                    _extra = "（连续模式）"
            else:
                _extra = "（全章批量模式）"
            return f"▶ 继续创作《{name}》（第 {st2['done']}/{st2['total']} 章）{_extra}…"
        return f"⚠️ 无法继续《{name}》：写作计划缺失或已完本。"

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


# ---------- v2.1 B：按文体建项目（/api/create 分发与 /api/<medium>/create 共用同一套 cfg 组装） ----------

def _provider_into(cfg: dict, payload: dict) -> dict:
    if payload.get("provider_id"):
        cfg.setdefault("defaults", {})["provider_id"] = payload["provider_id"]
    if payload.get("model"):
        cfg.setdefault("defaults", {})["model"] = payload["model"]
    return cfg


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
    """占位 key 立即返回，规划放后台线程（引擎里是多次 LLM 调用，同步跑会卡死事件循环）。"""
    import threading as _th
    from datetime import datetime as _dt
    display = (cfg.get("title") or cfg.get("idea") or default_name or "").strip()
    display = (display.splitlines()[0] if display else default_name)[:40] or default_name
    _name = safe_fn(cfg.get("title") or cfg.get("idea") or default_name)
    base = f"{prefix}{_dt.now().strftime('%Y%m%d-%H%M%S')}-{_name}"

    def _bg():
        writer.mark_pipeline(base, True)   # v3.1.0：规划+写作全程「创作中」可见
        try:
            create_fn(cfg, base_key=base)
            _link_references(base, ref_ids)
        except Exception as e:
            print(f"[bg] {medium}创作失败: {e}", flush=True)
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
    _safe_name = re.sub(r'[/:*?"<>|\s]+', '', title or (payload.get('idea') or '未命名'))[:24]
    base = f"{_ts}-{_safe_name}" or f"bk_{_ts}"
    # 去掉路径分隔符/空白，书名不参与目录命名以外的用途
    try:
        pdir = safe_join(writer.PROJECTS_DIR, base, label="作品 key")
    except ValueError:
        return JSONResponse({"error": "书名包含非法字符"}, status_code=400)
    os.makedirs(pdir, exist_ok=True)
    skeleton_plan = {"version": 1, "novelName": title or (payload.get("idea") or "未命名"),
                     "projectPath": pdir, "status": "planning",
                     "chapters": [], "totalChapters": total, "totalWords": 0,
                     "minWordsPerChapter": int(cfg.get("min_words_per_chapter") or 3000),
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

def _start_rewrite(key, chapters=None, dimensions=None, max_rounds=6):
    """v2.3.0：局部重写统一入口（/api/rewrite 端点与 attached 对话共用，不重复造轮子）。
    已在跑则返回 False；否则后台线程跑 writer.run_guided_rewrite，状态进 _REWRITING 表。"""
    if _REWRITING.get(key, {}).get("running"):
        return False
    _REWRITING[key] = {"running": True, "result": None}

    def _job():
        try:
            res = writer.run_guided_rewrite(key, chapters=chapters,
                                            dimensions=dimensions, max_rounds=max_rounds)
            _REWRITING[key] = {"running": False, "result": res}
        except Exception as e:
            _REWRITING[key] = {"running": False, "result": {"error": str(e)}}
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
        return JSONResponse({"error": "作品不存在"}, status_code=404)
    return it

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

@app.delete("/api/library/{key}/purge")
def api_lib_purge(key: str, confirm: str = ""):
    """彻底删除（不可逆）：confirm 必须与作品显示名完全一致。"""
    res = writer.purge_book(key, confirm)
    if not res.get("ok"):
        return JSONResponse(res, status_code=400)
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

@app.post("/api/rewrite-plan")
def api_rewrite_plan(payload: dict):
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
    cfg = (proj.plan() or {}).get("config") or {}
    brief = writer.rewrite_brief(proj, cfg)
    return brief

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
    _start_rewrite(key, chapters=payload.get("chapters"), dimensions=payload.get("dimensions"),
                   max_rounds=int(payload.get("max_rounds") or 6))
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

# ---------- v2.3.0：标题体系 + 一致性体检 ----------

@app.post("/api/title/set")
def api_title_set(payload: dict):
    key = str(payload.get("key") or "")
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    res = _apply_title(key, str(payload.get("title") or ""))
    if not res.get("ok"):
        return JSONResponse({"error": res.get("error") or "改名失败"}, status_code=400)
    return res

@app.post("/api/title/generate")
def api_title_generate(payload: dict):
    key = str(payload.get("key") or "")
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    try:
        n = min(max(int(payload.get("n") or 5), 1), 10)
    except (TypeError, ValueError):
        n = 5
    items = _generate_title_candidates(key, n=n)
    return {"ok": bool(items), "items": items}

@app.get("/api/title/candidates")
def api_title_candidates(key: str):
    if not _get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return {"items": _read_title_items(key)}

# ---------- v4.15.11：一致性一键修复（单个深度修复 + 全部一键修复） ----------

_FIXABLE_REWRITE_RE = re.compile(r"第(\d+)章")


def _consistency_fix_one(key: str, issue: str) -> dict:
    """单个体检问题的修复：先确定性修复（continuity.fix），改稿类自动升级 LLM 定向重写。"""
    from bot import continuity as _cont
    res = _cont.fix(_project_dir(key), issue)
    actions = list(res.get("actions") or [])
    if not res.get("fixed"):
        # 改稿类：定位章号（无章号则取最近完成章），启动定向重写（后台，状态进 _REWRITING）
        m = _FIXABLE_REWRITE_RE.search(issue)
        plan = writer.Project(_project_dir(key), key).plan() or {}
        no = int(m.group(1)) if m else max(
            [c.get("chapterNumber") or 0 for c in (plan.get("chapters") or [])
             if c.get("status") == "completed"] or [0])
        if no:
            ok = _start_rewrite(key, chapters=[no], dimensions=[issue], max_rounds=1)
            actions.append(f"已启动第{no}章定向重写（LLM 修复后台进行）" if ok
                           else "已有重写任务在跑，本章修复已排队语义跳过")
        else:
            actions.append("没有可定位的章节，仅标记已处理")
    return {"issue": issue, "fixed": res.get("fixed"), "actions": actions}


@app.post("/api/providers/fetch-models")
def api_providers_fetch_models(payload: dict):
    """v4.15.15：拉取供应商的可用模型列表（GET {base_url}/models）。
    添加/编辑供应商时，填完地址与密钥即可一键拉取，模型选择改为下拉。"""
    import urllib.request as _ur
    base = str(payload.get("base_url") or "").strip().rstrip("/")
    key = str(payload.get("api_key") or "").strip()
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
        return {"ok": True, "models": sorted(set(ids)), "count": len(set(ids))}
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
    """一键自更新：下载 GitHub 最新 release 源码 → 白名单覆盖 → 返回需重启。"""
    import urllib.request, zipfile, io, shutil, tempfile
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
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "yimu-lit",
                     **({"Authorization": f"Bearer {token}"} if token else {})})
        with urllib.request.urlopen(req, timeout=10) as r:
            rel = json.load(r)
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
        req = urllib.request.Request(url, headers={"User-Agent": "yimu-lit",
            **({"Authorization": f"Bearer {token}"} if token else {})})
        with urllib.request.urlopen(req, timeout=120) as r:
            blob = r.read()
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
    """重启服务（自更新后调用）。v4.15.18：-m uvicorn 方式（脚本路径方式会遮蔽标准库）+ 端口取自当前请求。"""
    import sys as _sys, subprocess as _sp
    _port = request.url.port or int(os.environ.get("NOVEL_BOT_PORT") or 46821)
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


@app.get("/api/check-update")
def api_check_update():
    """v4.15.12：检查 GitHub 仓库最新 release——前端版本号点击时调用（此前端点缺失 → 404「网络错误」）。
    GitHub 仓库是唯一更新来源：有新版提示拉取；无新版/网络不通都如实返回。"""
    import urllib.request
    local = version.current()
    repo = "yimuhub/yimu-lit"
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "yimu-lit"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.load(resp)
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
    proj = _get_project(key) if key else _current_proj()
    if not proj:
        return JSONResponse({"error": "作品不存在"}, status_code=404)
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

@app.get("/api/book-live")
def api_book_live(key: str):
    """v0.7 R4：对话区「本书在干什么」live 卡数据源（纯读、无 LLM、按书隔离）。
    聚合 阶段/当前章/进度/实时日志，前端 1.5s 轮询原地刷新（Hermes 式 live 工具步骤）。"""
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
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
            "call_gap": d.get("call_gap", 2), "max_global_agents": d.get("max_global_agents", 4),
            "chat_context_limit": chatsum.load_limit(),
            "chat_auto_compress": chatsum.compress_enabled()}


@app.post("/api/settings")
def api_settings_save(payload: dict):
    """收口关键开关：审稿强度、token 预算、Lorebook 检索、调用日志、对话压缩。"""
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
    if "chat_context_limit" in payload:
        v = str(payload.get("chat_context_limit") or "").lower()
        if v not in chatsum.CONTEXT_LIMITS:
            return JSONResponse({"error": "chat_context_limit 只能是 200k/400k/500k/1m"}, status_code=400)
        d["chat_context_limit"] = v
    if "chat_auto_compress" in payload:
        d["chat_auto_compress"] = bool(payload.get("chat_auto_compress"))
    atomic_write_json(cf, cfg)
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
    return {"ok": True, **saved}

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
_RELEASE_LOCK = threading.Lock()  # v4.7.1：放行门禁去重锁（见 api_release 的 TOCTOU 修复）

@app.post("/api/release")
def api_release(payload: dict):
    key = payload.get("key")
    proj = _get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
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
    # v3.1.0：validating/releasing 且线程已死 → 立即恢复，前端不再永远转圈
    try:
        writer.recover_stale_book(key, live_proj=proj)
    except Exception:
        pass
    st = proj.status()
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
    # ---- 非小说文体：走各自引擎的断点恢复（服务重启/线程中断后的唯一续跑通道）----
    _medium_mod = {"s_": "script_writer", "p_": "poetry_writer", "e_": "prose_writer",
                   "t_": "thesis_writer", "c_": "children_writer",
                   "n_": "news_writer", "k_": "copy_writer", "o_": "other_writer"}.get(str(key)[:2])
    if _medium_mod:
        import threading as _th
        from bot import script_writer, poetry_writer, prose_writer, thesis_writer
        mod = {"script_writer": script_writer, "poetry_writer": poetry_writer,
               "prose_writer": prose_writer, "thesis_writer": thesis_writer}[_medium_mod]
        fn = getattr(mod, "resume_project", None)
        if not callable(fn):
            return JSONResponse({"error": "该文体暂不支持断点恢复"}, status_code=501)
        def _bg():
            writer.mark_pipeline(key, True)   # v3.1.0：恢复期间「创作中」可见
            try:
                fn(key)
            except Exception as e:
                print(f"[bg] {_medium_mod} 断点恢复失败: {e}", flush=True)
            finally:
                writer.mark_pipeline(key, False)
        _th.Thread(target=_bg, daemon=True).start()
        return {"ok": True, "msg": "已从中断处恢复创作，工作台轮询可见进度。"}
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
        return JSONResponse({"error": "作品不存在"}, status_code=404)
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
        from bot import state as _stmod
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
        return JSONResponse({"error": "作品不存在"}, status_code=404)
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
        return JSONResponse({"error": "作品不存在"}, status_code=404)
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
        return JSONResponse({"error": "作品不存在"}, status_code=404)
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
    # v4.15.3：署名回退链 —— 作品 meta.author → 向导笔名（plan.config.penname）→ 全局默认笔名
    author = ((plan.get("meta") or {}).get("author") or "").strip()
    if not author:
        author = str(((plan.get("config") or {}).get("penname")) or "").strip()
    if not author:
        author = str((_load_pennames().get("default")) or "").strip()

    if fmt in ("md", "markdown"):
        data, media, ext = export_mod.markdown(proj).encode("utf-8"), "text/markdown; charset=utf-8", "md"
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
                res = _llm.chat_json([{"role": "user", "content": prompt}], cfg=_cfg())
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
        return {"ok": True, "agent": result, "name": name}
    except Exception as e:
        return JSONResponse({"error": f"添加 Agent 失败：{e}"}, status_code=500)


@app.post("/api/agents-set")
def api_agents_set(payload: dict):
    """v0.5 M4：给某子 Agent 单独配 provider/model（留空=回落 stage/默认）。"""
    am = agents.set_binding(payload.get("agent"), payload.get("provider_id"), payload.get("model"))
    return {"ok": True, "agent_models": am}

@app.post("/api/agents-skills")
def api_agents_skills(payload: dict):
    """v2.1 P2：给某岗位绑定技能（skillkit 文件 name）；空列表=解绑。
    绑定后该岗位产出的注入会优先带上这些技能（skillkit.pick 的 boost 维度）。"""
    try:
        agents.set_skills(payload.get("agent"), payload.get("skills"))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "agent": payload.get("agent"), "skills": agents.skills_for(payload.get("agent"))}

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
    default_workers = max(1, min(8, int(writer._cfg_defaults().get("chapter_workers") or 5)))
    nworkers = max(1, min(8, int(payload.get("workers") or default_workers)))
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
        if it.get("medium") not in (None, "novel"):
            continue   # 本看板只挂小说的并行/重写/放行动作，剧本等另走各自工作台
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
            # v3.1.0：cleanText = 章题+引子+正文（剥本章概要/章节备注），复制与对外分享用；
            # text 保留原结构供编辑器与 PUT 回写
            "cleanText": writer._clean_chapter_text(text),
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
    return {"version": version.current()}

# ---------- v1.0.0：文体路由 + 技能库（一幕·YIMU 神笔文学创作台） ----------

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
    """建剧本项目（与 /api/create + medium=script 同一条路径）。"""
    return _create_script_project(payload)


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


@app.post("/api/script/audit")
def api_script_audit(payload: dict):
    """v3.3.0：剧本一致性体检（人物在场/跳场/重复/时代串代）→ 22-剧本体检.json。"""
    key = str(payload.get("key") or "")
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    from bot import script_state
    report = script_state.audit_script(proj.path)
    script_state.save_audit(proj.path, report)
    return {"ok": True, "key": key, **report}


@app.get("/api/script/audit")
def api_script_audit_get(key: str):
    """读取上次剧本体检结果（22-剧本体检.json）。"""
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    from bot import script_state
    report = script_state.load_audit(proj.path)
    return {"ok": True, "key": key, **report}


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
        try:
            proj.save_plan(plan)
            proj.log(f"🧭 场次清单重新解析成功：{len(parsed)} 场（原计划里缺失，已回填）")
        except Exception:
            pass
        return parsed
    return []


@app.get("/api/script/scenes")
async def api_script_scenes(key: str):
    """场次清单（表格 + 解析后的结构化场次）。"""
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    scenes = _script_scenes_or_reparse(proj, key)
    return {"key": key, "text": script_writer.read_file_part(key, "scenes"),
            "scenes": scenes}


@app.get("/api/script/scene")
async def api_script_scene(key: str, no: int):
    """单场正文。"""
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return {"key": key, "no": no, "text": script_writer.read_scene(key, no)}


@app.get("/api/script/preview")
async def api_script_preview(key: str):
    """整剧预览：按场次顺序一次返回全部场次的标题/状态/字数/正文（省掉逐场 30 次往返）。

    场次标题由引擎的 scene_heading() 合成，未写的场次也占位返回 text=""，
    前端据此直接排版占位。written 为实际有正文的场次数，total 为计划总场数。
    """
    proj = script_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在"}, status_code=404)
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
            except (TimeoutError, asyncio.TimeoutError):  # v4.7.1 Python<3.11 捕不到会让改稿流报错
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
        return JSONResponse({"error": "项目不存在"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return assist_mod.list_versions(key, med, doc)


@app.post("/api/assist/restore")
async def api_assist_restore(payload: dict):
    from bot import assist as assist_mod

    medium = _assist_medium(payload)
    key = str(payload.get("key") or "")
    if not _assist_project(medium, key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在"}, status_code=404)
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
        return JSONResponse({"error": "项目不存在"}, status_code=404)
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


@app.post("/api/script/storyboard")
def api_script_storyboard(payload: dict):
    """v4.5.0：把已写场次拆成分镜头脚本（后台线程）。payload: {key, sceneNo?}"""
    import threading as _th
    key = str(payload.get("key") or "")
    scene_no = payload.get("sceneNo")
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    def _bg():
        try:
            script_writer.gen_storyboard(key, only_scene=int(scene_no) if scene_no else None)
        except Exception as e:
            print(f"[bg] 分镜生成失败: {e}", flush=True)
    _th.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "async": True, "msg": "分镜拆解已后台启动"}


@app.get("/api/script/storyboard")
async def api_script_storyboard_get(key: str):
    """v4.5.0：分镜表数据（结构化 JSON）。"""
    if not script_writer.get_project(key):
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return {"key": key, "text": script_writer.read_file_part(key, "storyboard"),
            **script_writer.storyboard_data(key)}


@app.post("/api/novel-to-drama")
def api_novel_to_drama(payload: dict):
    """v4.5.0：小说一键转 AI 短剧/漫剧剧本——按章改编成分集剧本 + 自动分镜。
    payload: {key, chapters?: [章号]， form?: short_drama|comic_drama, episodes_per_chapter?: 1}
    生成独立剧本项目（key 加 -短剧 后缀），原书不动。"""
    import threading as _th
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
    chs = chs[:12]   # 单次改编上限 12 章，防失控
    new_key_base = f"{src_key}-{'漫剧' if form == 'comic_drama' else '短剧'}"

    def _run():
        try:
            _novel_to_drama_impl(src_key, form, chs, new_key_base, plan, proj)
        except Exception as e:
            print(f"[bg] 小说转短剧失败: {e}", flush=True)
    _th.Thread(target=_run, daemon=True).start()
    return {"ok": True, "async": True,
            "msg": f"改编已后台启动（{len(chs)} 章 → {len(chs)} 集），完成后在作品库查看",
            "key_hint": new_key_base,
            "name_hint": f"{plan.get('novelName') or src_key}·{'漫剧' if form == 'comic_drama' else '短剧'}"}


def _novel_to_drama_impl(src_key, form, chapters, new_key_base, src_plan, proj=None):
    """v4.7.0：小说→短剧改编实现（后台线程）：逐章 → 分集剧本正文 → 场次清单 → 自动分镜。
    proj 参数由调用方传入（v4.5.0 版本缺少此参数导致 _chapter_path 抛出 NameError 静默失败）。"""
    import threading as _th
    from datetime import datetime as _dt
    from bot import llm as _llm
    name = re.sub(r'[\/:*?"<>|\s]+', "", f"{src_plan.get('novelName') or src_key}·{'漫剧' if form == 'comic_drama' else '短剧'}")[:40]
    pdir = safe_join(script_writer.PROJECTS_DIR, new_key_base, label="项目 key")
    os.makedirs(pdir, exist_ok=True)
    sp = script_writer.ScriptProject(pdir, name)
    script_writer.llm.book(new_key_base, "planning")
    sp.log(f"🎬 从《{src_plan.get('novelName') or src_key}》改编 {form}：{len(chapters)} 章逐集剧本化")
    era = "modern"
    scenes = []
    plan_doc = {
        "version": 1, "scriptName": name, "projectPath": pdir, "medium": "script",
        "form": form, "formLabel": script_writer.FORMS.get(form, {})["label"],
        "entryPath": "adapt", "totalScenes": 0, "targetPages": 30, "episodes": len(chapters),
        "createdAt": _dt.now().isoformat(), "status": "writing", "stages": {"S0": {"done": True}},
        "scenes": [], "adaptedFrom": {"key": src_key, "chapters": [c.get("chapterNumber") for c in chapters]},
        "config": {"form": form, "title": name, "idea": f"由小说《{src_plan.get('novelName') or src_key}》改编"},
    }
    # v4.7.0：proj 由调用方传入；兜底用 _get_project 重建，避免 NameError
    if proj is None:
        proj = _get_project(src_key)
    if proj is None:
        sp.log("💥 无法读取原小说项目，改编失败")
        return
    # v4.7.0：先落一份「进行中」的剧本计划，让项目立即进作品库（可查/可进工作台）；
    # 即使 LLM 未配置或全部失败，用户也能在作品库看到这部短剧条目（而非凭空消失）。
    sp.save_plan(plan_doc)
    for c in chapters:
        no = c.get("chapterNumber")
        src_text = writer._clean_chapter_text(open(_chapter_path(proj, c), encoding="utf-8").read()) if _chapter_path(proj, c) and os.path.exists(_chapter_path(proj, c)) else ""
        sp.log(f"🎬 第 {no} 章 → 第 {no} 集：剧本化…")
        body = _llm.chat([
            {"role": "system", "content":
                f"你是竖屏微短剧编剧。把小说章节改编成{script_writer.FORMS[form]['label']}第 {no} 集剧本（3-5 分钟一集）。"
                "格式：场标题行（内/外 地点 时间）+ 动作行 + 短促台词（角色名：台词）。"
                "开场前 3 秒必须抓人；集尾强钩子；台词短促信息密度高；删掉一切过场与心理描写。"},
            {"role": "user", "content": f"【原章节】第{no}章 {c.get('title','')}\n\n{src_text[:9000]}"}],
            stage="writing", max_tokens=6000) or ""
        if not body.strip():
            continue
        os.makedirs(os.path.join(pdir, script_writer.F_BODY), exist_ok=True)
        fn = f"第{no:03d}场.md"
        script_writer._write(os.path.join(pdir, script_writer.F_BODY, fn), body)
        scenes.append({"no": no, "kind": "集", "place": f"第 {no} 集", "time": "",
                       "cast": "", "status": "completed",
                       "fileName": fn, "wordCount": len(body)})
    if not scenes:
        sp.log("💥 改编失败：没有生成任何剧本内容")
        # v4.7.0：失败也保留计划文件并标记状态，让作品库可见（可删可重试），不是凭空消失
        plan_doc["status"] = "failed"
        sp.save_plan(plan_doc)
        return
    # v4.12.14 重大修复：scenes 回填到计划——此前从没写入 plan_doc["scenes"]，
    # 导致剧本工作台 0/0、状态永远 "writing"、分镜静默失败（一切假状态的总根因）
    plan_doc["scenes"] = scenes
    plan_doc["status"] = "completed"
    # 写出 03-场次清单.md（reparse 兜底 + 工作台场次页签数据源）
    md = [f"# {name} 场次清单\n", "| 场次 | 类型 | 集数 | 状态 |", "| :--- | :--- | :--- | :--- |"]
    for sc in scenes:
        md.append(f"| 第{sc['no']}场 | {sc['kind']} | {sc['place']} | 已完成 |")
    script_writer._write(os.path.join(pdir, script_writer.F_SCENES), "\n".join(md) + "\n")
    sp.save_plan(plan_doc)
    sp.log(f"✅ 改编完成：{len(scenes)} 集；启动分镜头拆解…")
    writer.mark_pipeline(new_key_base, True)
    try:
        script_writer.gen_storyboard(new_key_base)
    finally:
        writer.mark_pipeline(new_key_base, False)


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
        return JSONResponse({"error": "项目不存在"}, status_code=404)
    return proj.status()


@app.get("/api/thesis/part")
async def api_thesis_part(key: str, part: str = "final"):
    """论文文档：outline/refs/report/final 或 正文章（如 part=ch1）。"""
    proj = thesis_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
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


@app.post("/api/ai-generate")
def api_ai_generate(payload: dict):
    """v4.15.1：向导「AI 自动生成」——按体裁+模式+已选参数定制 prompt，随机种子保证每次不同。"""
    medium = str(payload.get("medium") or "novel")
    field = str(payload.get("field") or "title")   # title 或 idea
    hint = str(payload.get("hint") or "")           # 用户已输入的线索
    # v4.15.1：从 payload 提取已选参数（题材/基调/视角/模式等），喂给 AI 定制生成
    genre = str(payload.get("genre") or "")
    tone = str(payload.get("tone") or "")
    perspective = str(payload.get("perspective") or "")
    mode = str(payload.get("mode") or "")
    ctype = str(payload.get("ctype") or "")
    ntype = str(payload.get("ntype") or "")
    ktype = str(payload.get("ktype") or "")
    template = str(payload.get("template") or "")
    form = str(payload.get("form") or "")

    # 每个文体专属的生成指令模板（比旧版更细，结合体裁+模式）
    templates = {
        "novel": {
            "genre_map": {
                "悬疑": "悬疑推理，节奏紧凑，层层剥茧",
                "玄幻": "玄幻修真，宏大世界观，升级体系",
                "言情": "都市言情，细腻情感，人物羁绊",
                "科幻": "硬科幻设定，未来推演",
                "历史": "历史架空，权谋博弈",
                "都市": "都市生活，职场或情感",
                "武侠": "武侠江湖，恩怨情仇",
                "恐怖": "恐怖氛围，心理惊悚",
            },
            "gen": "为这部{genre_desc}小说起一个有辨识度的书名，并写一句话故事概念（谁/想要什么/被什么挡住）",
        },
        "script": {
            "gen": "为这部{form_desc}剧本起一个剧名，并写一句话故事概念（核心冲突 + 视觉钩子）",
            "form_map": {"film": "电影", "series": "剧集", "short_drama": "短剧", "animation": "动画"},
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
        base = base.replace("{genre_desc}", tpl["genre_map"].get(genre, "网络"))
    elif medium == "script":
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
    if tone: extras.append(f"基调：{tone}")
    if perspective: extras.append(f"视角：{perspective}")
    if mode: extras.append(f"创作模式：{mode}")
    if extras:
        base += "。" + "，".join(extras)

    hint_p = f"\n用户已输入的线索（在此基础上发散，不要照抄）：{hint}" if hint.strip() else ""
    # v4.15.4：结构化随机——每次从维度池抽 2-3 个硬约束，强制输出方向不同。
    # 此前只拼一串数字种子，模型直接忽略 → 每次输出趋同。
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
    _rand_block = "；".join(f"{k}必须围绕「{random.choice(_DIMS[k])}」展开" for k in _picked)
    _seed = f"随机批次号：{random.randint(100000, 999999)}（同批次内容禁止重复）"

    out = llm.chat_json([{"role": "user", "content":
        f"{base}。{hint_p}\n"
        f"硬性随机约束（必须严格围绕以下维度创作，每次都不同）：{_rand_block}。\n{_seed}\n"
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
        return JSONResponse({"error": "作品不存在"}, status_code=404)
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
    return JSONResponse({"error": "项目不存在"}, status_code=404)


@app.get("/api/mini/part")
async def api_mini_part(key: str, part: str = "final"):
    """v4.12.1：children/news/copy 通用文档端点。"""
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
            fp = None
            if part in mapping:
                fp = os.path.join(proj.path, mapping[part])
            elif part.endswith(".md"):
                fp = os.path.join(proj.path, cls.BODY_DIR if hasattr(cls, 'BODY_DIR') else "", part)
                if not os.path.exists(fp):
                    fp = os.path.join(proj.path, part)
            if fp and os.path.exists(fp):
                return {"key": key, "part": part, "text": open(fp, encoding="utf-8").read()}
            return {"key": key, "part": part, "text": ""}
    return JSONResponse({"error": "项目不存在"}, status_code=404)


@app.post("/api/thesis/refs-check")
def api_thesis_refs_check(payload: dict):
    """引用核查（确定性）：引注 ↔ 文献表。"""
    key = str(payload.get("key") or "")
    proj = thesis_writer.get_project(key)
    if not proj:
        return JSONResponse({"error": "项目不存在"}, status_code=404)
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
    """建散文项目（与 /api/create + medium=prose 同一条路径）。"""
    return _create_prose_project(payload)


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
                      str(payload.get("format") or payload.get("fmt") or "docx"))


from bot.v2api import register_v2_routes  # noqa: E402  (需在 _get_project/chat 定义后注册)

register_v2_routes(app, _get_project, chat)

