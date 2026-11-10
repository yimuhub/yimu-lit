"""统一交付标准 · 规则配置（v4.33.7）。

本模块是**纯数据**：把成稿中不应出现的内容归纳为编号问题类型，定义禁止项、
检查口径、阈值、挂载节点与处置动作。校验逻辑在 ``bot/delivery_gate.py``，
两者一起构成"后续每次创作都按同一标准执行"的唯一规则来源。

设计约束：
  · 零 LLM、零外部依赖、可单测；
  · 规则与阈值全部集中在此文件，任何文体不得在别处另写一套判定；
  · 每条问题类型必须带 stage（挂哪个流程节点）与 action（命中后怎么处置）。
"""
from __future__ import annotations

import re

SPEC_VERSION = "1.1"

# ---------------------------------------------------------------- 禁止项词表

# 生产标记白名单：这些拉丁字母串是行业格式而非污染，不参与 N01/S11 判定
LATIN_ALLOW = {
    "INT", "EXT", "EST", "BGM", "SFX", "VO", "POV", "CU", "ECU", "LS", "MS",
    "FS", "OS", "AI", "TTS", "JSON", "JSONL", "CSV", "ID", "EP", "CUT", "FADE",
}
RE_LATIN_TOKEN = re.compile(r"(?<![A-Za-z])[A-Za-z]{3,}(?![A-Za-z])")

# 元信息泄漏：评分/审稿/重写说明等生产痕迹，绝不允许进入正文
META_LEAK = (
    "评分", "得分", "百分制", "审稿意见", "评审意见", "重写说明", "改写说明",
    "去味说明", "修改说明", "创作说明", "字数统计", "本章概要", "章节备注",
    "自检", "自查报告", "待改进", "质量报告", "输出格式", "只输出",
)

# 语句级工作残留（v4.33.8）：比 META_LEAK 更隐蔽的生产痕迹——口语化自我独白、
# 字数备注、章节号元引用、工作备注。实测翻车样本：
#   「（第7章续写完成，约2276字）」「我们不要纠结这个，继续写」
#   「场记：本集完成第 29-30 章主要事件」「在第10章，他入山后收服的」
META_RESIDUAL_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("字数备注", re.compile(r"[（(][^（）()\n]*?(?:约|共|近|计)\s*\d{2,}\s*(?:字|词|tokens?)[^（）()\n]*?[）)]")),
    ("章节号元引用", re.compile(r"第\s*\d{1,4}\s*[章集]")),
    ("写作过程独白", re.compile(r"(?:不要纠结[这那]|续写完成|写作完成|本章完成[，。]|先[这样写]?继续写)")),
    ("工作备注", re.compile(r"(?:场记|工作备注|待办|TODO|FIXME)\s*[:：]")),
)

# 套语（AI 腔）：按每章出现次数判定密度，不做逐句改稿
SLOP_TERMS = (
    "一丝", "仿佛", "似乎", "缓缓", "微微", "瞬间", "眼中闪过", "深邃",
    "嘴角勾起", "心中一凛", "瞳孔一缩", "不由得", "不由自主", "意味深长",
    "空气仿佛凝固", "宛如", "赫然", "蓦地", "淡淡地", "眼神复杂",
)

# 章尾钩子信号：新事实 / 新代价 / 新威胁
HOOK_SIGNALS = (
    "忽然", "突然", "就在这时", "没想到", "竟然", "却", "传来了", "出现了",
    "逼近", "警告", "倒计时", "才发现", "身后", "锁定", "等着", "才刚刚",
    "不知道的是", "下一瞬", "黑屏", "定格", "画外音", "？", "?", "…",
)

# 终章开新坑信号：完本交付时禁止在最后一章引入未收束的新线
NEW_ARC_TAIL = (
    "才刚刚开始", "游戏开始", "新任务触发", "未完待续", "敬请期待", "第二季",
    "热身", "倒计时", "欢迎", "玩家", "新的敌人", "真正的战斗", "请看下集",
)

# 说话人字段污染：角色名里混入动作/情绪，导致 TTS 无法绑定音色
SPEAKER_ACTION = (
    "颤抖", "咬牙", "沉喝", "点头", "哼", "舔血", "低声", "冷笑", "怒吼",
    "皱眉", "转身", "抬头", "站起", "握紧", "大喊", "惊呼", "声音", "脸色",
    "忽然", "冷声", "轻声", "叹", "喊", "笑", "问", "道", "说",
)

