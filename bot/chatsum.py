"""对话上下文自动压缩（v2.9.0）——只影响「向创作者展示的对话」，后台创作完全不受影响。

设计：
  · 对话历史按 token 粗估超窗时，把溢出的早期消息用 LLM 压缩成「创作进度摘要」；
  · 摘要持久化在 <书>/chat_summary.json（滚动合并，反复压缩不丢早期信息）；
  · 摘要注入后续对话的 LLM 上下文（@Agent 群聊 / 副驾），让对话 Agent 记得全书进度；
  · 前端显示「已压缩 N 条早期对话」胶囊，点开可看摘要。
"""

import json
import os
import re

from .safeio import atomic_write_json

# 档位：对话展示上下文窗口（token 粗估）。默认 200K。
CONTEXT_LIMITS = {"200k": 200_000, "400k": 400_000, "500k": 500_000, "1m": 1_000_000}
DEFAULT_LIMIT = "200k"
SUMMARY_FILE = "chat_summary.json"


def load_limit() -> str:
    try:
        from .providers import ROOT
        cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
        v = str((cfg.get("defaults") or {}).get("chat_context_limit") or DEFAULT_LIMIT).lower()
        return v if v in CONTEXT_LIMITS else DEFAULT_LIMIT
    except Exception:
        return DEFAULT_LIMIT


def compress_enabled() -> bool:
    try:
        from .providers import ROOT
        cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
        return bool((cfg.get("defaults") or {}).get("chat_auto_compress", True))
    except Exception:
        return True


def estimate_tokens(messages) -> int:
    """中文 ≈ 1.5 字/token 的粗估（与 model_rules.clamp_context 同口径量级）。"""
    total = 0
    for m in messages or []:
        if isinstance(m, dict):
            total += len(re.findall(r"[\u4e00-\u9fff]", str(m.get("content") or ""))) // 3 * 2
            total += len(str(m.get("content") or "")) // 4
    return total


def _summary_path(pdir: str) -> str:
    return os.path.join(pdir, SUMMARY_FILE)


def load_summary(pdir: str) -> dict:
    try:
        return json.load(open(_summary_path(pdir), encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_summary(pdir: str, data: dict) -> None:
    try:
        atomic_write_json(_summary_path(pdir), data)
    except Exception:
        pass


def _llm_available():
    try:
        from . import llm
        return llm
    except Exception:
        return None


def maybe_compress(pdir: str, history: list) -> dict:
    """超窗则压缩早期消息。返回 {compressed, count, summary, history}。

    history: [{"role","content",...}]（时间正序）。已压缩过的消息数记录在
    chat_summary.json.compressed_msgs，重复调用只压缩新增溢出部分，不会反复摘要。
    任何失败都原样返回，绝不阻断对话。
    """
    res = {"compressed": False, "count": 0, "summary": "", "history": history}
    if not history or not compress_enabled():
        return res
    limit = CONTEXT_LIMITS.get(load_limit(), CONTEXT_LIMITS[DEFAULT_LIMIT])
    prev = load_summary(pdir)
    already = int(prev.get("compressed_msgs") or 0)
    if already >= len(history):
        return res
    recent = history[already:]
    if estimate_tokens(recent) <= limit:
        return res
    # 从最早开始保留尾部窗口内的一半（给本轮回复留空间），其余压缩
    keep_from = max(1, len(recent) // 2)
    while keep_from < len(recent) and estimate_tokens(recent[keep_from:]) > limit // 2:
        keep_from += 1
    overflow, keep = recent[:keep_from], recent[keep_from:]
    if not overflow:
        return res

    llm = _llm_available()
    transcript = "\n".join(
        f"{'用户' if (m.get('role') == 'user') else '助手'}：{str(m.get('content') or '')[:2000]}"
        for m in overflow[:80])
    prev_text = str(prev.get("summary") or "")
    prompt = (
        "你是创作对话的进度记录员。把下面这段创作者与助手的对话压缩成一份「创作进度摘要」，"
        "保留：作品名与题材、已完成章节与剧情要点、人物与设定决定、创作者下达过的修改指令与偏好。"
        "用紧凑的分点，500 字以内。\n")
    if prev_text:
        prompt += f"\n此前已有的摘要（请合并而非覆盖）：\n{prev_text[:2000]}\n\n"
    prompt += f"本轮对话：\n{transcript[:16000]}"
    summary = ""
    if llm is not None:
        try:
            summary = str(llm.chat([{"role": "user", "content": prompt}]) or "").strip()
        except Exception:
            summary = ""
    if not summary:
        # LLM 不可用：确定性兜底摘要（保关键信息不丢）
        heads = [str(m.get("content") or "")[:60] for m in overflow[:10]]
        summary = (prev_text + "\n" if prev_text else "") + "（早期对话要点）" + "／".join(h.strip() for h in heads if h.strip())
    _save_summary(pdir, {"summary": summary.strip(),
                         "compressed_msgs": already + len(overflow),
                         "at": __import__("datetime").datetime.now().isoformat(timespec="seconds")})
    return {"compressed": True, "count": already + len(overflow),
            "summary": summary.strip(), "history": keep}
