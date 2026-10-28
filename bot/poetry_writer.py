"""诗词创作流水线（v1.0.0 · 神笔文学创作台）

六步工作流（对应 bot/medium.py 的 poetry 阶段表）：
  I 释读 → P 规划 → C 起草 → K 校验 → R 评改 → F 定稿

**关键设计**：K 步用 bot/poetry_checker.py 的**确定性校验**（平水韵 106 部 + 粘对规则），
不用 LLM 猜格律。校验报告直接喂回 R 步改稿，最多 3 轮。

目录规范（projects/p_<key>/）：
  00-题记.md    01-意象表.md   02-初稿.md
  03-格律报告.md 04-定稿.md    诗词计划.json
"""
from __future__ import annotations

import datetime
import json
import os
import re
import threading

from . import llm, poetry_checker, skillkit
from . import writer as novel_writer
from bot.safeio import atomic_write_json, atomic_write_text, safe_join

ROOT = novel_writer.ROOT
PROJECTS_DIR = novel_writer.PROJECTS_DIR

F_NOTE = "00-题记.md"
F_IMAGERY = "01-意象表.md"
F_DRAFT = "02-初稿.md"
F_CHECK = "03-格律报告.md"
F_FINAL = "04-定稿.md"
F_PLAN = "诗词计划.json"

# 体式
FORMS = {
    "wujue": "五绝", "qijue": "七绝", "wulv": "五律", "qilv": "七律",
    "ci": "词", "qu": "曲", "modern": "现代诗", "free": "自由诗",
}
FORM_ALIASES = {
    "五绝": "wujue", "七绝": "qijue", "五律": "wulv", "七律": "qilv",
    "词": "ci", "填词": "ci", "曲": "qu", "散曲": "qu",
    "现代诗": "modern", "自由诗": "free", "新诗": "modern",
}


def _pipeline_running_for(path: str) -> bool:
    """v3.1.0：流水线计数器（write 线程 mark/unmark），让任意实例读到的 running 都一致。"""
    try:
        from bot import writer as _wr
        return _wr.pipeline_running(os.path.basename(str(path or "")))
    except Exception:
        return False


class PoetryProject:
    """诗词项目。字段与小说 / 剧本 Project 对齐。"""

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
        meta = plan.get("meta") or {}
        return {
            "state": plan.get("status", "unknown"),
            "done": meta.get("rounds", 0), "total": plan.get("maxRounds", 3),
            "words": meta.get("words", 0), "running": self.running or _pipeline_running_for(self.path),
            "name": plan.get("poemName", self.name), "medium": "poetry",
            "form": plan.get("formLabel", ""), "stage": meta.get("stage", ""),
            "verdict": meta.get("verdict", ""),
        }


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


def _merge_cfg(cfg: dict | None) -> dict:
    base = {"llm_timeout": 300, "call_gap": 2, "maxRounds": 3, "poetry_words": 200}
    try:
        d = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8")).get("defaults") or {}
    except Exception:
        d = {}
    base.update({k: v for k, v in d.items() if k.startswith("poetry_")
                 or k in ("llm_timeout", "call_gap")})
    if cfg:
        base.update({k: v for k, v in cfg.items() if v is not None})
    return base


def _ctx(stage: str, cfg: dict | None = None) -> str:
    text = skillkit.stage_inject(stage, "poetry")
    block = (cfg or {}).get("_reference_block")
    if block:
        text += "\n\n" + str(block)
    return text


def normalize_form(form: str) -> str:
    f = (form or "").strip()
    if f in FORMS:
        return f
    for k, v in FORM_ALIASES.items():
        if k in f:
            return v
    return "modern"


