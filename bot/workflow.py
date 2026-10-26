"""创作工作流：「一幕·神笔」创作系统四阶段落地
阶段0 偏好加载 / 阶段1 三层问答(快车道) / 阶段2 规划 / 阶段3 逐章创作 / 阶段4 校验
"""
import json, os, re, datetime
from . import llm, skill
from .safeio import atomic_write_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFS_FILE = os.path.join(ROOT, "user-preferences.json")

# ---------- 偏好系统（技能 shared-infrastructure 定义） ----------

def load_prefs():
    if os.path.exists(PREFS_FILE):
        try:
            return json.load(open(PREFS_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "preferences": {"favoriteGenres": [], "preferredProtagonist": "",
            "preferredPerspective": "", "preferredTone": "", "typicalChapterCount": None,
            "styleReferences": [], "dislikes": [], "creationHistory": []}}

def save_prefs(p):
    p["updatedAt"] = datetime.date.today().isoformat()
    atomic_write_json(PREFS_FILE, p)

# ---------- 阶段1：主题提取（快车道） ----------

def extract_config(user_text, cfg=None):
    prefs = load_prefs()
    sys_p = (
        "你是 「一幕·神笔」创作系统的创作策划。从用户的一句话主题里提取小说配置，严格输出 JSON，键：\n"
        "genre(题材)、idea(创意概要≤60字)、protagonist_type(男/女/双/群像)、protagonist(职业身份+核心性格≤30字)、"
        "conflict(核心冲突)、drive(主角驱动力)、worldview(世界观，主题没提就填\"\"）、"
        "perspective(叙事视角，没提填\"\"）、tone(基调，没提填\"\"）、theme(核心主题，没提填\"\"）、"
        "readers(目标读者，没提填\"\"）、chapters(用户指定了章数就填数字，否则填0)、"
        "special(特殊要求，没有填\"\"）、enough(主题信息是否足够直接规划: true/false)\n"
        f"用户历史偏好参考：favoriteGenres={prefs['preferences'].get('favoriteGenres')}，"
        f"typicalChapterCount={prefs['preferences'].get('typicalChapterCount')}。只输出 JSON。"
    )
    return llm.chat_json([{"role": "system", "content": sys_p},
                          {"role": "user", "content": user_text}], cfg=cfg, stage="planning")

def fill_defaults(cfg, chapters_default=10):
    """快车道补全：只补缺失字段（技能 Q4-Q8 的🎲随机生成），不覆盖用户已给的值"""
    missing = [k for k in ("worldview", "perspective", "tone", "theme", "readers") if not cfg.get(k)]
    if not missing and cfg.get("chapters"):
        cfg.setdefault("chapters", chapters_default)
        return cfg
    keys = list(missing)
    if not cfg.get("chapters"):
        keys.append("chapters")
    if not keys:
        return cfg
    sys_p = ("根据下面的小说配置，仅为空缺字段生成合理且出彩的取值（chapters 若需填则取 10/15/20/30 之一），"
             "已填字段不要输出。只输出 JSON：{" + ", ".join(f'"{k}"' for k in keys) + "}")
    out = llm.chat_json([{"role": "system", "content": sys_p},
                         {"role": "user", "content": json.dumps(cfg, ensure_ascii=False)}], cfg=cfg, stage="planning")
    for k in keys:  # 只回填空缺项，绝不覆盖已有值
        if k not in cfg or not cfg[k]:
            cfg[k] = out.get(k) or (k != "chapters" and "" or (chapters_default if k == "chapters" else ""))
    return cfg

def config_summary(cfg):
    return (f"📖 题材：{cfg.get('genre','')} — {cfg.get('idea','')}\n"
            f"🧑 主角：{cfg.get('protagonist','')}\n"
            f"💥 冲突：{cfg.get('conflict','')} | 驱动力：{cfg.get('drive','')}\n"
            f"🌍 世界观：{cfg.get('worldview') or '待定'} | 👁 视角：{cfg.get('perspective') or '待定'} | 🎨 基调：{cfg.get('tone') or '待定'}\n"
            f"💡 主题：{cfg.get('theme') or '待定'} | 📊 规模：{cfg.get('chapters') or '待定'}章")

def _craft_genre(cfg):
    """craft 注入用的题材标识：优先用前端归一好的 skillkit key（genre_key），
    没有则回落到中文题材名（skillkit._genre_key 会再做一次归一）。"""
    return str(cfg.get("genre_key") or cfg.get("genre") or "").strip()

def agent_skills_block(agent, cfg, stage, max_bytes=6000):
    """岗位×技能绑定（v2.1 P2）：该岗位在设置里绑了技能才注入，没绑定返回空串。

    这样默认路径的提示词体积与旧版一致，只有用户显式给某个岗位配了技能，
    那部分技能内容才会进入该岗位的上下文。
    """
    try:
        from . import agents as _agents, skillkit as _skillkit
        if not _agents.skills_for(agent):
            return ""
        return _skillkit.stage_inject(stage, cfg.get("medium") or "novel",
                                      genre=_craft_genre(cfg), agent=agent,
                                      max_bytes=max_bytes)
    except Exception:
        return ""

# ---------- 阶段2：规划 ----------

def gen_characters(cfg):
    sys_p = (
        "你是小说人物设计师，执行 「一幕·神笔」创作系统的 00-人物档案 生成。"
        "使用技能的 character-template.md 模板结构，参考 character-building.md 方法论：\n"
        "人物档案必须详细到可以直接指导写作：每个角色的性格核心、致命缺陷、说话风格/口头禅、恐惧/弱项、背景故事都要具体。\n"
        "只输出 markdown 档案正文（主角1-2名、反派1名、配角2-4名）。"
    )
    user_p = (f"小说配置：{json.dumps(cfg, ensure_ascii=False)}\n"
              f"【技能模板】\n{skill.guide('character-template')[:1500]}\n"
              f"【建人方法论摘录】\n{skill.guide('character-building')[:2500]}")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg, stage="planning")

