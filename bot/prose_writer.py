"""散文创作流水线（v1.0.0 · 神笔文学创作台）

六步工作流（对应 bot/medium.py 的 prose 阶段表）：
  B brief → S 素材 → D 起草 → A 去味 → P 精修 → F 定稿

**四操作**：write / review / refactor / recreate
  - write     从 brief 起草
  - review    只出诊断，不动一个字
  - refactor  就地最小修改
  - recreate  整篇重写

目录规范（projects/e_<key>/）：
  00-brief.md   01-素材.md   02-初稿.md
  03-去味报告.md 04-定稿.md   散文计划.json
"""
from __future__ import annotations

import datetime
import json
import os
import re
import threading

from . import llm, skillkit
from . import writer as novel_writer

ROOT = novel_writer.ROOT
PROJECTS_DIR = novel_writer.PROJECTS_DIR

F_BRIEF = "00-brief.md"
F_MATERIAL = "01-素材.md"
F_DRAFT = "02-初稿.md"
F_SLOP = "03-去味报告.md"
F_FINAL = "04-定稿.md"
F_PLAN = "散文计划.json"

OPERATIONS = {
    "write": "写（从 brief 起草）",
    "review": "审（只诊断，不改稿）",
    "refactor": "改（就地最小修改）",
    "recreate": "重写（整篇重写）",
}

GENRES = {
    "suibi": "随笔",
    "shuqing": "抒情散文",
    "yilun": "议论散文",
    "xushi": "叙事散文",
    "book": "书评 / 读后感",
    "profile": "人物特写",
    "custom": "自定义",
}


class ProseProject:
    """散文项目。字段与小说 / 剧本 / 诗词 Project 对齐。"""

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
            with open(os.path.join(self.path, F_PLAN), "w", encoding="utf-8") as f:
                json.dump(plan, f, ensure_ascii=False, indent=2)

    def status(self) -> dict:
        plan = self.plan() or {}
        meta = plan.get("meta") or {}
        return {
            "state": plan.get("status", "unknown"),
            "done": meta.get("stageIdx", 0), "total": 6,
            "words": meta.get("words", 0), "running": self.running,
            "name": plan.get("proseName", self.name), "medium": "prose",
            "form": plan.get("genreLabel", ""), "stage": meta.get("stage", ""),
            "operation": plan.get("operation", ""),
        }


# ---------------------------------------------------------------- 工具

def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "", name or "")[:40] or "未命名"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path: str) -> str:
    try:
        return open(path, encoding="utf-8").read()
    except Exception:
        return ""


def _merge_cfg(cfg: dict | None) -> dict:
    base = {"llm_timeout": 300, "call_gap": 2, "prose_words": 1500}
    try:
        d = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8")).get("defaults") or {}
    except Exception:
        d = {}
    base.update({k: v for k, v in d.items() if k.startswith("prose_")
                 or k in ("llm_timeout", "call_gap")})
    if cfg:
        base.update({k: v for k, v in cfg.items() if v is not None})
    return base


def _ctx(stage: str) -> str:
    return skillkit.stage_inject(stage, "prose")


def _gen(stage_label: str, task: str, ctx: str, cfg: dict,
         stage_code: str = "planning", max_tokens: int | None = None) -> str:
    sys_p = (f"你是一位中文写作者。当前工作阶段：{stage_label}。\n"
             f"{task}\n\n"
             f"严格要求：\n"
             f"1. 只输出成果本身，不要解释思路，不要写「好的」「以下是」。\n"
             f"2. 中文；具体优先，抽象词（显著、深刻、重要、赋能等）一律不用。\n"
             f"3. 不写 AI 腔套话与元话语（不写「本文将」「让我们一起」「不仅仅是……更是」）。\n"
             f"4. 不引用任何他人文章原文。\n"
             f"5. 交付形态：Markdown。\n")
    msgs = [{"role": "system", "content": sys_p + ("\n\n" + ctx if ctx else "")},
            {"role": "user", "content": f"请产出：{task}"}]
    try:
        return llm.chat(msgs, cfg=cfg, stage=stage_code, max_tokens=max_tokens)
    except Exception:
        return ""