def _gen(stage_label: str, task: str, ctx: str, cfg: dict,
         stage_code: str = "planning", max_tokens: int | None = None) -> str:
    """stage_label: 提示词里显示的阶段名；stage_code: 供模型路由（planning/writing/review）。"""
    sys_p = (f"你是一位古典诗词作家（中文创作）。当前工作阶段：{stage_label}。\n"
             f"{task}\n\n"
             f"严格要求：\n"
             f"1. 只输出成果本身，不要解释思路，不要写「好的」「以下是」。\n"
             f"2. 用中文；写诗用字要精准，宁可生僻也不要错。\n"
             f"3. 不引用任何他人诗词原文，不模仿特定在世或已成名作者的具体作品。\n"
             f"4. 交付形态：Markdown，可直接作为工作稿。\n")
    msgs = [{"role": "system", "content": sys_p + ("\n\n" + ctx if ctx else "")},
            {"role": "user", "content": f"请产出：{task}"}]
    try:
        return llm.chat(msgs, cfg=cfg, stage=stage_code, max_tokens=max_tokens)
    except Exception:
        return ""


# ---------------------------------------------------------------- 阶段

def gen_note(cfg: dict) -> str:
    """I 释读：题面理解 / 情感核心 / 体式判定。"""
    form = FORMS.get(normalize_form(cfg.get("form", "modern")), "现代诗")
    task = (
        f"写「题记与释读」，包含：\n"
        f"1. **题面**（用户给的题目/题材/情境）\n"
        f"2. **情感核心**（一句话：这首诗要让谁、在什么时刻、感到什么）\n"
        f"3. **体式判定**：{form}，并说明为什么选这个体式（字数、难度、适合承载的重量）\n"
        f"4. **一个具体的场景**（不是主题，是可想象的一个时刻、这个地方）\n"
        f"5. **难点预判**（写这首诗最可能卡住的地方）\n\n"
        f"用户输入：{cfg.get('idea') or '（未给，请自行拟定题目）'}"
    )
    return _gen("诗词 · 释读", task, _ctx("I", cfg=cfg), cfg, "planning")


def gen_plan(cfg: dict) -> str:
    """P 规划：意象表 / 章法 / 韵部。"""
    form = normalize_form(cfg.get("form", "modern"))
    if form in ("modern", "free"):
        task = (
            "写「创作规划」，包含：\n"
            "1. **意象表**：3-5 个核心意象，每个写清「物象 / 它承载什么 / 出现位置」\n"
            "2. **结构设计**：这首诗分几段推进，每段做什么（推进 / 停顿 / 转折）\n"
            "3. **节奏设计**：长短句的落差安排\n"
            "4. **题眼**：哪一个意象是全诗核心\n"
            "5. **结尾落点**：最后一行打算落在哪个画面上\n"
        )
    else:
        task = (
            "写「创作规划」，包含：\n"
            "1. **韵部选择**：选哪个平水韵部（说明理由——情绪与韵部音色）\n"
            "2. **韵脚备选**：该韵部里 8-12 个可用的字，按语义分组标注\n"
            "3. **意象表**：3-5 个核心意象，每个写清「物象 / 承载什么 / 放哪一句」\n"
            "4. **章法**：起承转合（或律诗的首联/颔联/颈联/尾联各写什么）\n"
            "5. **对仗设计**（律诗）：颔联、颈联各准备什么对子\n"
            "6. **难点预判**\n"
        )
    return _gen("诗词 · 规划", task, _ctx("P", cfg=cfg), cfg, "planning")


