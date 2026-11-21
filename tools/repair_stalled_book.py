#!/usr/bin/env python3
"""停摆书体检/修复工具（v5.5.1 写作韧性层）——默认只读体检，--apply 才写盘。

背景（实测《实习生掉马那天》2026-11-21）：供应商代理 120 秒读超时把规划 LLM 打断 →
45 张章卡全是占位卡 → v5 写前闸门拒收 → 写作批 `_ensure_card_planned` 失败停批 →
meta.plan_blocked + 任务档案 stage=plan_blocked →「继续」反复空转。本工具在不调
LLM 的前提下把这类书确定性救回可继续状态：

体检（默认，零写盘）
  · 计划/任务档案/章卡/事实源/状态库五处逐项诊断，给出卡点清单；
  · 幻影目录盘点（无任何文体计划文件且无完成章的目录）。

修复（--apply，全部原子写）
  1. 注册表/日志收口：任务档案 running→paused（journal.reconcile）、
     清 meta.plan_blocked / batch_stopped / budget_stopped / planning_running 残留；
  2. 事实源修补：占位主角/人物不足用 00-人物档案.md（含 `## 一、主角：X` 扩展头）
     确定性登记真名；locations 为空从档案/大纲抽候选；书名错位对齐 plan.novelName；
  3. 章卡修补：按闸门拒绝原因确定性补全硬字段（cast/locations/主角行动/目标/冲突/
     场景链/threads/scene_type/story_time，零 LLM）；复检仍不过 → 降级放行为
     legacy 章卡（写 v5-质量账本 + 报告，绝不静默）；
  4. 状态库一致性：18-状态库 metrics 与磁盘完成章对账修正；
  5. 续跑预检：模拟 writer.resume_book 的前置检查（不真写章），确认计划可继续。

幻影目录（--clean-phantoms，需配合 --apply）
  按 v5.5「先合并后删除」：rename_map 源目录且目标存在 → 先把聊天/事件历史并进
  真书目录再删；有正文（任何章文件）的绝不删，只在报告里列出。

写盘纪律
  · 执行任何写操作前检测 <root>/instance.lock：存在则等 3 秒重试一次；若锁对应
    的进程确实活着（桌面端在跑）→ 拒绝写盘并给出「请退出桌面端后重跑」指引，
    绝不强写。锁是残留（PID 已死/被无关进程复用）时判定可安全写入并在报告注明。

用法
  python3 tools/repair_stalled_book.py                       # 体检全部书
  python3 tools/repair_stalled_book.py --book 掉马那天        # 体检指定书（key 或关键词）
  python3 tools/repair_stalled_book.py --book 掉马那天 --apply
  python3 tools/repair_stalled_book.py --apply --clean-phantoms --all
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_ROOT = os.path.expanduser("~/Library/Application Support/YiMuLit")
APP_MARKERS = ("yimu", "一幕", "run.py", "yimu-lit")   # 判定锁 PID 是不是本应用
MEDIUM_PLAN_FILES = ("02-写作计划.json", "剧本计划.json", "诗词计划.json",
                     "散文计划.json", "论文计划.json", "计划.json")

Now = lambda: datetime.datetime.now().isoformat(timespec="seconds")


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if d is not None else default
    except Exception:
        return default


# ---------------------------------------------------------------- 锁检测（写盘纪律）

def _pid_command(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _lock_holders(lock_path: str) -> list[int]:
    """谁打开着这把锁文件（lsof 失败视为无人持有，不阻塞判断）。"""
    try:
        out = subprocess.run(["lsof", "-t", lock_path],
                             capture_output=True, text=True, timeout=5)
        pids = []
        for tok in (out.stdout or "").split():
            try:
                pids.append(int(tok))
            except ValueError:
                continue
        return pids
    except Exception:
        return []


def instance_lock_status(root: str) -> dict:
    """检测桌面端实例锁。返回 {"writable": bool, "reason": str, "waited": bool}。

    规则（绝不强写）：
    · 锁不存在 → 可写；
    · 锁内 PID 已死 / 被**无关进程**复用（routined 等系统进程拿到回收 PID）→
      判定锁残留，可写（报告注明证据）；
    · PID 活着且命令行像本应用（或 lsof 显示确实持有锁文件）→ 等 3 秒重试一次，
      仍在 → 拒绝写盘，给出「请退出桌面端后重跑」指引。"""
    lock = os.path.join(root, "instance.lock")
    if not os.path.exists(lock):
        return {"writable": True, "reason": "无实例锁", "waited": False}
    try:
        raw = open(lock, encoding="utf-8", errors="replace").read().strip()
    except OSError:
        raw = ""
    try:
        pid = int(str(raw).strip() or "0")
    except ValueError:
        pid = 0
    waited = False
    for attempt in (1, 2):
        cmd = _pid_command(pid) if pid else ""
        alive = bool(pid) and (cmd != "" or _pid_alive_fallback(pid))
        holders = _lock_holders(lock)
        if alive and holders and pid not in holders:
            # 持锁的是别人（罕见）：以持有者为准
            hcmd = _pid_command(holders[0])
            alive, cmd = True, hcmd or cmd
        if not alive:
            return {"writable": True, "waited": waited,
                    "reason": f"锁残留（instance.lock 的 PID {pid or '(空)'} 不在任何进程）——可安全写入"}
        low = (cmd or "").lower()
        looks_like_app = any(m in low for m in APP_MARKERS)
        if holders and not looks_like_app:
            looks_like_app = True   # 确实打开着锁文件：按在跑处理
        if not looks_like_app:
            return {"writable": True, "waited": waited,
                    "reason": f"锁残留（PID {pid} 已被无关进程复用：{cmd[:80] or '未知'}）——可安全写入"}
        if attempt == 1:
            time.sleep(3)           # 写盘纪律：存在则等待 3 秒重试一次
            waited = True
            continue
        return {"writable": False, "waited": True,
                "reason": (f"桌面端正在运行（PID {pid}，{cmd[:80]}），等待 3 秒重试后锁仍在。"
                           "请退出桌面端后重跑本工具；本次拒绝写盘（绝不强写）。")}
    return {"writable": False, "reason": "实例锁检测未通过", "waited": True}


def _pid_alive_fallback(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 体检

def _load_cards_raw(pdir: str) -> dict:
    cards = {}
    fp = os.path.join(pdir, "02d-章卡.jsonl")
    try:
        with open(fp, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict) and d.get("n"):
                    cards[int(d["n"])] = d
    except OSError:
        pass
    return cards


def audit_book(pdir: str) -> dict:
    """单书只读体检：计划/任务档案/章卡/事实源/状态库五处卡点。"""
    from bot import story_kernel as v5
    from bot import writer as w

    key = os.path.basename(pdir.rstrip("/"))
    rep: dict = {"key": key, "name": "", "plan": {}, "journal": {}, "facts": {},
                 "cards": {}, "state": {}, "stall_reasons": [], "phantom": False}
    plan = read_json(os.path.join(pdir, "02-写作计划.json")) or {}
    rep["plan_files"] = [f for f in MEDIUM_PLAN_FILES if os.path.exists(os.path.join(pdir, f))]
    rep["phantom"] = not rep["plan_files"]
    chapter_files = [f for f in os.listdir(pdir) if f.endswith(".md") and f.startswith("第")]
    rep["chapter_files"] = len(chapter_files)
    if rep["phantom"]:
        rep["stall_reasons"].append("无任何文体计划文件（幻影目录候选）")
        return rep
    rep["name"] = str(plan.get("novelName") or plan.get("title") or key)
    chs = plan.get("chapters") or []
    done = sum(1 for c in chs if c.get("status") == "completed") if chs else 0
    meta = plan.get("meta") or {}
    rep["plan"] = {"status": plan.get("status"), "chapters": len(chs), "done": done,
                   "meta_flags": {k: meta.get(k) for k in
                                  ("plan_blocked", "batch_stopped", "budget_stopped",
                                   "planning_running", "paused", "awaiting_title", "error")
                                  if meta.get(k) is not None}}
    # 任务档案（注册表落盘形态）
    jd = read_json(os.path.join(pdir, "任务档案.json")) or {}
    active = jd.get("active") if isinstance(jd.get("active"), dict) else None
    rep["journal"] = {"active": {k: active.get(k) for k in
                                 ("kind", "status", "stage", "note", "done", "total", "current")}
                      if active else None}
    if active and active.get("status") == "running":
        rep["stall_reasons"].append(
            f"任务档案残留 running（{active.get('kind')}「{active.get('title') or ''}」）——"
            "工具进程外没有任何写作线程，属注册表残留")
    if active and str(active.get("stage")) == "plan_blocked":
        rep["stall_reasons"].append("任务档案 stage=plan_blocked（章卡闸门拒收后停批）")
    for flag in ("plan_blocked", "batch_stopped", "budget_stopped", "planning_running"):
        if meta.get(flag):
            rep["stall_reasons"].append(f"计划 meta.{flag}=true 残留")
    # 事实源
    facts = read_json(os.path.join(pdir, "00-作品事实源.json")) or {}
    cast = [c for c in (facts.get("core_cast") or []) if isinstance(c, dict) and c.get("name")]
    protagonist = str(facts.get("protagonist") or "").strip()
    fr: dict = {"protagonist": protagonist, "cast": [c.get("name") for c in cast],
                "locations": facts.get("locations") or [], "title": facts.get("title"),
                "issues": []}
    if protagonist in ("主角", "主人公", "男主", "女主", ""):
        fr["issues"].append(f"protagonist 是占位词「{protagonist or '空'}」")
    if len(cast) < 2:
        fr["issues"].append(f"core_cast 只有 {len(cast)} 人（<2），冲突会退化")
    if not fr["locations"]:
        fr["issues"].append("facts.locations 为空（V5C13 会拦所有章卡）")
    plan_title = str(plan.get("novelName") or plan.get("title") or "").strip()
    fact_title = str(facts.get("title") or "").strip()
    if plan_title and fact_title and _norm_title(fact_title) != _norm_title(plan_title):
        fr["issues"].append(f"事实源书名「{fact_title}」≠ 计划书名「{plan_title}」"
                            "（V5F01 会拦整本书）")
    rep["facts"] = fr
    for x in fr["issues"]:
        rep["stall_reasons"].append("事实源：" + x)
    # 章卡闸门审计（真实判定走 story_kernel.card_lint，不复制逻辑）
    try:
        proj = w.Project(pdir, key)
        cards = _load_cards_raw(pdir)
        blocked = {}
        for c in chs:
            no = int(c.get("chapterNumber") or 0)
            if not no or c.get("status") == "completed":
                continue
            card = cards.get(no) or {}
            blocks = [x for x in v5.card_lint(proj, card, no, cards) if x.get("level") == "block"]
            if blocks:
                blocked[no] = [f"{x.get('code')}:{x.get('message')}" for x in blocks[:6]]
        rep["cards"] = {"total": len(cards), "blocked_chapters": len(blocked),
                        "sample": dict(list(blocked.items())[:3])}
        if blocked:
            rep["stall_reasons"].append(
                f"{len(blocked)} 张待写章卡被写前闸门拒收（V5C 系列硬字段缺失）")
    except Exception as e:
        rep["cards"] = {"error": str(e)}
    # 18-状态库一致性
    st = read_json(os.path.join(pdir, "18-状态库.json")) or {}
    metrics = st.get("metrics") or {}
    summaries = st.get("chapter_summaries") or {}
    issues = []
    if int(metrics.get("chapters") or 0) != done:
        issues.append(f"metrics.chapters={metrics.get('chapters')} ≠ 磁盘完成章 {done}")
    if int(metrics.get("words") or 0) != sum(int(c.get("wordCount") or 0) for c in chs):
        issues.append("metrics.words 与计划章节字数合计不一致")
    if done and len(summaries) < done:
        issues.append(f"chapter_summaries {len(summaries)} 条 < 完成章 {done}")
    rep["state"] = {"metrics": metrics, "summaries": len(summaries), "issues": issues}
    for x in issues:
        rep["stall_reasons"].append("状态库：" + x)
    if not rep["stall_reasons"]:
        rep["stall_reasons"] = []
        rep["verdict"] = "健康（未发现停摆卡点）"
    else:
        rep["verdict"] = "停摆（发现 " + str(len(rep["stall_reasons"])) + " 处卡点）"
    return rep


def _norm_title(s: str) -> str:
    import re
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(s or ""))


# ---------------------------------------------------------------- 修复

def repair_book(pdir: str) -> dict:
    """--apply：确定性修复（零 LLM、全部原子写）。返回修复动作清单。"""
    from bot import journal
    from bot import story_kernel as v5
    from bot import writer as w

    key = os.path.basename(pdir.rstrip("/"))
    actions: list[str] = []
    degraded: list[int] = []
    proj = w.Project(pdir, key)
    plan = proj.plan() or {}
    if not plan:
        return {"key": key, "actions": ["无 02-写作计划.json，跳过修复"], "degraded": []}

    # 1) 任务档案收口
    active = journal.current_task(pdir)
    if isinstance(active, dict) and active.get("status") == "running":
        journal.reconcile(pdir, note="停摆修复：残留 running 已转为可续跑")
        actions.append("任务档案 running→paused（journal.reconcile）")
    if isinstance(active, dict) and str(active.get("stage")) == "plan_blocked":
        journal.update(pdir, stage="", note="停摆修复：章卡已确定性修补，点「继续」直接开写")
        actions.append("任务档案 stage=plan_blocked 已清除")

    # 2) 计划 meta 残留清理
    meta = plan.setdefault("meta", {})
    popped = []
    for flag in ("plan_blocked", "batch_stopped", "budget_stopped", "planning_running"):
        if meta.pop(flag, None) is not None:
            popped.append(flag)
    if popped:
        actions.append("计划 meta 清除残留：" + "、".join(popped))
    chs = plan.get("chapters") or []
    pend = [c for c in chs if c.get("status") != "completed"]
    if pend and plan.get("status") not in ("completed", "planning", "awaiting_title") \
            and not meta.get("paused"):
        if plan.get("status") != "in_progress":
            plan["status"] = "in_progress"
            actions.append(f"计划 status {plan.get('status')} → in_progress（有待写章）")
    if popped or plan.get("status") == "in_progress":
        proj.save_plan(plan)

    # 3) 事实源修补（确定性）
    fr = w.repair_degenerate_facts(proj)
    if fr.get("fixed"):
        actions.append("事实源修补：" + "；".join(fr["fixed"]))
    facts = v5.load_facts(proj)

    # 4) 章卡修补（按闸门拒绝原因确定性补全；仍不过 → legacy 降级，记质量账本）
    cards = _load_cards_raw(pdir)
    repaired_cards = 0
    for c in chs:
        no = int(c.get("chapterNumber") or 0)
        if not no or c.get("status") == "completed":
            continue
        if w._gate_legacy_exempt(proj, no):
            continue   # 已登记 legacy 放行：幂等跳过（重复 --apply 不重复动作）
        card = cards.get(no) or {}
        blocks = w._gate_card_blocks(proj, card, no)
        if not blocks and not _is_placeholder_goal(card):
            continue
        filled = w._fill_card_hard_fields(proj, no, card, facts, blocks=blocks)
        blocks2 = w._gate_card_blocks(proj, filled, no)
        if blocks2:
            filled["v5_legacy_fallback"] = True
            filled["legacy_reason"] = "；".join(
                f"[{b.get('code')}] {b.get('message')}" for b in blocks2[:6])[:300]
            filled["repaired_by"] = "repair_tool+deterministic+legacy_fallback"
            w._save_repaired_card(proj, no, filled, "repair_tool_legacy")
            w._mark_gate_legacy(proj, no, filled["legacy_reason"])
            w._record_gate_fallback(proj, no, blocks2,
                                    note="repair_stalled_book：确定性修补后仍拒收，降级放行")
            degraded.append(no)
        else:
            filled["repaired_by"] = "repair_tool+deterministic"
            filled.pop("v5_legacy_fallback", None)
            w._save_repaired_card(proj, no, filled, "repair_tool")
        repaired_cards += 1
        actions.append(f"第{no}章章卡已修补"
                       + ("（仍拒收 → legacy 降级放行，已记质量账本）" if blocks2 else "（复检通过）"))
    if not repaired_cards:
        actions.append("章卡：全部可写（无待修补项；幂等跳过已修补/已放行章）")
    if degraded:
        actions.append(f"legacy 降级放行章：{degraded}（不静默，写后闸门照常追责）")

    # 5) 状态库 metrics 对账
    st_path = os.path.join(pdir, "18-状态库.json")
    st = read_json(st_path) or {}
    metrics = st.setdefault("metrics", {})
    done = sum(1 for c in chs if c.get("status") == "completed")
    words = sum(int(c.get("wordCount") or 0) for c in chs)
    if int(metrics.get("chapters") or 0) != done or int(metrics.get("words") or 0) != words:
        from bot.safeio import atomic_write_json
        metrics["chapters"], metrics["words"] = done, words
        st["updated_at"] = Now()
        atomic_write_json(st_path, st)
        actions.append(f"状态库 metrics 对账修正（chapters={done}, words={words}）")

    # 6) 续跑预检（模拟 writer.resume_book 的前置检查，不真写章）
    pre = resume_precheck(pdir)
    actions.append("续跑预检：" + ("可继续（" + pre["detail"] + "）" if pre["can_resume"]
                                  else "不可继续（" + pre["detail"] + "）"))

    report = {"key": key, "applied_at": Now(), "actions": actions,
              "degraded_chapters": degraded, "resume_precheck": pre}
    from bot.safeio import atomic_write_json
    atomic_write_json(os.path.join(pdir, "26-停摆修复报告.json"), report)
    actions.append("修复报告已写入 26-停摆修复报告.json")
    return report


def _is_placeholder_goal(card: dict) -> bool:
    g = str((card or {}).get("goal") or "")
    return (not g) or any(m in g for m in ("承接本弧", "承接「", "本章为弧内第", "本章推进本卷目标"))


def resume_precheck(pdir: str) -> dict:
    """模拟「继续」（writer.resume_book）的前置检查，不启动线程、不写正文。"""
    from bot import journal
    from bot import writer as w

    key = os.path.basename(pdir.rstrip("/"))
    proj = w.Project(pdir, key)
    plan = proj.plan()
    if not plan:
        return {"can_resume": False, "detail": "无计划文件"}
    if plan.get("status") == "completed":
        return {"can_resume": False, "detail": "已完本"}
    chs = plan.get("chapters") or []
    done = w.done_chapters(plan, proj.path)
    total = len(chs)
    if total and done >= total:
        return {"can_resume": False, "detail": f"全书正文已齐（{done}/{total}）"}
    if not total:
        return {"can_resume": False, "detail": "0 张章卡（需先重跑规划）"}
    task = journal.current_task(proj.path)
    nxt = next((int(c.get("chapterNumber") or 0) for c in chs
                if str(c.get("status")) != "completed"), 0)
    # start_writing 防重入预检：本工具进程内注册表天然为空；再确认自检无残留
    residue = w.registry_selfcheck(key, proj)
    running = bool(proj.running or w._book_running(key))
    if running:
        return {"can_resume": False, "detail": "注册表仍有活线程在写（不应发生在工具进程）"}
    return {"can_resume": True, "residue_cleaned": bool(residue),
            "detail": f"从第 {nxt} 章接着写（{done}/{total}）"
                      + (f"；任务档案断点 {task.get('stage') or 'writing'}" if task else "")}


# ---------------------------------------------------------------- 幻影目录

def plan_phantoms(root: str) -> dict:
    """盘点幻影目录：无任何文体计划文件且无完成章的目录（先合并后删除）。"""
    projects = os.path.join(root, "projects")
    out = {"delete": [], "keep_with_content": [], "keep_real": [], "merge_targets": {}}
    rename_map = read_json(os.path.join(root, "data", "rename_map.json")) or {}
    if not os.path.isdir(projects):
        return out
    for key in sorted(os.listdir(projects)):
        pdir = os.path.join(projects, key)
        if not os.path.isdir(pdir) or key.startswith("."):
            continue
        has_plan = any(os.path.exists(os.path.join(pdir, f)) for f in MEDIUM_PLAN_FILES)
        ch_files = [f for f in os.listdir(pdir) if f.endswith(".md") and f.startswith("第")]
        body_dirs = [d for d in ("正文", "chapters") if os.path.isdir(os.path.join(pdir, d))]
        if has_plan:
            out["keep_real"].append(key)
            continue
        target = rename_map.get(key)
        target_dir = os.path.join(projects, str(target)) if target else None
        if ch_files or body_dirs:
            out["keep_with_content"].append(
                {"key": key, "reason": "存在正文文件（绝不删）" + (f"，别名目标 {target}" if target else "")})
            continue
        if target and target_dir and os.path.isdir(target_dir):
            out["delete"].append(key)
            out["merge_targets"][key] = target
        else:
            out["keep_with_content"].append(
                {"key": key, "reason": "无计划文件也无正文，但不在 rename_map（无合并目标，报告不动）"})
    return out


def clean_phantoms(root: str, ph: dict | None = None) -> dict:
    """执行幻影清理：先合并聊天/事件历史到真书目录，再删除（仅 rename_map 源目录）。"""
    from bot import writer as w
    projects = os.path.join(root, "projects")
    ph = ph or plan_phantoms(root)
    result = {"merged": {}, "removed": [], "kept": [x if isinstance(x, str) else x.get("key")
                                                    for x in ph.get("keep_with_content", [])]}
    for old_key, new_key in ph.get("merge_targets", {}).items():
        old_dir = os.path.join(projects, old_key)
        new_dir = os.path.join(projects, new_key)
        if not os.path.isdir(old_dir) or not os.path.isdir(new_dir):
            continue
        merged = w._merge_ghost_history(old_dir, new_dir)
        result["merged"][old_key] = {"into": new_key, "files": merged}
        shutil.rmtree(old_dir, ignore_errors=True)
        if not os.path.isdir(old_dir):
            result["removed"].append(old_key)
    return result


# ---------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="停摆书体检/修复（默认只读，--apply 才写盘）")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="数据根目录（默认桌面端 YiMuLit）")
    ap.add_argument("--book", default="", help="书 key 或名称关键词；缺省=全部书")
    ap.add_argument("--apply", action="store_true", help="执行修复（写盘）")
    ap.add_argument("--clean-phantoms", action="store_true", help="随 --apply 清理幻影目录（先合并后删除）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    root = os.path.abspath(os.path.expanduser(args.root))
    projects = os.path.join(root, "projects")
    if not os.path.isdir(projects):
        print(f"❌ 项目目录不存在：{projects}")
        return 2

    # 导入 bot 前先把 DATA_ROOT 指到目标数据根（bot.paths 读 YIMU_DATA_DIR）
    os.environ["YIMU_DATA_DIR"] = root
    sys.path.insert(0, REPO_ROOT)

    lock = instance_lock_status(root)
    reports = {"root": root, "generated_at": Now(), "lock": lock,
               "books": [], "phantoms": None, "phantom_cleanup": None, "errors": []}
    wants_write = bool(args.apply or args.clean_phantoms)
    if wants_write and not lock["writable"]:
        print("⛔ " + lock["reason"])
        print("   按写盘纪律本次不执行任何写操作；体检结果如下（只读不受影响）。")

    keys = sorted(os.listdir(projects))
    if args.book:
        keys = [k for k in keys if args.book in k]
    for key in keys:
        pdir = os.path.join(projects, key)
        if not os.path.isdir(pdir) or key.startswith("."):
            continue
        try:
            rep = audit_book(pdir)
            if args.apply and lock["writable"] and not rep.get("phantom"):
                fix = repair_book(pdir)
                rep["repair"] = fix
            reports["books"].append(rep)
        except Exception as e:
            reports["errors"].append({"key": key, "error": f"{type(e).__name__}: {e}"})

    if args.clean_phantoms:
        try:
            ph = plan_phantoms(root)
            reports["phantoms"] = ph
            if args.apply and lock["writable"]:
                reports["phantom_cleanup"] = clean_phantoms(root, ph)
        except Exception as e:
            reports["errors"].append({"key": "-phantoms-", "error": f"{type(e).__name__}: {e}"})

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=1))
        return 0 if not reports["errors"] else 1

    # 人读输出
    print(f"== 停摆书体检/修复 · {root} · {reports['generated_at']} ==")
    print("锁检测：" + lock["reason"] + ("" if lock["writable"] else "（本次拒绝写盘）"))
    for rep in reports["books"]:
        print(f"\n——《{rep.get('name') or rep['key']}》（{rep['key']}）"
              + ("【幻影候选】" if rep.get("phantom") else ""))
        if rep.get("phantom"):
            print(f"  · 无计划文件；正文文件 {rep.get('chapter_files', 0)} 个；"
                  + ("可按先合并后删除清理" if not rep.get("chapter_files") else "有正文，绝不删"))
            continue
        p = rep.get("plan") or {}
        print(f"  · 计划：status={p.get('status')} 章 {p.get('done')}/{p.get('chapters')}｜"
              f"meta 残留：{json.dumps(p.get('meta_flags') or {}, ensure_ascii=False)}")
        j = (rep.get("journal") or {}).get("active")
        if j:
            print(f"  · 任务档案：{j.get('kind')}/{j.get('status')} stage={j.get('stage')} "
                  f"进度 {j.get('done')}/{j.get('total')}｜{j.get('note') or ''}")
        f = rep.get("facts") or {}
        if f.get("issues"):
            print("  · 事实源问题：" + "；".join(f["issues"]))
        c = rep.get("cards") or {}
        if c.get("blocked_chapters"):
            print(f"  · 章卡闸门拒收：{c['blocked_chapters']} 章；示例 {json.dumps(c.get('sample') or {}, ensure_ascii=False)[:300]}")
        s = rep.get("state") or {}
        if s.get("issues"):
            print("  · 状态库：" + "；".join(s["issues"]))
        print("  · 结论：" + str(rep.get("verdict")))
        for x in rep.get("stall_reasons") or []:
            print("    - " + x)
        if rep.get("repair"):
            print("  · 修复动作：")
            for a in rep["repair"].get("actions") or []:
                print("    ✅ " + a)
    if reports.get("phantoms"):
        ph = reports["phantoms"]
        print("\n== 幻影目录盘点 ==")
        for k in ph.get("delete") or []:
            print(f"  · 可清理（先合并后删除）：{k} → {ph['merge_targets'].get(k)}")
        for x in ph.get("keep_with_content") or []:
            print(f"  · 保留：{x.get('key')}（{x.get('reason')}）")
        print(f"  · 正常书：{len(ph.get('keep_real') or [])} 个")
        if reports.get("phantom_cleanup"):
            cl = reports["phantom_cleanup"]
            print("  清理结果：已删 " + json.dumps(cl.get("removed") or [], ensure_ascii=False)
                  + "；历史合并 " + json.dumps(cl.get("merged") or {}, ensure_ascii=False)[:300])
    if reports["errors"]:
        print("\n⚠️ 部分书体检失败：")
        for e in reports["errors"]:
            print(f"  · {e['key']}: {e['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
