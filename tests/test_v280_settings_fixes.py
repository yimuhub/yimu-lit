# v2.8.0：一致性体检修复链路 + 时间线全量 + 状态注入过滤 handled。
#
# 回归背景（E2E 实测踩坑）：
# 1. 右栏「一键修复」调 ctx.api.fixConsistency → TypeError（ctx 无 api）→ 已改用模块级 api；
#    后端 continuity.fix 此前不存在，只会假装成功 → 现在有确定性修复 + handled 标记。
# 2. /api/state 的 timeline 只回 30 条且前端字段名读错 → 时间线只剩几个空点。
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import security

KEY = "20260101-000000-体检书"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(appmod.writer, "PROJECTS_DIR", str(tmp_path))
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    yield
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()


def _mk_book(tmp_path, timeline_items=None):
    pdir = tmp_path / KEY
    pdir.mkdir()
    (pdir / "02-写作计划.json").write_text(json.dumps(
        {"novelName": "体检书", "status": "completed",
         "chapters": [{"chapterNumber": 1, "title": "第1章", "status": "completed",
                       "wordCount": 3000}]},
        ensure_ascii=False), encoding="utf-8")
    state = {"metrics": {"words": 3000}, "timeline": timeline_items or []}
    (pdir / "18-状态库.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return pdir


def test_fix_consistency_marks_handled(tmp_path, monkeypatch):
    """修复后：/api/fix-consistency 真正落 handled 标记，/api/state 不再重复提示。"""
    pdir = _mk_book(tmp_path)
    (pdir / "22-规划体检.json").write_text(json.dumps(
        {"at": "2026-10-28T10:00:00", "scope": "book",
         "issues": ["第2弧缺少 1 张章卡", "第3章章末钩子缺失"]},
        ensure_ascii=False), encoding="utf-8")
    client = TestClient(appmod.app)
    r = client.post("/api/fix-consistency", json={"key": KEY, "id": "第2弧缺少 1 张章卡"})
    assert r.status_code == 200
    res = r.json()["result"]
    assert res["ok"] is True and isinstance(res["actions"], list) and res["actions"]
    audit = json.loads((pdir / "22-规划体检.json").read_text(encoding="utf-8"))
    assert "第2弧缺少 1 张章卡" in (audit.get("handled") or [])
    # /api/state 的 last_audit 只剩未处理问题
    r2 = client.get("/api/state", params={"key": KEY})
    la = (r2.json().get("state") or {}).get("last_audit") or {}
    assert la.get("issues") == ["第3章章末钩子缺失"]


def test_fix_consistency_requires_issue_id(tmp_path):
    _mk_book(tmp_path)
    client = TestClient(appmod.app)
    assert client.post("/api/fix-consistency", json={"key": KEY}).status_code == 400
    assert client.post("/api/fix-consistency", json={"key": "nope", "id": "x"}).status_code == 404


def test_state_timeline_not_capped_at_30(tmp_path):
    """时间线全量返回（v2.7 及以前截断 30 条 → 界面只剩几个点）。"""
    items = [{"chapter": i, "time": f"第{i}日", "event": f"事件{i}",
              "location": "", "characters": []} for i in range(1, 41)]
    _mk_book(tmp_path, timeline_items=items)
    client = TestClient(appmod.app)
    tl = client.get("/api/state", params={"key": KEY}).json()["timeline"]
    assert len(tl) == 40
    assert tl[0]["event"] == "事件1" and tl[-1]["event"] == "事件40"


def test_chat_history_roundtrip(tmp_path, monkeypatch):
    """/api/chat/history 与 /api/chat/clear 修复后全链路可用：
    写入（_save_chat_msg）→ 读取 → 清空。此前 _project 元组误用导致恒 500。"""
    _mk_book(tmp_path)
    from bot.v2api import _save_chat_msg
    proj = appmod._get_project(KEY)
    assert proj is not None
    _save_chat_msg(proj, "user", "继续")
    _save_chat_msg(proj, "assistant", "▶ 继续创作《体检书》…")
    client = TestClient(appmod.app)
    r = client.get("/api/chat/history", params={"key": KEY})
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["role"] for i in items] == ["user", "assistant"]
    assert items[0]["content"] == "继续"
    assert client.post("/api/chat/clear", json={"key": KEY}).status_code == 200
    assert client.get("/api/chat/history", params={"key": KEY}).json()["items"] == []
    assert client.get("/api/chat/history", params={"key": "nope"}).status_code == 404
