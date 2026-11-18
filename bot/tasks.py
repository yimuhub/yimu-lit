"""长任务续跑注册表 —— 「继续」的唯一判定入口。

## 为什么要有这一层

「继续」的语义是固定的：**接着没做完的那件事往下做**，不重新开始。
难的不是语义，是「认任务」——系统必须先知道上次那件事**是什么类型**。

v4.22.2 的 `resume_task` 是一串 if/elif，只认识 writing / rewrite / score。
一旦被打断的是别的任务（按分数改写、补章卡、批量导出），它会掉到最后一行
`resume_book()` —— 用户想接着做 A，系统却开写新章。这不是「记不住」，是「认错人」。
认错人比忘事更糟：忘事只是没动静，认错人会毁掉正在做的事。

v4.23.0 把「认任务」改成一张注册表：任何长任务只要

    @runs("kind")
    def _my_runner(key, proj, task, left, batch_size=None): ...

就自动具备「中断后继续」。新增任务类型不用再改这里的 if/elif，
也就不会再出现「新任务忘了登记续跑」这类结构性漏洞。

## runner 的返回值约定

    None        → 这次没有可续的进度（例如章节列表已空），交回默认写作路径
    {"ok":…}    → 已接手，text 是一句给用户看的人话

## 纪律

runner 必须在后台线程里跑，立刻返回；不许在请求线程里同步干长活。
"""
from __future__ import annotations

import threading

from . import journal

# kind -> runner(key, proj, task, left, batch_size)
_RUNNERS: dict = {}


def runs(kind):
    """把一个函数登记成某类任务的续跑器。"""
    def _deco(fn):
        _RUNNERS[str(kind).strip().lower()] = fn
        return fn
    return _deco


def registered() -> list:
    """已登记可续跑的 kind（诊断/测试用）。"""
    return sorted(_RUNNERS)


def todo_chapters(task: dict) -> list:
    """从任务档案里取「还没做完的章号」。

    优先用 params.done_chapters 取差集——并行任务完成顺序是乱的，
    只按 done 计数切片会漏做/重做。没有这份记录时退回按计数切片（兼容旧档案）。
    """
    if not isinstance(task, dict):
        return []
    params = task.get("params") if isinstance(task.get("params"), dict) else {}
    todo = []
    for x in params.get("chapters") or []:
        try:
            todo.append(int(x))
        except (TypeError, ValueError):
            continue
    if not todo:
        return []
    done_set = set()
    for x in params.get("done_chapters") or []:
        try:
            done_set.add(int(x))
        except (TypeError, ValueError):
            continue
    if done_set:
        return [c for c in todo if c not in done_set]
    try:
        done = max(0, int(task.get("done") or 0))
    except (TypeError, ValueError):
        done = 0
    return todo[done:]


def _label(task: dict) -> str:
    kind = str((task or {}).get("kind") or "")
    return journal.KIND_LABEL.get(kind, kind or "任务")


def _write_default(key, proj, batch_size, why: str) -> dict:
    """默认路径：当这是一件写作任务（或没有任何档案）时，接着写书。"""
    from . import writer
    ok = writer.resume_book(key, batch_size=batch_size)
    text = writer._resume_text(key) if ok else (why or "没有可接着写的章节")
    return {"ok": bool(ok), "kind": "writing", "text": text}


def _bg(fn) -> None:
    threading.Thread(target=fn, daemon=True).start()


def resume(key, batch_size=None) -> dict:
    """「继续」的唯一入口：读档案 → 认 kind → 交给对应续跑器。

    返回 {"ok": bool, "kind": str, "text": 人话}。
    ok=False 时调用方应当把 text 原样告诉用户，**不要**偷偷改成从头开始。
    """
    from . import writer
    p = writer.Project(writer._project_dir(key), key)
    task = journal.current_task(p.path)
    if not isinstance(task, dict):
        return _write_default(key, p, batch_size, "没有未完成的任务")

    kind = str(task.get("kind") or "writing").strip().lower()
    if kind in ("", "writing"):
        return _write_default(key, p, batch_size, "")

    runner = _RUNNERS.get(kind)
    if runner is None:
        # 认不出这个任务：宁可如实说，也不要自作主张去写新章
        return {"ok": False, "kind": kind,
                "text": f"上次中断的是{_label(task)}「{task.get('title') or ''}」，"
                        f"但这一类任务还没有续跑器，需要重新发起一次"}

    left = todo_chapters(task)
    try:
        got = runner(key, p, task, left, batch_size)
    except Exception as e:
        return {"ok": False, "kind": kind, "text": f"{_label(task)}恢复失败：{str(e)[:100]}"}
    if isinstance(got, dict):
        return got
    return _write_default(key, p, batch_size, f"{_label(task)}已无剩余章节")


# ---------------------------------------------------------------- 各 kind 的续跑器


