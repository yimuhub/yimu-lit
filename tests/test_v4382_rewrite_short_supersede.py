"""v4.38.2：按字数重写、最新指令取代旧队列、暂停/继续接最新任务。"""
import json
import os
import threading
import time

import pytest

from bot import chief, journal, tasks, writer


class _Proj:
    def __init__(self, path, plan):
        self.path = path
        self._plan = plan
        self.lock = threading.RLock()
        self.logs = []

    def plan(self):
        return self._plan

    def save_plan(self, plan):
        self._plan = plan

    def log(self, msg, *_a, **_k):
        self.logs.append(msg)


def _write_ch(path, no, text):
    fp = os.path.join(path, f"{no:02d}.md")
    with open(fp, "w", encoding="utf-8") as f:
        f.write("# 第%d章\n\n%s\n" % (no, text))
    return f"{no:02d}.md"


def _plan_with(path, sizes):
    chs = []
    for no, n in sizes.items():
        fp = _write_ch(path, no, "字" * n)
        chs.append({"chapterNumber": no, "title": f"第{no}章", "filePath": fp,
                    "status": "completed"})
    return {"novelName": "测试书", "chapters": chs, "config": {}}


def test_short_chapters_uses_disk_words(tmp_path):
    plan = _plan_with(str(tmp_path), {1: 400, 2: 3200, 3: 100})
    proj = _Proj(str(tmp_path), plan)
    got = writer.short_chapters(proj, 3000)
    assert [x["ch"] for x in got] == [1, 3]
    assert got[0]["words"] < 1000


def test_expand_short_chapter_keeps_expansion(tmp_path, monkeypatch):
    plan = _plan_with(str(tmp_path), {1: 400})
    proj = _Proj(str(tmp_path), plan)
    monkeypatch.setattr(writer, "score_chapter", lambda _p, _n: 80)
    monkeypatch.setattr(writer, "_build_ctx", lambda _p, _c: {})
    monkeypatch.setattr(writer.workflow, "expand_chapter",
                        lambda body, ctx, min_words=3000, current_words=0:
                        "# 第1章\n\n" + "扩" * 3200 + "。")
    r = writer.expand_short_chapter(proj, 1, target_words=3000)
    assert r["kept"] is True
    assert r["after_words"] > r["before_words"]
    body = open(os.path.join(str(tmp_path), "01.md"), encoding="utf-8").read()
    assert "扩" in body


def test_expand_short_chapter_restores_on_score_drop(tmp_path, monkeypatch):
    plan = _plan_with(str(tmp_path), {1: 400})
    proj = _Proj(str(tmp_path), plan)
    scores = iter([90, 60])
    monkeypatch.setattr(writer, "score_chapter", lambda _p, _n: next(scores))
    monkeypatch.setattr(writer, "_build_ctx", lambda _p, _c: {})
    monkeypatch.setattr(writer.workflow, "expand_chapter",
                        lambda body, ctx, min_words=3000, current_words=0:
                        "# 第1章\n\n" + "扩" * 3200 + "。")
    r = writer.expand_short_chapter(proj, 1, target_words=3000)
    assert r["kept"] is False
    body = open(os.path.join(str(tmp_path), "01.md"), encoding="utf-8").read()
    assert "扩" not in body
    assert "字" in body


def test_start_task_superseded_job_cannot_finish_new_task(tmp_path, monkeypatch):
    key = "k"
    pdir = os.path.join(str(tmp_path), key)
    os.makedirs(pdir)
    proj = _Proj(pdir, {"novelName": "测试书", "chapters": []})
    monkeypatch.setattr(writer, "_project_dir", lambda _k: pdir)
    monkeypatch.setattr(writer, "Project", lambda _p, _k: proj)
    journal.begin(pdir, "rewrite", "旧任务", params={"chapters": [1, 2]}, total=2)
    ev1, ev2 = threading.Event(), threading.Event()

    def old_batch(_todo):
        ev1.set()
        ev2.wait(2)          # 等新任务登记后再收尾

    writer.start_task(key, "rewrite", "旧任务", [1, 2], batch=old_batch)
    assert ev1.wait(1)
    ev3 = threading.Event()

    def new_batch(_todo):
        ev3.set()
        time.sleep(0.4)

    r2 = writer.start_task(key, "rewrite", "新任务", [5], batch=new_batch)
    ev2.set()
    assert ev3.wait(1)
    time.sleep(0.1)
    # 旧 job 已收尾，但新 job 还在跑：活动任务必须仍然是新的
    cur = journal.current_task(pdir)
    assert cur is not None and cur["id"] == r2["task"]["id"]
    time.sleep(0.6)
    # 新任务跑完后正常结束；旧任务绝不能顶替或篡改它
    cur = journal.current_task(pdir)
    assert cur is None or cur["id"] == r2["task"]["id"]


def test_resume_rewrite_honors_expand_mode_and_clears_pause(tmp_path, monkeypatch):
    key = "k"
    pdir = os.path.join(str(tmp_path), key)
    os.makedirs(pdir)
    plan = {"novelName": "测试书", "chapters": [], "meta": {"paused": True},
            "status": "paused"}
    proj = _Proj(pdir, plan)
    monkeypatch.setattr(writer, "_project_dir", lambda _k: pdir)
    monkeypatch.setattr(writer, "Project", lambda _p, _k: proj)
    journal.begin(pdir, "rewrite", "补字", params={
        "mode": "expand_short", "below_words": 3000,
        "chapters": [1, 2], "done_chapters": [1]}, total=2)
    called = []
    done = threading.Event()

    def fake_expand(_key, chapters=None, below_words=3000, workers=2):
        called.append((list(chapters), below_words))
        journal.mark_chapter_done(pdir, 2, total=2, stage="rewriting")
        done.set()
        return {"ok": True}

    monkeypatch.setattr(writer, "run_short_expand", fake_expand)
    out = tasks.resume(key)
    assert out["ok"] is True
    assert "少于 3000 字" in out["text"]
    assert done.wait(2)
    assert called == [([2], 3000)]
    assert proj.plan()["meta"]["paused"] is False


def test_chief_exposes_rewrite_short_and_alias():
    assert "rewrite_short" in chief.ACTIONS
    assert chief._ACTION_ALIASES["rewrite_by_words"] == "rewrite_short"
    act = chief._norm_action({"action": "rewrite_by_words", "below_words": 2000})
    assert act["action"] == "rewrite_short"
    assert act["params"]["below_words"] == 2000
