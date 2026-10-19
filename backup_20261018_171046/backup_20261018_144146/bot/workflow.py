"""创作工作流：chinese-novelist 技能的四个阶段落地
阶段0 偏好加载 / 阶段1 三层问答(快车道) / 阶段2 规划 / 阶段3 逐章创作 / 阶段4 校验
"""
import json, os, re, datetime
from . import llm, skill

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
    with open(PREFS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)

# ---------- 阶段1：主题提取（快车道） ----------

def extract_config(user_text, cfg=None):
    prefs = load_prefs()
    sys_p = (
        "你是 chinese-novelist 技能的创作策划。从用户的一句话主题里提取小说配置，严格输出 JSON，键：\n"
        "genre(题材)、idea(创意概要≤60字)、protagonist_type(男/女/双/群像)、protagonist(职业身份+核心性格≤30字)、"
        "conflict(核心冲突)、drive(主角驱动力)、worldview(世界观，主题没提就填\"\"）、"
        "perspective(叙事视角，没提填\"\"）、tone(基调，没提填\"\"）、theme(核心主题，没提填\"\"）、"
        "readers(目标读者，没提填\"\"）、chapters(用户指定了章数就填数字，否则填0)、"
        "special(特殊要求，没有填\"\"）、enough(主题信息是否足够直接规划: true/false)\n"
        f"用户历史偏好参考：favoriteGenres={prefs['preferences'].get('favoriteGenres')}，"
        f"typicalChapterCount={prefs['preferences'].get('typicalChapterCount')}。只输出 JSON。"
    )
    return llm.chat_json([{"role": "system", "content": sys_p},
                          {"role": "user", "content": user_text}], cfg=cfg)

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
                         {"role": "user", "content": json.dumps(cfg, ensure_ascii=False)}], cfg=cfg)
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

# ---------- 阶段2：规划 ----------

def gen_characters(cfg):
    sys_p = (
        "你是小说人物设计师，执行 chinese-novelist 技能的 00-人物档案 生成。"
        "使用技能的 character-template.md 模板结构，参考 character-building.md 方法论：\n"
        f"人物档案必须详细到可以直接指导写作：每个角色的性格核心、致命缺陷、说话风格/口头禅、恐惧/弱项、背景故事都要具体。\n"
        "只输出 markdown 档案正文（主角1-2名、反派1名、配角2-4名）。"
    )
    user_p = (f"小说配置：{json.dumps(cfg, ensure_ascii=False)}\n"
              f"【技能模板】\n{skill.guide('character-template')[:1500]}\n"
              f"【建人方法论摘录】\n{skill.guide('character-building')[:2500]}")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg)

def gen_outline(cfg):
    sys_p = (
        "你是小说架构师，执行 chinese-novelist 技能的 01-大纲 生成。使用 outline-template.md 模板结构，"
        "参考 plot-structures.md 选一种经典结构。大纲必须人物驱动、冲突递进、每章有钩子。\n"
        "输出 markdown，必须包含：## 基本信息 / ## 章节规划（表格：章节|标题|核心事件|承接上章|章首引子类型|悬念钩子|出场人物|场景列表）"
        "/ ## 全书悬念线 / ## 设定词典（表格先留1行示例即可）/ ## 章节详情规划（每章一个 ### 第N章 小节，写核心事件展开、场景分镜、伏笔）"
        "章节数 = 配置里的 chapters。只输出 markdown。"
    )
    user_p = (f"小说配置：{json.dumps(cfg, ensure_ascii=False)}\n"
              f"【技能模板】\n{skill.guide('outline-template')[:2000]}\n"
              f"【情节结构方法摘录】\n{skill.guide('plot-structures')[:3000]}")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg)

def gen_style(cfg):
    sys_p = ("你执行 chinese-novelist 技能的 03-文风基准 生成，包含四部分：\n"
             "## 文风指纹（对话占比目标35%-45%、句子节奏、段落呼吸、叙述距离、描写倾向，依据配置的基调/读者生成）\n"
             "## 基准段落（2段共600-1000字的正文范例，直接写出，体现目标文风）\n"
             "## 负面清单（引用 chapter-guide.md 的「AI 写作痕迹清除」条目）\n"
             "## 本书校准段落（初始留空，注明第1章完成后回填）\n只输出 markdown。")
    user_p = (f"小说配置：{json.dumps(cfg, ensure_ascii=False)}\n"
              f"【AI 写作痕迹清除条目】\n{skill.guide('chapter-guide')[:3500]}")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg)

def gen_titles(cfg, n=3):
    sys_p = (f"执行 chinese-novelist 技能的标题生成：参考 title-guide.md，基于故事元素生成 {n} 个候选标题，"
             "每个使用不同的创作技巧，附一句话说明。输出 JSON：{\"titles\":[{\"title\":..,\"technique\":..,\"why\":..}]}")
    user_p = (f"故事元素：{json.dumps({k: cfg.get(k) for k in ('genre','idea','conflict','protagonist','theme','tone')}, ensure_ascii=False)}\n"
              f"【标题技巧摘录】\n{skill.guide('title-guide')[:2500]}")
    out = llm.chat_json([{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}], cfg=cfg)
    return out.get("titles", [])

