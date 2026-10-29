"""文体注册表（v1.0.0 · 神笔文学创作台）

一个「文体」= 一张阶段表 + 一组评估维度 + 一套目录规范 + 一组角色。
这里定义全部四类文体的元数据，供 skillkit 注入、流水线调度、前端分流共用。

新增文体只需在 MEDIUMS 里加一条；流水线实现放 bot/<medium>_writer.py。
"""
from __future__ import annotations

# ---------- 阶段代号 ----------
# novel  P0..P5
# script S0..S7（长片/短片/舞台） X0..X6（板腔体） Q0..Q6（曲牌体）
# poetry I P C K R F
# prose  B S D A P F

MEDIUMS: dict[str, dict] = {
    "novel": {
        "label": "小说",
        "icon": "📖",
        "desc": "长篇连载 / 短篇 / 网文，逐章推进、伏笔回收、连续性核对",
        "dir_prefix": "",
        "stages": [
            ("P0", "规划", "人物档案 / 故事引擎 / 大纲 / 文风基准 / 故事圣经 / 线索账本 / 时间线"),
            ("P1", "黄金三章门", "前 3 章单独过「开篇是否立住」"),
            ("P2", "逐章创作", "写前注入上下文，写后补章节摘要 + 线索账本"),
            ("P3", "连续性检查", "L1 确定性规则：伏笔闲置 / 超期未兑现 / 主角失踪 / 字数 / 爽点"),
            ("P4", "五角色审校", "Architect / Scribe / Editor / Guardian / Curator"),
            ("P5", "百分制质检", "六维加权评分，达标线 90"),
        ],
        "score_dims": [
            ("结构", 20), ("因果可信", 20), ("人物", 15),
            ("连续性", 20), ("语言", 15), ("读者体验", 10),
        ],
        "pass_line": 90,
        "agents": ["planner", "writer", "reviewer"],
        "checker": None,
        "live": "/api/book-live",
    },
    "script": {
        "label": "剧本",
        "icon": "🎬",
        "desc": "电影长片 / 电视剧集 / 短片 / 舞台剧 / 戏曲，真人拍摄，不做 AI 视频",
        "dir_prefix": "s_",
        "stages": [
            ("S0", "引擎", "核心前提 / 主题 / 控制思想 / logline / 故事引擎"),
            ("S1", "人物网与季弧", "主要人物 / 关系网 / 对手设计 / 冲突编排"),
            ("S2", "文档", "故事圣经 / 人物小传 / 世界设定 / 剧集圣经"),
            ("S3", "试播结构", "单集结构 / 幕点 / 冷开场 / 集尾钩子"),
            ("S4", "分集大纲", "逐集大纲 / 场次清单 / 集数弧线"),
            ("S5", "写作", "按场次写正文，含场景头、动作行、台词、字幕"),
            ("S6", "修订", "通读修订 / 删改记录 / 场景级打磨"),
            ("S7", "交付", "定稿导出（按行业格式）"),
        ],
        "score_dims": [
            ("前提", 25), ("结构", 25), ("人物", 20),
            ("对白", 15), ("格式", 10), ("可行性", 5),
        ],
        "pass_line": 85,
        "agents": ["script_planner", "script_writer", "script_reviewer"],
        "checker": "bot.script_checker",
        "live": "/api/script-live",
    },
    "poetry": {
        "label": "诗词",
        "icon": "🖋",
        "desc": "近体诗（四体）/ 词（词牌）/ 曲（曲牌）/ 现代诗，附确定性格律校验",
        "dir_prefix": "p_",
        "stages": [
            ("I", "释读", "理解题面 / 判定体式 / 锁定情感核心与场景"),
            ("P", "规划", "意象表 / 章法 / 韵部 / 起承转合设计"),
            ("C", "起草", "按平仄与句读落笔"),
            ("K", "校验", "确定性平仄/押韵/对仗检查（pass / fail / unclear）"),
            ("R", "评改", "按校验结果与审美标准改稿，最多 3 轮"),
            ("F", "定稿", "定稿 + 注释 / 译文（如需）"),
        ],
        "score_dims": [("意象", 30), ("章法", 25), ("声律", 25), ("情感", 20)],
        "pass_line": 80,
        "agents": ["poet_planner", "poet_writer", "poet_reviewer"],
        "checker": "bot.poetry_checker",
        "live": None,
    },
    "prose": {
        "label": "散文",
        "icon": "📄",
        "desc": "随笔 / 抒情散文 / 议论 / 叙事散文，四操作：写 / 审 / 改 / 重写",
        "dir_prefix": "e_",
        "stages": [
            ("B", "创作简报", "体裁 / 受众 / 目的 / 语气 / 约束 / 事实清单"),
            ("S", "素材", "素材收集 / 细节库 / 事实核验 / 缺口标记"),
            ("D", "起草", "先收集实质再落笔，缺事实留缺口标记"),
            ("A", "去味", "三层去 AI 腔诊断：表达 / 用笔 / 故事逻辑"),
            ("P", "精修", "结构 / 音律 / 事实 / 语气打磨 + preflight 终检"),
            ("F", "定稿", "定稿 + 改动说明"),
        ],
        "score_dims": [("表达", 30), ("结构", 25), ("真实度", 25), ("音律", 20)],
        "pass_line": 80,
        "agents": ["prose_planner", "prose_writer", "prose_reviewer"],
        "checker": "bot.slop_checker",
        "live": None,
    },
    # v4.6.0：论文文体（提纲 → 分章撰写 → 引用管理 → 降 AIGC 润色 → 定稿）
    "thesis": {
        "label": "论文",
        "icon": "🎓",
        "desc": "学术论文：提纲 / 分章撰写 / 引用文献管理 / 降 AIGC 润色",
        "dir_prefix": "t_",
        "stages": [
            ("T1", "提纲", "选题 / 摘要 / 章节结构 / 引用池"),
            ("T2", "撰写", "分章撰写：引言/综述/方法/分析/结论"),
            ("T3", "引用", "引用核查：正文引注 ↔ 文献表一一对应"),
            ("T4", "降味", "降 AIGC 润色：句式节奏/连接词多样化"),
            ("T5", "定稿", "参考文献定稿 + 查重报告"),
        ],
        "score_dims": [("学术", 30), ("结构", 25), ("引用", 25), ("表达", 20)],
        "pass_line": 80,
        "agents": ["thesis_writer"],
        "checker": None,
        "live": None,
    },
    # v4.10.0：儿童文学（分级语言 / 互动设计 / 插图提示词）
    "children": {
        "label": "儿童文学",
        "icon": "🧸",
        "desc": "寓言 / 绘本故事 / 儿童科幻 / 科学故事，按年龄段分级创作",
        "dir_prefix": "c_",
        "stages": [
            ("C1", "选题分级", "年龄段 / 主题 / 价值观红线"),
            ("C2", "故事", "按故事单元创作，语言分级"),
            ("C3", "互动", "亲子问题 / 知识点 / 安全提示"),
            ("C4", "插图", "插图提示词（每单元 2-4 张）"),
            ("C5", "定稿", "定稿 + 朗读适配"),
        ],
        "score_dims": [("童趣", 30), ("分级", 25), ("正面引导", 25), ("结构", 20)],
        "pass_line": 80,
        "agents": ["children_writer"],
        "checker": None,
        "live": None,
    },
    # v4.11.0：新闻稿（倒金字塔 / 事实核查 / 多版本）
    "news": {
        "label": "新闻稿",
        "icon": "📰",
        "desc": "产品发布 / 官方公告 / 活动通稿 / 企业动态，倒金字塔结构",
        "dir_prefix": "n_",
        "stages": [
            ("N1", "素材", "素材与信源整理，5W1H 校验"),
            ("N2", "撰写", "倒金字塔：标题/导语/主体/引语/背景"),
            ("N3", "核查", "时间/数字/人名/引语归属一致性"),
            ("N4", "多版本", "通稿版 / 社媒短版 / 标题候选"),
            ("N5", "定稿", "定稿 + 发布清单"),
        ],
        "score_dims": [("准确", 30), ("结构", 25), ("新闻语体", 25), ("传播力", 20)],
        "pass_line": 80,
        "agents": ["news_writer"],
        "checker": None,
        "live": None,
    },
    # v4.12.0：文案创作（口播 / 种草 / 宣传 / 营销）
    "copy": {
        "label": "文案",
        "icon": "📣",
        "desc": "口播稿 / 种草笔记 / 宣传文案 / 营销文案，钩子库 + 平台适配",
        "dir_prefix": "k_",
        "stages": [
            ("K1", "需求", "平台 / 受众 / 卖点 / 钩子方向"),
            ("K2", "钩子", "前 3 秒钩子 10 候选"),
            ("K3", "正文", "口播稿（停顿/重音标注）或图文文案"),
            ("K4", "标题", "标题与标签候选"),
            ("K5", "适配", "多平台版本适配 + 定稿"),
        ],
        "score_dims": [("钩子", 30), ("转化力", 25), ("平台适配", 25), ("表达", 20)],
        "pass_line": 80,
        "agents": ["copy_writer"],
        "checker": None,
        "live": None,
    },
}

