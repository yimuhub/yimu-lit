"""写作骨架（v0.9.0）：每本书一份 `17-写作骨架.md`。

聚合「写到哪 / 写了多少 / 本次重点 / 为什么这么写 / 续写提示」，形成一份**可读 + 可喂给
Agent 的总体状态**。写完 / 重写 / 放行后自动再生；续写与改写时经 `_build_ctx` 注入 Agent 上下文，
让它无需通读全书即可快速对齐总体状态（比通读快得多，也更容易保持一致）。

纯文件聚合（不花 LLM），因此始终可用、秒级生成。
"""
import json
import os
import re
import datetime

from bot import writer

SKELETON = "17-写作骨架.md"


def _rf(pdir, fname, cap=8000):
    p = os.path.join(pdir, fname)
    try:
        t = open(p, encoding="utf-8").read()
        return t[:cap]
    except Exception:
        return ""


def _truncate(s, n=260):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _plain(md, cap=260):
    """把 markdown 段（去 # 标题/强调符/引用/子弹）压成一段可读文字，避免 '## 文风指纹' 混入。"""
    txt = re.sub(r"^#{1,6}\s*", "", md or "", flags=re.M)
    txt = txt.replace("**", "").replace(">", "").replace("·", " ")
    lines = [ln.strip() for ln in txt.split("\n") if ln.strip() and not ln.strip().startswith(("-", "*"))]
    out = " ".join(lines)
    return out[: cap - 1] + "…" if len(out) > cap else out


def _section(md, heading, stop=("##",), cap=1600):
    """取某 `## 标题` 到下一个同级/标题 之间的正文。"""
    if not md:
        return ""
    lines = md.split("\n")
    out, grab = [], False
    for ln in lines:
        if ln.startswith("## ") and heading in ln:
            grab = True
            continue
        if grab and ln.startswith(stop[0]):
            break
        if grab:
            out.append(ln)
    txt = "\n".join(out).strip()
    return txt[:cap]


def _chapter_summaries(pdir):
    """从 11-章节摘要.md 抽每章摘要（同名取最长一条，去重）。返回 {章号: 摘要}。"""
    md = _rf(pdir, "11-章节摘要.md", 40000)
    if not md:
        return {}
    best = {}
    cur = None
    for ln in md.split("\n"):
        m = re.match(r"^##\s*第\s*(\d+)\s*章", ln.strip())
        if m:
            cur = int(m.group(1))
            best.setdefault(cur, "")
            continue
        if cur is not None:
            mm = re.match(r"^-\s*摘要：\s*(.+)", ln.strip())
            if mm and len(mm.group(1).strip()) > len(best.get(cur, "")):
                best[cur] = mm.group(1).strip()
    return best


def _blueprint_beats(pdir):
    """从 02b-总架构.md『全章节拍』抽 {章号: (目标,事件,钩子)}。"""
    md = _rf(pdir, "02b-总架构.md", 60000)
    if not md:
        return {}
    beats = {}
    cur = None
    for ln in md.split("\n"):
        m = re.match(r"^###\s*第\s*(\d+)\s*章", ln.strip())
        if m:
            cur = int(m.group(1))
            beats[cur] = {"目标": "", "事件": "", "钩子": ""}
            continue
        if cur is not None and ln.strip().startswith("-"):
            mm = re.match(r"^-\s*(目标|事件|钩子|承接)[:：]\s*(.+)", ln.strip())
            if mm:
                beats[cur][mm.group(1)] = mm.group(2).strip()
        if ln.strip().startswith("## ") and "全章节拍" not in ln and cur is not None:
            cur = None
    return beats


