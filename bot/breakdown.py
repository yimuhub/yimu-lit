"""拆书工作台（v5.4.0，docs/系统化升级方案 8.1，全部自研实现）。

把一本外部作品（上传 txt/md/docx/epub 或粘贴全文）拆成「章 → 结构化分析 → 全书报告」：

1. 自动切章（自研防误切规则，见 split_chapters 的规则说明）；
2. 逐章拆解：每章一次结构化 JSON 调用（段落编号 [n] 送入，超长章取前 400 段），
   前三章附加「黄金三章」深拆（golden 字段）；
3. 全书报告：聚合各章梗概再调一次 → 编辑点评 / 大纲还原 / 人物弧光 / 伏笔账本 /
   节奏曲线 / 可复用手法清单（每条带证据引用，证据必须逐字命中原文，未命中丢弃）；
4. 项目存储 data/breakdowns/<id>.json（列表 + 各章 analysis + 报告 + 人物名聚合；
   失败章可重试）+ 进度轮询；
5. 手法入账本：选中手法写入现有 bot/lexicon_ledger（复用其数据结构与 API——
   {gram: {count, chapters, last}} 与 record()，入账到拆书项目自己的账本目录；
   传 project_key 时入账到真实作品目录，与现有体系闭环）。

边界与纪律
- 拆书是附加能力：LLM 失败章可重试可弃，绝不影响主创作链路；本模块不写任何 projects/ 正文。
- 上传安全完全沿用 bot/ingest.py 的白名单 / 大小 / 文件头校验。
- 纯净域：拆书对象是外部文本，不涉及正文出口；入账的手法卡不带正文原文片段
  （只带手法描述 + 证据位置），描述里检测到原文逐字片段也会被拒。
- 后台任务形态（说明）：拆书任务轻量（逐章串行调 LLM），用「守护线程 + 项目 JSON
  进度落盘」即可，不进 bot/jobs.json 全局任务表——进度轮询端点直接读项目文件，
  进程重启后项目停在最后一次落盘状态，失败章可重试，不需要断点续跑注册表。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import threading
import uuid
from collections import Counter

from . import ingest, lexicon_ledger, llm
from .paths import data
from .safeio import atomic_write_json, safe_join

# ---------------------------------------------------------------- 常量（tests monkeypatch DATA_DIR 即可整体隔离）

DATA_DIR = data("breakdowns")      # 项目存储根：data/breakdowns/<id>.json
MAX_PROJECTS = 50                  # 项目存储轮转上限：超出删最旧的未运行项目
MAX_TEXT_CHARS = 3_000_000         # 单本拆书对象字数上限（防超大 JSON 与失控任务）
MAX_CHAPTERS = 500                 # 切章数量上限（防几千章连载一次发起几千次 LLM 调用）
MAX_PARAS_PER_CH = 400             # 超长章取前 400 段送拆
MAX_INPUT_CHARS = 30_000           # 单章送 LLM 的字符上限（与段数上限取交集）
TITLE_MAX = 60                     # 章名（章号后的文字）长度上限
LINE_MAX = 80                      # 标题行总长上限（含「第N章」前缀）
GOLDEN_COUNT = 3                   # 前三章做「黄金三章」深拆
GAP_MAX = 60                       # 切章链上相邻章号的最大合法跳跃
MIN_EVIDENCE_CHARS = 8             # 证据规范化后的最短长度（太短无法可靠定位，视为无效）

# 运行态注册表（防重入；全部读写进锁，TOCTOU 安全）
_RUN_LOCK = threading.Lock()
_RUNNING: set[str] = set()

_ID_RE = re.compile(r"^bd-\d{14}-[0-9a-f]{6}$")

# 拆解可用的节奏标记类型（超出按字符串原样保留，前端只对已知类型做图标）
RHYTHM_TYPES = ("hook", "conflict", "twist", "climax", "cool", "turn", "setup")


# ---------------------------------------------------------------- 中文数字转换

_CN_DIG = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def cn_to_int(s: str) -> int:
    """中文数字（含万/零/两，支持混写阿拉伯数字）→ int；非法返回 -1。

    「第一万二千三百章」→ 12300；「十二」→ 12；「一百零三」→ 103；
    「第一三章」→ 13（连载常见的省「百」写法，按位拼接处理）。
    """
    s = str(s or "").strip()
    if not s:
        return -1
    if s.isdigit():
        return int(s)
    total, section, pending, saw = 0, 0, 0, False
    for ch in s:
        if ch in _CN_DIG:
            pending = pending * 10 + _CN_DIG[ch]   # 「一三」= 13（无单位按位拼）
            saw = True
        elif ch in _CN_UNIT:
            if pending == 0 and not saw:
                pending = 1            # 「十二」开头省略「一」
            section += pending * _CN_UNIT[ch]
            pending = 0
            saw = True
        elif ch == "万":
            section += pending
            pending = 0
            total = (total + section) * 10000
            section = 0
            saw = True
        else:
            return -1
    return total + section + pending


# ---------------------------------------------------------------- 切章（自研防误切）

_NUM_CLS = r"[0-9零〇一二两三四五六七八九十百千万]+"
# 章标题：第N章/回/节/篇（N 为阿拉伯或中文数字）
_CH_LINE = re.compile(rf"^第\s*({_NUM_CLS})\s*[章回节篇]\s*(.*)$")
# 卷标题：第N卷/部 或 卷N（不拆章，只做分组展示）
_VOL_LINE = re.compile(rf"^第\s*({_NUM_CLS})\s*[卷部]\s*(.*)$|^卷\s*({_NUM_CLS})\s*(.*)$")
# 带句号/叹号/问号（及同族分号省略号）的行不是标题
_BAD_TITLE_PUNCT = "。！？；…!?;"

_DECO_LEAD = re.compile(r"^[#\s>\-\*·•【】\[\u300c\u300d]+")
_DECO_BOTH = "【】「」『』《》 \u3000"


def _strip_deco(line: str) -> str:
    """剥掉行首 Markdown / 列表装饰与两侧括号书名号，再判标题。"""
    s = _DECO_LEAD.sub("", (line or "").strip())
    return s.strip(_DECO_BOTH).strip()


def _chapter_candidate(lines: list[str], i: int) -> dict | None:
    """判定第 i 行是否是「章标题候选」。候选≠标题——还要过单调链筛选。

    防误切规则（设计稿 8.1 + 实现补充）：
      1) 行剥装饰后总长 ≤ LINE_MAX（正文行一般更长）；
      2) 行内不带句号/叹号/问号/分号/省略号（「带句号的行不算标题」）；
      3) 章名（章号后的文字）≤ TITLE_MAX 字（超长标题视为正文）；
      4) 章号可解析为正整数（中文数字含万）。
    """
    s = _strip_deco(lines[i])
    if not s or len(s) > LINE_MAX:
        return None
    if any(p in s for p in _BAD_TITLE_PUNCT):
        return None
    m = _CH_LINE.match(s)
    if not m:
        return None
    num = cn_to_int(m.group(1))
    if num <= 0:
        return None
    title = m.group(2).strip(" ：:-—·")
    if len(title) > TITLE_MAX:
        return None
    standalone = i > 0 and not lines[i - 1].strip()
    return {"i": i, "num": num, "title": title, "line": s,
            "score": 2 if standalone else 0}


def _volume_candidate(lines: list[str], i: int) -> dict | None:
    """判定第 i 行是否是卷标题（卷只做分组展示，不影响切章；防误切靠短行 + 无句读 + 章名上限）。

    正文提及「第三卷」一般带上下文与标点，会被句读守卫拦下；
    卷标题不要求独立成行（不少电子书正文与卷标题之间没有空行）。
    """
    s = _strip_deco(lines[i])
    if not s or len(s) > LINE_MAX:
        return None
    if any(p in s for p in _BAD_TITLE_PUNCT):
        return None
    m = _VOL_LINE.match(s)
    if not m:
        return None
    raw = m.group(1) or m.group(3) or ""
    num = cn_to_int(raw)
    if num <= 0:
        return None
    title = (m.group(2) or m.group(4) or "").strip(" ：:-—·")
    if len(title) > TITLE_MAX:
        return None
    return {"i": i, "num": num, "title": title, "line": s}


def _pick_chain(cands: list[dict]) -> list[dict]:
    """从候选里挑最优「章号严格递增链」（序号单调性校验 + 序号不增的是正文回指）。

    动态规划：链上相邻候选 0 < gap ≤ GAP_MAX（防正文里提的巨大章号串链），
    最大化 (链长, 独立行加分)——正文回指即使恰好递增，链长也敌不过真章序列。
    """
    if not cands:
        return []
    n = len(cands)
    # best[j] = (链长, 分数, 前驱下标)
    best = [(1, c["score"], -1) for c in cands]
    for j in range(n):
        cj = cands[j]
        for i in range(j):
            gap = cj["num"] - cands[i]["num"]
            if 0 < gap <= GAP_MAX:
                li, si, _pi = best[i]
                if (li + 1, si + cj["score"]) > best[j][:2]:
                    best[j] = (li + 1, si + cj["score"], i)
    j = max(range(n), key=lambda k: best[k][:2])
    chain = []
    while j != -1:
        chain.append(cands[j])
        j = best[j][2]
    chain.reverse()
    return chain


def _paras_of(lines: list[str], start: int, end: int) -> list[str]:
    """[start, end) 行区间 → 段落列表（空行分段；段内多行用 \n 连接）。"""
    paras: list[str] = []
    cur: list[str] = []
    for ln in lines[start:end]:
        if ln.strip():
            cur.append(ln.rstrip())
        elif cur:
            paras.append("\n".join(cur))
            cur = []
    if cur:
        paras.append("\n".join(cur))
    return paras


def split_chapters(text: str) -> dict:
    """自动切章主入口。返回 {ok, chapters, volumes, warnings, chars}。

    识别不到任何章时整本作为单章并给警告（不抛错——无章作品也能拆）。
    """
    clean = ingest.clean_text(text or "")
    if not clean.strip():
        return {"ok": False, "error": "没有可拆的正文文本"}
    if len(clean) > MAX_TEXT_CHARS:
        return {"ok": False,
                "error": f"文本超过 {MAX_TEXT_CHARS // 10000} 万字上限（当前约 {len(clean) // 10000} 万字），请分段拆解"}

    lines = clean.split("\n")
    warnings: list[str] = []
    # 标题候选全量扫描（一行一次正则，百万行内可接受）；候选数封顶防 DP 平方级失控
    scan_to = len(lines)
    cand_cap = 4000
    cands: list[dict] = []
    for i in range(scan_to):
        c = _chapter_candidate(lines, i)
        if c:
            cands.append(c)
            if len(cands) >= cand_cap:
                warnings.append(f"章标题候选超过 {cand_cap} 个，超出部分未参与切章")
                break
    chain = _pick_chain(cands)

    # 单个候选不成证据：只接受「开头 30 行内、章号 ≤ 200」这类可信的第一章；
    # 其余孤立标记（正文中段的回指、巨大章号）宁可整本单章也不拦腰错切
    if len(chain) == 1:
        c0 = chain[0]
        if not (c0["i"] <= 30 and c0["num"] <= 200):
            chain = []
    if len(chain) > MAX_CHAPTERS:
        chain = chain[:MAX_CHAPTERS]
        warnings.append(f"章数超过 {MAX_CHAPTERS} 上限，仅保留前 {MAX_CHAPTERS} 章")

    vol_cands = [v for v in (_volume_candidate(lines, i) for i in range(scan_to)) if v][:500]

    chapters: list[dict] = []
    volumes: list[dict] = []
    if chain:
        last_vol_i = -1
        for k, c in enumerate(chain):
            body_end = chain[k + 1]["i"] if k + 1 < len(chain) else len(lines)
            paras = _paras_of(lines, c["i"] + 1, body_end)
            vol = None
            for v in vol_cands:
                if last_vol_i < v["i"] < c["i"]:
                    vol = v
            if vol:
                last_vol_i = vol["i"]
                volumes.append({"no": len(volumes) + 1, "num": vol["num"],
                                "title": vol["title"] or f"第{vol['num']}卷",
                                "line": vol["line"]})
            chapters.append({
                "no": k + 1,
                "num": c["num"],
                "title": c["line"],
                "volume": volumes[-1]["line"] if volumes else "",
                "paras": paras,
                "chars": sum(len(p.replace("\n", "")) for p in paras),
                "state": "pending",
                "error": "",
                "golden": k < GOLDEN_COUNT,
                "analysis": None,
            })
    else:
        paras = _paras_of(lines, 0, len(lines))
        chapters.append({
            "no": 1, "num": 1, "title": "（未识别章节）",
            "volume": "", "paras": paras,
            "chars": sum(len(p.replace("\n", "")) for p in paras),
            "state": "pending", "error": "", "golden": True, "analysis": None,
        })
        warnings.append("未识别到「第N章/回/节」标题（可能无章作品或格式特殊），已整本按单章处理")

    # 第一个被采纳的章标题之前的文本（书名页/版权/简介等前置内容）不进任何章；
    # 体量可观时如实提醒，避免「内容少了」被当成丢稿。
    if chain and chain[0]["i"] > 0:
        front_chars = sum(len(ln) for ln in lines[:chain[0]["i"]])
        if front_chars >= 200:
            warnings.append(f"第一个章标题前有约 {front_chars} 字前置文本（书名页/简介等），未纳入任何章")

    return {"ok": True, "chapters": chapters, "volumes": volumes,
            "warnings": warnings, "chars": len(clean)}


# ---------------------------------------------------------------- 项目存储

def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def new_id() -> str:
    return f"bd-{datetime.datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"


def _bd_path(bd_id: str) -> str:
    """项目 JSON 路径。id 必须匹配白名单格式（防路径逃逸），否则抛 ValueError。"""
    bd_id = str(bd_id or "").strip()
    if not _ID_RE.match(bd_id):
        raise ValueError("拆书项目 id 非法")
    return os.path.join(DATA_DIR, bd_id + ".json")


def _ledger_dir(bd_id: str) -> str:
    """拆书项目自己的手法账本目录（lexicon_ledger 以目录为单位记账）。"""
    d = os.path.join(DATA_DIR, str(bd_id))
    os.makedirs(d, exist_ok=True)
    return d


def load_project(bd_id: str) -> dict | None:
    try:
        p = _bd_path(bd_id)
    except ValueError:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def save_project(bd: dict) -> None:
    bd["updated_at"] = _now()
    atomic_write_json(_bd_path(bd["id"]), bd)


def is_running(bd_id: str) -> bool:
    with _RUN_LOCK:
        return str(bd_id) in _RUNNING


def _progress_of(bd: dict) -> dict:
    chapters = bd.get("chapters") or []
    done = sum(1 for c in chapters if c.get("state") == "done")
    failed = sum(1 for c in chapters if c.get("state") == "failed")
    current = next((c for c in chapters if c.get("state") == "running"), None)
    return {"total": len(chapters), "done": done, "failed": failed,
            "current": current["no"] if current else None,
            "currentTitle": current["title"] if current else ""}


def _refresh_status(bd: dict) -> str:
    """按章节状态刷新项目状态：有失败=partial、还有待拆=draft、全完成=done。"""
    chapters = bd.get("chapters") or []
    if any(c.get("state") == "failed" for c in chapters):
        bd["status"] = "partial"
    elif any(c.get("state") == "pending" for c in chapters):
        bd["status"] = "draft"
    else:
        bd["status"] = "done"
    return bd["status"]


def _summary_of(bd: dict) -> dict:
    pg = _progress_of(bd)
    return {"id": bd.get("id"), "title": bd.get("title"), "source": bd.get("source"),
            "filename": bd.get("filename"), "status": bd.get("status"),
            "chars": bd.get("chars"), "created_at": bd.get("created_at"),
            "updated_at": bd.get("updated_at"),
            "chapters": pg["total"], "done": pg["done"], "failed": pg["failed"],
            "has_report": bool(bd.get("report")),
            "adopted": len(bd.get("adopted_techniques") or []),
            "running": is_running(bd.get("id"))}


def list_projects() -> list[dict]:
    """项目列表（按更新时间倒序）。单个文件读挂了跳过，不影响列表。"""
    out = []
    try:
        names = os.listdir(DATA_DIR)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        bd = load_project(name[:-5])
        if bd:
            out.append(_summary_of(bd))
    out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return out


def _rotate(reserve: int = 0) -> None:
    """项目存储轮转：加上即将新增的 reserve 个后超出 MAX_PROJECTS 时，删最旧的未运行项目。"""
    items = list_projects()
    overflow = len(items) + max(0, int(reserve)) - MAX_PROJECTS
    if overflow <= 0:
        return
    # 只删非运行态；最旧优先（list 已按 updated_at 倒序）
    victims = [x for x in reversed(items) if not x.get("running")]
    for v in victims[:overflow]:
        try:
            delete_project(v["id"])
        except Exception:
            pass


def delete_project(bd_id: str) -> dict:
    if is_running(bd_id):
        return {"ok": False, "note": "拆解正在进行中，请等结束或先不再运行后再删除"}
    try:
        p = _bd_path(bd_id)
    except ValueError:
        return {"ok": False, "note": "拆书项目 id 非法"}
    removed = False
    try:
        if os.path.exists(p):
            os.unlink(p)
            removed = True
    except OSError as e:
        return {"ok": False, "note": f"删除失败：{e}"}
    # 顺手清账本目录（若有）
    d = os.path.join(DATA_DIR, str(bd_id))
    if os.path.isdir(d):
        try:
            ledger = os.path.join(d, lexicon_ledger.LEDGER_NAME)
            if os.path.exists(ledger):
                os.unlink(ledger)
            if not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    return {"ok": True, "removed": removed}


def create_project(title: str, text: str, source: str = "paste",
                   filename: str = "") -> dict:
    """新建拆书项目：切章 + 落盘。返回 {ok, project, warnings, error}。"""
    sp = split_chapters(text)
    if not sp.get("ok"):
        return {"ok": False, "error": sp.get("error") or "切章失败"}
    bd = {
        "version": 1,
        "id": new_id(),
        "title": (str(title or "").strip() or "未命名拆书")[:80],
        "source": source,
        "filename": filename[:120],
        "created_at": _now(),
        "updated_at": _now(),
        "status": "draft",
        "chars": sp["chars"],
        "warnings": sp["warnings"],
        "volumes": sp["volumes"],
        "chapters": sp["chapters"],
        "report": None,
        "characters": [],
        "adopted_techniques": [],
        "progress": {},
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        _rotate(reserve=1)
        save_project(bd)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"项目保存失败：{e}"}
    return {"ok": True, "project": bd, "warnings": sp["warnings"]}


def resplit_project(bd_id: str) -> dict:
    """整体重切：把现有章节段落重新拼回全文再切一次（章节状态与分析清零）。"""
    if is_running(bd_id):
        return {"ok": False, "note": "拆解正在进行中，不能重切"}
    bd = load_project(bd_id)
    if not bd:
        return {"ok": False, "note": "拆书项目不存在"}
    text = "\n\n".join(p for c in (bd.get("chapters") or []) for p in (c.get("paras") or []))
    sp = split_chapters(text)
    if not sp.get("ok"):
        return {"ok": False, "note": sp.get("error") or "重切失败"}
    bd["chapters"] = sp["chapters"]
    bd["volumes"] = sp["volumes"]
    bd["warnings"] = sp["warnings"]
    bd["chars"] = sp["chars"]
    bd["report"] = None
    bd["characters"] = []
    bd["status"] = "draft"
    bd["progress"] = {}
    save_project(bd)
    return {"ok": True}


# ---------------------------------------------------------------- 逐章拆解

def _chapter_messages(bd: dict, chapter: dict) -> list[dict]:
    """组装单章拆解 messages：段落编号 [n] 送入，超长章取前 400 段并截字符。"""
    paras = chapter.get("paras") or []
    pieces: list[str] = []
    used = 0
    for i, p in enumerate(paras[:MAX_PARAS_PER_CH], 1):
        piece = f"[{i}] {p}"
        if pieces and used + len(piece) > MAX_INPUT_CHARS:
            break
        pieces.append(piece)
        used += len(piece)
    truncated = len(pieces) < len(paras)
    golden = bool(chapter.get("golden"))
    sys_p = (
        "你是资深网文编辑，正在做「拆书」结构分析：把一章拆成可供作者复用的结构化笔记。"
        "只输出严格 JSON（不要解释、不要 markdown 围栏），字段：\n"
        '{"brief":"150字内本章梗概",'
        '"rhythm":[{"para":段落号,"type":"hook|conflict|twist|climax|cool|turn|setup","note":"一句话说明"}],'
        '"conflict":"本章核心冲突一句话","value_shift":"价值翻转（如 安全→危险、卑微→扬眉吐气）",'
        '"hook":{"type":"悬念钩|危机钩|期待钩|情绪钩|信息钩|无","text":"章末钩子一句话"},'
        '"outline":[{"startPara":1,"endPara":5,"title":"节点小标题","summary":"一句话"}],'
        '"techniques":["本章可复用写作手法标签，3-6个"],"characters":["出场人物名"]}'
        "\nrhythm/outline 的段落号必须使用上面 [n] 编号；outline 节点按顺序给出段落区间。"
        "手法标签写抽象写法（如「章末反转钩」「情绪压—放」），不要复述原文句子。"
    )
    if golden:
        sys_p += (
            "\n本章属于全书前三章（黄金三章），请额外给出 golden 字段：\n"
            '{"golden":{"opening_hook":"开篇钩子怎么下、第几段兑现",'
            '"immersion":"代入感手法（视角/身份困境/金手指如何给出）",'
            '"info_pacing":"信息释放节奏（设定何时给、 withheld 什么）"}}'
        )
    if truncated:
        sys_p += f"\n注意：本章过长，仅给出前 {len(pieces)} 段的分析。"
    user_p = (f"【书名】{bd.get('title', '')}\n【章号】{chapter.get('title', '')}\n\n"
              "【本章段落（[n] 为段落号）】\n" + "\n".join(pieces))
    return [{"role": "system", "content": sys_p},
            {"role": "user", "content": user_p}]


def _call_json(messages: list[dict], cfg: dict | None):
    """LLM JSON 调用（scene 标签「拆书」）；失败抛异常，由调用方决定重试。"""
    return llm.chat_json(messages, temperature=0.35, cfg=cfg, stage="planning", scene="拆书")


def _clamp_str(v, limit: int) -> str:
    return str(v or "").strip()[:limit]


def _coerce_analysis(raw, chapter: dict, golden: bool) -> dict | None:
    """把模型返回的任意 JSON 整形为合法 analysis；缺关键字段时返回 None（视为解析失败）。"""
    if not isinstance(raw, dict):
        return None
    n = len(chapter.get("paras") or [])
    brief = _clamp_str(raw.get("brief"), 400)
    conflict = _clamp_str(raw.get("conflict"), 400)
    value_shift = _clamp_str(raw.get("value_shift"), 200)
    outline_raw = raw.get("outline") if isinstance(raw.get("outline"), list) else []
    outline = []
    for o in outline_raw[:12]:
        if not isinstance(o, dict):
            continue
        s, e = o.get("startPara"), o.get("endPara")
        try:
            s = max(1, min(int(s), n))
            e = max(1, min(int(e), n))
        except (TypeError, ValueError):
            continue
        if s > e:
            s, e = e, s
        outline.append({"startPara": s, "endPara": e,
                        "title": _clamp_str(o.get("title"), 40),
                        "summary": _clamp_str(o.get("summary"), 200)})
    outline.sort(key=lambda x: x["startPara"])
    rhythm = []
    for r in (raw.get("rhythm") if isinstance(raw.get("rhythm"), list) else [])[:12]:
        if not isinstance(r, dict):
            continue
        try:
            para = max(1, min(int(r.get("para")), n))
        except (TypeError, ValueError):
            continue
        rhythm.append({"para": para,
                       "type": _clamp_str(r.get("type"), 10) or "turn",
                       "note": _clamp_str(r.get("note"), 100)})
    rhythm.sort(key=lambda x: x["para"])
    hook = raw.get("hook") if isinstance(raw.get("hook"), dict) else {}
    techniques = [_clamp_str(t, 24) for t in (raw.get("techniques") if isinstance(raw.get("techniques"), list) else [])
                  if _clamp_str(t, 24)][:8]
    characters = [_clamp_str(c, 20) for c in (raw.get("characters") if isinstance(raw.get("characters"), list) else [])
                  if _clamp_str(c, 20)][:20]
    golden_out = None
    if golden:
        g = raw.get("golden") if isinstance(raw.get("golden"), dict) else {}
        golden_out = {"opening_hook": _clamp_str(g.get("opening_hook"), 300),
                      "immersion": _clamp_str(g.get("immersion"), 300),
                      "info_pacing": _clamp_str(g.get("info_pacing"), 300)}
    # 关键字段全空 = 模型没有真正产出（如 mock 返回 {}）→ 视为解析失败
    if not brief and not conflict and not outline and not rhythm:
        return None
    return {"brief": brief, "rhythm": rhythm, "conflict": conflict,
            "value_shift": value_shift,
            "hook": {"type": _clamp_str(hook.get("type"), 10) or "无",
                     "text": _clamp_str(hook.get("text"), 200)},
            "outline": outline, "techniques": techniques,
            "characters": characters, "golden": golden_out,
            "para_count": n}


def analyze_chapter(bd: dict, chapter: dict, cfg: dict | None = None) -> bool:
    """拆一章（原地写 chapter["analysis"] 与 chapter["state"]）。JSON 解析失败自动重试一次。"""
    msgs = _chapter_messages(bd, chapter)
    golden = bool(chapter.get("golden"))
    last_err = ""
    for attempt in range(2):
        try:
            raw = _call_json(msgs if attempt == 0 else msgs + [
                {"role": "user",
                 "content": "上一次输出不是合法的结构化 JSON 或缺少字段。请严格按系统提示的字段定义，只输出一个 JSON 对象。"}
            ], cfg)
            analysis = _coerce_analysis(raw, chapter, golden)
        except Exception as e:            # 传输/解析异常都算这次失败，进入重试
            analysis = None
            last_err = str(e)[:300]
        if analysis is not None:
            chapter["analysis"] = analysis
            chapter["error"] = ""
            chapter["state"] = "done"
            return True
        if not last_err:
            last_err = "模型没有返回有效的结构化结果"
    chapter["analysis"] = None
    chapter["error"] = last_err or "拆解失败"
    chapter["state"] = "failed"
    return False


def aggregate_characters(bd: dict) -> list[dict]:
    """人物名滚动聚合：各章 analysis.characters 计数，取前 50。"""
    cnt: Counter = Counter()
    for c in bd.get("chapters") or []:
        a = c.get("analysis") or {}
        for name in (a.get("characters") or []):
            name = str(name or "").strip()
            if name:
                cnt[name] += 1
    return [{"name": k, "count": v} for k, v in cnt.most_common(50)]


# ---------------------------------------------------------------- 后台拆解任务（线程 + 进度落盘）

def run_breakdown(bd_id: str, cfg: dict | None = None) -> dict:
    """启动拆解后台任务（守护线程，逐章串行；进度即时落盘供轮询）。"""
    with _RUN_LOCK:
        if str(bd_id) in _RUNNING:
            return {"ok": False, "note": "拆解已在进行中"}
        bd = load_project(bd_id)
        if not bd:
            return {"ok": False, "note": "拆书项目不存在"}
        pending = [c for c in bd.get("chapters") or [] if c.get("state") != "done"]
        if not pending:
            return {"ok": False, "note": "全部章节都已拆解完成，无需再次运行"}
        _RUNNING.add(str(bd_id))

    bd["status"] = "running"
    bd["progress"] = _progress_of(bd)
    try:
        save_project(bd)
    except (OSError, ValueError):
        with _RUN_LOCK:
            _RUNNING.discard(str(bd_id))
        return {"ok": False, "note": "项目保存失败"}

    def _worker():
        try:
            for c in pending:
                with _RUN_LOCK:
                    alive = str(bd_id) in _RUNNING
                if not alive:
                    return
                c["state"] = "running"
                c["error"] = ""
                bd["progress"] = _progress_of(bd)
                try:
                    save_project(bd)
                except (OSError, ValueError):
                    pass
                ok = analyze_chapter(bd, c, cfg)   # state 由 analyze_chapter 写（done/failed）
                bd["progress"] = _progress_of(bd)
                bd["characters"] = aggregate_characters(bd)
                try:
                    save_project(bd)
                except (OSError, ValueError):
                    pass
            _refresh_status(bd)
        except Exception as e:            # 拆书绝不影响主链路：异常降级为 partial
            bd["status"] = "partial"
            bd["error"] = str(e)[:300]
        finally:
            with _RUN_LOCK:
                _RUNNING.discard(str(bd_id))
            bd["progress"] = _progress_of(bd)
            try:
                save_project(bd)
            except (OSError, ValueError):
                pass

    threading.Thread(target=_worker, daemon=True, name=f"breakdown-{bd_id}").start()
    return {"ok": True, "id": bd_id, "status": "running", "pending": len(pending)}


def retry_chapter(bd_id: str, no: int, cfg: dict | None = None) -> dict:
    """重试单个失败章（同步执行：单章一次 LLM 调用，前端轮询进度即可看到结果）。"""
    if is_running(bd_id):
        return {"ok": False, "note": "拆解正在进行中，请等结束后再重试"}
    bd = load_project(bd_id)
    if not bd:
        return {"ok": False, "note": "拆书项目不存在"}
    chapter = next((c for c in bd.get("chapters") or [] if c.get("no") == no), None)
    if not chapter:
        return {"ok": False, "note": f"第 {no} 章不存在"}
    chapter["state"] = "running"
    chapter["error"] = ""
    save_project(bd)
    ok = analyze_chapter(bd, chapter, cfg)   # state 由 analyze_chapter 写（done/failed）
    bd["characters"] = aggregate_characters(bd)
    _refresh_status(bd)
    save_project(bd)
    out = {"ok": True, "chapter": {"no": chapter["no"], "state": chapter["state"],
                                   "error": chapter.get("error", "")}}
    if not ok:
        out["note"] = chapter.get("error") or "拆解失败，可再次重试"
    return out


# ---------------------------------------------------------------- 全书报告

REPORT_CAP = 60   # 报告聚合的章数上限（更多章节时取前 60 章并注明）


def _report_digest(bd: dict) -> tuple[str, int, bool]:
    """聚合各章 briefs + 节奏点/钩子所在段的原文摘句（证据必须逐字命中，模型必须见过原文）。

    返回 (聚合文本, 章数)。每章：梗概 + 章末段 + 节奏标记所在段（每段截 120 字）。
    """
    done = [c for c in (bd.get("chapters") or []) if c.get("state") == "done" and c.get("analysis")]
    truncated = len(done) > REPORT_CAP
    parts: list[str] = []
    for c in done[:REPORT_CAP]:
        a = c["analysis"]
        paras = c.get("paras") or []
        n = len(paras)
        lines = [f"【第{c['no']}章 {c.get('title', '')}】梗概：{a.get('brief') or '（无）'}"]
        quotes: set[int] = set()
        if n:
            quotes.add(n)   # 章末段（钩子证据所在）
        for r in a.get("rhythm") or []:
            try:
                quotes.add(int(r.get("para")))
            except (TypeError, ValueError):
                continue
        for pno in sorted(x for x in quotes if 1 <= x <= n):
            text = paras[pno - 1].replace("\n", "")
            tag = "章末" if pno == n else f"段{pno}"
            lines.append(f"  [{tag}] {text[:120]}")
        if a.get("hook", {}).get("type") and a["hook"].get("type") != "无":
            lines.append(f"  章末钩子类型：{a['hook']['type']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts), len(done[:REPORT_CAP]), truncated


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def _join_normalized(paras: list[str]) -> tuple[str, list[int]]:
    """段落规范化拼接 + 每段在拼接文本中的起始偏移（用于证据定位段落区间）。"""
    joined = ""
    starts: list[int] = []
    for p in paras or []:
        starts.append(len(joined))
        joined += _norm_ws(p)
    return joined, starts


def locate_evidence(evidence: str, paras: list[str]) -> tuple[int, int] | None:
    """证据逐字定位：返回 (startPara, endPara)；未命中返回 None。

    逐字口径：两侧都去掉全部空白字符后要求子串命中（模型引用时偶尔换行不算失配）。
    """
    ev = _norm_ws(evidence)
    if len(ev) < MIN_EVIDENCE_CHARS:
        return None
    joined, starts = _join_normalized(paras)
    pos = joined.find(ev)
    if pos < 0:
        return None
    end_pos = pos + len(ev) - 1
    start = 1
    end = 1
    for i, off in enumerate(starts, 1):
        if off <= pos:
            start = i
        if off <= end_pos:
            end = i
    return (start, end)


def _coerce_report(raw, bd: dict) -> dict | None:
    """整形全书报告；可复用手法逐条做证据逐字校验，未命中的丢弃。"""
    if not isinstance(raw, dict):
        return None
    chapters = bd.get("chapters") or []
    paras_by_no = {c["no"]: (c.get("paras") or []) for c in chapters}
    editor_comment = _clamp_str(raw.get("editor_comment"), 1200)
    outline_restore = _clamp_str(raw.get("outline_restore"), 6000)
    arcs = []
    for a in (raw.get("character_arcs") if isinstance(raw.get("character_arcs"), list) else [])[:20]:
        if isinstance(a, dict) and _clamp_str(a.get("name"), 20):
            arcs.append({"name": _clamp_str(a.get("name"), 20),
                         "arc": _clamp_str(a.get("arc"), 500)})
    foreshadow = []
    for f in (raw.get("foreshadow") if isinstance(raw.get("foreshadow"), list) else [])[:30]:
        if isinstance(f, dict) and _clamp_str(f.get("item"), 120):
            foreshadow.append({"item": _clamp_str(f.get("item"), 120),
                               "plant": _clamp_str(f.get("plant"), 40),
                               "payoff": _clamp_str(f.get("payoff"), 40)})
    curve = []
    valid_nos = {c["no"] for c in chapters}
    for p in (raw.get("pacing_curve") if isinstance(raw.get("pacing_curve"), list) else []):
        if isinstance(p, dict):
            try:
                no = int(p.get("no"))
                score = max(1, min(5, int(p.get("score"))))
            except (TypeError, ValueError):
                continue
            if no not in valid_nos:
                continue   # 只画真实存在章号的节奏曲线
            curve.append({"no": no, "score": score})
    curve.sort(key=lambda x: x["no"])
    # 可复用手法：证据逐字命中原文，未命中丢弃该条
    techniques, dropped = [], []
    for t in (raw.get("techniques") if isinstance(raw.get("techniques"), list) else [])[:20]:
        if not isinstance(t, dict):
            continue
        name = _clamp_str(t.get("name"), 40)
        desc = _clamp_str(t.get("desc"), 300)
        evidence = _clamp_str(t.get("evidence"), 300)
        if not name or not desc:
            dropped.append({"name": name or f"手法#{len(techniques) + len(dropped) + 1}",
                            "reason": "手法名或描述为空"})
            continue
        chapter_no = None
        try:
            chapter_no = int(t.get("chapter"))
        except (TypeError, ValueError):
            chapter_no = None
        hit = None
        order = [chapter_no] if chapter_no in paras_by_no else []
        order += [no for no in paras_by_no if no != chapter_no]
        for no in order:
            hit = locate_evidence(evidence, paras_by_no[no])
            if hit:
                chapter_no = no
                break
        if not hit:
            dropped.append({"name": name, "reason": "证据未逐字命中原文" if evidence else "缺少证据引用"})
            continue
        techniques.append({"name": name, "desc": desc, "evidence": evidence,
                           "chapter": chapter_no, "startPara": hit[0], "endPara": hit[1]})
    if not editor_comment and not outline_restore and not techniques and not arcs:
        return None
    return {"editor_comment": editor_comment, "outline_restore": outline_restore,
            "character_arcs": arcs, "foreshadow": foreshadow,
            "pacing_curve": curve, "techniques": techniques,
            "dropped_techniques": dropped, "generated_at": _now()}


def build_report(bd_id: str, cfg: dict | None = None) -> dict:
    """全书报告（同步一次调用；JSON 解析失败自动重试一次）。"""
    if is_running(bd_id):
        return {"ok": False, "note": "章节拆解正在进行中，请等结束后再生成报告"}
    bd = load_project(bd_id)
    if not bd:
        return {"ok": False, "note": "拆书项目不存在"}
    done = [c for c in (bd.get("chapters") or []) if c.get("state") == "done" and c.get("analysis")]
    if not done:
        return {"ok": False, "note": "还没有已拆解完成的章节，先生成章节分析"}
    digest, used, truncated = _report_digest(bd)
    sys_p = (
        "你是资深网文编辑，基于下面的逐章拆解笔记写「全书拆书报告」。只输出严格 JSON，字段：\n"
        '{"editor_comment":"编辑点评（300字内：这本书强在哪、弱在哪、能不能签约/上架）",'
        '"outline_restore":"大纲还原（markdown 列表：全书故事结构、主线与关键节点）",'
        '"character_arcs":[{"name":"人物名","arc":"人物弧光一句话"}],'
        '"foreshadow":[{"item":"伏笔","plant":"第N章埋下","payoff":"第M章回收/未回收"}],'
        '"pacing_curve":[{"no":章号,"score":1-5的节奏强度}],'
        '"techniques":[{"name":"可复用手法名","desc":"怎么用、为什么有效（抽象描述，不复述原文）",'
        '"evidence":"逐字摘自上方原文摘句的证据（连续若干字，不许改写）","chapter":章号}]}'
        "\ntechniques 的 evidence 必须逐字摘自笔记中给出的原文摘句（[章末]/[段n] 后的文字），"
        "改写、拼凑或编造都会被程序丢弃。"
    )
    if truncated:
        sys_p += f"\n章节较多，本次聚合前 {used} 章的拆解笔记。"
    msgs = [{"role": "system", "content": sys_p},
            {"role": "user", "content": "【逐章拆解笔记（含原文摘句）】\n" + digest}]
    last_err = ""
    for attempt in range(2):
        try:
            raw = _call_json(msgs if attempt == 0 else msgs + [
                {"role": "user", "content": "上一次输出不是合法 JSON 或缺少字段，请严格按字段定义只输出一个 JSON 对象。"}
            ], cfg)
            report = _coerce_report(raw, bd)
        except Exception as e:
            report = None
            last_err = str(e)[:300]
        if report is not None:
            bd["report"] = report
            bd["characters"] = aggregate_characters(bd)
            _refresh_status(bd)
            save_project(bd)
            return {"ok": True, "report": report,
                    "chapters_used": used, "dropped": report.get("dropped_techniques") or []}
        if not last_err:
            last_err = "模型没有返回有效的报告 JSON"
    return {"ok": False, "note": f"报告生成失败（已重试一次）：{last_err}"}


# ---------------------------------------------------------------- 手法入账本（复用 lexicon_ledger）

def _book_project_dir(project_key: str) -> str | None:
    """校验真实作品 key 是否存在（与现有体系闭环时用）；不存在返回 None。"""
    if not str(project_key or "").strip():
        return None
    try:
        from . import writer
        pdir = writer._project_dir(str(project_key).strip())
    except Exception:
        return None
    return pdir if pdir and os.path.isdir(pdir) else None


def adopt_techniques(bd_id: str, indices, project_key: str = "") -> dict:
    """把选中的手法写入 lexicon_ledger。

    入账内容（纯净域）：手法名 + 抽象描述 + 证据位置（章/段区间）——
    **不带正文原文片段**；描述里检出原文逐字片段也会被拒。
    记账走 lexicon_ledger.record()（数据结构 {gram:{count,chapters,last}} 与现有账本一致）：
    project_key 指向真实作品时入作品账本，否则入拆书项目自己的账本目录。
    """
    bd = load_project(bd_id)
    if not bd:
        return {"ok": False, "note": "拆书项目不存在"}
    report = bd.get("report") or {}
    techs = report.get("techniques") or []
    if not techs:
        return {"ok": False, "note": "还没有全书报告或报告里没有可复用手法"}
    chapters = bd.get("chapters") or []
    paras_by_no = {c["no"]: (c.get("paras") or []) for c in chapters}
    adopted = bd.get("adopted_techniques") or []
    seen = {(a.get("name"), a.get("chapter")) for a in adopted}
    added, dropped = 0, []
    for idx in (indices or [])[:20]:
        try:
            t = techs[int(idx)]
        except (TypeError, ValueError, IndexError):
            continue
        if not isinstance(t, dict):
            continue
        name = _clamp_str(t.get("name"), 40)
        desc = _clamp_str(t.get("desc"), 300)
        chapter_no = t.get("chapter")
        if not name or not desc:
            dropped.append({"name": name or f"#{idx}", "reason": "手法名或描述为空"})
            continue
        # 证据复核：入账前再校验一次逐字命中
        hit = None
        try:
            hit = locate_evidence(t.get("evidence", ""), paras_by_no.get(int(chapter_no), []))
        except (TypeError, ValueError):
            hit = None
        if not hit:
            dropped.append({"name": name, "reason": "证据未逐字命中原文"})
            continue
        # 纯净域：手法描述不得携带原文片段
        ev_norm = _norm_ws(t.get("evidence", ""))
        if len(ev_norm) >= MIN_EVIDENCE_CHARS and ev_norm in _norm_ws(desc):
            dropped.append({"name": name, "reason": "手法描述携带原文片段，已按纯净域拒绝"})
            continue
        key = (name, chapter_no)
        if key in seen:
            dropped.append({"name": name, "reason": "同一章同名手法已入账"})
            continue
        where = f"第{chapter_no}章 段{hit[0]}-{hit[1]}" if hit[0] != hit[1] else f"第{chapter_no}章 段{hit[0]}"
        card = {"name": name, "desc": desc, "chapter": chapter_no,
                "startPara": hit[0], "endPara": hit[1], "where": where,
                "adopted_at": _now()}
        adopted.append(card)
        seen.add(key)
        # 复用 lexicon_ledger 的记账 API 与数据结构（手法词汇进账本热度体系）
        target_dir = _book_project_dir(project_key) or _ledger_dir(bd_id)
        try:
            lexicon_ledger.record(target_dir, int(chapter_no) if str(chapter_no).isdigit() else 0,
                                  f"{name}。{desc}")
        except Exception:
            pass   # 账本写入失败不影响手法卡本身
        added += 1
    bd["adopted_techniques"] = adopted[-200:]
    save_project(bd)
    return {"ok": True, "adopted": added, "dropped": dropped,
            "total": len(bd["adopted_techniques"])}
