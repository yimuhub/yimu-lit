"""全书口癖账本（v4.33.9）。

缺陷背景：口癖检查只有单章视野（slop_checker 只统计本章内 2-gram），
一部 100 章的书里「握紧」出现 158 次、「游戏才刚刚开始」7 次都没人管。
本模块给每本书一本**跨章账本**：章/集定稿后记账，写作前把高频词组
注入提示（禁用），阶段体检时输出热门口癖榜。

设计约束：
  · 零 LLM、确定性、可单测；
  · 账本文件 ``23-口癖账本.json``：{gram: {count, chapters(尾部30个), last}}；
  · 高频判定按「出现的章数占比」而非总次数——一章里刷 20 次和 20 章各 1 次是两种病；
  · 超高频虚词（他们/自己/一个…）进停用表，不进账本热度榜。
"""
from __future__ import annotations

import json
import os
import re

from .safeio import atomic_write_json

LEDGER_NAME = "23-口癖账本.json"

# 出现频率极高的功能性 2-gram：几乎每章都有，不作为口癖
STOPGRAMS = frozenset("""
我们 他们 她们 自己 一个 一下 什么 怎么 这个 那个 没有 已经 就是 不是 不会
出来 起来 过来 时候 现在 知道 觉得 直接 突然 立刻 马上 这样 那样 这里 那里
他的 她的 我的 你的 因为 所以 但是 然后 只是 还是 就是 都是 也是 在这 在那
一道 一种 一声 一片 身体 声音 双手 目光 声音 眼神 力量 空气 东西 地方 事情
看了 看着 说道 想着 问道 笑了 有点 一些 几乎 简直 甚至 果然 竟然 必须 需要
可以 应该 可能 或许 似乎 仿佛 缓缓 微微 轻轻 慢慢 悄悄
""".split())

_CN = re.compile(r"[\u4e00-\u9fff]+")


def _grams2(text: str) -> list[str]:
    out: list[str] = []
    for seg in _CN.findall(text or ""):
        out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return out


def _load(proj_path: str) -> dict:
    try:
        with open(os.path.join(proj_path, LEDGER_NAME), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def record(proj_path: str, unit_no: int, text: str, *, keep_tail: int = 30) -> int:
    """章/集定稿后记账。返回账本词条数。同一单元内同一词组只记一次「章级命中」，
    但 count 记录真实出现次数。"""
    data = _load(proj_path)
    for g in set(_grams2(text)):
        if g in STOPGRAMS:
            continue
        e = data.setdefault(g, {"count": 0, "chapters": [], "last": 0})
        e["count"] += (text or "").count(g)
        e["last"] = int(unit_no) if str(unit_no).isdigit() else e.get("last") or 0
        chs = e.setdefault("chapters", [])
        if unit_no not in chs:
            chs.append(unit_no)
        del chs[:-keep_tail]
    _save(proj_path, data)
    return len(data)


def _save(proj_path: str, data: dict) -> None:
    # 防膨胀：词条超 2 万时丢弃总次数最少的尾部（口癖榜只关心头部）
    if len(data) > 20000:
        keep = sorted(data.items(), key=lambda kv: -kv[1].get("count", 0))[:12000]
        data = dict(keep)
    atomic_write_json(os.path.join(proj_path, LEDGER_NAME), data)


def hot_items(proj_path: str, *, units_done: int, min_units: int = 8,
              ratio: float = 0.3, topn: int = 20) -> list[dict]:
    """热门口癖榜：在 ≥min_units 个单元出现且占比 ≥ratio 的 2-gram，按占比降序。"""
    if units_done <= 0:
        return []
    out = []
    for g, e in _load(proj_path).items():
        n_units = len(e.get("chapters") or [])
        r = n_units / units_done
        if n_units >= min_units and r >= ratio:
            out.append({"gram": g, "count": e.get("count", 0),
                        "units": n_units, "ratio": round(r, 2)})
    out.sort(key=lambda x: (-x["ratio"], -x["count"]))
    return out[:topn]


def banned_block(proj_path: str, *, units_done: int, min_units: int = 8,
                 ratio: float = 0.3, topn: int = 12) -> str:
    """写作前注入的禁用提示块；没有热门口癖时返回空串。"""
    items = hot_items(proj_path, units_done=units_done, min_units=min_units,
                      ratio=ratio, topn=topn)
    if not items:
        return ""
    lines = "、".join(f"「{it['gram']}」({it['units']}章)" for it in items)
    return (f"\n【全书口癖账本（这些词组已在多章高频出现，本章禁用；"
            f"若必须使用，全章至多 1 次）】{lines}\n")