def build(key, stamp=True):
    """生成/刷新 `17-写作骨架.md`，返回绝对路径；书不存在返回 None。"""
    pdir = os.path.join(writer.PROJECTS_DIR, key)
    if not os.path.isdir(pdir):
        return None
    li = writer.library_item(key, with_quality=True)
    plan = {}
    pj = os.path.join(pdir, "02-写作计划.json")
    if os.path.exists(pj):
        try:
            plan = json.load(open(pj, encoding="utf-8"))
        except Exception:
            plan = {}
    chapters = plan.get("chapters") or []
    name = li.get("name") or key
    done, total = li.get("done", 0), li.get("total", 0) or (plan.get("totalChapters") or 0)
    q, passing, pass_line = li.get("quality"), li.get("passing"), li.get("pass_line")
    qtxt = "未审稿" if q is None else f"{q} 分（{'已达标 ✅' if passing else '未达标'}，达标线 {pass_line}）"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # ① 状态 & 进度
    L = ["# 🦴 写作骨架 · 《%s》" % name, ""]
    L += ["> 自动聚合（一幕·神笔 · ShenBi NovelCraft）：写到哪 / 写了多少 / 本次重点 / 为什么这么写 / 续写提示。",
          "> Agent 续写或改写前，先读本文件快速对齐总体状态（比通读全书快、也更容易保持一致）。",
          f"_状态：{li.get('status_label','?')} · 更新：{now}_", ""]
    L += ["## ① 状态 & 进度",
          f"- 进度：**{done} / {total} 章** · 共 **{li.get('total_words',0)} 字** · 质量：{qtxt}"]
    if li.get("released"):
        L.append(f"- 已放行成书（最终 {li.get('final_quality') if li.get('final_quality') is not None else q} 分 · 门禁 {li.get('gate_rounds') or 0} 轮）")
    completed = [c.get("chapterNumber") for c in chapters if c.get("status") == "completed"]
    if completed:
        L.append(f"- 已完成章：第 {completed[0]}–{completed[-1]} 章（{len(completed)} 章）")
    L.append("")

    # ② 写到哪儿了（各章一句话）
    summ = _chapter_summaries(pdir)
    L += ["## ② 写到哪儿了（各章一句话）"]
    if chapters:
        for c in chapters:
            no = c.get("chapterNumber")
            st = c.get("status", "")
            mark = "✅" if st == "completed" else ("✍️" if st in ("writing", "in_progress") else "·")
            title = c.get("title", "") or f"第{no}章"
            sm = summ.get(no, "")
            L.append(f"- {mark} 第{no:02d}章 · {title} — {_truncate(sm, 200) or '（无摘要）'}")
    else:
        L.append("- （暂无章节记录）")
    # 当前主线节点（09 兑现节点 / 最近完成章的摘要）
    bible = _rf(pdir, "09-故事圣经.md", 6000)
    node = _section(bible, "兑现方式")
    if not node:
        node = _section(bible, "兑现节点")
    if node:
        L += ["", "**当前兑现节点 / 主线节奏：**", _truncate(node.replace("\n", " · "), 400)]
    L.append("")

    # ③ 为什么这么写（创作意图）
    L += ["## ③ 为什么这么写（创作意图）"]
    engine = _section(bible, "故事引擎")
    promise = _section(bible, "开书承诺")
    theme = _section(bible, "主题")
    if engine:
        L.append(f"- **故事引擎**：{_plain(engine, 300)}")
    if promise:
        L.append(f"- **核心爽点 / 情绪承诺**：{_plain(promise, 420)}")
    if theme:
        L.append(f"- **主题**：{_plain(theme, 300)}")
    style = _rf(pdir, "03-文风基准.md", 2000)
    stl = _plain(style, 240)
    if stl:
        L.append(f"- **文风基调**：{stl}")
    L.append("")

    # ④ 本次重点 / 最近改动
    rw = _rf(pdir, "16-重写记录.md", 4000)
    L += ["## ④ 本次重点 / 最近改动"]
    if rw.strip():
        head = rw.strip().split("\n")[0:2]
        L += [f"- {_truncate((h or '').lstrip('#>').strip(), 240)}" for h in head if (h or "").strip().lstrip('#').strip()]
        weak = _section(rw, "仍弱维度") or _section(rw, "改写后仍弱")
        if weak:
            L.append(f"- **仍弱维度（后续优先补）**：{_plain(weak, 320)}")
    else:
        L.append("- （暂无重写记录，尚未定向改写）")
    rev = _rf(pdir, "13-修订记录.md", 2000)
    if rev.strip():
        L.append(f"- 修订尾迹：{_truncate(rev.strip().splitlines()[-1].lstrip('#>').strip(), 200) or '—'}")
    L.append("")

    # ⑤ 续写 / 改写提示（给 Agent）
    L += ["## ⑤ 续写 / 改写提示（给 Agent）"]
    beats = _blueprint_beats(pdir)
    pending = [c for c in chapters if c.get("status") != "completed"]
    if pending:
        nxt = min(pending, key=lambda c: c.get("chapterNumber") or 999)
        nno = nxt.get("chapterNumber")
        L.append(f"- **待写章节**：第 {nno}–{max(c.get('chapterNumber') or 0 for c in pending)} 章（共 {len(pending)} 章未完成）")
        b = beats.get(nno, {})
        if b.get("目标") or b.get("事件"):
            L.append(f"- **下一章（第{nno}章）重点**：目标 {b.get('目标','—')}；事件 {b.get('事件','—')}；钩子 {b.get('钩子','—')}")
    else:
        L.append(f"- **全书 {total} 章已全部完成**；如需推进 → 走『定向重写/放行门禁』把弱维度补到达标。")
    timeline = _rf(pdir, "12-时间线与连续性.md", 4000)
    tl = _section(timeline, "未闭合") or _section(timeline, "待闭合") or _section(timeline, "连续性")
    if tl:
        L.append(f"- **注意连续性**：{_plain(tl, 260)}")
    bible2 = _rf(pdir, "09-故事圣经.md", 6000)
    world = _section(_rf(pdir, "02b-总架构.md", 6000), "世界观")
    if world:
        L.append(f"- **保持世界观/人物一致**：{_plain(world, 240)}")
    L.append("- **别破坏已兑现的伏笔 / 已定的人物弧线**；改写前先读 09 故事圣经 + 02b 总架构对齐。")
    L.append("")
    L.append("_（本文件由一幕·神笔自动生成，改写完会自动刷新；人可读，也可直接粘贴给 LLM 作为总体状态上下文。）_")

    out = os.path.join(pdir, SKELETON)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    # 记到 02-写作计划.json.meta.skeleton（供进度/时间戳展示）
    try:
        writer.set_meta(key, skeleton=now, skeleton_words=len("\n".join(L)))
    except Exception:
        pass
    return out


def read(key, cap=6000):
    """读取现有骨架（供前端展示 / Agent 上下文注入）。"""
    p = os.path.join(writer.PROJECTS_DIR, key, SKELETON)
    if not os.path.exists(p):
        return ""
    try:
        return open(p, encoding="utf-8").read()[:cap]
    except Exception:
        return ""


def regenerate(key):
    """供 app.py / 钩子调用：刷新骨架，返回路径或 None。"""
    return build(key)