# 不可视化的抽象心理：必须转成可拍摄的动作
ABSTRACT_EMOTION = (
    "心头一紧", "心中一凛", "心中一震", "眼神复杂", "感到孤独", "感到绝望",
    "意识到", "心情复杂", "情绪翻涌", "心生警惕", "隐隐不安", "若有所思",
)

# S15 动作句开头词：台词字段以这些纯动作短语开头（无括号、无引语）大概率是动作句
ACTION_LINE_STARTS = (
    "后退", "点头", "摇头", "转身", "愣", "皱眉", "叹了口气", "叹口气", "沉默",
    "笑了", "苦笑", "咬牙", "握紧", "抬头", "低头", "站起", "坐下", "挥手",
    "想到", "心头", "心中", "瞳孔", "脸色", "深吸", "喘", "看着", "看向",
)

# 集首强冲突信号
OPEN_CONFLICT = (
    "睁眼", "甩", "砸", "血", "杀", "跪", "吼", "炸", "冲", "撕", "倒",
    "怒", "刀", "剑", "雷", "咆哮", "抽", "摔", "踩", "掐", "打", "喝",
)

# 相邻章事件重演：同一标志性事件在相邻章重复出现即判为状态未继承
EVENT_MARKERS = ("系统已激活", "系统激活", "退婚书撕", "初次契约", "新手任务", "首次收服")

# 场标题允许的三种写法（同一部剧只允许出现一种）
SCENE_HEADING_PATTERNS = {
    "场N：": re.compile(r"(?m)^\s*\*{0,2}场\s*\d+\s*[：:]"),
    "第N场：": re.compile(r"(?m)^\s*\*{0,2}第\s*\d+\s*场\s*[：:]"),
    "INT/EXT.": re.compile(r"(?m)^\s*(?:INT|EXT)\b", re.I),
}

# ---------------------------------------------------------------- 阈值配置

CHECKS: dict = {
    "chapter_min_chars": 1500,
    "chapter_max_chars": 3500,
    "slop_per_chapter": 3,
    "hook_tail_chars": 120,
    "intro_dup_jaccard": 0.35,
    "template_open_ratio": 0.15,
    "adjacent_event_jaccard": 0.35,
    "episode_min_chars": 700,
    "episode_max_spoken_chars": 405,     # 90 秒 × 4.5 中文字/秒
    "max_line_chars": 15,
    "min_scenes": 2,
    "max_speaker_once_ratio": 0.15,
    "chars_per_second": 4.5,
    "ep_seconds_max": 90,
    "tail_hook_chars": 160,
    "open_conflict_chars": 120,
    "max_speaker_name_chars": 6,
}

# 出片必需资产与镜头包必需字段
REQUIRED_ASSETS = ("characters", "scenes", "shots", "prompts", "ai_label")
REQUIRED_SHOT_FIELDS = (
    "shot_id", "ep", "duration", "shot_size", "camera", "character",
    "action", "dialogue", "image_prompt", "video_prompt", "sfx", "bgm", "subtitle",
)

# ---------------------------------------------------------------- 问题类型

def _p(code, name, scope, severity, stage, rule, threshold, action):
    return {"code": code, "name": name, "scope": scope, "severity": severity,
            "stage": stage, "rule": rule, "threshold": threshold, "action": action}