# ---------- 阶段3：逐章创作 ----------

def _writing_system(ctx):
    """写作系统提示：黄金法则 + 技能指南摘录（运行时读技能仓库）"""
    parts = [
        "你是执行 chinese-novelist 技能写作流水线（phase3-writing.md）的专业中文小说家。",
        "三大黄金法则：\n" + skill.golden_rules(),
        "【章纲写作规范摘录】\n" + skill.guide("chapter-guide")[:2500],
        "【钩子技法摘录】\n" + skill.guide("hook-techniques")[:2500],
        "【对话写作规范摘录】\n" + skill.guide("dialogue-writing")[:2000],
        "【扩充技法（字数不足时用）】\n" + skill.guide("content-expansion")[:1800],
        "硬性要求：正文 3000-5000 汉字；章首引子 50-150 字；对话占比 30%+；全章≥2个张力波峰；"
        "≥1个意外转折；结尾必留悬念钩子；严守人物性格一致性；动笔前先消化上一章结尾语态。",
        "输出格式：按技能的 chapter-template.md 结构输出（## 本章概要 / ## 章首引子 / ## 正文 / ## 章节备注）。",
    ]
    return "\n\n".join(parts)

def _continuity_block(ctx):
    """把 v3.0 连续性结构（故事圣经/线索账本/章节摘要/时间线）拼成写前上下文；旧项目缺失则自动省略。"""
    parts = []
    if ctx.get("story_bible"):
        parts.append(f"【故事圣经（事实来源）】\n{ctx['story_bible'][:2000]}")
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

def write_chapter(ctx):
    cont = _continuity_block(ctx)
    if ctx.get("continuation"):
        user_p = (
            f"【本书文风基准】\n{ctx['style_text']}\n"
            f"【人物档案】\n{ctx['characters_text']}\n"
            f"【全书大纲要点】\n{ctx.get('outline_text','')[:3000]}\n"
            f"【上一章结尾】\n{ctx.get('prev_tail') or '（无）'}\n"
            f"{cont}\n"
            f"现在续写第 {ctx['chapter_no']} 章。这是承接前文的续写章：请严格消化上一章结尾的语态与悬念，"
            f"自然推进剧情，可引入新的转折/冲突/角色（须符合人物性格与既定设定），严禁重复已写情节；"
            f"严守人物性格一致性，结尾必留钩子。输出章节文件内容。"
        )
        return llm.chat([{"role": "system", "content": _writing_system(ctx)},
                         {"role": "user", "content": user_p}], cfg=ctx.get("cfg"))
    user_p = (
        f"【本书文风基准】\n{ctx['style_text']}\n"
        f"【人物档案】\n{ctx['characters_text']}\n"
        f"【大纲】\n{ctx['outline_text']}\n"
        f"【上一章结尾】\n{ctx.get('prev_tail') or '（第1章，无）'}\n"
        f"{cont}\n"
        f"现在写第 {ctx['chapter_no']} 章「{ctx['chapter_title']}」。严格按该章在大纲中的规划（核心事件/承接/钩子/出场人物/场景）创作。"
        f"输出章节文件内容。"
    )
    return llm.chat([{"role": "system", "content": _writing_system(ctx)},
                     {"role": "user", "content": user_p}], cfg=ctx.get("cfg"))

def expand_chapter(body, ctx):
    user_p = (f"下面这章字数不足 3000，请按 content-expansion.md 的扩充技巧（增加对话场景/人物内心/细节描写）"
              f"扩写到 3000-5000 字，保持情节与钩子不变，不要注水。\n\n{body}")
    return llm.chat([{"role": "system", "content": _writing_system(ctx)},
                     {"role": "user", "content": user_p}], cfg=ctx.get("cfg"))

def chapter_meta(body, ctx):
    sys_p = ("从章节正文提取：summary(300-500字本章摘要，供下章承接)、new_terms(本章首次出现的设定名词列表，"
             "每项 {name, known, truth}，无则空数组)、hook(结尾钩子一句话)。只输出 JSON。")
    return llm.chat_json([{"role": "system", "content": sys_p},
                          {"role": "user", "content": f"章节：第{ctx['chapter_no']}章 {ctx['chapter_title']}\n{body}"}],
                         cfg=ctx.get("cfg"))

def style_calibrate(ch1_body, cfg=None):
    sys_p = ("从第1章定稿中选出 1-2 个最佳段落（开头钩子段+高张力段，共600-1000字原文照录），"
             "作为本书校准段落。只输出选中的原文段落，不要评论。")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": ch1_body[:12000]}], cfg=cfg)

# ---------- 大纲解析 ----------

def outline_chapter_row(outline_md, no):
    """从大纲里抠出第 no 章的规划段落（表格行 + 详情小节）"""
    m = re.search(rf"第{no}章[^\n]*\n(.*?)(?=\n第{no+1}章|\n##|\Z)", outline_md, re.S)
    row = ""
    if m:
        row = m.group(0).strip()
    return row or outline_md[-3000:]

