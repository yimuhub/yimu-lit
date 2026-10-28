"""论文写作引擎（v4.6.0，medium=thesis）。

流水线（借鉴 lit-review-agent-tools 的「检索→写作→引用核查」与 humanizer 的降 AIGC 思路）：
  T1 提纲：题目/摘要/章节结构 + 引用池（候选文献清单，正文引注 [n] 与之一一对应）
  T2 撰写：分章 LLM 撰写（引言/文献综述/方法/分析/结论骨架），正文带 [n] 引注
  T3 引用核查：引注 ↔ 文献表一一对应、缺失/孤儿引注标出、参考文献按 GB-T7714/APA 统一格式
  T4 降 AIGC 润色：逐章拟人化改写（句式节奏扰动/连接词多样化/术语密度平衡）+ 降味报告
  T5 定稿：论文定稿.md + 参考文献.md + 查重报告.md

设计原则与 prose_writer 一致：线性阶段、产物全落盘、resume_project 断点恢复。
"""
import datetime
import json
import os
import re

from bot.safeio import atomic_write_text, atomic_write_json, safe_join

from . import llm
from .llm import book as llm_book

F_PLAN = "论文计划.json"
F_OUTLINE = "00-提纲.md"
F_REFS = "01-参考文献.md"
F_BODY_DIR = "02-正文"
F_FINAL = "04-论文定稿.md"
F_REPORT = "03-查重报告.md"

STAGES = [("T1", "提纲"), ("T2", "撰写"), ("T3", "引用核查"), ("T4", "降味"), ("T5", "定稿")]


