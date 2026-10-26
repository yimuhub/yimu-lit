"""剧本创作流水线（v1.0.0 · 神笔文学创作台）

与小说流水线（bot/writer.py）并列的独立实现，但复用同一套基础设施：
llm 缓存/超时/重试、providers 并发闸、skillkit 最小上下文注入、distill 风格蒸馏。

阶段（S0..S7，与 bot/medium.py 的 script 阶段表一致）：
  S0 引擎      → 前提 / 主题 / 控制思想 / logline / 类型契约
  S1 人物网    → 人物小传 / 反派梯队 / 关系与未结清单
  S2 文档      → 创作圣经（场景总表 / 季弧 / 禁区）
  S3 结构      → 三幕 / 幕点 / 序列 / 单集结构
  S4 场次大纲  → 逐场大纲（目标 / 触发 / 阻碍 / beat / 结果）
  S5 写作      → 逐场正文（场景标题 / 动作行 / 对白）
  S6 修订      → 通读修订 + 删改记录
  S7 交付      → 定稿导出

目录规范（projects/s_<key>/）：
  00-创作圣经.md    01-人物小传.md   02-结构大纲.md
  03-场次清单.md    04-正文/         05-修订记录.md
  06-季弧与分集.md  07-质量报告.md   剧本计划.json
"""
from __future__ import annotations

import datetime
import json
import os
import re
import threading

from . import llm, medium, script_checker, skillkit
from . import writer as novel_writer
from bot.safeio import atomic_write_json, atomic_write_text, safe_join

ROOT = novel_writer.ROOT
PROJECTS_DIR = novel_writer.PROJECTS_DIR

# 剧本项目文件约定
F_BIBLE = "00-创作圣经.md"
F_CHARS = "01-人物小传.md"
F_STRUCT = "02-结构大纲.md"
F_SCENES = "03-场次清单.md"
F_BODY = "04-正文"
F_REVISIONS = "05-修订记录.md"
F_SEASON = "06-季弧与分集.md"
F_QUALITY = "07-质量报告.md"
F_PLAN = "剧本计划.json"

# 媒介形式
FORMS = {
    "film": {"label": "电影长片", "scenes": "场", "unit": "页", "target_pages": 100},
    "series": {"label": "电视剧集", "scenes": "场", "unit": "集", "target_pages": 45},
    "short": {"label": "短片", "scenes": "场", "unit": "分钟", "target_pages": 20},
    "stage": {"label": "舞台剧", "scenes": "场", "unit": "分钟", "target_pages": 90},
    "opera": {"label": "戏曲", "scenes": "场", "unit": "折", "target_pages": 60},
    "custom": {"label": "自定义", "scenes": "场", "unit": "页", "target_pages": 60},
}

# 入口路径
ENTRY_PATHS = {
    "material": "有参考材料（原著/素材/真实事件）",
    "idea": "只有一个想法或题材",
    "adapt": "改编已有作品",
    "revise": "改已有初稿",
    "continue": "续写已有剧本",
}


class ScriptProject:
    """一个剧本项目。字段与小说 Project 对齐，便于前端统一处理。"""

    def __init__(self, path: str, name: str):
        self.path = path
        self.name = name
        self.events: list[str] = []
        self.running = False
        self.lock = threading.RLock()

    def log(self, msg: str) -> None:
        with self.lock:
            line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
            self.events.append(line)
            if len(self.events) > 200:
                self.events.pop(0)
            try:
                lf = os.path.join(self.path, "运行日志.txt")
                with open(lf, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                lines = open(lf, encoding="utf-8").read().splitlines()
                if len(lines) > 300:
                    open(lf, "w", encoding="utf-8").write("\n".join(lines[-300:]) + "\n")
            except Exception:
                pass

    def plan(self) -> dict | None:
        p = os.path.join(self.path, F_PLAN)
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return None

    def save_plan(self, plan: dict) -> None:
        plan["updatedAt"] = datetime.datetime.now().isoformat()
        with self.lock:
            atomic_write_json(os.path.join(self.path, F_PLAN), plan)

    def status(self) -> dict:
        plan = self.plan() or {}
        scenes = plan.get("scenes") or []
        done = sum(1 for s in scenes if s.get("status") == "completed")
        words = sum(s.get("wordCount") or 0 for s in scenes)
        meta = plan.get("meta") or {}
        return {
            "state": plan.get("status", "unknown"),
            "done": done, "total": len(scenes), "words": words,
            "running": self.running, "name": plan.get("scriptName", self.name),
            "medium": "script", "form": plan.get("formLabel") or plan.get("form", ""),
            "stage": meta.get("stage", ""), "paused": bool(meta.get("paused")),
            "setting": meta.get("setting") or {},
            "era_warnings": meta.get("era_warnings") or [],
        }

    def body_dir(self) -> str:
        d = os.path.join(self.path, F_BODY)
        os.makedirs(d, exist_ok=True)
        return d


# ---------------------------------------------------------------- 工具

def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "", name or "")[:40] or "未命名"


