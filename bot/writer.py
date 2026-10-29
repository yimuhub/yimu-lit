"""写作引擎：阶段3 疯狂创作 + 阶段4 自动校验（后台线程，事件推送）；v0.6 增并行章节创作（总架构+工作池）"""
from typing import Optional
import json, os, re, datetime, threading, queue
from . import workflow, skill, continuity, llm, providers, versions
from .safeio import atomic_write_json, atomic_write_text, safe_join

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_DIR = os.path.join(ROOT, "projects")

def _skeleton_refresh(key):
    """v0.9.0：刷新该书『17-写作骨架.md』（纯文件聚合，不花 LLM）。失败不影响主流程。"""
    try:
        from bot import skeleton
        skeleton.build(key)
    except Exception:
        pass

class Project:
    def __init__(self, path, name):
        self.path = path
        self.name = name
        self.events = []
        self.running = False
        self.lock = threading.RLock()   # v0.6 B：并行写作下保护 02-写作计划.json 读改写

    def log(self, msg):
        with self.lock:
            line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
            self.events.append(line)
            if len(self.events) > 200:
                self.events.pop(0)
            # v0.7 R4：持久化逐条过程日志（供 /api/book-live live 卡 + 断点排障；封顶 ~300 行）
            try:
                lf = os.path.join(self.path, "运行日志.txt")
                with open(lf, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                lines = open(lf, encoding="utf-8").read().splitlines()
                if len(lines) > 300:
                    open(lf, "w", encoding="utf-8").write("\n".join(lines[-300:]) + "\n")
            except Exception:
                pass

    def plan(self):
        """读 02-写作计划.json；文件缺失或内容损坏都返回 None（调用方需按 None 兜底）。

        v4.7.1 修复：原先是 `json.load(open(p))`，既无异常保护也不关句柄。任何一本书的
        计划文件被手工改坏 / 写入中断（JSONDecodeError），就会让 /api/library、
        /api/studio-overview 等聚合端点整片 500 —— 用户看到的是「所有作品凭空消失」，
        而且不知道原因。现在单本损坏只降级该本（state=missing），不再拖垮作品库。
        """
        p = os.path.join(self.path, "02-写作计划.json")
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def save_plan(self, plan):
        plan["updatedAt"] = datetime.datetime.now().isoformat()
        with self.lock:   # v0.6 B：并行写作串行化 02-写作计划.json 写盘（RLock，可被读改写临界区复用）
            atomic_write_json(os.path.join(self.path, "02-写作计划.json"), plan)

    def status(self):
        plan = self.plan()
        if not plan:
            return {"state": "missing", "done": 0, "total": 0, "words": 0}
        chs = plan.get("chapters") or []
        done = sum(1 for c in chs if c.get("status") == "completed")
        words = sum(c.get("wordCount") or 0 for c in chs)
        meta = plan.get("meta") or {}
        return {"state": plan.get("status", "unknown"), "done": done, "total": len(chs),
                "words": words, "running": self.running,
                "paused": bool(meta.get("paused")),
                "queued": len([q for q in meta.get("queued_msgs", []) if not q.get("consumed")]),
                "name": plan.get("novelName", self.name),
                "quality": quality_score(self.path)}

def rebuild_book(proj) -> str:
    """按当前章节文件重建「成书-*.md」（编辑/回滚后调用；无成书文件则不动）。"""
    plan = proj.plan() or {}
    name = plan.get("novelName") or proj.name
    book_path = os.path.join(proj.path, f"成书-{name}.md")
    if not os.path.exists(book_path):
        return ""
    parts, total = [], 0
    for ch in plan.get("chapters") or []:
        fp = _chapter_file_path(proj, ch)
        if os.path.exists(fp):
            parts.append(_clean_chapter_text(open(fp, encoding="utf-8").read()))
            total += int(ch.get("wordCount") or 0)
    atomic_write_text(book_path,
                      f"# {name}\n\n> 共 {len(plan.get('chapters') or [])} 章 · 约 {total} 字\n\n"
                      + "\n\n---\n\n".join(parts))
    return book_path


# ---------- 阶段2 落盘 ----------

def _parse_outline_titles(outline_md, total):
    """从大纲表格解析每章标题，失败则兜底。

    v3.1.0：LLM 的大纲表格首列常写「01」「1」甚至全角数字，旧正则只认「第N章」
    导致 21/28 本书 100% 落入默认标题。现在兼容三种写法。"""
    def _cn_int(s):
        s = s.strip()
        if s.isdigit():
            return int(s)
        try:
            return int(s.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        except ValueError:
            return None

    titles = []
    # 形式一：| 第N章 | 标题 |
    for m in re.finditer(r"^\|\s*第(\d+)章\s*\|\s*([^|]+)\|", outline_md, re.M):
        n, t = int(m.group(1)), re.sub(r"[*`]", "", m.group(2)).strip()
        titles.append((n, t or f"第{n}章"))
    # 形式二：| 01 | 标题 |（首列纯数字/全角数字的表格行）
    for m in re.finditer(r"^\|\s*([0-9０-９]{1,4})\s*\|\s*([^|\n]+)\|", outline_md, re.M):
        n = _cn_int(m.group(1))
        if not n or n > max(total * 2, 200):
            continue
        t = re.sub(r"[*`]", "", m.group(2)).strip()
        if t and not any(x[0] == n for x in titles):
            titles.append((n, t))
    # 形式三：### 第N章 标题
    for m in re.finditer(r"^#{1,4}\s*第(\d+)章\s*[:：、\.]?\s*(\S[^\n]{0,40})", outline_md, re.M):
        n, t = int(m.group(1)), re.sub(r"[*`]", "", m.group(2)).strip()
        if t and not any(x[0] == n for x in titles):
            titles.append((n, t))
    titles.sort(key=lambda x: x[0])
    if len(titles) < total:
        for n in range(1, total + 1):
            if not any(t[0] == n for t in titles):
                titles.append((n, f"第{n}章"))
    out = [t for n, t in sorted(titles)[:total]]
    return out or [f"第{n}章" for n in range(1, total + 1)]

def _strip_md_style(text: str) -> str:
    """v4.4.0：正文排版净化——去加粗/斜体/行首 #/列表符/反引号/分隔线，
    只留正常中文标点（防模型不守排版纪律）。章节题行与结构小节标记由调用方处理。"""
    t = re.sub(r"\*{1,3}([^*\n]{1,80}?)\*{1,3}", r"\1", text)          # **加粗** / *斜体*
    t = re.sub(r"(?m)^(\s*)[-*•]\s+", r"\1", t)                        # 行首列表符
    t = re.sub(r"_{1,3}([^_\n]{1,80}?)_{1,3}", r"\1", t)                # __下划线强调__
    t = re.sub(r"`+", "", t)                                             # 反引号
    t = re.sub(r"(?m)^\s*([-*_]\s*){3,}$", "", t)                      # --- 分隔线
    # 行首 # 的结构标题（章题/引子/正文小节）保留——成书阅读需要层级；
    # 正文中间的 # 由提示词禁止 + 本章概要/备注剥离后基本不出现。
    return t


def _clean_chapter_text(text):
    """成书时剥离规划元信息（本章概要/章节备注），保留 章题+章首引子+正文。
    v3.1.0：补「仅有概要、无引子」分支——旧实现此时会把概要原样带进成书。
    v4.4.0：输出前统一过 _strip_md_style 排版净化。"""
    m_sum = re.search(r"\n## 本章概要", text)
    m_intro = re.search(r"\n## 章首引子", text)
    m_notes = re.search(r"\n## 章节备注", text)
    end = m_notes.start() if m_notes else len(text)
    if m_intro and m_sum and m_sum.start() < m_intro.start():
        return _strip_md_style((text[:m_sum.start()] + text[m_intro.start():end])).strip()
    if m_intro:
        return _strip_md_style(text[:m_intro.start() + 1] + text[m_intro.start():end]).strip()
    if m_sum:
        # 只有概要没有引子：剥掉概要段（从标题行到下一个 ## 或文末）
        nxt = re.search(r"\n## ", text[m_sum.end():])
        cut = m_sum.end() + (nxt.start() if nxt else len(text) - m_sum.end())
        return _strip_md_style(text[:m_sum.start()] + text[cut:]).strip()
    if m_notes:
        return _strip_md_style(text[:m_notes.start()]).strip()
    return _strip_md_style(text).strip()

def _cfg_defaults():
    try:
        return json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8")).get("defaults", {}) or {}
    except Exception:
        return {}

def _write_cfg_text(pdir, fname, body):
    with open(os.path.join(pdir, fname), "w", encoding="utf-8") as f:
        f.write(body)

def _plan_references(proj=None, cfg=None, limit=8):
    """给总规划/卷弧规划用的参考资料摘要；只读 meta，不触发 LLM。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        from . import references as _refs
        ids = [str(x) for x in (cfg.get("reference_ids") or []) if str(x).strip()]
        key = os.path.basename(proj.path) if proj is not None else ""
        if key:
            for rid in _refs.project_ref_ids(key):
                if rid not in ids:
                    ids.append(rid)
        out = []
        for rid in ids[:limit]:
            meta = _refs.get_ref(rid) or {}
            if not meta or meta.get("usage_mode") == "disabled":
                continue
            a = meta.get("analysis") if isinstance(meta.get("analysis"), dict) else {}
            bits = [str(a.get("summary") or meta.get("summary") or "").strip()]
            if a.get("structure"):
                bits.append("结构：" + str(a["structure"]))
            if a.get("style_traits"):
                bits.append("风格：" + "；".join(str(x) for x in a["style_traits"][:6]))
            if a.get("key_facts"):
                bits.append("事实：" + "；".join(str(x) for x in a["key_facts"][:8]))
            if a.get("caution"):
                bits.append("注意：" + "；".join(str(x) for x in a["caution"][:4]))
            out.append({"id": rid, "title": meta.get("title") or rid,
                        "excerpt": " ".join(b for b in bits if b)[:900]})
        return out
    except Exception:
        return []

def create_project(cfg, titles, pdir=None, name=None):
    """生成项目目录 + 00人物/01大纲/03文风 + 02-写作计划.json（按选定标题命名）
    ShenBi：另生成 09故事圣经/10线索账本/11章节摘要/12时间线（可配 v3_pipeline 关）。

    pdir 由调用方传入时（例如 /api/create 的骨架目录），规划结果写回同一个目录，
    避免“接口返回 key A、后台却创建目录 B”的双项目问题。
    """
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = re.sub(r'[\\/:*?"<>|\s]+', "", name or titles[0]["title"]) or "未命名"
    if not pdir:
        pdir = os.path.join(PROJECTS_DIR, f"{ts}-{name}")
    os.makedirs(pdir, exist_ok=True)
    proj = Project(pdir, name)
    llm.book(os.path.basename(pdir), "planning")     # v0.3.x：规划阶段 token 归集到该书
    proj.log("📐 阶段2：生成人物档案…")
    characters = workflow.gen_characters(cfg)
    open(os.path.join(pdir, "00-人物档案.md"), "w", encoding="utf-8").write(
        f"# {name} 人物档案\n\n" + characters)

    proj.log("📐 阶段2：生成大纲…")
    try:
        total = int(str(cfg.get("chapters") or 10))
    except (ValueError, TypeError):
        total = 10
    cfg2 = dict(cfg)
    cfg2["chapters"] = total
    # 百万字规模下不让一次 LLM 输出上千章细纲：先生成前 40 章的“开卷细纲”，
    # 其余结构交给 longform 的卷/弧/章卡分层规划按窗口滚动补齐。
    cfg_outline = dict(cfg2)
    if total > 60:
        cfg_outline["chapters"] = 40
    outline = workflow.gen_outline(cfg_outline)
    open(os.path.join(pdir, "01-大纲.md"), "w", encoding="utf-8").write(
        f"# {name} 大纲\n\n" + outline)

    proj.log("📐 阶段2：生成文风基准…")
    style = workflow.gen_style(cfg)
    open(os.path.join(pdir, "03-文风基准.md"), "w", encoding="utf-8").write(style)

    # v0.3 P2：题材×风格技能（金手指/基调/视角/节奏/语言/卷）→ style.json，供 craft 注入
    import json as _json
    _sk = {"genre": cfg.get("genre", ""), "tone": cfg.get("tone", ""),
           "perspective": cfg.get("perspective", ""), "style": cfg.get("style") or {},
           "platform": cfg.get("platform", ""), "platform_note": cfg.get("platform_note", ""),
           "sells": cfg.get("sells") or []}
    atomic_write_json(os.path.join(pdir, "style.json"), _sk)

    # ---------- 连续性结构（加法，单个失败不阻断规划） ----------
    if _cfg_defaults().get("v3_pipeline", True):
        proj.log("📐 阶段2：生成 连续性结构（故事圣经/线索账本/时间线）…")
        try:
            _write_cfg_text(pdir, "09-故事圣经.md", "# 故事圣经\n\n" + workflow.gen_story_bible(cfg))
        except Exception as e:
            proj.log(f"⚠️ 故事圣经生成失败（跳过）：{e}")
        try:
            _write_cfg_text(pdir, "10-线索账本.md", "# 线索账本\n\n" + workflow.gen_clue_ledger(cfg, outline))
        except Exception as e:
            proj.log(f"⚠️ 线索账本生成失败（跳过）：{e}")
        try:
            _write_cfg_text(pdir, "12-时间线与连续性.md", "# 时间线与连续性\n\n" + workflow.gen_timeline(cfg, outline))
        except Exception as e:
            proj.log(f"⚠️ 时间线生成失败（跳过）：{e}")
        _write_cfg_text(pdir, "11-章节摘要.md",
                        "# 章节摘要\n\n> 每章写后更新：发生了什么 / 选择与代价 / 线索状态 / 时间线 / 未解决连贯性问题。\n")
    # P5：构建故事图谱（15-故事图谱.json），可配 p5_pipeline 关
    if _cfg_defaults().get("p5_pipeline", True):
        try:
            from . import graph as _graph
            _graph.build_graph(pdir, total_chapters=total)
            proj.log("🕸️ 已构建故事图谱（15）")
        except Exception as e:
            proj.log(f"⚠️ 故事图谱构建失败（跳过）：{e}")

    # 章节名以大纲表格为准
    chapter_titles = _parse_outline_titles(outline, total)
    plan = {
        "version": 1, "novelName": name, "projectPath": pdir,
        "totalChapters": total, "minWordsPerChapter": int(cfg.get("min_words_per_chapter") or 3000),
        "createdAt": datetime.datetime.now().isoformat(),
        "status": "planning", "writingMode": "serial", "config": cfg,
        "titles": titles,
        "chapters": [{"chapterNumber": i + 1, "title": t,
                      "filePath": "第%02d章-" % (i + 1) + re.sub(r'[\\/:*?\"<>]', '', t) + ".md",
                      "status": "pending", "wordCount": None,
                      "wordCountPass": None, "retryCount": 0}
                     for i, t in enumerate(chapter_titles)]}
    proj.save_plan(plan)
    proj.log(f"📐 阶段2 完成：《{name}》{total}章规划就绪")
    # v2 长篇分层规划：卷 → 弧 → 章卡（支持百万字规模与后续滚动重规划）
    _lf = (cfg.get("longform") if isinstance(cfg.get("longform"), dict)
           else (_cfg_defaults().get("longform") or {}))
    if _lf.get("enabled", True):
        try:
            from . import longform as _longform
            proj.log("📚 阶段2：生成分层规划（卷 / 弧 / 章卡）…")
            _longform.ensure_plan(proj, cfg)
            _longform.plan_book(proj, cfg, references=_plan_references(proj, cfg))
            d = _longform.plan_digest(proj)
            proj.log(f"📚 分层规划就绪：{d.get('chapters_planned')} 章 / "
                     f"{d.get('volumes')} 卷；目标 {d.get('scale', {}).get('target_words', 0)} 字")
        except Exception as e:
            proj.log(f"⚠️ 分层规划失败（可稍后重建，不影响开写）：{e}")
    return proj

# ---------- 阶段3/4 ----------

def append_chapters(proj: Project, extra):
    """续写：给已（或未完成）作品追加 extra 章，标为 continuation，状态回 in_progress。"""
    plan = proj.plan()
    if not plan:
        return
    total = plan.get("totalChapters") or len(plan.get("chapters", []))
    base = max(total, len(plan.get("chapters", [])))
    for k in range(1, int(extra) + 1):
        no = base + k
        plan["chapters"].append({
            "chapterNumber": no, "title": "续写",
            "filePath": f"第{no:02d}章-续写.md",
            "status": "pending", "wordCount": None, "wordCountPass": None,
            "retryCount": 0, "continuation": True})
    plan["totalChapters"] = base + int(extra)
    plan["status"] = "in_progress"
    proj.save_plan(plan)
    proj.log(f"➕ 续写：追加 {int(extra)} 章（第{base+1}–{base+int(extra)}章）")

def budget_blocked(proj) -> str:
    """返回预算耗尽提示（空串=未超）。单本预算来自全局 config.defaults.token_budget。"""
    cap = llm.budget_tokens()
    if not cap:
        return ""
    used = llm.book_tokens(os.path.basename(proj.path))
    if used < cap:
        return ""
    return f"已达 token 预算上限（已用 {used}/{cap}），写作暂停；调高预算或关闭上限后可继续"


def _pause_for_budget(proj, msg: str) -> None:
    plan = proj.plan() or {}
    meta = plan.setdefault("meta", {})
    meta["paused"] = True
    meta["budget_exceeded"] = msg
    plan["status"] = "paused"
    proj.save_plan(plan)
    proj.log("💰 " + msg)


def _ensure_writable_plan(proj) -> Optional[dict]:
    """写作前守卫：计划缺失或 0 章（规划线程被服务重启/异常杀掉）时先重建规划再写。
    没有它，「继续」会把 0 章的空书直接审校、成书、标记完结（v2.7 E2E 实测踩坑）。"""
    plan = proj.plan()
    if plan and (plan.get("chapters") or []):
        return plan
    proj.log("📐 写作计划缺失或没有章卡（规划中断）：先重建规划…")
    try:
        cfg = dict((plan or {}).get("config") or {})
        name = (plan or {}).get("novelName") or proj.name
        titles = (plan or {}).get("titles") or [
            {"title": name, "technique": "恢复", "why": "规划中断后重建"}]
        create_project(cfg, titles, pdir=proj.path, name=name)
        plan = proj.plan()
    except Exception as e:
        proj.log(f"💥 重建规划失败：{e}")
        plan = proj.plan()
    if not plan or not (plan.get("chapters") or []):
        try:
            p = proj.plan() or {}
            p["status"] = "planning"
            proj.save_plan(p)
        except Exception:
            pass
        proj.log("⛔ 仍无可写章节，停止（请检查模型规划调用是否正常后再「继续」）")
        return None
    return plan


def start_writing(proj: Project):
    if proj.running:
        return
    threading.Thread(target=_write_loop, args=(proj,), daemon=True).start()
    proj.running = True

# ---------- v0.4：暂停 / 恢复 / 即时指令队列（读者像 Agent 聊天一样随时控）----------
def _project_dir(key):
    return safe_join(PROJECTS_DIR, key, label="作品 key")

def is_paused(proj):
    return bool((proj.plan() or {}).get("meta", {}).get("paused"))

def pause_book(key):
    """置暂停位：写线程在下一检查点停，status=paused。"""
    p = Project(_project_dir(key), key)
    plan = p.plan()
    if not plan:
        return False
    plan.setdefault("meta", {})["paused"] = True
    plan["status"] = "paused"
    p.save_plan(plan)
    p.log("⏸ 已请求暂停（在下一检查点停，停在当前进度，可点「▶继续」恢复）")
    return True

def resume_book(key):
    """清暂停位 + 续写剩余章节。"""
    p = Project(_project_dir(key), key)
    plan = p.plan()
    if not plan:
        return False
    plan.setdefault("meta", {})["paused"] = False
    plan["status"] = "in_progress"
    p.save_plan(plan)
    p.log("▶ 继续写作（从上次进度接着写剩余章节）")
    start_writing(p)
    return True

# ---------- v3.1.0：陈旧状态恢复（服务重启/线程死亡后的 validating 卡死） ----------

# ---------- v3.1.0：非小说流水线运行标志（跨实例可见；计数器防多线程误清） ----------
_PIPELINE_LOCK = threading.Lock()
_PIPELINE_COUNT: dict[str, int] = {}


def mark_pipeline(key, on=True):
    """按作品 key 增/减流水线运行计数（线程启动 +1，结束 -1）。"""
    k = os.path.basename(str(key or ""))
    if not k:
        return
    with _PIPELINE_LOCK:
        _PIPELINE_COUNT[k] = max(0, _PIPELINE_COUNT.get(k, 0) + (1 if on else -1))


def pipeline_running(key) -> bool:
    return _PIPELINE_COUNT.get(os.path.basename(str(key or "")), 0) > 0


def _chapter_has_content(proj, c, min_words):
    """章文件是否有实质内容（≥ 0.6*min 字），供陈旧恢复判定。"""
    fp = _chapter_file_path(proj, c)
    if not fp or not os.path.exists(fp):
        return False
    try:
        txt = open(fp, encoding="utf-8").read()
        wc = len(txt.replace(" ", "").replace("\n", ""))
        return wc >= int(min_words * 0.6)
    except Exception:
        return False


def recover_stale_book(key, live_proj=None):
    """v3.1.0：校验中/放行中但写作线程已死（服务重启）的书自动救回：
    有实质内容的 in_progress/failed 章复位 completed，status 回 in_progress（可「继续」）。
    幂等：非 stale 状态直接返回 None。返回复位章数或 None。
    live_proj：调用方若持有写作线程使用的同一 Project 实例（app.PROJECT_INDEX），
    其 running 标志才是「线程还活着」的权威来源；临时实例一律 False 不可信。"""
    try:
        p = live_proj if live_proj is not None else Project(_project_dir(key), key)
        if p.path != _project_dir(key):
            return None
    except ValueError:
        return None
    plan = p.plan()
    if not plan or plan.get("status") not in ("validating", "releasing"):
        return None
    if p.running:
        return None   # 线程还活着，不动
    min_words = plan.get("minWordsPerChapter", 3000)
    changed = 0
    for c in (plan.get("chapters") or []):
        if c.get("status") in ("in_progress", "failed") and _chapter_has_content(p, c, min_words):
            c["status"] = "completed"
            if not c.get("wordCount"):
                try:
                    fp = _chapter_file_path(p, c)
                    c["wordCount"] = len(open(fp, encoding="utf-8").read().replace(" ", "").replace("\n", ""))
                except Exception:
                    pass
            changed += 1
    plan["status"] = "in_progress"
    p.save_plan(plan)
    p.log(f"🛟 检测到「{plan.get('status') or 'validating'}」状态残留（写作线程已中断），"
          f"已复位 {changed} 章并恢复为可继续；说「继续」即可续跑校验收尾。")
    try:
        rebuild_book(p)   # 成书文件同步到当前章节状态
    except Exception:
        pass
    return changed


def recover_all_stale():
    """启动时全量扫一遍（后台线程调用，绝不阻塞 boot）。"""
    n = 0
    try:
        for d in sorted(os.listdir(PROJECTS_DIR)):
            if not os.path.isdir(os.path.join(PROJECTS_DIR, d)):
                continue
            if recover_stale_book(d) is not None:
                n += 1
    except Exception:
        pass
    return n


def queue_msg(key, text, immediate=False):
    """存一条读者即时指令。immediate=下一章立即用；否则下一条生效（用完标 consumed）。"""
    p = Project(_project_dir(key), key)
    plan = p.plan()
    if not plan:
        return False
    text = (text or "").strip()
    if not text:
        return False
    qs = plan.setdefault("meta", {}).setdefault("queued_msgs", [])
    qs.append({"text": text, "immediate": bool(immediate), "consumed": False,
               "ts": datetime.datetime.now().isoformat()})
    p.save_plan(plan)
    p.log(("⚡ 立即修正指令：「" if immediate else "📝 待发送指令（下一条生效）：「") + text[:40] + "」")
    # v2.3.0 任务三：用户即时改稿指令计入学习样本（技能进化界面可见改稿口味）；异常吞掉不影响入队
    try:
        from . import learn as _learn
        _learn.note_user_directive(key, text)
    except Exception:
        pass
    # ── v2.4.0 修复：queue_msg 存完后自动触发写作 ──
    # 原问题：queue_msg 只存消息不启动写作线程，导致"并入创作"后无后续
    # 修复：如果没有写作线程在跑且存在 pending 章节，自动 start_writing
    try:
        _has_pending = any(
            ch.get("status") not in ("completed", "writing", "in_progress")
            for ch in (plan.get("chapters") or [])
        )
        if _has_pending and not p.running:
            plan["status"] = "in_progress"
            p.save_plan(plan)
            start_writing(p)
            p.log("▶ 检测到未完成章节，自动启动写作线程")
    except Exception:
        pass
    return True

def pending_queue(key):
    """尚未消化的读者指令列表（给 UI 显示「待发送」），带原始下标 i 供移除/标记用。"""
    p = Project(_project_dir(key), key)
    qm = (p.plan() or {}).get("meta", {}).get("queued_msgs") or []
    return [{"i": i, "text": q.get("text", ""), "immediate": bool(q.get("immediate"))}
            for i, q in enumerate(qm) if not q.get("consumed")]


def queue_action(key, index, action):
    """对某条待发送指令：'remove'=移除 / 'imm'=标记立即生效。返回剩余（未消化）列表。"""
    p = Project(_project_dir(key), key)
    plan = p.plan() or {}
    qm = plan.get("meta", {}).get("queued_msgs") or []
    if 0 <= index < len(qm):
        if action == "remove":
            qm.pop(index)
        elif action == "imm":
            qm[index]["immediate"] = True
    p.save_plan(plan)
    return [q for q in qm if not q.get("consumed")]

def _pop_queue_note(proj, ch_no):
    with proj.lock:
        plan = proj.plan() or {}
        qs = (plan.get("meta") or {}).get("queued_msgs") or []
        if not qs:
            return ""
        active = [q for q in qs if not q.get("consumed")]
        if not active:
            return ""
        imm = [q for q in active if q.get("immediate")]
        pick = imm[-1] if imm else active[-1]
        pick["consumed"] = True   # 无论「立即」还是「排队」，本章用一次即消，避免同一指令重复注入后续每章
        try:
            proj.save_plan(plan)   # 必须保存被修改的这一份；重新 plan() 会丢掉 consumed 标记
        except Exception:
            pass
    return (pick.get("text") or "").strip()

def _chapter_file_path(proj, ch):
    return os.path.join(proj.path, ch["filePath"])

def _book_skills_into(cfg, plan):
    """v3.0.0：把计划 meta.agent_skills（按作品岗位技能绑定）注入 cfg，
    供 workflow.agent_skills_block 双维度合并。原 cfg 就地补键。"""
    try:
        if isinstance(cfg, dict) and isinstance(plan, dict):
            cfg.setdefault("_book_agent_skills",
                           (plan.get("meta") or {}).get("agent_skills") or {})
    except Exception:
        pass
    return cfg


def _build_ctx(proj, ch):
    """v2：优先用分层上下文编译器（章卡/状态库/伏笔/预算/参考资料）；
    任何异常都退回旧版拼装，保证老项目与弱环境仍可写。"""
    plan = proj.plan() or {}
    cfg = plan.get("config") if isinstance(plan.get("config"), dict) else {}
    _book_skills_into(cfg, plan)   # v3.0.0：按作品岗位技能绑定
    _lf = cfg.get("longform") if isinstance(cfg.get("longform"), dict) else None
    _lf = _lf or (_cfg_defaults().get("longform") or {})
    if _lf.get("enabled", True):
        try:
            from . import context as _context
            ctx = _context.build(proj, ch, cfg, None)
            # 每章按本章问题动态检索参考资料，覆盖建档时的静态摘要
            try:
                from . import references as _refs
                ids = _refs.project_ref_ids(os.path.basename(proj.path))
                if ids:
                    q = " ".join(str(x) for x in (
                        ch.get("title") or "",
                        (ctx.get("longform_card") or {}).get("goal") or "",
                        (ctx.get("prev_tail") or "")[-500:]) if x)
                    block = _refs.context_block(q, ids,
                                                max_chars=int(_lf.get("context_chars", 4000)))
                    if block:
                        ctx["references_block"] = block
            except Exception:
                pass
            ctx["queue_note"] = ""
            return ctx
        except Exception as e:
            try:
                proj.log(f"⚠️ 分层上下文不可用，已回退旧版：{e}")
            except Exception:
                pass
    return _build_ctx_legacy(proj, ch)


def _build_ctx_legacy(proj, ch):
    plan = proj.plan()
    cfg = plan["config"]
    def _rf(fname):
        fp = os.path.join(proj.path, fname)
        return open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
    outline_text = _rf("01-大纲.md") or open(os.path.join(proj.path, "01-大纲.md"), encoding="utf-8").read()
    characters_text = _rf("00-人物档案.md")
    style_text = _rf("03-文风基准.md")
    # v0.3 P2：题材×风格技能（style.json）→ 追加到 style_text，write/expand craft 自动带上
    try:
        import json as _js
        _sp = os.path.join(proj.path, "style.json")
        if os.path.exists(_sp):
            _sk = _js.load(open(_sp, encoding="utf-8"))
            _d = _sk.get("style") or {}
            _parts = [f"{k}：{v}" for k, v in [
                ("题材", _sk.get("genre", "")), ("基调", _sk.get("tone", "")), ("视角", _sk.get("perspective", "")),
                ("金手指", _d.get("金手指", "")), ("风格基调", _d.get("基调", "")), ("节奏", _d.get("节奏", "")),
                ("语言", _d.get("语言", "")), ("卷", _d.get("卷", "")),
                ("发布平台", _sk.get("platform", "")),
                ("卖点", "、".join(str(s) for s in (_sk.get("sells") or [])))] if v]
            if _parts:
                style_text += "\n\n## 创作风格技能（本作品按此技能设定书写，严格遵守）\n" + "\n".join(_parts)
            if _sk.get("platform_note"):
                style_text += ("\n\n## 平台节奏要求（" + str(_sk.get("platform", "")) + "）\n"
                               + str(_sk["platform_note"]))
    except Exception:
        pass
    # v0.3.4：专属蒸馏风格（style_ref.json）→ 追加注入，按该风格技法书写
    try:
        import json as _js2
        _sr = os.path.join(proj.path, "style_ref.json")
        if os.path.exists(_sr):
            _sc = _js2.load(open(_sr, encoding="utf-8"))
            if _sc.get("style_prompt"):
                style_text += ("\n\n## 专属蒸馏风格（" + str(_sc.get("name", "")) + "，按此技法写，不改变剧情）\n"
                               + _sc["style_prompt"])
    except Exception:
        pass
    prev_tail = ""
    if ch["chapterNumber"] > 1:
        prev = plan["chapters"][ch["chapterNumber"] - 2]
        pp = os.path.join(proj.path, prev["filePath"])
        if os.path.exists(pp):
            prev_tail = open(pp, encoding="utf-8").read()[-1200:]
    from .workflow import outline_chapter_row
    chapter_plan = outline_chapter_row(outline_text, ch["chapterNumber"])
    ctx = {"chapter_no": ch["chapterNumber"], "chapter_title": ch["title"],
           "outline_text": outline_text, "chapter_plan": chapter_plan,
           "characters_text": characters_text[:8000], "style_text": style_text[:6000],
           "prev_tail": prev_tail, "cfg": cfg,
           "continuation": bool(ch.get("continuation")),
           # 连续性结构（旧项目缺失则为空串，_continuity_block 自动省略）
           "story_bible": _rf("09-故事圣经.md"), "clue_ledger": _rf("10-线索账本.md"),
           "chapter_summaries": _rf("11-章节摘要.md"), "timeline": _rf("12-时间线与连续性.md")}
    # Lorebook：只注入本章相关的设定词条（避免整本圣经塞满上下文）
    ctx["lore_relevant"] = ""
    if _cfg_defaults().get("lorebook", True):
        try:
            from . import lorebook
            q = f"{ch['title']} {chapter_plan[:300]} {prev_tail[-500:]}"
            ctx["lore_relevant"] = lorebook.relevant(proj.path, q)
        except Exception:
            pass
    # P5：故事图谱相关实体 + 语义检索既往片段（可配 p5_pipeline 关；旧项目无 15 文件则降级为空）
    ctx["graph_ctx"], ctx["retrieval_ctx"] = {}, []
    if _cfg_defaults().get("p5_pipeline", True):
        try:
            from . import graph as _graph, retrieval as _retr
            ctx["graph_ctx"] = _graph.active_context(proj.path, ch["chapterNumber"])
            q = f"{ch['title']} {chapter_plan[:140]}"
            ctx["retrieval_ctx"] = _retr.retrieve(q, proj.path, k=3, exclude_ch=(ch["chapterNumber"] - 1))
        except Exception as e:
            ctx["graph_ctx"], ctx["retrieval_ctx"] = {}, []
    # v0.6 B：总架构蓝图注入（写任何一章前先对齐全局设定/人物/文风/连续性）
    try:
        ctx["blueprint"] = blueprint_block(proj, ch["chapterNumber"])
    except Exception:
        ctx["blueprint"] = ""
    # v0.9.0：写作骨架注入 —— 续写/改写前先读总体状态（写到哪/多少/重点/为何/续写提示），快速对齐
    try:
        _sk = _rf("17-写作骨架.md", 3200)
        if _sk.strip():
            ctx["blueprint"] = (ctx.get("blueprint") or "") + "\n\n## 写作骨架（总体状态，动手前先读）\n" + _sk.strip()
    except Exception:
        pass
    return ctx

# ---------- v0.6 B：总架构（蓝图）+ 并行章节创作 ----------
BLUEPRINT_JSON = "02b-总架构.json"
BLUEPRINT_MD = "02b-总架构.md"

def _rule_blueprint(proj, plan):
    """无 LLM 熔炼时的规则式兜底蓝图（从散件提取，保证并行写作始终有『总架构』可读）。"""
    def _rf(f):
        p = os.path.join(proj.path, f)
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    from .workflow import outline_chapter_row
    outline = _rf("01-大纲.md")
    beats = []
    for c in (plan.get("chapters") or []):
        no = c.get("chapterNumber")
        row = (outline_chapter_row(outline, no) or "").strip() if outline else ""
        beats.append({"no": no, "title": c.get("title", f"第{no}章"), "goal": row[:160],
                     "events": "", "hook": "", "continuity": ""})
    chars = []
    for m in re.finditer(r'(?:^|\n)\s*[-#*]?\s*\**([^：:*\n]{1,16})\**\s*[：:]\s*(.+)', _rf("00-人物档案.md")):
        chars.append({"name": m.group(1).strip(), "role": m.group(2).strip()[:60], "arc": "", "quirk": ""})
        if len(chars) >= 24:
            break
    return {"world": _rf("09-故事圣经.md")[:800], "characters": chars,
            "style": _rf("03-文风基准.md")[:500], "beats": beats,
            "continuity_rules": _rf("12-时间线与连续性.md")[:600]}

def ensure_blueprint(proj):
    """生成/复用『总架构』单一事实源（02b-总架构）：先熔炼成单一蓝图，失败则规则式兜底。
    所有并行写作的 Agent 写任何一章前都先读它，保证又快又一致。"""
    jpath = os.path.join(proj.path, BLUEPRINT_JSON)
    if os.path.exists(jpath):
        try:
            cached = json.load(open(jpath, encoding="utf-8"))
            # v0.6 B：缓存命中但 .md 缺失/为空（旧 bug 遗留）→ 从 .json 重生成，保证查看器有内容
            mdpath = os.path.join(proj.path, BLUEPRINT_MD)
            if (not os.path.exists(mdpath) or os.path.getsize(mdpath) == 0):
                try:
                    open(mdpath, "w", encoding="utf-8").write(_blueprint_md(cached))
                except Exception:
                    pass
            return cached
        except Exception:
            pass
    plan = proj.plan() or {}
    cfg = plan.get("config") or {}
    bp = None
    if _cfg_defaults().get("blueprint", True):
        try:
            proj.log("🧩 熔炼『总架构』全局蓝图（写任何一章前统一对齐）…")
            def _f(n):
                p = os.path.join(proj.path, n)
                return open(p, encoding="utf-8").read() if os.path.exists(p) else ""
            ch_list = "\n".join(f"第{c.get('chapterNumber')}章 {c.get('title','')}" for c in (plan.get("chapters") or []))
            src = (f"【大纲】\n{_f('01-大纲.md')[:4000]}\n\n【人物】\n{_f('00-人物档案.md')[:2500]}\n\n"
                   f"【文风】\n{_f('03-文风基准.md')[:1500]}\n\n【故事圣经】\n{_f('09-故事圣经.md')[:2000]}\n\n"
                   f"【时间线/连续性】\n{_f('12-时间线与连续性.md')[:1500]}\n\n【章节列表】\n{ch_list}")
            bp = llm.chat_json([
                {"role": "system", "content": "你是『总架构熔炼师』。把散落的小说设定熔炼成一份唯一、紧凑、机器可读的全局蓝图 JSON，供多个写作 Agent 并行写章时严格对齐。只抽取设定/技法，不复制他人文字。严格输出 JSON，不要 markdown 代码块。"},
                {"role": "user", "content": src + '\n\n请输出 JSON：{"world":"一句话到一段的世界观/硬性设定","characters":[{"name","role","arc","quirk"}],"style":"文风基调一句话","beats":[{"no","title","goal","events","hook","continuity"}],"continuity_rules":"连续性铁律要点"}'}
            ], cfg=cfg, stage="planning")
            if not isinstance(bp, dict) or "beats" not in bp:
                raise ValueError("熔炼结果缺 beats 字段")
        except Exception as e:
            proj.log(f"⚠️ 总架构熔炼失败，改用规则式蓝图：{e}")
            bp = None
    if not bp:
        bp = _rule_blueprint(proj, plan)
    atomic_write_json(jpath, bp)
    open(os.path.join(proj.path, BLUEPRINT_MD), "w", encoding="utf-8").write(_blueprint_md(bp))
    proj.log("🧩 总架构就绪（02b-总架构，写任何一章前先读它）")
    return bp

def _s(x, cap=0):
    """v0.6 B：把蓝图字段规整成纯文本（LLM 可能给 list/dict），空给 ''。"""
    if x is None:
        return ""
    if isinstance(x, (list, tuple)):
        v = "；".join(str(y) for y in x if y not in (None, ""))
    elif isinstance(x, dict):
        v = "；".join(f"{k}:{val}" for k, val in x.items() if val not in (None, ""))
    else:
        v = str(x).strip()
    return v[:cap] if cap else v

def _blueprint_md(bp):
    L = ["# 总架构（全局蓝图 · 所有写作 Agent 写章前统一对齐）", ""]
    L += ["## 世界观/设定", _s(bp.get("world")) or "（略）", ""]
    L += ["## 人物", ""]
    for c in (bp.get("characters") or [])[:30]:
        if isinstance(c, str):
            L.append("- " + c); continue
        L.append(f"- **{c.get('name','')}**（{_s(c.get('role'))}" + (f" 弧线：{_s(c.get('arc'))}" if c.get("arc") else "") + (f"；{_s(c.get('quirk'))}" if c.get("quirk") else "") + "）")
    L += ["", "## 全章节拍", ""]
    for b in (bp.get("beats") or []):
        if isinstance(b, str):
            L.append("- " + b); continue
        L.append(f"### 第{b.get('no','?')}章 {b.get('title','')}")
        L.append(f"- 目标：{_s(b.get('goal'))}\n- 事件：{_s(b.get('events'))}\n- 钩子：{_s(b.get('hook'))}\n- 承接：{_s(b.get('continuity'))}")
    L += ["", "## 文风", _s(bp.get("style")) or "（略）", "", "## 连续性铁律", _s(bp.get("continuity_rules")) or "（略）"]
    return "\n".join(L)

def blueprint_block(proj, ch_no):
    """给单章写作注入的『总架构』紧凑块（全局 + 本章/邻章节拍），写任何一章前必读。"""
    jpath = os.path.join(proj.path, BLUEPRINT_JSON)
    if not os.path.exists(jpath):
        return ""
    try:
        bp = json.load(open(jpath, encoding="utf-8"))
    except Exception:
        return ""
    L = ["【总架构 · 全局蓝图（写本章前必读，严格对齐设定/人物/文风/连续性）】"]
    if bp.get("world"):
        L.append("· 世界观/设定：" + _s(bp["world"], 400))
    if bp.get("characters"):
        cs = "；".join(f"{_s(c.get('name'))}(角色:{_s(c.get('role'))}" + (f"/弧线:{_s(c.get('arc'))}" if c.get("arc") else "") + ")"
                       for c in (bp["characters"] if isinstance(bp["characters"], list) else [bp["characters"]])[:14]
                       if isinstance(c, dict))
        L.append("· 人物：" + cs[:900])
    if bp.get("style"):
        L.append("· 文风：" + _s(bp["style"], 300))
    if bp.get("continuity_rules"):
        L.append("· 连续性铁律：" + _s(bp["continuity_rules"], 400))
    beats = bp.get("beats") or []
    no_set = [b.get("no") for b in beats if isinstance(b, dict)]
    def _bt(no):
        b = next((x for x in beats if isinstance(x, dict) and x.get("no") == no), None)
        if not b:
            return f"第{no}章（无独立节拍）"
        return (f"第{b.get('no')}章「{b.get('title','')}」目标:{b.get('goal','')} "
                f"事件:{b.get('events','')} 钩子:{b.get('hook','')} 承接:{b.get('continuity','')}")
    if ch_no > 1:
        L.append("· 上一章（承接）：" + _bt(ch_no - 1))
    L.append("· 本章（重点）：" + _bt(ch_no))
    if no_set and ch_no < max(no_set):
        L.append("· 下一章（预告对齐）：" + _bt(ch_no + 1))
    return "\n".join(L)[:3500]

_LF_GUARD: dict[str, int] = {}
_LF_GUARD_LOCK = threading.Lock()


def _longform_cfg(proj):
    plan = proj.plan() or {}
    cfg = plan.get("config") if isinstance(plan.get("config"), dict) else {}
    lf = cfg.get("longform") if isinstance(cfg.get("longform"), dict) else None
    return cfg, (lf or (_cfg_defaults().get("longform") or {}))


def _ensure_layered_plan(proj):
    """老项目开写前迁移成卷/弧/章卡；纯文件操作，不花 LLM，失败不阻断。"""
    cfg, lf = _longform_cfg(proj)
    if not lf.get("enabled", True):
        return
    try:
        from . import longform as _lfmod
        _lfmod.ensure_plan(proj, cfg)
    except Exception as e:
        try:
            proj.log(f"⚠️ 分层规划迁移失败（沿用扁平大纲）：{e}")
        except Exception:
            pass


def _maybe_replan(proj, next_no):
    """每 N 章 / 弧卷边界 / 伏笔逾期时，滚动规划未来窗口的章卡。"""
    cfg, lf = _longform_cfg(proj)
    if not lf.get("enabled", True):
        return
    try:
        from . import longform as _lfmod
        need, reason = _lfmod.should_replan(proj, next_no, cfg)
        if not need:
            return
        with _LF_GUARD_LOCK:
            if _LF_GUARD.get(proj.path, 0) >= next_no:
                return
            _LF_GUARD[proj.path] = next_no
        window = max(4, int(cfg.get("window_chapters") or lf.get("window_chapters") or 12))
        start, end = int(next_no), int(next_no) + window - 1
        proj.log(f"🧭 滚动重规划：{reason}（第{start}–{end}章）")
        res = _lfmod.plan_window(proj, start, end, reason=reason)
        if isinstance(res, dict) and res.get("updated"):
            proj.log(f"🧭 已更新 {res['updated']} 张章卡（{res.get('source')}）")
    except Exception as e:
        with _LF_GUARD_LOCK:
            if _LF_GUARD.get(proj.path) == next_no:
                _LF_GUARD.pop(proj.path, None)
        try:
            proj.log(f"⚠️ 滚动重规划失败（继续按现有规划写）：{e}")
        except Exception:
            pass


def _audit_at_boundary(proj, cfg, no):
    """弧/卷收束时做确定性体检，把问题写进 22-规划体检.json，供下一轮重规划参考。"""
    lf = (cfg or {}).get("longform") if isinstance((cfg or {}).get("longform"), dict) else None
    lf = lf or (_cfg_defaults().get("longform") or {})
    if not lf.get("enabled", True):
        return
    try:
        from . import longform as _lfmod
        plan = _lfmod.ensure_plan(proj) or {}
        no = int(no)
        vol_end = None
        arc_end = None
        for v in plan.get("volumes") or []:
            if not isinstance(v, dict):
                continue
            if int(v.get("end_chapter") or 0) == no:
                vol_end = int(v.get("no") or 0)
            for a in v.get("arcs") or []:
                if isinstance(a, dict) and int(a.get("end_chapter") or 0) == no:
                    arc_end = int(a.get("no") or 0)
        if vol_end:
            scope, issues = f"volume:{vol_end}", _lfmod.audit_volume(proj, vol_end)
        elif arc_end:
            scope, issues = f"arc:{arc_end}", _lfmod.audit_arc(proj, arc_end)
        else:
            return
        total = int((plan.get("scale") or {}).get("chapters") or 0)
        if total and no >= total:
            scope = "book"
            issues = _lfmod.audit_book(proj) or issues
        if issues:
            proj.log(f"🩺 规划体检（{scope}）：{len(issues)} 项待关注，已记录")
            atomic_write_json(os.path.join(proj.path, "22-规划体检.json"),
                              {"at": datetime.datetime.now().isoformat(),
                               "scope": scope, "chapter": no, "issues": issues[:50]})
    except Exception as e:
        try:
            proj.log(f"⚠️ 规划体检失败（不阻断）：{e}")
        except Exception:
            pass


def _index_chapter_continuity(proj, ch, cfg):
    """成章定稿后写状态库：摘要/伏笔/知情范围；参考重合只告警。绝不阻断成章。"""
    lf = (cfg or {}).get("longform") if isinstance((cfg or {}).get("longform"), dict) else None
    lf = lf or (_cfg_defaults().get("longform") or {})
    if not lf.get("enabled", True):
        return
    no = ch["chapterNumber"]
    fp = _chapter_file_path(proj, ch)
    if not os.path.exists(fp):
        return
    try:
        text = open(fp, encoding="utf-8").read()
    except OSError:
        return
    try:
        from . import longform as _lfmod
        res = _lfmod.summarize_and_extract(proj, no, text, cfg)
        proj.log(f"🧠 第{no}章连续性入库：摘要 {len(res.get('summary') or '')} 字，"
                 f"伏笔操作 {len(res.get('foreshadow_ops') or [])} 条，"
                 f"问题 {len(res.get('issues') or [])} 条")
    except Exception as e:
        proj.log(f"⚠️ 第{no}章连续性提取失败（不阻断）：{e}")
    try:
        from . import references as _refs
        ids = _refs.project_ref_ids(os.path.basename(proj.path))
        if ids:
            ov = _refs.overlap_check(text, ids)
            if ov.get("matches"):
                proj.log(f"⚠️ 第{no}章与参考资料存在 {len(ov['matches'])} 处长句重合，建议改为转述")
    except Exception:
        pass
    _audit_at_boundary(proj, cfg, no)


# ---------- v2.3.0：里程碑标题回流（基于已成稿内容再生成书名候选） ----------
TITLE_CANDIDATES_FILE = "05-标题候选.json"   # 与另一 Agent 的接口约定文件名
_TITLE_MILESTONE_STEP = 10                    # 每累计完成 10 章触发一次
_TITLE_CAP = 20                               # 候选封顶条数


def _recent_chapter_digests(proj, k=3) -> str:
    """取最近 k 章的成稿摘要（供标题回流的提示词）；来自 longform 的章摘要账本，缺失则空串。"""
    try:
        from . import longform as _lf
        rows = _lf._load_chapter_summaries(proj) or {}
        out = []
        for n in sorted(rows, reverse=True)[:k]:
            sm = (rows[n] or {}).get("summary") if isinstance(rows[n], dict) else ""
            if sm:
                out.append(f"第{n}章：{str(sm)[:200]}")
        return "\n".join(reversed(out))
    except Exception:
        return ""


def _gen_milestone_titles(proj, plan, cfg):
    """基于大纲摘要 + 最近 3 章成稿摘要，让 LLM 给出 3 个更贴合已成稿、更能抓读者的书名候选。
    走 llm.chat_json（与 gen_titles 内部同一风格，但能吃进真实成稿内容，更贴合'基于实际写出的内容'）。
    返回 [{title, technique, why}]；解析不出返回 []（异常由上层 _maybe_backflow_titles 兜住）。"""
    outline = ""
    try:
        op = os.path.join(proj.path, "01-大纲.md")
        if os.path.exists(op):
            outline = open(op, encoding="utf-8").read()[:1200]
    except OSError:
        outline = ""
    digests = _recent_chapter_digests(proj)
    sys_p = (
        "你是「一幕·神笔」的书名策划。这本书已在连载，请基于【已实际成稿】的内容（不是仅凭初始设定），"
        "再想 3 个更贴合已成稿卖点/爽点、更能抓住读者的书名。每个书名注明所用技巧与一句话理由。"
        '严格输出 JSON：{"titles":[{"title":..,"technique":..,"why":..}]}。只输出 JSON。')
    user_p = (
        f"【初始配置】{json.dumps({k: cfg.get(k) for k in ('genre','idea','conflict','protagonist','theme','tone') if cfg.get(k)}, ensure_ascii=False)}\n"
        f"【大纲摘要】\n{outline or '（无）'}\n"
        f"【最近成稿摘要】\n{digests or '（暂无摘要，按大纲与配置给名）'}\n"
        "【要求】结合上面已成稿剧情给出 3 个书名，比初始候选更抓读者。")
    out = llm.chat_json([{"role": "system", "content": sys_p},
                         {"role": "user", "content": user_p}], cfg=cfg, stage="planning")
    titles = out.get("titles") if isinstance(out, dict) else None
    if not isinstance(titles, list):
        return []
    res = []
    for t in titles[:3]:
        if isinstance(t, dict) and str(t.get("title") or "").strip():
            res.append({"title": str(t["title"]).strip(),
                        "technique": str(t.get("technique") or "").strip(),
                        "why": str(t.get("why") or "").strip()})
    return res


def _append_title_candidates(proj, items) -> int:
    """把 items 追加进 <作品目录>/05-标题候选.json；按 title 去重、封顶 _TITLE_CAP 条。返回实际新增条数。
    契约格式（与另一 Agent 一字不差）：{"updated_at":..., "items":[{"title","technique","why","at"}]}。"""
    fp = os.path.join(proj.path, TITLE_CANDIDATES_FILE)
    existing = []
    if os.path.exists(fp):
        try:
            loaded = json.load(open(fp, encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = [x for x in (loaded.get("items") or []) if isinstance(x, dict)]
        except Exception:
            existing = []
    seen = {str(x.get("title") or "").strip() for x in existing}
    now = datetime.datetime.now().isoformat(timespec="seconds")
    added = 0
    for it in items or []:
        title = str(it.get("title") or "").strip()
        if not title or title in seen:
            continue
        existing.append({"title": title, "technique": it.get("technique", ""),
                         "why": it.get("why", ""), "at": now})
        seen.add(title)
        added += 1
    doc = {"updated_at": now, "items": existing[-_TITLE_CAP:]}
    atomic_write_json(fp, doc)
    return added


# ---------- v3.1.0：章名回流（默认“第N章”的章写完/里程碑时起真名） ----------

def _set_chapter_title(proj, no, title):
    """回填章名：计划 + 章节文件头 + 02d 章卡（best-effort）。filePath 不动（保护版本/快照）。"""
    title = re.sub(r'[\\/:*?"<>|\n\r]', "", str(title or "").strip())[:16]
    if not title:
        return False
    ch = None
    with proj.lock:
        plan = proj.plan() or {}
        ch = next((c for c in (plan.get("chapters") or []) if c.get("chapterNumber") == no), None)
        if not ch:
            return False
        old = ch.get("title") or ""
        if old == title:
            return False
        ch["title"] = title
        proj.save_plan(plan)
    # 章节文件头：# 第NN章 旧名 → 新名
    try:
        fp = _chapter_file_path(proj, ch)
        if fp and os.path.exists(fp):
            head = f"# 第{no:02d}章 "
            txt = open(fp, encoding="utf-8").read()
            if txt.startswith(head):
                rest = txt.split("\n", 1)
                new_head = f"# 第{no:02d}章 {title}"
                open(fp, "w", encoding="utf-8").write(new_head + ("\n" + rest[1] if len(rest) > 1 else "\n"))
    except Exception:
        pass
    # 02d 章卡标题同步（best-effort，失败不影响计划）
    try:
        from . import longform as _lf
        cards_fp = os.path.join(proj.path, "02d-章卡.jsonl")
        if os.path.exists(cards_fp):
            lines = [l for l in open(cards_fp, encoding="utf-8").read().splitlines() if l.strip()]
            changed = False
            out = []
            for l in lines:
                try:
                    d = json.loads(l)
                except Exception:
                    out.append(l); continue
                if d.get("n") == no and d.get("title") != title:
                    d["title"] = title
                    changed = True
                out.append(json.dumps(d, ensure_ascii=False))
            if changed:
                atomic_write_text(cards_fp, "\n".join(out) + "\n")
    except Exception:
        pass
    return True


def _is_default_title(t):
    return bool(re.fullmatch(r"第\d+章", (t or "").strip()))


def _name_chapter(proj, no, digest):
    """写完一章后用本章摘要起 4-8 字章名（已有真名则跳过）。零额外读盘：digest 来自 chapter_meta。"""
    try:
        if not digest or len(digest) < 20 or not _cfg_defaults().get("chapter_naming", True):
            return
        plan = proj.plan() or {}
        ch = next((c for c in (plan.get("chapters") or []) if c.get("chapterNumber") == no), None)
        if not ch or not _is_default_title(ch.get("title")):
            return
        out = llm.chat_json(
            [{"role": "system",
              "content": "给小说章节起名。根据本章摘要输出 4-8 字章名，像实体书目录那样有信息量与悬念，"
                         "不剧透结局，不要出现“第X章”字样或书名号。只输出 JSON：{\"title\": \"...\"}"},
             {"role": "user", "content": str(digest)[:600]}],
            stage="naming")
        t = str((out or {}).get("title") or "").strip()
        if t and _set_chapter_title(proj, no, t):
            proj.log(f"🏷️ 第{no}章定名「{t}」")
    except Exception:
        pass


def _backfill_chapter_names(proj, limit=10):
    """里程碑批量回填：把仍是默认标题的已完成章，用 21-章摘要.jsonl 批量起名（1 次 LLM 调用）。"""
    try:
        plan = proj.plan() or {}
        todo = [c for c in (plan.get("chapters") or [])
                if c.get("status") == "completed" and _is_default_title(c.get("title"))][:limit]
        if not todo:
            return 0
        summaries = {}
        fp = os.path.join(proj.path, "21-章摘要.jsonl")
        if os.path.exists(fp):
            for line in open(fp, encoding="utf-8"):
                try:
                    d = json.loads(line)
                    summaries[d.get("n")] = d.get("summary") or ""
                except Exception:
                    pass
        items = [{"no": c["chapterNumber"], "summary": (summaries.get(c["chapterNumber"]) or "")[:300]}
                 for c in todo]
        items = [i for i in items if i["summary"]]
        if not items:
            return 0
        out = llm.chat_json(
            [{"role": "system",
              "content": "给下列小说章节各起一个 4-8 字章名，像实体书目录那样有信息量与悬念，不剧透结局。"
                         '只输出 JSON：{"names":[{"no":1,"title":"..."},...]}，no 必须与输入一致。'},
             {"role": "user", "content": json.dumps(items, ensure_ascii=False)}],
            stage="naming")
        n = 0
        for it in (out or {}).get("names") or []:
            try:
                if _set_chapter_title(proj, int(it.get("no") or 0), str(it.get("title") or "")):
                    n += 1
            except (TypeError, ValueError):
                continue
        if n:
            proj.log(f"🏷️ 章名回填：{n} 章定名")
        return n
    except Exception as e:
        try:
            proj.log(f"⚠️ 章名回填失败（不阻断写作）：{e}")
        except Exception:
            pass
        return 0


def _maybe_backflow_titles(proj, ch_no=None):
    """v2.3.0 任务一挂点：每累计完成 10 章触发一次标题回流，追加 3 个更贴合的书名候选。
    用 plan['meta']['title_milestone'] 记录上次回流时的里程碑（10 的整数倍）；在 proj.lock 内读改写，
    先把里程碑推到位再生成，并行 worker 下不会双跑。全流程防御：任何异常都不抛出、不中断写作。"""
    try:
        with proj.lock:
            plan = proj.plan() or {}
            done = sum(1 for c in (plan.get("chapters") or []) if c.get("status") == "completed")
            target = (done // _TITLE_MILESTONE_STEP) * _TITLE_MILESTONE_STEP
            meta = plan.setdefault("meta", {})
            last = int(meta.get("title_milestone") or 0)
            if target <= 0 or target <= last:
                return None
            meta["title_milestone"] = target
            proj.save_plan(plan)
        cfg = plan.get("config") if isinstance(plan.get("config"), dict) else {}
        items = _gen_milestone_titles(proj, plan, cfg)
        if not items:
            return None
        added = _append_title_candidates(proj, items)
        if added:
            proj.log(f"📚 新增 {added} 个标题候选（第{target}章里程碑）")
        _backfill_chapter_names(proj)   # v3.1.0：里程碑同时批量回填默认章名
        return added
    except Exception as e:
        try:
            proj.log(f"⚠️ 标题候选回流失败（不阻断写作）：{e}")
        except Exception:
            pass
        return None


def _write_loop_parallel(proj, n_workers=5):
    """v0.6 B：单本书内 N 个 worker 分章齐写（工作窃取）。
    先熔炼『总架构』，各 worker 领 1..N 章，写完一个立即取下一未完成章；
    真 LLM 并发由 providers.ConcurrencyGuard 兜底防 429。"""
    n_workers = max(1, int(n_workers or 5))
    llm.book(os.path.basename(proj.path), "writing")   # v2.9.0 修复：并行模式下主线程（蓝图/规划）token 也要归集到书
    ensure_blueprint(proj)                       # 写任何一章前先读总架构
    _ensure_layered_plan(proj)
    plan = _ensure_writable_plan(proj)
    if not plan:
        return
    plan["status"] = "in_progress"; proj.save_plan(plan)
    # v2.5.4：检查标记 completed 但实际无内容/内容过短的章节，重置为 pending
    _reset = []
    for c in plan["chapters"]:
        if c.get("status") == "completed":
            fp = _chapter_file_path(proj, c)
            has_content = False
            if fp and os.path.exists(fp):
                try:
                    txt = open(fp, encoding="utf-8").read()
                    wc = len(txt.replace(" ", "").replace("\n", ""))
                    has_content = wc >= 200
                except Exception:
                    has_content = False
            if not has_content:
                c["status"] = "pending"
                _reset.append(c.get("chapterNumber"))
    if _reset:
        proj.save_plan(plan)
        proj.log(f"🔧 检测到 {_reset} 章标记完成但无实际内容，已重置为待写")
    pend = [c.get("chapterNumber") for c in plan["chapters"] if c.get("status") != "completed"]
    if not pend:
        proj.log("ℹ️ 所有章节均已完成且有内容，无需并行创作")
        plan["status"] = "completed"; proj.save_plan(plan)
        return
    proj.log(f"📋 待写章节 {len(pend)} 章：{pend[:10]}{'…' if len(pend) > 10 else ''}")
    q = queue.Queue()
    for n in pend:
        q.put(n)
    min_words = plan.get("minWordsPerChapter", 3000)
    done_log = []

    def _worker(wid):
        # v2.9.0 修复：worker 是新线程，threading.local 不继承——不打标记的话
        # 并行模式的写作 token 全部漏记（usage.json 少计 + 每章 token 无法归集）
        llm.book(os.path.basename(proj.path), "writing")
        while True:
            if is_paused(proj):
                break
            _msg = budget_blocked(proj)
            if _msg:
                _pause_for_budget(proj, _msg)
                break
            try:
                no = q.get_nowait()
            except queue.Empty:
                break
            p = proj.plan()
            ch = next((c for c in p["chapters"] if c.get("chapterNumber") == no), None)
            if not ch:
                q.task_done(); continue
            try:
                proj.log(f"🧵 Agent#{wid} 领第{no}章（总架构已注入，写完立即取下一章）")
                _maybe_replan(proj, no)
                _write_chapter(proj, ch, min_words, 3)   # 内部 LLM 走 ConcurrencyGuard
            except llm.BudgetExceeded as e:
                _pause_for_budget(proj, str(e))
                break
            except Exception as e:
                proj.log(f"⚠️ Agent#{wid} 第{no}章异常：{e}")
            q.task_done()
    threads = [threading.Thread(target=_worker, args=(i,), daemon=True) for i in range(1, n_workers + 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if is_paused(proj):
        p = proj.plan(); p["status"] = "paused"; proj.save_plan(p)
        proj.log("⏸ 并行写作已暂停（点「▶ 继续」恢复剩余章）")
        return
    _validate_and_finish(proj)
    proj.log(f"🧵 并行写作完成（{n_workers} 个 Agent，总架构对齐）")
    _skeleton_refresh(os.path.basename(proj.path))   # v0.9.0：并行写完 → 刷新写作骨架

# v0.6 B 起：让 _write_chapter 的『领章/完成』在并行下不互踩 —— 由 providers.ConcurrencyGuard 限真 LLM 并发；
# 章状态落盘仍走 proj.save_plan（各 worker 写不同章文件；plan 更新按章号区分，末尾 _validate 汇总）。


def _ch_review_ctx(proj, ch_no):
    """给 chapter_review 的参照上下文（文风基准节选 + 人物档案节选 + 上一章尾）。"""
    def _rf(f):
        p = os.path.join(proj.path, f)
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    style = _rf("03-文风基准.md")[:1500]
    chars = _rf("00-人物档案.md")[:1500]
    chs = proj.plan().get("chapters", [])
    prev = ""
    if ch_no > 1 and ch_no - 1 < len(chs):
        pp = chs[ch_no - 2].get("filePath", "")
        if pp and os.path.exists(os.path.join(proj.path, pp)):
            prev = open(os.path.join(proj.path, pp), encoding="utf-8").read()[-800:]
    return (f"【文风基准节选】\n{style or '（无）'}\n【人物档案节选】\n{chars or '（无）'}\n"
            f"【上一章尾】\n{prev or '（第1章，无）'}")

def score_chapter(proj, ch_no):
    """评单章 0-100，写 meta.chapter_scores，返回 total（None=无法评）。"""
    chs = proj.plan().get("chapters", [])
    ch = next((c for c in chs if c.get("chapterNumber") == ch_no), None)
    if not ch:
        return None
    fp = os.path.join(proj.path, ch.get("filePath", ""))
    if not os.path.exists(fp):
        return None
    body = open(fp, encoding="utf-8").read()
    cfg = proj.plan().get("config") or {}
    llm.book(os.path.basename(proj.path), "review")           # 评分 token 归集
    r = workflow.chapter_review(body, cfg, _ch_review_ctx(proj, ch_no))
    total = workflow.score_dimensions(r.get("scores")) if r else None
    with proj.lock:   # v0.6 B：并行写作下 meta.chapter_scores 读改写原子化
        plan = proj.plan()
        if not isinstance(plan.get("meta"), dict):
            plan["meta"] = {}
        cs = plan["meta"].setdefault("chapter_scores", {})
        pl = _pass_line()
        cs[str(ch_no)] = {"score": total, "dims": r.get("scores", {}) if r else {},
                          "weak": (r.get("weak", []) if r else []), "verdict": (r.get("verdict", "") if r else ""),
                          "evidence": (r.get("evidence") or [])[:6] if r else [],
                          "status": "unscored" if total is None else ("pass" if total >= pl else "below"),
                          "ts": int(datetime.datetime.now().timestamp())}
        proj.save_plan(plan)
    return total

def _auto_rewrite_chapter(proj, ch_no, max_rounds=None, pass_line=None):
    """低于线就局部重写该章（weak 维度当要求）→ 重评，最多 max_rounds 次。返回最终分。"""
    plan = proj.plan()
    d = _cfg_defaults()
    if max_rounds is None:
        max_rounds = int(d.get("max_ch_rounds", 2))
    if pass_line is None:
        pass_line = _pass_line()
    chs = plan.get("chapters", [])
    ch = next((c for c in chs if c.get("chapterNumber") == ch_no), None)
    if not ch:
        return None
    fp = os.path.join(proj.path, ch.get("filePath", ""))
    # 先记一个基线分（原章），重写只保留「真的更高分」，避免 LLM 评分波动把章节改坏
    base_score = score_chapter(proj, ch_no)
    orig_body = open(fp, encoding="utf-8").read()
    versions.snapshot(proj.path, ch_no, orig_body, label="自动重写前")
    # deslop 定点去味（规则层定位 + LLM 最小改法；LLM 失败自动保留原文）
    try:
        ds = workflow.deslop_chapter(orig_body, {"cfg": plan.get("config") or {}})
        if ds.get("cleaned") and ds["cleaned"] != orig_body:
            atomic_write_text(fp, ds["cleaned"])
            proj.log(f"🧹 第{ch_no}章 deslop 定点去味 {len(ds.get('findings') or [])} 处")
            orig_body = ds["cleaned"]
            base_score = score_chapter(proj, ch_no) or base_score
    except Exception:
        pass
    rounds = 0
    while rounds < max_rounds:
        sc = score_chapter(proj, ch_no)
        if sc is None or sc >= pass_line:
            break
        weak = ((proj.plan().get("meta") or {}).get("chapter_scores", {}).get(str(ch_no), {}).get("weak", []))
        body = open(fp, encoding="utf-8").read()
        if not weak:
            break
        fix = "\n".join(f"【第{ch_no}章·主攻】提升【{w}】维度" for w in weak[:3])
        try:
            ctx = _build_ctx(proj, ch)
        except Exception:
            ctx = {}
        cons = (f"人物：{ctx.get('characters_text','')[:2000]}\n文风：{ctx.get('style_text','')[:1000]}\n"
                f"大纲要点：{ctx.get('outline_text','')[:1500]}\n上一章结尾：{(ctx.get('prev_tail') or '')[-400:]}\n"
                f"世界观/故事圣经：{(ctx.get('story_bible') or '')[:1200]}\n"
                f"设定词典（本章相关）：{(ctx.get('lore_relevant') or '')[:1200]}\n"
                f"线索账本（须兑现/不得乱揭）：{(ctx.get('clue_ledger') or '')[:1200]}")
        cfg = plan.get("config") or {}
        new = workflow.guided_rewrite(body, fix, cons, cfg)
        new = _clean_chapter_text(new or "")
        if new and new.strip():
            versions.snapshot(proj.path, ch_no, body, label="重写前")
            atomic_write_text(fp, new)
            sc2 = score_chapter(proj, ch_no)
            if (sc2 is not None and sc is not None and sc2 > sc):
                proj.log(f"🔁 第{ch_no}章 {sc}→{sc2}（提分，保留），按[{','.join(weak[:3])}]局部重写（第{rounds+1}/{max_rounds}轮）")
            else:
                atomic_write_text(fp, body)   # 没提分 → 还原原章，防改坏
                sc2 = sc
                proj.log(f"↩️ 第{ch_no}章重写未提分（{sc2}），已还原原章（防 LLM 波动改坏）")
        rounds += 1
    final = score_chapter(proj, ch_no)
    # 若最终分低于最初基线，还原到原章基线（保守：只进不退）
    if (final or 0) < (base_score or 0):
        atomic_write_text(fp, orig_body)
        final = base_score
    return final

def per_chapter_scores(proj):
    """[{ch, score, weak, status, verdict}]（来自 meta.chapter_scores，按章号排序）。"""
    cs = (proj.plan().get("meta") or {}).get("chapter_scores", {}) or {}
    out = []
    for k in sorted(cs, key=lambda x: int(x) if str(x).isdigit() else 9999):
        v = cs[k]
        out.append({"ch": int(k) if str(k).isdigit() else k, "score": v.get("score"),
                    "weak": v.get("weak", []), "status": v.get("status", "unscored"),
                    "verdict": v.get("verdict", ""), "evidence": v.get("evidence") or []})
    return out

def _write_chapter(proj, ch, min_words, max_retries):
    no = ch["chapterNumber"]
    proj.log(f"✍️ 开始第{no}章「{ch['title']}」")
    paused = is_paused(proj)
    if paused:
        proj.log(f"⏸ 第{no}章未开写即暂停")
        return
    # v2.9.0：每章 token 消耗精确记账（线程标记章号 → usage.json by_chapter，
    # 并行写作下各章互不串账；含本章的重试/扩充/评分重写全部消耗）
    _bk = os.path.basename(proj.path)
    llm.set_chapter(no)
    _ch_started_at = datetime.datetime.now().isoformat(timespec="seconds")
    with proj.lock:   # 必须读最新计划再改；ch 是调用方手里的旧快照，直接 save 会覆盖并发写入
        plan0 = proj.plan() or {}
        for c in plan0.get("chapters", []):
            if c.get("chapterNumber") == no:
                c["status"] = "in_progress"
        if plan0:
            proj.save_plan(plan0)
    ctx = _build_ctx(proj, ch)
    # v4.7.0（借鉴 reveric 手法账本）：把最近几章的断章钩子/情绪/紧张度注入写作上下文，
    # 防止连续多章用同款钩子、同款情绪（模式收敛）。
    try:
        fp = os.path.join(proj.path, "21-章摘要.jsonl")
        hooks = []
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        q = d.get("quality_signals") or {}
                        hooks.append({"n": d.get("n"),
                                      "hook_type": q.get("hook_type") or "",
                                      "emotion": q.get("emotion") or "",
                                      "tension": q.get("tension")})
                    except Exception:
                        continue
        ctx["recent_hooks"] = hooks[-5:]
    except Exception:
        pass
    qn = _pop_queue_note(proj, no)
    if qn:
        ctx["queue_note"] = qn
        proj.log(f"📌 第{no}章注入读者即时指令：「{qn[:30]}」")
    body = workflow.write_chapter(ctx)
    fp = _chapter_file_path(proj, ch)
    if os.path.exists(fp):
        try:
            versions.snapshot(proj.path, no, open(fp, encoding="utf-8").read(), label="重新生成前")
        except OSError:
            pass
    words, ok, i = 0, False, 0
    for i in range(max_retries + 1):
        atomic_write_text(fp, f"# 第{no:02d}章 {ch['title']}\n\n{body}")
        ok, words = skill.check_words(fp, min_words)
        if ok:
            break
        proj.log(f"⚠️ 第{no}章仅 {words} 字，按 content-expansion.md 扩充（第{i+1}轮）")
        body = workflow.expand_chapter(body, ctx)
    # 章节元数据 → 大纲摘要 + 设定词典 + 章节摘要/线索账本（复用同一 meta，不加 LLM 调用）
    try:
        meta = workflow.chapter_meta(open(fp, encoding="utf-8").read()[:9000], ctx)
        summary = meta.get('summary', '')
        with open(os.path.join(proj.path, "01-大纲.md"), "a", encoding="utf-8") as f:
            f.write(f"\n### 第{no:02d}章 摘要\n{summary}\n")
            for t in (meta.get("new_terms") or [])[:10]:
                f.write(f"- 设定：{t.get('name','')}（已知：{t.get('known','')}，真相：{t.get('truth','')}，揭示：待定）\n")
        # 连续性：章节摘要账本（若文件存在才追加，旧项目无则跳过）
        cs = os.path.join(proj.path, "11-章节摘要.md")
        if os.path.exists(cs):
            with open(cs, "a", encoding="utf-8") as f:
                f.write(f"\n## 第{no:02d}章 {ch['title']}\n- 摘要：{summary}\n"
                        f"- 未解决连贯性问题：（本章写后自检，若有则填写，否则「无」）\n")
        cl = os.path.join(proj.path, "10-线索账本.md")
        if os.path.exists(cl):
            with open(cl, "a", encoding="utf-8") as f:
                f.write(f"\n_第{no:02d}章后新增/强化线索（登记，控制数量）：_\n")
                for t in (meta.get("new_terms") or [])[:5]:
                    f.write(f"- 线索：{t.get('name','')}（已知：{t.get('known','')}，真相：{t.get('truth','')}）→ 需计划兑现\n")
    except Exception as e:
        proj.log(f"⚠️ 第{no}章元数据提取失败：{e}")
        summary = ""
    # v3.1.0：默认章名 → 用本章摘要起真名（写完即定名，不额外读全文）
    if summary:
        _name_chapter(proj, no, summary)
    # 第1章：文风校准回填
    if no == 1:
        try:
            cal = workflow.style_calibrate(open(fp, encoding="utf-8").read(), cfg=ctx.get("cfg"))
            sp = os.path.join(proj.path, "03-文风基准.md")
            txt = open(sp, encoding="utf-8").read()
            atomic_write_text(sp, txt.rstrip() + f"\n\n## 本书校准段落（第1章定稿回填）\n{cal.strip()}\n")
        except Exception:
            pass
    with proj.lock:   # v0.6 B：并行下读改写 02-写作计划.json 原子化，避免多 worker 互踩
        plan = proj.plan()
        # v1.2.2：扩写 3 轮仍不足 min_words，但有实质内容（≥60% 目标）→ 视为完成（soft pass），
        # 不再把整章标 failed（真实 LLM 常给 2600-2900 字；标 failed 会误导用户以为没写出来）
        soft_ok = ok or words >= int(min_words * 0.6)
        for c in plan["chapters"]:
            if c["chapterNumber"] == no:
                c["status"] = "completed" if soft_ok else "failed"
                c["wordCount"] = words
                c["wordCountPass"] = ok
                c["retryCount"] = i
                # v2.9.0：章节完成卡片数据源（对话卡片展示：完成时间/字数/token/质量分）
                if soft_ok:
                    c["completedAt"] = _ch_started_at
                    try:
                        c["tokens"] = llm.book_chapter_tokens(_bk, no)
                    except Exception:
                        c["tokens"] = None
        proj.save_plan(plan)
    proj.log(f"✅ 第{no}/{len(plan['chapters'])}章完成（{words}字）"
             if (ok or words >= int(min_words * 0.6)) else f"❌ 第{no}章未达标（{words}字）")
    # v0.5 M1：逐章评分 + 低于 90 自动局部重写（可配关，失败不阻断主流程）
    if ok:
        try:
            _d = _cfg_defaults()
            if _d.get("chapter_quality", True):
                proj.log(f"📊 第{no}章逐章评分（<90 自动局部重写，最多 {_d.get('max_ch_rounds', 2)} 轮）…")
                fin = _auto_rewrite_chapter(proj, no)
                _st = ((proj.plan().get("meta") or {}).get("chapter_scores", {}).get(str(no), {}).get("status"))
                proj.log(f"📊 第{no}章 {fin if fin is not None else '?'} 分（{_st}）")
        except Exception as e:
            proj.log(f"⚠️ 第{no}章逐章评分失败（不阻断）：{e}")
    _index_chapter_continuity(proj, ch, ctx.get("cfg"))
    _maybe_backflow_titles(proj, no)   # v2.3.0 任务一：每 10 章里程碑基于已成稿回流书名候选（防御性，绝不抛）
    llm.set_chapter(None)   # v2.9.0：本章结束，之后的审校/成书调用不再计入任何章

def _validate_and_finish(proj):
    llm.book(os.path.basename(proj.path), "review")   # v0.3.x：五角色审校 token 归集
    plan = proj.plan()
    if not (plan.get("chapters") or []):
        # 兜底红线：0 章绝不完结（成书 0 字 + 标记 completed 是最难恢复的死局）
        proj.log("⛔ 没有可成书的章节，取消完结；请重新「继续」以重建规划")
        plan["status"] = "planning"
        proj.save_plan(plan)
        return
    plan["status"] = "validating"
    proj.save_plan(plan)
    min_words = plan.get("minWordsPerChapter", 3000)
    soft_floor = int(min_words * 0.6)
    for round_no in range(3):
        # v3.1.0：只重写「真失败」的章。旧逻辑把 soft-pass（完成但未达标，wcp=False）
        # 也当失败整章重写，几百章的书等效无限循环、服务一重启就永远卡在「校验中」。
        failed = [c for c in plan["chapters"]
                  if c.get("status") == "failed"
                  or (c.get("status") == "completed" and (c.get("wordCount") or 0) < soft_floor)]
        if not failed:
            break
        capped, overflow = failed[:10], failed[10:]
        proj.log(f"🔁 校验第{round_no+1}轮：{len(failed)}章真失败，本轮重写 {len(capped)} 章"
                 + (f"（其余 {len(overflow)} 章下轮继续）" if overflow else ""))
        for ch in capped:
            ch["status"] = "failed"
            _write_chapter(proj, ch, min_words, 3)
        plan = proj.plan()
    # 分层编辑审稿（可配关；report=出修订记录，autofix=有界重写 high 项）
    val = _cfg_defaults().get("validation") or {}
    if val.get("enabled", True):
        try:
            res = _run_layered(proj, val.get("mode", "report"), int(val.get("max_fix_rounds", 2)))
            proj.log(f"📝 五角色审校 + 确定性引擎：百分制 {res.get('total')}（达标 {'是' if res.get('pass') else '否'}），"
                     f"autofix {res.get('fixed', 0)} 处 → 14-质量报告.md / 13-修订记录.md")
        except Exception as e:
            proj.log(f"⚠️ 分层审稿失败（不影响成书）：{e}")

    # 成书编译（剥离规划元信息，只保留章题/引子/正文）
    parts, total = [], 0
    for ch in plan["chapters"]:
        fp = _chapter_file_path(proj, ch)
        if os.path.exists(fp):
            parts.append(_clean_chapter_text(open(fp, encoding="utf-8").read()))
            total += ch.get("wordCount") or 0
    book_path = os.path.join(proj.path, f"成书-{plan['novelName']}.md")
    atomic_write_text(book_path,
                      f"# {plan['novelName']}\n\n> 共 {len(plan['chapters'])} 章 · 约 {total} 字\n\n"
                      + "\n\n---\n\n".join(parts))
    plan["status"] = "completed"
    plan["totalWords"] = total
    proj.save_plan(plan)
    proj.log(f"📚 《{plan['novelName']}》成书完成：{total} 字，文件：成书-{plan['novelName']}.md")
    # 放行门禁：成书后自动（后台线程）带要求重写到 ≥达标线，不阻断成书；已放行则跳过
    # v0.7：一键完稿驱动时由 ⑤ 统一门控，此处不再二次触发（避免同书 6 轮冗余 LLM）
    if _cfg_defaults().get("release_gate", True) and not ((plan.get("meta") or {}).get("released")) \
            and not one_shot_driving(os.path.basename(proj.path)):
        _gkey = os.path.basename(proj.path)
        threading.Thread(target=lambda: run_release_gate(_gkey), daemon=True).start()
        proj.log("🔓 成书后自动触发放行门禁（后台定向重写到 ≥ 达标线，约几分钟）…")
    # 偏好记忆
    p = workflow.load_prefs()
    p["preferences"]["creationHistory"].append(
        {"novel": plan["novelName"], "date": datetime.date.today().isoformat(),
         "chapters": len(plan["chapters"]), "words": total, "genre": plan["config"].get("genre")})
    p["preferences"]["typicalChapterCount"] = len(plan["chapters"])
    g = p["preferences"]["favoriteGenres"]
    if plan["config"].get("genre") and plan["config"]["genre"] not in g:
        g.append(plan["config"]["genre"])
    workflow.save_prefs(p)

def _run_layered(proj, mode, max_rounds):
    """L1 确定性引擎 + 五角色审校 + 百分制；autofix 且未达标时对 high 项有界重写并重评。
    写 13-修订记录.md + 14-质量报告.md。"""
    plan = proj.plan(); cfg = plan["config"]
    _book_skills_into(cfg, plan)   # v3.0.0：审校/总编同样吃到本书绑定
    pdir = proj.path
    min_words = plan.get("minWordsPerChapter", 3000)

    def _book():
        return "\n\n".join(_clean_chapter_text(open(_chapter_file_path(proj, c), encoding="utf-8").read())
                           for c in plan["chapters"] if os.path.exists(_chapter_file_path(proj, c)))

    book_text = _book()
    l1 = continuity.run_project(pdir, min_words=min_words)
    pass_line = int((_cfg_defaults().get("validation") or {}).get("pass_line", workflow.PASS_LINE))
    review = workflow.five_role_review(book_text, cfg, l1["findings"]) or {}
    total = workflow.score_dimensions(review.get("scores"))
    fixed = 0
    if mode == "autofix" and total < pass_line:
        for act in (review.get("fix_actions") or [])[:max_rounds]:
            cno = act.get("chapter")
            if not isinstance(cno, int):
                continue
            ch = next((c for c in plan["chapters"] if c["chapterNumber"] == cno), None)
            if not ch:
                continue
            fp = _chapter_file_path(proj, ch)
            old = open(fp, encoding="utf-8").read()
            new = re.sub(r"```[a-zA-Z]*\n?", "", workflow.fix_chapter(old, act.get("action", ""), {"cfg": cfg})).strip()
            if new:
                versions.snapshot(proj.path, cno, old, label="审稿修复前")
                atomic_write_text(fp, new)
                fixed += 1
        if fixed:
            book_text = _book()
            review = workflow.five_role_review(book_text, cfg, l1["findings"]) or {}
            total = workflow.score_dimensions(review.get("scores"))
    reader_data = workflow.reader_panel(book_text, cfg)[:3] if _cfg_defaults().get("reader_panel") else []
    _write_quality_report(proj, l1, review, total, mode, fixed, pass_line, reader_data=reader_data)
    _write_revision_log(proj, review, mode, fixed, total)
    return {"scores": review.get("scores"), "total": total, "pass": total >= pass_line,
            "findings": review.get("findings"), "l1": l1["findings"], "fixed": fixed, "book": book_text}

def _write_revision_log(proj, review, mode, fixed, total):
    """写 13-修订记录.md（五角色结论 + 评分 + 待改进清单）。"""
    scores = review.get("scores") or {}
    findings = review.get("findings") or []
    lines = ["# 修订记录", f"> 五角色审校 · 模式 {mode} · autofix 已修 {fixed} 处 · 百分制 {total} · {datetime.datetime.now():%Y-%m-%d %H:%M}", ""]
    lines.append("## 五角色结论（证据→结论）")
    for fd in findings[:40]:
        lines.append(f"- 【{fd.get('role','?')}·{fd.get('severity','?')}】第{fd.get('chapter','?')}章 {fd.get('problem','')}\n  证据：{fd.get('evidence','')}")
    lines.append("\n## 评分（各维 0-10，权重见 14-质量报告）")
    lines.append("| 维度 | 分 |\n|---|---|" + "".join(f"\n| {k} | {v} |" for k, v in scores.items()))
    items = [f"{i}. 第{a.get('chapter','?')}章：{a.get('action','')}" for i, a in enumerate((review.get("fix_actions") or [])[:8], 1)]
    lines.append("\n## 待改进清单（按优先级）\n" + "\n".join(items) if items else "\n## 待改进清单（按优先级）\n（无 high 项）")
    lines.append("\n_百分制合计见 14-质量报告（权重：结构20/因果20/人物15/连续性20/语言15/读者体验10）。_")
    atomic_write_text(os.path.join(proj.path, "13-修订记录.md"), "\n".join(lines) + "\n")

def _write_quality_report(proj, l1, review, total, mode, fixed, pass_line, reader_data=None):
    """写 14-质量报告.md（L1 + 五角色 + 百分制 + 达标判定 + P5 读者人格）。"""
    w = workflow.SCORE_WEIGHTS
    scores = review.get("scores") or {}
    l1rows = "\n".join(f"| {f.get('check')} | 命中 | {f.get('severity')} | {f.get('location')} |"
                       for f in l1["findings"]) or "| — | 无命中 | — | — |"
    revrows = "\n".join(f"| {fd.get('role')} | {fd.get('problem','')} | {fd.get('evidence','')} |"
                        for fd in (review.get("findings") or [])[:12]) or "| — | — | — |"
    scorows = "\n".join(f"| {k} | {w[k]} | {scores.get(k,'—')} |" for k in w)
    ok = "是" if total >= pass_line else "否"
    body = (f"# 质量报告\n> 五角色审校 + 确定性引擎 · 模式 {mode} · autofix {fixed} 处 · {datetime.datetime.now():%Y-%m-%d %H:%M}\n\n"
            f"## 1) 确定性规则（bot/continuity.py，零成本）\n| 检查 | 命中 | severity | 位置 |\n|---|---|---|---|\n{l1rows}\n\n"
            f"## 2) 五角色 LLM 审校\n| 角色 | 结论 | 证据 |\n|---|---|---|\n{revrows}\n\n"
            f"## 3) 百分制评分与达标\n| 维度 | 权重 | 得分(0-10) |\n|---|---|---|\n{scorows}\n"
            f"| **合计（百分制）** | 100 | **{total}** |\n\n"
            f"- 达标线：**{pass_line}** · 达标：**{ok}** · 修订：{'autofix ' + str(fixed) + ' 处' if fixed else '无（report）'}\n"
            f"- 是否可出书：**{ok}**\n")
    # v0.5 M1：逐章评分表（<90 标红 ⚠️，可据此单独重写）
    pcs = per_chapter_scores(proj)
    if pcs:
        _rows = "\n".join(
            f"| 第{c['ch']}章 | **{c['score'] if c['score'] is not None else '—'}** | {'、'.join(c['weak'] or []) or '—'} "
            f"| {'⚠️ 未达标' if (c['status'] == 'below') else ('✅ 达标' if c['status']=='pass' else '未评')} |"
            + (f" {c['verdict'][:30]}" if c.get('verdict') else "") + " |"
            + ("<br>".join(f"「{e.get('quote','')[:40]}」{e.get('why','')[:30]}" for e in (c.get("evidence") or [])[:2])
               or "—") + " |"
            for c in pcs)
        body += (f"\n## 4) 逐章评分（M1 · <{pass_line} 自动/手动重写）\n"
                 f"| 章节 | 百分制 | 薄弱维度 | 达标 | 判定 | 评分证据 |\n|---|---|---|---|---|---|\n{_rows}\n")
    if reader_data:
        body += ("\n## P5 读者人格 beta 读（3 类读者试读）\n"
                 + "".join(f"- **{r.get('persona','读者')}**：追读「{str(r.get('追读点',''))[:50]}」｜弃书「{str(r.get('弃书点',''))[:50]}」｜建议「{str(r.get('建议',''))[:70]}」\n"
                           for r in reader_data[:3]))
    elif _cfg_defaults().get("reader_panel"):
        body += "\n## P5 读者人格 beta 读\n- （本次未产生数据）\n"
    atomic_write_text(os.path.join(proj.path, "14-质量报告.md"),
                      body + "\n_模板：quality-report（一幕·神笔 · ShenBi NovelCraft）。_")

def validate_project(proj, mode="report", max_rounds=2):
    """端点用：对已完成/任意项目按需跑分层审稿（report 或 autofix），返回 JSON 摘要。"""
    plan = proj.plan()
    plan["status"] = "validating"
    proj.save_plan(plan)
    proj.log(f"🔍 手动分层审稿（{mode}）…")
    res = {}
    try:
        res = _run_layered(proj, mode, max_rounds)
        proj.log(f"✅ 五角色审校 + 确定性引擎完成：百分制 {res.get('total')}（达标 {'是' if res.get('pass') else '否'}），"
                 f"autofix {res.get('fixed', 0)} 处 → 14-质量报告.md")
    except Exception as e:
        proj.log(f"⚠️ 分层审稿失败：{e}")
    finally:
        p2 = proj.plan()
        p2["status"] = "completed"
        proj.save_plan(p2)
    return res

def _write_loop(proj: Project):
    _sem = _write_semaphore()          # v0.3 P3：并发（默认 2 本同时写，超出排队）
    _sem.acquire()
    llm.book(os.path.basename(proj.path), "writing")   # v0.3.x：写稿 token 归集到该书
    try:
        plan = _ensure_writable_plan(proj)
        if not plan:
            return
        plan["status"] = "in_progress"
        proj.save_plan(plan)
        _ensure_layered_plan(proj)
        for ch in plan["chapters"]:
            if is_paused(proj):
                break
            _msg = budget_blocked(proj)
            if _msg:
                _pause_for_budget(proj, _msg)
                break
            if ch["status"] != "completed":
                _maybe_replan(proj, ch["chapterNumber"])
                _write_chapter(proj, ch, plan.get("minWordsPerChapter", 3000), 3)
                _skeleton_refresh(os.path.basename(proj.path))   # v0.9.0：每写完一章即刷新写作骨架
                if is_paused(proj):
                    break
        if is_paused(proj):
            plan = proj.plan(); plan["status"] = "paused"; proj.save_plan(plan)
            proj.log("⏸ 已暂停（停在当前进度，点「▶继续」恢复；也可「⏹ 停止」）")
            _skeleton_refresh(os.path.basename(proj.path))
            return
        _validate_and_finish(proj)
        _skeleton_refresh(os.path.basename(proj.path))   # v0.9.0：全书写完 → 刷新写作骨架
    except llm.BudgetExceeded as e:
        _pause_for_budget(proj, str(e))
    except Exception as e:
        proj.log(f"💥 写作异常中断（说「继续」可续写）：{e}")
        plan = proj.plan()
        plan["status"] = "in_progress"
        proj.save_plan(plan)
    finally:
        _sem.release()
        proj.running = False

# v0.3 P3：并发写（同一时刻最多 N 本在写，N=defaults.concurrency 默认 2，超出排队）
_WRITE_SEM, _WRITE_SEM_N = None, 0
def _write_semaphore():
    global _WRITE_SEM, _WRITE_SEM_N
    try:
        n = int(_cfg_defaults().get("concurrency", 2))
    except Exception:
        n = 2
    if _WRITE_SEM is None or _WRITE_SEM_N != n:
        _WRITE_SEM = threading.Semaphore(max(1, n)); _WRITE_SEM_N = n
    return _WRITE_SEM

# ---------- v0.3 P3：定时任务（批量写 + 调度） ----------
SCHEDULES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schedules.json")
def load_schedules():
    try:
        return json.load(open(SCHEDULES_FILE, encoding="utf-8"))
    except Exception:
        return {}
def save_schedules(d):
    atomic_write_json(SCHEDULES_FILE, d)

def start_writing_batch(proj, limit):
    """v0.3 P3：只写最多 limit 章（定时批次用），写完停在 in_progress，供下次续。默认并发受限。"""
    if getattr(proj, "running", False):
        return False
    def _bl():
        _sem = _write_semaphore(); _sem.acquire()
        llm.book(os.path.basename(proj.path), "writing")   # v0.3.x：定时批次写稿 token 归集
        try:
            plan = proj.plan(); plan["status"] = "in_progress"; proj.save_plan(plan)
            wrote = 0
            for ch in plan["chapters"]:
                if wrote >= limit:
                    break
                if ch["status"] != "completed":
                    _write_chapter(proj, ch, plan.get("minWordsPerChapter", 3000), 3); wrote += 1
            proj.log(f"⏱ 定时批次：本次写 {wrote} 章（上限 {limit}），待续")
        except Exception as e:
            proj.log(f"💥 定时批次异常：{e}")
        finally:
            _sem.release(); proj.running = False
    proj.running = True
    threading.Thread(target=_bl, daemon=True).start()
    return True

# ---------- 阶段0 中断续写检测 ----------

def discover_projects():
    out = []
    if not os.path.isdir(PROJECTS_DIR):
        return out
    for d in sorted(os.listdir(PROJECTS_DIR), reverse=True):
        pdir = os.path.join(PROJECTS_DIR, d)
        if os.path.exists(os.path.join(pdir, "02-写作计划.json")):
            try:
                out.append({"key": d, **Project(pdir, d).status()})
            except Exception:
                continue
    return out

# ---------- v0.2：作品库管理 + 审查后定向重写 ----------
import re as _re

_DIM_ADVICE = {
    "结构": "强化节奏曲线与钩子密度，每章结尾必留悬念/转折；检查三幕/章节结构是否失衡。",
    "因果可信": "补因果链：每个关键转折给出动机与代价，消除『突然』『莫名其妙』，设定与规则前后自洽。",
    "人物": "补人物弧与一致性：动机/性格/称谓前后统一，配角不工具化，主角成长可感。",
    "连续性": "修连续性：伏笔全部兑现或明确搁置、时间线/伤势/物品/身份不穿帮、人物姓名称谓不混用。",
    "语言": "去 AI 腔：少用『仿佛/瞬间/不由得』堆砌与空洞形容词，多用具体动作与感官细节，对话去书面腔。",
    "读者体验": "强化爽点/追读：数据化打脸、反转前置、情绪钩子密集，照顾读者代入与期待管理。",
}

def quality_score(pdir):
    """v2.5.0: 从 14-质量报告.md 或计划文件章节级 quality_signals 抽百分制平均分；缺失 → None。"""
    # 1) 优先读质量报告文件（审稿后生成，最准确）
    p = os.path.join(pdir, "14-质量报告.md")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if "合计（百分制）" not in line:
                continue
            cells = [c for c in line.split("|") if c.strip()]
            for c in reversed(cells):          # 最后一格才是真实合计分
                m = _re.search(r"\d+(?:\.\d+)?", c)
                if m:
                    return float(m.group())
            break
    # 2) v2.5.0: 没有报告文件时，从计划文件的章节级 quality_signals 动态计算
    try:
        med, pj, plan = _plan_of(pdir)
        if not plan:
            return None
        scores = []
        for ch in (plan.get("chapters") or []):
            if not isinstance(ch, dict):
                continue
            qs = ch.get("quality_signals")
            if isinstance(qs, dict):
                s = qs.get("score")
                if s is not None:
                    try:
                        scores.append(float(s))
                    except (ValueError, TypeError):
                        pass
        if scores:
            return round(sum(scores) / len(scores), 1)
    except Exception:
        pass
    # 3) v2.5.1: 没有任何评分数据时，基于已完成章节数估算一个临时分数
    try:
        med, pj, plan = _plan_of(pdir)
        if plan:
            chs = plan.get("chapters") or []
            done = sum(1 for c in chs if isinstance(c, dict) and c.get("status") == "completed")
            if done > 0:
                return min(85, 55 + done * 2)
    except Exception:
        pass
    return None

def _pass_line():
    return int((_cfg_defaults().get("validation") or {}).get("pass_line", workflow.PASS_LINE))

def _meta(proj):
    return (proj.plan() or {}).get("meta") or {}

# 四套引擎各写各的计划文件；文件名是项目文体唯一的判据。
_PLAN_BY_MEDIUM = (("novel", "02-写作计划.json"), ("script", "剧本计划.json"),
                   ("poetry", "诗词计划.json"), ("prose", "散文计划.json"),
                   ("thesis", "论文计划.json"))   # v4.6.0：论文
_NAME_KEY = {"novel": "novelName", "script": "scriptName", "poetry": "poemName", "prose": "proseName", "thesis": "thesisName"}
_FORM_LABEL_KEY = {"novel": "", "script": "formLabel", "poetry": "formLabel", "prose": "genreLabel", "thesis": "degreeLabel"}
_OTHER_LABEL = {"completed": "已完成", "planning": "规划中", "writing": "写作中",
                "in_progress": "创作中", "failed": "失败", "unknown": "草稿"}


def _plan_of(pdir):
    """(文体, 计划文件路径, plan)；没有任一计划文件的目录不算作品。"""
    for med, fname in _PLAN_BY_MEDIUM:
        pj = os.path.join(pdir, fname)
        if os.path.exists(pj):
            try:
                return med, pj, json.load(open(pj, encoding="utf-8"))
            except Exception:
                return med, pj, {}
    return "", "", {}


def _lifecycle(meta):
    """v2.2 作品生命周期字段（展示口径，惰性判定不写盘）。

    archived_at=归档，trashed_at=回收站；旧数据只有 meta.archived=true 时按回收站兼容，
    因为旧版「归档」实际就是前端作品库里的回收站。"""
    meta = meta or {}
    archived_at = meta.get("archived_at")
    trashed_at = meta.get("trashed_at")
    archived = bool(archived_at)
    legacy_trash = bool(meta.get("archived")) and not archived_at and not trashed_at
    return {"archived": archived, "archived_at": archived_at or None,
            "trashed_at": trashed_at or None,
            "trashed": bool(trashed_at) or legacy_trash}


def _stage_label_of(med, code):
    """阶段码 → 中文名（S5=写作 等）；缺失返回空。"""
    try:
        from . import medium as _medium_mod
        return _medium_mod.stage_names(med).get(code or "", "")
    except Exception:
        return ""


def _other_medium_item(key, pdir, med, plan):
    """非小说作品：进度/字数取该文体引擎的 status()，小说专有字段留空而非造假值。
    项目对象用这里已解析好的 pdir 构造，避免各引擎自己的 PROJECTS_DIR 与本模块漂移。"""
    st = {}
    try:
        from bot import poetry_writer, prose_writer, script_writer
        from bot import thesis_writer
        cls = {"script": script_writer.ScriptProject, "poetry": poetry_writer.PoetryProject,
               "prose": prose_writer.ProseProject, "thesis": thesis_writer.ThesisProject}[med]
        st = cls(pdir, key).status() or {}
    except Exception:
        st = {}
    meta = plan.get("meta") or {}
    state = str(st.get("state") or plan.get("status") or "unknown")
    total = int(st.get("total") or 0)
    done = int(st.get("done") or 0)
    # 各引擎的 form 不统一（剧本给码，诗词/散文给标签），展示一律取计划里的 *Label。
    form = plan.get(_FORM_LABEL_KEY[med]) or ""
    cfg = plan.get("config") or {}
    genre = ((cfg.get("genre") or "") if med == "script" else "") or form or "未分类"
    words = int(st.get("words") or 0)
    if state == "completed":
        total = max(total, 1)
        done = total   # 完成态按定稿算：诗词的轮数（如 1/3）只反映改稿轮数，不是完成度
    return {"key": key, "name": st.get("name") or plan.get(_NAME_KEY[med]) or key,
            "medium": med, "form": form, "genre": genre,
            "state": state, "status_label": _OTHER_LABEL.get(state, state),
            "done": done, "total": total,
            "progress": round(100 * done / total) if total else 0,
            "total_words": words, "running": bool(st.get("running")),
            "quality": None, "passing": None, "pass_line": _pass_line(),
            "released": None, "final_quality": None, "awaiting_title": False,
            "gate_rounds": None, "paused": bool(meta.get("paused")), "queued": 0,
            "halted": False, "stage": st.get("stage") or "",
            "stage_label": _stage_label_of(med, st.get("stage")), "verdict": st.get("verdict") or "",
            "created_at": plan.get("createdAt"), "updated_at": plan.get("updatedAt"),
            "favorite": bool(meta.get("favorite")), "tags": meta.get("tags", []),
            **_lifecycle(meta)}


def library_item(key, with_quality=True):
    """单个作品富元数据（作品库管理用）；文体由计划文件判定。"""
    try:
        pdir = _project_dir(key)
    except ValueError:
        return {}
    med, pj, plan = _plan_of(pdir)
    if not med:
        return {}
    if med != "novel":
        return _other_medium_item(key, pdir, med, plan)
    st = Project(pdir, key).status()
    genre = (plan.get("config") or {}).get("genre") or plan.get("genre") or "未分类"
    total_words = plan.get("totalWords") or st.get("words") or 0
    q = quality_score(pdir) if with_quality else None
    passing = (q is not None and q >= _pass_line()) if q is not None else None
    meta = _meta_from_plan(plan)
    state = st.get("state") or st.get("status")
    # 放行门禁 / 选名 待处理（来自 02.meta）
    released = meta.get("released")
    final_q = meta.get("final_quality")
    awaiting_title = bool(meta.get("awaiting_title"))
    label = {"completed": "已出书", "in_progress": "创作中", "validating": "校验中",
             "awaiting_title": "选书名", "failed": "失败", "idle": "草稿"}.get(state,
             "规划中" if state in ("planning", "awaiting_title") or not (st.get("total") or 0)
             else ("未完成" if st.get("done", 0) < st.get("total", 1) else "已完成"))
    return {"key": key, "name": st.get("name") or key, "medium": "novel", "form": "",
            "genre": genre, "state": state,
            "status_label": label, "done": st.get("done", 0), "total": st.get("total", 0),
            "progress": round(100 * st.get("done", 0) / st.get("total", 1)) if st.get("total") else 0,
            "total_words": total_words, "running": st.get("running", False),
            "quality": q, "passing": passing, "pass_line": _pass_line(),
            "released": released, "final_quality": final_q, "awaiting_title": awaiting_title,
            "gate_rounds": meta.get("gate_rounds"),
            "paused": bool(meta.get("paused")),
            "queued": len([q for q in (meta.get("queued_msgs") or []) if not q.get("consumed")]),
            "halted": bool(state == "in_progress" and not meta.get("paused") and total_words == 0),
            "created_at": plan.get("createdAt"), "updated_at": plan.get("updatedAt"),
            "favorite": bool(meta.get("favorite")), "tags": meta.get("tags", []),
            **_lifecycle(meta)}

def _meta_from_plan(plan):
    return plan.get("meta") or {}

def library_all(view="active", include_archived=None):
    """作品列表。view: active（默认，排除归档/回收站）/ archived / trash。
    include_archived=True 保留给旧调用方，等价于不过滤、返回全部。"""
    items = [library_item(d) for d in _project_dirs()]
    if include_archived:
        return items
    if view == "archived":
        return [i for i in items if i["archived"] and not i["trashed"]]
    if view == "trash":
        return [i for i in items if i["trashed"]]
    return [i for i in items if not i["archived"] and not i["trashed"]]

def _project_dirs():
    if not os.path.isdir(PROJECTS_DIR):
        return []
    return [d for d in sorted(os.listdir(PROJECTS_DIR), reverse=True)
            if _plan_of(os.path.join(PROJECTS_DIR, d))[0]]

def library_stats(items):
    ts = [i["total"] for i in items]
    qs = [i["quality"] for i in items if i["quality"] is not None]
    genres = {}
    for i in items:
        genres[i["genre"]] = genres.get(i["genre"], 0) + 1
    return {"count": len(items),
            "by_state": {s: sum(1 for i in items if i["state"] == s) for s in sorted({i["state"] for i in items})},
            "passing": sum(1 for i in items if i.get("passing") is True),
            "unrated": sum(1 for i in items if i.get("quality") is None),
            "avg_quality": round(sum(qs) / len(qs), 1) if qs else None,
            "total_words": sum(i["total_words"] for i in items),
            "genres": genres}

def set_meta(key, **fields):
    """收藏/标签/归档 等写入该作品自己的计划文件（文体由计划文件名判定）。"""
    try:
        pdir = _project_dir(key)
    except ValueError:
        return {}
    med, pj, plan = _plan_of(pdir)
    if not med:
        return {}
    plan["meta"] = plan.get("meta") or {}
    for k, v in fields.items():
        if k == "tags":
            plan["meta"]["tags"] = list(v or [])
        else:
            plan["meta"][k] = v
    plan["updatedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
    atomic_write_json(pj, plan)
    return library_item(key)

def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _mutate_meta(key, set_fields=None, drop_fields=()):
    """按 key 改该作品计划文件的 meta 并刷新 updatedAt；返回最新列表项。
    作品不存在/非法 key/缺计划文件 → {}（调用方据此返回 ok=False）。"""
    try:
        pdir = _project_dir(key)
    except ValueError:
        return {}
    if not os.path.isdir(pdir):
        return {}
    med, pj, plan = _plan_of(pdir)
    if not med:
        return {}
    meta = plan.get("meta") or {}
    for k in drop_fields:
        meta.pop(k, None)
    meta.update(set_fields or {})
    plan["meta"] = meta
    plan["updatedAt"] = _now_iso()
    atomic_write_json(pj, plan)
    return library_item(key)


def archive_book(key, on=True):
    """归档：从侧栏/默认列表收起，作品库「归档」页可见、可恢复；与回收站互斥。"""
    if on:
        item = _mutate_meta(key, {"archived_at": _now_iso()}, ("trashed_at", "archived"))
    else:
        item = _mutate_meta(key, drop_fields=("archived_at",))
    if not item:
        return {"ok": False, "note": "该作品不存在"}
    return {"ok": True, "item": item}


def trash_book(key):
    """移入回收站：默认全隐藏、可恢复、可彻底删除；与归档互斥。"""
    item = _mutate_meta(key, {"trashed_at": _now_iso()}, ("archived_at", "archived"))
    if not item:
        return {"ok": False, "note": "该作品不存在"}
    return {"ok": True, "item": item}


def restore_book(key):
    """从归档或回收站恢复：三个生命周期标记一并清除。"""
    item = _mutate_meta(key, drop_fields=("trashed_at", "archived_at", "archived"))
    if not item:
        return {"ok": False, "note": "该作品不存在"}
    return {"ok": True, "item": item}


def purge_book(key, confirm):
    """彻底删除：必须完整输入作品显示名，否则不动任何文件。"""
    item = library_item(key)
    if not item:
        return {"ok": False, "note": "该作品不存在"}
    if (confirm or "").strip() != item.get("name"):
        return {"ok": False, "note": "请完整输入作品名以确认彻底删除"}
    res = delete_book(key, hard=True)
    if not res.get("ok"):
        return res
    return {"ok": True, "note": "已彻底删除（不可恢复）"}


def rename_book(key, new_name):
    """v0.3.3：改书名（只改显示名 novelName + 成书文件名，key/目录不变，避免破坏引用）。"""
    new_name = (new_name or "").strip()
    if not new_name:
        return {"ok": False, "note": "书名不能为空"}
    if any(ch in new_name for ch in '/\\\x00'):
        return {"ok": False, "note": "书名不能包含路径分隔符"}
    try:
        pdir = _project_dir(key)
    except ValueError:
        return {"ok": False, "note": "作品 key 非法"}
    med, pj, plan = _plan_of(pdir)
    if not med:
        return {"ok": False, "note": "作品计划文件缺失"}
    nk = _NAME_KEY[med]
    old = plan.get(nk)
    plan[nk] = new_name
    plan["updatedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
    atomic_write_json(pj, plan)
    if old and old != new_name:
        oldf, newf = os.path.join(pdir, f"成书-{old}.md"), os.path.join(pdir, f"成书-{new_name}.md")
        if os.path.exists(oldf) and not os.path.exists(newf):
            try: os.rename(oldf, newf)
            except Exception: pass
    return {"ok": True, "key": key, "name": new_name}

def delete_book(key, hard=False):
    """v0.3.3：删除。soft（默认）=归档进回收站可恢复；hard=彻底删除（删目录+清 schedules，不可逆）。"""
    try:
        pdir = _project_dir(key)
    except ValueError:
        return {"ok": False, "note": "作品 key 非法"}
    if not os.path.isdir(pdir):
        return {"ok": False, "note": "该作品不存在"}
    if hard:
        import shutil
        shutil.rmtree(pdir, ignore_errors=True)
        try:
            sc = json.load(open(SCHEDULES_FILE, encoding="utf-8")) if os.path.exists(SCHEDULES_FILE) else {}
            if isinstance(sc, dict) and sc.pop(key, None) is not None:
                atomic_write_json(SCHEDULES_FILE, sc)
        except Exception: pass
        return {"ok": True, "hard": True, "note": "已彻底删除（不可恢复）"}
    res = trash_book(key)
    if not res.get("ok"):
        return res
    return {"ok": True, "hard": False, "archived": True,
            "note": "已移入回收站（可恢复）", "item": res.get("item")}

# ---------- v0.7 R3：逐章『保留更好』守护 + 按书任务串行（防并发写同一本书死锁）----------
import contextlib
_BOOK_LOCKS = {}          # key -> threading.RLock（同一本书的 重写/门禁 串行，防 proj.lock 竞争死锁）
_BL_LOCK = threading.Lock()

@contextlib.contextmanager
def _acq_book(key):
    """同一本书的重写/门禁/一键任务串行（可重入：门禁内部再调重写不自我死锁）；跨书互不影响。"""
    with _BL_LOCK:
        lk = _BOOK_LOCKS.setdefault(key, threading.RLock())
    lk.acquire()
    try:
        yield
    finally:
        lk.release()

import functools
def _serialize_book(fn):
    """同书任务串行装饰器：取首参 key，整段调用置于该书的 RLock（可重入，嵌套不自我死锁）。
    门禁线程 / 一键任务 / 手动重写 并发落到同一本书时排队执行，避免 proj.lock 竞争 + 章节文件并发写死锁。"""
    @functools.wraps(fn)
    def wrap(*a, **k):
        key = a[0] if a else k.get("key")
        if not key:
            return fn(*a, **k)
        with _acq_book(key):
            return fn(*a, **k)
    return wrap

# v0.7：一键完稿驱动标记（抑制 成书自动放行 与 一键⑤门禁 双重门控，避免同书 6 轮冗余 LLM）
_ONESHOT_DRIVING = set()

def one_shot_driving(key):
    return key in _ONESHOT_DRIVING

@contextlib.contextmanager
def one_shot_scope(key):
    """一键完稿全程占用该书的『驱动』标记：期间 成书 不再自动触发放行门禁（由一键⑤统一门控）。"""
    _ONESHOT_DRIVING.add(key)
    try:
        yield
    finally:
        _ONESHOT_DRIVING.discard(key)

def one_shot_begin(key):
    _ONESHOT_DRIVING.add(key)

def one_shot_end(key):
    _ONESHOT_DRIVING.discard(key)

# ---------- 审查后定向重写 ----------

def rewrite_brief(proj, cfg, pass_line=None):
    """从 L1 + 五角色 + 百分制 + 读者 提炼『重写要求书』（全局主攻方向 + 逐章待修）。并写 16-重写要求书.md。"""
    _book_skills_into(cfg, proj.plan())   # v3.0.0：本书绑定在定向重写同样生效
    pass_line = pass_line or _pass_line()
    book = workflow.list_chapters_text(proj.path) or ""
    l1 = continuity.run_project(proj.path, min_words=1)
    l1 = l1.get("findings", []) if isinstance(l1, dict) else []
    review = workflow.five_role_review(book, cfg, l1) or {}
    scores = review.get("scores") or {}
    total = workflow.score_dimensions(scores)
    weak = sorted([d for d in workflow.SCORE_WEIGHTS if d in scores],
                  key=lambda d: scores.get(d, 0))[:3]
    per_ch = {}
    for fd in review.get("findings") or []:
        if fd.get("severity") != "high":
            continue
        n = fd.get("chapter")
        if not isinstance(n, int):
            continue
        per_ch.setdefault(n, []).append(
            f"[{fd.get('role','?')}] {fd.get('problem','')}（证据：{str(fd.get('evidence',''))[:60]}）→ 建议：{fd.get('fix','')}")
    for a in review.get("fix_actions") or []:
        n = a.get("chapter")
        if isinstance(n, int):
            per_ch.setdefault(n, []).append(f"【定向修复】{a.get('action','')}")
    for f in l1:
        m = _re.search(r"第(\d+)章", str(f.get("location", "")))
        if m:
            n = int(m.group(1))
            per_ch.setdefault(n, []).append(f"[L1·{f.get('severity','?')}] {f.get('check','')}：{f.get('detail','')}")
    brief = {
        "before_total": total, "pass_line": pass_line, "pass": total >= pass_line,
        "weak_dims": [{"dim": d, "score": scores.get(d), "advice": _DIM_ADVICE.get(d, "")} for d in weak],
        "global_directions": [f"{d}（{scores.get(d)}分）：{_DIM_ADVICE.get(d, '')}" for d in weak],
        "chapters": [{"chapter": n, "requirements": sorted(set(per_ch.get(n, [])))}
                     for n in sorted(per_ch)],
    }
    # 写 16-重写要求书.md
    lines = ["# 重写要求书（16）", f"> 审查基准：百分制 **{total}** / 达标线 **{pass_line}** · {'未达标→建议重写' if not brief['pass'] else '已达标→可选优化'} · {datetime.datetime.now():%Y-%m-%d %H:%M}", ""]
    lines.append("## 全局主攻方向（最低维度）")
    lines += [f"- **{g}**" for g in brief["global_directions"]] or ["- （无明显短板）"]
    lines.append("\n## 逐章待修（severity=high + L1 硬伤）")
    if brief["chapters"]:
        for c in brief["chapters"]:
            lines.append(f"### 第{c['chapter']}章")
            lines += [f"- {r}" for r in c["requirements"]]
    else:
        lines.append("- （无定位到章的高严重度问题）")
    atomic_write_text(os.path.join(proj.path, "16-重写要求书.md"), "\n".join(lines))
    return brief

def _top_targets(brief, max_rounds):
    cs = [c["chapter"] for c in brief["chapters"]]
    return cs[:max_rounds] if cs else []

# ---------- v0.7 R2：重写工作台 = 逐章实时进度 + 可取消（解决"点了重写没反应"）----------
_RW_PROGRESS = {}   # key -> {running,started_at,total,done,cancelled,per_chapter:[{ch,status,before,after,orig_words,new_words,ok}],result}
_RW_CANCEL = set()  # 请求取消的重写 key（逐章检查点生效）

def rewrite_progress(key):
    """R2：返回该书重写 job 的实时逐章进度（纯读，供 /api/rewrite-progress 轮询）。"""
    return _RW_PROGRESS.get(key)

def cancel_rewrite(key):
    """R2：请求取消该书重写（下一检查点停，已写章保留）。"""
    _RW_CANCEL.add(key)
    _RW_PROGRESS.get(key, {}).update(cancelled=True)
    return True

def _pset(key, **kw):
    _RW_PROGRESS.setdefault(key, {"running": True, "started_at": int(datetime.datetime.now().timestamp()),
                                  "total": 0, "done": 0, "cancelled": False, "per_chapter": [], "result": None})
    _RW_PROGRESS[key].update(kw)

def _pch_before(proj, ch_no):
    try:
        cs = (proj.plan().get("meta") or {}).get("chapter_scores", {}) or {}
        return (cs.get(str(ch_no)) or {}).get("score")
    except Exception:
        return None

@_serialize_book
def run_guided_rewrite(key, chapters=None, dimensions=None, max_rounds=6, cfg_over=None):
    """执行『审查后定向重写』：把要求书逐条带进目标章重写，重评出前后对比，写 16-重写记录.md。
    v0.7 R2：逐章实时进度写 _RW_PROGRESS[key]（供 /api/rewrite-progress 轮询）+ 支持取消（_RW_CANCEL）。"""
    proj = Project(_project_dir(key), key)
    llm.book(key, "rewrite")            # v0.3.x：定向重写 token 归集
    _RW_CANCEL.discard(key)             # 新 job 清旧取消标记
    plan = proj.plan()
    _book_skills_into(cfg, plan)   # v3.0.0：本书绑定
    cfg = dict((plan.get("config") or {}))
    cfg.update(cfg_over or {})
    brief = rewrite_brief(proj, cfg)
    targets = chapters or _top_targets(brief, int(_cfg_defaults().get("max_rewrite_rounds", max_rounds)))
    chmap = {c["chapterNumber"]: c for c in plan.get("chapters", [])}
    # R2：初始化逐章进度（before 分来自 meta，免费）
    _pset(key, running=True, total=len(targets), done=0, cancelled=False, result=None,
          per_chapter=[{"ch": n, "status": "queued", "before": _pch_before(proj, n), "after": None,
                        "orig_words": None, "new_words": None, "ok": False} for n in targets])
    changed = []
    for n in targets:
        if key in _RW_CANCEL:           # R2：取消检查点
            for p in _RW_PROGRESS[key]["per_chapter"]:
                if p["status"] == "queued":
                    p["status"] = "skipped"
            _RW_PROGRESS[key].update(cancelled=True, running=False, result={"cancelled": True})
            proj.log(f"⏹ 第{key} 重写已取消（已写 {len(changed)} 章保留，余章跳过）")
            return {"cancelled": True}
        p = next((x for x in _RW_PROGRESS[key]["per_chapter"] if x["ch"] == n), None)
        if p: p["status"] = "rewriting"
        ch = chmap.get(n)
        fp = os.path.join(proj.path, ch["filePath"]) if ch and ch.get("filePath") else None
        if not fp or not os.path.exists(fp):
            if p: p["status"] = "skipped"
            continue
        orig = open(fp, encoding="utf-8").read()
        ctx = _build_ctx(proj, ch)
        cons = (f"人物：{ctx.get('characters_text','')[:2500]}\n文风：{ctx.get('style_text','')[:1200]}\n"
                f"大纲要点：{ctx.get('outline_text','')[:2000]}\n上一章结尾：{(ctx.get('prev_tail') or '')[-500:]}\n"
                f"世界观/故事圣经：{(ctx.get('story_bible') or '')[:1500]}\n"
                f"设定词典（本章相关）：{(ctx.get('lore_relevant') or '')[:1500]}\n"
                f"线索账本（须兑现/不得乱揭）：{(ctx.get('clue_ledger') or '')[:1500]}")
        reqs = [f"【全局主攻】{g}" for g in brief["global_directions"]]
        reqs += [f"【第{n}章】{r}" for r in next((c["requirements"] for c in brief["chapters"] if c["chapter"] == n), [])]
        if dimensions:
            reqs += [f"【重点维度】{d}" for d in dimensions]      # R2：只带所选维度
        new = workflow.guided_rewrite(orig, "\n".join(reqs), cons, cfg)
        new = _clean_chapter_text(new or "")
        if not new.strip():
            if p: p.update(status="done", ok=False, new_words=0)
            continue
        # ---- v0.7 R3：逐章『保留更好』守护：写新稿→逐章评分→新稿分低于该章原分则回滚原稿（绝不把书改差）----
        before_sc = _pch_before(proj, n)
        versions.snapshot(proj.path, n, orig, label="定向重写前")
        atomic_write_text(fp, new)
        new_sc = None
        try:
            new_sc = score_chapter(proj, n)          # 评新稿（读=新稿）
        except Exception:
            new_sc = None
        reverted = bool(before_sc is not None and new_sc is not None and new_sc < before_sc)
        if reverted:
            atomic_write_text(fp, orig)      # 回滚原稿
            if p: p.update(status="reverted", ok=False, orig_words=len(orig), new_words=len(new),
                           before=before_sc, after=before_sc, keep_better=True,
                           note=f"新稿 {new_sc} < 原稿 {before_sc} → 保留原稿（守护）")
            proj.log(f"🔒 第{n}章 保留更好守护：重写分 {new_sc} < 原分 {before_sc} → 已回滚原稿")
            continue
        changed.append({"chapter": n, "injected": reqs, "orig_words": len(orig), "new_words": len(new),
                        "kept_better": True, "before_score": before_sc, "after_score": new_sc})
        if p: p.update(status="done", ok=True, orig_words=len(orig), new_words=len(new),
                       before=before_sc, after=new_sc, keep_better=not reverted)
        _RW_PROGRESS[key]["done"] = len(changed)
    # 重评（保留：书级 before→after + 逐章 after）
    book = workflow.list_chapters_text(proj.path) or ""
    l1 = continuity.run_project(proj.path, min_words=1).get("findings", [])
    review2 = workflow.five_role_review(book, cfg, l1) or {}
    after = workflow.score_dimensions(review2.get("scores") or {})
    weak2 = sorted([d for d in workflow.SCORE_WEIGHTS if d in (review2.get("scores") or {})],
                   key=lambda d: (review2.get("scores") or {}).get(d, 0))[:3]
    delta = round(after - brief["before_total"], 1)
    rec = ["# 重写记录（16）",
           f"> 模式：审查后定向重写 · 改写 {len(changed)} 章 · 百分制 {brief['before_total']} → {after}（{delta:+.1f}）· "
           f"达标线 {brief['pass_line']} · 现{'已达标 ✅' if after >= brief['pass_line'] else '未达标'} · {datetime.datetime.now():%Y-%m-%d %H:%M}", ""]
    rec.append("## 各章改写（注入的要求）")
    for c in changed:
        rec.append(f"### 第{c['chapter']}章（{c['orig_words']}→{c['new_words']} 字）")
        rec += [f"- {r}" for r in c["injected"][:8]]
    rec.append("\n## 改写后仍弱维度")
    rec += [f"- **{d}**：{_DIM_ADVICE.get(d, '')}" for d in weak2] or ["- （无明显短板）"]
    atomic_write_text(os.path.join(proj.path, "16-重写记录.md"), "\n".join(rec))
    # v0.5 M1 + v0.7 R2：改写后逐章重评（刷新 meta + 回填进度 after）
    for c in changed:
        try:
            af = score_chapter(proj, c["chapter"])
            p = next((x for x in _RW_PROGRESS[key]["per_chapter"] if x["ch"] == c["chapter"]), None)
            if p: p["after"] = af
        except Exception:
            pass
    _RW_PROGRESS[key].update(running=False, result={"before": brief["before_total"], "after": after, "delta": delta,
                                                    "changed": changed, "pass": after >= brief["pass_line"],
                                                    "weak_after": weak2, "brief": brief,
                                                    "per_chapter": per_chapter_scores(proj)})
    proj.log(f"🔁 定向重写 {len(changed)} 章：{brief['before_total']} → {after}（{delta:+.1f}）")
    _skeleton_refresh(key)   # v0.9.0：定向重写完成 → 刷新写作骨架（本次重点/仍弱维度）
    return {"before": brief["before_total"], "after": after, "delta": delta,
            "changed": changed, "pass": after >= brief["pass_line"],
            "weak_after": weak2, "brief": brief,
            "per_chapter": per_chapter_scores(proj)}

# ---------- v0.2.1：放行门禁（未达标自动带要求重写直到 ≥pass_line） ----------
_GATE_CANCEL = set()   # 请求取消的门禁 key（下一轮检查点生效）

def _write_gate_report(proj, before, after, released, pass_line, rounds, gate_results, cancelled=False):
    """把放行判定追加进 14-质量报告.md。"""
    p = os.path.join(proj.path, "14-质量报告.md")
    base = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    if cancelled:
        ok = f"⏹️ 已取消（beta，质量 {after}/{pass_line}）"
        tail = "已取消·门禁未跑完，仅作 beta 阅读"
    else:
        ok = "✅ 已放行（可出书）" if released else f"⚠️ 未放行（beta·未达标，质量 {after}/{pass_line}）"
        tail = "已放行可出书" if released else "未放行·仅作 beta 阅读，未达放行线"
    sec = [f"\n## 放行判定（{datetime.datetime.now():%Y-%m-%d %H:%M}）",
           f"- 判定：**{ok}** · 达标线 **{pass_line}** · 自动重写 {len(gate_results)} 轮",
           f"- 门禁前 → 门禁后：{before} → {after}"]
    for g in gate_results:
        sec.append(f"  - 第{g['round']}轮：{g['before']} → {g['after']}{'（已达标停）' if g.get('stopped') else ''}")
    sec.append(f"- 成书状态：{tail}")
    atomic_write_text(p, base + "\n".join(sec))

@_serialize_book
def run_release_gate(key, cfg_over=None):
    """放行门禁：循环「重评→未达标则带要求书定向重写→再评」，直到 ≥pass_line 或到轮数上限。写 02.meta + 14 放行判定。"""
    proj = Project(_project_dir(key), key)
    llm.book(key, "gate")               # v0.3.x：门禁重写 token 归集
    plan = proj.plan()
    cfg = dict(plan.get("config") or {}); cfg.update(cfg_over or {})
    d = _cfg_defaults(); pl = _pass_line()
    rounds = int(d.get("max_release_rounds", 3))
    rwp = min(int(d.get("max_release_rewrite", 3)), int(d.get("max_rewrite_rounds", 6)))  # 每轮只改 top-3 章，控时长
    before = None; final = None; released = False; gate_results = []; cancelled = False
    for r in range(rounds):
        if key in _GATE_CANCEL:
            cancelled = True; break
        brief = rewrite_brief(proj, cfg)
        cur = brief["before_total"]
        if before is None:
            before = cur
        # v0.5 M1：优先重写「逐章<90 的章」，没有逐章分则回落到整书 top-3
        below = [x["ch"] for x in per_chapter_scores(proj)
                 if x.get("score") is not None and x["score"] < pl]
        if cur >= pl and not below:
            released = True; final = cur; break
        targets = (below or _top_targets(brief, rwp))[:rwp]
        if not targets:
            released = bool(cur >= pl); final = cur; break
        res = run_guided_rewrite(key, chapters=targets, cfg_over=cfg_over)
        after = res.get("after", cur)
        step = {"round": r + 1, "before": cur, "after": after}
        if res.get("pass"):
            released = True; step["stopped"] = True
        final = after
        gate_results.append(step)
        if released:
            break
    meta = plan.get("meta") or {}
    meta["released"] = bool(released and final is not None and final >= pl)
    meta["final_quality"] = final; meta["gate_rounds"] = len(gate_results); meta["gate_results"] = gate_results
    if cancelled:
        meta["gate_cancelled"] = True
    with proj.lock:   # 重读最新计划再合并 meta：门禁跑几分钟，期间收藏/暂停/指令可能已写入
        latest = proj.plan() or plan
        latest_meta = latest.get("meta") or {}
        latest_meta.update(meta)
        latest["meta"] = latest_meta
        proj.save_plan(latest)
    meta = latest_meta
    _write_gate_report(proj, before, final, meta["released"], pl, len(gate_results), gate_results, cancelled=cancelled)
    _GATE_CANCEL.discard(key)
    proj.log(f"🔓 放行门禁 {before} → {final}：{'取消' if cancelled else ('放行✅' if meta['released'] else '未放行⚠️')}（{len(gate_results)} 轮）")
    _skeleton_refresh(key)   # v0.9.0：放行门禁结束 → 刷新写作骨架
    return {"before": before, "after": final, "released": meta["released"], "cancelled": cancelled,
            "pass_line": pl, "rounds": len(gate_results), "gate_results": gate_results}

def cancel_release_gate(key):
    """请求取消某项目的放行门禁（下一轮检查点生效）。"""
    _GATE_CANCEL.add(key)
    return {"ok": True, "key": key, "msg": "已请求取消（下一检查点生效）"}

def pick_title(key, title):
    """选定书名：改名（同步 02.meta.name + 成书文件名）并开写。用于 awaiting_title 项目。"""
    try:
        proj = Project(_project_dir(key), key)
    except ValueError:
        return None
    plan = proj.plan()
    if not plan:
        return None
    meta = plan.get("meta") or {}; meta["awaiting_title"] = False; meta["selected_title"] = title
    newdir = _safe_dir_name(title)
    base = os.path.basename(proj.path)
    _parts = base.split("-", 2); prefix = "-".join(_parts[:2]) + "-" if len(_parts) >= 2 else base + "-"
    try:
        target = safe_join(PROJECTS_DIR, prefix + newdir, label="目录名")
    except ValueError:
        target = proj.path
    if target != proj.path and not os.path.exists(target):
        os.rename(proj.path, target); proj.path = target
    plan["novelName"] = title; proj.save_plan({**plan, "meta": meta, "novelName": title})
    start_writing(proj)
    return os.path.basename(proj.path)

def _safe_dir_name(title):
    return _re.sub(r'[\\/:*?"<>|\s]+', "", title or "") or "未命名"
