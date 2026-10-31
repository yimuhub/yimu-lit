"""字数守护（v4.16.3）：全文体通用的字数验收、扩充约束、续写兜底与采纳门禁。

背景（v4.16.2 小说引擎实证）：LLM 扩充/改写会偶发返回远短于原稿的「删节版」，
盲目采纳导致「2858→458」倒退与多轮无效重写浪费 token。本模块把三类防线下沉为
通用件，供所有文体引擎接入：
  1. 首写目标 = min_words × 1.1（提示带硬下限，验收仍按 min_words）；
  2. 扩充/改写结果过采纳门禁（比原稿短或低于下限八成 → 拒绝）；
  3. 末轮从结尾续写拼接（增量保证，不丢已有内容）。
"""
from __future__ import annotations

import re


def cn_len(text: str) -> int:
    """中文字符数（与 skill.check_words 同口径）。"""
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def target_of(min_words: int, over: float = 1.1) -> int:
    """写作提示用的目标字数（默认上浮 10%）；验收仍按 min_words。"""
    return int(max(200, int(min_words or 0)) * over)


def expansion_prompt(target: int, current: int) -> str:
    """扩充调用的硬约束片段：真实数字 + 严禁缩写。"""
    return (f"当前正文仅 {current} 字，未达到硬性下限 {target} 字。"
            f"请扩写到不少于 {int(target * 1.1)} 字：增加对话场景、人物内心、细节描写与环境氛围，"
            f"保持情节走向、人物设定与既有钩子完全不变；"
            f"严禁注水、严禁缩写、严禁输出任何说明性文字，直接输出扩充后的完整正文。")


def adoption_ok(old_text: str, new_text: str, min_words: int, floor_ratio: float = 0.8) -> bool:
    """采纳门禁：新稿比原稿短（倒退）或低于下限×floor_ratio → 拒绝采纳。"""
    o, n = cn_len(old_text), cn_len(new_text)
    if n < o:
        return False
    if n < int(min_words * floor_ratio):
        return False
    return True


def continue_prompt(tail_text: str, need_words: int) -> str:
    """续写调用的硬约束片段：从断点无缝续写指定字数。"""
    return (f"下面是本章/本节已有正文的结尾。请从它的断点**无缝继续往下写**约 {need_words} 字，"
            f"自然延续当前场景、人物与叙事节奏，保持同样的文风；"
            f"不要重复已有内容、不要总结、不要输出任何说明性文字，直接输出续写的正文。\n\n……{tail_text}")


def clean_continuation(text: str) -> str:
    """续写结果去杂音：剥客套开场、续写小节标题、思考标签。"""
    t = (text or "").strip()
    t = re.sub(r"^(好的|以下是|续写如下|续写)[:：,，]?\s*", "", t)
    t = re.sub(r"(?m)^#{1,3}\s*.*?(续|补).*?$", "", t)
    t = re.sub(r"</?think[^>]*>", "", t)
    return t.strip()