def _write(path: str, text: str) -> None:
    atomic_write_text(path, text)


def _read(path: str) -> str:
    try:
        return open(path, encoding="utf-8").read()
    except Exception:
        return ""


def _cfg_defaults() -> dict:
    try:
        cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    except Exception:
        cfg = {}
    return cfg.get("defaults") or {}


def _merge_cfg(cfg: dict | None) -> dict:
    """合并默认配置（沿用小说的 defaults 结构，只加 script_* 键）。"""
    base = {
        "llm_timeout": 300, "planning_timeout": 300, "call_gap": 2,
        "script_pages": 100, "script_scenes": 30, "script_episodes": 12,
        "script_revise_rounds": 2,
    }
    base.update({k: v for k, v in _cfg_defaults().items() if k.startswith("script_")
                 or k in ("llm_timeout", "planning_timeout", "call_gap")})
    if cfg:
        base.update({k: v for k, v in cfg.items() if v is not None})
    return base


def _ctx(stage: str, extra: list[str] | None = None, include_case: bool = False,
         cfg: dict | None = None) -> str:
    """剧本阶段的最小上下文注入；创作前上传的参考资料随 cfg 一并进入。"""
    text = skillkit.stage_inject(stage, "script", extra=extra, include_case=include_case)
    block = (cfg or {}).get("_reference_block")
    if block:
        text += "\n\n" + str(block)
    return text


# ---------------------------------------------------------------- 时代/世界观

_GENRE_ERA = {
    "古装": "ancient", "历史": "ancient", "武侠": "ancient",
    "都市": "modern", "现实": "modern",
    "科幻": "future",
    "仙侠": "fantasy", "玄幻": "fantasy", "架空": "fantasy",
}
_TITLE_ERA_WORDS = (
    ("ancient", ("古代", "古装", "王朝", "江湖", "宫廷")),
    ("republic", ("民国",)),
    ("modern", ("当代", "现代", "都市")),
    ("future", ("未来", "科幻")),
    ("fantasy", ("架空", "仙侠", "玄幻")),
)


def _era_of(cfg: dict) -> str:
    """时代判定：优先向导字段 setting_era；其次是题材/标题里的明确词；最后按词表投票。

    仍判不出返回 ""：此时不做词表拦截，避免把现代剧里的「公司」当成古代剧的错。
    """
    era = str((cfg or {}).get("setting_era") or "").strip()
    if era in script_checker.ERA_LABELS:
        return era
    genre = str((cfg or {}).get("genre") or "")
    for word, code in _GENRE_ERA.items():
        if word in genre:
            return code
    title = str((cfg or {}).get("title") or "")
    for code, words in _TITLE_ERA_WORDS:
        if any(w in title for w in words):
            return code
    text = " ".join(str((cfg or {}).get(k) or "") for k in ("title", "idea", "notes", "genre"))
    return script_checker.guess_era(text)


def _world_block(cfg: dict) -> str:
    """把时代与世界观压成一段硬约束，注入 S0/S1/S3/S4/S5 的每一次生成。"""
    era = _era_of(cfg)
    label = script_checker.ERA_LABELS.get(era, "")
    world = str((cfg or {}).get("setting_world") or "").strip() or "（未填写，按该时代的生活常识、称谓与器物考据）"
    if not era:
        return ("【世界设定·硬约束】\n"
                f"世界观：{world}\n"
                "时代未指定，但全剧必须沿用同一时代设定\n"
                "违反上述设定的描写一律视为无效，必须改写。")
    lines = ["【世界设定·硬约束】", f"时代：{label}", f"世界观：{world}"]
    terms = script_checker.forbidden_terms(era)[:20]
    if terms:
        lines.append("本作禁止出现：" + "、".join(terms))
    lines.append("违反上述设定的描写一律视为无效，必须改写。")
    return "\n".join(lines)


def _era_feedback(issues: list[dict]) -> str:
    """把违规词与片段整理成可回灌给 LLM 的修正指令。"""
    picks = [f"- 「{it['term']}」×{it['count']}：…{it['snippet']}…" for it in issues]
    return ("【校验未过·必须修正】\n"
            "以下用词与设定时代冲突，逐处改写（换器物/场所/称谓/交通），不得保留：\n"
            + "\n".join(picks))


# ---------------------------------------------------------------- 阶段生成

def _gen(stage: str, task: str, ctx: str, cfg: dict, stage_code: str = "planning",
         max_tokens: int | None = None) -> str:
    """调 LLM 生成一段文本。失败返回空串（调用方降级）。"""
    sys_p = (f"你是一位资深剧本编剧（中文创作）。当前工作阶段：{stage}。\n"
             f"{task}\n\n"
             f"严格要求：\n"
             f"1. 只输出成果本身，不要解释你的思路，不要写「好的」「以下是」这类开场白。\n"
             f"2. 用中文，专业、具体、可执行；空话套话一律不要。\n"
             f"3. 不引用任何外部作品原文，不复刻他人文本。\n"
             f"4. 交付形态：Markdown，可直接作为工作稿使用。\n")
    msgs = [{"role": "system", "content": sys_p + ("\n\n" + ctx if ctx else "")},
            {"role": "user", "content": f"请产出：{task}"}]
    try:
        return llm.chat(msgs, cfg=cfg, stage=stage_code, max_tokens=max_tokens)
    except Exception:
        return ""


