"""v2.2.1 文稿副驾引擎：剧本 / 诗词 / 散文 通用的「对话改稿 + 留档」。

一个作品里能被改的文档抽象成 doc：剧本是**场次**，诗词/散文是**定稿/初稿**；
doc 再按空行切成 block（段）。对话里说「第3场第2段」「定稿第2段」就能定位，
并且**只命中目标段**：先出 patch（old / new / base_hash），用户点「应用」才落盘。
落盘的安全链固定为 hash 校验 → 版本快照 → 原子写 → 范围自证：
写完后除目标块外每块 hash 必须与写前一致，有一处不等就恢复旧全文并报越界。

对话与改稿都留档在作品目录的 ``.assist/`` 下：``chat.jsonl``（对话历史）、
``edits.jsonl``（改稿记录），随作品走，换机器换浏览器都还在。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re

from bot.safeio import atomic_write_text

from . import llm, poetry_writer, prose_writer, script_writer

MEDIA = ("script", "poetry", "prose")

# 版本快照：剧本沿用 04-正文/.versions/第NNN场-时间.md，其余文体放作品根 .versions/
VERSIONS_DIR = ".versions"
ASSIST_DIR = ".assist"
KEEP_VERSIONS = 30

# 中文数字
_NUM = r"[0-9零〇一二三四五六七八九十百两]+"
_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "百": 100,
}

# 场/段定位（组合写法必须先于单写法，避免「第3场第2段」把 3 误当段号）
_RE_SCENE_BLOCK_SHORT = re.compile(rf"场\s*({_NUM})\s*段\s*({_NUM})")
_RE_SCENE_BLOCK_LONG = re.compile(rf"第\s*({_NUM})\s*[场幕]\s*(?:第\s*)?({_NUM})\s*段")
_RE_SCENE_A = re.compile(rf"第\s*({_NUM})\s*[场幕]")
_RE_SCENE_B = re.compile(rf"场\s*({_NUM})")
_RE_BLOCK_A = re.compile(rf"第\s*({_NUM})\s*段")
_RE_BLOCK_B = re.compile(rf"段\s*({_NUM})")

# 诗词/散文按文档名定位：定稿 / 草稿 / 初稿
_RE_DOC_FINAL = re.compile(r"定稿|成稿|终稿")
_RE_DOC_DRAFT = re.compile(r"草稿|初稿|底稿")

# 意图词表
_EDIT_WORDS = (
    "改", "换成", "换掉", "重写", "重做", "删掉", "删除", "删去", "去掉",
    "加一句", "加一段", "补一句", "插入", "调成", "调整", "收紧", "放缓",
    "润色", "口语", "更紧张", "更冷", "更狠", "压缩", "精简", "扩写",
    "拆成", "合并", "改成", "更有", "再写", "润",
)
_DISCUSS_WORDS = (
    "为什么", "为啥", "分析", "建议", "怎么样", "如何", "评价", "点评",
    "优缺点", "好不好", "行不行", "怎么看", "聊聊", "讨论",
)

# 场景头：替换段不允许以它开头（那等于换场）
_HEADING_RE = re.compile(r"^(?:内景|外景|INT|EXT)(?:[.\s．:：]|$)", re.I)
_ACK_RE = re.compile(r"^(好的|好|以下是|下面是|这是|明白|收到)[^\n]{0,40}[：:\n]")

# 人物小传里的姓名行
_NAME_RE = re.compile(r"姓名[^\n：:]{0,20}[：:]\s*([^\s，,、。；;：:（(【\[]+)")

# 文体名（给模型看的自称）
_MEDIUM_NAME = {"script": "剧本", "poetry": "诗词", "prose": "散文"}

# 诗词/散文可改文档：id → (标题, 文件名)
_POETRY_DOCS = (("final", "定稿", poetry_writer.F_FINAL), ("draft", "草稿", poetry_writer.F_DRAFT))
_PROSE_DOCS = (("final", "定稿", prose_writer.F_FINAL), ("draft", "初稿", prose_writer.F_DRAFT))


# ---------------------------------------------------------------- 基础工具

def _cn_to_int(s: str) -> int | None:
    """中文/阿拉伯数字转 int：十=10、十二=12、二十=20、二十三=23、一百零二=102。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _DIGITS:
        return _DIGITS[s]
    total = 0
    rest = s
    if "百" in rest:
        left, rest = rest.split("百", 1)
        if left and left not in _DIGITS:
            return None
        total = (_DIGITS[left] if left else 1) * 100
    if "十" in rest:
        left, rest = rest.split("十", 1)
        if left and left not in _DIGITS:
            return None
        total += (_DIGITS[left] if left else 1) * 10
    rest = rest.replace("零", "").replace("〇", "")
    if rest:
        if rest not in _DIGITS:
            return None
        total += _DIGITS[rest]
    return total or None


