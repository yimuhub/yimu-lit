"""P0 回归：路径穿越、鉴权、创建主流程、队列指令、放行状态。

这些测试对应审查报告里的高危项；任何一项失败都说明修复被回退。
"""
import json
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import security, workflow, writer
from bot import safeio
from bot.safeio import UnsafePathError, atomic_write_text, safe_join


# ---------------------------------------------------------------- 路径安全

@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", "", "/abs", "x\\y", "a\x00b"])
def test_safe_join_rejects_bad_names(bad, tmp_path):
    with pytest.raises(UnsafePathError):
        safe_join(str(tmp_path), bad)


def test_safe_join_accepts_plain_name(tmp_path):
    assert safe_join(str(tmp_path), "20260101-120000-书名").startswith(str(tmp_path))


def test_atomic_write_keeps_file_permissions(tmp_path):
    """原子写不能把用户本来能读的文件悄悄改成只有自己能读（mkstemp 默认 0600）。"""
    p = tmp_path / "正文.md"
    atomic_write_text(str(p), "第一版")
    assert stat.S_IMODE(os.stat(p).st_mode) == safeio._DEFAULT_FILE_MODE
    os.chmod(p, 0o644)
    atomic_write_text(str(p), "第二版")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o644
    assert p.read_text(encoding="utf-8") == "第二版"


def test_get_project_rejects_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    assert appmod._get_project("../secrets") is None
    assert appmod._get_project("..") is None


def test_delete_book_rejects_traversal(monkeypatch, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path / "projects"))
    (tmp_path / "projects").mkdir(exist_ok=True)
    res = writer.delete_book("../victim", hard=True)
    assert res["ok"] is False
    assert victim.exists()


def test_bkchat_rejects_traversal_key(monkeypatch, tmp_path):
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    c = TestClient(appmod.app)
    assert c.get("/api/bkchat?key=../evil").status_code == 400
    assert c.post("/api/bkchat/../evil/feedback", json={"ts": 1, "rating": "up"}).status_code in (400, 404)


# ---------------------------------------------------------------- 本地鉴权

def test_api_requires_token_when_auth_enabled(monkeypatch):
    monkeypatch.setattr(security, "AUTH_ENABLED", True)
    c = TestClient(appmod.app)
    assert c.get("/api/library").status_code == 401
    ok = c.get("/api/library", headers={"X-Shenbi-Token": security.TOKEN})
    assert ok.status_code == 200


def test_index_sets_auth_cookie(monkeypatch):
    monkeypatch.setattr(security, "AUTH_ENABLED", True)
    c = TestClient(appmod.app)
    r = c.get("/")
    assert r.status_code == 200
    assert security.COOKIE_NAME in r.cookies


# ---------------------------------------------------------------- 创建主流程

def _stub_planning(monkeypatch):
    monkeypatch.setattr(workflow, "fill_defaults", lambda cfg: dict(cfg))
    calls = {"titles": 0}

    def _gen_titles(cfg, n=8):
        calls["titles"] += 1
        time.sleep(0.05)   # 模拟真实 LLM 延迟，暴露“后台又建一个目录”的竞态
        return [{"title": "候选甲", "technique": "t", "why": ""},
                {"title": "候选乙", "technique": "t", "why": ""}][:n]

    monkeypatch.setattr(workflow, "gen_titles", _gen_titles)
    monkeypatch.setattr(workflow, "gen_characters", lambda cfg: "人物")
    monkeypatch.setattr(workflow, "gen_outline",
                        lambda cfg: "| 第1章 | 开端 |\n| 第2章 | 转折 |\n")
    monkeypatch.setattr(workflow, "gen_style", lambda cfg: "文风")
    monkeypatch.setattr(workflow, "gen_story_bible", lambda cfg: "圣经")
    monkeypatch.setattr(workflow, "gen_clue_ledger", lambda cfg, outline: "线索")
    monkeypatch.setattr(workflow, "gen_timeline", lambda cfg, outline: "时间")
    monkeypatch.setattr(appmod.learn, "record_book", lambda *a, **k: None)
    return calls


def test_create_writes_same_dir_and_titles_readonly(monkeypatch, tmp_path):
    calls = _stub_planning(monkeypatch)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    c = TestClient(appmod.app)

    r = c.post("/api/create", json={"idea": "一个关于灯塔的故事", "total_chapters": 2})
    assert r.status_code == 200, r.text
    key = r.json()["key"]

    for _ in range(100):
        j = c.get("/api/titles?key=" + key).json()
        if j.get("status") == "awaiting_title" or j.get("error"):
            break
        time.sleep(0.05)

    dirs = [d for d in os.listdir(tmp_path) if os.path.isdir(tmp_path / d)]
    assert dirs == [key], f"接口 key 与实际目录不一致：{dirs}"
    plan = json.load(open(tmp_path / key / "02-写作计划.json", encoding="utf-8"))
    assert len(plan["chapters"]) == 2
    assert plan["status"] == "awaiting_title"
    assert plan["meta"]["title_candidates"]

    before = calls["titles"]
    c.get("/api/titles?key=" + key)
    c.get("/api/titles?key=" + key)
    assert calls["titles"] == before, "轮询 /api/titles 不应再触发 LLM"