def gen_draft(cfg: dict, plan_text: str) -> str:
    """C 起草。"""
    form = normalize_form(cfg.get("form", "modern"))
    label = FORMS.get(form, "现代诗")
    if form == "ci":
        _pai = str(cfg.get("cipai") or "").strip()
        extra = (f"词牌为「{_pai}」，" if _pai else "按所选词牌，") + (
            "句读字数逐句写，字数必须精确对上。\n词牌规格见「词牌格律表.md」。")
    elif form == "qu":
        _pai = str(cfg.get("cipai") or "").strip()
        extra = (f"曲牌为「{_pai}」，" if _pai else "按所选曲牌，") + (
            "宫调与句数按谱，衬字不要滥用。")
    elif form in ("modern", "free"):
        extra = "自由诗，不拘格律，但必须有呼吸节奏与长短落差。"
    else:
        extra = (f"{label}：句数、每句字数、押韵句必须严格符合体式。"
                 "偶数位平仄要分明，注意粘对。")
    task = (
        f"写这首{label}的初稿。\n\n"
        f"{extra}\n\n"
        f"要求：\n"
        f"1. 只输出诗本身，不要标题、不要注释、不要说明。\n"
        f"2. 每行一句，用换行分隔（不要用逗号连成一段）。\n"
        f"3. 意象用具体的东西，不要抽象词。\n"
        f"4. 结尾要落得住，不要用套话收尾。\n\n"
        f"创作规划：\n{plan_text[:3000]}"
    )
    return _gen("诗词 · 起草", task, _ctx("C", cfg=cfg), cfg, stage_code="writing", max_tokens=1500)


def check_draft(text: str, form: str) -> dict:
    """K 校验：确定性格律检查。"""
    if form in ("modern", "free"):
        return {
            "form": form, "form_label": "现代诗", "verdict": "pass",
            "lines": poetry_checker.extract_lines(text),
            "findings": [{"type": "note", "severity": "info",
                          "message": "现代诗不做格律校验，转由审校评「呼吸节奏 / 意象 / 结尾落点」"}],
            "counts": {"fail": 0, "advisory": 0, "unclear": 0},
            "summary": "现代诗：不做格律检查",
        }
    report = poetry_checker.validate(text, form)
    return report


def gen_revise(text: str, report: dict, cfg: dict) -> str:
    """R 评改：按校验报告改稿。"""
    findings = [x for x in report.get("findings") or [] if x.get("severity") in ("fail", "advisory", "unclear")]
    fl = "\n".join(f"- {x.get('message')}" for x in findings) or "（无格律问题）"
    task = (
        "按下面的格律检查结果改写这首诗。\n\n"
        "【原诗】\n" + text.strip() + "\n\n"
        "【格律检查结果】\n" + fl + "\n\n"
        "改写要求：\n"
        "1. 优先解决标为「违规」的问题（字数、押韵）；「建议」类问题尽量一并处理。\n"
        "2. **不要改动原诗的立意与核心意象**——改的是格律，不是诗。\n"
        "3. 保持原有的句子数与结构。\n"
        "4. 「待定」类问题（多音字）按句义选读音，自己定夺。\n"
        "5. 只输出改后的诗本身，不要说明改了什么。\n"
    )
    return _gen("诗词 · 评改", task, _ctx("R", cfg=cfg), cfg, stage_code="review", max_tokens=1500)


def gen_critique(text: str, cfg: dict) -> str:
    """F 定稿前的审美审校。"""
    task = (
        "对这首诗做审美审校（不看格律，只看诗本身），按下面的小标题给出简短评语：\n"
        "## 意象是否有力\n## 情感是否真实具体\n## 节奏与呼吸\n## 章法与转折\n"
        "## 结尾是否落住\n## 一句可改的诗\n\n"
        "要求：每项 2-4 句，点名具体的句子或字，说清「读起来的后果」。\n"
        "最后给一个总分（百分制，四个维度：意象30 / 章法25 / 声律25 / 情感20 的加权）。\n"
        "输出严格 JSON：\n"
        '{"dims":{"意象":0,"章法":0,"声律":0,"情感":0},'
        '"comments":{"意象":"...","情感":"...","节奏":"...","章法":"...","结尾":"...","可改的一句":"..."},'
        '"total":0,"verdict":"一句话总评"}\n\n'
        f"诗：\n{text.strip()}"
    )
    out = llm.chat_json([{"role": "system", "content": "你是一位严格的诗歌审读者。"},
                          {"role": "user", "content": task}], cfg=cfg, stage="review")
    return out if isinstance(out, dict) else {}