# 别名：用户口语 → 文体 key
_ALIASES: list[tuple[tuple[str, ...], str]] = [
    (("剧本", "影视", "话剧", "电影", "电视剧", "短剧", "微电影", "戏曲", "screenplay", "script"), "script"),
    (("诗词", "诗", "词", "曲", "古诗", "现代诗", "律诗", "绝句", "poem", "poetry"), "poetry"),
    (("散文", "随笔", "议论文", "抒情", "美文", "essay", "prose"), "prose"),
    (("小说", "网文", "长篇", "短篇", "连载", "novel", "fiction"), "novel"),
]

# 强信号词：单独出现即可判定文体
_STRONG: dict[str, tuple[str, ...]] = {
    "poetry": ("七律", "五律", "七绝", "五绝", "律诗", "绝句", "词牌", "格律",
               "平水韵", "词林正韵", "楹联", "押韵", "填词", "水调歌头", "念奴娇",
               "沁园春", "满江红", "渔家傲", "水仙子", "西江月", "如梦令",
               "鹧鸪天", "定风波", "虞美人", "蝶恋花", "临江仙", "唐诗", "宋词", "元曲"),
    "script": ("分集", "场次", "台词", "人物小传", "故事梗概", "舞台剧", "京剧", "越剧",
               "昆曲", "豫剧", "川剧", "评剧", "黄梅戏", "话剧本", "分场", "独幕",
               "剧集", "剧集大纲", "试播集", "第一集", "分集梗概"),
}

