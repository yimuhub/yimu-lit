"""
自然语言意图理解层（NLU v2）
将用户在聊天框输入的任意自然语言提炼为结构化意图，再由路由分发器匹配到可执行操作。

设计：
  1. 优先用 LLM（llm.chat_json）做语义理解——任意自然语言均可解析
  2. LLM 不可用或返回异常时，自动降级到规则匹配
  3. 规则匹配覆盖全部四种文体（novel/script/poetry/prose）

调用入口：
  from bot.nlu import parse_intent
  result = parse_intent(text, project_status=None)
  # result = {"intent": "start_writing", "confidence": 0.95, "params": {...}, "source": "llm"|"rule", "raw": text}
"""

import json
import re
import datetime

# ── 意图定义 ──────────────────────────────────────────────

INTENTS = {
    "start_writing":     "启动或继续创作（全部章节 / 整本 / 开写）",
    "start_writing_all": "一次性写完全部章节（不等逐章确认）",
    "rewrite_chapter":   "重写 / 修改指定章节",
    "resume_writing":    "继续 / 续写 / 接着写下一章",
    "batch_writing":     "批量写 N 章 / 写完全部 / 再写 N 章",
    "pause_writing":     "暂停写作",
    "query_status":      "查询进度 / 看看写到哪了",
    "set_title":         "修改标题或书名",
    "new_project":       "新建作品 / 重新开始",
    "set_style":         "调整风格 / 语调 / 人称 / 叙事视角",
    "add_constraint":    "追加创作要求 / 设定限制（融入后续创作）",
    "query_outline":     "查看大纲 / 目录 / 章节列表",
    "export_book":       "导出 / 下载成书",
    "creative_hint":     "普通创作建议或修改指令（并入创作）",
    "unknown":           "无法识别",
}

# ── LLM Prompt ────────────────────────────────────────────

_NLU_SYSTEM_PROMPT = """你是一个自然语言意图理解引擎。用户在文学创作系统的聊天框中输入消息，你需要将其提炼为结构化意图。

可用意图（intent 字段只填以下之一）：
- start_writing_all: 用户想一次性写完全部章节/整本书（如"全部章节创作""把整本书写完""一键全部写"）
- start_writing: 用户想启动创作（但未明确说全部，如"开始写""开始创作""帮我写"）
- resume_writing: 继续或续写下一章（如"继续""接着写""下一章""往下写"）
- batch_writing: 批量写N章（如"再写10章""往下写20章""再写50章"）或全部写完（"写完全部""全部写完""把所有章节写完""一键全部写"）
- rewrite_chapter: 重写或修改指定章节（如"第3章重写""第三章改一下""重写第12章"）
- pause_writing: 暂停写作（如"暂停""先停一下""别写了"）
- query_status: 查询进度（如"写到哪了""进度怎么样""现在几章了"）
- set_title: 修改标题或书名（如"改名叫XX""标题改成XX""书名换成XX"）
- new_project: 新建作品（如"新建一本""重新开始""换个题材"）
- set_style: 调整写作风格（如"语气幽默一点""用第一人称""改成悬疑风格""文风再文艺些"）
- add_constraint: 追加创作要求或设定（如"主角要叫张三""增加一个反派""背景设定在唐朝"）
- query_outline: 查看大纲或目录（如"看看大纲""章节列表""目录是什么"）
- export_book: 导出成书（如"导出""下载""生成PDF""打包"）
- creative_hint: 不属于以上任何操作的普通创作建议或修改意见

返回 JSON（仅返回 JSON，不要任何其他文字）：
{
  "intent": "意图名称",
  "confidence": 0.0到1.0的置信度,
  "params": {
    "chapter_number": 章号数字（仅 rewrite_chapter 时）,
    "title": 标题文本（仅 set_title 时）,
    "style_desc": 风格描述（仅 set_style 时）,
    "constraint_text": 约束内容（仅 add_constraint 时）
  }
}

规则：
1. 只返回 JSON，不要 markdown 代码块标记
2. confidence < 0.6 表示不确定
3. 如果消息含章号（"第N章""第N回"），rewrite_chapter 的优先级高于 start_writing
4. "全部""整本""所有章节""从头到尾" → start_writing_all
5. "继续""接着""下一章""往下" → resume_writing
"""

# ── 规则匹配（LLM 降级方案） ──────────────────────────────