def gen_premise(cfg: dict) -> str:
    """S0 引擎：前提 / 主题 / 控制思想 / logline / 类型契约。"""
    form = FORMS.get(cfg.get("form", "film"), FORMS["film"])
    task = (
        f"为一部【{form['label']}】写「创作引擎」部分，包含：\n"
        f"1. **logline**（一到两句，念出来不拗口）\n"
        f"2. **控制思想**（主题 + 因果 + 代价，写成「如果一个人愿意付出 X，那么他就能 Y，或者失去 Z」）\n"
        f"3. **戏核**（一句话，必须可演：两个人在一个空间里能演出来）\n"
        f"4. **主角五问**（主角是谁 / 想要什么 / 谁挡着 / 为什么现在必须行动 / 最怕失去什么）\n"
        f"5. **类型契约**（主类型 + 1-2 个混合类型；观众会看到什么 / 为什么还在看 / 会得到什么）\n"
        f"6. **主角 vs 对手** 三行对照（各自想要什么 / 各自的赌注 / 为什么不能两全）\n"
        f"7. **剧本一句话卖点**（给制片方看的，20 字内）\n\n"
        f"{_world_block(cfg)}\n\n"
        f"用户给的素材/想法：{cfg.get('idea') or '（用户未给，请基于给定前提自行设计）'}\n"
        f"补充说明：{cfg.get('notes') or '无'}"
    )
    return _gen("剧本 · 引擎", task, _ctx("S0", cfg=cfg), cfg, "planning")


def gen_characters(cfg: dict, premise: str = "") -> str:
    """S1 人物网：人物小传 / 反派梯队 / 关系与未结清单。"""
    task = (
        "写「人物小传」，包含：\n"
        "1. **主要人物**（每位一节）：姓名 / 年龄职业外形 / 生理维度（限制他行动的一件事）/"
        "社会维度（位置、管谁怕谁欠谁、违反的规则与代价）/ 心理维度（相信什么、恐惧什么、"
        "说给自己听的谎言）/ 外在目标 / 内在需求 / 矛盾渴望 / 与主角的镜面关系 / 弧光 / "
        "语言标记（句长、词汇层级、口头禅、从不说什么）\n"
        "2. **次要人物表**：姓名 / 结构性功能（推进者·阻碍者·镜子·催化剂·黏合剂·观众代言人）/ "
        "与主角关系 / 与谁有未结的事\n"
        "3. **反派梯队**（三级：执行层 / 阻碍层 / 根源层），每级写战术、战略、哲学三层\n"
        "4. **人物关系网**：每两人之间一件未结的事\n\n"
        f"{_world_block(cfg)}\n\n"
        f"【创作引擎与前期设定】\n{(premise or '（见引擎部分）')[:3000]}\n\n"
        f"已知素材：{cfg.get('idea') or '（见引擎部分）'}\n"
        f"补充说明：{cfg.get('notes') or '无'}\n\n"
        "人物的姓名、身份、职业、称谓必须落在上述时代；古代不得出现现代职业与器物。"
    )
    return _gen("剧本 · 人物", task, _ctx("S1", cfg=cfg), cfg, "planning")


def gen_structure(cfg: dict, premise: str = "", chars: str = "") -> str:
    """S3 结构：幕点 / 序列 / 单集结构。"""
    series = cfg.get("form") == "series"
    if series:
        task = (
            "写「剧集结构」：\n"
            "1. **主引擎**（一句话：一个能持续产出冲突的结构）与**副引擎**\n"
            "2. **本季核心问题**（这一季结束时，主角必须承认 ____）\n"
            f"3. **幕点表**：第 1-3 集建世界 / 第 4-8 集试探引擎 / 第 9-12 集首次危机 / "
            f"第 13-{cfg.get('episodes', 12)} 集复杂化 / 末集收束\n"
            "4. **季中点**（第几集，打破什么平衡，怎么打破）\n"
            "5. **季终结算**（结算什么 + 兑现什么 + 开什么新引擎）\n"
            "6. **A/B/C 故事线**各一段\n"
            "7. **集尾钩子分级表**（本季每集一个等级，轮换不重复）\n"
        )
    else:
        task = (
            f"写「故事结构」（目标约 {cfg.get('pages', 100)} 页）：\n"
            "1. **幕点表**：开场画面 / 主题陈述 / 铺垫 / 催化剂 / 第一危机 / 中点 / 第二危机 / "
            "All Is Lost / 最终抉择 / 收束（标出页码）\n"
            "2. **中点场景**（具体描述：谁在哪、想要什么、被什么挡住、怎么变）\n"
            "3. **All Is Lost 场景**（主角在这里失去的不只是外部目标，还有信念）\n"
            "4. **序列清单**（每个序列一句小高潮 + 场景数 + 页数）\n"
            "5. **第二幕「对手赢的戏」清单**（第三幕要原样奉还的场次）\n"
            "6. **结尾一句话**（最后一场戏的新平衡是什么）\n"
        )
    task += (
        f"\n{_world_block(cfg)}\n\n"
        f"【创作圣经（引擎/主题/控制思想）】\n{(premise or '（未提供）')[:2500]}\n\n"
        f"【人物小传】\n{(chars or '（未提供）')[:2000]}\n\n"
        "结构里的场所、事件、冲突手段必须能在该时代发生；不得靠现代科技制造阻碍或解法。"
    )
    return _gen("剧本 · 结构", task, _ctx("S3", cfg=cfg), cfg, "planning")