# 文体识别优先级：剧本 > 诗词 > 散文 > 小说（“写一个悬疑小说”要判成小说）
_PRIORITY = ["script", "poetry", "prose", "novel"]


def normalize(name: str) -> str:
    """把任意名字/别名归一到 medium key；认不出返回 ''。"""
    s = (name or "").strip().lower()
    if s in MEDIUMS:
        return s
    for keys, val in _ALIASES:
        for k in keys:
            if k in s:
                return val
    return ""


def detect(text: str) -> str:
    """从一段用户输入里判定文体；判定不出返回 ''（由调用方决定追问或走默认）。

    规则：命中越多关键词的文体得分越高；同分时按 _PRIORITY 顺序。
    """
    s = (text or "").lower()
    if not s.strip():
        return ""
    score: dict[str, int] = {}
    for keys, val in _ALIASES:
        for k in keys:
            if k in s:
                score[val] = score.get(val, 0) + 1
    for verb, val in (("剧本", "script"), ("话剧", "script"), ("分集", "script"),
                      ("场次", "script"), ("台词", "script"), ("分镜", "script"),
                      ("故事梗概", "script"), ("人物小传", "script"),
                      ("七律", "poetry"), ("五律", "poetry"), ("七绝", "poetry"), ("五绝", "poetry"),
                      ("律诗", "poetry"), ("绝句", "poetry"), ("词牌", "poetry"),
                      ("格律", "poetry"), ("押韵", "poetry"), ("平水韵", "poetry"),
                      ("楹联", "poetry"), ("咏", "poetry"),
                      ("散文", "prose"), ("随笔", "prose"), ("议论", "prose"),
                      ("小说", "novel"), ("章节", "novel"), ("网文", "novel"),
                      ("连载", "novel")):
        if verb in s:
            score[val] = score.get(val, 0) + 1
    # 特化体式词（七律/五绝/词牌/剧种…）是强信号：直接把该文体顶到并列第一
    for val, words in _STRONG.items():
        for w in words:
            if w in s:
                score[val] = score.get(val, 0) + 3
    if not score:
        return ""
    best = max(score.values())
    winners = [m for m in _PRIORITY if score.get(m, 0) == best]
    return winners[0] if winners else ""


def stages(medium: str) -> list[tuple[str, str, str]]:
    """阶段表：[(代号, 名称, 说明), ...]"""
    return list(MEDIUMS.get(medium, {}).get("stages", []))


def stage_names(medium: str) -> dict[str, str]:
    return {c: n for c, n, _ in stages(medium)}


def stage_desc(medium: str, code: str) -> str:
    for c, _n, d in stages(medium):
        if c == code:
            return d
    return ""


def all_stage_codes(medium: str) -> list[str]:
    return [c for c, _n, _d in stages(medium)]


def score_dims(medium: str) -> list[tuple[str, int]]:
    return list(MEDIUMS.get(medium, {}).get("score_dims", []))


def pass_line(medium: str) -> int:
    return int(MEDIUMS.get(medium, {}).get("pass_line", 90))


def dir_prefix(medium: str) -> str:
    return MEDIUMS.get(medium, {}).get("dir_prefix", "")


def agents(medium: str) -> list[str]:
    return list(MEDIUMS.get(medium, {}).get("agents", []))


def label(medium: str) -> str:
    return MEDIUMS.get(medium, {}).get("label", medium or "未选")


def icon(medium: str) -> str:
    return MEDIUMS.get(medium, {}).get("icon", "📄")


def desc(medium: str) -> str:
    return MEDIUMS.get(medium, {}).get("desc", "")


def is_medium(name: str) -> bool:
    return name in MEDIUMS


# ---------- v2.1：开书向导字段 schema（/api/onboarding.by_medium 的唯一事实源）----------
# 三段对应向导三屏：brief=第1屏（体裁与模式）、basic=第2屏（基本信息）、style=第3屏（参考与风格）。
# 字段：{key,label,type,options,default,required,hint,min,max,unit,when,options_from}
#   type: select | radio | scale | number | text | textarea | chips | toggle | note
#   scale = 小圆点快捷项 + 末项「自定义」（选中自定义才露出 customKey 数字框，fill 决定快捷项的数值）
#   when  = {"field": "form", "in": ["ci"]} → 只在指定取值下出现
#   options_from = 词表名，由 wizard_payload() 从调用方注入（题材/基调等词表仍归 app 与引擎所有）


