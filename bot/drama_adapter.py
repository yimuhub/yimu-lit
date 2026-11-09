"""小说 → AI 短剧全书改编引擎（v4.33.0）。

旧实现（app.py 内联）有四个结构性错误，用户实拍一本 100 章 / 25.8 万字的小说
改编出来只有 12 集、其中第 11 集 42 字：

1. ``chs[:12]`` 硬截断 —— 全书 100 章只用了前 12 章，88% 的原著凭空消失；
2. 1 章 = 1 集 = 1「场」—— 计划里 12 条 scenes，但每集正文内部本来就有多场；
3. 只把 ``src_text[:9000]`` 塞给模型 —— 项目里现成的 ``21-章摘要.jsonl``
   （逐章摘要 + 节拍）、``09-故事圣经.md``、``00-人物档案.md``、``20-卷摘要.md``
   全都没用上，每集彼此独立裸调，人物/设定/前情不继承，逻辑必然断裂；
4. 只判 ``body.strip()`` 非空 —— 模型输出中途截断的 42 字残片照样写盘标
   ``completed``，于是「已完成」的集其实是半句话。

本模块把改编拆成可独立验证的三层：

* **素材层**：从原小说目录聚合全书摘要/圣经/人物/卷弧（不烧 token 读 25 万字）；
* **蓝图层**：确定性把全书章节切成集（1~6 章/集或指定目标集数），再让模型
  提炼一次「创作圣经」，逐集写作时全程携带，保证世界观与人物一致；
* **写作层**：每集按预算分配原著全文（不再一刀切 9000 字，且保留章末钩子），
  带前情滚动摘要，输出过质量门禁才落盘；不达标自动重试，仍失败标待重跑，
  绝不把残片记成「已完成」。

本模块只依赖 bot 内的纯 Python 设施，不 import app / FastAPI，便于单测。
"""
from __future__ import annotations

import json
import math
import os
import re

from . import delivery_gate

# 蓝图版本：旧剧本没有这个字段，_resume_novel_to_drama 据此判定需要升级
ADAPTER_VERSION = 2

DEFAULT_CHAPTERS_PER_EPISODE = 2
MAX_EPISODES = 120            # 防失控上限；100 章 / 1 章每集 = 100 集，仍在范围内
MIN_EPISODE_CHARS = 700       # 一集 3-5 分钟竖屏短剧，正文不该低于这个量级
MIN_SCENE_HEADINGS = 2        # 一集至少两个场景标题
MIN_DIALOGUE_LINES = 3        # 一集至少三句台词
MAX_ATTEMPTS = 3              # 单集质量门禁最多重试次数
SOURCE_BUDGET = 24000         # 每集喂给模型的原著正文字数预算
BIBLE_BUDGET = 6000           # 创作圣经注入预算
PREV_EPISODE_CHARS = 700      # 上一集结尾摘要长度

# 场景标题：兼容「**场1：外 广场 日**」「第 1 场」「[内 长老殿 夜]」「INT. ...」
_SCENE_HEAD_RE = re.compile(
    r"(?m)^\s*(?:"
    r"\*{0,2}\s*(?:场\s*\d+|第\s*\d+\s*场|场景\s*\d+)\s*[：:·\-—]?"
    r"|\[?\s*(?:内|外|内景|外景|INT|EXT)\b"
    r")"
)
# 台词：「角色名：台词」，角色名不含标点且不宜过长
_LINE_RE = re.compile(r"(?m)^\s*[^\s：:\[\]（）()]{1,14}\s*[：:]\s*\S")
# 结尾正常收束的符号；以汉字/字母/逗号结尾多半是 max_tokens 中途截断
_TAIL_OK = set("。！？!?…．”’\"')）】》」』>~-—*`|")
_TAIL_TRUNCATED = set("，,、；;：:")


def _read(path: str, limit: int = 0) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return ""
    return text[:limit] if limit > 0 else text


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_int(value, lo: int, hi: int, default: int) -> int:
    """把外部传入的密度/字数参数夹到安全区间（缺省值也要合法）。"""
    n = _int(value, default)
    return max(lo, min(hi, n or default))


# 供 app.py 复用的只读工具（公开别名）
read_text = _read


