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

def write_chapter(ctx):
    user_p = (
        f"【本书文风基准】\n{ctx['style_text']}\n"
        f"【人物档案】\n{ctx['characters_text']}\n"
        f"【大纲】\n{ctx['outline_text']}\n"
        f"【上一章结尾】\n{ctx.get('prev_tail') or '（第1章，无）'}\n"
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