def _f(key: str, label: str, ftype: str, **kw) -> dict:
    field = {"key": key, "label": label, "type": ftype}
    field.update(kw)
    return field


def _opts(pairs) -> list[dict]:
    out = []
    for item in pairs:
        if isinstance(item, str):
            out.append({"value": item, "label": item})
        elif isinstance(item, dict):
            out.append(item)
        else:
            out.append({"value": item[0], "label": item[1]})
    return out


# 小说：题材一律给出 skillkit 的 craft key（skillkit._genre_key 实测结果），避免静默回落 _index。
_NOVEL_TONES = ["爽文", "虐", "轻松", "暗黑", "复仇", "燃"]
_NOVEL_PERSPECTIVES = ["男主", "女主", "不限", "第一人称"]
_NOVEL_MODES = (
    {"value": "rolling", "label": "滚动规划", "desc": "写一段、看反馈、再规划，适合连载。", "icon": "refresh"},
    {"value": "outline", "label": "大纲优先", "desc": "先定全书骨架，再逐章落地。", "icon": "list"},
    {"value": "one_shot", "label": "一次成稿", "desc": "适合短篇，一次生成再统一打磨。", "icon": "zap"},
)
# 规模快捷项：fill = 该档的章数目标（与前端 DEFAULT_CHAPTERS、longform.resolve_scale 一致）
_NOVEL_SCALE = (
    {"value": "short", "label": "短篇", "desc": "约 30 万字", "fill": 100},
    {"value": "1m", "label": "百万字", "desc": "约 100 万字", "fill": 300},
    {"value": "3m", "label": "三百万字", "desc": "约 300 万字", "fill": 900},
    {"value": "5m", "label": "五百万字", "desc": "约 500 万字", "fill": 1500},
)

_NOVEL_SECTIONS = {
    "style_title": "卖点与风格预设",
    "brief": [
        _f("platform", "发布平台", "chips", options_from="platforms", default="", effect="platform",
           hint="选平台带入该站的分类习惯与更新节奏，创作不受限制。"),
        _f("genre", "题材", "select", options_from="genres", default="悬疑/刑侦", required=True,
           hint="题材决定注入哪套题材 craft（悬疑/言情/恐怖/玄幻…），之后仍可微调。"),
        _f("tone", "基调", "select", options_from="tones", default="",
           hint="留空表示不限。基调与题材共同决定文风预设。"),
        _f("perspective", "叙事视角", "select", options_from="perspectives", default=""),
        _f("mode", "创作模式", "cards", options=_NOVEL_MODES, default="rolling", required=True),
    ],
    "basic": [
        _f("title", "书名", "text", max_length=40,
           hint="留空则由系统生成 8 个候选，创建后可随时换。"),
        _f("idea", "一句话 premise", "textarea", rows=4, max_length=2000, required=True,
           hint="写清「谁 / 想要什么 / 被什么挡住」最容易得到可用的规划。"),
        _f("scale", "规模", "scale", options=_NOVEL_SCALE, default="1m", required=True,
           custom_key="total_chapters", min=1, max=2000, unit="章",
           hint="章节数作为规划目标，写作过程中仍可扩展。"),
        _f("min_words_per_chapter", "每章字数", "number", default=3000, min=500, max=10000, unit="字",
           hint="单章目标字数（写入 minWordsPerChapter）；网文通常 2000-4000。"),
    ],
    "style": [
        _f("sells", "卖点", "chips", options_from="sells", multi=True,
           hint="卖点写进风格技能，正文按这些爽点节拍安排冲突。"),
        _f("style_preset", "风格预设", "chips", options_from="style_presets", default="",
           hint="跟随题材表示按上面选定的题材自动取预设。"),
        _f("style_preview", "本次生效", "preview",
           hint="派生自题材与平台预设，只读；创建后仍可在作品规划里调整。"),
    ],
}

