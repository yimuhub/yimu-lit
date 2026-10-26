"""v2.2.1：通用副驾接口（/api/assist/*）的路由测试。

覆盖：诗词/散文的 SSE 对话与定向改写、落盘只动目标段、409 stale、
版本快照与回退、对话历史与改稿记录留档、旧剧本路径仍兼容。
"""
import json

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import assist, poetry_writer, prose_writer, security, writer


def _client(monkeypatch, root):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(root))
    monkeypatch.setattr(poetry_writer, "PROJECTS_DIR", str(root))
    monkeypatch.setattr(prose_writer, "PROJECTS_DIR", str(root))
    monkeypatch.setattr(writer, "SCHEDULES_FILE", str(root / "schedules.json"))
    appmod.PROJECT_INDEX.clear()
    return TestClient(appmod.app)


def _mk_poetry(root, key="p_r"):
    pdir = root / key
    pdir.mkdir(parents=True)
    plan = {"poemName": "夜雨", "form": "wujue", "status": "drafting", "meta": {"words": 10}}
    (pdir / poetry_writer.F_PLAN).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (pdir / poetry_writer.F_FINAL).write_text("第一段：霜落满阶。\n\n第二段：风过纸窗。\n", encoding="utf-8")
    (pdir / poetry_writer.F_DRAFT).write_text("草稿一段。\n\n草稿二段。\n", encoding="utf-8")
    return pdir


def _mk_prose(root, key="s_r"):
    pdir = root / key
    pdir.mkdir(parents=True)
    plan = {"proseName": "旧巷", "status": "drafting", "meta": {"words": 10}}
    (pdir / prose_writer.F_PLAN).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (pdir / prose_writer.F_FINAL).write_text("定稿一段。\n\n定稿二段。\n", encoding="utf-8")
    return pdir


def _sse_events(text):
    """SSE 是 `event: <类型>\ndata: {...}`，类型在 event 行上。"""
    out = []
    kind = ""
    for line in text.splitlines():
        if line.startswith("event:"):
            kind = line[6:].strip()
        elif line.startswith("data:"):
            try:
                payload = json.loads(line[5:].strip())
            except ValueError:
                continue
            payload["type"] = kind
            out.append(payload)
            kind = ""
    return out


@pytest.fixture()
def root(tmp_path):
    return tmp_path


# ---------------------------------------------------------------- 流式对话


def test_assist_stream_for_poetry(monkeypatch, root):
    _mk_poetry(root)
    monkeypatch.setattr(assist.llm, "chat", lambda *a, **k: "可以把这句的意象再压一层。")
    c = _client(monkeypatch, root)
    res = c.post("/api/assist/stream", json={
        "key": "p_r", "medium": "poetry", "message": "定稿第二段为什么这样写",
        "focus": {"doc": "final", "block": 2}})
    assert res.status_code == 200, res.text
    events = _sse_events(res.text)
    done = [e for e in events if e.get("type") == "done"]
    assert done, res.text[:400]
    d = done[-1]
    assert d["ok"] is True
    assert d["intent"]["kind"] == "discussion"
    assert d["intent"]["doc"] == "final"


def test_assist_stream_returns_patch_for_edit(monkeypatch, root):
    _mk_poetry(root)
    monkeypatch.setattr(assist.llm, "chat", lambda *a, **k: "第二段：风穿过纸窗，响了一下。")
    c = _client(monkeypatch, root)
    res = c.post("/api/assist/stream", json={
        "key": "p_r", "medium": "poetry", "message": "把定稿第2段改得更有画面感"})
    events = _sse_events(res.text)
    done = [e for e in events if e.get("type") == "done"][-1]
    assert done["intent"]["kind"] == "edit"
    patch = done["patch"]
    assert patch["doc"] == "final" and patch["block"] == 2
    assert patch["base_hash"] == assist.block_hash("第二段：风过纸窗。")


def test_assist_stream_logs_chat_history(monkeypatch, root):
    _mk_poetry(root)
    monkeypatch.setattr(assist.llm, "chat", lambda *a, **k: "建议改第二段的尾句。")
    c = _client(monkeypatch, root)
    c.post("/api/assist/stream", json={
        "key": "p_r", "medium": "poetry", "message": "定稿怎么改", "focus": {"doc": "final"}})
    hist = c.get("/api/assist/history", params={"key": "p_r", "medium": "poetry"}).json()
    assert hist["ok"] is True
    assert [m["role"] for m in hist["messages"]] == ["user", "assistant"]
    assert hist["messages"][0]["content"] == "定稿怎么改"


def test_assist_stream_rejects_bad_request(monkeypatch, root):
    _mk_poetry(root)
    c = _client(monkeypatch, root)
    assert c.post("/api/assist/stream", json={"key": "p_r", "medium": "poetry"}).status_code == 400
    assert c.post("/api/assist/stream",
                  json={"key": "nope", "medium": "poetry", "message": "hi"}).status_code == 404


# ---------------------------------------------------------------- 落盘