def gen_extra(text: str, cfg: dict, kind: str) -> str:
    """附产（向导勾选才生成）：kind = notes 创作手记 / translation 白话译文。"""
    if kind == "translation":
        task = ("给出这首诗的白话译文：逐句对应，忠实原意与顺序，不添解释、不做评价。\n"
                "输出格式：每行「原句 → 白话」。")
        label = "诗词 · 译文"
    else:
        task = ("写一则 150-250 字的「创作手记」：为什么选这个体式、核心意象从哪来、"
                "哪一处最难落笔、成稿后自己最不满意的一点。用第一人称，不夸成果。")
        label = "诗词 · 手记"
    return _gen(label, task + f"\n\n诗：\n{text.strip()}", _ctx("F", cfg=cfg), cfg,
                "review", max_tokens=900)


# ---------------------------------------------------------------- 生命周期

def create_project(cfg: dict, base_key: str = "") -> PoetryProject:
    """建诗词项目 + 走完 I→P→C→K→R 循环。"""
    cfg = _merge_cfg(cfg)
    form = normalize_form(cfg.get("form", "modern"))
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _safe(cfg.get("title") or cfg.get("idea") or "未命名诗")
    pdir = safe_join(PROJECTS_DIR, base_key or f"p_{ts}-{name}", label="项目 key")
    os.makedirs(pdir, exist_ok=True)
    proj = PoetryProject(pdir, name)
    proj.running = True   # v3.1.0：流水线全程对 UI 可见为「创作中」
    llm.book(os.path.basename(pdir), "planning")

    max_rounds = int(cfg.get("maxRounds", 3))
    plan = {
        "version": 1, "poemName": name, "projectPath": pdir, "medium": "poetry",
        "form": form, "formLabel": FORMS.get(form, "现代诗"),
        "createdAt": datetime.datetime.now().isoformat(),
        "status": "planning", "maxRounds": max_rounds,
        "config": {k: v for k, v in cfg.items() if k != "api_key"},
        "meta": {"stage": "I", "rounds": 0},
    }
    proj.save_plan(plan)

    proj.log("🖋 阶段 I：释读…")
    note = gen_note(cfg)
    _write(os.path.join(pdir, F_NOTE), f"# {name} · 题记\n\n" + (note or "（释读生成失败）"))
    plan["meta"]["stage"] = "P"
    proj.save_plan(plan)

    proj.log("📋 阶段 P：创作规划…")
    plan_md = gen_plan(cfg)
    _write(os.path.join(pdir, F_IMAGERY), f"# {name} · 创作规划\n\n" + (plan_md or "（规划生成失败）"))
    plan["meta"]["stage"] = "C"
    proj.save_plan(plan)

    proj.log("✍️ 阶段 C：起草…")
    draft = gen_draft(cfg, plan_md or "")
    if not draft.strip():
        draft = "（起草失败，请重试）"
    _write(os.path.join(pdir, F_DRAFT), draft.strip() + "\n")
    plan["meta"]["stage"] = "K"
    proj.save_plan(plan)

    _check_and_finalize(proj, plan, cfg, form, draft)
    return proj


