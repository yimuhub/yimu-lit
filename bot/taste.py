"""市场标尺（v4.36.0）：把「读者用什么标准判一本小说值不值得看」固化成可执行参数。

为什么要有这个模块
------------------
此前平台的判据是「全书均分 ≥ 80」，那把尺子有三个毛病：
  1) 它是模型自评，实测 ±8 分随机震荡，拿它驱动重写等于抽卡；
  2) 它只判「这一段写得好不好」，不判「读者会不会追下去」；
  3) 它对连载和出版用同一套标准，而两者的读者行为完全不同
     —— 连载读者每章都在决定「明天还来不来」，出版读者买断后看的是整体完成度。

本模块改为按**真实市场口径**分形态定义标尺，且区分三类指标：
  * STRUCTURE  结构参数（字数/章数/章长/卷数）——硬性，用于规划与体检；
  * CRAFT_GATES 工艺闸门（可确定性检测）——分 A/B/C 层，A 层才拦交付；
  * BENCHMARKS  读者行为基准（平台公开数据）——只用于**目标设定与报告**，
    绝不作为写作用力的方向。因为平台指标是结果，不是可写的对象。

数据来源与时效
--------------
BENCHMARKS 里的数字来自 2026 年公开行业报道、平台官方作者公告与作者社区经验帖，
属于**编辑性整理的快照**，不是平台接口、不是承诺值：
  - 番茄 10 万字完读率近年均值 16%~17%，高于 17% 视为优秀（中国作家网·吉云飞 2026-01）
  - 番茄追更比理想 2:1；作者社区经验线「1:3 稳、1:4~1:5 不错、跌破 1:6 警惕」
  - 番茄追读/书架比：22%~29% 及格、≥30% 优秀、接近 40% 具爆款潜质（作者侧经验帖）
  - 起点收订比 10:1 正常、5:1 优秀、20:1 崩盘；真追≈假追 0.6~0.7；试水推约 6 万字
  - 番茄官方签约评估四原则：原创合规、故事精彩、创新独特、立意深刻、表达流畅
  - 番茄官方明确整治四类低质：AI 粗制滥造 / 格式混乱 / 结构失常 / 空洞水文
平台上这些数字会变，所以全部带 `as_of` 字段，界面展示时应连带展示口径。

设计约束
--------
* 纯数据 + 纯函数，**不导入 bot.writer**（避免循环导入），不调用 LLM；
* 所有阈值都可被 cfg 覆盖（`cfg["form_overrides"]`），以便按题材微调；
* 缺省形态取 serial —— 平台历史项目全是连载形态，不能因升级而改变老项目行为。
"""
from __future__ import annotations

# ---------- 形态 ----------

FORM_SERIAL = "serial"   # 连载型：免费/付费连载，按章追更
FORM_BOOK = "book"       # 出版型：整本完结，实体/电子书买断