def gen_outline(cfg):
    sys_p = (
        "你是小说架构师，执行 「一幕·神笔」创作系统的 01-大纲 生成。使用 outline-template.md 模板结构，"
        "参考 plot-structures.md 选一种经典结构。大纲必须人物驱动、冲突递进、每章有钩子。\n"
        "输出 markdown，必须包含：## 基本信息 / ## 章节规划（表格：章节|标题|核心事件|承接上章|章首引子类型|悬念钩子|出场人物|场景列表）"
        "/ ## 全书悬念线 / ## 设定词典（表格先留1行示例即可）/ ## 章节详情规划（每章一个 ### 第N章 小节，写核心事件展开、场景分镜、伏笔）"
        "章节数 = 配置里的 chapters。只输出 markdown。"
    )
    user_p = (f"小说配置：{json.dumps(cfg, ensure_ascii=False)}\n"
              f"【技能模板】\n{skill.guide('outline-template')[:2000]}\n"
              f"【情节结构方法摘录】\n{skill.guide('plot-structures')[:3000]}")
    planner_skills = agent_skills_block("planner", cfg, "P0")
    if planner_skills:
        sys_p += "\n\n" + planner_skills
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg, stage="planning")

def gen_style(cfg):
    sys_p = ("你执行 「一幕·神笔」创作系统的 03-文风基准 生成，包含四部分：\n"
             "## 文风指纹（对话占比目标35%-45%、句子节奏、段落呼吸、叙述距离、描写倾向，依据配置的基调/读者生成）\n"
             "## 基准段落（2段共600-1000字的正文范例，直接写出，体现目标文风）\n"
             "## 负面清单（引用 chapter-guide.md 的「AI 写作痕迹清除」条目）\n"
             "## 本书校准段落（初始留空，注明第1章完成后回填）\n只输出 markdown。")
    user_p = (f"小说配置：{json.dumps(cfg, ensure_ascii=False)}\n"
              f"【AI 写作痕迹清除条目】\n{skill.guide('chapter-guide')[:3500]}")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg, stage="planning")

def gen_titles(cfg, n=3):
    sys_p = (f"执行 「一幕·神笔」创作系统的标题生成：参考 title-guide.md，基于故事元素生成 {n} 个候选标题，"
             "每个使用不同的创作技巧，附一句话说明。输出 JSON：{\"titles\":[{\"title\":..,\"technique\":..,\"why\":..}]}")
    user_p = (f"故事元素：{json.dumps({k: cfg.get(k) for k in ('genre','idea','conflict','protagonist','theme','tone')}, ensure_ascii=False)}\n"
              f"【标题技巧摘录】\n{skill.guide('title-guide')[:2500]}")
    out = llm.chat_json([{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}], cfg=cfg, stage="planning")
    return out.get("titles", [])