def normalize_genre(g: str) -> str:
    s = (g or "").strip()
    if s in GENRES:
        return s
    for k, v in (("随笔", "suibi"), ("抒情", "shuqing"), ("议论", "yilun"),
                 ("叙事", "xushi"), ("书评", "book"), ("读后感", "book"),
                 ("特写", "profile"), ("人物", "profile")):
        if k in s:
            return v
    return "suibi"


def normalize_op(op: str) -> str:
    s = (op or "").strip().lower()
    if s in OPERATIONS:
        return s
    if "审" in s or "查" in s or "诊断" in s:
        return "review"
    if "重写" in s or "重做" in s:
        return "recreate"
    if "改" in s or "润色" in s:
        return "refactor"
    return "write"


# ---------------------------------------------------------------- 阶段

def gen_brief(cfg: dict) -> str:
    """B brief：七项必填。"""
    genre = GENRES.get(normalize_genre(cfg.get("genre", "suibi")), "随笔")
    task = (
        f"写「创作简报（brief）」，目标文体：{genre}。\n\n"
        f"必填七项，每项写清楚（用户没给的就标注「待确认」并给一个建议值）：\n"
        f"1. **体裁**\n2. **受众**（谁看？懂多少？）\n3. **目的**（读完要他做什么/记住什么）\n"
        f"4. **关系**（作者与读者的关系：陌生 / 朋友 / 权威 / 亲历者）\n"
        f"5. **语气**（克制 / 锐利 / 温和 / 冷，并给具体档位）\n"
        f"6. **约束**（目标字数、必须包含的、不能碰的）\n"
        f"7. **事实清单**（要写到的具体事实、数字、事件、引文；不确定的列出来）\n\n"
        f"用户给的内容：{cfg.get('idea') or '（未给，请拟定一个可行的 brief 并标注哪些是待确认）'}"
    )
    return _gen("散文 · Brief", task, _ctx("B"), cfg, "planning")


def gen_material(cfg: dict, brief: str) -> str:
    """S 素材：细节库 + 事实核验 + 缺口。"""
    task = (
        "写「素材与细节库」，包含：\n"
        "1. **可用细节**（8-12 条，每条：具体物 + 它承载什么信息）\n"
        "2. **事实与来源**（要用到的事实、数字、引文；标出哪些待核实）\n"
        "3. **缺口清单**（写不出来的地方用 `[待补：…]` 标记）\n"
        "4. **结构方案**（选一种：时间顺叙 / 主题辐射 / 双线交织 / 问题驱动，并说明为什么）\n"
        "5. **文风参考**（若用户给了样本，说明要采样的维度）\n\n"
        f"Brief：\n{brief[:2500]}"
    )
    return _gen("散文 · 素材", task, _ctx("S"), cfg, "planning")


