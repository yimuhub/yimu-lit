"""v4.34.0：状态规则与生成参数回归。

覆盖：出书（整本导出）停止创作、成书账面归位、正文已齐不重复启动、
livejob 泄漏行活性超时、向导 AI 生成不再丢失题材/风格。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import livejob, security, writer


KEY = "20260101-000000-v4340测试书"


def _mk_novel(tmp_path, status="in_progress", done=3, total=5):
    pdir = tmp_path / KEY
    pdir.mkdir(parents=True, exist_ok=True)
    chapters = []
    for i in range(1, total + 1):
        completed = i <= done
        ch = {"chapterNumber": i, "title": f"第{i}章",
              "status": "completed" if completed else "pending",
              "wordCount": 3000 if completed else None}
        fn = f"第{i:02d}章-x.md"
        ch["filePath"] = fn
        chapters.append(ch)
        if completed:
            (pdir / fn).write_text("正文" * 1500, encoding="utf-8")
    plan = {"novelName": "v4340测试书", "status": status, "totalChapters": total,
            "minWordsPerChapter": 3000, "chapters": chapters,
            "config": {"batch_size": "1"}, "meta": {}}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return writer.Project(str(pdir), KEY)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    writer._RUNNING_BOOKS.clear()
    livejob.reset_for_tests()
    yield
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    writer._RUNNING_BOOKS.clear()
    livejob.reset_for_tests()


# ---------- stop_creation：出书即停止 ----------

def test_stop_creation_marks_completed_when_body_full(monkeypatch, tmp_path):
    proj = _mk_novel(tmp_path, status="in_progress", done=5, total=5)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    r = writer.stop_creation(KEY, reason="测试出书")
    assert r["stopped"] is True
    assert r["state"] == "completed"
    plan = json.load(open(os.path.join(str(proj.path), "02-写作计划.json"), encoding="utf-8"))
    assert plan["status"] == "completed"
    assert plan["meta"]["paused"] is True


def test_stop_creation_pauses_when_body_partial(monkeypatch, tmp_path):
    proj = _mk_novel(tmp_path, status="in_progress", done=3, total=5)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    r = writer.stop_creation(KEY, reason="测试出书")
    assert r["stopped"] is True
    assert r["state"] == "paused"


def test_full_book_export_stops_creation(monkeypatch, tmp_path):
    """整本导出（frm=to=0）→ _export_settle 触发 stop_creation；范围导出不触发。"""
    proj = _mk_novel(tmp_path, status="in_progress", done=5, total=5)
    monkeypatch.setattr(appmod, "_get_project", lambda k: proj if k == KEY else None)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(writer, "stop_creation",
                        lambda k, reason="": called.append((k, reason)) or {"stopped": True})
    client = TestClient(appmod.app)
    # 整本导出 md（无正文报错风险：5 章正文已齐）
    resp = client.get(f"/api/export?key={KEY}&fmt=md")
    assert resp.status_code == 200
    assert called and called[0][0] == KEY
    # 章节范围导出不得触发出书收口
    called.clear()
    resp = client.get(f"/api/export?key={KEY}&fmt=md&frm=1&to=2")
    assert resp.status_code == 200
    assert called == []


# ---------- 正文已齐不重复启动 ----------

def test_resume_book_refuses_when_body_full(monkeypatch, tmp_path):
    """100% 已完成的书（哪怕账面状态还是 in_progress）点「继续」不得启动写作线程，
    且账面归位 completed——「已完成仍显示创作中」的根治点之一。"""
    proj = _mk_novel(tmp_path, status="in_progress", done=5, total=5)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    started = {}
    monkeypatch.setattr(writer, "start_writing", lambda *a, **kw: started.setdefault("x", True) or True)
    ok = writer.resume_book(KEY)
    assert ok is False
    assert "x" not in started
    plan = proj.plan()
    assert plan["status"] == "completed"


# ---------- livejob 泄漏行活性超时 ----------

def test_livejob_stale_unfinished_row_expires():
    """未 finish 的泄漏行（无心跳）在超时后自动过期，不再钉死「正在创作中」。"""
    livejob.begin(KEY, detail="泄漏行")
    with livejob._LOCK:   # 把 updated_at 拨回 31 分钟前，模拟无人收口
        livejob._ACTIVE[KEY]["updated_at"] = time.time() - 1860
    assert livejob.get(KEY) == {}
    assert not livejob.is_active(KEY)
    # 心跳新鲜的行不受影响
    livejob.begin(KEY, detail="活行")
    assert livejob.is_active(KEY)


# ---------- 向导 AI 生成：题材/风格不再丢失 ----------

def test_ai_generate_genre_not_degraded(monkeypatch):
    """16 词题材表全部不得退化成「网络」；风格维度与 style_id 进 prompt。"""
    captured = {}
    monkeypatch.setattr(appmod.llm, "chat_json",
                        lambda msgs, **kw: captured.update({"content": msgs[0]["content"]})
                        or {"title": "t", "idea": "i"})
    genres = ["都市", "玄幻", "重生", "系统流", "穿越", "历史/民国", "军旅", "悬疑/刑侦",
              "末日", "科幻", "游戏", "职场", "现言", "古言/宫斗", "快穿", "恐怖/灵异"]
    for g in genres:
        appmod.api_ai_generate({"medium": "novel", "field": "title", "genre": g})
        assert "网络" not in captured["content"], g
    # 风格进 prompt
    appmod.api_ai_generate({"medium": "novel", "field": "title", "genre": "悬疑/刑侦",
                            "tone": "爽文",
                            "style": {"金手指": "马甲扮猪吃虎"},
                            "style_id": "st_x"})
    c = captured["content"]
    assert "悬疑推理" in c and "基调：爽文" in c
    assert "写作风格（必须遵循）：金手指：马甲扮猪吃虎" in c
    assert "硬性随机约束" not in c   # 随机块已降级，不再压制用户参数


def test_ai_generate_script_genre_and_form(monkeypatch):
    """剧本题材进 prompt；form_map 与 script_writer.FORMS 同源（不再有幽灵键）。"""
    captured = {}
    monkeypatch.setattr(appmod.llm, "chat_json",
                        lambda msgs, **kw: captured.update({"content": msgs[0]["content"]})
                        or {"title": "t", "idea": "i"})
    appmod.api_ai_generate({"medium": "script", "field": "title",
                            "genre": "悬疑", "form": "short_drama"})
    assert "悬疑题材" in captured["content"]
    assert "AI短剧剧本" in captured["content"]