# v4.6.0：论文向导（学历/学科/字数/引用格式；引擎 bot/thesis_writer.py）
_THESIS_WIZARD = {
    "style_title": "引用与降味",
    "brief": [
        _f("degree", "层次", "select", options=_opts([
            ("undergrad", "本科毕业论文"), ("master", "硕士论文"), ("journal", "期刊论文"), ("report", "课程报告")]),
           default="undergrad", required=True),
        _f("field", "学科方向", "text", max_length=30, required=True,
           hint="例如：计算机 / 汉语言文学 / 市场营销"),
    ],
    "basic": [
        _f("title", "论文题目", "text", max_length=60,
           hint="留空则由引擎基于学科方向出选题候选。"),
        _f("idea", "研究要点", "textarea", rows=4, max_length=2000,
           hint="研究问题 / 方法 / 预期结论；写得越具体，提纲越可用。"),
        _f("words_scale", "目标字数", "scale",
           options=[{"value": "8k", "label": "8千字", "fill": 8000},
                    {"value": "15k", "label": "1.5万字", "fill": 15000},
                    {"value": "30k", "label": "3万字", "fill": 30000}],
           custom_key="target_words", min=3000, max=100000, unit="字", default="8k", required=True),
    ],
    "style": [
        _f("cite_style", "引用格式", "select", options=_opts([
            ("gb", "GB/T 7714（国标）"), ("apa", "APA"), ("mla", "MLA")]), default="gb"),
        _f("humanize", "降 AIGC 润色", "toggle", default=True,
           hint="定稿前逐章拟人化改写，降低 AI 检测率并输出降味报告。"),
    ],
}

# v4.10-4.12：儿童文学 / 新闻稿 / 文案向导
_CHILDREN_WIZARD = {
    "style_title": "分级与安全",
    "brief": [
        _f("ctype", "类型", "select", options=_opts([
            ("fable", "寓言故事"), ("picture", "绘本故事"), ("scifi", "儿童科幻"),
            ("science", "科学故事"), ("bedtime", "睡前故事")]), default="fable", required=True),
        _f("age", "年龄段", "select", options=_opts([
            ("3-6", "3-6 岁（学龄前）"), ("7-9", "7-9 岁（小学低年级）"),
            ("10-12", "10-12 岁（小学高年级）")]), default="7-9", required=True),
    ],
    "basic": [
        _f("title", "故事标题", "text", max_length=40, hint="留空则由引擎出候选。"),
        _f("idea", "故事要点", "textarea", rows=4, max_length=2000,
           hint="主角 / 想要什么 / 学到什么；价值观红线（不能出现的内容）也写在这里。"),
        _f("words_scale", "单篇字数", "scale",
           options=[{"value": "800", "label": "800字", "fill": 800},
                    {"value": "1500", "label": "1500字", "fill": 1500},
                    {"value": "3000", "label": "3000字", "fill": 3000}],
           custom_key="target_words", min=300, max=20000, unit="字", default="1500", required=True),
    ],
    "style": [
        _f("values_note", "价值观红线", "textarea", rows=2, max_length=500,
           hint="必守：如「不说教」「无暴力冲突」「主角不撒谎」等。"),
        _f("with_interactive", "亲子互动设计", "toggle", default=True,
           hint="每个故事单元附亲子讨论问题与知识点。"),
        _f("with_illustration", "插图提示词", "toggle", default=True,
           hint="每个故事单元输出 2-4 张插画描述（可转绘图提示词）。"),
    ],
}
_NEWS_WIZARD = {
    "style_title": "发布口径",
    "brief": [
        _f("ntype", "类型", "select", options=_opts([
            ("product", "产品/新品发布"), ("official", "官方公告"), ("event", "活动通稿"),
            ("corporate", "企业动态"), ("honor", "获奖/荣誉")]), default="product", required=True),
        _f("org", "发布主体", "text", max_length=40, required=True,
           hint="公司 / 机构 / 品牌名"),
    ],
    "basic": [
        _f("title", "新闻标题", "text", max_length=60, hint="留空则由引擎出候选。"),
        _f("idea", "核心事实", "textarea", rows=5, max_length=3000, required=True,
           hint="时间/地点/人物/事件/数据/引语——事实越完整，稿子越可用。"),
    ],
    "style": [
        _f("cite_style", "口径风格", "select", options=_opts([
            ("formal", "官方正式"), ("tech", "科技媒体"), ("warm", "温度感/人文"),
            ("finance", "财经严谨")]), default="formal"),
        _f("with_versions", "多版本输出", "toggle", default=True,
           hint="通稿版 + 社媒短版 + 标题候选。"),
    ],
}
_COPY_WIZARD = {
    "style_title": "平台与钩子",
    "brief": [
        _f("ktype", "文案类型", "select", options=_opts([
            ("voice", "口播稿"), ("planting", "种草笔记"), ("promo", "宣传文案"),
            ("marketing", "营销长文")]), default="voice", required=True),
        _f("platform", "投放平台", "select", options=_opts([
            ("douyin", "抖音"), ("xhs", "小红书"), ("gzh", "公众号"),
            ("bili", "B站"), ("shipinhao", "视频号")]), default="douyin", required=True),
    ],
    "basic": [
        _f("title", "主题/产品", "text", max_length=60, required=True),
        _f("idea", "卖点与受众", "textarea", rows=4, max_length=2000, required=True,
           hint="卖什么 / 卖给谁 / 核心卖点 / 竞品差异。"),
        _f("duration", "口播时长", "scale",
           options=[{"value": "30", "label": "30秒", "fill": 30},
                    {"value": "60", "label": "60秒", "fill": 60},
                    {"value": "180", "label": "3分钟", "fill": 180}],
           custom_key="duration_secs", min=15, max=600, unit="秒", default="60",
           when={"field": "ktype", "in": ["voice"]}),
    ],
    "style": [
        _f("tone", "语气", "select", options=_opts([
            ("energetic", "活力种草"), ("professional", "专业可信"),
            ("humorous", "幽默玩梗"), ("warm", "温暖治愈")]), default="energetic"),
        _f("with_hooks", "钩子候选", "toggle", default=True,
           hint="前 3 秒钩子 10 候选（悬念/反差/痛点/数字/热点借势）。"),
    ],
}

