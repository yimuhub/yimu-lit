"""其他文字创作引擎（v4.13.0，medium=other）。

模板化低门槛扩展——每种模板定义自己的阶段提示词，共用同一套引擎骨架。
模板：剧本杀 / 播客脚本 / 课程稿 / 演讲稿 / 拆书稿 / 影评书评
"""
import datetime
import json
import os
import re

from bot.safeio import atomic_write_text, atomic_write_json, safe_join

from . import llm
from .mini_writer_base import MiniWriterProject, _safe, _read, make_get_project

F_PLAN = "其他创作计划.json"
F_OUTLINE = "00-大纲.md"
F_FINAL = "04-定稿.md"
F_REPORT = "03-打磨报告.md"

TEMPLATES = {
    "script_kill": {
        "label": "剧本杀", "stages": 3,
        "outline_prompt": "设计一个剧本杀剧本大纲。输出 JSON：{\"sections\":[\"第一幕：...\",\"第二幕：...\",\"第三幕：...\"],\"outline\":\"每幕概要\"}",
        "write_prompt": "写剧本杀的第 {i} 幕「{sec}」。格式：场景描述 + 角色动作 + 对话。包含线索埋设与玩家互动点。目标 {words} 字。",
    },
    "podcast": {
        "label": "播客脚本", "stages": 2,
        "outline_prompt": "设计一期播客节目的分段大纲。输出 JSON：{\"sections\":[\"开场\",\"主题段1\",...,\"结尾\"],\"outline\":\"各段概要\"}",
        "write_prompt": "写播客的第 {i} 段「{sec}」。口语化、有节奏感、加 [停顿] 和 [音效] 标注。目标 {words} 字。",
    },
    "course": {
        "label": "课程稿", "stages": 4,
        "outline_prompt": "设计一门知识付费课程的章节大纲。输出 JSON：{\"sections\":[\"第1课：...\",...],\"outline\":\"每课概要\"}",
        "write_prompt": "写课程第 {i} 课「{sec}」。结构：引入 → 核心知识点 → 案例 → 总结 → 作业。目标 {words} 字。",
    },
    "speech": {
        "label": "演讲稿", "stages": 2,
        "outline_prompt": "设计一篇演讲稿的结构大纲。输出 JSON：{\"sections\":[\"开场\",\"主体1\",\"主体2\",\"结尾\"],\"outline\":\"各段概要\"}",
        "write_prompt": "写演讲稿的第 {i} 段「{sec}」。有感染力，用故事开篇，金句收尾。目标 {words} 字。",
    },
    "book_review": {
        "label": "拆书稿", "stages": 3,
        "outline_prompt": "设计一篇拆书稿的章节大纲。输出 JSON：{\"sections\":[\"开篇引入\",\"核心观点1\",...],\"outline\":\"各章概要\"}",
        "write_prompt": "写拆书稿的第 {i} 节「{sec}」。结构：书中的观点 → 案例/故事 → 你的解读 → 读者行动建议。目标 {words} 字。",
    },
    "review": {
        "label": "影评/书评", "stages": 2,
        "outline_prompt": "设计一篇影评/书评的结构大纲。输出 JSON：{\"sections\":[\"引入\",\"分析\",\"评价\"],\"outline\":\"各段概要\"}",
        "write_prompt": "写影评/书评的第 {i} 段「{sec}」。有观点、有论据、不剧透关键情节。目标 {words} 字。",
    },
}


class OtherProject(MiniWriterProject):
    PLAN_FILE = F_PLAN
    NAME_KEY = "otherName"
    FORM_KEY = "templateLabel"
    FINAL_FILE = F_FINAL
    MEDIUM = "other"


get_project = make_get_project(OtherProject)
PROJECTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects")


def create_project(cfg: dict):
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _safe(cfg.get("title") or cfg.get("idea") or "未命名")
    pdir = safe_join(PROJECTS_DIR, cfg.get("_base_key") or f"o_{ts}-{name}", label="作品 key")
    os.makedirs(pdir, exist_ok=True)
    proj = OtherProject(pdir, name)
    proj.running = True
    llm.book(os.path.basename(pdir), "planning")
    try:
        _pipeline(proj, pdir, cfg)
    except Exception as e:
        proj.log(f"💥 {e}")
        plan = proj.plan() or {}
        plan["status"] = "failed"
        proj.save_plan(plan)
    finally:
        proj.running = False
    return proj