# ---------- 阶段3：逐章创作 ----------

def _writing_system(ctx):
    """写作系统提示：「一幕·神笔」原创创作系统（运行时读内置 skill/ 文件）"""
    cfg = ctx.get("cfg") or {}
    no = ctx.get("chapter_no") or 0
    parts = [
        "你是「一幕·神笔 · ShenBi NovelCraft」原创创作系统的资深中文网文作者，目标是写出能上排行榜前列的长篇。",
        "三大黄金法则 + 七条创作底线：\n" + skill.golden_rules(),
        f"【题材 craft（{cfg.get('genre','') or '通用'}）】\n" + skill.genre(_craft_genre(cfg))[:900],
        "【章纲写作规范】\n" + skill.guide("chapter-guide")[:2500],
        "【钩子与卡点】\n" + skill.guide("hook-techniques")[:2200],
        "【对话写法】\n" + skill.guide("dialogue-writing")[:1800],
        "【扩充（字数不足时，不注水）】\n" + skill.guide("content-expansion")[:1500],
        "【反 AI 腔（边写边去腔）】\n" + skill.anti_slop()[:1100],
    ]
    if no and no <= 3:
        parts.append("【黄金三章门（本章在前 3 章）】\n" + skill.golden3()[:800])
    if ctx.get("longform_card"):
        parts.append("【长篇章卡纪律】本章只推进章卡列出的目标/冲突/转折/兑现与章末钩子；"
                     "不提前消费后续卷弧事件；人物已知信息以权威状态为准；新事实若与状态冲突，以状态为准。")
    if ctx.get("references_block") or cfg.get("_reference_block"):
        parts.append("【参考资料纪律】只吸收事实、结构与风格，禁止连续复制原文 12 字以上；"
                     "引用须标注来源；与用户设定冲突时以用户设定为准。")
    writer_skills = agent_skills_block("writer", cfg, "P2")
    if writer_skills:
        parts.append(writer_skills)
    parts += [
        "硬性要求：正文 3000-5000 汉字；章首引子 50-150 字；对话占比 30%+；全章≥2个爽点/张力波峰；"
        "≥1个意外转折；结尾必留钩子；严守人物性格与知情范围一致性；动笔前先消化上一章结尾语态。",
        "输出格式：## 本章概要 / ## 章首引子 / ## 正文 / ## 章节备注。"
    ]
    return "\n\n".join(parts)

def _continuity_block(ctx):
    """把 连续性结构（故事圣经/线索账本/章节摘要/时间线）拼成写前上下文；旧项目缺失则自动省略。"""
    parts = []
    if ctx.get("story_bible"):
        parts.append(f"【故事圣经（事实来源）】\n{ctx['story_bible'][:1600]}")
    if ctx.get("lore_relevant"):
        parts.append(f"【本章相关设定（关键词检索，写错=崩设定）】\n{ctx['lore_relevant']}")
    if ctx.get("clue_ledger"):
        parts.append(f"【线索账本（须兑现/不得乱揭的谜团）】\n{ctx['clue_ledger'][:1500]}")
    if ctx.get("chapter_summaries"):
        parts.append(f"【前文章节摘要（承接状态，勿复述）】\n{ctx['chapter_summaries'][-2500:]}")
    if ctx.get("timeline"):
        parts.append(f"【时间线与连续性核对】\n{ctx['timeline'][:1200]}")
    if not parts:
        return ""
    return ("\n" + "\n\n".join(parts) +
            "\n【连续性纪律】动笔前核对：人物姓名/称谓/身份、谁此刻知道什么、时间/伤势/物品/规则是否前后一致；"
            "本章开头承接上一章结尾的即时状态，不复述已发生内容。")

def _longform_block(ctx):
    """长篇内核：本章章卡 + 权威状态 + 连续性警告 + 参考资料（缺失自动省略）。"""
    from . import longform as _lf
    parts = []
    card = ctx.get("longform_card")
    if isinstance(card, dict) and (card.get("goal") or card.get("conflict") or card.get("turn")):
        parts.append("【本章章卡（最高优先级：只写卡内事件，不提前消费后续剧情）】\n"
                     + _lf.render_card(card))
    if ctx.get("state_excerpt"):
        parts.append("【权威状态（本章相关；与人物档案冲突时以此为准）】\n"
                     + str(ctx["state_excerpt"])[:5000])
    warns = ctx.get("continuity_warnings") or []
    if warns:
        parts.append("【连续性警告（动笔前处理）】\n" + "\n".join(f"- {w}" for w in warns[:10]))
    refs = ctx.get("references_block") or (ctx.get("cfg") or {}).get("_reference_block")
    if refs:
        parts.append(str(refs)[:4500])
    return "\n\n".join(parts)