def test_assist_apply_for_poetry(monkeypatch, root):
    pdir = _mk_poetry(root)
    c = _client(monkeypatch, root)
    body = {"key": "p_r", "medium": "poetry", "doc": "final", "block": 2,
            "base_hash": assist.block_hash("第二段：风过纸窗。"), "new_text": "新的第二段。"}
    res = c.post("/api/assist/apply", json=body)
    assert res.status_code == 200, res.text
    d = res.json()
    assert d["ok"] is True and d["version"]
    text = (pdir / poetry_writer.F_FINAL).read_text(encoding="utf-8")
    assert "新的第二段。" in text
    assert "第一段：霜落满阶。" in text

    stale = c.post("/api/assist/apply", json=body)
    assert stale.status_code == 409 and stale.json()["code"] == "stale"


def test_assist_apply_for_prose(monkeypatch, root):
    pdir = _mk_prose(root)
    c = _client(monkeypatch, root)
    body = {"key": "s_r", "medium": "prose", "doc": "final", "block": 1,
            "base_hash": assist.block_hash("定稿一段。"), "new_text": "改过的第一段。"}
    res = c.post("/api/assist/apply", json=body)
    assert res.status_code == 200, res.text
    assert "改过的第一段。" in (pdir / prose_writer.F_FINAL).read_text(encoding="utf-8")


def test_assist_apply_rejects_bad_input(monkeypatch, root):
    _mk_poetry(root)
    c = _client(monkeypatch, root)
    assert c.post("/api/assist/apply",
                  json={"key": "p_r", "medium": "poetry", "doc": "final"}).status_code == 400
    assert c.post("/api/assist/apply",
                  json={"key": "p_r", "medium": "poetry", "doc": "final", "block": 99,
                        "base_hash": "x", "new_text": "y"}).status_code == 400
    assert c.post("/api/assist/apply",
                  json={"key": "nope", "medium": "poetry", "doc": "final", "block": 1,
                        "base_hash": "x", "new_text": "y"}).status_code == 404


# ---------------------------------------------------------------- 版本 / 留档


def test_assist_versions_and_restore_for_poetry(monkeypatch, root):
    pdir = _mk_poetry(root)
    c = _client(monkeypatch, root)
    body = {"key": "p_r", "medium": "poetry", "doc": "final", "block": 2,
            "base_hash": assist.block_hash("第二段：风过纸窗。"), "new_text": "新的第二段。"}
    ver = c.post("/api/assist/apply", json=body).json()["version"]
    assert ver.startswith("final-")

    listed = c.get("/api/assist/versions",
                   params={"key": "p_r", "medium": "poetry", "doc": "final"}).json()
    assert listed["ok"] is True
    assert ver in [i["version"] for i in listed["items"]]

    res = c.post("/api/assist/restore",
                 json={"key": "p_r", "medium": "poetry", "doc": "final", "version": ver})
    assert res.status_code == 200, res.text
    assert "第二段：风过纸窗。" in (pdir / poetry_writer.F_FINAL).read_text(encoding="utf-8")


def test_assist_history_reports_edits(monkeypatch, root):
    _mk_poetry(root)
    c = _client(monkeypatch, root)
    body = {"key": "p_r", "medium": "poetry", "doc": "final", "block": 2,
            "base_hash": assist.block_hash("第二段：风过纸窗。"), "new_text": "新的第二段。"}
    c.post("/api/assist/apply", json=body)
    hist = c.get("/api/assist/history", params={"key": "p_r", "medium": "poetry"}).json()
    assert len(hist["edits"]) == 1
    ed = hist["edits"][0]
    assert ed["doc"] == "final" and ed["block"] == 2
    assert ed["old"] == "第二段：风过纸窗。" and ed["new"] == "新的第二段。"
    assert ed["version"]


def test_assist_history_404_for_missing_project(monkeypatch, root):
    c = _client(monkeypatch, root)
    assert c.get("/api/assist/history", params={"key": "nope"}).status_code == 404


# ---------------------------------------------------------------- 旧路径兼容


def test_legacy_script_assist_path_still_works(monkeypatch, root):
    from bot import script_writer

    pdir = root / "s_leg"
    (pdir / script_writer.F_BODY).mkdir(parents=True)
    plan = {"scriptName": "旧剧", "medium": "script", "status": "in_progress",
            "scenes": [{"no": 1, "heading": "客栈", "fileName": "第001场.md",
                        "status": "completed", "wordCount": 6}]}
    (pdir / script_writer.F_PLAN).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (pdir / script_writer.F_BODY / "第001场.md").write_text("第一段。\n\n第二段。\n", encoding="utf-8")
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(root))

    c = _client(monkeypatch, root)
    body = {"key": "s_leg", "scene": 1, "block": 2,
            "base_hash": assist.block_hash("第二段。"), "new_text": "新的第二段。"}
    res = c.post("/api/script/assist/apply", json=body)
    assert res.status_code == 200, res.text
    ver = res.json()["version"]
    assert ver.startswith("第001场-")
    listed = c.get("/api/script/assist/versions",
                   params={"key": "s_leg", "scene": 1}).json()
    assert ver in [i["version"] for i in listed["items"]]
