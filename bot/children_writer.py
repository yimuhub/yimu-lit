"""儿童文学写作引擎（v4.10.0，medium=children）。借鉴 MM-StoryAgent 多阶段流程。

C1 选题分级 → C2 故事（语言分级）→ C3 互动设计 → C4 插图提示词 → C5 定稿
"""
import datetime
import json
import os
import re

from bot.safeio import atomic_write_text, atomic_write_json, safe_join

from . import llm
from .mini_writer_base import MiniWriterProject, _safe, _read, make_get_project

F_PLAN = "儿童文学计划.json"
F_OUTLINE = "00-分级设定.md"
F_BODY_DIR = "02-故事"
F_FINAL = "04-故事定稿.md"
F_INTERACTIVE = "03-互动与插图.md"

AGE_GUIDES = {
    "3-6": "学龄前：句长 ≤10 字；词汇限于常用 500 字；每段 ≤2 句；多用拟声词与重复句式；无文字暴力。",
    "7-9": "小学低年级：句长 ≤15 字；可有小冲突但必须有温暖化解；主角同龄人；对话驱动。",
    "10-12": "小学高年级：可处理复杂情节与轻微悬疑；尊重儿童逻辑；说教改为故事内自然体现。",
}
TYPES = {"fable": "寓言故事", "picture": "绘本故事", "scifi": "儿童科幻",
         "science": "科学故事", "bedtime": "睡前故事"}


class ChildrenProject(MiniWriterProject):
    PLAN_FILE = F_PLAN
    NAME_KEY = "bookName"
    FORM_KEY = "typeLabel"
    BODY_DIR = F_BODY_DIR
    FINAL_FILE = F_FINAL
    MEDIUM = "children"


get_project = make_get_project(ChildrenProject)
PROJECTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects")


def create_project(cfg: dict):
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _safe(cfg.get("title") or cfg.get("idea") or "未命名故事")
    pdir = safe_join(PROJECTS_DIR, f"c_{ts}-{name}", label="作品 key")
    os.makedirs(pdir, exist_ok=True)
    proj = ChildrenProject(pdir, name)
    proj.running = True
    llm.book(os.path.basename(pdir), "planning")
    try:
        _pipeline(proj, pdir, cfg)
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
    if not isinstance(plan, dict) or not (plan.get("config") or plan.get("bookName")):
        return None
    proj = ChildrenProject(pdir, plan.get("bookName") or key)
    proj.running = True
    try:
        if plan.get("status") == "completed":
            proj.log("✅ 已定稿，无需恢复")
            return proj
        proj.log("▶ 断点恢复：检查已有产物…")
        _pipeline(proj, pdir, plan.get("config") or {}, plan=plan)
    finally:
        proj.running = False
    return proj


def _pipeline(proj, pdir: str, cfg: dict, plan: dict | None = None):
    plan = plan if isinstance(plan, dict) and plan else (proj.plan() or {})
    plan.setdefault("meta", {})
    meta = plan["meta"]
    age = cfg.get("age") or "7-9"
    ctype = TYPES.get(cfg.get("ctype") or "fable", "寓言故事")
    plan["bookName"] = cfg.get("title") or cfg.get("idea") or "未命名故事"
    plan["typeLabel"] = f"{ctype} · {age} 岁"
    plan["config"] = {k: v for k, v in cfg.items() if k != "api_key"}
    plan["status"] = "in_progress"
    proj.save_plan(plan)

    stages = [
        ("C1", "选题分级", lambda: _stage_outline(proj, pdir, cfg, plan, age, ctype)),
        ("C2", "故事创作", lambda: _stage_write(proj, pdir, cfg, plan, age, ctype)),
        ("C3", "互动设计", lambda: _stage_interactive(proj, pdir, cfg, plan, age)),
        ("C4", "插图提示词", lambda: _stage_illustration(proj, pdir, cfg, plan)),
        ("C5", "定稿", lambda: _stage_final(proj, pdir, cfg, plan)),
    ]
    for code, label, fn in stages:
        if (plan.get("chapterStatus") or {}).get(code) == "completed":
            continue
        proj.log(f"📋 {code} {label}…")
        meta["stage"] = code
        proj.save_plan(plan)
        try:
            fn()
            plan.setdefault("chapterStatus", {})[code] = "completed"
        except Exception as e:
            proj.log(f"⚠️ {code} 失败：{e}")
            plan.setdefault("chapterStatus", {})[code] = "failed"
        meta["stage"] = code
        plan["meta"] = meta
        proj.save_plan(plan)
    plan["status"] = "completed"
    meta["stage"] = "F"
    proj.save_plan(plan)
    proj.log("✅ 儿童故事定稿完成")