# start_writing_all: 一次性全写
_PAT_ALL = re.compile(
    r"全部.*?(创作|写|完)|整本.*?写|全书.*?写|所有章节.*?(写|创作)|"
    r"从头到尾.*?写|一次.*?(写完|全写)|批量.*?写|全部章节|"
    r"请.*?(写完|创作完|全部.*?创作)|一气呵成|一口气.*?写",
    re.I,
)

# start_writing: 启动创作（非全部）
_PAT_START = re.compile(
    r"开始.*?(写|创作)|开写|启动.*?创作|进入.*?创作|"
    r"生成.*?(正文|内容)|写.*?正文|动手.*?写|"
    r"(剧本|诗词|散文|小说|诗歌).{0,6}(创作|来一部|来一个)|"
    r"写.{0,4}(正文|内容)",
    re.I,
)

# batch_writing: 批量写N章 / 全部写完
_PAT_BATCH = re.compile(
    r"(再写|继续写|往下写|接着写|接下来写|然后写).*?(\d+|[零一二三四五六七八九十百千两]+).*?(章|回|场)",
    re.I,
)
_PAT_BATCH_ALL = re.compile(
    r"(写完全部|全部写完|把所有.*?写完|全部章节写完|写完所有|全部写完|一次性写完)",
    re.I,
)

# resume_writing: 续写
_PAT_RESUME = re.compile(
    r"继续|续写|接着写|写下去|往下写|下一章|下一回|接着.*?写|"
    r"继续写|往下|接下去|再写.*?(章|回)",
    re.I,
)

# rewrite_chapter: 重写指定章节（支持阿拉伯数字和中文数字）
_CH_NUM = r"(\d+|[零一二三四五六七八九十百千两]+)"
_PAT_REWRITE = re.compile(
    r"(第\s*" + _CH_NUM + r"\s*[章回节话].*?(重写|重新写|再写|改写|改|修改|修订|重做|调整|换))"
    r"|((重写|重新写|再写|改写|改|修改|修订|重做|调整|换).*?第\s*" + _CH_NUM + r"\s*[章回节话])",
    re.I,
)

