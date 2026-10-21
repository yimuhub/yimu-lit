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
        "checker": None,
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
            ("B", "Brief", "体裁 / 受众 / 目的 / 语气 / 约束 / 事实清单"),
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
