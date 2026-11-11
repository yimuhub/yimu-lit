"""v4.26.0：总编辑 Agent 回归。

覆盖：停滞处理（v4.34.0 起不再自动恢复，一律停止等指令）、用户暂停绝不自动恢复、
每章闸门（空章打回/未评分派评/低分派重写）、完稿终审、
选书名停摆修复（采用→开写+确认回复）、自动化程度设置。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from fastapi.testclient import TestClient

import app as appmod
from bot import security, supervisor, writer


KEY = "20260101-000000-监工测试书"


def _mk_novel(tmp_path, status="in_progress", paused=False, done=3, total=5,
              scores=None, words=3000):
    pdir = tmp_path / KEY
    pdir.mkdir(parents=True, exist_ok=True)
    chapters = []
    for i in range(1, total + 1):
        completed = i <= done
        ch = {"chapterNumber": i, "title": f"第{i}章",
              "status": "completed" if completed else "pending",
              "wordCount": words if completed else None}
        fn = f"第{i:02d}章-x.md"
        ch["filePath"] = fn
        chapters.append(ch)
        if completed:
            (pdir / fn).write_text("正文" * (words // 2 or 1), encoding="utf-8")
    plan = {"novelName": "监工测试书", "status": status, "totalChapters": total,
            "minWordsPerChapter": 3000,
            "chapters": chapters,
            "config": {"batch_size": "1"},
            "meta": {"paused": paused}}
    if scores:
        plan.setdefault("meta", {})["chapter_scores"] = scores
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return writer.Project(str(pdir), KEY)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    writer._RUNNING_BOOKS.clear()
    yield
    supervisor.stop(KEY)
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    writer._RUNNING_BOOKS.clear()


# ---------- 停滞处理（v4.34.0 状态规则：不再自动恢复） ----------

def test_stall_never_auto_resume(monkeypatch, tmp_path):
    """v4.34.0：创作停滞时一律停止——只有用户明确操作指令（点「继续」等）
    才重新进入创作中，看护线程不得自行拉起写作。"""
    proj = _mk_novel(tmp_path, done=3)
    monkeypatch.setattr(appmod, "_get_project", lambda k: proj if k == KEY else None)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(writer, "pipeline_running", lambda k: False)
    called = {}
    monkeypatch.setattr(supervisor, "_dispatch_resume",
                        lambda k, med, pdir: called.setdefault("resumed", True) or True)
    r = supervisor.tick_once(KEY)
    assert r["action"] == "stalled"
    assert "resumed" not in called


def test_stall_stops_watchdog(monkeypatch, tmp_path):
    """v4.34.0：停滞上报后看护线程退出（stop 被调用），后续 tick 不再动作。"""
    proj = _mk_novel(tmp_path, done=3)
    monkeypatch.setattr(appmod, "_get_project", lambda k: proj if k == KEY else None)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(writer, "pipeline_running", lambda k: False)
    monkeypatch.setattr(supervisor, "_dispatch_resume", lambda *a: True)
    stopped = {}
    monkeypatch.setattr(supervisor, "stop", lambda k: stopped.setdefault(k, True))
    r = supervisor.tick_once(KEY)
    assert r["action"] == "stalled"
    assert stopped.get(KEY)


def test_user_paused_never_auto_resume(monkeypatch, tmp_path):
    proj = _mk_novel(tmp_path, done=3, paused=True)
    monkeypatch.setattr(supervisor, "_load", lambda k: ("novel", str(proj.path), os.path.join(str(proj.path), "02-写作计划.json"), proj.plan()))
    called = {}
    monkeypatch.setattr(supervisor, "_dispatch_resume", lambda *a: called.setdefault("x", True) or True)
    r = supervisor.tick_once(KEY)
    assert r["action"] == "standby"
    assert "x" not in called


# ---------- 每章闸门 ----------

def test_completed_empty_chapter_gets_reset(monkeypatch, tmp_path):
    proj = _mk_novel(tmp_path, done=3, words=3000)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    # 第 3 章"完成"但只有 20 字（空章）
    ch3 = proj.plan()["chapters"][2]
    with open(os.path.join(str(proj.path), ch3["filePath"]), "w", encoding="utf-8") as f:
        f.write("短")
    monkeypatch.setattr(supervisor, "_load", lambda k: ("novel", str(proj.path), os.path.join(str(proj.path), "02-写作计划.json"), proj.plan()))
    monkeypatch.setattr(writer, "pipeline_running", lambda k: True)
    r = supervisor.tick_once(KEY)
    assert r["action"] == "checked"
    plan = json.load(open(os.path.join(str(proj.path), "02-写作计划.json"), encoding="utf-8"))
    assert plan["chapters"][2]["status"] == "pending"   # 被打回待写


def test_unscored_chapter_dispatches_score(monkeypatch, tmp_path):
    proj = _mk_novel(tmp_path, done=3)
    monkeypatch.setattr(supervisor, "_load", lambda k: ("novel", str(proj.path), os.path.join(str(proj.path), "02-写作计划.json"), proj.plan()))
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(writer, "pipeline_running", lambda k: True)
    monkeypatch.setattr(writer, "per_chapter_scores", lambda proj: [])
    called = {}
    monkeypatch.setattr(supervisor, "_dispatch_score",
                        lambda k, med, pdir, no: called.setdefault("no", no))
    supervisor.tick_once(KEY)
    assert called.get("no") == 3   # 最近完成章被派补评


def test_low_score_chapter_dispatches_rewrite(monkeypatch, tmp_path):
    proj = _mk_novel(tmp_path, done=3)
    monkeypatch.setattr(supervisor, "_load", lambda k: ("novel", str(proj.path), os.path.join(str(proj.path), "02-写作计划.json"), proj.plan()))
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(writer, "pipeline_running", lambda k: True)
    monkeypatch.setattr(writer, "per_chapter_scores",
                        lambda proj: [{"ch": 3, "score": 62, "weak": ["结构"]}])
    called = {}
    monkeypatch.setattr(supervisor, "_dispatch_rewrite",
                        lambda k, med, pdir, chs: called.setdefault("chs", chs) or True)
    supervisor.tick_once(KEY)
    assert called.get("chs") == [3]


# ---------- 完稿终审 ----------

def test_final_acceptance_on_completed(monkeypatch, tmp_path):
    proj = _mk_novel(tmp_path, status="completed", done=5)
    monkeypatch.setattr(supervisor, "_load", lambda k: ("novel", str(proj.path), os.path.join(str(proj.path), "02-写作计划.json"), proj.plan()))
    monkeypatch.setattr(writer, "per_chapter_scores",
                        lambda proj: [{"ch": i, "score": 85} for i in range(1, 6)])
    events = []
    monkeypatch.setattr(supervisor, "_evt",
                        lambda k, phase, title, output="", chapter=0, thinking="": events.append(title))
    r = supervisor.tick_once(KEY)
    assert r["action"] == "final_acceptance"
    assert any("成品已就绪" in t for t in events)


# ---------- 选书名停摆修复 ----------

def test_title_set_on_awaiting_book_starts_writing(monkeypatch, tmp_path):
    proj = _mk_novel(tmp_path, status="awaiting_title", done=0, total=3)
    _pl = proj.plan()
    _pl.setdefault("meta", {})["awaiting_title"] = True
    with open(os.path.join(str(proj.path), "02-写作计划.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(_pl, ensure_ascii=False))
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    appmod.PROJECT_INDEX[KEY] = proj
    monkeypatch.setattr(appmod, "_get_project", lambda k: proj if k == KEY else None)
    monkeypatch.setattr(appmod.writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(writer, "start_writing", lambda p, batch_size=None: True)
    bubbles = []

    def _fake_bubble(p, role, text, meta=None):
        bubbles.append(text)

    import bot.v2api as v2
    monkeypatch.setattr(v2, "_save_chat_msg", _fake_bubble)
    # start_writing 被 mock，_apply_title 内部用 writer.start_writing（app 侧 import writer）——同模块对象
    client = TestClient(appmod.app)
    r = client.post("/api/title/set", json={"key": KEY, "title": "监工测试书·正名"})
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is True
    assert any("已收到" in b and "马上安排创作" in b for b in bubbles)
    plan = json.load(open(os.path.join(str(proj.path), "02-写作计划.json"), encoding="utf-8"))
    assert plan["meta"]["awaiting_title"] is False
    assert plan["status"] == "in_progress"


# ---------- 设置：自动化程度 ----------

def test_settings_supervisor_autonomy_roundtrip():
    monkey = pytest.MonkeyPatch()
    try:
        client = TestClient(appmod.app)
        r = client.post("/api/settings", json={"supervisor_autonomy": "auto"})
        assert r.status_code == 200
        assert r.json()["supervisor_autonomy"] == "auto"
        bad = client.post("/api/settings", json={"supervisor_autonomy": "wtf"})
        assert bad.status_code == 400
        r2 = client.post("/api/settings", json={"supervisor_autonomy": "guarded"})
        assert r2.json()["supervisor_autonomy"] == "guarded"
    finally:
        monkey.undo()
