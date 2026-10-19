"""串行写作引擎：阶段3 疯狂创作 + 阶段4 自动校验（后台线程，事件推送）"""
import json, os, re, datetime, threading
from . import workflow, skill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_DIR = os.path.join(ROOT, "projects")

class Project:
    def __init__(self, path, name):
        self.path = path
        self.name = name
        self.events = []
        self.running = False
        self.lock = threading.Lock()

    def log(self, msg):
        with self.lock:
            self.events.append(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}")
            if len(self.events) > 200:
                self.events.pop(0)

    def plan(self):
        p = os.path.join(self.path, "02-写作计划.json")
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None

    def save_plan(self, plan):
        plan["updatedAt"] = datetime.datetime.now().isoformat()
        with open(os.path.join(self.path, "02-写作计划.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

    def status(self):
        plan = self.plan()
        if not plan:
            return {"state": "missing", "done": 0, "total": 0, "words": 0}
        chs = plan["chapters"]
        done = sum(1 for c in chs if c["status"] == "completed")
        words = sum(c.get("wordCount") or 0 for c in chs)
        return {"state": plan.get("status", "unknown"), "done": done, "total": len(chs),
                "words": words, "running": self.running,
                "name": plan.get("novelName", self.name)}

# ---------- 阶段2 落盘 ----------

def _parse_outline_titles(outline_md, total):
    """从大纲表格解析每章标题，失败则兜底"""
    titles = []
    for m in re.finditer(r"^\|\s*第(\d+)章\s*\|\s*([^|]+)\|", outline_md, re.M):
        n, t = int(m.group(1)), re.sub(r"[*`]", "", m.group(2)).strip()
        titles.append((n, t or f"第{n}章"))
    titles.sort(key=lambda x: x[0])
    if len(titles) < total:
        for n in range(1, total + 1):
            if not any(t[0] == n for t in titles):
                titles.append((n, f"第{n}章"))
    out = [t for n, t in sorted(titles)[:total]]
    return out or [f"第{n}章" for n in range(1, total + 1)]

def _clean_chapter_text(text):
    """成书时剥离规划元信息（本章概要/章节备注），保留 章题+章首引子+正文"""
    m_sum = re.search(r"\n## 本章概要", text)
    m_intro = re.search(r"\n## 章首引子", text)
    m_notes = re.search(r"\n## 章节备注", text)
    end = m_notes.start() if m_notes else len(text)
    if m_intro and m_sum and m_sum.start() < m_intro.start():
        return (text[:m_sum.start()] + text[m_intro.start():end]).strip()
    if m_intro:
        return (text[:m_intro.start() + 1] + text[m_intro.start():end]).strip()
    if m_notes:
        return text[:m_notes.start()].strip()
    return text.strip()

def _cfg_defaults():
    try:
        return json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8")).get("defaults", {}) or {}
    except Exception:
        return {}

def _write_cfg_text(pdir, fname, body):
    with open(os.path.join(pdir, fname), "w", encoding="utf-8") as f:
        f.write(body)

def create_project(cfg, titles):
    """生成项目目录 + 00人物/01大纲/03文风 + 02-写作计划.json（按选定标题命名）
    v3.0：另生成 09故事圣经/10线索账本/11章节摘要/12时间线（可配 v3_pipeline 关）。"""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = re.sub(r'[\\/:*?"<>|\s]+', "", titles[0]["title"]) or "未命名"
    pdir = os.path.join(PROJECTS_DIR, f"{ts}-{name}")
    os.makedirs(pdir, exist_ok=True)
    proj = Project(pdir, name)

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
    outline = workflow.gen_outline(cfg2)
    open(os.path.join(pdir, "01-大纲.md"), "w", encoding="utf-8").write(
        f"# {name} 大纲\n\n" + outline)

    proj.log("📐 阶段2：生成文风基准…")
    style = workflow.gen_style(cfg)
    open(os.path.join(pdir, "03-文风基准.md"), "w", encoding="utf-8").write(style)

    # ---------- v3.0 连续性结构（加法，单个失败不阻断规划） ----------
    if _cfg_defaults().get("v3_pipeline", True):
        proj.log("📐 阶段2：生成 v3.0 连续性结构（故事圣经/线索账本/时间线）…")
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
                        f"# 章节摘要\n\n> 每章写后更新：发生了什么 / 选择与代价 / 线索状态 / 时间线 / 未解决连贯性问题。\n")

    # 章节名以大纲表格为准
    chapter_titles = _parse_outline_titles(outline, total)
    plan = {
        "version": 1, "novelName": name, "projectPath": pdir,
        "totalChapters": total, "minWordsPerChapter": 3000,
        "createdAt": datetime.datetime.now().isoformat(),
        "status": "planning", "writingMode": "serial", "config": cfg,
        "titles": titles,
        "chapters": [{"chapterNumber": i + 1, "title": t,
                      "filePath": f"第{i+1:02d}章-{re.sub(r'[\\/:*?\"<>]', '', t)}.md",
                      "status": "pending", "wordCount": None,
                      "wordCountPass": None, "retryCount": 0}
                     for i, t in enumerate(chapter_titles)]}
    proj.save_plan(plan)
    proj.log(f"📐 阶段2 完成：《{name}》{total}章规划就绪")
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

def start_writing(proj: Project):
    if proj.running:
        return
    threading.Thread(target=_write_loop, args=(proj,), daemon=True).start()
    proj.running = True

def _chapter_file_path(proj, ch):
    return os.path.join(proj.path, ch["filePath"])

def _build_ctx(proj, ch):
    plan = proj.plan()
    cfg = plan["config"]
    def _rf(fname):
        fp = os.path.join(proj.path, fname)
        return open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
    outline_text = _rf("01-大纲.md") or open(os.path.join(proj.path, "01-大纲.md"), encoding="utf-8").read()
    characters_text = _rf("00-人物档案.md")
    style_text = _rf("03-文风基准.md")
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
           # v3.0 连续性结构（旧项目缺失则为空串，_continuity_block 自动省略）
           "story_bible": _rf("09-故事圣经.md"), "clue_ledger": _rf("10-线索账本.md"),
           "chapter_summaries": _rf("11-章节摘要.md"), "timeline": _rf("12-时间线与连续性.md")}
    return ctx