def split_blocks(text: str) -> list[str]:
    """按空行切段：去首尾空白、丢弃全空块、保留块内单换行。"""
    if not text:
        return []
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for part in re.split(r"\n\s*\n", norm):
        s = part.strip()
        if s:
            out.append(s)
    return out


def block_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


def _join_blocks(blocks: list[str]) -> str:
    return "\n\n".join(blocks)


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _word_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 文档抽象

def _proj(key: str, medium: str):
    if medium == "script":
        return script_writer.get_project(key)
    if medium == "poetry":
        return poetry_writer.get_project(key)
    if medium == "prose":
        return prose_writer.get_project(key)
    return None


def _script_docs(proj) -> list[dict]:
    plan = proj.plan() or {}
    out = []
    for sc in plan.get("scenes") or []:
        no = _as_int(sc.get("no")) or 0
        if not no:
            continue
        fn = str(sc.get("fileName") or f"第{no:03d}场.md").strip()
        fn = os.path.basename(fn) or f"第{no:03d}场.md"
        head = (sc.get("heading") or "").strip() or script_writer.scene_heading(sc)
        title = f"第 {no} 场" + (f" · {head}" if head else "")
        out.append({"id": str(no), "kind": "scene", "no": no, "title": title,
                    "rel": os.path.join(script_writer.F_BODY, fn),
                    "words": sc.get("wordCount") or 0})
    return out


def _flat_docs(spec) -> list[dict]:
    return [{"id": did, "kind": "doc", "no": None, "title": title, "rel": fn, "words": 0}
            for did, title, fn in spec]


def docs_for(key: str, medium: str) -> list[dict]:
    """作品里所有可改文档。"""
    proj = _proj(key, medium)
    if not proj:
        return []
    if medium == "script":
        return _script_docs(proj)
    if medium == "poetry":
        return _flat_docs(_POETRY_DOCS)
    if medium == "prose":
        return _flat_docs(_PROSE_DOCS)
    return []


def resolve_doc(key: str, medium: str, doc_id) -> dict | None:
    """按 id 取文档描述，附带绝对路径。"""
    if doc_id is None or doc_id == "":
        return None
    want = str(doc_id).strip()
    for d in docs_for(key, medium):
        if d["id"] == want:
            proj = _proj(key, medium)
            if not proj:
                return None
            return {**d, "path": os.path.join(proj.path, d["rel"])}
    return None


def _default_doc(medium: str) -> str:
    return "final"


# ---------------------------------------------------------------- 定位

def parse_target(text: str, medium: str = "script") -> dict:
    """从自然语言里抽目标文档与段号；没有的维度返回 None。"""
    t = text or ""
    if medium == "script":
        for rx in (_RE_SCENE_BLOCK_SHORT, _RE_SCENE_BLOCK_LONG):
            m = rx.search(t)
            if m:
                return {"doc": _cn_to_int(m.group(1)), "block": _cn_to_int(m.group(2))}
        doc = None
        m = _RE_SCENE_A.search(t) or _RE_SCENE_B.search(t)
        if m:
            doc = _cn_to_int(m.group(1))
        block = None
        m = _RE_BLOCK_A.search(t) or _RE_BLOCK_B.search(t)
        if m:
            block = _cn_to_int(m.group(1))
        return {"doc": doc, "block": block}
    # 诗词/散文：段号 + 文档名（定稿/草稿）
    doc = None
    if _RE_DOC_FINAL.search(t):
        doc = "final"
    elif _RE_DOC_DRAFT.search(t):
        doc = "draft"
    block = None
    m = _RE_BLOCK_A.search(t) or _RE_BLOCK_B.search(t)
    if m:
        block = _cn_to_int(m.group(1))
    return {"doc": doc, "block": block}