def gen_scenes(cfg: dict, total_scenes: int, bible: str = "", struct: str = "") -> str:
    """S4 场次大纲：逐场目标 / 触发 / 阻碍 / beat / 结果。"""
    task = (
        f"写「场次清单」，共 {total_scenes} 场，用 Markdown 表格，列为：\n"
        "| 场次 | 内/外 | 地点 | 时间 | 出场 | 道具 | 前提 | 触发 | 阻碍 | 结果 | 页数 |\n\n"
        "要求：\n"
        "1. 每场都有明确的目标、阻碍、结果；结果分四类（赢/输/转折赢/代价）\n"
        "2. 相邻场次的目的不重复（推进/揭示/悬念/喘息 轮换）\n"
        "3. 阻力必须可具象化，不写「他不愿意」要写「他做不到」\n"
        "4. 主要角色的戏占比 50-60%，留出 B 故事线与轻喜剧线的位置\n"
        "5. 场景数与总页数要能对上（中文剧本 1 页约 1 分钟）\n"
        "6. 地点 / 时间 / 道具 / 称谓必须落在设定时代内；古代不得写医院、公寓、手机、汽车、"
        "公司、警察等现代物事，人物职业与称呼同此\n\n"
        f"{_world_block(cfg)}\n\n"
        f"【创作圣经（引擎/主题）】\n{(bible or '（未提供）')[:4000]}\n\n"
        f"【结构大纲】\n{(struct or '（未提供）')[:2500]}\n\n"
        f"补充说明：{cfg.get('notes') or '无'}"
    )
    return _gen("剧本 · 场次", task, _ctx("S4", cfg=cfg), cfg, "planning")


def gen_scene_body(scene: dict, ctx_extra: str, cfg: dict) -> str:
    """S5 写一场戏的正文。"""
    task = (
        f"写第 {scene.get('no', '?')} 场的剧本正文。\n\n"
        f"场次信息：\n"
        f"- 场景：{scene.get('heading', '（按场次清单）')}\n"
        f"- 出场：{scene.get('cast', '（按场次清单）')}\n"
        f"- 道具：{scene.get('props', '无')}\n"
        f"- 前提（场前的状态）：{scene.get('setup', '')}\n"
        f"- 触发（开场的事件）：{scene.get('trigger', '')}\n"
        f"- 阻碍：{scene.get('obstacle', '')}\n"
        f"- 结果：{scene.get('outcome', '')}\n\n"
        f"格式要求（严格遵守）：\n"
        f"1. 第一行是场景标题：`内景  地点  时间` 或 `外景  地点  时间`，全部大写，末尾无句号\n"
        f"2. 之后是动作行：只写摄像机能拍到的，一段不超过三行\n"
        f"3. 对白格式：角色名顶格缩进单独一行（大写），下一行是台词，台词一行不超过 30 字\n"
        f"4. 表演提示用小写括号，单独一行，不超过 6 个字\n"
        f"5. 人物首次出场时，名字在动作行中大写介绍\n"
        f"6. 不要写镜头指令（推拉摇移、景别），那是分镜表的活\n"
        f"7. 场景内要有 3-8 个节拍（价值翻转），结尾停在问题刚解决或刚变严重的地方\n"
        f"8. 进场时人物已经在麻烦里；不要用台词解释观众已知的事\n"
        f"9. 只输出这一场的正文，不要写场次编号、页码或任何说明\n\n"
        f"{_world_block(cfg)}\n\n"
        f"上下文材料：\n{ctx_extra}"
    )
    return _gen("剧本 · 写作", task, _ctx("S5", cfg=cfg), cfg, "writing", max_tokens=4000)


def gen_revision(full_text: str, cfg: dict) -> str:
    """S6 修订：通读修订建议。"""
    task = (
        "通读下面这份剧本初稿，给出**修订清单**。\n\n"
        "按下面的小标题分组，每条写清「位置 / 问题 / 后果 / 最小改法」：\n"
        "## 前提与主题\n## 结构\n## 人物与冲突\n## 对白与潜台词\n"
        "## 场景与节拍\n## 格式\n## 可行性（场景数、页数、预算）\n\n"
        "要求：\n"
        "1. 只提真正的问题，不为凑数写无关紧要的建议\n"
        "2. 每条必须指向具体位置，并说明「不改会怎样」\n"
        "3. 给出可执行的最小改法，不要说「加强人物塑造」这类空话\n"
        "4. 如果某方面没问题，写「无重大问题」即可\n\n"
        f"初稿：\n{full_text[:12000]}"
    )
    return _gen("剧本 · 修订", task, _ctx("S6", cfg=cfg), cfg, "review")