FORMS = {
    FORM_SERIAL: {
        "label": "连载型",
        "note": "按章连载、按章收费或按广告分成；读者的每一次「点下一章」都是一次投票。",
        "audience": "追更读者：耐心短、反馈快、对套路熟悉、为「接下来会怎样」付费",
        # ---- 结构参数 ----
        "total_words": (300000, 1500000),   # 规划区间（下限=可上架，上限=别失控）
        "default_total_words": 600000,
        "chapters": (120, 500),
        "default_chapters": 200,
        "chapter_words": (2000, 4000),      # 番茄偏 2200-2800，起点偏 3000-4000
        "default_chapter_words": 2800,
        "volumes": (4, 10),
        "default_volumes": 6,
        # ---- 开篇 ----
        "opening": {
            "hook_within_chars": 500,       # 首章前 500 字必须出现核心冲突
            "golden_chapters": 3,           # 黄金三章
            "must_in_first_3": ["主角身份", "核心困境", "第一次小胜或转机"],
            "ban": ["大段世界观说明", "主角迟迟不出场", "连续两章无冲突"],
        },
        # ---- 节奏 ----
        "rhythm": {
            "hook_every_chapter": True,     # 每章结尾必须有钩子
            "small_payoff_every": 2,        # 每 2 章一个小爽点/反转
            "big_climax_every": 10,         # 每 10 章一个阶段性高潮
            "max_flat_chapters": 2,         # 连续平淡章上限
        },
        # ---- 伏笔 ----
        "foreshadow": {"max_open_a": 12, "must_payoff_ratio": 0.85,
                       "allow_series_hook": True},
        # ---- 结局 ----
        "ending": {"closed": False, "note": "阶段闭合即可，可留续作钩子；但当前卷主线必须收束"},
        # ---- 爽点 / 情绪 ----
        "emotion": {"density": "high", "note": "稳定供给情绪价值：每 2 章一个可被读者记住的爽点或反转"},
    },
    FORM_BOOK: {
        "label": "出版型",
        "note": "整本完结后交付（实体、电子书、影视改编）；读者买的是一次完整的阅读体验。",
        "audience": "买断读者与编辑：容忍慢热，但不能容忍结构塌陷、人物崩坏、结局泄气",
        "total_words": (100000, 350000),
        "default_total_words": 200000,
        "chapters": (25, 80),
        "default_chapters": 45,
        "chapter_words": (2500, 6000),
        "default_chapter_words": 4200,
        "volumes": (1, 4),
        "default_volumes": 3,
        "opening": {
            # 出版型不要求 500 字出冲突，但要求第一章结束前把「这个人为什么要被写」立住
            "hook_within_chars": 1500,
            "golden_chapters": 1,
            "must_in_first_3": ["主角处境", "核心矛盾", "这本书要回答的问题"],
            "ban": ["开篇纯写景超过 300 字", "第一章未出现主角", "前 3 章无线索推进"],
        },
        "rhythm": {
            # 关键差异：出版型**不要求**每章硬拗悬念。章尾可以是收束句，但整章必须有推进。
            "hook_every_chapter": False,
            "small_payoff_every": 3,
            "big_climax_every": 8,
            "max_flat_chapters": 1,
            "reversal_windows": [0.25, 0.50, 0.75],  # 三幕两个转折 + 中点，按全书比例
        },
        "foreshadow": {"max_open_a": 8, "must_payoff_ratio": 1.0,   # 出版型要求全回收
                       "allow_series_hook": False},
        "ending": {"closed": True, "note": "主线必须闭合：所有 A 级伏笔有答案，人物弧线有落点"},
        "emotion": {"density": "medium", "note": "不追求爽点密度，追求情感转折的必然性与意义感"},
    },
}

DEFAULT_FORM = FORM_SERIAL


# ---------- 读者行为基准（只用于目标与报告，不作为写作方向） ----------

BENCHMARKS = {
    "fanqie": {   # 番茄：免费 + 广告，算法看留存
        "as_of": "2026-11",
        "currency": "免费阅读·广告分成",
        "metrics": {
            "complete_rate_100k": {"median": 0.165, "good": 0.17,
                                   "note": "10 万字完读率，近年均值 16%~17%，>17% 有机会获流量扶持"},
            "chase_ratio": {"ideal": 2.0, "ok": 3.0, "warn": 6.0,
                            "note": "追更比 = 在读 : 追到最新章；2:1 理想，1:3 稳，跌破 1:6 警惕"},
            "retention_7d": {"pass": 0.22, "good": 0.30, "viral": 0.40,
                             "note": "追读率/书架比：22%~29% 及格，≥30% 优秀，接近 40% 具爆款潜质"},
            "click_test": {"pass": 300, "strong": 500,
                           "note": "首秀验证期吸量：300 点击合格(A级)，500 以上明显倾斜(SS级)"},
        },
        "levers": ["书名/封面/简介的吸量", "前 300 字冲突", "章尾钩子", "日更稳定"],
    },
    "qidian": {   # 起点：付费订阅，编辑看真追与收订比
        "as_of": "2026-11",
        "currency": "订阅付费",
        "metrics": {
            "order_ratio": {"normal": 10.0, "good": 5.0, "collapse": 20.0,
                            "note": "收订比（收藏:订阅），10:1 正常，5:1 优秀，20:1 崩盘"},
            "true_chase_factor": {"value": 0.65,
                                  "note": "真追（付费追读）≈ 假追 × 0.6~0.7，编辑只看真追"},
            "trial_push_words": {"value": 60000, "note": "约 6 万字进试水推，核心考核有效付费追读"},
            "collect_milestone": {"words": 150000, "collect": 5000,
                                  "note": "15 万字 5000 收藏是常见生死线"},
        },
        "levers": ["黄金三章", "长期期待感", "世界观自洽", "稳定更新不掉订阅"],
    },
    "jinjiang": {
        "as_of": "2026-11",
        "currency": "订阅付费·签人",
        "metrics": {
            "note": "晋江签作者而非签单本，文风口碑与长评文化权重高；AI 仅允许校对，写情节直接锁文",
        },
        "levers": ["人设张力", "情感浓度", "文风辨识度", "CP 关系推进"],
    },
}