# 剧本：引擎真实消费 title/idea/form/entryPath/scenes/pages/episodes/notes（见 bot/script_writer.py）。
_SCRIPT_GENRES = ["悬疑/犯罪", "古装/历史", "都市/现实", "科幻", "战争/军事",
                  "喜剧", "爱情", "家庭/伦理", "武侠/仙侠", "恐怖/灵异"]
# 时代/世界观是硬约束：写进场次与正文的每一次生成，并由 bot/script_checker 做词表校验。
_ERA_OPTIONS = _opts([("auto", "跟随题材/素材（推荐）"), ("ancient", "古代"), ("republic", "民国"),
                      ("modern", "当代"), ("future", "近未来"), ("fantasy", "架空"), ("custom", "自定义")])
_SCRIPT_SCALE_SCENES = (
    {"value": "s20", "label": "短片体量", "desc": "约 20 场", "fill": 20},
    {"value": "s30", "label": "长片标准", "desc": "约 30 场", "fill": 30},
    {"value": "s45", "label": "长片偏长", "desc": "约 45 场", "fill": 45},
    {"value": "s60", "label": "多幕/戏曲", "desc": "约 60 场", "fill": 60},
)
_SCRIPT_SCALE_PAGES = (
    {"value": "p60", "label": "精简", "desc": "约 60 页", "fill": 60},
    {"value": "p90", "label": "长片下限", "desc": "约 90 页", "fill": 90},
    {"value": "p100", "label": "行业标准", "desc": "约 100 页", "fill": 100},
    {"value": "p120", "label": "偏长", "desc": "约 120 页", "fill": 120},
)
_SCRIPT_SCALE_EPISODES = (
    {"value": "e6", "label": "短季", "desc": "6 集", "fill": 6},
    {"value": "e12", "label": "一季", "desc": "12 集", "fill": 12},
    {"value": "e16", "label": "长季", "desc": "16 集", "fill": 16},
    {"value": "e24", "label": "季播", "desc": "24 集", "fill": 24},
)

_SCRIPT_SECTIONS = {
    "style_title": "格式与交付",
    "brief": [
        _f("form", "媒介形式", "select", options_from="script_forms", default="film", required=True,
           hint="形式决定页数/时长目标与结构模板；戏曲走板腔体或曲牌体阶段表。"),
        _f("entryPath", "入口路径", "radio", options_from="script_entry_paths", default="idea",
           required=True, hint="决定第一遍是搭引擎还是先读材料。"),
        _f("genre", "类型", "select", options=_SCRIPT_GENRES, default="悬疑/犯罪",
           hint="类型契约会影响引擎与观众预期两条线。"),
    ],
    "basic": [
        _f("title", "剧名", "text", max_length=40, hint="留空则由系统按素材生成。"),
        _f("setting_era", "时代", "select", options=_ERA_OPTIONS, default="auto",
           hint="贯穿场次与正文的硬约束：地点、道具、称谓、交通都必须落在该时代；跟随素材时由题材与标题推断。"),
        _f("setting_world", "世界观与考据约束", "textarea", rows=2, max_length=600,
           hint="一句话写清世界规则与必须考据的细节；会作为硬约束贯穿场次与正文。"),
        _f("idea", "logline / 素材", "textarea", rows=4, max_length=4000, required=True,
           hint="有原著就写清原著与想保留的内核；只有想法就写「谁 / 想要什么 / 被什么挡住」。"),
        # scale 的 token 槽位（*_scale）只给前端选中态用；数字落引擎 key（custom_key）。
        # 两者绝不能同名：否则值袋里 token 与数字互相覆盖，选中态与校验都会错。
        _f("episodes_scale", "集数", "scale", options=_SCRIPT_SCALE_EPISODES, default="e12",
           custom_key="episodes", min=1, max=200, unit="集",
           when={"field": "form", "in": ["series"]}),
        _f("scenes_scale", "目标场次", "scale", options=_SCRIPT_SCALE_SCENES, default="s30",
           custom_key="scenes", min=1, max=1000, unit="场",
           hint="场次清单按这个规模生成，之后可增删。"),
        _f("pages_scale", "目标页数", "scale", options=_SCRIPT_SCALE_PAGES, default="p100",
           custom_key="pages", min=5, max=600, unit="页",
           hint="行业惯例约 1 页 ≈ 1 分钟银幕时间。"),
        _f("notes", "创作备注", "textarea", rows=3, max_length=2000,
           hint="会原样进入引擎生成提示：想避开的套路、必须出现的场面、尺度边界。"),
    ],
    "style": [
        _f("format_spec", "格式规范", "chips",
           options=_opts(["标准场景头", "动作行现在时", "台词居中", "字幕/画外音标注", "分页每场一页"]),
           default=["标准场景头", "动作行现在时"], multi=True,
           hint="交付格式要求；导出与修订阶段按此核对。"),
        _f("page_note", "页数换算", "note",
           text="电影长片 90-120 页；单集剧本 45-60 页；舞台剧按幕/场计，通常 60-90 页。"),
    ],
}