def _stage_outline(proj, pdir, cfg, plan, age, ctype):
    guide = AGE_GUIDES.get(age, AGE_GUIDES["7-9"])
    out = llm.chat_json([{"role": "user", "content":
        f"为 {age} 岁儿童设计一个{ctype}的故事提纲。主题：{cfg.get('idea') or cfg.get('title') or '自行设计'}。\n"
        f"语言分级要求：{guide}\n"
        "价值观红线：" + (cfg.get("values_note") or "正面引导，无说教，无暴力。") + "\n"
        '输出 JSON：{"title":"故事标题","sections":["第1个故事单元标题",...],"outline":"每个单元 2-3 句概要"}\n'
        "3-6 岁出 2-3 个单元（每个 300-500 字）；7-9 岁出 3-5 个单元（每个 500-800 字）；10-12 岁出 4-6 个单元。只输出 JSON。"}],
        cfg=cfg, stage="planning")
    if not isinstance(out, dict) or not out.get("sections"):
        raise ValueError("提纲生成失败")
    plan["outline"] = out
    plan["bookName"] = out.get("title") or plan["bookName"]
    plan["totalChapters"] = len(out["sections"])
    plan["meta"]["stage"] = "C2"
    proj.save_plan(plan)
    atomic_write_text(os.path.join(pdir, F_OUTLINE),
                      f"# {out.get('title')}\n\n{out.get('outline')}\n\n"
                      + "\n".join(f"- {s}" for s in out.get("sections") or []))


def _stage_write(proj, pdir, cfg, plan, age, ctype):
    guide = AGE_GUIDES.get(age, AGE_GUIDES["7-9"])
    outline = plan.get("outline") or {}
    sections = outline.get("sections") or []
    outline_text = outline.get("outline") or ""
    body_dir = os.path.join(pdir, F_BODY_DIR)
    os.makedirs(body_dir, exist_ok=True)
    all_text = []
    for i, sec in enumerate(sections, 1):
        fp = os.path.join(body_dir, f"{i:02d}-{_safe(sec)}.md")
        if os.path.exists(fp):
            all_text.append(_read(fp))
            continue
        proj.log(f"✍️ 写故事单元 {i}/{len(sections)}：{sec}…")
        body = llm.chat([{"role": "user", "content":
            f"为 {age} 岁儿童写{ctype}的第 {i} 个故事单元「{sec}」。\n"
            f"全书提纲：{outline_text[:1500]}\n"
            f"语言分级：{guide}\n"
            f"价值观红线：{cfg.get('values_note') or '正面引导，无说教。'}\n"
            f"前文摘要：{' '.join(t[-300:] for t in all_text[-2:])}\n"
            "只输出故事正文，不要标题行、不要解释。"}],
            cfg=cfg, stage="writing", max_tokens=4000)
        body = (body or "").strip()
        atomic_write_text(fp, f"## {sec}\n\n{body}\n")
        all_text.append(body)
    meta = plan.setdefault("meta", {})
    meta["words"] = sum(len(re.findall(r"[\u4e00-\u9fff]", t)) for t in all_text)
    plan["meta"] = meta
    proj.save_plan(plan)


def _stage_interactive(proj, pdir, cfg, plan, age):
    if cfg.get("with_interactive") is False:
        return
    outline = plan.get("outline") or {}
    sections = outline.get("sections") or []
    tasks = []
    for sec in sections:
        tasks.append(f"故事单元「{sec}」：出 2 个亲子讨论问题 + 1 个知识点（{age} 岁能懂）。")
    out = llm.chat([{"role": "user", "content":
        "为以下儿童故事各出亲子互动内容。\n" + "\n".join(tasks) +
        "\n输出 markdown，按单元分节。"}], cfg=cfg, stage="review")
    atomic_write_text(os.path.join(pdir, F_INTERACTIVE.split(".md")[0] + ".md"),
                      f"# 亲子互动与知识\n\n{out or '（生成失败）'}")


def _stage_illustration(proj, pdir, cfg, plan):
    if cfg.get("with_illustration") is False:
        return
    outline = plan.get("outline") or {}
    sections = outline.get("sections") or []
    out = llm.chat([{"role": "user", "content":
        f"为以下儿童故事的每个单元出 2-4 张插画提示词（中文描述画面/构图/色调/角色表情，"
        "可直接转 AI 绘图）。故事提纲：" + (outline.get("outline") or "")[:1500] +
        "\n输出 markdown，按单元分节，每张提示词一行。"}],
        cfg=cfg, stage="review")
    fp = os.path.join(pdir, F_INTERACTIVE.split(".md")[0] + "-插图.md")
    atomic_write_text(fp, f"# 插图提示词\n\n{out or '（生成失败）'}")


def _stage_final(proj, pdir, cfg, plan):
    outline = plan.get("outline") or {}
    sections = outline.get("sections") or []
    body_dir = os.path.join(pdir, F_BODY_DIR)
    parts = [f"# {plan.get('bookName')}\n"]
    for sec in sections:
        fp = os.path.join(body_dir, f"{_safe(sec)}.md")
        for attempt in [(f"{i:02d}-{_safe(sec)}.md") for i in range(1, len(sections) + 1)]:
            alt = os.path.join(body_dir, attempt)
            if os.path.exists(alt):
                parts.append("\n\n---\n\n" + _read(alt))
                break
    interactive = _read(os.path.join(pdir, F_INTERACTIVE.split(".md")[0] + ".md"))
    if interactive.strip():
        parts.append("\n\n---\n\n" + interactive)
    atomic_write_text(os.path.join(pdir, F_FINAL), "\n".join(parts))
    meta = plan.setdefault("meta", {})
    meta["words"] = sum(len(re.findall(r"[\u4e00-\u9fff]", p)) for p in parts)
    proj.save_plan(plan)
