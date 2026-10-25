"""固定评测集与可复现打分（技能晋升的「刹车片」）。

职责：
1. 内置 8 个固定用例（四文体各 2 个），提示词与约束完全写死，保证不同版本的技能
   在同一把尺子上比较；
2. ``run_case`` 只经 ``bot.llm.chat`` 调用模型；模型不可用时返回 ok=False，不抛异常；
3. ``score_output`` 优先让 LLM 按 medium.py 的维度打分；LLM 空/失败时走确定性启发式
   （字数区间、结构标记、AI 腔词、重复 n-gram、约束达成），保证离线可跑；
4. ``compare`` 逐案比较 baseline / candidate，给出胜平负与「是否建议晋升」。

数据文件：``data/evals/<ts>-<id>.json``（每次评测留档，便于回滚与审计）。
安全边界：只读内置用例与技能文本；不访问网络（LLM 由调用方配置）；文件名做白名单清洗。
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import UTC, datetime

from . import llm
from . import medium as medium_mod
from .safeio import safe_join

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
#: 评测留档目录；测试可 monkeypatch。
EVAL_DIR = os.path.join(DATA_DIR, "evals")

# 通用 AI 腔词：只做扣分提示，不做「一票否决」——避免把正常书面语误杀
_SLOP_WORDS = (
    "总而言之", "综上所述", "值得注意的是", "不难发现", "毋庸置疑", "不可否认",
    "彰显", "不可估量", "意义重大", "深刻地", "极大地", "在这个", "的时代",
    "空气突然安静", "仿佛时间静止", "命运的齿轮", "内心深处", "灵魂深处",
    "宛如一幅画卷", "令人不禁", "不由得", "一丝不易察觉",
)
_HEDGE_RE = re.compile(r"(?:非常|十分|极其|格外|无比|异常|颇为|甚是)")
_SCENE_HEAD_RE = re.compile(
    r"(?m)^\s*(?:\d+[.、]\s*)?(?:INT\.?|EXT\.?|内景|外景|场景|第[一二三四五六七八九十0-9]+场|"
    r"(?:日|夜|晨|昏)\s*[／/]\s*(?:内|外))"
)
_DIALOGUE_RE = re.compile(r"(?m)^\s*[\u4e00-\u9fa5A-Za-z0-9·]{1,8}\s*[：:]")

# ---------------------------------------------------------------- 固定评测集

CASES: list[dict] = [
    {
        "id": "novel-opening",
        "medium": "novel",
        "stage": "P1",
        "prompt": (
            "写一部长篇小说的第一章开头（800–1200 字）。设定：现代都市，主角是一名夜班公交司机，"
            "连续三晚在同一站台看见同一个撑黑伞的女人，第四晚她上了车却没有投币。"
            "要求：第三人称限知；前 300 字内出现一个具体、可验证的异常；"
            "主角必须做出一个会带来代价的行动；章末留下一个具体的新问题。"
        ),
        "constraints": [
            "前 300 字内出现具体异常",
            "主角有可执行目标并采取行动",
            "章末留下新问题而不是抒情总结",
            "不使用总结式议论与 AI 腔套话",
        ],
        "rubric": {
            "chars": [800, 1200],
            "checks": [
                {"id": "anomaly", "label": "开篇具体异常", "kind": "any",
                 "terms": ["伞", "投币", "硬币", "监控", "站台", "车门", "座位", "影子"]},
                {"id": "goal", "label": "主角采取行动", "kind": "any",
                 "terms": ["问", "追", "停", "回头", "下车", "报警", "拍照", "按", "喊"]},
                {"id": "hook", "label": "章末新问题", "kind": "regex", "pattern": r"[？?！!…]\s*$"},
            ],
        },
    },
    {
        "id": "novel-volume-arc",
        "medium": "novel",
        "stage": "P0",
        "prompt": (
            "为一部长篇都市悬疑小说设计第一卷（30 章）的卷弧。输出必须包含："
            "①本卷主目标与失败代价；②三个弧段（各 8–12 章）的转折与新增变量；"
            "③第 10 / 20 / 30 章的滚动重规划检查点；④本卷埋下、最迟下卷兑现的 3 条伏笔。"
            "用条目化中文，不要写正文。"
        ),
        "constraints": [
            "包含目标与代价",
            "包含三个弧段及转折",
            "包含 10/20/30 章检查点",
            "包含 3 条伏笔及兑现窗口",
        ],
        "rubric": {
            "chars": [500, 1600],
            "checks": [
                {"id": "cost", "label": "失败代价", "kind": "any",
                 "terms": ["代价", "失去", "牺牲", "后果"]},
                {"id": "arcs", "label": "三个弧段", "kind": "all",
                 "terms": ["弧", "转折", "变量"]},
                {"id": "checkpoint", "label": "滚动检查点", "kind": "all",
                 "terms": ["10", "20", "30"]},
                {"id": "foreshadow", "label": "伏笔兑现", "kind": "all",
                 "terms": ["伏笔", "兑现"]},
            ],
        },
    },
    {
        "id": "script-scene",
        "medium": "script",
        "stage": "S5",
        "prompt": (
            "写一场 2 页左右的剧本场景。情境：医院走廊，深夜；女儿要替父亲签一份放弃抢救同意书，"
            "哥哥赶到阻止。要求：标准场景头（内景/外景、地点、时间）；动作行只写镜头能拍到的东西；"
            "对白有潜台词；场景结束时价值发生翻转（签 / 不签）。不要写摄影机说明。"
        ),
        "constraints": [
            "含标准场景头",
            "动作行可视化、不写心理",
            "对白有潜台词",
            "场景结尾价值翻转",
        ],
        "rubric": {
            "chars": [600, 1800],
            "checks": [
                {"id": "scene_head", "label": "场景头", "kind": "regex",
                 "pattern": r"(?m)^\s*(?:INT\.?|EXT\.?|内景|外景)"},
                {"id": "action", "label": "动作行", "kind": "any",
                 "terms": ["她", "他", "门", "走廊", "笔", "纸", "推", "抓", "递"]},
                {"id": "dialogue", "label": "对白", "kind": "regex",
                 "pattern": r"(?m)^\s*[\u4e00-\u9fa5A-Za-z0-9·]{1,8}\s*[：:]"},
                {"id": "turn", "label": "价值翻转", "kind": "any",
                 "terms": ["签", "不签", "撕", "放下", "收回", "转身"]},
            ],
        },
    },
    {
        "id": "script-dialogue",
        "medium": "script",
        "stage": "S5",
        "prompt": (
            "写一段 40 行以内的双人对白戏。情境：审讯室，警察与嫌疑人隔着桌子；"
            "警察知道嫌疑人在撒谎，但没有直接证据。要求：不写「他愤怒地说」这类表演提示；"
            "每句台词不超过 40 字；至少三次信息差反转；最后一句让嫌疑人主动露出破绽。"
            "只输出场景头、动作行与对白。"
        ),
        "constraints": [
            "双人对白、40 行以内",
            "不写情绪表演提示",
            "至少三次信息差反转",
            "末句让嫌疑人露出破绽",
        ],
        "rubric": {
            "chars": [350, 1500],
            "checks": [
                {"id": "two_people", "label": "双人对白", "kind": "all",
                 "terms": ["警察", "嫌疑人"]},
                {"id": "turns", "label": "多次反转", "kind": "any",
                 "terms": ["不对", "你怎么", "谁说", "可是", "等等", "你确定", "如果"]},
                {"id": "slip", "label": "露出破绽", "kind": "any",
                 "terms": ["你怎么知道", "我没说", "当时", "在场", "名字", "时间"]},
            ],
        },
    },
    {
        "id": "poetry-qijue",
        "medium": "poetry",
        "stage": "C",
        "prompt": (
            "以《秋夜归人》为题写一首七言绝句。要求：四句，每句七字；押平水韵平声韵，一韵到底；"
            "首句可入韵可不入；至少一个意象来自「灯 / 雨 / 门 / 衣」；"
            "不要出现「愁」「孤独」「思念」等直说情绪的词。只输出诗题与四句诗。"
        ),
        "constraints": [
            "七言绝句四句",
            "平水韵一韵到底",
            "意象来自灯/雨/门/衣",
            "不直说情绪词",
        ],
        "rubric": {
            "lines": [4, 4],
            "chars": [28, 40],
            "checks": [
                {"id": "image", "label": "指定意象", "kind": "any",
                 "terms": ["灯", "雨", "门", "衣"]},
                {"id": "no_direct", "label": "不直说情绪", "kind": "none",
                 "terms": ["愁", "孤独", "思念", "悲伤", "寂寞"]},
                {"id": "title", "label": "有诗题", "kind": "any",
                 "terms": ["秋夜归人"]},
            ],
        },
    },
    {
        "id": "poetry-modern",
        "medium": "poetry",
        "stage": "C",
        "prompt": (
            "以《旧车站》为题写一首现代诗，12–20 行。要求：不用「灵魂」「远方」「时光」等大词；"
            "至少出现三个可被看见的具体物件；行与行之间用换行制造停顿；"
            "结尾不要总结，留一个动作或画面。只输出诗。"
        ),
        "constraints": [
            "12–20 行",
            "不用灵魂/远方/时光等大词",
            "至少三个具体物件",
            "结尾落在动作或画面上",
        ],
        "rubric": {
            "lines": [12, 20],
            "chars": [150, 700],
            "checks": [
                {"id": "objects", "label": "具体物件", "kind": "any",
                 "terms": ["站台", "长椅", "时刻表", "铁轨", "售票窗", "行李", "广播", "雨棚"]},
                {"id": "no_big", "label": "不用大词", "kind": "none",
                 "terms": ["灵魂", "远方", "时光", "永恒", "岁月"]},
                {"id": "ending", "label": "结尾动作/画面", "kind": "regex",
                 "pattern": r"[。！？…]\s*$"},
            ],
        },
    },
    {
        "id": "prose-narrative",
        "medium": "prose",
        "stage": "D",
        "prompt": (
            "写一篇 700–1000 字的叙事散文：写一次搬家，重点是父亲把一件旧家具搬下楼的过程。"
            "要求：第一人称；至少两处可验证的细节（年份、楼层、物件尺寸、声音）；"
            "不直接说「我很感动」；结尾停在一个具体动作上。"
        ),
        "constraints": [
            "700–1000 字",
            "第一人称",
            "至少两处可验证细节",
            "不直说感动，结尾落在动作",
        ],
        "rubric": {
            "chars": [700, 1000],
            "checks": [
                {"id": "first_person", "label": "第一人称", "kind": "any",
                 "terms": ["我", "我们"]},
                {"id": "detail", "label": "可验证细节", "kind": "any",
                 "terms": ["楼", "层", "年", "厘米", "米", "声音", "吱", "灰"]},
                {"id": "action_end", "label": "结尾动作", "kind": "regex",
                 "pattern": r"[。…]\s*$"},
            ],
        },
    },
    {
        "id": "prose-argument",
        "medium": "prose",
        "stage": "D",
        "prompt": (
            "写一篇 700–1000 字的议论散文，题目《慢一点，未必是坏事》。"
            "要求：开头用一个具体场景切入而不是名言；提出一个可被反驳的中心论点；"
            "至少一个反例并回应；结尾给出一个可执行的小建议，不用口号。"
        ),
        "constraints": [
            "700–1000 字",
            "场景开头、不用名言",
            "中心论点可被反驳",
            "含反例与回应",
            "结尾给可执行建议",
        ],
        "rubric": {
            "chars": [700, 1000],
            "checks": [
                {"id": "thesis", "label": "中心论点", "kind": "any",
                 "terms": ["我认为", "在我看来", "论点", "未必", "并不是"]},
                {"id": "counter", "label": "反例与回应", "kind": "all",
                 "terms": ["反例", "但是", "然而"]},
                {"id": "action", "label": "可执行建议", "kind": "any",
                 "terms": ["可以", "试着", "每天", "第一步", "具体做法"]},
            ],
        },
    },
]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def list_cases(medium: str = "") -> list[dict]:
    """返回内置用例的深拷贝，调用方改 rubric 不会污染全局。"""
    out = []
    for case in CASES:
        if medium and case["medium"] != medium:
            continue
        out.append(json.loads(json.dumps(case, ensure_ascii=False)))
    return out


def _case_by_id(case_id: str) -> dict | None:
    for case in CASES:
        if case["id"] == case_id:
            return case
    return None


def _messages(case: dict, skill_text: str = "") -> list[dict]:
    constraints = "\n".join(f"- {c}" for c in case.get("constraints") or [])
    system = (
        "你是中文文学创作评测中的执笔者。只输出作品本身，不解释思路，不列提纲。\n"
        f"文体：{medium_mod.label(case['medium'])}；阶段：{case.get('stage', '')}。\n"
        "必须逐条满足下列约束：\n" + (constraints or "- 无额外约束")
    )
    if skill_text:
        system += "\n\n以下是本阶段应遵循的技能要点（只作为方法参考，不照抄其中示例）：\n" + skill_text
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": case["prompt"]},
    ]


def run_case(case: dict, skill_text: str = "", cfg=None) -> dict:
    """跑单个用例；LLM 空输出时 ok=False 但不抛，保证离线测试可完成。"""
    if not isinstance(case, dict) or not case.get("id"):
        return {"case_id": "", "output": "", "ok": False, "error": "用例非法"}
    try:
        raw = llm.chat(_messages(case, skill_text), cfg=cfg)
    except Exception as exc:  # LLM 层任何异常都不应打断评测
        return {"case_id": case["id"], "output": "", "ok": False,
                "error": f"模型调用失败：{exc.__class__.__name__}"}
    output = raw if isinstance(raw, str) else ""
    output = output.strip()
    return {
        "case_id": case["id"],
        "output": output,
        "ok": bool(output),
        "error": "" if output else "模型未返回内容（已降级为启发式评分）",
    }


# ---------------------------------------------------------------- 打分

def _clamp(x: float) -> int:
    return int(max(0, min(100, round(x))))


def _ngram_repeat(text: str) -> tuple[int, str]:
    """统计重复 4-gram 的额外次数；返回 (extra, 最常见片段)。"""
    chars = re.sub(r"[\s\W_]+", "", text)
    if len(chars) < 8:
        return 0, ""
    counts: dict[str, int] = {}
    for i in range(len(chars) - 3):
        gram = chars[i:i + 4]
        counts[gram] = counts.get(gram, 0) + 1
    extra = sum(c - 2 for c in counts.values() if c > 2)
    top = max(counts.items(), key=lambda kv: kv[1]) if counts else ("", 1)
    return extra, top[0] if top[1] > 2 else ""


def _metrics(output: str, case: dict) -> dict:
    text = output or ""
    chars = len(re.sub(r"\s+", "", text))
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    lines = [ln for ln in text.splitlines() if ln.strip()]
    sents = [s for s in re.split(r"[。！？!?；;…]+", text) if s.strip()]
    rubric = case.get("rubric") or {}
    target = rubric.get("chars") or []

    len_score = 0.0
    if target and len(target) == 2:
        lo, hi = int(target[0]), int(target[1])
        if lo <= chars <= hi:
            len_score = 100.0
        elif chars < lo:
            len_score = 100.0 * chars / max(lo, 1)
        else:
            len_score = max(20.0, 100.0 - (chars - hi) / max(hi, 1) * 60.0)
    elif chars:
        len_score = 70.0

    line_target = rubric.get("lines") or []
    if line_target and len(line_target) == 2:
        lo, hi = int(line_target[0]), int(line_target[1])
        if lo <= len(lines) <= hi:
            line_score = 100.0
        elif len(lines) < lo:
            line_score = 100.0 * len(lines) / max(lo, 1)
        else:
            line_score = max(30.0, 100.0 - (len(lines) - hi) * 8.0)
    else:
        line_score = 100.0 if lines else 0.0

    slop_hits = [w for w in _SLOP_WORDS if w in text]
    hedge = len(_HEDGE_RE.findall(text))
    rep_extra, rep_top = _ngram_repeat(text)
    dialogue_lines = len(_DIALOGUE_RE.findall(text))
    scene_heads = len(_SCENE_HEAD_RE.findall(text))
    quote_marks = text.count("“") + text.count("「")

    checks = rubric.get("checks") or []
    passed: list[str] = []
    failed: list[str] = []
    for check in checks:
        kind = check.get("kind") or "any"
        terms = [str(t) for t in check.get("terms") or []]
        ok = False
        if kind == "any":
            ok = any(t in text for t in terms)
        elif kind == "all":
            ok = bool(terms) and all(t in text for t in terms)
        elif kind == "none":
            ok = not any(t in text for t in terms)
        elif kind == "regex" and check.get("pattern"):
            try:
                ok = re.search(str(check["pattern"]), text) is not None
            except re.error:
                ok = False
        (passed if ok else failed).append(str(check.get("label") or check.get("id") or ""))
    check_count = len(checks)
    check_ratio = len(passed) / check_count if check_count else 0.5

    # 结构分：不同文体看不同的「可观察骨架」
    med = case.get("medium")
    if med == "script":
        struct = 25 + 25 * min(scene_heads, 2) / 2 + 25 * min(dialogue_lines, 6) / 6
        struct += 25 * min(len(paras), 3) / 3
        struct = min(100.0, struct)
        format_score = _clamp(20 + 40 * min(scene_heads, 1) + 20 * min(dialogue_lines, 4) / 4
                              + 20 * (1 if len(lines) >= 6 else 0))
        dialogue_score = _clamp(20 + 45 * min(dialogue_lines, 8) / 8
                                + 20 * min(check_ratio, 1) + 15 * (1 if quote_marks else 0))
    elif med == "poetry":
        struct = 45 * min(line_score / 100.0, 1.0) + 35 * min(check_ratio, 1)
        struct += 20 * (1 if lines and len(lines) <= 40 else 0)
        format_score = _clamp(line_score)
        dialogue_score = 0
    else:
        para_score = min(len(paras), 5) / 5
        sent_var = 0.0
        if len(sents) >= 3:
            lengths = [len(s) for s in sents]
            mean = sum(lengths) / len(lengths)
            var = sum((x - mean) ** 2 for x in lengths) / len(lengths)
            sent_var = min(1.0, var / 400.0)
        struct = 30 + 25 * para_score + 20 * min(check_ratio, 1) + 15 * sent_var
        if med == "novel":
            struct += 10 * (1 if quote_marks else 0)
        struct = min(100.0, struct)
        format_score = _clamp(40 + 30 * para_score + 30 * (1 if lines else 0))
        dialogue_score = _clamp(20 + 40 * min(dialogue_lines, 6) / 6 + 20 * quote_marks / 2
                                + 20 * min(check_ratio, 1))

    specificity = 0.0
    specificity += 0.4 if re.search(r"\d", text) else 0.0
    specificity += 0.3 if re.search(r"[「“\"].{2,40}[」”\"]", text) else 0.0
    specificity += 0.3 if re.search(r"[A-Za-z]{2,}|[\u4e00-\u9fa5]{2,}路|[\u4e00-\u9fa5]{2,}街", text) else 0.0
    specificity = min(1.0, specificity)

    hook = 0.0
    tail = text[-120:]
    if re.search(r"[？?！!…]\s*$", tail):
        hook += 0.5
    if re.search(r"[。]\s*$", tail) and len(sents) >= 3:
        hook += 0.3
    if any(k in tail for k in ("突然", "却", "没想到", "这时", "直到", "敲门", "电话")):
        hook += 0.2
    hook = min(1.0, hook)

    language = 92.0 - len(slop_hits) * 7 - min(hedge, 6) * 1.5 - min(rep_extra, 12) * 2.5
    if len(sents) >= 3:
        language += 3
    language = max(20.0, min(100.0, language))

    return {
        "chars": chars,
        "lines": len(lines),
        "paras": len(paras),
        "len_score": len_score,
        "line_score": line_score,
        "struct": struct,
        "format_score": format_score,
        "dialogue_score": dialogue_score,
        "check_ratio": check_ratio,
        "checks_passed": passed,
        "checks_failed": failed,
        "slop_hits": slop_hits,
        "hedge": hedge,
        "rep_extra": rep_extra,
        "rep_top": rep_top,
        "specificity": specificity,
        "hook": hook,
        "language": language,
        "dialogue_lines": dialogue_lines,
        "scene_heads": scene_heads,
    }


def _heuristic_dims(case: dict, m: dict) -> dict[str, int]:
    med = case.get("medium")
    chk = m["check_ratio"]
    spec = m["specificity"]
    hook = m["hook"]
    lang = m["language"]
    struct = m["struct"]
    if med == "novel":
        return {
            "结构": _clamp(0.7 * struct + 30 * chk),
            "因果可信": _clamp(45 + 25 * chk + 20 * spec - 3 * len(m["slop_hits"])),
            "人物": _clamp(45 + 25 * min(m["dialogue_lines"], 4) / 4 + 20 * chk
                         + 10 * (1 if m["dialogue_lines"] else 0)),
            "连续性": _clamp(50 + 20 * spec + 20 * chk - 2 * m["rep_extra"]),
            "语言": _clamp(lang),
            "读者体验": _clamp(0.45 * m["len_score"] + 45 * hook + 10 * chk),
        }
    if med == "script":
        return {
            "前提": _clamp(45 + 45 * chk + 10 * spec),
            "结构": _clamp(struct),
            "人物": _clamp(50 + 25 * min(m["dialogue_lines"], 6) / 6 + 25 * chk),
            "对白": _clamp(m["dialogue_score"]),
            "格式": _clamp(m["format_score"]),
            "可行性": _clamp(50 + 25 * min(m["scene_heads"], 2) / 2 + 25 * (1 if m["paras"] >= 2 else 0)),
        }
    if med == "poetry":
        return {
            "意象": _clamp(45 + 30 * spec + 25 * chk),
            "章法": _clamp(0.6 * struct + 40 * chk),
            "声律": _clamp(55 + 25 * m["line_score"] / 100 + 20 * chk),
            "情感": _clamp(45 + 30 * hook + 25 * chk - 2 * len(m["slop_hits"])),
        }
    return {
        "表达": _clamp(lang),
        "结构": _clamp(0.7 * struct + 30 * chk),
        "真实度": _clamp(45 + 25 * spec + 25 * chk - 2 * len(m["slop_hits"])),
        "音律": _clamp(50 + 25 * min(m["paras"], 4) / 4 + 25 * chk),
    }


def _notes(case: dict, m: dict, empty: bool) -> list[str]:
    notes = ["LLM 评分不可用，使用确定性启发式（字数/结构/AI 腔/重复/约束）"]
    if empty:
        notes.append("输出为空，按 0 分处理")
        return notes
    target = (case.get("rubric") or {}).get("chars") or []
    if target:
        notes.append(f"字数 {m['chars']}，目标区间 {target[0]}–{target[1]}")
    if m["checks_failed"]:
        notes.append("未达成约束：" + "、".join(m["checks_failed"][:4]))
    if m["slop_hits"]:
        notes.append("命中 AI 腔词：" + "、".join(m["slop_hits"][:4]))
    if m["rep_top"]:
        notes.append(f"重复片段「{m['rep_top']}」出现 {m['rep_extra']} 次冗余")
    if m["hedge"] >= 4:
        notes.append(f"程度副词偏多（{m['hedge']} 处），语气发虚")
    if not m["dialogue_lines"] and case.get("medium") in ("script", "novel"):
        notes.append("缺少可辨识的对白行")
    return notes[:8]


def score_output(case: dict, output: str, cfg=None) -> dict:
    """优先 LLM JSON 评分；失败/空输出走确定性启发式。始终返回 0–100。"""
    if not isinstance(case, dict) or not case.get("id"):
        return {"total": 0, "dims": {}, "notes": ["用例非法"], "fallback": True}
    text = output if isinstance(output, str) else ""
    dims_spec = medium_mod.score_dims(case.get("medium", "novel"))
    allowed = {name: weight for name, weight in dims_spec}

    if text.strip():
        try:
            prompt = (
                "请按下列维度给这份中文作品打分（0–100 的整数），只输出 JSON："
                '{"dims": {"维度名": 分数}, "notes": ["不超过三条中文短评"]}。\n'
                "维度与权重：" + "、".join(f"{n}({w})" for n, w in dims_spec) +
                "\n约束：" + "；".join(case.get("constraints") or []) +
                "\n\n作品：\n" + text[:8000]
            )
            data = llm.chat_json([
                {"role": "system", "content": "你是严格但公正的文学编辑，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ], cfg=cfg)
            if isinstance(data, dict) and isinstance(data.get("dims"), dict):
                got = {}
                for name in allowed:
                    try:
                        got[name] = _clamp(float(data["dims"][name]))
                    except (KeyError, TypeError, ValueError):
                        continue
                if len(got) >= max(1, len(allowed) // 2):
                    total_w = sum(allowed[n] for n in got)
                    total = sum(got[n] * allowed[n] for n in got) / max(total_w, 1)
                    notes = [str(x)[:120] for x in (data.get("notes") or [])][:5]
                    return {"total": _clamp(total), "dims": got, "notes": notes,
                            "fallback": False}
        except Exception:
            pass

    m = _metrics(text, case)
    dims = _heuristic_dims(case, m) if text.strip() else {n: 0 for n in allowed}
    total_w = sum(allowed.values()) or 1
    total = sum(dims.get(n, 0) * w for n, w in allowed.items()) / total_w
    return {
        "total": _clamp(total),
        "dims": dims,
        "notes": _notes(case, m, not text.strip()),
        "fallback": True,
    }


# ---------------------------------------------------------------- 比较与留档

def _selected(case_ids) -> list[dict]:
    if not case_ids:
        return list(CASES)
    wanted = {str(x) for x in case_ids}
    return [c for c in CASES if c["id"] in wanted]


def compare(baseline_text: str, candidate_text: str, case_ids=None, cfg=None) -> dict:
    """逐案跑 baseline / candidate，返回均值、胜平负与晋升建议。

    晋升规则：候选均值 ≥ baseline + 2，且没有任何单案下降超过 8 分。
    baseline 传空串表示「没有旧技能」，此时只看候选绝对分与稳定性。
    """
    cases = _selected(case_ids)
    if not cases:
        return {**_empty_compare(), "reason": "没有匹配的评测用例"}
    details = []
    win = tie = loss = 0
    drops: list[float] = []
    fallback = False
    for case in cases:
        b_run = run_case(case, baseline_text or "", cfg=cfg)
        c_run = run_case(case, candidate_text or "", cfg=cfg)
        b_score = score_output(case, b_run["output"], cfg=cfg)
        c_score = score_output(case, c_run["output"], cfg=cfg)
        fallback = fallback or bool(b_score["fallback"]) or bool(c_score["fallback"])
        delta = round(c_score["total"] - b_score["total"], 1)
        if delta > 0.5:
            verdict = "win"
            win += 1
        elif delta < -0.5:
            verdict = "loss"
            loss += 1
        else:
            verdict = "tie"
            tie += 1
        if delta < 0:
            drops.append(abs(delta))
        details.append({
            "case_id": case["id"],
            "medium": case["medium"],
            "stage": case.get("stage", ""),
            "baseline": b_score,
            "candidate": c_score,
            "delta": delta,
            "verdict": verdict,
            "baseline_ok": b_run["ok"],
            "candidate_ok": c_run["ok"],
        })

    baseline_avg = round(sum(d["baseline"]["total"] for d in details) / len(details), 2)
    candidate_avg = round(sum(d["candidate"]["total"] for d in details) / len(details), 2)
    delta = round(candidate_avg - baseline_avg, 2)
    worst_drop = round(max(drops), 1) if drops else 0.0
    has_baseline = bool((baseline_text or "").strip())
    recommend = bool(delta >= 2 and worst_drop <= 8)
    if recommend:
        prefix = "候选均值" if has_baseline else "候选相对无技能基线"
        reason = f"{prefix} +{delta}，最大单案下降 {worst_drop}，达到晋升线"
    elif delta < 2:
        reason = f"候选均值仅 {delta:+}，未达到 +2 的晋升线"
    else:
        reason = f"存在单案下降 {worst_drop} 分，超过允许的 8 分"

    return {
        "baseline_avg": baseline_avg,
        "candidate_avg": candidate_avg,
        "delta": delta,
        "win": win,
        "tie": tie,
        "loss": loss,
        "worst_drop": worst_drop,
        "cases": details,
        "recommend": recommend,
        "has_baseline": has_baseline,
        "fallback": fallback,
        "reason": reason,
        "ts": _now(),
    }


def _empty_compare() -> dict:
    return {
        "baseline_avg": 0.0, "candidate_avg": 0.0, "delta": 0.0,
        "win": 0, "tie": 0, "loss": 0, "worst_drop": 0.0, "cases": [],
        "recommend": False, "has_baseline": False, "fallback": True,
        "reason": "无可用用例", "ts": _now(),
    }


def _safe_id(value: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))[:40]
    return s or secrets.token_hex(4)


def save_run(run: dict) -> str:
    """保存到 data/evals/<ts>-<id>.json；文件名白名单清洗，失败返回空串。"""
    if not isinstance(run, dict):
        return ""
    rid = _safe_id(run.get("id") or run.get("candidate_id") or "run")
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = f"{ts}-{rid}-{secrets.token_hex(2)}.json"
    try:
        os.makedirs(EVAL_DIR, exist_ok=True)
        path = safe_join(EVAL_DIR, name, label="评测文件名")
        try:
            payload = json.dumps(run, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            payload = json.dumps(run, ensure_ascii=False, indent=2, default=str)
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
        return path
    except Exception:
        return ""


def list_runs(limit: int = 20) -> list[dict]:
    """按时间倒序列出评测留档；坏文件跳过。"""
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 20
    try:
        names = [n for n in os.listdir(EVAL_DIR) if n.endswith(".json")]
    except OSError:
        return []
    names.sort(reverse=True)
    out: list[dict] = []
    for name in names[:limit]:
        path = os.path.join(EVAL_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data.setdefault("saved_at", name[:15])
            data["path"] = path
            out.append(data)
    return out