def score_script(text: str, cfg: dict) -> dict:
    """六维百分制评分（沿用 medium.script 的维度）。"""
    dims = medium.score_dims("script")
    prompt = (
        f"给下面这份剧本打分（满分 100），维度与权重：\n"
        + "\n".join(f"- {n}：{w} 分" for n, w in dims) + "\n\n"
        "输出严格 JSON：\n"
        '{"scores":{"前提":0,"结构":0,"人物":0,"对白":0,"格式":0,"可行性":0},'
        '"weak":["最弱的一到三个维度"],"verdict":"一句话总评","fixes":["最重要的三条修改建议"]}\n\n'
        "评分要严格：85 分以上意味着可以直接进组。\n\n"
        f"剧本：\n{text[:12000]}"
    )
    out = llm.chat_json([{"role": "system", "content": "你是严格的剧本审稿人。"},
                          {"role": "user", "content": prompt}], cfg=cfg, stage="review")
    if not isinstance(out, dict):
        return {"scores": {}, "weak": [], "verdict": "评分失败", "fixes": []}
    scores = out.get("scores") or {}
    total = 0.0
    for n, w in dims:
        v = scores.get(n)
        try:
            total += (float(v) / 10.0) * w
        except (TypeError, ValueError):
            pass
    out["total"] = round(total, 1)
    out["pass_line"] = medium.pass_line("script")
    out["passed"] = total >= medium.pass_line("script")
    return out


# ---------------------------------------------------------------- 解析场次

_SCENE_ROW = re.compile(r"^\|\s*(\d+)\s*\|(.+)$", re.M)


def parse_scenes(md: str) -> list[dict]:
    """从场次清单表格解析出场次。"""
    scenes: list[dict] = []
    for m in _SCENE_ROW.finditer(md or ""):
        no = int(m.group(1))
        cells = [c.strip() for c in m.group(2).split("|")]
        # 去掉首尾空列
        while cells and not cells[0]:
            cells.pop(0)
        while cells and not cells[-1]:
            cells.pop()
        scenes.append({
            "no": no,
            "kind": cells[0] if len(cells) > 0 else "",
            "place": cells[1] if len(cells) > 1 else "",
            "time": cells[2] if len(cells) > 2 else "",
            "cast": cells[3] if len(cells) > 3 else "",
            "props": cells[4] if len(cells) > 4 else "",
            "setup": cells[5] if len(cells) > 5 else "",
            "trigger": cells[6] if len(cells) > 6 else "",
            "obstacle": cells[7] if len(cells) > 7 else "",
            "outcome": cells[8] if len(cells) > 8 else "",
            "pages": cells[9] if len(cells) > 9 else "",
        })
    return scenes


def scene_heading(sc: dict) -> str:
    """由场次清单合成标准场景标题。"""
    kind = (sc.get("kind") or "").strip()
    place = (sc.get("place") or "").strip()
    tm = (sc.get("time") or "").strip()
    if kind and place:
        head = f"{kind}  {place}"
        if tm:
            head += f"  {tm}"
        return head
    return (sc.get("heading") or f"场景 {sc.get('no', '?')}").strip()


# ---------------------------------------------------------------- 项目生命周期