# 中文数字转int
_CN_DIGIT = {"零":0,"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"两":2}
_CN_UNIT = {"十":10,"百":100,"千":1000}

def _cn_to_int(s):
    """将中文数字字符串转为 int，失败返回 None。"""
    if s.isdigit():
        return int(s)
    if not s:
        return None
    result = 0
    current = 0
    for ch in s:
        if ch in _CN_DIGIT:
            current = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if current == 0:
                current = 1
            result += current * unit
            current = 0
        else:
            return None
    result += current
    return result if result > 0 else None

# pause
_PAT_PAUSE = re.compile(r"暂停|停一下|先停|别写了|停止.*?写|halt|pause", re.I)

# query_status
_PAT_STATUS = re.compile(
    r"写到哪|进度|怎么样了|几章了|完成.*?多少|状态|看看.*?(进度|状态)|"
    r"现在.*?(写到|完成)|还剩.*?(章|多少)",
    re.I,
)

# set_title
_PAT_TITLE = re.compile(
    r"(改名叫|改名|标题改|书名改|名称改|名字改|改.*?标题|改.*?书名|"
    r"标题.*?换成|书名.*?换成|改名为|重命名)",
    re.I,
)

# new_project
_PAT_NEW = re.compile(
    r"新建.*?(作品|书|一本)|重新开始|换个.*?(题材|书|作品)|"
    r"另写.*?(本|部|篇)|再来(一本|一部|一篇)|再写(一本|一部|一篇)|"
    r"开.*?新书|创建.*?(作品|书)",
    re.I,
)

# new_project（文体+内容关键词：如"剧本 写一个短片""诗词 写一首七律""散文 创作一篇"）
_PAT_NEW_MEDIUM = re.compile(
    r"(剧本|诗词|散文|诗歌|小说|七律|五律|绝句|词牌|现代诗|短剧|舞台剧|电影剧本|话剧)"
    r".{0,8}(写|创作|来|弄|搞|做|出一个|一首|一篇|一段|一部)"
    r"|"
    r"(写|创作|来|弄|搞|做|出一个|一首|一篇|一段|一部)"
    r".{0,8}(剧本|诗词|散文|诗歌|七律|五律|绝句|词牌|现代诗|短剧|舞台剧|电影剧本|话剧)",
    re.I,
)

# set_style
_PAT_STYLE = re.compile(
    r"风格|语气|人称|视角|文风|叙事|笔法|调子|基调|"
    r"(幽默|严肃|轻松|文艺|口语化|正式|悬疑|浪漫|纪实).*?(一点|些|风格|调)",
    re.I,
)

# query_outline
_PAT_OUTLINE = re.compile(
    r"(查看|看看|看一下|显示).*(大纲|目录|章节.*?列表|结构|框架)|"
    r"大纲.*?(是什么|看看|显示)|目录.*?(是什么|看看|显示)",
    re.I,
)

# export
_PAT_EXPORT = re.compile(
    r"导出|下载|生成.*?(pdf|文档)|打包|保存.*?文件|成书",
    re.I,
)

# add_constraint: 追加设定
_PAT_CONSTRAINT = re.compile(
    r"主角.*?(叫|是|名字)|增加.*?(角色|人物|设定|反派|主角)|"
    r"背景.*?(设定|在|放到)|设定.*?(在|为)|"
    r"加入.*?(元素|设定|角色|情节)|要求.*?(加入|增加|设定)|"
    r"需要.*?(加入|增加|设定)|必须.*?(有|包含|加入)",
    re.I,
)


def _rule_parse(text):
    """规则匹配降级方案。返回 (intent, params, confidence)。"""
    t = (text or "").strip()
    if not t:
        return "unknown", {}, 0.0

    # rewrite_chapter 优先（因为含章号）
    m = _PAT_REWRITE.search(t)
    if m:
        # 提取章号（支持阿拉伯数字和中文数字）
        nums = re.findall(r"第\s*" + _CH_NUM + r"\s*[章回节话]", t)
        ch_no = None
        if nums:
            ch_no = _cn_to_int(nums[0])
        return "rewrite_chapter", {"chapter_number": ch_no}, 0.85

    # batch_writing: 再写N章 / 写完全部（优先于 start_writing_all 的泛化匹配）
    m = _PAT_BATCH.search(t)
    if m:
        nums = re.findall(r"(\d+|[零一二三四五六七八九十百千两]+)", t)
        batch = None
        for n in nums:
            val = _cn_to_int(n)
            if val and val > 0 and val < 10000:
                batch = val
                break
        return "batch_writing", {"batch_size": batch or "1"}, 0.82
    if _PAT_BATCH_ALL.search(t):
        return "batch_writing", {"batch_size": "all"}, 0.82

    if _PAT_ALL.search(t):
        return "start_writing_all", {}, 0.85

    # new_project 优先于 start_writing（文体+内容关键词先匹配）
    if _PAT_NEW_MEDIUM.search(t):
        return "new_project", {}, 0.80
    if _PAT_NEW.search(t):
        return "new_project", {}, 0.75

    if _PAT_RESUME.search(t):
        return "resume_writing", {}, 0.80
    if _PAT_START.search(t):
        return "start_writing", {}, 0.75
    if _PAT_PAUSE.search(t):
        return "pause_writing", {}, 0.85
    if _PAT_STATUS.search(t):
        return "query_status", {}, 0.80
    if _PAT_TITLE.search(t):
        # 提取标题
        title_match = re.search(r"(?:改名叫|改名|标题改|书名改|改名为|重命名|换成)\s*[「「『\"]?(.+?)[」」』\"]?$", t)
        title = title_match.group(1).strip() if title_match else ""
        return "set_title", {"title": title}, 0.75
    if _PAT_NEW.search(t):
        return "new_project", {}, 0.75
    if _PAT_OUTLINE.search(t):
        return "query_outline", {}, 0.80
    if _PAT_EXPORT.search(t):
        return "export_book", {}, 0.80

    # 第N章 + 额外反馈（无显式「重写/修改」动词）→ 也按局部重写处理，
    # 如「第十二章节奏慢了」；纯查询（看看/查看第3章）不在此列。
    m_ch = re.search(r"第\s*" + _CH_NUM + r"\s*[章回节话]", t)
    if m_ch and not re.match(r"^(看看|查看|看一下|显示|预览|打开|读|讲|翻)", t):
        rest = t.replace(m_ch.group(0), "", 1).strip(" ，。,.！!？?")
        if rest and not re.search(r"大纲|目录|列表|结构|框架|导出|下载|进度|状态", rest):
            ch_no = _cn_to_int(m_ch.group(1))
            return "rewrite_chapter", {"chapter_number": ch_no}, 0.75

    if _PAT_STYLE.search(t):
        return "set_style", {"style_desc": t}, 0.70
    if _PAT_CONSTRAINT.search(t):
        return "add_constraint", {"constraint_text": t}, 0.65

    # 兜底：如果消息较长且有创作相关词，当作 creative_hint
    if len(t) > 5 and re.search(
        r"写|创作|故事|情节|人物|角色|章节|内容|对话|描写|叙述|节奏|"
        r"开头|结尾|过渡|伏笔|悬念|冲突|高潮|转折|情感|心理|"
        r"环境|场景|细节|修辞|文采|笔触|张力|氛围",
        t,
    ):
        return "creative_hint", {}, 0.50

    return "unknown", {}, 0.0


# ── LLM 意图理解 ──────────────────────────────────────────

def _llm_parse(text):
    """用 LLM 做自然语言意图理解。返回 (intent, params, confidence) 或 None。"""
    try:
        from . import llm
    except ImportError:
        try:
            import bot.llm as llm
        except ImportError:
            return None

    messages = [
        {"role": "system", "content": _NLU_SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    try:
        raw = llm.chat_json(
            messages,
            temperature=0.1,
            stage="nlu",
            agent="nlu_parser",
            use_cache=True,
        )
    except Exception:
        return None

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            # 尝试提取 JSON 块
            m = re.search(r"\{[\s\S]*\}", raw)
            if m:
                try:
                    raw = json.loads(m.group(0))
                except json.JSONDecodeError:
                    return None
            else:
                return None

    if not isinstance(raw, dict):
        return None

    intent = str(raw.get("intent", "")).strip()
    if intent not in INTENTS:
        # 尝试模糊匹配
        for key in INTENTS:
            if key in intent or intent in key:
                intent = key
                break
        else:
            return None

    confidence = float(raw.get("confidence", 0.5))
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    # 章号整数化
    if "chapter_number" in params and params["chapter_number"]:
        try:
            params["chapter_number"] = int(params["chapter_number"])
        except (ValueError, TypeError):
            params["chapter_number"] = None

    return intent, params, confidence


# ── 公开接口 ──────────────────────────────────────────────

def parse_intent(text, project_status=None):
    """
    解析用户输入的自然语言，返回结构化意图。

    参数：
        text: 用户原始输入文本
        project_status: 可选，当前作品状态 dict（如 {"state": "in_progress", "medium": "novel"}）
                       用于上下文消歧（例如"继续"在 idle 时可能意图是 start_writing）

    返回：
        {
            "intent": str,        # 意图名称
            "confidence": float,  # 0.0-1.0
            "params": dict,       # 意图参数
            "source": str,        # "llm" 或 "rule"
            "raw": str,           # 原始输入
        }
    """
    text = (text or "").strip()
    if not text:
        return {"intent": "unknown", "confidence": 0.0, "params": {}, "source": "rule", "raw": text}

    # 优先 LLM
    llm_result = _llm_parse(text)
    if llm_result and llm_result[2] >= 0.6:
        intent, params, conf = llm_result
        return {"intent": intent, "confidence": conf, "params": params, "source": "llm", "raw": text}

    # 降级规则
    intent, params, conf = _rule_parse(text)

    # 上下文消歧
    if project_status:
        state = str(project_status.get("state", "")).lower()
        # idle 状态下"继续"应理解为 start_writing（还没开始写，继续 = 开始）
        # 但如果明确说了"下一章""续写"等，保持 resume_writing
        if intent == "resume_writing" and state in ("idle", "", "awaiting_title"):
            if not re.search(r"下一章|下一回|续写|接着写|往下写|再写", text):
                intent = "start_writing"
                conf = max(conf, 0.70)
        # 已经在写，"开始写"理解为 resume
        if intent == "start_writing" and state in ("in_progress", "paused"):
            intent = "resume_writing"
            conf = max(conf, 0.75)
        # 已经在写且全部完成，"继续"理解为 start_writing_all（重新审视未完成项）
        if intent == "resume_writing" and state == "completed":
            intent = "start_writing_all"
            conf = max(conf, 0.70)

    return {"intent": intent, "confidence": conf, "params": params, "source": "rule", "raw": text}


def format_intent_result(parsed):
    """格式化意图解析结果，用于日志/调试。"""
    return (
        f"[NLU] intent={parsed['intent']} "
        f"confidence={parsed['confidence']:.2f} "
        f"source={parsed['source']} "
        f"params={parsed['params']} "
        f"raw={parsed['raw'][:50]!r}"
    )
