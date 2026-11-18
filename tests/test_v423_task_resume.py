"""v4.23.0 通用任务续跑：任何长任务被打断，说「继续」都能接着做。

这一版要守的三条铁律：

1. **认得出人**：任务档案里记的是哪一类任务，「继续」就必须交回给同一类续跑器。
   认不出时要如实说不认识，**绝不能**退回去写新章——那是把用户正在做的事打断。
2. **不重做已完成**：并行重写是乱序完成的，「已完成 3 章」不等于「前 3 章」。
   必须按章号记，续跑取差集。
3. **僵尸任务能被救**：进程被杀时来不及写「已暂停」，档案会停在 running。
   启动对账要把它改标成可续跑，否则用户点一百次「继续」也没反应。
"""
import json
import time

import pytest

import app as appmod
from bot import journal, tasks, writer

KEY = "book"


def _wait(pred, timeout=5.0):
    """等一个后台线程的结果（不 sleep 死等，按谓词轮询）。"""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _proj(tmp_path, monkeypatch, total=4, done=0):
    pdir = tmp_path / KEY
    pdir.mkdir()
    chapters = []
    for n in range(1, total + 1):
        fp = f"第{n:03d}章.md"
        if n <= done:
            (pdir / fp).write_text(f"# 第{n:02d}章 标题{n}\n\n## 正文\n他关掉收音机。\n",
                                   encoding="utf-8")
        chapters.append({"chapterNumber": n, "title": f"标题{n}",
                         "status": "completed" if n <= done else "pending",
                         "wordCount": 12 if n <= done else 0, "filePath": fp})
    json.dump({"novelName": "测试书", "status": "in_progress", "chapters": chapters,
               "totalChapters": total, "meta": {}, "config": {}},
              open(pdir / "02-写作计划.json", "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    return pdir


# ---------------------------------------------------------------- 注册表

def test_all_long_task_kinds_have_runner():
    """写书/重写/评分/补章卡都必须有续跑器——少一个就有一类任务「继续」不了。"""
    got = set(tasks.registered())
    for kind in ("writing", "rewrite", "score", "plan"):
        if kind == "writing":
            continue      # 写作走默认路径，不需要登记
        assert kind in got, f"{kind} 没有续跑器"


def test_todo_chapters_uses_done_chapters_not_count():
    """并行完成是乱序的：第 7 章可能比第 5 章先好。

    只按 done 计数切片会把第 5 章当成已完成而漏做，又把第 6 章重做一遍。
    """
    task = {"params": {"chapters": [5, 6, 7], "done_chapters": [7]}, "done": 1}
    assert tasks.todo_chapters(task) == [5, 6]


def test_todo_chapters_falls_back_to_count_for_old_journal():
    """老档案没有 done_chapters，退回按计数切片，不能直接返回空。"""
    task = {"params": {"chapters": [5, 6, 7]}, "done": 1}
    assert tasks.todo_chapters(task) == [6, 7]


def test_todo_chapters_empty_when_all_done():
    task = {"params": {"chapters": [5, 6], "done_chapters": [5, 6]}, "done": 2}
    assert tasks.todo_chapters(task) == []


# ---------------------------------------------------------------- 认任务

def test_unknown_kind_does_not_silently_start_writing(tmp_path, monkeypatch):
    """认不出的任务类型：如实说不认识，不许转头去写新章。"""
    pdir = _proj(tmp_path, monkeypatch, total=3, done=0)
    journal.begin(str(pdir), "future_kind", "某件还没实现续跑的事", params={}, total=3)
    r = tasks.resume(KEY)
    assert r["ok"] is False
    assert r["kind"] == "future_kind"
    assert "还没有续跑器" in r["text"]


def test_resume_dispatches_score_runner(tmp_path, monkeypatch):
    """档案里是 score，「继续」就必须接着评分，不能开写作。"""
    pdir = _proj(tmp_path, monkeypatch, total=3, done=3)
    journal.begin(str(pdir), "score", "补评分", params={"chapters": [1, 2, 3]}, total=3)
    journal.mark_chapter_done(str(pdir), 1)
    called = []
    monkeypatch.setattr(writer, "score_chapter", lambda p, n: called.append(n))
    monkeypatch.setattr(writer, "resume_book", lambda *a, **k: pytest.fail("不该去写新章"))
    r = tasks.resume(KEY)
    assert r["ok"] is True and r["kind"] == "score"
    assert _wait(lambda: sorted(called) == [2, 3])


def test_resume_rewrite_honours_saved_workers(tmp_path, monkeypatch):
    """续跑要沿用上次的并发/轮数设定，不能悄悄改回默认值。"""
    pdir = _proj(tmp_path, monkeypatch, total=3, done=3)
    journal.begin(str(pdir), "rewrite", "重写", params={"chapters": [1, 2, 3],
                                                       "workers": 5, "max_rounds": 3},
                  total=3)
    journal.mark_chapter_done(str(pdir), 1)
    seen = {}

    def _fake(key, chapters=None, workers=3, max_rounds=1, target=None):
        seen.update({"chapters": chapters, "workers": workers, "rounds": max_rounds})
        return {"ok": True, "results": []}

    monkeypatch.setattr(writer, "run_parallel_rewrite", _fake)
    r = tasks.resume(KEY)
    assert r["ok"] is True and r["kind"] == "rewrite"
    assert _wait(lambda: seen.get("chapters") == [2, 3])
    assert seen["workers"] == 5 and seen["rounds"] == 3


# ---------------------------------------------------------------- 记账

def test_mark_chapter_done_accumulates_by_chapter_no(tmp_path, monkeypatch):
    pdir = _proj(tmp_path, monkeypatch)
    journal.begin(str(pdir), "rewrite", "重写", params={"chapters": [3, 1, 2]}, total=3)
    journal.mark_chapter_done(str(pdir), 3)
    journal.mark_chapter_done(str(pdir), 1)
    journal.mark_chapter_done(str(pdir), 3)          # 重复标记不该翻倍
    t = journal.current_task(str(pdir))
    assert (t["params"]["done_chapters"]) == [3, 1]
    assert t["done"] == 2


def test_start_task_per_chapter_registers_progress_and_finishes(tmp_path, monkeypatch):
    pdir = _proj(tmp_path, monkeypatch, total=3, done=3)
    done = []
    writer.start_task(KEY, "score", "补评分", [1, 2, 3],
                      per_chapter=lambda n, i: done.append(n))
    assert _wait(lambda: sorted(done) == [1, 2, 3])
    assert _wait(lambda: journal.current_task(str(pdir)) is None)      # 已收尾
    hist = journal.last_finished(str(pdir), kinds=("score",))
    assert hist and hist["status"] == "done" and hist["done"] == 3


def test_start_task_failure_leaves_rest_for_resume(tmp_path, monkeypatch):
    """失败的章不许记成 done——否则「继续」不会再试，那章就永远是烂的。"""
    pdir = _proj(tmp_path, monkeypatch, total=2, done=2)

    def _boom(n, i):
        raise RuntimeError("上游 502")

    writer.start_task(KEY, "score", "补评分", [1, 2], per_chapter=_boom)
    assert _wait(lambda: (journal.current_task(str(pdir)) or {}).get("status") == "paused")
    t = journal.current_task(str(pdir))
    assert t["stage"] == "partial"
    assert "没做完" in t["note"]
    assert tasks.todo_chapters(t) == [1, 2]      # 两章都还在待办里


def test_partial_task_only_retries_the_failed_chapters(tmp_path, monkeypatch):
    """第 2 章挂了、第 1 章成了 —— 继续时只该重试第 2 章。"""
    pdir = _proj(tmp_path, monkeypatch, total=2, done=2)

    def _half(n, i):
        if n == 2:
            raise RuntimeError("上游 502")

    writer.start_task(KEY, "score", "补评分", [1, 2], per_chapter=_half)
    assert _wait(lambda: (journal.current_task(str(pdir)) or {}).get("status") == "paused")
    assert tasks.todo_chapters(journal.current_task(str(pdir))) == [2]


def test_same_chapter_gives_up_after_three_failures(tmp_path, monkeypatch):
    """同一章连续三次失败就跳过它，否则这本书永远收不了尾。"""
    pdir = _proj(tmp_path, monkeypatch, total=1, done=1)

    def _boom(n, i):
        raise RuntimeError("文件坏了")

    writer.start_task(KEY, "score", "补评分", [1], per_chapter=_boom)
    assert _wait(lambda: (journal.current_task(str(pdir)) or {}).get("status") == "paused")
    # 模拟两次「继续」都再挂一次
    for _ in range(2):
        writer.start_task(KEY, "score", "补评分", [1], per_chapter=_boom)
        time.sleep(0.05)
    assert _wait(lambda: journal.current_task(str(pdir)) is None)
    hist = journal.last_finished(str(pdir), kinds=("score",))
    assert hist and hist["status"] == "done"


def test_start_task_batch_path(tmp_path, monkeypatch):
    pdir = _proj(tmp_path, monkeypatch, total=2, done=2)
    got = []
    writer.start_task(KEY, "rewrite", "重写", [7, 8], batch=lambda todo: got.extend(todo))
    assert _wait(lambda: got == [7, 8])
    assert _wait(lambda: journal.current_task(str(pdir)) is None)


# ---------------------------------------------------------------- 僵尸任务

def test_reconcile_turns_zombie_running_into_paused(tmp_path, monkeypatch):
    """进程被杀时来不及写 paused——启动对账必须把 running 改标，否则永远「在跑」。"""
    pdir = _proj(tmp_path, monkeypatch)
    journal.begin(str(pdir), "score", "补评分", params={"chapters": [1, 2]}, total=2)
    assert journal.current_task(str(pdir))["status"] == "running"
    assert journal.reconcile(str(pdir)) is True
    t = journal.current_task(str(pdir))
    assert t["status"] == "paused" and "中断" in t["note"]


def test_reconcile_leaves_finished_task_alone(tmp_path, monkeypatch):
    pdir = _proj(tmp_path, monkeypatch)
    journal.begin(str(pdir), "score", "补评分", params={"chapters": [1]}, total=1)
    journal.finish(str(pdir), "done")
    assert journal.reconcile(str(pdir)) is False


def test_reconcile_all_counts(tmp_path, monkeypatch):
    dirs = []
    for name in ("a", "b"):
        d = tmp_path / name
        d.mkdir()
        journal.begin(str(d), "score", "补评分", params={"chapters": [1]}, total=1)
        dirs.append(str(d))
    assert journal.reconcile_all(dirs) == 2
    assert journal.reconcile_all(dirs) == 0      # 幂等


# ---------------------------------------------------------------- 接口层

def test_resume_endpoint_reports_kind(tmp_path, monkeypatch):
    """「继续」的回复要告诉用户接的是哪一类任务。"""
    from fastapi.testclient import TestClient
    pdir = _proj(tmp_path, monkeypatch, total=3, done=3)
    journal.begin(str(pdir), "score", "补评分", params={"chapters": [1]}, total=1)
    monkeypatch.setattr(writer, "score_chapter", lambda p, n: None)
    c = TestClient(appmod.app)
    j = c.post("/api/resume", json={"key": KEY}).json()
    assert j.get("kind") == "score"
    assert "补评分" in (j.get("msg") or "")


def test_rewrite_low_score_registers_journal(tmp_path, monkeypatch):
    """按分数改写以前漏登记 —— 「继续」会认不出它去写新章。"""
    _proj(tmp_path, monkeypatch, total=3, done=3)
    monkeypatch.setattr(writer, "per_chapter_scores",
                        lambda p: [{"ch": 2, "score": 50}])
    monkeypatch.setattr(writer, "_pass_line", lambda: 80)
    monkeypatch.setattr(writer, "run_parallel_rewrite",
                        lambda *a, **k: {"ok": True, "results": []})
    from fastapi.testclient import TestClient
    c = TestClient(appmod.app)
    c.post("/api/chat", json={"message": "把低于65分的章节全部重写，目标80分", "key": KEY})
    pdir = tmp_path / KEY
    assert _wait(lambda: journal.last_finished(str(pdir), kinds=("rewrite",)) is not None,
                 timeout=8)