def _write_chapter(proj, ch, min_words, max_retries):
    no = ch["chapterNumber"]
    proj.log(f"✍️ 开始第{no}章「{ch['title']}」")
    ch["status"] = "in_progress"
    proj.save_plan(proj.plan())
    ctx = _build_ctx(proj, ch)
    body = workflow.write_chapter(ctx)
    fp = _chapter_file_path(proj, ch)
    words, ok, i = 0, False, 0
    for i in range(max_retries + 1):
        with open(fp, "w", encoding="utf-8") as f:
            f.write(f"# 第{no:02d}章 {ch['title']}\n\n")
            f.write(body)
        ok, words = skill.check_words(fp, min_words)
        if ok:
            break
        proj.log(f"⚠️ 第{no}章仅 {words} 字，按 content-expansion.md 扩充（第{i+1}轮）")
        body = workflow.expand_chapter(body, ctx)
    # 章节元数据 → 大纲摘要 + 设定词典 + v3.0 章节摘要/线索账本（复用同一 meta，不加 LLM 调用）
    try:
        meta = workflow.chapter_meta(open(fp, encoding="utf-8").read()[:9000], ctx)
        summary = meta.get('summary', '')
        with open(os.path.join(proj.path, "01-大纲.md"), "a", encoding="utf-8") as f:
            f.write(f"\n### 第{no:02d}章 摘要\n{summary}\n")
            for t in (meta.get("new_terms") or [])[:10]:
                f.write(f"- 设定：{t.get('name','')}（已知：{t.get('known','')}，真相：{t.get('truth','')}，揭示：待定）\n")
        # v3.0：章节摘要账本（若文件存在才追加，旧项目无则跳过）
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
    # 第1章：文风校准回填
    if no == 1:
        try:
            cal = workflow.style_calibrate(open(fp, encoding="utf-8").read(), cfg=ctx.get("cfg"))
            sp = os.path.join(proj.path, "03-文风基准.md")
            txt = open(sp, encoding="utf-8").read()
            open(sp, "w", encoding="utf-8").write(
                txt.rstrip() + f"\n\n## 本书校准段落（第1章定稿回填）\n{cal.strip()}\n")
        except Exception:
            pass
    plan = proj.plan()
    for c in plan["chapters"]:
        if c["chapterNumber"] == no:
            c["status"] = "completed" if ok else "failed"
            c["wordCount"] = words
            c["wordCountPass"] = ok
            c["retryCount"] = i
    proj.save_plan(plan)
    proj.log(f"✅ 第{no}/{len(plan['chapters'])}章完成（{words}字）"
             if ok else f"❌ 第{no}章未达标（{words}字）")