@runs("rewrite")
def _resume_rewrite(key, proj, task, left, batch_size=None):
    """批量重写：接着把剩下的章改完（并发数/轮数/目标分沿用上次的设定）。"""
    if not left:
        return None
    from . import writer
    # v4.24.16 修复：无「已在跑」检查——重写批次进行中再点一次「继续」，第二个
    # run_parallel_rewrite 会把第一批刚改好的章按旧快照再改一遍（白烧 token、可能把好稿改坏）。
    prog = getattr(writer, "_PARA_RW_PROGRESS", {}).get(key)
    if isinstance(prog, dict) and prog.get("running"):
        return {"ok": True, "kind": "rewrite",
                "text": "批量重写正在进行中，等它跑完即可（进度见右栏改写面板）"}
    params = task.get("params") if isinstance(task.get("params"), dict) else {}

    def _num(name, default):
        try:
            return int(params.get(name))
        except (TypeError, ValueError):
            return default

    workers = max(1, min(8, _num("workers", 3)))
    rounds = max(1, min(6, _num("max_rounds", 2)))
    target = params.get("target")
    try:
        target = int(target) if target is not None else None
    except (TypeError, ValueError):
        target = None

    def _job():
        try:
            journal.update(proj.path, status="running", stage="rewriting")
            writer.run_parallel_rewrite(key, chapters=left, workers=workers,
                                        max_rounds=rounds, target=target)
            journal.finish(proj.path, "done", note=f"重写完成 {len(left)} 章")
            journal.remember(proj.path, "system", f"批量重写续跑完成 {len(left)} 章")
        except Exception as e:
            journal.update(proj.path, status="paused", stage="interrupted",
                           note=f"重写中断：{str(e)[:80]}")
            journal.remember(proj.path, "system", f"批量重写再次中断：{str(e)[:100]}")

    _bg(_job)
    head = "、".join(str(x) for x in left[:6]) + ("…" if len(left) > 6 else "")
    return {"ok": True, "kind": "rewrite",
            "text": f"接着上次没做完的批量重写，还剩 {len(left)} 章（第 {head} 章）"}


@runs("score")
def _resume_score(key, proj, task, left, batch_size=None):
    """批量补评分：接着把剩下的章评完。"""
    if not left:
        return None
    from . import writer

    def _job():
        try:
            journal.update(proj.path, status="running", stage="scoring")
            total = len(left)
            base = int(task.get("done") or 0)
            for i, n in enumerate(left, 1):
                try:
                    writer.score_chapter(proj, n)
                except Exception as e:
                    proj.log(f"⚠️ 第{n}章评分失败（继续下一章）：{e}")
                journal.mark_chapter_done(proj.path, n, done=base + i,
                                          stage="scoring", total=total)
            journal.finish(proj.path, "done", note="补评分完成")
            journal.remember(proj.path, "system", f"补评分续跑完成 {total} 章")
        except Exception as e:
            journal.update(proj.path, status="paused", stage="interrupted",
                           note=f"补评分中断：{str(e)[:80]}")
            journal.remember(proj.path, "system", f"补评分再次中断：{str(e)[:100]}")

    _bg(_job)
    return {"ok": True, "kind": "score",
            "text": f"接着上次没做完的补评分，还剩 {len(left)} 章"}


@runs("plan")
def _resume_plan(key, proj, task, left, batch_size=None):
    """补章卡：把仍是模版话术的章卡逐章重新规划成真卡。

    这是长篇跑偏的根因之一——占位卡等于没有大纲，模型只能瞎写。
    中断后接着补，已补好的章节不会再动。
    """
    if not left:
        return None
    from . import longform

    def _job():
        try:
            journal.update(proj.path, status="running", stage="planning")
            cfg = (proj.plan() or {}).get("config") or {}
            total = len(left)
            base = int(task.get("done") or 0)
            filled = 0
            for i, n in enumerate(left, 1):
                try:
                    card = longform.plan_chapter_now(proj, n, cfg, reason="resume:补章卡")
                    if card:
                        filled += 1
                except Exception as e:
                    proj.log(f"⚠️ 第{n}章补章卡失败（继续下一章）：{e}")
                journal.mark_chapter_done(proj.path, n, done=base + i,
                                          stage="planning", total=total)
            journal.finish(proj.path, "done", note=f"补章卡完成 {filled}/{total} 章")
            journal.remember(proj.path, "system", f"补章卡续跑完成 {filled}/{total} 章")
        except Exception as e:
            journal.update(proj.path, status="paused", stage="interrupted",
                           note=f"补章卡中断：{str(e)[:80]}")

    _bg(_job)
    return {"ok": True, "kind": "plan",
            "text": f"接着上次没做完的补章卡，还剩 {len(left)} 章"}


@runs("export")
def _resume_export(key, proj, task, left, batch_size=None):
    """导出是秒级任务，被打断说明进程重启了——直接说清楚，不必假装能续。"""
    return {"ok": False, "kind": "export",
            "text": "上次的导出被中断了（导出是秒级操作），重新点一次「导出」即可"}