def _p5_block(ctx):
    """P5：故事图谱相关实体 + 语义检索的既往片段（防长篇遗忘设定）；缺失则省略。"""
    g, r = ctx.get("graph_ctx") or {}, ctx.get("retrieval_ctx") or []
    parts = []
    if g and (g.get("persons") or g.get("active_threads")):
        s = "【故事图谱·本章相关实体】\n- 相关人物：" + ("、".join(g.get("persons", [])[:8]) or "（未登记）")
        if g.get("active_threads"):
            s += "\n- 活跃/临近兑现伏笔：" + "、".join(g.get("active_threads", [])[:6]) + "（须推进或兑现，勿丢）"
        parts.append(s)
    if r:
        s = "【相关既往片段（跨章记忆，防遗忘设定，勿复述）】"
        for x in r:
            s += f"\n- [第{x.get('ch') or '?'}章·{x.get('src')}] {(x.get('snippet') or '')[:150]}"
        parts.append(s)
    return "\n".join(parts)

def write_chapter(ctx):
    cont = _continuity_block(ctx)
    p5 = _p5_block(ctx)
    lf = _longform_block(ctx)
    # v0.4：读者即时修正指令（运行中随时插一条，标 immediate 立即生效 / 否则下一条生效）
    qnote = (ctx.get("queue_note") or "").strip()
    qblock = ("\n【读者即时指令（本章必须优先满足，不破坏既有剧情设定）】\n" + qnote) if qnote else ""
    # v0.6 B：总架构蓝图（并行写作时写任何一章前必读，放最前对齐全局）
    bp = (ctx.get("blueprint") or "").strip()
    bblock = (bp + "\n") if bp else ""
    if ctx.get("continuation"):
        user_p = (
            f"{bblock}"
            f"{lf}\n"
            f"【本书文风基准】\n{ctx['style_text']}\n"
            f"【人物档案】\n{ctx['characters_text']}\n"
            f"【全书大纲要点】\n{ctx.get('outline_text','')[:3000]}\n"
            f"【上一章结尾】\n{ctx.get('prev_tail') or '（无）'}\n"
            f"{cont}\n"
            f"{p5}\n"
            f"{qblock}\n"
            f"现在续写第 {ctx['chapter_no']} 章。这是承接前文的续写章：请严格消化上一章结尾的语态与悬念，"
            f"自然推进剧情，可引入新的转折/冲突/角色（须符合人物性格与既定设定），严禁重复已写情节；"
            f"严守人物性格一致性，结尾必留钩子。输出章节文件内容。"
        )
        return llm.chat([{"role": "system", "content": _writing_system(ctx)},
                         {"role": "user", "content": user_p}], cfg=ctx.get("cfg"), stage="writing")
    user_p = (
        f"{bblock}"
        f"{lf}\n"
        f"【本书文风基准】\n{ctx['style_text']}\n"
        f"【人物档案】\n{ctx['characters_text']}\n"
        f"【大纲】\n{ctx['outline_text']}\n"
        f"【上一章结尾】\n{ctx.get('prev_tail') or '（第1章，无）'}\n"
        f"{cont}\n"
        f"{p5}\n"
        f"{qblock}\n"
        f"现在写第 {ctx['chapter_no']} 章「{ctx['chapter_title']}」。严格按该章在大纲中的规划（核心事件/承接/钩子/出场人物/场景）创作。"
        f"输出章节文件内容。"
    )
    return llm.chat([{"role": "system", "content": _writing_system(ctx)},
                     {"role": "user", "content": user_p}], cfg=ctx.get("cfg"), stage="writing")

def expand_chapter(body, ctx):
    bp = (ctx.get("blueprint") or "").strip()
    bblock = (bp + "\n") if bp else ""
    user_p = (f"{bblock}下面这章字数不足 3000，请按 content-expansion.md 的扩充技巧（增加对话场景/人物内心/细节描写）"
              f"扩写到 3000-5000 字，保持情节与钩子不变，不要注水。\n\n{body}")
    return llm.chat([{"role": "system", "content": _writing_system(ctx)},
                     {"role": "user", "content": user_p}], cfg=ctx.get("cfg"), stage="writing")

