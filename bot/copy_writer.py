"""文案创作引擎（v4.12.0，medium=copy）。钩子库 + 正文 + 平台适配。

K1 需求 → K2 钩子库 → K3 正文 → K4 标题标签 → K5 平台适配
"""
import datetime
import json
import os

from .paths import DATA_ROOT
import re

from bot.safeio import ProjectPath, atomic_write_text, safe_join

from . import llm
from .mini_writer_base import MiniWriterProject, _safe, _read, make_get_project, run_pipeline

F_PLAN = "文案计划.json"
F_OUTLINE = "00-需求与钩子.md"
F_FINAL = "04-文案定稿.md"
F_TITLE_TAGS = "03-标题与标签.md"
# v4.33.5：钩子库原来只有字面量「01-钩子库.md」，工作台「钩子」页签拿不到映射
# 就 404（页签永远空白）。给一个常量，接口与写入共用同一个名字。
F_HOOKS = "01-钩子库.md"

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
PROJECTS_DIR = os.path.join(DATA_ROOT, "projects")


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
        _pipeline(proj, ProjectPath(proj), cfg)
    finally:
        proj.running = False
    return proj


def resume_project(key: str):
    key = CopyProject.resolve_key(key)
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
        llm.book(key, "writing")   # v4.24.16：resume 在新线程跑，不标记则 token 记账失活
        _pipeline(proj, ProjectPath(proj), plan.get("config") or {}, plan=plan)
    finally:
        proj.running = False
    return proj


def _pipeline(proj, pdir: str, cfg: dict, plan: dict | None = None):
    plan = plan if isinstance(plan, dict) and plan else (proj.plan() or {})
    plan.setdefault("meta", {})
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
        ("K2", "钩子库", lambda: _stage_hooks(proj, pdir, cfg, plan), True),
        ("K3", "正文", lambda: _stage_body(proj, pdir, cfg, plan, ktype, platform, tone)),
        ("K4", "标题标签", lambda: _stage_titles(proj, pdir, cfg, plan, platform), True),
        ("K5", "适配", lambda: _stage_adapt(proj, pdir, cfg, plan, platform), True),
    ]
    run_pipeline(proj, cfg, stages, plan=plan, done_msg="✅ 文案定稿完成")


def _stage_brief(proj, pdir, cfg, plan, ktype, platform, tone):
    atomic_write_text(os.path.join(pdir, F_OUTLINE),
                      f"# 需求单\n\n类型：{ktype}\n平台：{platform}\n语气：{tone}\n"
                      f"主题/产品：{cfg.get('title') or ''}\n"
                      f"卖点与受众：{cfg.get('idea') or ''}\n"
                      + (f"口播时长：{cfg.get('duration_secs') or cfg.get('duration') or 60} 秒\n" if ktype == "口播稿" else ""))


def _stage_hooks(proj, pdir, cfg, plan):
    if cfg.get("with_hooks") is False:
        return
    out = llm.chat([{"role": "user", "content":
        f"为以下产品/主题出 10 个前 3 秒钩子候选（口播/短视频开场第一句）。\n"
        f"主题：{cfg.get('title')}\n卖点：{cfg.get('idea')}\n"
        "钩子类型覆盖：悬念、反差、痛点、数字、热点借势、提问、直给利益。每个一行。\n"
        "输出 markdown 列表。"}], cfg=cfg, stage="review")
    atomic_write_text(os.path.join(pdir, F_HOOKS),
                      f"# 钩子库（前 3 秒开场）\n\n{out or '（生成失败）'}")


