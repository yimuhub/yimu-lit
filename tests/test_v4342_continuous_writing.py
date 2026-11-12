"""v4.34.2：「让一直写」两条链路回归。

实拍现场（《复仇太妃：改写贞观》，2026-11-12）：
  用户说「写完全部」「继续写全部」，界面回「（全章批量模式）」，但每批只写 1 章就停；
  停下之后创作状态仍显示「创作中」，点继续也没反应——两件事各有独立根因：

  ① 规划器把话听对了（steps=['start_writing_all']），执行时却没把 all 传下去：
     resume_book 的 batch_size 是 None → 回落到 plan.config.batch_size（开书时是 1）。
     文案说「全章批量」，行为是「按批 1 章」，正好相反。
  ② /api/continue 先 livejob.begin() 登记作业，写作线程退出时只清 running /
     mark_pipeline，从不 livejob.finish()。前端判 running 的条件是
     `data.running || (job && !job.finished)` → 已停的书继续显示「创作中」，
     直到 30 分钟活性超时才自愈。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import livejob, planner, security, writer


KEY = "20260101-000000-v4342连续写"


def _mk_novel(tmp_path, status="in_progress", done=0, total=3):
    pdir = tmp_path / KEY
    pdir.mkdir(parents=True, exist_ok=True)
    chapters = []
    for i in range(1, total + 1):
        completed = i <= done
        fn = f"第{i:02d}章-x.md"
        chapters.append({"chapterNumber": i, "title": f"第{i}章", "filePath": fn,
                         "status": "completed" if completed else "pending",
                         "wordCount": 3000 if completed else None})
        if completed:
            (pdir / fn).write_text("正文" * 1500, encoding="utf-8")
    plan = {"novelName": "v4342连续写", "status": status, "totalChapters": total,
            "minWordsPerChapter": 3000, "chapters": chapters,
            "config": {"batch_size": "1", "mode": "rolling"}, "meta": {}}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return writer.Project(str(pdir), KEY)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    writer._RUNNING_BOOKS.clear()
    livejob.reset_for_tests()
    yield
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    writer._RUNNING_BOOKS.clear()
    livejob.reset_for_tests()


def _session():
    return {"project": KEY, "session": "s-v4342", "title_candidates": None}


def _force_plan(monkeypatch, action):
    """让规划器只吐一步指定动作——绕开模型，专测「听懂之后有没有照做」。"""
    monkeypatch.setattr(
        planner, "plan_actions",
        lambda msg, ctx: {"understanding": "测试", "steps": [
            {"action": action, "title": action, "params": {}, "why": "测试"}]})


# ---------------------------------------------------------------- ① 写完全部

def test_start_writing_all_really_passes_all(monkeypatch, tmp_path):
    """「写完全部」必须把 batch_size='all' 传到写作循环，不能只改文案。"""
    proj = _mk_novel(tmp_path, done=0, total=3)
    called = {}

    def _fake_resume(key, batch_size=None):
        called["key"], called["batch"] = key, batch_size
        return True

    monkeypatch.setattr(writer, "resume_book", _fake_resume)
    _force_plan(monkeypatch, "start_writing_all")

    out = appmod._chat_attached(_session(), proj, "写完全部")

    assert called.get("batch") == "all", \
        f"start_writing_all 仍是按批停：batch_size={called.get('batch')!r}"
    assert "全章批量模式" in out


def test_resume_writing_still_honours_plan_config(monkeypatch, tmp_path):
    """普通「继续」不许被顺手改成全部写完——开书设的每批章数仍要生效。"""
    proj = _mk_novel(tmp_path, done=1, total=3)
    called = {}
    monkeypatch.setattr(
        writer, "resume_book",
        lambda key, batch_size=None: called.update(batch=batch_size) or True)
    _force_plan(monkeypatch, "resume_writing")

    out = appmod._chat_attached(_session(), proj, "继续写")

    assert called.get("batch") is None   # None = 交给 plan.config.batch_size
    assert "本批写 1 章后暂停" in out


# ---------------------------------------------------------------- ② 停止即收口

def _stub_write_loop(monkeypatch):
    monkeypatch.setattr(writer, "_maybe_replan", lambda p, n: None)
    monkeypatch.setattr(writer, "_ensure_card_planned", lambda p, n, tries=2: True)
    monkeypatch.setattr(writer, "_write_chapter", lambda p, ch, mw, mr: None)
    monkeypatch.setattr(writer, "_validate_and_finish", lambda p: None)
    monkeypatch.setattr(writer, "_skeleton_refresh", lambda k: None)
    monkeypatch.setattr(writer, "_ensure_layered_plan", lambda p: None)


def test_write_loop_finishes_livejob_on_batch_stop(monkeypatch, tmp_path):
    """按批停下 = 线程真的结束 → 实时作业登记必须收口，否则界面一直「创作中」。"""
    proj = _mk_novel(tmp_path, done=0, total=3)
    _stub_write_loop(monkeypatch)
    livejob.begin(KEY, kind="resume", detail="正在继续写作…")
    assert livejob.is_active(KEY)

    writer._write_loop(proj, batch_size="1")

    row = livejob.get(KEY)
    assert row, "登记行不该被抹掉——要留结论给前端"
    assert row["finished"] is True, "写线程已退出，登记却还是 running"
    assert row["ok"] is True
    assert "本批已写 1 章" in row["detail"]
    assert livejob.is_active(KEY) is False


def test_write_loop_finishes_livejob_on_exception(monkeypatch, tmp_path):
    """异常中断也要收口，并把 ok=False 的结论留给用户。"""
    proj = _mk_novel(tmp_path, done=0, total=2)
    _stub_write_loop(monkeypatch)

    def _boom(p, ch, mw, mr):
        raise RuntimeError("上游 500")

    monkeypatch.setattr(writer, "_write_chapter", _boom)
    livejob.begin(KEY, kind="resume", detail="正在继续写作…")

    writer._write_loop(proj, batch_size="all")

    row = livejob.get(KEY)
    assert row["finished"] is True and row["ok"] is False
    assert "异常中断" in row["detail"]


# ---------------------------------------------------------------- ③ 状态接口口径

def test_book_live_reports_not_running_after_stop(monkeypatch, tmp_path):
    """登记收口后 /api/book-live 必须回 running=false（前端据此显示「待继续」）。"""
    proj = _mk_novel(tmp_path, done=1, total=3)
    _stub_write_loop(monkeypatch)
    appmod.PROJECT_INDEX[KEY] = proj
    livejob.begin(KEY, kind="resume", detail="正在继续写作…")
    writer._write_loop(proj, batch_size="1")

    c = TestClient(appmod.app)
    r = c.get("/api/book-live", params={"key": KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert (body.get("job") or {}).get("finished") is True
