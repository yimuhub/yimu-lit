"""阶段体检（v4.33.9）：批量写作每 10 章/集的确定性中途体检。

缺陷背景：batch_size="all" 一口气写完 100 章，中途零人工介入、零中途信号——
等成书体检发现问题，25 万字已经写完了。本模块每 N 章/集产出一份《阶段体检》：
口癖热度榜、未登记实体候选、数值口径回归、伏笔账本健康、最近单元的交付残留。

设计约束：
  · 全部确定性（零 LLM），单次执行秒级；
  · 只读 + 写一份 ``24-阶段体检.json``，不阻断创作（发现的坑由下一章的
    fill_plan / 口癖禁用 / 交付预检回灌去消化）；
  · 剧本与小说共用：传 rows=[{n,title,text}] 即可。
"""
from __future__ import annotations

import json
import os
import re

from . import delivery_gate, lexicon_ledger, lorebook
from .safeio import atomic_write_json

REPORT_NAME = "24-阶段体检.json"


# --------------------------------------------------------------- 伏笔账本健康

def foreshadow_health(proj_path: str, current_no: int) -> dict:
    """19-伏笔账本.json 健康度：open/超期/空描述/重复坑。"""
    path = os.path.join(proj_path, "19-伏笔账本.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"total": 0, "open": 0, "overdue": [], "empty_desc": 0, "duplicates": []}
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, dict):
        items = {str(i): (it if isinstance(it, dict) else {}) for i, it in enumerate(items or [])}
    open_no = 0
    overdue = []
    empty_desc = 0
    seen_pit: dict[str, str] = {}
    dups = []
    for name, it in items.items():
        if not isinstance(it, dict):
            continue
        st = str(it.get("status") or "open").lower()
        if st in ("paid", "dropped", "回收", "放弃"):
            continue
        open_no += 1
        desc = str(it.get("description") or it.get("notes") or "").strip()
        if not desc:
            empty_desc += 1
        due = it.get("due_to") or it.get("due_from") or 0
        try:
            due = int(due)
        except (TypeError, ValueError):
            due = 0
        if due and current_no and current_no >= due:
            overdue.append(f"{name}(应第{due}章前回收)")
        key = re.sub(r"[\s，。；、!？?]", "", str(it.get("notes") or it.get("description") or name))[:18]
        if key and key in seen_pit:
            dups.append(f"{name} ≈ {seen_pit[key]}")
        else:
            seen_pit[key] = name
    return {"total": len(items), "open": open_no, "overdue": overdue[:8],
            "empty_desc": empty_desc, "duplicates": dups[:5]}


# --------------------------------------------------------------- 未登记实体候选

_STOP_CAND = frozenset("""
时候 他们 自己 我们 一个 没有 已经 这样 什么 就是 现在 知道 然后 突然 最后
一下 起来 出来 这个 那个 而且 但是 所以 因为 如果 虽然 可是 只是 还是 真的
东西 地方 事情 声音 力量 身体 双手 目光 眼神 脸上 手中 之中 上方 下方 前方
最后 终于 竟然 似乎 仿佛 宛如 瞬间 顿时 立刻 缓缓 微微 轻轻 慢慢 渐渐 一声
巨响 之中 之上 之下 之后 之前 之间 名字 样子 样子 里面 外面 旁边 周围 附近
""".split())


def unregistered_candidates(texts: list[str], known_names: set[str], *,
                            min_count: int = 6, topn: int = 15) -> list[dict]:
    """疑似未登记实体：全书高频 3-gram，不在圣经/状态库已知名单里。
    中文无分词，这里只做候选提示（人工/一键补录），不判实体。"""
    counter: dict[str, int] = {}
    for t in texts:
        z = re.sub(r"[^\u4e00-\u9fff]+", "", t or "")
        for i in range(len(z) - 2):
            g = z[i:i + 3]
            counter[g] = counter.get(g, 0) + 1
        for i in range(len(z) - 3):
            g = z[i:i + 4]
            counter[g] = counter.get(g, 0) + 1
    out = []
    for g, c in sorted(counter.items(), key=lambda kv: (-len(kv[0]), -kv[1])):
        if c < min_count or g in _STOP_CAND:
            continue
        if any(g in k or k in g for k in known_names):
            continue
        # 4 字词优先，吞掉它的 3 字子串（青铜角犀 优先于 青铜角/铜角犀）
        if any(g in o["gram"] for o in out):
            continue
        out.append({"gram": g, "count": c})
        if len(out) >= topn:
            break
    return out


# --------------------------------------------------------------- 阶段体检主入口

def stage_checkup(proj, rows: list[dict], *, kind: str = "novel") -> dict:
    """每 N 章/集调用一次。rows：本次区间内已定稿单元 [{n,title,text}]。
    结果写 24-阶段体检.json（追加 history）并 log。"""
    proj_path = proj.path
    done = int(rows[-1]["n"]) if rows and str(rows[-1].get("n") or "").isdigit() else len(rows)
    # 口癖账本
    hot = lexicon_ledger.hot_items(proj_path, units_done=max(done, len(rows)),
                                   min_units=max(5, len(rows) // 2), ratio=0.3, topn=10)
    # 数值口径（只看本区间）
    numeric = delivery_gate.numeric_findings(rows)
    # 交付残留（N01/N02 快扫本区间）
    residue = []
    for r in rows:
        for f in delivery_gate.chapter_purity_blocks(r.get("text") or "",
                                                     chapter_no=int(r["n"]) if str(r.get("n") or "").isdigit() else 0):
            residue.append({"code": f["code"], "loc": f["location"], "detail": f["detail"][:60]})
    # 未登记实体候选
    known = {e.get("name") for e in lorebook.entries(proj_path) if e.get("name")}
    try:
        from . import state as _state
        for k in ((_state.load(proj_path) or {}).get("entities") or {}):
            known.add(str(k))
    except Exception:
        pass
    candidates = unregistered_candidates([r.get("text") or "" for r in rows], known)
    # 伏笔健康（小说）
    foreshadow = foreshadow_health(proj_path, done) if kind == "novel" else {}
    report = {
        "at_unit": done, "kind": kind,
        "lexicon_hot": hot,
        "numeric_conflicts": [f"{f['location']} {f['detail']}" for f in numeric],
        "delivery_residue": residue[:10],
        "unregistered_candidates": candidates,
        "foreshadow": foreshadow,
        "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    _append_report(proj_path, report)
    # 汇总日志（说人话版）
    bits = []
    if hot:
        bits.append("口癖热门：" + "、".join(f"「{h['gram']}」×{h['units']}章" for h in hot[:5]))
    if numeric:
        bits.append(f"数值口径冲突 {len(numeric)} 处")
    if residue:
        bits.append(f"交付残留 {len(residue)} 处")
    if candidates:
        bits.append("疑似未登记实体：" + "、".join(c["gram"] for c in candidates[:5]))
    if foreshadow:
        bits.append(f"伏笔 open {foreshadow.get('open', 0)}"
                    + (f"（超期 {len(foreshadow['overdue'])}）" if foreshadow.get("overdue") else ""))
    proj.log("🩺 " + (f"第{done}章阶段体检：" + "；".join(bits) if bits else f"第{done}章阶段体检：未发现异常"))
    return report


def _append_report(proj_path: str, report: dict) -> None:
    path = os.path.join(proj_path, REPORT_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    hist = data.setdefault("history", [])
    hist.append(report)
    del hist[:-20]
    data["latest"] = report
    atomic_write_json(path, data)