# ---------------------------------------------------------------- 素材层

def load_chapter_summaries(pdir: str) -> dict[int, dict]:
    """读 ``21-章摘要.jsonl``；缺失时返回空字典，由调用方退化为章节标题。"""
    out: dict[int, dict] = {}
    for fname in ("21-章摘要.jsonl", "11-章节摘要.md"):
        fp = os.path.join(pdir, fname)
        if not os.path.exists(fp):
            continue
        if fname.endswith(".jsonl"):
            for line in _read(fp).splitlines():
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                n = _int(row.get("n"))
                if n > 0:
                    out[n] = row
        else:
            out[-1] = {"raw": _read(fp, 12000)}
    return out


def collect_material(proj) -> dict:
    """聚合全书级素材。这些都是小说流水线已经落盘的产物，不额外烧 token。"""
    pdir = getattr(proj, "path", "") or ""
    summaries = load_chapter_summaries(pdir)
    bible = "\n\n".join(filter(None, [
        _read(os.path.join(pdir, "09-故事圣经.md"), 4000).strip(),
        _read(os.path.join(pdir, "00-人物档案.md"), 4000).strip(),
    ]))
    season = _read(os.path.join(pdir, "20-卷摘要.md"), 6000).strip()
    outline = _read(os.path.join(pdir, "01-大纲.md"), 3000).strip()
    return {
        "summaries": summaries,
        "bible": bible[:BIBLE_BUDGET],
        "season": season,
        "outline": outline,
        # 全书摘要压缩成一条「总览」，用于一次性生成创作圣经
        "digest": build_book_digest(summaries, budget=9000),
    }