def _validate_and_finish(proj):
    plan = proj.plan()
    plan["status"] = "validating"
    proj.save_plan(plan)
    min_words = plan.get("minWordsPerChapter", 3000)
    for round_no in range(3):
        failed = [c for c in plan["chapters"] if not c.get("wordCountPass")]
        if not failed:
            break
        proj.log(f"🔁 校验第{round_no+1}轮：{len(failed)}章未达标，重写中")
        for ch in failed:
            ch["status"] = "failed"
            _write_chapter(proj, ch, min_words, 3)
        plan = proj.plan()
    # v3.0 分层编辑审稿（可配关；report=出修订记录，autofix=有界重写 high 项）
    val = _cfg_defaults().get("validation") or {}
    if val.get("enabled", True):
        try:
            res = _run_layered(proj, val.get("mode", "report"), int(val.get("max_fix_rounds", 2)))
            sc = res.get("scores") or {}
            proj.log(f"📝 v3.0 分层审稿：{len(res.get('findings') or [])} 处发现，"
                     f"autofix {res.get('fixed', 0)} 处 → 13-修订记录.md")
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
    with open(book_path, "w", encoding="utf-8") as f:
        f.write(f"# {plan['novelName']}\n\n> 共 {len(plan['chapters'])} 章 · 约 {total} 字\n\n")
        f.write("\n\n---\n\n".join(parts))
    plan["status"] = "completed"
    plan["totalWords"] = total
    proj.save_plan(plan)
    proj.log(f"📚 《{plan['novelName']}》成书完成：{total} 字，文件：成书-{plan['novelName']}.md")
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
    """跑 v3.0 分层审稿 → 写 13-修订记录.md；mode=autofix 时对 high 项做有界重写并重建全书文本。"""
    cfg = proj.plan()["config"]
    book_text = "\n\n".join(_clean_chapter_text(open(_chapter_file_path(proj, c), encoding="utf-8").read())
                            for c in proj.plan()["chapters"] if os.path.exists(_chapter_file_path(proj, c)))
    review = workflow.layered_review(book_text, cfg) or {}
    fixed = 0
    if mode == "autofix":
        for act in (review.get("fix_actions") or [])[:max_rounds]:
            cno = act.get("chapter")
            if not isinstance(cno, int):
                continue
            ch = next((c for c in proj.plan()["chapters"] if c["chapterNumber"] == cno), None)
            if not ch:
                continue
            fp = _chapter_file_path(proj, ch)
            old = open(fp, encoding="utf-8").read()
            new = workflow.fix_chapter(old, act.get("action", ""), {"cfg": cfg})
            new = re.sub(r"```[a-zA-Z]*\n?", "", new).strip()
            if new:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(new)
                fixed += 1
        if fixed:
            book_text = "\n\n".join(_clean_chapter_text(open(_chapter_file_path(proj, c), encoding="utf-8").read())
                                    for c in proj.plan()["chapters"] if os.path.exists(_chapter_file_path(proj, c)))
    _write_revision_log(proj, review, mode, fixed)
    return {"scores": review.get("scores"), "findings": review.get("findings"),
            "fix_actions": review.get("fix_actions"), "fixed": fixed, "book": book_text}

def _write_revision_log(proj, review, mode, fixed):
    """按 revision-log 模板写 13-修订记录.md（含六轮结论、评分、待改进清单）。"""
    scores = review.get("scores") or {}
    findings = review.get("findings") or []
    lines = ["# 修订记录", f"> 模式：{mode} · autofix 已修 {fixed} 处 · 时间：{datetime.datetime.now():%Y-%m-%d %H:%M}", ""]
    lines.append("## 六轮审稿结论（证据→结论）")
    for fd in findings[:40]:
        lines.append(f"- 【{fd.get('dim','?')}·{fd.get('severity','?')}】第{fd.get('chapter','?')}章 "
                     f"{fd.get('problem','')}\n  证据：{fd.get('evidence','')}")
    lines.append("\n## 评分（0-10）")
    lines.append("| 维度 | 分 |\n|---|---|" + "".join(f"\n| {k} | {v} |" for k, v in scores.items()))
    items = [f"{i}. 第{a.get('chapter','?')}章：{a.get('action','')}"
             for i, a in enumerate((review.get("fix_actions") or [])[:8], 1)]
    lines.append("\n## 待改进清单（按优先级）\n" + "\n".join(items) if items else "\n## 待改进清单（按优先级）\n（无 high 项）")
    lines.append("\n_修订规则：v3.0 分层编辑（结构/因果/人物/连续性/语言/读者体验）。_")
    with open(os.path.join(proj.path, "13-修订记录.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def validate_project(proj, mode="report", max_rounds=2):
    """端点用：对已完成/任意项目按需跑分层审稿（report 或 autofix），返回 JSON 摘要。"""
    plan = proj.plan()
    plan["status"] = "validating"
    proj.save_plan(plan)
    proj.log(f"🔍 手动分层审稿（{mode}）…")
    res = {}
    try:
        res = _run_layered(proj, mode, max_rounds)
        proj.log(f"✅ 分层审稿完成：{len(res.get('findings') or [])} 处发现，autofix {res.get('fixed', 0)} 处")
    except Exception as e:
        proj.log(f"⚠️ 分层审稿失败：{e}")
    finally:
        p2 = proj.plan()
        p2["status"] = "completed"
        proj.save_plan(p2)
    return res

def _write_loop(proj: Project):
    try:
        plan = proj.plan()
        plan["status"] = "in_progress"
        proj.save_plan(plan)
        for ch in plan["chapters"]:
            if ch["status"] != "completed":
                _write_chapter(proj, ch, plan.get("minWordsPerChapter", 3000), 3)
        _validate_and_finish(proj)
    except Exception as e:
        proj.log(f"💥 写作异常中断（说「继续」可续写）：{e}")
        plan = proj.plan()
        plan["status"] = "in_progress"
        proj.save_plan(plan)
    finally:
        proj.running = False

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