# 诗词：引擎消费 title/idea/form/maxRounds；词牌、韵部、情感核心等由向导折进 idea/校验开关。
_CI_PAI = ["水调歌头", "念奴娇", "沁园春", "满江红", "西江月", "如梦令", "鹧鸪天",
           "定风波", "虞美人", "蝶恋花", "临江仙", "渔家傲", "水仙子", "自定义"]
_POETRY_SECTIONS = {
    "style_title": "格律与附产",
    "brief": [
        _f("form", "体式", "select", options_from="poetry_forms", default="modern", required=True,
           hint="体式即题材：近体四体 / 词 / 曲 / 现代诗，决定格律校验规则。"),
        _f("cipai", "词牌 / 曲牌", "select", options=_CI_PAI, default="水调歌头",
           when={"field": "form", "in": ["ci", "qu"]},
           hint="写入题面，起草时按该牌调句读字数逐句对齐。"),
        _f("maxRounds", "改稿轮数", "radio",
           options=_opts([(1, "1 轮：快出稿"), (2, "2 轮：平衡"), (3, "3 轮：细磨")]),
           default=3, required=True, hint="每轮 = 起草 → 确定性格律校验 → 按结果改稿。"),
    ],
    "basic": [
        _f("title", "题目", "text", max_length=40, hint="留空表示「无题」或由系统自拟。"),
        _f("idea", "题面 / 情境", "textarea", rows=4, max_length=2000, required=True,
           hint="给出题材、场景、要寄的情感，越具体越不容易写成空泛套话。"),
        _f("emotion", "情感核心", "text", max_length=120,
           hint="一句话：这首诗要让谁、在什么时刻、感到什么。留空由引擎自行判定。"),
    ],
    "style": [
        _f("rhyme_note", "韵书", "note",
           text="格律校验按平水韵 106 部（确定性规则）判定平仄与押韵；"
                "词林正韵、中华新韵暂不核验，规划阶段可按韵部自行取舍。",
           when={"field": "form", "in": ["wujue", "qijue", "wulv", "qilv", "ci", "qu"]}),
        _f("metrical_check", "格律校验", "toggle", default=True,
           when={"field": "form", "in": ["wujue", "qijue", "wulv", "qilv", "ci", "qu"]},
           hint="关闭则跳过确定性格律核对与改稿轮，只按审美定稿。"),
        _f("with_notes", "附创作手记", "toggle", default=False,
           hint="勾选后定稿末尾追加一段第一人称创作手记。"),
        _f("with_translation", "附白话译文", "toggle", default=False,
           hint="勾选后定稿末尾追加逐句白话译文。"),
    ],
}

_PROSE_AUDIENCES = ["普通读者", "同行/专业读者", "平台订阅读者", "自己或小圈子"]
_PROSE_PURPOSES = ["分享经验", "说服立场", "记录与纪念", "科普解释", "求助与讨论"]
_PROSE_TONES = ["克制", "锐利", "温和", "冷", "幽默"]
_PROSE_SCALE_WORDS = (
    {"value": "w800", "label": "短", "desc": "约 800 字", "fill": 800},
    {"value": "w1200", "label": "中", "desc": "约 1200 字", "fill": 1200},
    {"value": "w2000", "label": "长", "desc": "约 2000 字", "fill": 2000},
    {"value": "w3000", "label": "特稿", "desc": "约 3000 字", "fill": 3000},
)
_PROSE_SECTIONS = {
    "style_title": "去味与核验",
    "brief": [
        _f("genre", "文体", "select", options_from="prose_genres", default="suibi", required=True,
           hint="文体决定 brief 的七项侧重与语域。"),
        _f("operation", "操作", "radio", options_from="prose_operations", default="write",
           required=True, hint="审=只诊断不改；改=就地最小修改；重写=整篇重来。"),
    ],
    "basic": [
        _f("title", "标题", "text", max_length=40),
        _f("idea", "主题与素材", "textarea", rows=4, max_length=4000,
           required=True, when={"field": "operation", "in": ["write", "recreate"]},
           hint="写清要写的人/事/物、手头的具体细节与事实，缺的会标成待补。"),
        _f("sourceText", "已有正文", "textarea", rows=8, max_length=20000,
           required=True, when={"field": "operation", "in": ["review", "refactor", "recreate"]},
           hint="审 / 改 / 重写需要贴入原稿；原稿会进入诊断与改写。"),
        _f("audience", "受众", "select", options=_PROSE_AUDIENCES, default="普通读者"),
        _f("purpose", "写作目的", "select", options=_PROSE_PURPOSES, default="分享经验"),
        _f("tone", "语气", "select", options=_PROSE_TONES, default="克制"),
        _f("words", "目标字数", "scale", options=_PROSE_SCALE_WORDS, default="w1200",
           custom_key="target_words", min=100, max=20000, unit="字",
           when={"field": "operation", "in": ["write", "recreate"]}),
    ],
    "style": [
        _f("slop_depth", "去 AI 腔强度", "radio",
           options=_opts([("light", "轻度：只标记不清改"),
                          ("standard", "标准：三层诊断 + 改写"),
                          ("deep", "深度：重写 AI 腔最重的段落")]),
           default="standard", hint="三层 = 表达 / 用笔 / 故事逻辑，对应散文流水线的 A 阶段。"),
        _f("fact_check", "事实核验", "toggle", default=True,
           hint="开启后素材阶段标出待核实项，写不出来的地方留 [待补] 标记。"),
    ],
}