def build_book_digest(summaries: dict[int, dict], budget: int = 9000) -> str:
    """把逐章摘要拼成全书总览（均匀抽样，保证首尾都在，不因超长丢掉结局）。"""
    rows = [(n, s) for n, s in sorted(summaries.items()) if n > 0 and isinstance(s, dict)]
    if not rows:
        return ""
    parts = [f"第{n}章《{s.get('title') or ''}》：{(s.get('summary') or '').strip()}"
             for n, s in rows]
    joined = "\n".join(parts)
    if len(joined) <= budget:
        return joined
    # 超预算：按整书均匀取样，但强制保留前 3 章与最后 3 章
    head = parts[:3]
    tail = parts[-3:]
    keep = set(range(3)) | set(range(len(parts) - 3, len(parts)))
    rest_budget = max(0, budget - sum(len(x) for x in head + tail) - 200)
    middle = [p for i, p in enumerate(parts) if i not in keep]
    step = max(1, math.ceil(len(middle) / max(1, rest_budget // 200)))
    picked = middle[::step]
    out = head + picked + tail
    text = "\n".join(out)
    return text[:budget] if len(text) > budget else text


def chapter_text(proj, ch: dict) -> str:
    """读单章正文；文件名只允许纯文件名，挡住计划被篡改后的越界。"""
    from . import writer as novel_writer

    name = os.path.basename(str(ch.get("filePath") or ""))
    if not name:
        return ""
    fp = os.path.join(getattr(proj, "path", ""), name)
    if not os.path.exists(fp):
        return ""
    try:
        return novel_writer._clean_chapter_text(_read(fp), str(ch.get("title") or ""))
    except Exception:
        return _read(fp)


def episode_source_text(proj, chapter_rows: list[dict], budget: int = SOURCE_BUDGET) -> str:
    """按预算分配本集涉及的**全部**章节正文。

    旧实现 ``src_text[:9000]`` 只取单章开头；多章合成一集时会把后面几章整章
    丢掉，单章超长时又把结尾（往往正是集尾钩子）砍掉。这里改成：总长不超预算
    就全文给；超了就每章均分，并各自保留开头与结尾。
    """
    texts = []
    for ch in chapter_rows:
        body = chapter_text(proj, ch)
        if not body.strip():
            continue
        n = _int(ch.get("chapterNumber"), 0)
        title = str(ch.get("title") or "").strip()
        texts.append((n, title, body))
    if not texts:
        return ""
    total = sum(len(t[2]) for t in texts)
    if total <= budget:
        chunks = [f"【第{n}章 {title}】\n{body}" for n, title, body in texts]
    else:
        # 预留标题/省略标记的开销，避免最后再统一截断把末章的「章末钩子」切掉
        per = max(300, (budget - len(texts) * 60) // len(texts))
        head_chars = int(per * 0.65)
        tail_chars = per - head_chars
        chunks = []
        for n, title, body in texts:
            piece = (body[:head_chars] + "\n……（本章中段略）……\n" + body[-tail_chars:]
                     if len(body) > per else body)
            chunks.append(f"【第{n}章 {title}】\n{piece}")
    return "\n\n".join(chunks)


# ---------------------------------------------------------------- 蓝图层

def group_chapters(chapters: list[dict], per_episode: int = DEFAULT_CHAPTERS_PER_EPISODE,
                   target_episodes: int | None = None) -> list[dict]:
    """把全书章节按序切成集，返回分集蓝图（确定性，不调用模型）。

    ``target_episodes`` 优先：把全部章节连续均分成至多 N 集；否则按
    ``per_episode`` 章/集。任何情况下都不丢章——最后一组吸收全部剩余章节。
    """
    rows = sorted((c for c in chapters if _int(c.get("chapterNumber"), 0) > 0),
                  key=lambda c: _int(c["chapterNumber"]))
    if not rows:
        return []
    pe = clamp_int(per_episode, 1, 6, DEFAULT_CHAPTERS_PER_EPISODE)
    if target_episodes:
        n = clamp_int(target_episodes, 1, min(MAX_EPISODES, len(rows)), len(rows))
        size = max(1, math.ceil(len(rows) / n))
        groups = [rows[i:i + size] for i in range(0, len(rows), size)]
        # 均分可能产生 n+1 组，把多出来的尾巴并进最后一组，保证集数不超上限
        if len(groups) > n:
            groups = groups[:n - 1] + [sum(groups[n - 1:], [])]
    else:
        groups = [rows[i:i + pe] for i in range(0, len(rows), pe)]
    out = []
    for idx, grp in enumerate(groups, 1):
        nums = [_int(c["chapterNumber"]) for c in grp]
        out.append({
            "no": idx,
            "chapters": nums,
            "titles": [str(c.get("title") or f"第{n}章")
                       for c, n in zip(grp, nums, strict=False)],
            "title": str(grp[0].get("title") or f"第{nums[0]}章"),
        })
    return out


def outline_brief(outline: list[dict], summaries: dict[int, dict]) -> str:
    """把分集蓝图渲染成 ``06-季弧与分集.md``（工作台/模型都能读）。"""
    md = ["# 分集改编蓝图\n",
          f"共 {len(outline)} 集；覆盖原著 {sum(len(x['chapters']) for x in outline)} 章。\n",
          "| 集 | 覆盖章节 | 本集章名 |", "| :--- | :--- | :--- |"]
    for ep in outline:
        nums = ep["chapters"]
        span = f"{nums[0]}-{nums[-1]}" if len(nums) > 1 else str(nums[0])
        md.append(f"| 第{ep['no']}集 | 第 {span} 章 | {' / '.join(ep['titles'])} |")
    md.append("\n## 逐集要点\n")
    for ep in outline:
        md.append(f"### 第{ep['no']}集 · {ep['title']}")
        for n in ep["chapters"]:
            s = summaries.get(n) or {}
            if not s:
                continue
            beats = s.get("beats") or []
            md.append(f"- 第{n}章《{s.get('title') or ''}》：{(s.get('summary') or '')[:220]}")
            for b in beats[:3]:
                md.append(f"  - {str(b)[:120]}")
        md.append("")
    return "\n".join(md) + "\n"


def build_bible(llm, material: dict, novel_name: str, form_label: str,
                log=None) -> str:
    """一次模型调用提炼全书创作圣经；失败时退回已有素材，绝不阻断改编。

    这是解决「每集人物/设定对不上」的关键：全书只提炼一次，之后每集都带着
    同一份硬规则写作。
    """
    digest = material.get("digest") or ""
    raw = "\n\n".join(filter(None, [
        material.get("bible", ""), material.get("season", ""),
        material.get("outline", ""), digest,
    ]))
    if raw.strip():
        prompt = (
            f"你是短剧总编剧，要把长篇小说《{novel_name}》完整改编成{form_label}。"
            "请从全书素材中提炼一份《改编创作圣经》，输出 Markdown，包含：\n"
            "1. 一句话故事核与主线冲突；2. 主角、主要盟友、反派梯队（姓名必须与原著完全一致）；\n"
            "3. 世界观硬规则与能力体系（改编时绝不许改）；4. 全书起承转合的四段式弧线；\n"
            "5. 改编原则：哪些必须保留、哪些可以压缩、哪些内心戏要转成可视动作与台词。\n"
            "只输出圣经正文，不要解释过程，不要写分集内容。\n\n"
            f"【全书素材】\n{raw[:BIBLE_BUDGET + 9000]}"
        )
        try:
            out = (llm.chat([{"role": "user", "content": prompt}],
                            stage="planning", max_tokens=4000, use_cache=False) or "").strip()
            if len(out) > 200:
                return out[:BIBLE_BUDGET]
            if log:
                log("⚠️ 创作圣经生成结果过短，改用原著已有素材作为全局设定")
        except Exception as e:
            if log:
                log(f"⚠️ 创作圣经生成失败（{type(e).__name__}），改用原著已有素材作为全局设定")
    # 兜底：原著已落盘的圣经/人物档案本身就是可靠的全书级事实
    return ("# 改编创作圣经（依据原著既有设定）\n\n"
            + (material.get("bible") or "")
            + "\n\n## 全书弧线\n\n" + (material.get("season") or "")
            + "\n\n## 章节总览\n\n" + (digest or ""))[:BIBLE_BUDGET + 9000]


def episode_beat_text(ep: dict, summaries: dict[int, dict]) -> str:
    """本集大纲：覆盖章节的摘要 + 节拍，作为模型写作的事实约束。"""
    parts = []
    for n in ep["chapters"]:
        s = summaries.get(n) or {}
        if not s:
            parts.append(f"- 第{n}章（原著无摘要，按正文改编）")
            continue
        beats = s.get("beats") or []
        line = f"- 第{n}章《{s.get('title') or ''}》：{(s.get('summary') or '').strip()[:300]}"
        if beats:
            line += "\n  节拍：" + "；".join(str(b)[:100] for b in beats[:4])
        parts.append(line)
    return "\n".join(parts)


def previous_episode_tail(path: str, limit: int = PREV_EPISODE_CHARS) -> str:
    """上一集正文结尾，作为前情提要（保证集与集接得上）。"""
    text = _read(path).strip()
    return text[-limit:] if text else ""


# ---------------------------------------------------------------- 写作层

def validate_episode(body: str, *, min_chars: int = MIN_EPISODE_CHARS,
                     min_headings: int = MIN_SCENE_HEADINGS,
                     min_lines: int = MIN_DIALOGUE_LINES) -> tuple[bool, str]:
    """单集质量门禁。返回 (是否合格, 不合格原因)。

    第 11 集那种 42 字残片会在第一关就被拦下；此外还检查场景数、台词数与
    「半句话结尾」——模型撞 max_tokens 时最典型的特征就是末尾没有收束标点
    或括号/加粗没闭合。
    """
    text = (body or "").strip()
    if len(text) < min_chars:
        return False, f"正文仅 {len(text)} 字，低于 {min_chars} 字下限（疑似输出截断）"
    headings = len(_SCENE_HEAD_RE.findall(text))
    if headings < min_headings:
        return False, f"只有 {headings} 个场景标题，至少需要 {min_headings} 个"
    lines = len(_LINE_RE.findall(text))
    if lines < min_lines:
        return False, f"只有 {lines} 句台词，至少需要 {min_lines} 句"
    if text.count("**") % 2 or text.count("（") > text.count("）") or text.count("[") > text.count("]"):
        return False, "加粗/括号未闭合，疑似输出中途截断"
    tail = text.rstrip()
    if not tail:
        return False, "正文为空"
    last = tail[-1]
    if last in _TAIL_TRUNCATED:
        return False, "正文以逗号/分号结尾，疑似半句截断"
    # 中文剧本正常以句号、引号、动作括号收束；以汉字/字母收尾即「扶手上」这类残片
    if last not in _TAIL_OK:
        return False, "正文末尾没有收束标点，疑似输出截断"
    # v4.33.7 统一交付标准：语言污染 / 元信息泄漏属于任何档位都不允许的硬缺陷。
    # 命中后返回的原因会被 generate_episode 自动回灌给模型重写，不新增重试轮次。
    _hard = [f for f in delivery_gate.check_episode(text, profile="universal")
             if f["severity"] == "block" and f["code"] in ("N02", "N03", "S11")]
    if _hard:
        return False, "；".join(f"{f['code']} {f['name']}：{f['detail']}" for f in _hard[:2])
    return True, ""


def inspect_existing_body(path: str, **kw) -> tuple[bool, str]:
    """检查磁盘上已存在的「已完成」集是否真的合格。

    旧版把残片也标成 completed，升级旧剧本时必须靠这个把坏集挑出来重跑。
    """
    if not os.path.exists(path):
        return False, "正文文件不存在"
    return validate_episode(_read(path), **kw)


def build_episode_messages(*, bible: str, ep: dict, total: int, source: str,
                           beat: str, prev_tail: str, form_label: str,
                           novel_name: str, min_chars: int,
                           min_headings: int, min_lines: int) -> list[dict]:
    sys_msg = (
        f"你是竖屏微短剧总编剧，正在把长篇小说《{novel_name}》完整改编成{form_label}。"
        "必须严格遵守《改编创作圣经》里的姓名、世界观与能力体系，人物不许改名、"
        "设定不许自创；把心理描写转成可拍摄的动作与短促台词。\n"
        "输出格式：先写一行「# 第N集：集名」，然后每场以「**场M：内/外 地点 时间**」"
        "开头，场内写动作行与台词（角色名：台词）。\n"
        f"硬性要求：本集不少于 {min_chars} 字、不少于 {min_headings} 个场景、"
        f"不少于 {min_lines} 句台词，且必须是完整收束的正文（不许中途截断）。"
        "开场前 3 秒抓人，集尾留强钩子；覆盖章节的主要事件都要交代，不能只写开头。"
        "只输出剧本正文，不要解释、不要写创作说明、不要加代码块。"
    )
    user = [
        f"【全书创作圣经 · 禁改】\n{bible[:BIBLE_BUDGET]}",
        f"【全书进度】本剧共 {total} 集，现在写第 {ep['no']} 集；"
        f"本集覆盖原著第 {ep['chapters'][0]}-{ep['chapters'][-1]} 章"
        f"（{len(ep['chapters'])} 章）。",
    ]
    if prev_tail:
        user.append(f"【上一集结尾 · 必须接得上】\n……{prev_tail}")
    user.append(f"【本集改编大纲 · 事实约束】\n{beat}")
    user.append(f"【原著对应章节正文】\n{source}")
    user.append("请据此写出完整的一集剧本。")
    return [{"role": "system", "content": sys_msg},
            {"role": "user", "content": "\n\n".join(user)}]


def generate_episode(llm, messages: list[dict], *, max_attempts: int = MAX_ATTEMPTS,
                     **limits) -> tuple[str | None, str, int]:
    """生成单集并过质量门禁；不合格就把原因回灌给模型重写。

    返回 (正文 or None, 失败原因, 实际调用次数)。use_cache=False + 每轮追加
    纠错消息，避免命中缓存拿到同一份残片。
    """
    msgs = [dict(m) for m in messages]
    reason = ""
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            body = (llm.chat(msgs, stage="writing", max_tokens=8000,
                             use_cache=False) or "").strip()
        except Exception as e:
            reason = f"LLM 调用失败：{type(e).__name__}: {e}"
            continue
        ok, reason = validate_episode(body, **limits)
        if ok:
            return body, "", attempt
        msgs.append({"role": "assistant", "content": body[:3000]})
        msgs.append({"role": "user", "content":
                     f"上一稿不合格：{reason}。请重新输出**完整的一集剧本**，"
                     "从集名第一行写到集尾钩子收束，不要截断、不要只写开头、不要解释。"})
    return None, reason or "未拿到可用正文", max(1, max_attempts)