def chapter_meta(body, ctx):
    sys_p = ("从章节正文提取：summary(300-500字本章摘要，供下章承接)、new_terms(本章首次出现的设定名词列表，"
             "每项 {name, known, truth}，无则空数组)、hook(结尾钩子一句话)。只输出 JSON。")
    return llm.chat_json([{"role": "system", "content": sys_p},
                          {"role": "user", "content": f"章节：第{ctx['chapter_no']}章 {ctx['chapter_title']}\n{body}"}],
                         cfg=ctx.get("cfg"), stage="writing")

def style_calibrate(ch1_body, cfg=None):
    sys_p = ("从第1章定稿中选出 1-2 个最佳段落（开头钩子段+高张力段，共600-1000字原文照录），"
             "作为本书校准段落。只输出选中的原文段落，不要评论。")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": ch1_body[:12000]}], cfg=cfg, stage="writing")

# ---------- 大纲解析 ----------

def outline_chapter_row(outline_md, no):
    """从大纲里抠出第 no 章的规划段落（表格行 + 详情小节）"""
    m = re.search(rf"第{no}章[^\n]*\n(.*?)(?=\n第{no+1}章|\n##|\Z)", outline_md, re.S)
    row = ""
    if m:
        row = m.group(0).strip()
    return row or outline_md[-3000:]

# ---------- 阶段2.5：连续性结构（故事圣经 / 线索账本 / 时间线） ----------

def gen_story_bible(cfg):
    """故事圣经：全书唯一事实来源（引擎/承诺/主题/世界规则/视角底线/主线结局/设定词典）。"""
    sys_p = ("执行 「一幕·神笔」的『故事圣经』：建立全书唯一事实来源。用 story-bible 模板，"
             "填写 故事引擎(一句话) / 故事承诺(题材·卖点·情绪·兑现方式) / 主题 / 世界规则与系统边界 / "
             "叙事视角与风格底线 / 总主线与结局方向 / 设定词典。内容须具体可执行，能直接指导后续写作与校验。"
             "只输出 markdown 正文。")
    user_p = (f"小说配置：{json.dumps(cfg, ensure_ascii=False)}\n"
              f"【模板】\n{skill.template('story-bible')[:1400]}")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg, stage="planning")

def gen_clue_ledger(cfg, outline_md):
    """线索账本：从大纲提取重要悬念/伏笔，登记为可追踪表（含真实答案与兑现计划）。"""
    sys_p = ("执行 ShenBi『线索账本』：从大纲中提取所有服务主线的悬念/伏笔，登记为线索表"
             "（线索ID/首次出现/初始表象/真实含义/可推理证据/误导方式/计划兑现/状态）。"
             "只登记真正重要的线索（控制 3-8 条），每条都要有明确兑现计划与作者掌握的真实答案。"
             "只输出 markdown 表 + 简短纪律说明。")
    user_p = (f"小说配置：{json.dumps(cfg, ensure_ascii=False)}\n"
              f"【大纲】\n{outline_md[:4000]}\n【模板】\n{skill.template('clue-ledger')[:900]}")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg, stage="planning")

def gen_timeline(cfg, outline_md):
    """时间线与连续性：全书事件时间线 + 连续性核对清单（每章写前必查）。"""
    sys_p = ("执行 ShenBi『时间线与连续性』：建立全书事件时间线（章节/时间/地点/事件/持续/先后）与"
             "连续性核对清单（人物身份·知情范围·伤势·物品·世界规则·后果）。用于每章写前核对前后一致。"
             "只输出 markdown。")
    user_p = (f"小说配置：{json.dumps(cfg, ensure_ascii=False)}\n"
              f"【大纲】\n{outline_md[:4000]}\n【模板】\n{skill.template('timeline')[:900]}")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg, stage="planning")

# ---------- 阶段4.5：分层编辑审稿（「一幕·神笔」五角色 + 百分制） ----------

def layered_review(book_md, cfg, l1_findings=None):
    """[兼容别名] 现为五角色审校 + 百分制（见 five_role_review / score_dimensions）。"""
    return five_role_review(book_md, cfg, l1_findings)