def _check_and_finalize(proj: "PoetryProject", plan: dict, cfg: dict, form: str, text: str) -> None:
    """K→R 循环 + F 定稿 + 附产（create_project 与断点恢复共用）。"""
    pdir = proj.path
    name = plan.get("poemName") or proj.name
    max_rounds = int(cfg.get("maxRounds", plan.get("maxRounds", 3)))

    # ---- K → R 循环 ----
    report: dict = {}
    if cfg.get("metrical_check", True) is False:   # 向导里可关闭格律校验（现代诗/自由诗本就不校验）
        max_rounds = 0
        plan["meta"]["verdict"] = "skipped"
        _write(os.path.join(pdir, F_CHECK),
               f"# {name} · 格律报告\n\n- 判定：**已跳过**（本次关闭了格律校验）\n")
    for rnd in range(1, max_rounds + 1):
        proj.log(f"🔍 阶段 K：格律校验（第 {rnd} 轮）…")
        report = check_draft(text, form)
        plan["meta"]["verdict"] = report.get("verdict", "")
        plan["meta"]["rounds"] = rnd
        plan["meta"]["stage"] = "K"
        proj.save_plan(plan)
        _write(os.path.join(pdir, F_CHECK),
               f"# {name} · 格律报告\n\n"
               f"- 体式：{report.get('form_label', '')}\n"
               f"- 判定：**{report.get('verdict', '')}**\n"
               f"- 轮次：第 {rnd}/{max_rounds} 轮\n\n"
               "```yaml\n" + poetry_checker.to_yaml(report) + "```\n")
        if report.get("verdict") == "pass":
            proj.log("✅ 格律通过")
            break
        if form in ("modern", "free"):
            break
        proj.log(f"🔁 阶段 R：按报告改稿（第 {rnd} 轮）…")
        revised = gen_revise(text, report, cfg)
        if not revised.strip() or revised.strip() == text.strip():
            proj.log("⚠️ 改稿无变化，停止迭代")
            break
        text = revised
        plan["meta"]["stage"] = "R"
        proj.save_plan(plan)
        _write(os.path.join(pdir, F_DRAFT), text.strip() + "\n")

    # ---- F 定稿 ----
    proj.log("📋 阶段 F：审校与定稿…")
    crit = gen_critique(text, cfg)
    header = f"# {name}\n\n"
    if crit:
        dims = crit.get("dims") or {}
        header += (f"> 总分 **{crit.get('total', 0)} / 100** ｜ {crit.get('verdict', '')}\n\n"
                   f"> 意象 {dims.get('意象', 0)} / 章法 {dims.get('章法', 0)} / "
                   f"声律 {dims.get('声律', 0)} / 情感 {dims.get('情感', 0)}\n\n")
    _write(os.path.join(pdir, F_FINAL), header + text.strip() + "\n")

    # 附产：创作手记 / 白话译文（勾选才生成，追加到定稿末尾）
    tail = ""
    for _kind, _title, _flag in (("notes", "创作手记", "with_notes"),
                                 ("translation", "白话译文", "with_translation")):
        if cfg.get(_flag) is not True:
            continue
        proj.log(f"📎 阶段 F：生成附产（{_title}）…")
        try:
            extra = gen_extra(text, cfg, _kind).strip()
        except Exception as e:
            proj.log(f"⚠️ {_title}生成失败（跳过）：{e}")
            continue
        if extra:
            tail += f"\n\n## {_title}\n\n" + extra
    if tail:
        _write(os.path.join(pdir, F_FINAL), header + text.strip() + "\n" + tail)

    plan["status"] = "completed"
    plan["meta"]["stage"] = "F"
    plan["meta"]["words"] = len(re.findall(r"[\u4e00-\u9fff]", text))
    plan["critique"] = crit
    plan["meta"]["verdict"] = report.get("verdict") or plan["meta"].get("verdict") or ""
    proj.save_plan(plan)
    proj.log("✅ 定稿完成")