def create_project(cfg: dict, base_key: str = "") -> ScriptProject:
    """建剧本项目 + 逐阶段生成。"""
    cfg = _merge_cfg(cfg)
    form = cfg.get("form", "film")
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _safe(cfg.get("title") or cfg.get("idea") or "未命名剧本")
    pdir = safe_join(PROJECTS_DIR, base_key or f"s_{ts}-{name}", label="项目 key")
    os.makedirs(pdir, exist_ok=True)
    proj = ScriptProject(pdir, name)
    llm.book(os.path.basename(pdir), "planning")

    era = _era_of(cfg)
    plan = {
        "version": 1, "scriptName": name, "projectPath": pdir, "medium": "script",
        "form": form, "formLabel": FORMS.get(form, FORMS["film"])["label"],
        "entryPath": cfg.get("entryPath", "idea"),
        "totalScenes": int(cfg.get("scenes") or 30),
        "targetPages": int(cfg.get("pages") or 100),
        "episodes": int(cfg.get("episodes") or 12),
        "createdAt": datetime.datetime.now().isoformat(),
        "status": "planning", "stages": {}, "scenes": [],
        "meta": {"setting": {
            "era": era,
            "era_label": script_checker.ERA_LABELS.get(era, ""),
            "world": str(cfg.get("setting_world") or "").strip(),
        }},
        "config": {k: v for k, v in cfg.items() if k != "api_key"},
    }
    proj.save_plan(plan)

    proj.log("🎬 阶段 S0：生成创作引擎…")
    premise = gen_premise(cfg)
    bible_text = _bible_header(cfg, name) + "\n\n" + (premise or "（引擎生成失败，请重试）")
    _write(os.path.join(pdir, F_BIBLE), bible_text)
    plan["stages"]["S0"] = {"done": bool(premise), "at": datetime.datetime.now().isoformat()}
    proj.save_plan(plan)

    proj.log("🎭 阶段 S1：生成人物小传…")
    chars = gen_characters(cfg, premise=bible_text)
    _write(os.path.join(pdir, F_CHARS), f"# {name} 人物小传\n\n" + (chars or "（人物生成失败，请重试）"))
    plan["stages"]["S1"] = {"done": bool(chars), "at": datetime.datetime.now().isoformat()}
    proj.save_plan(plan)

    proj.log("🧭 阶段 S3：生成结构大纲…")
    struct = gen_structure(cfg, premise=bible_text, chars=chars)
    _write(os.path.join(pdir, F_STRUCT), f"# {name} 结构大纲\n\n" + (struct or "（结构生成失败，请重试）"))
    plan["stages"]["S3"] = {"done": bool(struct), "at": datetime.datetime.now().isoformat()}
    proj.save_plan(plan)

    proj.log("🎞️ 阶段 S4：生成场次清单…")
    era = _era_of(cfg)
    scenes_md = gen_scenes(cfg, plan["totalScenes"], bible=bible_text, struct=struct)
    guard = script_checker.check_text(scenes_md, era)
    if not guard["ok"]:
        terms = "、".join(it["term"] for it in guard["issues"][:8])
        proj.log(f"⚠️ 场次表时代校验未过（{terms}），带反馈重生成一次…")
        cfg_retry = dict(cfg)
        cfg_retry["notes"] = "\n".join(
            x for x in (cfg.get("notes") or "", _era_feedback(guard["issues"])) if x)
        retry_md = gen_scenes(cfg_retry, plan["totalScenes"], bible=bible_text, struct=struct)
        if retry_md.strip():
            scenes_md = retry_md
        guard = script_checker.check_text(scenes_md, era)
    _write(os.path.join(pdir, F_SCENES), f"# {name} 场次清单\n\n" + (scenes_md or "（场次生成失败，请重试）"))
    scenes = parse_scenes(scenes_md)
    for i, sc in enumerate(scenes, 1):
        sc["no"] = sc.get("no") or i
        sc["status"] = "pending"
        sc["fileName"] = f"第{sc['no']:03d}场.md"
        sc["wordCount"] = None
    plan["scenes"] = scenes
    plan["stages"]["S4"] = {"done": bool(scenes), "at": datetime.datetime.now().isoformat()}
    if not guard["ok"]:
        # 场次表持续与时代冲突：暂停自动写作，把冲突留给用户确认，绝不静默开写。
        plan.setdefault("meta", {})["paused"] = True
        plan["meta"]["era_warnings"] = guard["issues"]
        proj.save_plan(plan)
        terms = "、".join(it["term"] for it in guard["issues"][:8])
        proj.log(f"⛔ 场次表与时代设定冲突（{terms}），已暂停自动写作，请先修正时代/世界观或场次表")
        return proj
    proj.save_plan(plan)

    if cfg.get("form") == "series":
        proj.log("📺 生成季弧与分集…")
        _write(os.path.join(pdir, F_SEASON), f"# {name} 季弧与分集\n\n" + (struct or ""))

    proj.log(f"✅ 规划就绪：《{name}》{len(scenes)} 场")
    # v1.2.2：规划完成后自动开写全部场次（后台线程），状态 planning → writing
    import threading as _th
    def _auto():
        try:
            write_scenes(proj)
        except Exception as e:
            print(f"[bg] 剧本自动写作失败: {e}", flush=True)
    _th.Thread(target=_auto, daemon=True).start()
    return proj