# 平台官方明确的「低质」四类（番茄 2026-05 公告口径），用于自检措辞
OFFICIAL_LOW_QUALITY = [
    ("ai_slop", "AI 粗制滥造", "缺乏原创性与完整性、语言生硬、内容空洞"),
    ("format_broken", "格式混乱", "章节结构错乱、markdown 残留、正文混入说明性文字"),
    ("structure_broken", "结构失常", "前后矛盾、主线断裂、大段无效铺陈拉长篇幅"),
    ("water", "空洞水文", "重复堆砌、机械扩写、情节不推进"),
]


# ---------- 工艺闸门定义 ----------
# layer: A=硬伤(必拦) / B=结构(按形态) / C=体验(只提示)
# forms: 适用形态；空表示通用
CRAFT_GATES = [
    # ---- A 层：确定性硬伤，任何形态都不允许 ----
    {"id": "C01", "name": "半句截断", "layer": "A", "check": "truncated",
     "forms": [], "desc": "章节末尾停在半句（无句末标点且末尾像未完成的词），通常是生成中断"},
    {"id": "C02", "name": "复读循环", "layer": "A", "check": "repeat",
     "forms": [], "desc": "相邻段落或全章出现高比例重复段落，模型陷入复读"},
    {"id": "C03", "name": "元信息泄漏", "layer": "A", "check": "meta_leak",
     "forms": [], "desc": "正文叙述中出现「第X章」「本章」「上一章」等创作层元信息"},
    {"id": "C04", "name": "过程产物残留", "layer": "A", "check": "process_residue",
     "forms": [], "desc": "正文混入修改思路/评分表/本章概要等非正文内容"},
    {"id": "C05", "name": "数数错误", "layer": "A", "check": "count_error",
     "forms": [], "desc": "「说了四个字」但实际不是四个字——AI 分词造成的典型穿帮"},
    {"id": "C06", "name": "格式污染", "layer": "A", "check": "format_pollution",
     "forms": [], "desc": "正文出现 markdown 符号（* # > ` 列表符）或英文残留"},

    # ---- B 层：结构，按形态判定 ----
    {"id": "C10", "name": "开篇要素缺失", "layer": "B", "check": "opening",
     "forms": [], "desc": "前 N 字内未出现核心冲突/主角处境（连载 N=500，出版 N=1500）"},
    {"id": "C11", "name": "章尾无钩子", "layer": "B", "check": "hook_tail",
     "forms": [FORM_SERIAL], "desc": "连载型每章结尾必须留钩；出版型不要求"},
    {"id": "C12", "name": "章长失控", "layer": "B", "check": "chapter_len",
     "forms": [], "desc": "单章字数偏离目标区间（过短=信息不足，过长=该拆章）"},
    {"id": "C13", "name": "对话占比失衡", "layer": "B", "check": "dialogue_ratio",
     "forms": [], "desc": "对话占比过低（叙述压死节奏）或过高（缺少画面与内心）"},
    {"id": "C14", "name": "结局未闭合", "layer": "B", "check": "ending_closed",
     "forms": [FORM_BOOK], "desc": "出版型收尾必须给主线答案；连载型允许续作钩"},
    {"id": "C15", "name": "连续平淡章", "layer": "B", "check": "flat_streak",
     "forms": [FORM_SERIAL], "desc": "连续多章无爽点/反转，追读会掉"},

    # ---- C 层：体验，只提示不改写 ----
    {"id": "C20", "name": "句长过于均匀", "layer": "C", "check": "burstiness",
     "forms": [], "desc": "句长方差过低，读起来像说明书——AI 文本的典型节律"},
    {"id": "C21", "name": "AI 句式密度高", "layer": "C", "check": "ai_phrases",
     "forms": [], "desc": "「不是……而是……」「与此同时」「值得一提的是」等高频模板句"},
    {"id": "C22", "name": "段落过长", "layer": "C", "check": "paragraph_len",
     "forms": [], "desc": "手机阅读段落过长会显著拉低完读率"},
    {"id": "C23", "name": "描写压过事件", "layer": "C", "check": "desc_heavy",
     "forms": [], "desc": "环境/心理描写占比过高，事件推进不足"},
]