def resume_project(key: str) -> PoetryProject | None:
    """断点恢复：服务重启/线程中断后，从盘上产物继续（题记/规划/初稿缺哪段补哪段）。
    同步执行（调用方放后台线程）。"""
    try:
        pdir = safe_join(PROJECTS_DIR, key, label="作品 key")
    except ValueError:
        return None
    if not os.path.isdir(pdir):
        return None
    plan = {}
    try:
        plan = json.load(open(os.path.join(pdir, F_PLAN), encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(plan, dict) or not (plan.get("config") or plan.get("poemName")):
        return None
    cfg = _merge_cfg(plan.get("config") or {})
    form = normalize_form(cfg.get("form", "modern"))
    name = plan.get("poemName") or key
    proj = PoetryProject(pdir, name)
    proj.running = True   # v3.1.0
    plan.setdefault("meta", {})
    if plan.get("status") == "completed":
        proj.log("✅ 该作品已定稿，无需恢复")
        return proj
    proj.log("▶ 断点恢复：检查已有产物…")

    note = _read(os.path.join(pdir, F_NOTE))
    if not note.strip():
        proj.log("🖋 阶段 I：释读…")
        note = gen_note(cfg) or ""
        _write(os.path.join(pdir, F_NOTE), f"# {name} · 题记\n\n" + (note or "（释读生成失败）"))
    plan_md = _read(os.path.join(pdir, F_IMAGERY))
    if not plan_md.strip():
        proj.log("📋 阶段 P：创作规划…")
        plan_md = gen_plan(cfg) or ""
        _write(os.path.join(pdir, F_IMAGERY), f"# {name} · 创作规划\n\n" + (plan_md or "（规划生成失败）"))
    draft = _read(os.path.join(pdir, F_DRAFT))
    if not draft.strip():
        proj.log("✍️ 阶段 C：起草…")
        draft = gen_draft(cfg, plan_md) or "（起草失败，请重试）"
        _write(os.path.join(pdir, F_DRAFT), draft.strip() + "\n")

    _check_and_finalize(proj, plan, cfg, form, draft.strip())
    return proj


# ---------------------------------------------------------------- 查询

def get_project(key: str) -> PoetryProject | None:
    try:
        pdir = safe_join(PROJECTS_DIR, key, label="作品 key")
    except ValueError:
        return None
    if not os.path.isdir(pdir):
        return None
    return PoetryProject(pdir, key)


def discover_projects() -> list[dict]:
    out = []
    if not os.path.isdir(PROJECTS_DIR):
        return out
    for d in sorted(os.listdir(PROJECTS_DIR), reverse=True):
        if not d.startswith("p_"):
            continue
        pdir = os.path.join(PROJECTS_DIR, d)
        if not os.path.isdir(pdir):
            continue
        plan = {}
        try:
            plan = json.load(open(os.path.join(pdir, F_PLAN), encoding="utf-8"))
        except Exception:
            pass
        meta = plan.get("meta") or {}
        out.append({
            "key": d, "name": plan.get("poemName", d), "medium": "poetry",
            "form": plan.get("formLabel", ""),
            "total": plan.get("maxRounds", 3), "done": meta.get("rounds", 0),
            "status": plan.get("status", "unknown"),
            "verdict": meta.get("verdict", ""),
            "updatedAt": plan.get("updatedAt", ""),
        })
    return out


def read_part(key: str, part: str) -> str:
    proj = get_project(key)
    if not proj:
        return ""
    mapping = {"note": F_NOTE, "imagery": F_IMAGERY, "draft": F_DRAFT,
               "check": F_CHECK, "final": F_FINAL}
    return _read(os.path.join(proj.path, mapping.get(part, part)))


def recheck(key: str) -> dict:
    """对当前草稿重新做一次格律校验（改了草稿后用）。"""
    proj = get_project(key)
    if not proj:
        return {}
    plan = proj.plan() or {}
    text = _read(os.path.join(proj.path, F_DRAFT))
    form = plan.get("form", "modern")
    report = check_draft(text, form)
    _write(os.path.join(proj.path, F_CHECK),
           f"# {proj.name} · 格律报告\n\n"
           f"- 体式：{report.get('form_label', '')}\n"
           f"- 判定：**{report.get('verdict', '')}**\n\n"
           "```yaml\n" + poetry_checker.to_yaml(report) + "```\n")
    meta = plan.setdefault("meta", {})
    meta["verdict"] = report.get("verdict", "")
    proj.save_plan(plan)
    return report


def rhyme_info(ch: str) -> dict:
    """查字的平水韵信息。"""
    return poetry_checker.rhyme_lookup(ch)


# v3.1.0：同步流水线结束时复位 running（create/resume 都经此出口）