def fix_chapter(ch_body, action, ctx):
    """按 fix_action 对单章做局部重写（保留章题/引子/正文结构，只修被指出的问题，不注水不改情节走向）。"""
    sys_p = "你执行「一幕·神笔」分层修复。只按下述指令局部修改本章，保留原情节走向、钩子与字数量级，" \
            "不注水、不改核心设定。输出整章修正后的 markdown（含 ## 本章概要/## 章首引子/## 正文/## 章节备注 结构）。"
    user_p = f"修复指令：{action}\n\n待修章节：\n{ch_body[:9000]}"
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=ctx.get("cfg"), stage="review")

def deslop_chapter(ch_body, ctx, max_findings=15):
    """AI 腔定点去味（两阶段，不装确定）：
    1) 纯规则 slop_checker.scan → blocking/advisory findings + top_words（不调 LLM，零误报定位）
    2) 把 findings 交给 LLM 出「最小改法」定点改写（保留情节/钩子/字数量级，只修被指出的位置）
    返回 {"report": dict, "cleaned": str, "findings": list}；LLM 失败时 cleaned 保持原文。"""
    from . import slop_checker
    report = slop_checker.scan(ch_body)
    findings = report.get("findings") or []
    top = slop_checker.top_words(ch_body, n=8)
    top_txt = "、".join(f"{w}×{c}" for w, c in top) if top else "（无明显重复 2-gram）"
    if not findings:
        return {"report": report, "cleaned": ch_body, "findings": []}
    flines = []
    for f in findings[:max_findings]:
        flines.append(f"- [第{f.get('line','?')}行] ({f.get('severity','?')}/{f.get('category','?')}) {f.get('snippet','')[:60]} → {f.get('hint','')[:80]}")
    sys_p = ("你是「一幕·神笔」去味师（deslopper）。按下列定点诊断做最小改写：只修改被指出的位置，"
             "保留情节走向、钩子、关键名词与字数量级；用具体细节替换空话，不新增套话；"
             "【重复高频 2-gram】必须去重。输出整章修正后的 markdown。")
    user_p = ("【定点诊断（按行定位，行号为准）】\n" + "\n".join(flines) +
              f"\n\n【重复高频 2-gram】{top_txt}\n\n待修章节：\n{ch_body[:9000]}")
    try:
        cleaned = llm.chat([{"role": "system", "content": sys_p},
                            {"role": "user", "content": user_p}], cfg=ctx.get("cfg"), stage="review")
        if not cleaned or len(cleaned.strip()) < 200:
            cleaned = ch_body
    except Exception:
        cleaned = ch_body
    return {"report": report, "cleaned": cleaned, "findings": findings}

# ---------- 排行级质检：五角色审校 + 百分制（对标 90 达标线） ----------

SCORE_WEIGHTS = {"结构": 20, "因果可信": 20, "人物": 15, "连续性": 20, "语言": 15, "读者体验": 10}
PASS_LINE = 90  # 百分制达标线

def score_dimensions(scores):
    """各维 0-10 → 百分制加权总分。"""
    s = scores or {}
    total, weight = 0.0, 0
    for k, w in SCORE_WEIGHTS.items():
        v = s.get(k)
        if isinstance(v, (int, float)):
            total += (float(v) / 10.0) * w
            weight += w
    return round(total, 1) if weight else 0

