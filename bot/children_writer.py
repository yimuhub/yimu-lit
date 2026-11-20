"""儿童文学写作引擎（v4.10.0，medium=children）。借鉴 MM-StoryAgent 多阶段流程。

C1 选题分级 → C2 故事（语言分级）→ C3 互动设计 → C4 插图提示词 → C5 定稿
"""
import datetime
import json
import os

from .paths import DATA_ROOT
import re

from bot.safeio import atomic_write_text, safe_join

from . import llm
from . import purity
from .mini_writer_base import MiniWriterProject, _safe, _read, make_get_project, run_pipeline

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
PROJECTS_DIR = os.path.join(DATA_ROOT, "projects")


def create_project(cfg: dict):
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _safe(cfg.get("title") or cfg.get("idea") or "未命名故事")
    # v4.12.1：支持 _spawn_simple_writer 预创建的目录（异步模式）
    if cfg.get("_base_key"):
        pdir = safe_join(PROJECTS_DIR, cfg["_base_key"], label="作品 key")
    else:
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
        llm.book(key, "writing")   # v4.24.16：resume 在新线程跑，不标记则 token 记账失活
        _pipeline(proj, pdir, plan.get("config") or {}, plan=plan)
    finally:
        proj.running = False
    return proj


def _pipeline(proj, pdir: str, cfg: dict, plan: dict | None = None):
    plan = plan if isinstance(plan, dict) and plan else (proj.plan() or {})
    plan.setdefault("meta", {})
    age = cfg.get("age") or "7-9"
    ctype = TYPES.get(cfg.get("ctype") or "fable", "寓言故事")
    plan["bookName"] = cfg.get("title") or cfg.get("idea") or "未命名故事"
    plan["typeLabel"] = f"{ctype} · {age} 岁"
    plan["config"] = {k: v for k, v in cfg.items() if k != "api_key"}
    plan["medium"] = ""   # v4.12.6：技能/右栏按文体匹配
    plan["status"] = "in_progress"
    proj.save_plan(plan)

    stages = [
        ("C1", "选题分级", lambda: _stage_outline(proj, pdir, cfg, plan, age, ctype)),
        ("C2", "故事创作", lambda: _stage_write(proj, pdir, cfg, plan, age, ctype)),
        ("C3", "互动设计", lambda: _stage_interactive(proj, pdir, cfg, plan, age), True),
        ("C4", "插图提示词", lambda: _stage_illustration(proj, pdir, cfg, plan), True),
        ("C5", "定稿", lambda: _stage_final(proj, pdir, cfg, plan)),
    ]
    run_pipeline(proj, cfg, stages, plan=plan, done_msg="✅ 儿童故事定稿完成")


def _stage_outline(proj, pdir, cfg, plan, age, ctype):
    guide = AGE_GUIDES.get(age, AGE_GUIDES["7-9"])
    try:
        out = llm.chat_json([{"role": "user", "content":
            f"为 {age} 岁儿童设计一个{ctype}的故事提纲。主题：{cfg.get('idea') or cfg.get('title') or '自行设计'}。\n"
            f"语言分级要求：{guide}\n"
            "价值观红线：" + (cfg.get("values_note") or "正面引导，无说教，无暴力。") + "\n"
            '输出 JSON：{"title":"故事标题","sections":["第1个故事单元标题",...],"outline":"每个单元 2-3 句概要"}\n'
            "3-6 岁出 2-3 个单元（每个 300-500 字）；7-9 岁出 3-5 个单元（每个 500-800 字）；10-12 岁出 4-6 个单元。只输出 JSON。"}],
            cfg=cfg, stage="planning")
    except Exception as e:
        proj.log(f"⚠️ C1 提纲生成失败，改用单单元兜底：{e}")
        out = {}
    if not isinstance(out, dict) or not out.get("sections"):
        # 429/网络/JSON 异常时不能让“提纲为空”一路带病走到定稿。
        # 用用户输入直接起一个单单元故事，保住核心交付。
        out = {"title": plan.get("bookName") or cfg.get("title") or "未命名故事",
               "sections": ["完整故事"],
               "outline": str(cfg.get("idea") or cfg.get("title") or "一个温暖的小故事")}
    plan["outline"] = out
    title = str(out.get("title") or "").strip()
    if title:
        plan["bookName"] = title
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
    from bot import wordguard as _wg
    _tw = int(cfg.get("target_words") or 0)
    _per = max(300, _tw // max(1, len(sections))) if _tw else 800   # 每单元目标
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
            + (f"**本单元字数下限：不少于 {_wg.target_of(_per)} 字**（交稿前自查）。\n" if _tw else "")
            + f"前文摘要：{' '.join(t[-300:] for t in all_text[-2:])}\n"
            "只输出故事正文，不要标题行、不要解释。"}],
            cfg=cfg, stage="writing", max_tokens=4000)
        body = (body or "").strip()
        # v4.16.3：单元字数守护——不足时续写拼接（增量保证）
        if _tw and _wg.cn_len(body) < _per:
            _cont = _wg.clean_continuation(llm.chat([{"role": "user", "content":
                _wg.continue_prompt(body[-500:], max(300, _per - _wg.cn_len(body) + 150))}],
                cfg=cfg, stage="writing", max_tokens=2500) or "")
            if _wg.cn_len(_cont) >= 80:
                body = body.rstrip() + "\n\n" + _cont
        atomic_write_text(fp, f"## {sec}\n\n{body}\n")
        all_text.append(body)
    meta = plan.setdefault("meta", {})
    meta["words"] = sum(len(re.findall(r"[\u4e00-\u9fff]", t)) for t in all_text)
    plan["meta"] = meta
    proj.save_plan(plan)
    # C2 是核心交付：一个没有正文的「儿童故事」绝不能继续走到互动/定稿。
    # 注意：儿童故事多为短句分行的韵文，不能用「连续 40 个汉字」判断；
    # 实测 2.4KB 的完整故事因换行/标点被判为空，整条管线被误停。
    if not any(len(re.findall(r"[\u4e00-\u9fff]", t or "")) >= 40 for t in all_text):
        raise ValueError("故事正文为空或不足 40 字，停止后续阶段")