def _bible_header(cfg: dict, name: str) -> str:
    form = FORMS.get(cfg.get("form", "film"), FORMS["film"])
    entry = ENTRY_PATHS.get(cfg.get("entryPath", "idea"), "")
    era = _era_of(cfg)
    era_label = script_checker.ERA_LABELS.get(era, "未指定")
    world = str(cfg.get("setting_world") or "").strip() or "（未填写）"
    return (
        f"# {name} · 创作圣经\n\n"
        f"> 媒介形式：{form['label']} ｜ 入口路径：{entry}\n"
        f"> 目标规模：{cfg.get('pages', 100)} 页 / {cfg.get('scenes', 30)} 场\n"
        f"> 时代：{era_label} ｜ 世界观：{world}\n"
        f"> 生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    )


# ---------------------------------------------------------------- 写正文

def _act_hint(struct_md: str, no: int, total: int) -> str:
    """从结构大纲里取本场所在幕/序列，取不到返回 ""（绝不报错）。"""
    if not struct_md:
        return ""
    lines = struct_md.splitlines()
    nodes: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") or (s.startswith("**") and s.endswith("**")):
            title = s.lstrip("#").strip().strip("*").strip()
            if re.search(r"幕|序列|集|Act", title):
                nodes.append((i, title))
    if not nodes:
        return ""
    hit = None
    for i, title in nodes:
        m = re.search(r"第\s*(\d+)\s*(?:[-–~至]\s*(\d+))?\s*场", title)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            if lo <= no <= hi:
                hit = (i, title)
                break
    if hit is None and total > 0:
        idx = min(len(nodes) - 1, max(0, (no - 1) * len(nodes) // total))
        hit = nodes[idx]
    if hit is None:
        return ""
    i, title = hit
    body: list[str] = []
    for line in lines[i + 1:]:
        if line.strip().startswith("#"):
            break
        body.append(line)
        if sum(len(x) for x in body) > 500:
            break
    text = "\n".join(body).strip()
    return f"【本场所属幕/序列】{title}" + (f"\n{text[:500]}" if text else "")


def write_scenes(proj: ScriptProject, only: list[int] | None = None) -> dict:
    """S5：按场次清单逐场写正文。"""
    plan = proj.plan() or {}
    scenes = plan.get("scenes") or []
    if not scenes:
        return {"ok": False, "error": "没有场次清单，请先生成规划"}
    cfg = _merge_cfg(plan.get("config") or {})
    cfg.setdefault("title", plan.get("scriptName") or "")
    setting = (plan.get("meta") or {}).get("setting") or {}
    # 旧项目没有 setting 字段，但计划里一旦补上，就以它为准覆盖 config 里的缺省。
    if setting.get("era"):
        cfg["setting_era"] = setting["era"]
    if setting.get("world"):
        cfg["setting_world"] = setting["world"]
    bible = _read(os.path.join(proj.path, F_BIBLE))
    chars = _read(os.path.join(proj.path, F_CHARS))
    struct_md = _read(os.path.join(proj.path, F_STRUCT))
    body_dir = proj.body_dir()
    era = _era_of(cfg)
    world = _world_block(cfg)
    total = len(scenes)

    plan["status"] = "writing"
    meta = plan.setdefault("meta", {})
    meta["stage"] = "S5"
    if era and not setting.get("era"):
        # 旧项目首次按设定重写时把判定结果落盘：之后体检、界面与重写用同一个时代。
        meta["setting"] = {"era": era, "era_label": script_checker.ERA_LABELS.get(era, ""),
                           "world": setting.get("world", "")}
    proj.save_plan(plan)

    done = 0
    for sc in scenes:
        no = sc.get("no")
        if only and no not in only:
            continue
        if sc.get("status") == "completed":
            continue
        proj.log(f"✍️ 写第 {no} 场…")
        extra = (f"【创作圣经·全文（节选）】\n{bible[:6000]}\n\n"
                 f"【人物小传】\n{chars[:2500]}\n\n"
                 f"【结构大纲】\n{struct_md[:2500]}\n\n"
                 f"{world}")
        hint = _act_hint(struct_md, no if isinstance(no, int) else 0, total)
        if hint:
            extra += "\n\n" + hint
        text = gen_scene_body(sc, extra, cfg)
        if not text.strip():
            proj.log(f"⚠️ 第 {no} 场生成失败，跳过")
            sc["status"] = "failed"
            continue
        check = script_checker.check_text(text, era)
        if not check["ok"]:
            terms = "、".join(it["term"] for it in check["issues"][:8])
            proj.log(f"⚠️ 第 {no} 场时代校验未过（{terms}），带反馈重生成一次…")
            retry = gen_scene_body(sc, extra + "\n\n" + _era_feedback(check["issues"]), cfg)
            if retry.strip():
                text = retry
                check = script_checker.check_text(text, era)
        if not check["ok"]:
            sc["warn"] = check["issues"]
            terms = "、".join(it["term"] for it in check["issues"][:8])
            proj.log(f"⚠️ 第 {no} 场仍有时代偏差：{terms}（已保留，待人工修正）")
        else:
            sc.pop("warn", None)
        _write(os.path.join(body_dir, sc.get("fileName") or f"第{no:03d}场.md"), text.strip() + "\n")
        sc["status"] = "completed"
        sc["wordCount"] = len(re.findall(r"[\u4e00-\u9fff]", text))
        done += 1
        proj.save_plan(plan)

    # v1.2.2：全部场次完成 → 状态 completed（否则一直 in_progress，用户以为没写完）
    if done and done >= len(scenes):
        plan["status"] = "completed"
        plan.setdefault("meta", {})["stage"] = "S7"
    else:
        plan["status"] = "in_progress" if done else plan.get("status", "planning")
    proj.save_plan(plan)
    proj.log(f"✅ 本轮写完 {done} 场")
    return {"ok": True, "written": done, "total": len(scenes)}


def full_text(proj: ScriptProject) -> str:
    """把正文按场次顺序拼成全文。"""
    plan = proj.plan() or {}
    parts = []
    for sc in plan.get("scenes") or []:
        p = os.path.join(proj.path, F_BODY, sc.get("fileName") or "")
        t = _read(p)
        if t.strip():
            parts.append(t.strip())
    return "\n\n".join(parts)


def run_revision(proj: ScriptProject) -> dict:
    """S6：通读修订 + 质量评分。"""
    plan = proj.plan() or {}
    text = full_text(proj)
    if not text:
        return {"ok": False, "error": "还没有正文"}
    cfg = _merge_cfg(plan.get("config") or {})
    proj.log("🔍 阶段 S6：通读修订…")
    rev = gen_revision(text, cfg)
    _write(os.path.join(proj.path, F_REVISIONS),
           f"# {proj.name} 修订记录\n\n"
           f"> 生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n" + (rev or "（修订建议生成失败）"))
    proj.log("📊 评分…")
    sc = score_script(text, cfg)
    _write(os.path.join(proj.path, F_QUALITY),
           f"# {proj.name} 质量报告\n\n"
           f"- 总分：**{sc.get('total', 0)} / 100**（及格线 {sc.get('pass_line', 85)}）\n"
           f"- 结论：{'达标' if sc.get('passed') else '未达标'}\n"
           f"- 总评：{sc.get('verdict', '')}\n\n"
           f"## 分项\n"
           + "\n".join(f"- {k}：{v}/10" for k, v in (sc.get("scores") or {}).items())
           + "\n\n## 最弱维度\n"
           + "\n".join(f"- {w}" for w in (sc.get("weak") or []))
           + "\n\n## 优先修改\n"
           + "\n".join(f"- {f}" for f in (sc.get("fixes") or []))
           + "\n")
    plan["stages"]["S6"] = {"done": True, "score": sc.get("total"),
                             "at": datetime.datetime.now().isoformat()}
    proj.save_plan(plan)
    return {"ok": True, "revision": rev, "score": sc}


# ---------------------------------------------------------------- 查询

def project_dir(key: str) -> str:
    return os.path.join(PROJECTS_DIR, key)


def get_project(key: str) -> ScriptProject | None:
    pdir = project_dir(key)
    if not os.path.isdir(pdir):
        return None
    return ScriptProject(pdir, key)


def discover_projects() -> list[dict]:
    """列出所有剧本项目。"""
    out = []
    if not os.path.isdir(PROJECTS_DIR):
        return out
    for d in sorted(os.listdir(PROJECTS_DIR), reverse=True):
        if not d.startswith("s_"):
            continue
        pdir = os.path.join(PROJECTS_DIR, d)
        if not os.path.isdir(pdir):
            continue
        plan = {}
        try:
            plan = json.load(open(os.path.join(pdir, F_PLAN), encoding="utf-8"))
        except Exception:
            pass
        scenes = plan.get("scenes") or []
        done = sum(1 for s in scenes if s.get("status") == "completed")
        out.append({
            "key": d, "name": plan.get("scriptName", d),
            "medium": "script", "form": plan.get("formLabel", ""),
            "total": len(scenes), "done": done,
            "status": plan.get("status", "unknown"),
            "updatedAt": plan.get("updatedAt", ""),
        })
    return out


def read_scene(key: str, no: int) -> str:
    """读单场正文；场次缺/坏 fileName 时按冻结命名约定 第NNN场.md 兜底（见 write_scenes）。"""
    proj = get_project(key)
    if not proj:
        return ""
    plan = proj.plan() or {}
    for sc in plan.get("scenes") or []:
        if int(sc.get("no") or 0) == int(no):
            fn = sc.get("fileName")
            if not isinstance(fn, str) or not fn.strip():
                fn = f"第{int(no):03d}场.md"
            # fileName 来自计划文件（可被用户手改），与 export.py 一致只取末段，杜绝 ../../ 逃逸
            fn = os.path.basename(fn.strip()) or f"第{int(no):03d}场.md"
            return _read(os.path.join(proj.path, F_BODY, fn))
    return ""


def read_file_part(key: str, part: str) -> str:
    """按 part 读项目文件：bible / chars / struct / scenes / revisions / season / quality"""
    proj = get_project(key)
    if not proj:
        return ""
    mapping = {
        "bible": F_BIBLE, "chars": F_CHARS, "struct": F_STRUCT,
        "scenes": F_SCENES, "revisions": F_REVISIONS, "season": F_SEASON,
        "quality": F_QUALITY,
    }
    fn = mapping.get(part, part)
    return _read(os.path.join(proj.path, fn))


def export_text(key: str) -> str:
    """S7 交付：导出定稿全文（带页眉）。"""
    proj = get_project(key)
    if not proj:
        return ""
    plan = proj.plan() or {}
    name = plan.get("scriptName", key)
    head = (f"{name}\n\n"
            f"修订日期：{datetime.datetime.now().strftime('%m/%d/%y')}\n"
            f"第 1 页\n\n")
    return head + full_text(proj)