def chapter_review(ch_body, cfg, context=""):
    """v0.5 M1：对**单章**做 5 角色百分制评分（评这一章而非整书）。
    返回 {"scores":{6维0-10}, "weak":[维度名×3], "verdict":"一句话"}；失败返回 {}。
    评的是"这一章在本书里是否达标"，吃 context（文风/人物/前后章）做参照。"""
    try:
        genre_txt = skill.genre(_craft_genre(cfg))
        sys_p = ("你是「一幕·神笔」五角色审校团。现在只审**下面这一章**（在整本书语境下），"
                 "按 6 维打分（0-10）：结构/因果可信/人物/连续性/语言/读者体验。"
                 f"【题材craft】{genre_txt[:600]}\n【反AI腔(供语言维)】{skill.anti_slop()[:600]}\n"
                 + (f"【参照上下文(文风/人物/前后章)】\n{context[:4000]}\n" if context else "")
                 + "评分纪律：就事论事、按证据打分；找出最弱的 3 个维度。"
                 '输出 JSON：{"scores":{"结构":0-10,"因果可信":0-10,"人物":0-10,"连续性":0-10,"语言":0-10,"读者体验":0-10},'
                 '"weak":["最弱维度名1","最弱维度名2","最弱维度名3"],'
                 '"evidence":[{"dimension":"维度名","quote":"必须引用本章原文一句","why":"为何扣分"}],'
                 '"verdict":"一句话本章质量判断与最该修的点"}。'
                 'evidence 至少 2 条，quote 必须是原文片段，不得编造。只输出 JSON。')
        user_p = f"待审章节（第 ? 章，只评它）：\n{ch_body[:9000]}"
        reviewer_skills = agent_skills_block("reviewer", cfg, "P4")
        if reviewer_skills:
            sys_p += "\n" + reviewer_skills
        # 规则层：slop 定位（语言维证据，不装确定）
        try:
            from . import slop_checker
            _r = slop_checker.scan(ch_body)
            _top = slop_checker.top_words(ch_body, n=5)
            _top_txt = "、".join(f"{w}×{c}" for w, c in _top) if _top else "无"
            _slop = ("【规则层定位】slop 命中 %d 处（blocking %d）；高频 2-gram：%s；"
                     "以上仅为定位线索，按证据打分，不装确定。" %
                     (len(_r.get("findings",[])), _r.get("stats",{}).get("blocking",0), _top_txt)
                     if _r.get("findings") else
                     "【规则层定位】slop 零命中；零命中不替代人工三层扫描（表达/用笔/故事）。")
            sys_p += "\n" + _slop
        except Exception:
            pass
        out = llm.chat_json([{"role": "system", "content": sys_p},
                             {"role": "user", "content": user_p}], cfg=cfg, stage="review")
        if not isinstance(out, dict) or not out.get("scores"):
            return {}
        scores = out.get("scores")
        # weak：没给就按最低 3 维补齐
        weak = out.get("weak") or []
        if not isinstance(weak, list) or not weak:
            weak = [k for k, _ in sorted(((k, scores.get(k)) for k in SCORE_WEIGHTS),
                                          key=lambda kv: (kv[1] if isinstance(kv[1], (int, float)) else 99))[:3]]
        ev = [e for e in (out.get("evidence") or [])
              if isinstance(e, dict) and e.get("quote")][:6]
        return {"scores": scores, "weak": [w for w in weak if w in SCORE_WEIGHTS][:3] or weak[:3],
                "evidence": ev, "verdict": (out.get("verdict") or "").strip()[:200]}
    except Exception:
        return {}

def five_role_review(book_md, cfg, l1_findings=None):
    """五角色审校团（Architect/Scribe/Editor/Guardian/Curator）单次 LLM 覆盖，
    吃 L1 确定性结果作为证据；返回 6 维(0-10)评分 + findings + fix_actions。"""
    genre_txt = skill.genre(_craft_genre(cfg))
    sys_p = ("你是「一幕·神笔」五角色审校团，对全书做分层编辑审稿并分工：\n"
             "Architect(结构/节拍/黄金三章)、Scribe(正文是否注水/推进)、"
             "Editor(语言·去AI腔，套用反AI腔清单)、Guardian(连续性，先采信 L1 再复核)、"
             "Curator(题材口吻一致)。每条结论必须引用原句作证据，无证据不判问题。\n"
             f"【题材craft】{genre_txt[:1000]}\n"
             f"【反AI腔清单(供Editor)】{skill.anti_slop()[:1100]}\n"
             f"【黄金三章门(供Architect)】{skill.golden3()[:800]}\n"
             f"【L1 确定性引擎结果(Guardian必复核)】\n"
             + ("\n".join(f"- [{f.get('severity')}] {f.get('check')} @ {f.get('location')}：{f.get('detail')}"
                           for f in (l1_findings or [])) or "（L1 无命中）") + "\n"
             "输出 JSON：{\"scores\":{\"结构\":0-10,\"因果可信\":0-10,\"人物\":0-10,\"连续性\":0-10,\"语言\":0-10,"
             "\"读者体验\":0-10},"
             "\"findings\":[{\"role\":\"Architect|Scribe|Editor|Guardian|Curator\",\"chapter\":N或\"全书\","
             "\"severity\":\"high|mid|low\",\"evidence\":\"原句引用\",\"problem\":\"问题\",\"fix\":\"建议\"}],"
             "\"fix_actions\":[{\"chapter\":N,\"action\":\"具体可执行的局部重写指令\"}]}。"
             "fix_actions 只放 severity=high 且能定位到章节的（最多 6 条）。只输出 JSON。")
    user_p = f"全书（节选）：\n{book_md[:30000]}"
    editor_skills = agent_skills_block("editor", cfg, "P4")
    if editor_skills:
        sys_p += "\n" + editor_skills
    return llm.chat_json([{"role": "system", "content": sys_p},
                          {"role": "user", "content": user_p}], cfg=cfg, stage="review")