def _doc_label(medium: str, doc: dict) -> str:
    return doc.get("title") or (f"第 {doc.get('id')} 场" if doc.get("kind") == "scene" else doc.get("id"))


# ---------------------------------------------------------------- 上下文

def _setting_lines(plan: dict) -> list[str]:
    st = (plan.get("meta") or {}).get("setting") or {}
    if not isinstance(st, dict):
        return []
    out = []
    era = st.get("era_label") or st.get("era")
    if era:
        out.append(f"时代：{era}")
    if st.get("world"):
        out.append(f"世界：{st['world']}")
    return out


def _character_brief(proj) -> str:
    text = _read(os.path.join(proj.path, script_writer.F_CHARS))
    names = []
    for m in _NAME_RE.finditer(text):
        n = (m.group(1) or "").strip()
        if n and n not in names:
            names.append(n)
    return "、".join(names[:12]) if names else text[:1500]


def _first_sentence(block: str, limit: int = 30) -> str:
    line = block.strip().split("\n", 1)[0].strip()
    seg = re.split(r"[。！？!?]", line, maxsplit=1)[0].strip() or line
    return seg[:limit]


def _work_name(proj, plan: dict, medium: str) -> str:
    for field in ("scriptName", "poemName", "proseName", "novelName"):
        if plan.get(field):
            return str(plan[field])
    return proj.name


def _history_msgs(history: list | None) -> list[dict]:
    out = []
    for h in (history or [])[-6:]:
        if not isinstance(h, dict):
            continue
        role, content = h.get("role"), h.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        if content.strip():
            out.append({"role": role, "content": content.strip()[:2000]})
    return out


def _clean_block(raw: str) -> str:
    """去掉代码围栏与「好的/以下是」开场，保留段内换行。"""
    t = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[^\n]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    t = t.strip()
    t = _ACK_RE.sub("", t, count=1).strip()
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return t


def _looks_like_heading(text: str) -> bool:
    return bool(_HEADING_RE.match(text.strip()))


# ---------------------------------------------------------------- 生成

_GEN_SYS = {
    "script": [
        "你是一位资深中文剧本编剧，本次任务只做「单段替换」。",
        "你只输出替换后的这一段正文：不要场景头（内景/外景/INT/EXT），不要场次编号，"
        "不要解释，不要代码围栏，不要「好的」「以下是」这类开场白。",
        "只改这一段，不要顺手改其他段落；保持与上下文一致的语气、人称、格式。",
    ],
    "poetry": [
        "你是一位资深中文诗人（诗词作者），本次任务只做「单段替换」。",
        "你只输出替换后的这一段正文：不要标题、不要编号、不要解释、不要代码围栏，"
        "不要「好的」「以下是」这类开场白。",
        "只改这一段，不要动其它段落；保持整首的意象系统、语气与格律一致，"
        "不能破坏这一段的韵脚与句式。",
    ],
    "prose": [
        "你是一位资深中文散文作者，本次任务只做「单段替换」。",
        "你只输出替换后的这一段正文：不要小标题、不要解释、不要代码围栏，"
        "不要「好的」「以下是」这类开场白。",
        "只改这一段，不要动其它段落；保持全文的语感、节奏与抒情节制一致。",
    ],
}


def _gen_block(medium: str, doc: dict, block: int, total: int, old: str, prev: str, nxt: str,
               message: str, plan: dict, proj, cfg: dict) -> str:
    sys_lines = list(_GEN_SYS.get(medium) or _GEN_SYS["script"])
    setting = _setting_lines(plan)
    if setting:
        sys_lines.append("【世界观硬约束】" + "；".join(setting))
    if medium == "script":
        sys_lines.append("【主要人物】" + _character_brief(proj))
    user_lines = []
    if prev:
        user_lines.append(f"【该段前一段（仅供衔接，不要改写）】\n{prev}")
    user_lines.append(f"【目标段原文 · 第 {block} 段 / 共 {total} 段】\n{old}")
    if nxt:
        user_lines.append(f"【该段后一段（仅供衔接，不要改写）】\n{nxt}")
    user_lines.append(f"【用户指令】\n{message}")
    user_lines.append(f"请直接输出替换后的第 {block} 段。")
    msgs = [{"role": "system", "content": "\n".join(sys_lines)},
            {"role": "user", "content": "\n\n".join(user_lines)}]
    return llm.chat(msgs, cfg=cfg, stage="writing", max_tokens=2500)


