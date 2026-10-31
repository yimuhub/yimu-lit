"""文案创作引擎（v4.12.0，medium=copy）。钩子库 + 正文 + 平台适配。

K1 需求 → K2 钩子库 → K3 正文 → K4 标题标签 → K5 平台适配
"""
import datetime
import json
import os
import re

from bot.safeio import atomic_write_text, atomic_write_json, safe_join

from . import llm
from .mini_writer_base import MiniWriterProject, _safe, _read, make_get_project

F_PLAN = "文案计划.json"
F_OUTLINE = "00-需求与钩子.md"
F_FINAL = "04-文案定稿.md"
F_TITLE_TAGS = "03-标题与标签.md"

KTYPES = {"voice": "口播稿", "planting": "种草笔记", "promo": "宣传文案", "marketing": "营销长文"}
PLATFORMS = {"douyin": "抖音", "xhs": "小红书", "gzh": "公众号", "bili": "B站", "shipinhao": "视频号"}
TONES = {"energetic": "活力种草", "professional": "专业可信", "humorous": "幽默玩梗", "warm": "温暖治愈"}


class CopyProject(MiniWriterProject):
    PLAN_FILE = F_PLAN
    NAME_KEY = "copyName"
    FORM_KEY = "platformLabel"
    FINAL_FILE = F_FINAL
    MEDIUM = "copy"


get_project = make_get_project(CopyProject)
PROJECTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects")