def gen_draft(cfg: dict, brief: str, material: str, operation: str,
              source_text: str = "") -> str:
    """D 起草（write）或按操作处理已有稿（review/refactor/recreate）。"""
    genre = GENRES.get(normalize_genre(cfg.get("genre", "suibi")), "随笔")
    if operation == "review":
        task = (
            "审下面这篇稿子，**只出诊断，不要改动原文一个字**。\n\n"
            "按这个表格输出：\n"
            "| 位置 | 问题 | 读起来的后果 | 最小改法 |\n"
            "|---|---|---|---|\n\n"
            "要求：\n"
            "1. 分三层检查：表达层（电报体/套路词/机械重复/总结句）、"
            "用笔层（详略/过程缺失/细节无功能）、故事层（因果/立场/知情顺序/动机/变化）\n"
            "2. 每条指明具体位置，说清「不改会怎样」，给出可执行的最小改法\n"
            "3. 不要为凑数写无关紧要的建议；确实没问题的维度写「无重大问题」\n"
            "4. 最后列出「优先改的三处」\n\n"
            f"原文：\n{source_text[:10000]}"
        )
        return _gen("散文 · 审稿", task, _ctx("A"), cfg, "review", max_tokens=4000)

    if operation == "refactor":
        task = (
            "对下面这篇稿子做**就地最小修改**。\n\n"
            "原则：\n"
            "1. 每一处改动都要能说出理由（语法/歧义/与事实矛盾/违反 brief/纯填充物）\n"
            "2. **不改立意、不换结构、不换例子**\n"
            "3. 保留作者原有语气与用词偏好\n"
            "4. 输出改后的全文，然后在文末用「改动说明：」列出改了哪些地方、为什么\n\n"
            f"Brief：\n{brief[:1500]}\n\n"
            f"素材：\n{material[:1200]}\n\n"
            f"原文：\n{source_text[:10000]}"
        )
        return _gen("散文 · 修改", task, _ctx("A"), cfg, "review", max_tokens=6000)

    if operation == "recreate":
        task = (
            "把这篇稿子**整篇重写**。可以换结构、换例子、换角度，但要保留核心意思与事实。\n\n"
            "要求：\n"
            "1. 先在「重写思路」里写：新结构是什么、为什么比原结构好、保留了哪些事实\n"
            "2. 然后输出「重写稿」全文\n"
            "3. 最后写「改了什么」清单\n\n"
            f"Brief：\n{brief[:1500]}\n\n"
            f"素材：\n{material[:1200]}\n\n"
            f"原文：\n{source_text[:8000]}"
        )
        return _gen("散文 · 重写", task, _ctx("D"), cfg, "writing", max_tokens=6000)

    task = (
        f"按下面的 brief 与素材，写一篇{genre}。\n\n"
        f"要求：\n"
        f"1. 只输出文章正文，不要标题行、不要说明。\n"
        f"2. 具体优先：能用具体的东西就不用抽象词。\n"
        f"3. 不写 AI 腔套话（至关重要/不可或缺/不仅……更是/让我们一起）与元话语。\n"
        f"4. 每段一件事；段首句是这一段的主张；不要用总结句收尾。\n"
        f"5. 事实按素材里的用；缺的地方保留 `[待补：…]` 标记，不要用模糊表述填空。\n"
        f"6. 结尾落在一个具体的画面或结论上，不落在「总而言之」。\n\n"
        f"Brief：\n{brief[:2000]}\n\n"
        f"素材：\n{material[:1800]}"
    )
    return _gen("散文 · 起草", task, _ctx("D"), cfg, "writing", max_tokens=6000)


def gen_polish(text: str, cfg: dict) -> str:
    """P 精修：结构 / 音律 / 事实 / 语气打磨 + preflight。"""
    task = (
        "对下面这篇稿子做最终精修，**直接输出改后的全文**。\n\n"
        "精修只处理这四类，不要重写：\n"
        "1. **结构**：段落职责不清、顺序别扭的地方\n"
        "2. **音律**：长短句节奏、连续三段雷同、念起来卡的地方\n"
        "3. **事实**：前后矛盾、表述含混、缺来源的说法\n"
        "4. **语气**：与 brief 不符、跑偏的段落\n\n"
        "然后另起一段写「preflight 结论」，逐条确认：体裁明确 / 受众匹配 / 目的达成 / "
        "有具体事实 / 段落各讲一件事 / 无套话 / 无元话语 / 引用可追溯 / 结尾落得住 / 字数达标。"
        "有未过的写明原因。\n\n"
        f"稿件：\n{text[:10000]}"
    )
    return _gen("散文 · 精修", task, _ctx("P"), cfg, "review", max_tokens=7000)


# ---------------------------------------------------------------- 生命周期