def _discuss(proj, key: str, medium: str, plan: dict, message: str, doc: dict | None,
             history: list | None, cfg: dict) -> str:
    name = _work_name(proj, plan, medium)
    mname = _MEDIUM_NAME.get(medium, "作品")
    cur = ""
    if doc:
        cur = _read(doc.get("path") or "")
    if not cur.strip() and medium == "script":
        for sc in plan.get("scenes") or []:
            no = _as_int(sc.get("no"))
            if no:
                cur = script_writer.read_scene(key, no)
                if cur.strip():
                    break
    sys_lines = [
        f"你是《{name}》的{mname}创作助理，熟悉这部作品的设定、人物与结构。",
        "你现在只做讨论与分析：你没有修改任何文件，只有用户点应用才会改。",
        "回答要具体、可执行：指到具体段落/词句，给 1-3 个可落地的改法，不要空话。",
    ]
    setting = _setting_lines(plan)
    if setting:
        sys_lines.append("【世界观】" + "；".join(setting))
    if medium == "script":
        bible = _read(os.path.join(proj.path, script_writer.F_BIBLE))
        if bible.strip():
            sys_lines.append("【创作圣经（节选）】\n" + bible[:2500])
    if cur.strip():
        head = f"【{doc.get('title')}（节选）】" if doc else "【当前内容（节选）】"
        sys_lines.append(head + "\n" + cur[:2500])
    msgs = [{"role": "system", "content": "\n\n".join(sys_lines)}]
    msgs.extend(_history_msgs(history))
    msgs.append({"role": "user", "content": message})
    return llm.chat(msgs, cfg=cfg, stage="review", max_tokens=2000)


# ---------------------------------------------------------------- 结果封装

def _result(ok: bool, reply: str, kind: str, doc, block: int | None,
            patch: dict | None = None, doc_title: str = "") -> dict:
    return {"ok": ok, "reply": reply,
            "intent": {"kind": kind, "doc": doc, "block": block, "doc_title": doc_title},
            "patch": patch}


# ---------------------------------------------------------------- 留档