LAYER_LABEL = {"A": "硬伤", "B": "结构", "C": "体验"}
LAYER_BLOCKING = {"A": True, "B": False, "C": False}   # B 层仅在对应形态下升级为告警


# ---------- 取值 ----------

def normalize(form) -> str:
    """把各种叫法归一成 serial / book；未知一律回落到默认形态。"""
    s = str(form or "").strip().lower()
    if s in ("book", "novel", "publish", "published", "出版", "出版型", "实体书", "完结"):
        return FORM_BOOK
    if s in ("serial", "series", "webnovel", "web", "连载", "连载型", "网文"):
        return FORM_SERIAL
    return DEFAULT_FORM


def form_of(cfg) -> str:
    """从作品 cfg 读形态；兼容旧项目（无 form 字段 → 连载）。"""
    c = cfg if isinstance(cfg, dict) else {}
    return normalize(c.get("form") or c.get("book_form") or c.get("publish_mode"))


def spec(form, cfg=None) -> dict:
    """取形态规格，并允许 cfg['form_overrides'] 覆盖标量/区间字段。"""
    f = normalize(form)
    base = dict(FORMS[f])
    base["form"] = f
    ov = (cfg or {}).get("form_overrides") if isinstance(cfg, dict) else None
    if isinstance(ov, dict):
        for k, v in ov.items():
            if k in base and isinstance(base[k], (int, float, tuple, str)) and v is not None:
                base[k] = v
    return base