_WIZARD: dict[str, dict[str, list[dict]]] = {
    "novel": _NOVEL_SECTIONS,
    "script": _SCRIPT_SECTIONS,
    "poetry": _POETRY_SECTIONS,
    "prose": _PROSE_SECTIONS,
}


def _copy_sections(medium: str) -> dict:
    """深拷贝三段字段，避免调用方改写模块常量。"""
    _extra = {"thesis": _THESIS_WIZARD, "children": _CHILDREN_WIZARD,
              "news": _NEWS_WIZARD, "copy": _COPY_WIZARD}
    src = _WIZARD.get(medium) or _extra.get(medium) or {"brief": [], "basic": [], "style": []}
    out = {group: [dict(f, **({"options": list(f.get("options") or [])} if f.get("options") else {}))
                   for f in src.get(group, [])]
           for group in ("brief", "basic", "style")}
    out["style_title"] = src.get("style_title", "")
    return out


def wizard_schema(medium: str) -> dict:
    """某文体的三段向导字段（结构；options_from 指向的词表由调用方注入）。"""
    return _copy_sections(medium)


def novel_genre_options(vocab: dict | None = None) -> list[dict]:
    """小说题材：每项带 skillkit craft key（_genre_key 实测），未覆盖的题材显式标注走通用默认。"""
    import os
    from . import skillkit as sk
    gen_dir = os.path.join("references", "genre")
    have = {sf.genre for sf in sk.index() if gen_dir in sf.relpath and sf.genre and sf.genre != "_index"}
    labels = list((vocab or {}).get("genres") or [])
    out, seen = [], set()
    for label in labels:
        if label in seen:
            continue
        seen.add(label)
        key = sk._genre_key(label)
        out.append({"value": label, "label": label, "key": key,
                    "desc": sk.GENRE_LABELS.get(key, key) + ("（通用默认）" if key not in have else "")})
    # 有专属 craft 但词表没给的（如仙侠）补上，保证每套 craft 都可达
    ordered = [k for k in sk.GENRE_ORDER if k in have]
    for key in ordered:
        if any(o["key"] == key for o in out):
            continue
        label = sk.GENRE_LABELS.get(key, key)
        out.append({"value": label, "label": label, "key": key, "desc": label})
    return out


def wizard_payload(vocab: dict | None = None) -> dict:
    """给 /api/onboarding 的 by_medium：四文体三屏字段 + 调用方注入的词表。

    vocab 约定（由 app.py 从引擎与 _ONB 取，保证「引擎读什么，向导就问什么」）：
      genres / tones / perspectives / sells / platforms / style_presets
      script_forms / script_entry_paths / poetry_forms / prose_genres / prose_operations
    """
    vocab = vocab or {}
    out = {}
    for key in MEDIUMS:
        sections = _copy_sections(key)
        for group in ("brief", "basic", "style"):
            for fld in sections[group]:
                src = fld.pop("options_from", "")
                if src and not fld.get("options"):
                    fld["options"] = (novel_genre_options(vocab) if src == "genres"
                                      else _opts(vocab.get(src) or []))
        out[key] = sections
    return out


def registry() -> list[dict]:
    """给前端用的完整注册表。"""
    out = []
    for key, m in MEDIUMS.items():
        out.append({
            "key": key,
            "label": m["label"],
            "icon": m["icon"],
            "desc": m["desc"],
            "dir_prefix": m["dir_prefix"],
            "stages": [{"code": c, "name": n, "desc": d} for c, n, d in m["stages"]],
            "score_dims": [{"name": n, "weight": w} for n, w in m["score_dims"]],
            "pass_line": m["pass_line"],
        })
    return out