def _stage_interactive(proj, pdir, cfg, plan, age):
    if cfg.get("with_interactive") is False:
        return
    outline = plan.get("outline") or {}
    sections = outline.get("sections") or []
    if not sections:
        return
    # v5.2：把故事正文（不是只有单元标题）交给互动设计。旧版只给标题，
    # 模型看不到故事，会反问“请提供故事列表”，并把这段反问写进定稿。
    body_dir = os.path.join(pdir, F_BODY_DIR)
    story_parts = []
    for i, sec in enumerate(sections, 1):
        fp = os.path.join(body_dir, f"{i:02d}-{_safe(sec)}.md")
        if os.path.isfile(fp):
            story_parts.append(_read(fp))
    story_text = "\n\n".join(story_parts)[:6000]
    tasks = []
    for sec in sections:
        tasks.append(f"故事单元「{sec}」：出 2 个亲子讨论问题 + 1 个知识点（{age} 岁能懂）。")
    out = llm.chat([{"role": "user", "content":
        "为以下儿童故事各出亲子互动内容。\n" + "\n".join(tasks) +
        f"\n\n故事正文：\n{story_text}"
        "\n输出 markdown，按单元分节；只输出互动内容，不要反问、不要要求用户补充材料。"}],
        cfg=cfg, stage="review")
    atomic_write_text(os.path.join(pdir, F_INTERACTIVE.split(".md")[0] + ".md"),
                      f"# 亲子互动与知识\n\n{purity.strip_placeholders(out or '').strip() or '（本阶段未生成）'}")


def _stage_illustration(proj, pdir, cfg, plan):
    if cfg.get("with_illustration") is False:
        return
    outline = plan.get("outline") or {}
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
    found = 0
    # v4.23.2：_stage_write 的落盘名就是「序号-单元名.md」，这里直接用同一套规则拼，
    # 不再先算一个永远不存在的 `{单元名}.md` 再内层遍历 1..N 猜序号（原来每条都白读一次路径）。
    for i, sec in enumerate(sections, 1):
        fp = os.path.join(body_dir, f"{i:02d}-{_safe(sec)}.md")
        if os.path.exists(fp):
            body = _read(fp).strip()
            if body:
                parts.append("\n\n---\n\n" + body)
                found += 1
    if not found:
        raise ValueError("没有故事正文，不能定稿")
    # v5.2：定稿只装故事。互动/插图是附产，单独成文件；旧版把互动区整段拼进
    # 故事定稿，一次 C3 的“请提供故事列表”反问就能污染整本交付稿。
    text = "\n".join(parts)
    text, _gaps = purity.extract_placeholders(text)
    atomic_write_text(os.path.join(pdir, F_FINAL), text + "\n")
    meta = plan.setdefault("meta", {})
    meta["words"] = len(re.findall(r"[\u4e00-\u9fff]", text))
    proj.save_plan(plan)