def chapter_words_of(cfg) -> int:
    """单章目标字数：用户显式设定优先，其次形态默认值，最后题材微调。"""
    c = cfg if isinstance(cfg, dict) else {}
    # `min_words_per_chapter` 是 writer/app 实际写入的键名，必须一并认，
    # 否则出版型默认 4200 会盖掉用户显式填的 3000。
    for k in ("wordsPerChapter", "chapter_words", "perChapter", "min_words_per_chapter"):
        try:
            v = int(c.get(k) or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            return v
    sp = spec(form_of(c), c)
    return int(sp.get("default_chapter_words") or 2800)


def chapter_count_of(cfg) -> int:
    """规划章数：用户显式设定优先，其次由总字数/单章字数推，最后形态默认。"""
    c = cfg if isinstance(cfg, dict) else {}
    for k in ("totalChapters", "chapters"):
        try:
            v = int(c.get(k) or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            return v
    sp = spec(form_of(c), c)
    try:
        total = int(c.get("totalWords") or c.get("target_words") or 0)
    except (TypeError, ValueError):
        total = 0
    if total > 0:
        return max(1, round(total / max(1, chapter_words_of(c))))
    return int(sp.get("default_chapters") or 200)


def total_words_of(cfg) -> int:
    c = cfg if isinstance(cfg, dict) else {}
    try:
        v = int(c.get("totalWords") or c.get("target_words") or 0)
    except (TypeError, ValueError):
        v = 0
    if v > 0:
        return v
    return chapter_count_of(c) * chapter_words_of(c)


def gates_for(form=None, layer=None) -> list:
    """按形态与层级筛出适用闸门。"""
    f = normalize(form)
    out = []
    for g in CRAFT_GATES:
        if g["forms"] and f not in g["forms"]:
            continue
        if layer and g["layer"] != layer:
            continue
        out.append(dict(g))
    return out


def gate(gid) -> dict | None:
    return next((dict(g) for g in CRAFT_GATES if g["id"] == gid), None)


# ---------- 给模型的写作纪律（由形态生成，避免两套提示词漂移） ----------

def writing_discipline(cfg) -> str:
    """写前注入的形态纪律：只写与「当前形态」相关的硬约束，不堆通用废话。"""
    f = form_of(cfg)
    sp = spec(f, cfg)
    cw = chapter_words_of(cfg)
    lines = [f"【形态纪律 · {sp['label']}】{sp['note']}",
             f"读者是谁：{sp['audience']}"]

    op = sp.get("opening") or {}
    if f == FORM_SERIAL:
        lines.append(
            f"开篇：首章前 {op.get('hook_within_chars', 500)} 字内必须出现核心冲突；"
            f"前 {op.get('golden_chapters', 3)} 章完成「{ '、'.join(op.get('must_in_first_3') or []) }」；"
            f"禁止 {'、'.join(op.get('ban') or [])}。")
        rh = sp.get("rhythm") or {}
        lines.append(
            f"节奏：每章结尾必须留钩子；每 {rh.get('small_payoff_every', 2)} 章一个小爽点或反转；"
            f"每 {rh.get('big_climax_every', 10)} 章一个阶段性高潮；"
            f"连续平淡不超过 {rh.get('max_flat_chapters', 2)} 章。")
        lines.append(f"章长：{cw} 字左右（区间 {sp['chapter_words'][0]}–{sp['chapter_words'][1]}），"
                     "宁短勿水：凑字数是平台明令的低质行为。")
    else:
        lines.append(
            f"开篇：第 1 章结束前必须立住主角处境与核心矛盾（前 {op.get('hook_within_chars', 1500)} 字内）；"
            f"前 3 章要让读者明白「{ '、'.join(op.get('must_in_first_3') or []) }」；"
            f"禁止 {'、'.join(op.get('ban') or [])}。")
        rh = sp.get("rhythm") or {}
        lines.append(
            "节奏：**不要求**每章硬拗悬念（那是连载手法，用在出版稿上会显得做作）；"
            f"每 {rh.get('small_payoff_every', 3)} 章一次实质推进或情感转折；"
            "三个转折窗口按全书 25%/50%/75% 布置。")
        lines.append(f"章长：{cw} 字左右（区间 {sp['chapter_words'][0]}–{sp['chapter_words'][1]}）；"
                     "章节按「场景/时间/视角」切分，不按字数硬切。")

    fs = sp.get("foreshadow") or {}
    lines.append(f"伏笔：同时开放的 A 级线不超过 {fs.get('max_open_a', 12)} 条；"
                 f"兑现率要求 {int(float(fs.get('must_payoff_ratio', 0.85)) * 100)}%"
                 + ("（出版型必须全部回收）" if f == FORM_BOOK else "") + "。")
    ed = sp.get("ending") or {}
    if ed.get("closed"):
        lines.append(f"收尾：{ed.get('note')}。")
    em = sp.get("emotion") or {}
    if em.get("note"):
        lines.append(f"情绪：{em['note']}。")
    lines.append("通用红线（平台明令低质）：不重复堆砌、不机械扩写、不写不推进情节的段落；"
                 "正文不出现任何创作层说明（第X章/本章/修改思路/评分）。")
    return "\n".join(lines)


def structure_template(cfg) -> str:
    """给规划器的结构模板：出版型给三幕比例，连载型给卷弧与阶段高潮。"""
    f = form_of(cfg)
    sp = spec(f, cfg)
    n = chapter_count_of(cfg)
    if f == FORM_BOOK:
        a, b = max(1, round(n * 0.25)), max(1, round(n * 0.50))
        return ("【结构模板 · 出版型三幕】\n"
                f"- 第一幕（1–{a} 章，25%）：建立处境、打破平衡、主角被迫行动\n"
                f"- 第二幕（{a + 1}–{a + b} 章，50%）：对抗升级；"
                f"第 {a + round(b * 0.5)} 章前后设中点反转；"
                f"第 {a + round(b * 0.8)} 章前后「一无所有」跌到谷底\n"
                f"- 第三幕（{a + b + 1}–{n} 章，25%）：用成长后的主角解决最终危机，主线闭合")
    vol = int(sp.get("default_volumes") or 6)
    per = max(1, n // max(1, vol))
    return ("【结构模板 · 连载型卷弧】\n"
            f"- 全书 {n} 章，建议 {vol} 卷，每卷约 {per} 章\n"
            "- 每卷内：卷首设新目标 → 中段加压 → 卷末高潮并兑现该卷伏笔\n"
            "- 每 10 章一个阶段性高潮；每 2 章一个小爽点或反转\n"
            "- 第 1–3 章为黄金三章：冲突前置、金手指/核心优势露面、第一次小胜")


_PLATFORM_ALIASES = {
    # 平台在 cfg 里可能存 key（fanqie）、也可能存中文名（番茄小说），两种都要认。
    "fanqie": ("fanqie", "番茄", "tomato"),
    "qidian": ("qidian", "起点"),
    "jinjiang": ("jinjiang", "晋江"),
}


def _bench_key(platform) -> str:
    p = str(platform or "").strip().lower()
    if not p:
        return ""
    for key, names in _PLATFORM_ALIASES.items():
        if any(n in p for n in names):
            return key
    return ""


def benchmark_note(platform) -> str:
    """给报告用：把平台基准转成一句人话，说明「这本书要打到什么水平才算站住」。

    没有公开基准的平台（七猫/飞卢等）不编数字，只回通用工艺线——报告可以说
    「没有平台口径」，但不能拿猜测冒充实测。
    """
    key = _bench_key(platform)
    b = BENCHMARKS.get(key) or {}
    if not b:
        if str(platform or "").strip():
            return (f"{platform} 暂无公开量化基准，按通用工艺线把握："
                    "前 300 字出冲突、章尾留钩、不复读、不注水。")
        return ""
    m = b.get("metrics") or {}
    if key == "fanqie":
        return (f"番茄口径（{b['as_of']}）：10 万字完读率中位 {m['complete_rate_100k']['median']:.1%}，"
                f">17% 才算优秀；追更比 2:1 理想、跌破 1:6 危险。"
                "这两项决定算法还给不给量，与文笔评价无关。")
    if key == "qidian":
        return (f"起点口径（{b['as_of']}）：收订比 10:1 正常、5:1 优秀、20:1 崩盘；"
                "编辑看真追（约为假追的 0.65），不看点击。")
    return f"晋江口径（{b['as_of']}）：{m.get('note', '')}"


def report_notes(cfg) -> list:
    """交付报告的开场白：先把「按什么标准衡量这本书」说清楚。

    报告里一串禁止项如果不铺垫标尺，作者读起来只觉得在挑刺；
    先给形态与平台基准，再看具体条目，才像一份「验收单」。
    """
    c = cfg if isinstance(cfg, dict) else {}
    form = form_of(c)
    notes = [f"作品形态：{FORMS[form]['label']}"
             f"（{chapter_count_of(c)} 章 × 约 {chapter_words_of(c)} 字）"]
    bn = benchmark_note(c.get("platform") or c.get("platformKey") or "")
    if bn:
        notes.append(bn)
    return notes


def payload() -> dict:
    """给前端：形态选项 + 标尺摘要（开书向导与设置页共用）。"""
    import copy
    return {
        "forms": [
            {"key": k, "label": v["label"], "note": v["note"],
             "total_words": list(v["total_words"]),
             "default_total_words": v["default_total_words"],
             "chapters": list(v["chapters"]),
             "default_chapters": v["default_chapters"],
             "chapter_words": list(v["chapter_words"]),
             "default_chapter_words": v["default_chapter_words"]}
            for k, v in FORMS.items()
        ],
        "default_form": DEFAULT_FORM,
        "benchmarks": copy.deepcopy(BENCHMARKS),
        "low_quality": [{"key": k, "name": n, "desc": d} for k, n, d in OFFICIAL_LOW_QUALITY],
        "gates": [{"id": g["id"], "name": g["name"], "layer": g["layer"],
                   "layer_label": LAYER_LABEL[g["layer"]], "desc": g["desc"]}
                  for g in CRAFT_GATES],
    }