def _stage_body(proj, pdir, cfg, plan, ktype, platform, tone):
    hooks_fp = os.path.join(pdir, F_HOOKS)
    hooks = _read(hooks_fp)[:1500] if os.path.exists(hooks_fp) else ""
    duration = cfg.get("duration_secs") or cfg.get("duration") or 60
    prompt = f"写一篇{ktype}。平台：{platform}。语气：{tone}。\n"
    if ktype == "口播稿":
        prompt += (f"口播时长约 {duration} 秒。\n"
                   "格式：用 [停顿] [重音] [语速加快] 等标注节奏；分段对应画面切换。"
                   "开头 3 秒必须有钩子；结尾有 CTA（关注/点击/购买）。")
    elif ktype == "种草笔记":
        prompt += "小红书风格：emoji 适量、分段短、有「亲测」「真实感受」的口吻、结尾带标签。"
    elif ktype == "营销长文":
        prompt += "营销长文：AIDA 或 PAS 框架；有数据/案例支撑；段落清晰。"
    else:
        prompt += "宣传文案：正式但有温度；突出核心卖点与差异化。"
    prompt += f"\n主题：{cfg.get('title')}\n卖点与受众：{cfg.get('idea')}\n"
    prompt += ("\n合规红线：遵守广告法，不使用“0成本/专治/治疗/100%有效”等绝对化或医疗功效表述；"
               "不得编造价格、销量、检测报告；价格与规格必须前后一致，未提供时不要写。")
    if hooks:
        prompt += f"\n钩子参考（选一个最好的用，不要全用）：\n{hooks}\n"
    # v4.16.3：字数守护——口播按时长（约 4 字/秒）给区间；其他类型默认 400 字
    from bot import wordguard as _wg
    _dur = int(cfg.get("duration_secs") or cfg.get("duration") or 0)
    # v5.2：30 秒口播约 120 字；旧逻辑硬性 300 字会把 30 秒稿写成 75 秒。
    if ktype == "口播稿" and _dur:
        _min_words = max(100, _dur * 4)
        _max_words = max(_min_words + 20, _dur * 5)
        prompt += (f"\n**字数区间：{_min_words}-{_max_words} 字（硬性区间，少于下限或多于上限都不合格）**"
                   "（交稿前自查；不要解释规则，直接写文案）。")
    else:
        _min_words = 400
        _max_words = 0
        prompt += f"\n**硬性字数下限：不少于 {_wg.target_of(_min_words)} 字**（交稿前自查）。"
    body = llm.chat([{"role": "user", "content": prompt}], cfg=cfg, stage="writing", max_tokens=3000)
    # v5.2：模型被矛盾规则绕晕时会输出「字数冲突说明」，必须重生成而不是当正文。
    def _meta_reply(t: str) -> bool:
        return bool(re.search(r"逻辑矛盾|字数上限冲突|无法直接满足|作为(?:一个)?AI|我不能同时满足", (t or "")[:300]))
    if _meta_reply(body):
        body = llm.chat([{"role": "user", "content":
            "直接输出成品文案正文，不要解释任何规则、字数或冲突。\n\n" + prompt}],
            cfg=cfg, stage="writing", max_tokens=3000) or ""
    # v5.2：免费模型经常无视字数下限；最多补写两次，避免 30 秒口播只剩 70 字。
    for _ in range(2):
        if _wg.cn_len(body) >= _min_words:
            break
        _need = (_max_words - _wg.cn_len(body) + 20) if (_max_words and ktype == "口播稿") \
            else max(250, _min_words - _wg.cn_len(body) + 120)
        _cont = _wg.clean_continuation(llm.chat([{"role": "user", "content":
            _wg.continue_prompt((body or "")[-500:], max(60, _need))}],
            cfg=cfg, stage="writing", max_tokens=2000) or "")
        if _wg.cn_len(_cont) < 30:
            break
        body = (body or "").rstrip() + "\n\n" + _cont
    if _meta_reply(body):
        raise ValueError("文案生成失败：模型反复解释字数规则，未输出正文")
    body = (body or "").strip()
    # v5.2：模型偶尔把内部「钩子选择」说明写进正文，发布稿必须清掉。
    body = re.sub(r"\s*（钩子(?:选择)?[：:][^）]*）", "", body).strip()
    if not body:
        raise ValueError("文案正文为空，停止后续阶段")
    # v5.2：30 秒口播写 300+ 字会变成 80 秒视频；超上限先要求模型压缩，失败再按句截断。
    if ktype == "口播稿" and _dur:
        if _wg.cn_len(body) > _max_words:
            compressed = ""
            try:
                compressed = llm.chat([{"role": "user", "content":
                    f"把下面这条口播压缩到不超过 {_max_words} 字（硬性上限），保留开头钩子和结尾 CTA，"
                    f"只输出正文，不要解释：\n\n{body}"}],
                    cfg=cfg, stage="writing", max_tokens=1200) or ""
            except Exception as e:
                proj.log(f"⚠️ 口播压缩失败（{e}），按句截断")
            if compressed and _min_words <= _wg.cn_len(compressed) <= _max_words + 10:
                body = compressed.strip()
            else:
                out = ""
                for sent in re.split(r"(?<=[。！？!?])", body):
                    if _wg.cn_len(out + sent) > _max_words:
                        break
                    out += sent
                body = (out or body[:_max_words]).strip()
    # v5.2：补写可能把新段落接在 CTA 后面，发布稿要保证行动指令落在最后。
    body = body.replace("**", "")
    body = re.sub(r"(?m)^\s*【画面[^】]*】\s*\n?", "", body).strip()
    _cta = re.search(r"[^。！？!?]*?(?:点击|小黄车|下单|购买|关注|链接|购物车|橱窗|主页)[^。！？!?]*[。！？!?]", body)
    if _cta and _cta.end() < len(body.rstrip()) - 16:
        _cta_text = _cta.group(0).strip()
        body = (body[:_cta.start()] + body[_cta.end():]).strip()
        body = body.rstrip() + "\n\n" + _cta_text
    elif not _cta:
        _cta_text = "想要同款或了解更多，点击下方链接；关注我，下期继续分享。"
        _budget = max(80, (_max_words if ktype == "口播稿" and _dur else 400) - _wg.cn_len(_cta_text) - 2)
        if _wg.cn_len(body) > _budget:
            _out = ""
            for _sent in re.split(r"(?<=[。！？!?])", body):
                if _wg.cn_len(_out + _sent) > _budget:
                    break
                _out += _sent
            body = (_out or body[:_budget]).strip()
        body = body.rstrip() + "\n\n" + _cta_text
    atomic_write_text(os.path.join(pdir, F_FINAL), body)
    meta = plan.setdefault("meta", {})
    meta["words"] = len(re.findall(r"[\u4e00-\u9fff]", body or ""))
    proj.save_plan(plan)


def _stage_titles(proj, pdir, cfg, plan, platform):
    final = _read(os.path.join(pdir, F_FINAL))
    if not final.strip():
        return          # v4.23.2：没有正文就不出标题（此前会拿空输入让模型瞎编）
    out = llm.chat([{"role": "user", "content":
        f"为以下{PLATFORMS.get(platform, '')}文案出 5 个标题候选 + 5-10 个话题标签。\n"
        "标题要求：每个角度不同（悬念/数字/痛点/直给/情绪）。输出 markdown。\n\n"
        # v4.23.2 修真 bug：此前只读了正文却没拼进 prompt，模型看不到文案，标题全是凭空编的
        f"【文案正文】\n{final[:4000]}"}],
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
