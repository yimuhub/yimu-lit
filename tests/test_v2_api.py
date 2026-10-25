"""v2 接口整合：能力探测、经典/新版入口、流式会话、长篇/状态接口的优雅降级。"""
import json
import os

from fastapi.testclient import TestClient

import app as appmod
from bot import security, writer


def _client(monkeypatch, tmp_path=None):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    if tmp_path is not None:
        monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
        appmod.PROJECT_INDEX.clear()
    return TestClient(appmod.app)


def _make_project(tmp_path, key="p-test", chapters=12):
    pdir = tmp_path / key
    pdir.mkdir(parents=True)
    plan = {
        "version": 1, "novelName": "测试书", "projectPath": str(pdir),
        "totalChapters": chapters, "minWordsPerChapter": 3000,
        "status": "in_progress", "writingMode": "serial",
        "config": {"genre": "悬疑", "chapters": chapters},
        "chapters": [{"chapterNumber": i, "title": f"第{i}章",
                      "filePath": f"第{i:02d}章.md", "status": "pending"}
                     for i in range(1, chapters + 1)],
        "meta": {},
    }
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return pdir


def test_capabilities(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/capabilities")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert "v2_ui" in d["features"] and "longform" in d["features"]
    assert {m["key"] for m in d["mediums"]} >= {"novel", "script", "poetry", "prose"}


def test_classic_route_sets_cookie(monkeypatch):
    monkeypatch.setattr(security, "AUTH_ENABLED", True)
    c = TestClient(appmod.app)
    r = c.get("/classic")
    assert r.status_code == 200
    assert security.COOKIE_NAME in r.cookies


def test_v2_entry(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/v2", follow_redirects=False)
    assert r.status_code in (307, 308)
    r2 = c.get("/v2/")
    if os.path.exists(os.path.join(str(appmod.ROOT), "static", "v2", "index.html")):
        assert r2.status_code == 200
    else:
        assert r2.status_code == 404


def test_plan_and_state_do_not_500(monkeypatch, tmp_path):
    """/api/plan 与 /api/state 在模块未就绪时返回 501，就绪时返回 200，绝不 500。"""
    _make_project(tmp_path)
    c = _client(monkeypatch, tmp_path)
    for url in ("/api/plan?key=p-test", "/api/state?key=p-test"):
        r = c.get(url)
        assert r.status_code in (200, 501), (url, r.status_code, r.text[:300])
        if r.status_code == 200:
            assert r.json().get("ok") is True
    r = c.get("/api/plan?key=missing")
    assert r.status_code == 404


def test_references_and_feedback_graceful(monkeypatch):
    c = _client(monkeypatch)
    for url in ("/api/references", "/api/feedback"):
        r = c.get(url)
        assert r.status_code in (200, 501), (url, r.status_code, r.text[:300])


def test_reference_upload_multipart_and_link(monkeypatch, tmp_path):
    """真实 multipart 上传 → 入库 → 关联项目 → 列表可查（v2 前端主链路）。"""
    from bot import references

    _make_project(tmp_path)
    monkeypatch.setattr(references, "REFERENCES_DIR", str(tmp_path / "references"))
    references._clear_cache()
    c = _client(monkeypatch, tmp_path)
    body = ("第一章 潮汐\n\n旧港的灯一盏盏亮起来，林舟没有回头。\n" * 20)
    r = c.post("/api/references/upload",
               data={"title": "潮汐手稿", "usage_mode": "style",
                     "project_key": "p-test", "copyright_confirmed": "true"},
               files={"file": ("chaoxi.txt", body.encode("utf-8"), "text/plain")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["ref_id"].startswith("r-")
    assert d["item"].get("title") == "潮汐手稿"

    listed = c.get("/api/references?key=p-test").json()
    assert listed["ok"] is True
    ids = [it["ref_id"] for it in listed["items"]]
    assert d["ref_id"] in ids
    assert os.path.isfile(tmp_path / "p-test" / "references.json")

    hit = c.get("/api/references/search", params={"key": "p-test", "q": "旧港的灯"})
    assert hit.status_code == 200 and hit.json()["ok"] is True

    r2 = c.post("/api/references/text",
                json={"title": "人物小传", "text": "林舟：旧港修船匠。" * 30,
                      "usage_mode": "facts", "project_key": "p-test"})
    assert r2.status_code == 200, r2.text
    tied = {it["ref_id"] for it in c.get("/api/references?key=p-test").json()["items"]}
    assert r2.json()["ref_id"] in tied
    references._clear_cache()


def test_skills_endpoint(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/skills")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and isinstance(d["items"], list)
    assert d["items"]


def test_chat_stream_sse(monkeypatch):
    c = _client(monkeypatch)
    with c.stream("POST", "/api/chat/stream",
                  json={"session_id": "sse-test", "message": "你好呀"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: start" in body
    assert "event: done" in body
    assert '"ok": true' in body