# ---------- 阶段2.5：v3.0 连续性结构（故事圣经 / 线索账本 / 时间线） ----------

def gen_story_bible(cfg):
    """故事圣经：全书唯一事实来源（引擎/承诺/主题/世界规则/视角底线/主线结局/设定词典）。"""
    sys_p = ("执行 chinese-novelist v3.0 的『故事圣经』：建立全书唯一事实来源。用 story-bible 模板，"
             "填写 故事引擎(一句话) / 故事承诺(题材·卖点·情绪·兑现方式) / 主题 / 世界规则与系统边界 / "
             "叙事视角与风格底线 / 总主线与结局方向 / 设定词典。内容须具体可执行，能直接指导后续写作与校验。"
             "只输出 markdown 正文。")
    user_p = (f"小说配置：{json.dumps(cfg, ensure_ascii=False)}\n"
              f"【模板】\n{skill.template('story-bible')[:1400]}")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg)

def gen_clue_ledger(cfg, outline_md):
    """线索账本：从大纲提取重要悬念/伏笔，登记为可追踪表（含真实答案与兑现计划）。"""
    sys_p = ("执行 v3.0『线索账本』：从大纲中提取所有服务主线的悬念/伏笔，登记为线索表"
             "（线索ID/首次出现/初始表象/真实含义/可推理证据/误导方式/计划兑现/状态）。"
             "只登记真正重要的线索（控制 3-8 条），每条都要有明确兑现计划与作者掌握的真实答案。"
             "只输出 markdown 表 + 简短纪律说明。")
    user_p = (f"小说配置：{json.dumps(cfg, ensure_ascii=False)}\n"
              f"【大纲】\n{outline_md[:4000]}\n【模板】\n{skill.template('clue-ledger')[:900]}")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg)

def gen_timeline(cfg, outline_md):
    """时间线与连续性：全书事件时间线 + 连续性核对清单（每章写前必查）。"""
    sys_p = ("执行 v3.0『时间线与连续性』：建立全书事件时间线（章节/时间/地点/事件/持续/先后）与"
             "连续性核对清单（人物身份·知情范围·伤势·物品·世界规则·后果）。用于每章写前核对前后一致。"
             "只输出 markdown。")
    user_p = (f"小说配置：{json.dumps(cfg, ensure_ascii=False)}\n"
              f"【大纲】\n{outline_md[:4000]}\n【模板】\n{skill.template('timeline')[:900]}")
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=cfg)

# ---------- 阶段4.5：分层编辑审稿（v3.0 §11，单次结构化覆盖六轮） ----------

def layered_review(book_md, cfg):
    """单次 LLM 覆盖六轮审稿（结构/因果/人物/连续性/语言/读者体验）→ JSON。
    每项结论须给文本证据（引用原句+章节），无证据不判问题。autofix 由调用方对 high 项做有界重写。"""
    sys_p = ("你是执行 chinese-novelist v3.0 分层编辑的主审。对全书做六轮审稿：结构 / 因果与智斗 / "
             "人物与情绪 / 连续性 / 语言与人味 / 读者体验。每条结论必须给文本证据（引用原句+所在章），"
             "无证据不得判为问题。输出 JSON：{\"scores\":{\"开篇吸引\":n,\"因果可信\":n,\"人物辨识度\":n,"
             "\"冲突节奏\":n,\"情绪关系\":n,\"连续性\":n,\"语言自然\":n,\"原创辨识度\":n,\"主题结局兑现\":n},"
             "\"findings\":[{\"chapter\":N或\"全书\",\"dim\":\"结构/因果/人物/连续性/语言/读者体验\","
             "\"severity\":\"high|mid|low\",\"evidence\":\"原句引用\",\"problem\":\"问题\",\"fix\":\"建议\"}],"
             "\"fix_actions\":[{\"chapter\":N,\"action\":\"具体可执行的局部重写指令\"}]}。"
             "fix_actions 只放 severity=high 且能定位到章节的（最多 6 条）。只输出 JSON。")
    user_p = f"全书（节选）：\n{book_md[:30000]}"
    return llm.chat_json([{"role": "system", "content": sys_p},
                          {"role": "user", "content": user_p}], cfg=cfg)

def fix_chapter(ch_body, action, ctx):
    """按 fix_action 对单章做局部重写（保留章题/引子/正文结构，只修被指出的问题，不注水不改情节走向）。"""
    sys_p = "你执行 chinese-novelist v3.0 的分层修复。只按下述指令局部修改本章，保留原情节走向、钩子与字数量级，" \
            "不注水、不改核心设定。输出整章修正后的 markdown（含 ## 本章概要/## 章首引子/## 正文/## 章节备注 结构）。"
    user_p = f"修复指令：{action}\n\n待修章节：\n{ch_body[:9000]}"
    return llm.chat([{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}], cfg=ctx.get("cfg"))