# ---------- P5c：读者人格 beta 读（可选，reader_panel 开关） ----------
_READER_PERSONAS = ["数据流爽文党（追更新、爱逆袭打脸、容忍度低）",
                    "慢热逻辑党（重设定因果、忌跳逻辑、弃书点敏感）",
                    "情感代入党（追人物情感弧、重角色魅力与共情）"]

def reader_panel(book_md, cfg):
    """3 类读者人格试读 → 追读点/弃书点/建议。失败/关/解析不出返回 []。"""
    try:
        sys_p = ("你依次扮演三类读者人格，读完本书（节选）后各给反馈。"
                 "务必只输出一个 JSON 数组（不要 markdown 代码块、不要多余文字），3 个元素，每个："
                 '{"persona":"<人格名>","追读点":"...","弃书点":"...","建议":"..."}。')
        user_p = ("三类人格：\n" + "\n".join(f"- {p}" for p in _READER_PERSONAS)
                  + f"\n\n全书（节选）：\n{book_md[:30000]}")
        # 规则层：slop 定位（语言维证据，不装确定；全书级）
        try:
            from . import slop_checker
            _cb = book_md or ""
            _r = slop_checker.scan(_cb)
            _top = slop_checker.top_words(_cb, n=5)
            _top_txt = "、".join(f"{w}×{c}" for w, c in _top) if _top else "无"
            _slop = ("【规则层定位】全书 slop 命中 %d 处（blocking %d）；高频 2-gram：%s；"
                     "以上仅为定位线索，按证据打分，不装确定。" %
                     (len(_r.get("findings",[])), _r.get("stats",{}).get("blocking",0), _top_txt)
                     if _r.get("findings") else
                     "【规则层定位】全书 slop 零命中；零命中不替代人工三层扫描（表达/用笔/故事）。")
            sys_p += "\n" + _slop
        except Exception:
            pass
        out = llm.chat_json([{"role": "system", "content": sys_p},
                             {"role": "user", "content": user_p}], cfg=cfg, stage="review")
        # 容错：dict→取其 list 值；单对象→包成 list；去 markdown
        if isinstance(out, dict):
            for v in out.values():
                if isinstance(v, list):
                    out = v
                    break
            else:
                out = [out]
        if isinstance(out, dict):
            out = [out]
        if not isinstance(out, list):
            return []
        return [o for o in out if isinstance(o, dict) and o.get("persona")][:3] or [o for o in out if isinstance(o, dict)][:3]
    except Exception:
        return []

# ---------- v0.2：审查后定向重写（把审查结论 + 重写方向逐条带进重写） ----------
def guided_rewrite(ch_body, requirements, constraints, cfg):
    """基于审查结论与重写方向重写整章：逐条落实要求，不注水、不改核心走向、保留钩子。"""
    sys_p = ("你执行「一幕·神笔」的『审查后定向重写』。下面给出本章的审查结论与重写方向（逐条列明"
             "要补齐什么、要提升什么、哪里有问题），请据此重写整章。纪律：必须逐条落实要求；严禁注水、"
             "严禁改变核心情节走向与人物性格；保留已有钩子与设定一致性。"
             "输出整章重写后的 markdown（保留 ## 本章概要 / ## 章首引子 / ## 正文 / ## 章节备注 结构）。")
    user_p = (f"【重写要求（必须逐条落实）】\n{requirements}\n\n"
              f"【全书约束（人物/设定/大纲/连续性，须保持不冲突）】\n{constraints[:6000]}\n\n"
              f"【待重写原章】\n{ch_body[:9000]}")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg, stage="review")

def list_chapters_text(pdir):
    """拼接项目里所有已写章节文件（第NN章-*.md）为全书文本；无则 ''。"""
    import glob
    files = sorted(glob.glob(os.path.join(pdir, "第*.md")))
    out = [open(f, encoding="utf-8").read() for f in files if "大纲" not in os.path.basename(f)]
    return "\n\n".join(out)