def create_project(cfg: dict):
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _safe(cfg.get("title") or cfg.get("idea") or "未命名文案")
    # v4.12.1：支持 _spawn_simple_writer 预创建的目录（异步模式）
    if cfg.get("_base_key"):
        pdir = safe_join(PROJECTS_DIR, cfg["_base_key"], label="作品 key")
    else:
        pdir = safe_join(PROJECTS_DIR, f"k_{ts}-{name}", label="作品 key")
    os.makedirs(pdir, exist_ok=True)
    proj = CopyProject(pdir, name)
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
    if not isinstance(plan, dict) or not (plan.get("config") or plan.get("copyName")):
        return None
    proj = CopyProject(pdir, plan.get("copyName") or key)
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
    ktype = KTYPES.get(cfg.get("ktype") or "voice", "口播稿")
    platform = PLATFORMS.get(cfg.get("platform") or "douyin", "抖音")
    tone = TONES.get(cfg.get("tone") or "energetic", "活力种草")
    plan["copyName"] = cfg.get("title") or cfg.get("idea") or "未命名文案"
    plan["platformLabel"] = f"{ktype} · {platform}"
    plan["config"] = {k: v for k, v in cfg.items() if k != "api_key"}
    plan["medium"] = ""   # v4.12.6：技能/右栏按文体匹配
    plan["status"] = "in_progress"
    proj.save_plan(plan)

    stages = [
        ("K1", "需求", lambda: _stage_brief(proj, pdir, cfg, plan, ktype, platform, tone)),
        ("K2", "钩子库", lambda: _stage_hooks(proj, pdir, cfg, plan)),
        ("K3", "正文", lambda: _stage_body(proj, pdir, cfg, plan, ktype, platform, tone)),
        ("K4", "标题标签", lambda: _stage_titles(proj, pdir, cfg, plan, platform)),
        ("K5", "适配", lambda: _stage_adapt(proj, pdir, cfg, plan, platform)),
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
    proj.log("✅ 文案定稿完成")


def _stage_brief(proj, pdir, cfg, plan, ktype, platform, tone):
    atomic_write_text(os.path.join(pdir, F_OUTLINE),
                      f"# 需求单\n\n类型：{ktype}\n平台：{platform}\n语气：{tone}\n"
                      f"主题/产品：{cfg.get('title') or ''}\n"
                      f"卖点与受众：{cfg.get('idea') or ''}\n"
                      + (f"口播时长：{cfg.get('duration_secs') or 60} 秒\n" if ktype == "voice" else ""))


def _stage_hooks(proj, pdir, cfg, plan):
    if cfg.get("with_hooks") is False:
        return
    out = llm.chat([{"role": "user", "content":
        f"为以下产品/主题出 10 个前 3 秒钩子候选（口播/短视频开场第一句）。\n"
        f"主题：{cfg.get('title')}\n卖点：{cfg.get('idea')}\n"
        "钩子类型覆盖：悬念、反差、痛点、数字、热点借势、提问、直给利益。每个一行。\n"
        "输出 markdown 列表。"}], cfg=cfg, stage="review")
    atomic_write_text(os.path.join(pdir, F_OUTLINE.replace(F_OUTLINE.split("-")[0] + "-", "") if False else "01-钩子库.md"),
                      f"# 钩子库（前 3 秒开场）\n\n{out or '（生成失败）'}")


def _stage_body(proj, pdir, cfg, plan, ktype, platform, tone):
    hooks_fp = os.path.join(pdir, "01-钩子库.md")
    hooks = _read(hooks_fp)[:1500] if os.path.exists(hooks_fp) else ""
    duration = cfg.get("duration_secs") or 60
    prompt = f"写一篇{ktype}。平台：{platform}。语气：{tone}。\n"
    if ktype == "voice":
        prompt += (f"口播时长约 {duration} 秒（约 {duration * 4} 字）。\n"
                   "格式：用 [停顿] [重音] [语速加快] 等标注节奏；分段对应画面切换。"
                   "开头 3 秒必须有钩子；结尾有 CTA（关注/点击/购买）。")
    elif ktype == "planting":
        prompt += "小红书风格：emoji 适量、分段短、有「亲测」「真实感受」的口吻、结尾带标签。"
    elif ktype == "marketing":
        prompt += "营销长文：AIDA 或 PAS 框架；有数据/案例支撑；段落清晰。"
    else:
        prompt += "宣传文案：正式但有温度；突出核心卖点与差异化。"
    prompt += f"\n主题：{cfg.get('title')}\n卖点与受众：{cfg.get('idea')}\n"
    if hooks:
        prompt += f"\n钩子参考（选一个最好的用，不要全用）：\n{hooks}\n"
    # v4.16.3：字数守护——口播按时长（约 4 字/秒）给下限；其他类型默认 400 字
    from bot import wordguard as _wg
    _dur = int(cfg.get("duration_secs") or 0)
    _min_words = max(300, _dur * 4) if _dur else 400
    prompt += f"\n**硬性字数下限：不少于 {_wg.target_of(_min_words)} 字**（交稿前自查）。"
    body = llm.chat([{"role": "user", "content": prompt}], cfg=cfg, stage="writing", max_tokens=3000)
    if _wg.cn_len(body) < _min_words:
        _cont = _wg.clean_continuation(llm.chat([{"role": "user", "content":
            _wg.continue_prompt((body or "")[-500:], max(250, _min_words - _wg.cn_len(body) + 120))}],
            cfg=cfg, stage="writing", max_tokens=2000) or "")
        if _wg.cn_len(_cont) >= 60:
            body = (body or "").rstrip() + "\n\n" + _cont
    atomic_write_text(os.path.join(pdir, F_FINAL), (body or "（撰写失败）").strip())
    meta = plan.setdefault("meta", {})
    meta["words"] = len(re.findall(r"[\u4e00-\u9fff]", body or ""))
    proj.save_plan(plan)


def _stage_titles(proj, pdir, cfg, plan, platform):
    final = _read(os.path.join(pdir, F_FINAL))
    out = llm.chat([{"role": "user", "content":
        f"为以下{PLATFORMS.get(platform, '')}文案出 5 个标题候选 + 5-10 个话题标签。\n"
        "标题要求：每个角度不同（悬念/数字/痛点/直给/情绪）。输出 markdown。"}],
        cfg=cfg, stage="review")
    atomic_write_text(os.path.join(pdir, F_TITLE_TAGS), (out or "（生成失败）").strip())


def _stage_adapt(proj, pdir, cfg, plan, platform):
    final = _read(os.path.join(pdir, F_FINAL))
    others = [p for p in PLATFORMS if p != platform][:2]
    names = "、".join(PLATFORMS[p] for p in others)
    out = llm.chat([{"role": "user", "content":
        f"把以下文案适配为 {names} 版本（保持核心信息，调整语气/长度/格式以符合各平台风格）。\n"
        f"原文：\n{final[:3000]}"}], cfg=cfg, stage="review")
    fp = os.path.join(pdir, F_FINAL.replace(".md", "-平台适配.md"))
    atomic_write_text(fp, (out or "（生成失败）").strip())