PROBLEM_TYPES: tuple[dict, ...] = (
    # ---- 通用正文（所有文体）
    _p("N01", "非中文/代码残留", "text", "block", "draft",
       "正文自然语言中出现白名单外的连续拉丁字母串或代码片段；含实体名被替换成外文",
       "0 处", "定点重写该段并回灌禁写指令"),
    _p("N02", "元信息泄漏", "text", "block", "draft",
       "评分/审稿/重写说明/字数统计等生产痕迹混入正文", "0 处", "整段剔除后重写"),
    _p("N03", "半句截断", "text", "block", "draft",
       "章节或集正文以逗号、分号、冒号或无收束标点的汉字结尾",
       "0 处", "从末段续写并复检"),
    _p("N04", "章标题编号回显", "novel", "block", "draft",
       "章标题中章节编号重复出现，或等于「第N章」无实义占位", "0 处", "重取标题"),
    # ---- 小说
    _p("N05", "章体量越界", "novel", "warn", "draft",
       "单章中文字数超出档位区间", "1500–3500 字", "补写或拆分，落盘前复检"),
    _p("N06", "题记与正文重复 / 模板化开篇", "novel", "warn", "draft",
       "首段题记与正文前 300 字 3-gram 重合超阈值；或全书同一开篇句式占比超阈值",
       "重合 ≤0.35；模板占比 ≤15%", "删除重复题记或重写开篇"),
    _p("N07", "套语密度超标", "novel", "warn", "draft",
       "AI 腔词表在单章出现次数超过上限", "≤3 次/章", "降味重写"),
    _p("N08", "章尾钩子缺失", "novel", "warn", "draft",
       "章末窗口内没有新事实/新代价/新威胁信号，仅以情绪或陈述收束",
       "≥90% 章达标", "补钩子并由验收复检"),
    _p("N09", "相邻章事件重演", "novel", "block", "final",
       "相邻章重复同一标志性事件，或开头 200 字 3-gram 重合超阈值",
       "0 处", "以后章为准回滚前章状态并重写"),
    _p("N10", "终章开新坑 / 未完本", "novel", "block", "final",
       "最后一章引入新任务、新反派、新倒计时或开放式断尾",
       "0 处", "按主线收束清单重写末章"),
    _p("N11", "标题承诺未兑现", "novel", "warn", "final",
       "书名中的数量/身份主张在正文缺少对应事实与后段兑现",
       "主张词在全书 ≥3 次且末 10 章出现", "补充事实或改标题，二选一"),
    _p("N12", "实体命名漂移", "text", "block", "final",
       "同一角色/系统在不同章使用未登记的别名或外文替代名",
       "0 处", "按设定词典归一并回灌"),
    _p("N13", "数值口径冲突", "text", "warn", "final",
       "进度/计数类数值与前文矛盾：同一分母下回退（5/5 之后又 4/5）、"
       "同一进度出现多个分母口径（4/5 与 27/100000 并存）、百分比明显回退",
       "0 处回退/口径打架", "按状态库校准并回灌改正"),
    _p("N15", "世界观违禁物", "text", "block", "draft",
       "正文出现项目世界观红线清单里的违禁物（原始部落的打火机、古代的手机）",
       "0 处", "定点替换为时代内替代物并回灌"),
    # ---- 剧本
    _p("S01", "对白与动作混写", "script", "block", "draft",
       "台词行以括号动作开头，或动作/心理描述写进对白字段",
       "0 行", "拆成 action 与 dialogue 两条记录"),
    _p("S02", "说话人字段污染", "script", "block", "draft",
       "speaker 含动作/情绪词或超长；一次性说话人占比超阈值",
       "0 处；一次性占比 ≤15%", "按角色卡归一"),
    _p("S03", "单句台词超长", "script", "block", "draft",
       "单句对白超过配音可执行的字数上限", "≤15 字", "拆句或转为画外音"),
    _p("S04", "集体量越界", "script", "block", "draft",
       "单集对白字数或场数超出档位预算", "≤405 字；≥2 场", "重切集粒度"),
    _p("S05", "场标题 schema 不一致", "script", "block", "draft",
       "同一部剧混用两种以上场标题写法", "1 种", "统一为档位指定写法"),
    _p("S06", "出片资产缺失", "script", "block", "final",
       "角色卡/场景卡/镜头包/提示词/AI 标识或镜头必需字段缺失",
       "字段完整率 100%", "补齐后方可导出成片包"),
    _p("S07", "抽象心理不可视化", "script", "warn", "draft",
       "画面描述使用无法生成图像的情绪词", "0 处", "转成可拍摄动作"),
    _p("S08", "场景/角色锚点别名", "script", "warn", "final",
       "同一地点或角色存在多种写法，未归并到 canonical id",
       "0 组别名", "建立别名表并全片替换"),
    _p("S09", "对白归属错配", "script", "block", "draft",
       "同一集内相邻两行由不同角色说完全相同的台词，或覆盖章节与大纲不符",
       "0 处", "按源章节重排对白归属"),
    _p("S10", "集首冲突 / 集尾钩子缺失", "script", "warn", "draft",
       "集首窗口无强冲突信号，或集尾窗口无钩子信号", "100% 集达标", "补开场与卡点"),
    _p("S11", "非中文/代码残留", "script", "block", "draft",
       "剧本正文出现白名单外的拉丁字母串或代码片段", "0 处", "定点重写并回灌"),
    _p("S12", "时长超档位", "script", "block", "final",
       "按对白字数与语速估算的单集时长超过档位上限",
       "≤90 秒/集", "重切集粒度"),
    _p("S13", "括号体系混用", "script", "warn", "draft",
       "同一部剧动作/舞台提示混用全角（）与方头【】两种括号体系",
       "1 种", "统一为一种并全片替换"),
    _p("S14", "场次编号重复", "script", "warn", "draft",
       "同一集内出现两个相同编号的场标题（如两个「场2」）",
       "0 处", "重排场次编号"),
    _p("S15", "动作句写进台词", "script", "warn", "draft",
       "台词字段是不带括号的纯动作句（如「后退一步。」「愣了一下。」）",
       "0 行", "拆成 action 与 dialogue 两条记录"),
)

