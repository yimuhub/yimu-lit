"""新闻稿写作引擎（v4.11.0，medium=news）。倒金字塔结构 + 事实核查 + 多版本。

N1 素材与信源 → N2 倒金字塔撰写 → N3 事实核查 → N4 多版本 → N5 定稿
"""
import datetime
import json
import os
import re

from bot.safeio import atomic_write_text, atomic_write_json, safe_join

from . import llm
from .mini_writer_base import MiniWriterProject, _safe, _read, make_get_project

F_PLAN = "新闻稿计划.json"
F_OUTLINE = "00-素材与信源.md"
F_FINAL = "04-新闻稿定稿.md"
F_CHECK = "03-事实核查.md"

TYPES = {"product": "产品/新品发布", "official": "官方公告", "event": "活动通稿",
         "corporate": "企业动态", "honor": "获奖/荣誉"}


class NewsProject(MiniWriterProject):
    PLAN_FILE = F_PLAN
    NAME_KEY = "newsName"
    FORM_KEY = "typeLabel"
    FINAL_FILE = F_FINAL
    MEDIUM = "news"


get_project = make_get_project(NewsProject)
PROJECTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects")


def create_project(cfg: dict):
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _safe(cfg.get("title") or cfg.get("idea") or "未命名新闻稿")
    pdir = safe_join(PROJECTS_DIR, f"n_{ts}-{name}", label="作品 key")
    os.makedirs(pdir, exist_ok=True)
    proj = NewsProject(pdir, name)
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
    if not isinstance(plan, dict) or not (plan.get("config") or plan.get("newsName")):
        return None
    proj = NewsProject(pdir, plan.get("newsName") or key)
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
    ntype = TYPES.get(cfg.get("ntype") or "product", "产品/新品发布")
    plan["newsName"] = cfg.get("title") or f"{cfg.get('org') or ''}{ntype}新闻稿"
    plan["typeLabel"] = ntype
    plan["config"] = {k: v for k, v in cfg.items() if k != "api_key"}
    plan["status"] = "in_progress"
    proj.save_plan(plan)

    style = {"formal": "官方正式", "tech": "科技媒体", "warm": "温度感/人文", "finance": "财经严谨"}.get(cfg.get("cite_style") or "formal", "官方正式")
    org = cfg.get("org") or ""
    facts = cfg.get("idea") or ""

    stages = [
        ("N1", "素材与信源", lambda: _stage_material(proj, pdir, cfg, plan, facts, org)),
        ("N2", "倒金字塔撰写", lambda: _stage_write(proj, pdir, cfg, plan, ntype, style, org, facts)),
        ("N3", "事实核查", lambda: _stage_check(proj, pdir, cfg, plan)),
        ("N4", "多版本", lambda: _stage_versions(proj, pdir, cfg, plan)),
        ("N5", "定稿", lambda: _stage_final(proj, pdir, cfg, plan)),
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
    proj.log("✅ 新闻稿定稿完成")


def _stage_material(proj, pdir, cfg, plan, facts, org):
    out = llm.chat_json([{"role": "user", "content":
        f"从以下素材中提取新闻 5W1H 要点。发布主体：{org}。素材：{facts[:3000]}\n"
        '输出 JSON：{"who":"","what":"","when":"","where":"","why":"","how":"",'
        '"quotes":[{"speaker":"","quote":""}],"numbers":["关键数据"],"gaps":["缺失或需确认的信息"]}。只输出 JSON。'}],
        cfg=cfg, stage="planning")
    plan["five_w1h"] = out if isinstance(out, dict) else {}
    proj.save_plan(plan)
    atomic_write_text(os.path.join(pdir, F_OUTLINE),
                      f"# 素材与信源\n\n发布主体：{org}\n类型：{TYPES.get(cfg.get('ntype'), '')}\n\n"
                      f"## 5W1H\n```json\n{json.dumps(plan.get('five_w1h', {}), ensure_ascii=False, indent=2)}\n```\n\n"
                      f"## 原始素材\n{facts[:3000]}")


def _stage_write(proj, pdir, cfg, plan, ntype, style, org, facts):
    w = plan.get("five_w1h") or {}
    body = llm.chat([{"role": "user", "content":
        f"写一篇{ntype}新闻稿。发布主体：{org}。语体：{style}。\n"
        f"5W1H：{json.dumps(w, ensure_ascii=False)}\n"
        f"原始素材：{facts[:3000]}\n\n"
        "倒金字塔结构：\n"
        "1. 标题（一句话，含关键信息）\n"
        "2. 导语（第一段，含 who/what/when/where，50-80 字）\n"
        "3. 主体（按重要性递减排列，含数据与引语）\n"
        "4. 引语（至少 1 条，格式：XX表示：「引语内容」）\n"
        "5. 背景（公司/产品简介， boilerplate）\n"
        "6. 媒体联系方式（占位）\n\n"
        "新闻语体：第三人称、客观、无形容词堆砌；数字精确；引语用直接引语。\n"
        "输出 markdown 正文（含 ## 分节）。"}],
        cfg=cfg, stage="writing", max_tokens=4000)
    atomic_write_text(os.path.join(pdir, F_FINAL), (body or "（撰写失败）").strip())
    meta = plan.setdefault("meta", {})
    meta["words"] = len(re.findall(r"[\u4e00-\u9fff]", body or ""))
    proj.save_plan(plan)


def _stage_check(proj, pdir, cfg, plan):
    final = _read(os.path.join(pdir, F_FINAL))
    w = plan.get("five_w1h") or {}
    report = llm.chat([{"role": "user", "content":
        f"对以下新闻稿做事实核查。原始 5W1H：{json.dumps(w, ensure_ascii=False)}\n"
        "逐项检查：1) 时间是否一致 2) 数字是否前后一致 3) 人名/机构名是否一致 "
        "4) 引语是否标注了说话者 5) 是否有形容词堆砌或主观判断（应客观）。\n"
        "输出 markdown：## 核查结果（逐项 ✓/⚠️）+ ## 修正建议。"}],
        cfg=cfg, stage="review")
    atomic_write_text(os.path.join(pdir, F_CHECK), (report or "（核查失败）").strip())


def _stage_versions(proj, pdir, cfg, plan):
    if cfg.get("with_versions") is False:
        return
    final = _read(os.path.join(pdir, F_FINAL))
    if not final.strip():
        return
    versions = llm.chat([{"role": "user", "content":
        "基于以下新闻稿输出三个版本：\n"
        "1. ## 社媒短版（140 字内，带话题标签 #）\n"
        "2. ## 标题候选（3 个不同角度的标题）\n"
        "3. ## 内部通报版（面向员工，语气亲近）\n\n"
        f"{final[:4000]}"}], cfg=cfg, stage="review")
    fp = os.path.join(pdir, F_FINAL.replace(".md", "-多版本.md"))
    atomic_write_text(fp, (versions or "（生成失败）").strip())


def _stage_final(proj, pdir, cfg, plan):
    final = _read(os.path.join(pdir, F_FINAL))
    check = _read(os.path.join(pdir, F_CHECK))
    versions_fp = os.path.join(pdir, F_FINAL.replace(".md", "-多版本.md"))
    versions = _read(versions_fp)
    meta = plan.setdefault("meta", {})
    meta["words"] = len(re.findall(r"[\u4e00-\u9fff]", final))
    if check.strip() and "⚠️" in check:
        # 有警告 → 附加修正建议到定稿末尾
        atomic_write_text(os.path.join(pdir, F_FINAL), final + "\n\n---\n\n" + check)
    proj.save_plan(plan)