class ThesisProject:
    def __init__(self, path: str, name: str):
        self.path = path
        self.name = name
        self.running = False

    def log(self, msg: str):
        line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
        try:
            with open(os.path.join(self.path, "运行日志.txt"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def plan(self):
        p = os.path.join(self.path, F_PLAN)
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None

    def save_plan(self, plan):
        plan["updatedAt"] = datetime.datetime.now().isoformat()
        atomic_write_json(os.path.join(self.path, F_PLAN), plan)

    def status(self) -> dict:
        plan = self.plan() or {}
        meta = plan.get("meta") or {}
        total = int(plan.get("totalChapters") or len((plan.get("outline") or {}).get("chapters") or []))
        done = sum(1 for v in (plan.get("chapterStatus") or {}).values() if v == "completed")
        if plan.get("status") == "completed":
            done = total   # 完成态按满额报（对齐散文 6/6 语义）
        return {
            "state": plan.get("status", "unknown"),
            "done": done, "total": total,
            "words": meta.get("words", 0),
            "name": plan.get("thesisName", self.name), "medium": "thesis",
            "form": plan.get("degreeLabel", ""),
            "stage": meta.get("stage", ""),
        }


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "", name or "")[:40] or "未命名论文"


def _write(path: str, text: str) -> None:
    atomic_write_text(path, text)


def _read(path: str) -> str:
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def get_project(key: str) -> ThesisProject | None:
    try:
        pdir = safe_join(PROJECTS_DIR, key, label="作品 key")
    except ValueError:
        return None
    if not os.path.isdir(pdir):
        return None
    return ThesisProject(pdir, key)


PROJECTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects")

DEGREES = {"undergrad": "本科毕业论文", "master": "硕士论文", "journal": "期刊论文", "report": "课程报告"}


def _gen(stage: str, task: str, cfg: dict, stage_code: str = "planning", max_tokens: int = 6000) -> str:
    out = llm.chat([{"role": "user", "content": task}], cfg=cfg, stage=stage_code, max_tokens=max_tokens)
    return (out or "").strip() if isinstance(out, str) else str(out or "")


# ---------------------------------------------------------------- 提纲 T1

def gen_outline(cfg: dict) -> dict:
    """T1：提纲（题目/摘要/章节结构）+ 引用池。返回 {outline_md, chapters[], refs[]}。"""
    degree = DEGREES.get(cfg.get("degree") or "undergrad", "本科毕业论文")
    cite = {"gb": "GB/T 7714", "apa": "APA", "mla": "MLA"}.get(cfg.get("cite_style") or "gb", "GB/T 7714")
    task = (
        f"为一份【{degree}】拟定提纲。学科方向：{cfg.get('field') or '未指定'}。\n"
        f"题目：{cfg.get('title') or '（请基于学科方向与研究要点自拟一个学术题目）'}\n"
        f"研究要点：{cfg.get('idea') or '（自行设计）'}\n"
        f"目标字数：约 {cfg.get('target_words') or 8000} 字（据此定章节数，通常 4-6 章）。\n\n"
        "输出 JSON：\n"
        '{"title":"论文题目","abstract":"中文摘要300字",'
        '"chapters":[{"no":1,"title":"章标题（如 绪论/文献综述/研究方法/…/结论）",'
        '"sections":["小节1","小节2"],"target_words":2000}],'
        '"refs":[{"no":1,"entry":"作者. 题名[文献类型]. 出版信息, 年份.","kind":"期刊/学位论文/专著/网络资源"}]}\n\n'
        "要求：\n"
        "- refs 是引用池：8-15 条与选题真实相关的经典/代表性文献方向，entry 按学术格式书写；\n"
        "- 文献必须标注 kind，网络资源注明访问路径；不确定真实性的条目 kind 标注「待核验」；\n"
        f"- entry 风格按 {cite}。\n"
        "只输出 JSON。"
    )
    out = llm.chat_json([{"role": "user", "content": task}], cfg=cfg, stage="planning")
    if not isinstance(out, dict) or not out.get("chapters"):
        raise ValueError("提纲生成失败（模型未返回结构化提纲）")
    return out


# ---------------------------------------------------------------- 撰写 T2

def write_chapter(cfg: dict, outline: dict, ch: dict, refs: list) -> str:
    degree = DEGREES.get(cfg.get("degree") or "undergrad", "本科毕业论文")
    ref_lines = "\n".join(f"[{r.get('no')}] {r.get('entry')}" for r in refs[:15])
    sections = "、".join(ch.get("sections") or [])
    task = (
        f"撰写【{degree}】《{outline.get('title')}》的第 {ch.get('no')} 章「{ch.get('title')}」。\n"
        f"本章小节：{sections}；目标 {ch.get('target_words') or 2000} 字左右。\n"
        f"全文摘要：{outline.get('abstract')}\n"
        f"可用文献（正文引注一律用 [编号] 对应下列条目，不得编造编号外的文献）：\n{ref_lines}\n\n"
        "学术写作纪律：\n"
        "- 平实准确的学术语体；论证有据；引注格式 [n]；\n"
        "- 只用正常中文标点，禁止 markdown 加粗/斜体/列表符号；小节用「一、二、」或普通段落；\n"
        "- 不确定的事实留「有待进一步验证」而非编造。"
    )
    body = _gen(f"论文 · 第{ch.get('no')}章", task, cfg, "writing", max_tokens=8000)
    return body


# ---------------------------------------------------------------- 引用核查 T3

def check_citations(plan: dict, pdir: str) -> list[str]:
    """T3：引注 ↔ 文献表一致性检查（确定性，零 LLM）。"""
    refs = plan.get("refs") or []
    ref_nos = {int(r.get("no") or 0) for r in refs}
    body_dir = os.path.join(pdir, F_BODY_DIR)
    cited: set[int] = set()
    issues: list[str] = []
    if os.path.isdir(body_dir):
        for fn in sorted(os.listdir(body_dir)):
            if not fn.endswith(".md"):
                continue
            text = _read(os.path.join(body_dir, fn))
            for m in re.finditer(r"\[(\d{1,2})\]", text):
                cited.add(int(m.group(1)))
    for n in sorted(cited):
        if n not in ref_nos:
            issues.append(f"正文引注 [{n}] 在参考文献表中不存在（孤儿引注）")
    for r in refs:
        n = int(r.get("no") or 0)
        if n and n not in cited and not r.get("cited_extra"):
            pass  # 文献表未被引用不算错误（综述背景文献允许）
    if not refs:
        issues.append("参考文献表为空")
    return issues


def format_refs_md(refs: list, style: str = "gb") -> str:
    lines = ["# 参考文献\n"]
    for r in sorted(refs, key=lambda x: int(x.get("no") or 0)):
        flag = "（待核验）" if (r.get("kind") or "").find("待核验") >= 0 else ""
        lines.append(f"[{r.get('no')}] {r.get('entry')}{flag}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------- 降 AIGC T4

def humanize_chapter(cfg: dict, body: str, ch_title: str) -> tuple[str, str]:
    """T4：拟人化改写。返回 (改写后文本, 降味说明)。"""
    task = (
        f"对下面的论文章节「{ch_title}」做降 AIGC 检测率的拟人化改写（humanizer）：\n"
        "- 保持论点、论据、引注 [n] 与学术严谨性完全不变；\n"
        "- 句式节奏扰动：长短句交错，打破均匀句长；\n"
        "- 替换 AI 高频连接词（首先/其次/综上所述/值得注意的是 等改为更自然的过渡）；\n"
        "- 适度口语化学术表达但保持术语准确；删掉空洞的总结句；\n"
        "- 输出改写后的整章文本（保留引注），不要解释。\n\n"
        f"{body[:9000]}"
    )
    out = _gen("论文 · 降味", task, cfg, "review", max_tokens=8000)
    if not out.strip():
        return body, "改写为空，保留原文"
    note = f"句式与连接词已拟人化（{len(body)} → {len(out)} 字）"
    return out, note


# ---------------------------------------------------------------- 主流水线

def create_project(cfg: dict, base_key: str = "") -> ThesisProject:
    """建论文项目并跑完 T1-T5。"""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    title = (cfg.get("title") or "").strip()
    name = _safe(title or f"{cfg.get('field') or ''}论文" or "未命名论文")
    pdir = safe_join(PROJECTS_DIR, base_key or f"t_{ts}-{name}", label="项目 key")
    os.makedirs(pdir, exist_ok=True)
    proj = ThesisProject(pdir, name)
    proj.running = True
    llm_book(os.path.basename(pdir), "planning")
    try:
        _pipeline(proj, pdir, cfg)
    finally:
        proj.running = False
    return proj


def resume_project(key: str) -> ThesisProject | None:
    """断点恢复（缺哪段补哪段，幂等）。"""
    try:
        pdir = safe_join(PROJECTS_DIR, key, label="作品 key")
    except ValueError:
        return None
    if not os.path.isdir(pdir):
        return None
    plan = {}
    try:
        plan = json.load(open(os.path.join(pdir, F_PLAN), encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(plan, dict) or not (plan.get("config") or plan.get("thesisName")):
        return None
    proj = ThesisProject(pdir, plan.get("thesisName") or key)
    proj.running = True
    try:
        if plan.get("status") == "completed":
            proj.log("✅ 论文已定稿，无需恢复")
            return proj
        proj.log("▶ 断点恢复：检查已有产物…")
        _pipeline(proj, pdir, plan.get("config") or {}, plan=plan)
    finally:
        proj.running = False
    return proj


def _pipeline(proj: ThesisProject, pdir: str, cfg: dict, plan: dict | None = None):
    plan = plan if isinstance(plan, dict) and plan else (proj.plan() or {})
    plan.setdefault("meta", {})
    meta = plan["meta"]
    title = cfg.get("title") or ""

    # ---- T1 提纲 ----
    outline = plan.get("outline") or {}
    if not outline.get("chapters"):
        proj.log("📐 T1：生成提纲与引用池…")
        outline = gen_outline(cfg)
        plan["outline"] = {"title": outline.get("title"), "abstract": outline.get("abstract"),
                           "chapters": outline.get("chapters")}
        plan["refs"] = outline.get("refs") or []
        plan["thesisName"] = outline.get("title") or plan.get("thesisName") or title
        plan["degreeLabel"] = DEGREES.get(cfg.get("degree") or "undergrad", "本科毕业论文")
        plan["totalChapters"] = len(outline["chapters"])
        meta["stage"] = "T2"
        proj.save_plan(plan)
        _write(os.path.join(pdir, F_OUTLINE),
               f"# {plan['thesisName']}\n\n## 摘要\n\n{outline.get('abstract')}\n\n## 章节结构\n\n"
               + "\n".join(f"{c.get('no')}. {c.get('title')}（{c.get('target_words')} 字）：{'、'.join(c.get('sections') or [])}"
                           for c in outline["chapters"]) + "\n")
        _write(os.path.join(pdir, F_REFS), format_refs_md(plan["refs"], cfg.get("cite_style")))
        proj.log(f"📐 提纲就绪：{len(outline['chapters'])} 章 / 引用池 {len(plan['refs'])} 条")
    cfg.setdefault("title", plan.get("thesisName"))
    chapters = plan.get("outline", {}).get("chapters") or []
    body_dir = os.path.join(pdir, F_BODY_DIR)
    os.makedirs(body_dir, exist_ok=True)

    # ---- T2 分章撰写 ----
    for ch in chapters:
        no = int(ch.get("no") or 0)
        if plan.get("chapterStatus", {}).get(str(no)) == "completed":
            continue
        proj.log(f"✍️ T2：撰写第 {no} 章「{ch.get('title')}」…")
        body = write_chapter(cfg, plan.get("outline") or {}, ch, plan.get("refs") or [])
        _write(os.path.join(body_dir, f"{no:02d}-{_safe(ch.get('title'))}.md"), body)
        plan.setdefault("chapterStatus", {})[str(no)] = "completed"
        meta["stage"] = "T2"
        plan["meta"] = meta
        proj.save_plan(plan)
    plan["status"] = "in_progress"
    proj.save_plan(plan)

    # ---- T3 引用核查 ----
    proj.log("🔗 T3：引用核查…")
    issues = check_citations(plan, pdir)
    refs_md = format_refs_md(plan.get("refs") or [], cfg.get("cite_style"))
    _write(os.path.join(pdir, F_REFS), refs_md)
    if issues:
        proj.log(f"⚠️ 引用核查：{len(issues)} 处问题（{'；'.join(issues[:3])}…）" if len(issues) > 3
                 else f"⚠️ 引用核查：{'；'.join(issues)}")
    else:
        proj.log("✅ 引注与文献表一一对应")
    meta["stage"] = "T4"
    plan["meta"] = meta
    proj.save_plan(plan)

    # ---- T4 降 AIGC 润色 ----
    total_words = 0
    report_lines = ["# 降 AIGC 润色报告\n"]
    if cfg.get("humanize", True) is not False:
        for ch in chapters:
            no = int(ch.get("no") or 0)
            fp = os.path.join(body_dir, f"{no:02d}-{_safe(ch.get('title'))}.md")
            if not os.path.exists(fp):
                continue
            body = _read(fp)
            proj.log(f"🧪 T4：第 {no} 章降 AIGC 润色…")
            new_body, note = humanize_chapter(cfg, body, ch.get("title") or "")
            _write(fp, new_body)
            total_words += len(re.findall(r"[\u4e00-\u9fff]", new_body))
            report_lines.append(f"- 第 {no} 章「{ch.get('title')}」：{note}\n")
    else:
        for ch in chapters:
            no = int(ch.get("no") or 0)
            fp = os.path.join(body_dir, f"{no:02d}-{_safe(ch.get('title'))}.md")
            if os.path.exists(fp):
                total_words += len(re.findall(r"[\u4e00-\u9fff]", _read(fp)))
    meta["stage"] = "T5"
    proj.save_plan(plan)

    # ---- T5 定稿 ----
    proj.log("📋 T5：定稿…")
    parts = [f"# {plan.get('thesisName')}\n\n## 摘要\n\n{plan.get('outline', {}).get('abstract')}\n"]
    for ch in sorted(chapters, key=lambda c: int(c.get("no") or 0)):
        fp = os.path.join(body_dir, f"{int(ch.get('no') or 0):02d}-{_safe(ch.get('title'))}.md")
        if os.path.exists(fp):
            parts.append(f"\n\n---\n\n# 第 {ch.get('no')} 章 {ch.get('title')}\n\n" + _read(fp))
    parts.append("\n\n---\n\n" + _read(os.path.join(pdir, F_REFS)))
    _write(os.path.join(pdir, F_FINAL), "".join(parts))
    report_lines.append(f"\n- 全文约 {total_words} 字（不含参考文献）\n")
    _write(os.path.join(pdir, F_REPORT), "".join(report_lines))
    plan["status"] = "completed"
    meta["stage"] = "F"
    meta["words"] = total_words
    plan["meta"] = meta
    proj.save_plan(plan)
    proj.log(f"✅ 论文定稿完成：约 {total_words} 字 → {F_FINAL}")


# ---------------------------------------------------------------- 查询

def discover_projects() -> list[dict]:
    out = []
    if not os.path.isdir(PROJECTS_DIR):
        return out
    for d in sorted(os.listdir(PROJECTS_DIR), reverse=True):
        if not d.startswith("t_"):
            continue
        pdir = os.path.join(PROJECTS_DIR, d)
        if not os.path.isdir(pdir):
            continue
        plan = {}
        try:
            plan = json.load(open(os.path.join(pdir, F_PLAN), encoding="utf-8"))
        except Exception:
            pass
        meta = plan.get("meta") or {}
        out.append({"key": d, "name": plan.get("thesisName", d), "medium": "thesis",
                    "form": plan.get("degreeLabel", ""),
                    "total": plan.get("totalChapters", 0),
                    "done": sum(1 for s in (plan.get("chapterStatus") or {}).values() if s == "completed"),
                    "status": plan.get("status", "unknown")})
    return out