def create_project(cfg: dict, base_key: str = "") -> ProseProject:
    """建散文项目并按操作跑完流程。"""
    cfg = _merge_cfg(cfg)
    op = normalize_op(cfg.get("operation", "write"))
    genre = normalize_genre(cfg.get("genre", "suibi"))
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = _safe(cfg.get("title") or cfg.get("idea") or "未命名散文")
    pdir = os.path.join(PROJECTS_DIR, base_key or f"e_{ts}-{name}")
    os.makedirs(pdir, exist_ok=True)
    proj = ProseProject(pdir, name)
    llm.book(os.path.basename(pdir), "planning")

    source_text = cfg.get("sourceText") or ""
    plan = {
        "version": 1, "proseName": name, "projectPath": pdir, "medium": "prose",
        "operation": op, "operationLabel": OPERATIONS[op],
        "genre": genre, "genreLabel": GENRES.get(genre, "随笔"),
        "createdAt": datetime.datetime.now().isoformat(),
        "status": "planning",
        "config": {k: v for k, v in cfg.items() if k != "api_key"},
        "meta": {"stage": "B", "stageIdx": 0},
    }
    proj.save_plan(plan)

    proj.log("📋 阶段 B：创作简报…")
    brief = gen_brief(cfg)
    _write(os.path.join(pdir, F_BRIEF), f"# {name} · 创作简报\n\n" + (brief or "（简报生成失败）"))
    plan["meta"]["stage"] = "S"
    plan["meta"]["stageIdx"] = 1
    proj.save_plan(plan)

    proj.log("🗂 阶段 S：素材与细节库…")
    material = gen_material(cfg, brief or "")
    _write(os.path.join(pdir, F_MATERIAL), f"# {name} · 素材\n\n" + (material or "（素材生成失败）"))
    plan["meta"]["stage"] = "D"
    plan["meta"]["stageIdx"] = 2
    proj.save_plan(plan)

    proj.log(f"✍️ 阶段 D：{OPERATIONS[op]}…")
    draft = gen_draft(cfg, brief or "", material or "", op, source_text)
    if not draft.strip():
        draft = "（生成失败，请重试）"
    _write(os.path.join(pdir, F_DRAFT), draft.strip() + "\n")
    plan["meta"]["stage"] = "A"
    plan["meta"]["stageIdx"] = 3
    proj.save_plan(plan)

    # review 模式到此为止（不动原文）
    if op == "review":
        _write(os.path.join(pdir, F_SLOP), draft.strip() + "\n")
        plan["status"] = "completed"
        plan["meta"]["stage"] = "A"
        plan["meta"]["stageIdx"] = 4
        plan["meta"]["words"] = len(re.findall(r"[\u4e00-\u9fff]", draft))
        proj.save_plan(plan)
        proj.log("✅ 审稿完成（未改动原文）")
        return proj

    proj.log("🔍 阶段 A：去味与精修…")
    polished = gen_polish(draft, cfg)
    if not polished.strip():
        polished = draft
    _write(os.path.join(pdir, F_SLOP), polished.strip() + "\n")
    plan["meta"]["stage"] = "P"
    plan["meta"]["stageIdx"] = 4
    proj.save_plan(plan)

    proj.log("📋 阶段 F：定稿…")
    _write(os.path.join(pdir, F_FINAL), f"# {name}\n\n" + polished.strip() + "\n")
    plan["status"] = "completed"
    plan["meta"]["stage"] = "F"
    plan["meta"]["stageIdx"] = 5
    plan["meta"]["words"] = len(re.findall(r"[\u4e00-\u9fff]", polished))
    proj.save_plan(plan)
    proj.log("✅ 定稿完成")
    return proj


# ---------------------------------------------------------------- 查询

def get_project(key: str) -> ProseProject | None:
    pdir = os.path.join(PROJECTS_DIR, key)
    if not os.path.isdir(pdir):
        return None
    return ProseProject(pdir, key)


def discover_projects() -> list[dict]:
    out = []
    if not os.path.isdir(PROJECTS_DIR):
        return out
    for d in sorted(os.listdir(PROJECTS_DIR), reverse=True):
        if not d.startswith("e_"):
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
            "key": d, "name": plan.get("proseName", d), "medium": "prose",
            "form": plan.get("genreLabel", ""),
            "operation": plan.get("operationLabel", ""),
            "total": 6, "done": meta.get("stageIdx", 0),
            "status": plan.get("status", "unknown"),
            "updatedAt": plan.get("updatedAt", ""),
        })
    return out


def read_part(key: str, part: str) -> str:
    proj = get_project(key)
    if not proj:
        return ""
    mapping = {"brief": F_BRIEF, "material": F_MATERIAL, "draft": F_DRAFT,
               "slop": F_SLOP, "final": F_FINAL}
    return _read(os.path.join(proj.path, mapping.get(part, part)))