def _assist_dir(proj) -> str:
    d = os.path.join(proj.path, ASSIST_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def _append_jsonl(path: str, entry: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def log_chat(key: str, medium: str, role: str, content: str, **extra) -> None:
    """对话留档：用户与 Agent 的每一轮都写进作品目录。"""
    proj = _proj(key, medium)
    if not proj:
        return
    entry = {"ts": _now(), "role": role, "content": str(content or "")[:4000]}
    entry.update({k: v for k, v in extra.items() if v is not None})
    _append_jsonl(os.path.join(_assist_dir(proj), "chat.jsonl"), entry)


def log_edit(key: str, medium: str, doc: str, doc_title: str, block: int,
             old: str, new: str, version: str, words: int = 0) -> None:
    """改稿留档：每落一次盘就记一条，可回看可追溯。"""
    proj = _proj(key, medium)
    if not proj:
        return
    entry = {"ts": _now(), "doc": doc, "doc_title": doc_title, "block": block,
             "old": old, "new": new, "version": version, "words": words}
    _append_jsonl(os.path.join(_assist_dir(proj), "edits.jsonl"), entry)


def _read_jsonl(path: str, limit: int = 200) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out[-limit:]


def history(key: str, medium: str) -> dict:
    """对话历史 + 改稿记录。"""
    proj = _proj(key, medium)
    if not proj:
        return {"ok": False, "error": "项目不存在"}
    d = os.path.join(proj.path, ASSIST_DIR)
    messages = _read_jsonl(os.path.join(d, "chat.jsonl"))
    edits = _read_jsonl(os.path.join(d, "edits.jsonl"))
    edits.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return {"ok": True, "messages": messages, "edits": edits}


# ---------------------------------------------------------------- assist

def _decide_kind(message: str, focus: dict) -> str:
    if any(w in message for w in _EDIT_WORDS):
        return "edit"
    if any(w in message for w in _DISCUSS_WORDS):
        return "discussion"
    if focus and ("这段" in message or "这一段" in message):
        return "edit"
    return "discussion"


def _clarify_doc(doc: dict, blocks: list[str], block: int | None) -> dict:
    title = doc.get("title") or ""
    if not blocks:
        return _result(True, f"{title} 还没有内容，先写出来再来改。", "clarify",
                       doc.get("id"), block, None, title)
    if block is None:
        head = "；".join(f"第{i}段「{_first_sentence(b)}」" for i, b in enumerate(blocks[:3], 1))
        return _result(True, f"{title} 共 {len(blocks)} 段：{head}。请指到具体某一段，"
                             f"比如「第1段」——我只改那一段，其他内容逐字不动。",
                       "clarify", doc.get("id"), None, None, title)
    return _result(True, f"{title} 只有 {len(blocks)} 段（第 {block} 段不存在）。"
                         f"请改成「第1段」到「第{len(blocks)}段」之间的段号。",
                   "clarify", doc.get("id"), block, None, title)


def assist(key: str, medium: str, message: str, focus: dict | None = None,
           history_msgs: list | None = None, cfg: dict | None = None) -> dict:
    """副驾入口：定位目标 → 讨论 / 追问 / 生成段级 patch（不写盘）。"""
    medium = medium if medium in MEDIA else "script"
    proj = _proj(key, medium)
    if not proj:
        return _result(False, f"找不到项目：{key}", "discussion", None, None)
    plan = proj.plan() or {}
    use_cfg = cfg or plan.get("config") or {}
    focus = focus if isinstance(focus, dict) else {}
    message = (message or "").strip()
    if not message:
        return _result(True, "想改哪一段？点一下正文里的段落，或直接说「第2段」。",
                       "discussion", focus.get("doc"), _as_int(focus.get("block")))

    parsed = parse_target(message, medium)
    msg_doc, msg_block = parsed["doc"], parsed["block"]
    fdoc = focus.get("doc")
    fdoc = None if fdoc in (None, "") else str(fdoc)
    fblock = _as_int(focus.get("block"))

    if msg_doc is None and msg_block is None:
        doc_id, block = fdoc, fblock
    elif msg_doc is None:
        doc_id, block = fdoc, msg_block
    elif msg_block is None:
        doc_id = str(msg_doc)
        block = fblock if (fdoc is not None and fdoc == doc_id) else None
    else:
        doc_id, block = str(msg_doc), msg_block

    kind = _decide_kind(message, focus)

    if kind == "discussion":
        doc = resolve_doc(key, medium, doc_id) if doc_id else None
        try:
            reply = _discuss(proj, key, medium, plan, message, doc, history_msgs, use_cfg)
        except Exception as e:
            return _result(False, f"模型调用失败：{e}", "discussion", doc_id, block)
        if not (reply or "").strip():
            return _result(False, "模型调用失败：模型返回为空", "discussion", doc_id, block)
        return _result(True, reply.strip(), "discussion", doc_id, block,
                       None, doc.get("title") if doc else "")

    # edit：目标不全先追问，不整篇重写
    if not doc_id:
        doc_id = _default_doc(medium) if medium != "script" else None
    if not doc_id:
        if medium == "script":
            return _result(True, "请说明改哪一场第几段，比如「第3场第2段」——我只改那一段，"
                                 "其余内容逐字不变。", "clarify", None, block)
        return _result(True, "请说明改哪一段，比如「第2段」——我只改那一段，其余内容逐字不变。",
                       "clarify", None, block)
    doc = resolve_doc(key, medium, doc_id)
    if not doc:
        return _result(True, f"没找到这个文档（{doc_id}），请重新指一下。", "clarify", doc_id, block)
    title = _doc_label(medium, doc)
    blocks = split_blocks(_read(doc["path"]))
    if block is None or block < 1 or block > len(blocks):
        return _clarify_doc(doc, blocks, block)

    old = blocks[block - 1]
    prev = blocks[block - 2] if block >= 2 else ""
    nxt = blocks[block] if block < len(blocks) else ""
    try:
        raw = _gen_block(medium, doc, block, len(blocks), old, prev, nxt, message, plan, proj, use_cfg)
    except Exception as e:
        return _result(False, f"模型调用失败：{e}", "edit", doc_id, block, None, title)
    new = _clean_block(raw)
    if not new:
        return _result(False, "模型没有返回可用内容，这一版没有生成 patch，请重试。",
                       "edit", doc_id, block, None, title)
    if medium == "script" and _looks_like_heading(new):
        return _result(False, "模型返回的内容以场景头（内景/外景/INT/EXT）开头。这里只改段、"
                              "不换场，已拒绝这一版，请重试。", "edit", doc_id, block, None, title)
    if len(new) > len(old) * 3 + 200:
        return _result(False, f"改写结果过长（{len(old)} → {len(new)} 字），超出单段改写范围，"
                              f"已拒绝这一版，请把指令说得更具体。", "edit", doc_id, block, None, title)

    patch = {"doc": doc_id, "block": block, "base_hash": block_hash(old),
             "old": old, "new": new, "doc_title": title}
    reply = (f"{title} 第 {block} 段已生成改写稿（{len(old)} → {len(new)} 字）。"
             f"原文开头「{_first_sentence(old)}」，新文开头「{_first_sentence(new)}」。"
             f"确认后点「应用」才会写入文件。")
    return _result(True, reply, "edit", doc_id, block, patch, title)


# ---------------------------------------------------------------- 版本快照

def _versions_dir(proj, medium: str) -> str:
    if medium == "script":
        base = os.path.join(proj.path, script_writer.F_BODY)
    else:
        base = proj.path
    d = os.path.join(base, VERSIONS_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def _version_name(medium: str, doc_id: str, stamp: datetime.datetime) -> str:
    if medium == "script":
        return f"第{int(doc_id):03d}场-{stamp.strftime('%Y%m%d-%H%M%S')}.md"
    return f"{doc_id}-{stamp.strftime('%Y%m%d-%H%M%S')}.md"


def _version_matches(medium: str, name: str) -> str | None:
    """版本文件名 → 文档 id；不合法返回 None。"""
    if medium == "script":
        m = re.match(r"^第(\d{3})场-\d{8}-\d{6}\.md$", name)
        return str(int(m.group(1))) if m else None
    m = re.match(r"^([A-Za-z0-9_-]+)-\d{8}-\d{6}\.md$", name)
    return m.group(1) if m else None


def _version_time(name: str) -> datetime.datetime | None:
    """版本文件名里的时间戳（日期 8 位 + 破折号 + 时刻 6 位）。"""
    m = re.search(r"(\d{8})-(\d{6})\.md$", name)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(f"{m.group(1)}-{m.group(2)}", "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def _snapshot(proj, medium: str, doc_id: str, text: str) -> str:
    d = _versions_dir(proj, medium)
    stamp = datetime.datetime.now()
    for _ in range(120):
        name = _version_name(medium, doc_id, stamp)
        path = os.path.join(d, name)
        if not os.path.exists(path):
            atomic_write_text(path, text)
            _prune(d, medium, doc_id)
            return name
        stamp += datetime.timedelta(seconds=1)
    raise RuntimeError("无法分配版本快照文件名")


def _prune(d: str, medium: str, doc_id: str) -> None:
    try:
        names = os.listdir(d)
    except OSError:
        return
    mine = [n for n in names if _version_matches(medium, n) == str(doc_id)]
    mine.sort()
    if len(mine) <= KEEP_VERSIONS:
        return
    for name in mine[:-KEEP_VERSIONS]:
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass


def _update_word_count(proj, medium: str, doc_id: str, words: int) -> None:
    plan = proj.plan() or {}
    if medium == "script":
        for sc in plan.get("scenes") or []:
            if str(_as_int(sc.get("no"))) == str(doc_id):
                sc["wordCount"] = words
                break
    else:
        meta = plan.setdefault("meta", {})
        dw = meta.setdefault("docWords", {})
        dw[str(doc_id)] = words
        # 只有定稿改动了，全文字数才跟着变；改草稿不动成稿的字数
        if str(doc_id) == "final":
            meta["words"] = words
    plan["updatedAt"] = _now()
    proj.save_plan(plan)


# ---------------------------------------------------------------- 落盘 / 回退

def apply_edit(key: str, medium: str, doc_id, block, base_hash: str, new_text: str) -> dict:
    """把 patch 写回目标文档：hash 校验 → 快照 → 原子写 → 范围自证 → 更新计划。"""
    proj = _proj(key, medium)
    if not proj:
        return {"ok": False, "error": "项目不存在"}
    doc = resolve_doc(key, medium, doc_id)
    if not doc:
        return {"ok": False, "error": "文档不存在"}
    block = _as_int(block)
    if not block:
        return {"ok": False, "error": "段号不合法"}
    old_text = _read(doc["path"])
    blocks = split_blocks(old_text)
    if block < 1 or block > len(blocks):
        return {"ok": False, "error": f"{doc.get('title')} 只有 {len(blocks)} 段，找不到第 {block} 段"}
    if block_hash(blocks[block - 1]) != (base_hash or ""):
        return {"ok": False, "code": "stale", "error": "这段内容已被修改，请刷新后重试"}
    new_clean = (new_text or "").strip()
    if not new_clean:
        return {"ok": False, "error": "替换内容为空"}

    idx = block - 1
    new_blocks = list(blocks)
    new_blocks[idx] = new_clean
    new_full = _join_blocks(new_blocks)
    version = _snapshot(proj, medium, doc["id"], old_text)
    atomic_write_text(doc["path"], new_full.rstrip("\n") + "\n")

    # 范围自证：除目标索引外，每块 hash 必须与写前一致，否则恢复旧全文
    after = split_blocks(_read(doc["path"]))
    in_scope = len(after) == len(blocks) and all(
        i == idx or block_hash(after[i]) == block_hash(blocks[i]) for i in range(len(blocks)))
    if not in_scope:
        atomic_write_text(doc["path"], old_text)
        return {"ok": False, "error": "改写范围越界，已回退"}

    words = _word_count(new_full)
    _update_word_count(proj, medium, doc["id"], words)
    log_edit(key, medium, doc["id"], doc.get("title") or "", block,
             blocks[idx], new_clean, version, words)
    return {"ok": True, "text": new_full, "doc": doc["id"], "block": block,
            "wordCount": words, "version": version}


def list_versions(key: str, medium: str, doc_id) -> dict:
    proj = _proj(key, medium)
    if not proj:
        return {"ok": False, "error": "项目不存在"}
    doc = resolve_doc(key, medium, doc_id)
    if not doc:
        return {"ok": False, "error": "文档不存在"}
    d = _versions_dir(proj, medium)
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    items = []
    for name in names:
        if _version_matches(medium, name) != str(doc["id"]):
            continue
        at = _version_time(name)
        if at is None:
            continue
        words = _word_count(_read(os.path.join(d, name)))
        items.append({"version": name, "at": at.strftime("%Y-%m-%d %H:%M:%S"),
                      "words": words, "doc": doc["id"]})
    items.sort(key=lambda x: x["version"], reverse=True)
    return {"ok": True, "items": items}


def restore_version(key: str, medium: str, doc_id, version: str) -> dict:
    """回退到某次快照：只认 basename + 冻结命名，先给当前内容再快照一次。"""
    proj = _proj(key, medium)
    if not proj:
        return {"ok": False, "error": "项目不存在"}
    doc = resolve_doc(key, medium, doc_id)
    if not doc:
        return {"ok": False, "error": "文档不存在"}
    raw = str(version or "").strip()
    name = os.path.basename(raw)
    if not raw or name != raw or _version_matches(medium, name) != str(doc["id"]):
        return {"ok": False, "error": "版本文件名不合法"}
    d = _versions_dir(proj, medium)
    src = os.path.join(d, name)
    if not os.path.isfile(src):
        return {"ok": False, "error": "版本不存在"}
    text = _read(src)
    _snapshot(proj, medium, doc["id"], _read(doc["path"]))
    atomic_write_text(doc["path"], text)
    words = _word_count(text)
    _update_word_count(proj, medium, doc["id"], words)
    log_edit(key, medium, doc["id"], doc.get("title") or "", 0, "", text, name, words)
    return {"ok": True, "text": text, "wordCount": words, "version": name, "doc": doc["id"]}