BY_CODE = {p["code"]: p for p in PROBLEM_TYPES}

# ---------------------------------------------------------------- 交付档位

PROFILES: dict = {
    "universal": {
        "label": "通用正文洁净档（所有文体默认）",
        "enforcement": "advisory",
        "block_on_export": ("N01", "N02", "N03", "N04", "S11"),
        "checks": {},
    },
    "novel_submission": {
        "label": "小说投稿档",
        "enforcement": "enforcing",
        "block_on_export": tuple(p["code"] for p in PROBLEM_TYPES
                                 if p["scope"] in ("novel", "text") and p["severity"] == "block"),
        "checks": {"chapter_min_chars": 1800, "chapter_max_chars": 3200,
                   "slop_per_chapter": 3, "hook_rate": 0.90},
    },
    "script_production": {
        "label": "短剧出片档",
        "enforcement": "enforcing",
        "block_on_export": tuple(p["code"] for p in PROBLEM_TYPES
                                 if p["scope"] in ("script", "text") and p["severity"] == "block"),
        "checks": {"max_line_chars": 15, "ep_seconds_max": 90,
                   "min_scenes": 2, "require_assets": True},
    },
}

# 流程节点 → 该节点必须执行的检查
STAGES: dict = {
    "draft": ("N01", "N02", "N03", "N04", "N05", "N06", "N07", "N08", "N15",
              "S01", "S02", "S03", "S04", "S05", "S07", "S09", "S10", "S11",
              "S13", "S14", "S15"),
    "final": ("N09", "N10", "N11", "N12", "N13", "S06", "S08", "S12"),
    "export": ("N01", "N02", "N03", "N04", "N15", "S11"),
}


def profile(name: str | None) -> dict:
    return PROFILES.get((name or "").strip() or "universal", PROFILES["universal"])


def profile_for(kind: str) -> str:
    """文体 → 默认交付档位。所有新创作一律按档位验收，不逐作品例外。"""
    return {"novel": "novel_submission", "script": "script_production"}.get(
        (kind or "").strip(), "universal")


def stamp_plan(plan: dict, kind: str, *, plan_exists: bool = False,
               existing: dict | None = None) -> dict:
    """新建作品时在计划里盖上交付规格版本，作为「按新标准创作」的唯一凭据。

    历史作品计划已存在且处于创作中/完成态，绝不追溯盖章；它们仍可导出，
    但每次导出都会产出《交付就绪报告》，让问题可见。
    existing 用于多写入者场景：旧快照回写时不得把规格章冲掉。
    """
    if not isinstance(plan, dict):
        return plan
    if isinstance(existing, dict):
        for k in ("deliverySpecVersion", "deliveryProfile"):
            if not plan.get(k) and existing.get(k):
                plan[k] = existing[k]
    if plan.get("deliverySpecVersion"):
        return plan
    if plan_exists and str(plan.get("status") or "") not in ("", "planning", "draft", "pending"):
        return plan
    plan["deliverySpecVersion"] = SPEC_VERSION
    plan["deliveryProfile"] = profile_for(kind)
    return plan


def threshold(key: str, profile_name: str | None = "universal") -> object:
    prof = profile(profile_name)
    return prof.get("checks", {}).get(key, CHECKS.get(key))


def rules_for(stage: str, scope: str = "") -> list[dict]:
    codes = STAGES.get(stage, ())
    out = [BY_CODE[c] for c in codes if c in BY_CODE]
    if scope:
        out = [p for p in out if p["scope"] in (scope, "text")]
    return out


def spec() -> dict:
    """供前端/接口读取的完整规格快照。"""
    return {"version": SPEC_VERSION, "profiles": PROFILES, "checks": CHECKS,
            "stages": STAGES, "problem_types": PROBLEM_TYPES,
            "required_assets": REQUIRED_ASSETS,
            "required_shot_fields": REQUIRED_SHOT_FIELDS}