def resume_project(key: str):
    try:
        pdir = safe_join(PROJECTS_DIR, key, label="作品 key")
    except ValueError:
        return None
    if not os.path.isdir(pdir):
        return None
    plan = None
    try:
        plan = json.load(open(os.path.join(pdir, F_PLAN), encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(plan, dict) or not (plan.get("config") or plan.get("otherName")):
        return None
    proj = OtherProject(pdir, plan.get("otherName") or key)
    proj.running = True
    try:
        if plan.get("status") == "completed":
            proj.log("✅ 已定稿")
            return proj
        proj.log("▶ 断点恢复…")
        _pipeline(proj, pdir, plan.get("config") or {}, plan=plan)
    finally:
        proj.running = False
    return proj


def _pipeline(proj, pdir: str, cfg: dict, plan: dict | None = None):
    plan = plan if isinstance(plan, dict) and plan else (proj.plan() or {})
    plan.setdefault("meta", {})
    meta = plan["meta"]
    tpl_key = cfg.get("template") or "script_kill"
    tpl = TEMPLATES.get(tpl_key, TEMPLATES["script_kill"])
    tone = {"casual": "轻松口语", "formal": "正式严谨", "humorous": "幽默风趣", "inspiring": "激励人心"}.get(cfg.get("tone") or "casual", "轻松口语")
    audience = cfg.get("audience") or "通用受众"
    plan["otherName"] = cfg.get("title") or cfg.get("idea") or "未命名"
    plan["templateLabel"] = f"{tpl['label']} · {audience}"
    plan["config"] = {k: v for k, v in cfg.items() if k != "api_key"}
    plan["medium"] = "other"
    plan["status"] = "in_progress"
    plan.setdefault("chapterStatus", {})
    proj.save_plan(plan)

    # O1 大纲
    if not plan.get("outline"):
        proj.log("📐 O1：生成大纲…")
        out = llm.chat_json([{"role": "user", "content":
            f"{tpl['outline_prompt']}\n主题：{cfg.get('title') or ''}\n要点：{cfg.get('idea') or ''}\n"
            f"受众：{audience}\n语气：{tone}\n目标总字数：约 {cfg.get('target_words') or 5000} 字。\n只输出 JSON。"}],
            cfg=cfg, stage="planning")
        if not isinstance(out, dict) or not out.get("sections"):
            raise ValueError("大纲生成失败")
        plan["outline"] = out
        plan["totalChapters"] = len(out["sections"])
        plan["chapterStatus"]["O1"] = "completed"
        plan["meta"]["stage"] = "O2"
        proj.save_plan(plan)
        atomic_write_text(os.path.join(pdir, F_OUTLINE),
                          f"# {out.get('title', plan['otherName'])}\n\n{out.get('outline', '')}\n\n"
                          + "\n".join(f"- {s}" for s in out.get("sections", [])))
        proj.log(f"📐 大纲就绪：{len(out['sections'])} 节")

    # O2 撰写
    outline = plan.get("outline") or {}
    sections = outline.get("sections") or []
    words_per = int(cfg.get("target_words") or 5000) // max(1, len(sections))
    body_dir = os.path.join(pdir, "02-正文")
    os.makedirs(body_dir, exist_ok=True)
    all_text = []
    for i, sec in enumerate(sections, 1):
        fp = os.path.join(body_dir, f"{i:02d}-{_safe(sec)}.md")
        if os.path.exists(fp):
            all_text.append(_read(fp))
            continue
        proj.log(f"✍️ 写第 {i}/{len(sections)} 节：{sec}…")
        prompt = tpl["write_prompt"].format(i=i, sec=sec, words=words_per)
        prev = all_text[-1][-300:] if all_text else ""
        body = llm.chat([{"role": "user", "content":
            f"{prompt}\n语气：{tone}\n受众：{audience}\n前文摘要：{prev}\n只输出正文。"}],
            cfg=cfg, stage="writing", max_tokens=4000)
        body = (body or "").strip()
        atomic_write_text(fp, f"## {sec}\n\n{body}\n")
        all_text.append(body)
    meta["stage"] = "O3"
    plan["chapterStatus"]["O2"] = "completed"
    meta["words"] = sum(len(re.findall(r"[\u4e00-\u9fff]", t)) for t in all_text)
    plan["meta"] = meta
    proj.save_plan(plan)

    # O3 打磨
    proj.log("📝 O3：打磨…")
    report = llm.chat([{"role": "user", "content":
        "对以下内容做降AI腔+语感打磨。输出格式：先写【修改说明】要点（不超过 10 条），"
        "然后单独一行输出 ---（三个减号分隔线），分隔线之后输出修改后的完整正文，"
        "正文里不要出现任何说明性文字。\n"
        + "\n\n".join(all_text)[:8000]}], cfg=cfg, stage="review", max_tokens=6000)
    report = (report or "").strip()
    atomic_write_text(os.path.join(pdir, F_REPORT), report or "（打磨失败）")
    plan["chapterStatus"]["O3"] = "completed"
    meta["stage"] = "O4"
    plan["meta"] = meta
    proj.save_plan(plan)

    # O4 定稿
    proj.log("📋 O4：定稿…")
    # 定稿只收分隔线之后的正文，说明性文字留在打磨报告里
    polished = report.split("\n---\n", 1)[-1].strip() if "---" in report else ""
    if len(polished) < len("\n\n".join(all_text)) * 0.4:
        polished = "\n\n".join(all_text)
    atomic_write_text(os.path.join(pdir, F_FINAL),
                      f"# {plan.get('otherName')}\n\n{polished.strip()}")
    plan["chapterStatus"]["O4"] = "completed"
    plan["status"] = "completed"
    proj.save_plan(plan)
    proj.log(f"✅ 定稿完成：约 {meta.get('words', 0)} 字 → {F_FINAL}")