# ---------------------------------------------------------------- 队列 / 放行

def test_queue_note_marks_consumed(monkeypatch, tmp_path):
    pdir = tmp_path / "book"
    pdir.mkdir()
    plan = {"chapters": [{"chapterNumber": 1}],
            "meta": {"queued_msgs": [{"text": "别让主角死", "consumed": False}]}}
    json.dump(plan, open(pdir / "02-写作计划.json", "w", encoding="utf-8"), ensure_ascii=False)
    proj = writer.Project(str(pdir), "book")

    assert writer._pop_queue_note(proj, 1) == "别让主角死"
    saved = json.load(open(pdir / "02-写作计划.json", encoding="utf-8"))
    assert saved["meta"]["queued_msgs"][0]["consumed"] is True
    # 第二次不应重复注入
    assert writer._pop_queue_note(proj, 2) == ""


def test_release_running_uses_dict_result(monkeypatch, tmp_path):
    pdir = tmp_path / "book"
    pdir.mkdir()
    json.dump({"chapters": [], "meta": {}},
              open(pdir / "02-写作计划.json", "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(writer, "run_release_gate", lambda key, cfg_over=None: {"before": 1, "after": 2})
    appmod.PROJECT_INDEX.clear()
    appmod._RELEASE_RUNNING.clear()
    c = TestClient(appmod.app)

    assert c.post("/api/release", json={"key": "book"}).json()["running"] is True
    for _ in range(50):
        j = c.get("/api/release-log?key=book").json()
        if not j["running"]:
            break
        time.sleep(0.02)
    assert j["running"] is False
    assert j["result"] == {"before": 1, "after": 2}


# ---------------------------------------------------------------- P1 任务表 / 实时流

def test_jobs_run_persists_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setattr(appmod.jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    j = appmod.jobs.run("demo", lambda: {"ok": 1}, key="book", title="t")
    assert j["status"] == "queued"
    for _ in range(100):
        got = appmod.jobs.get(j["id"])
        if got["status"] in ("done", "failed"):
            break
        time.sleep(0.02)
    assert got["status"] == "done"
    assert got["result"] == {"ok": 1}
    assert got["finished_at"]
    assert [x["id"] for x in appmod.jobs.list_jobs(key="book")] == [j["id"]]
    assert appmod.jobs.list_jobs(key="other") == []
    assert appmod.jobs.list_jobs(kind="nope") == []

    def boom():
        raise RuntimeError("boom")

    bad = appmod.jobs.run("demo2", boom)
    for _ in range(100):
        got = appmod.jobs.get(bad["id"])
        if got["status"] in ("done", "failed"):
            break
        time.sleep(0.02)
    assert got["status"] == "failed"
    assert "boom" in got["error"]


def test_dispatch_is_async_and_reported_by_jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(appmod.jobs, "JOBS_FILE", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(appmod.agents, "dispatch_write",
                        lambda keys, get, write: [{"key": k, "ok": True} for k in keys])
    c = TestClient(appmod.app)
    r = c.post("/api/agents-dispatch", json={"keys": ["A", "B"]})
    body = r.json()
    assert body["ok"] is True and body["async"] is True and body["job_id"]

    for _ in range(100):
        items = c.get("/api/jobs?kind=dispatch").json()["items"]
        if items and items[0]["status"] in ("done", "failed"):
            break
        time.sleep(0.02)
    assert items[0]["id"] == body["job_id"]
    assert items[0]["status"] == "done"
    assert len(items[0]["result"]) == 2


def test_live_sse_emits_status_and_done(monkeypatch, tmp_path):
    pdir = tmp_path / "bk"
    pdir.mkdir()
    json.dump({"status": "completed",
               "chapters": [{"status": "completed", "wordCount": 10}],
               "meta": {"novelName": "bk"}},
              open(pdir / "02-写作计划.json", "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    appmod._RELEASE_RUNNING.clear()
    appmod._ONESHOT_RUNNING.clear()
    appmod._PARALLEL_RUNNING.clear()
    appmod._REWRITING.clear()
    c = TestClient(appmod.app)
    with c.stream("GET", "/api/live/bk") as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        body = "".join(r.iter_text())
    assert "event: status" in body
    assert "event: done" in body
    assert '"state": "completed"' in body


# ---------------------------------------------------------------- P1 编辑 / 版本 / 导出

def _novel_fixture(tmp_path, monkeypatch):
    pdir = tmp_path / "book"
    pdir.mkdir()
    (pdir / "第001章.md").write_text("第一章初稿\n正文内容", encoding="utf-8")
    json.dump({"novelName": "测试书", "status": "completed",
               "chapters": [{"chapterNumber": 1, "title": "开端", "status": "completed",
                             "wordCount": 4, "filePath": "第001章.md"}],
               "meta": {}},
              open(pdir / "02-写作计划.json", "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    return pdir


def test_chapter_edit_keeps_version_and_restores(monkeypatch, tmp_path):
    pdir = _novel_fixture(tmp_path, monkeypatch)
    c = TestClient(appmod.app)

    r = c.put("/api/chapter", json={"key": "book", "n": 1, "text": "改后的正文，多几个字"})
    assert r.status_code == 200 and r.json()["words"] > 0
    assert (pdir / "第001章.md").read_text(encoding="utf-8") == "改后的正文，多几个字"
    plan = json.load(open(pdir / "02-写作计划.json", encoding="utf-8"))
    assert plan["chapters"][0]["wordCount"] == r.json()["words"]

    items = c.get("/api/chapter/versions?key=book&n=1").json()["items"]
    assert items, "编辑前应留快照"
    vid = items[0]["id"]
    assert "第一章初稿" in c.get(f"/api/chapter/version?key=book&n=1&id={vid}").json()["text"]
    assert "-第一章初稿" in c.get(f"/api/chapter/diff?key=book&n=1&id={vid}").json()["diff"]

    assert c.post("/api/chapter/restore", json={"key": "book", "n": 1, "id": vid}).json()["ok"] is True
    assert "第一章初稿" in (pdir / "第001章.md").read_text(encoding="utf-8")
    # 回滚前的内容也留了快照，可再滚回去
    assert any("改后的正文" in c.get(f"/api/chapter/version?key=book&n=1&id={x['id']}").json()["text"]
               for x in c.get("/api/chapter/versions?key=book&n=1").json()["items"])


def test_chapter_bad_inputs(monkeypatch, tmp_path):
    _novel_fixture(tmp_path, monkeypatch)
    c = TestClient(appmod.app)
    assert c.put("/api/chapter", json={"key": "book", "n": 99, "text": "x"}).status_code == 404
    assert c.put("/api/chapter", json={"key": "book", "n": 1, "text": 123}).status_code == 400
    assert c.put("/api/chapter", json={"key": "../secrets", "n": 1, "text": "x"}).status_code == 404
    assert c.get("/api/chapter/version?key=book&n=1&id=../x").status_code == 400


def test_export_formats(monkeypatch, tmp_path):
    _novel_fixture(tmp_path, monkeypatch)
    c = TestClient(appmod.app)
    md = c.get("/api/export?key=book&fmt=md")
    assert md.status_code == 200 and "第一章初稿" in md.text

    docx = c.get("/api/export?key=book&fmt=docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"

    epub = c.get("/api/export?key=book&fmt=epub")
    assert epub.status_code == 200 and epub.content[:2] == b"PK"
    import io, zipfile
    z = zipfile.ZipFile(io.BytesIO(epub.content))
    assert z.read("mimetype") == b"application/epub+zip"
    assert any(n.startswith("OEBPS/ch") for n in z.namelist())

    page = c.get("/api/export?key=book&fmt=print")
    assert page.status_code == 200 and "第1章" in page.text

    assert c.get("/api/export?key=book&fmt=nope").status_code == 400


def test_script_export_fountain_and_fdx(monkeypatch, tmp_path):
    pdir = tmp_path / "s_demo"
    (pdir / "04-正文").mkdir(parents=True)
    (pdir / "04-正文" / "第001场.md").write_text("内景  公交车站  夜\n\n陈默站在站牌下。\n\n赵凯\n（冷笑）\n写好了？",
                                     encoding="utf-8")
    json.dump({"scriptName": "末班车", "form": "film", "status": "completed",
               "scenes": [{"no": 1, "heading": "公交车站 夜", "status": "completed",
                           "fileName": "第001场.md"}]},
              open(pdir / "剧本计划.json", "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    c = TestClient(appmod.app)

    f = c.get("/api/export?key=s_demo&fmt=fountain")
    assert f.status_code == 200
    assert ".内景" in f.text and "@赵凯" in f.text and "写好了？" in f.text

    x = c.get("/api/export?key=s_demo&fmt=fdx")
    assert x.status_code == 200
    assert 'Type="Scene Heading"' in x.text and "写好了？" in x.text


# ---------------------------------------------------------------- 新手指引

def test_load_sample_copies_editable_book(monkeypatch, tmp_path):
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    c = TestClient(appmod.app)

    r1 = c.post("/api/sample")
    assert r1.status_code == 200 and r1.json()["ok"] is True
    key = r1.json()["key"]
    pdir = tmp_path / key
    plan = json.load(open(pdir / "02-写作计划.json", encoding="utf-8"))
    assert plan["novelName"] == "雨夜出租车"
    assert plan["projectPath"] == str(pdir)
    assert plan["meta"]["sample"] is True
    assert (pdir / "第01章-红雨衣.md").exists() and (pdir / "成书-雨夜出租车.md").exists()
    assert c.get(f"/api/preview?key={key}").json()["has_book"] is True

    r2 = c.post("/api/sample")
    assert r2.status_code == 200 and r2.json()["key"] != key


def test_load_sample_missing_source(monkeypatch, tmp_path):
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(appmod, "SAMPLE_DIR", str(tmp_path / "nope"))
    c = TestClient(appmod.app)
    assert c.post("/api/sample").status_code == 404
