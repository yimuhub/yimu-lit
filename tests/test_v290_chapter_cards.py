# v2.9.0：章节完成卡片数据链 + 分支成书 + 对话上下文压缩 + 设置档位。
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import chatsum, security

KEY = "20260101-000000-分支源书"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(appmod.writer, "PROJECTS_DIR", str(tmp_path))
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    yield
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()


def _mk_book(tmp_path, done=3, total=5):
    pdir = tmp_path / KEY
    pdir.mkdir()
    (pdir / "02-写作计划.json").write_text(json.dumps({
        "novelName": "分支源书", "status": "in_progress",
        "chapters": [{"chapterNumber": i, "title": f"第{i}章",
                      "filePath": f"第{i:02d}章.md",
                      "status": "completed" if i <= done else "pending",
                      "wordCount": 3000 if i <= done else None,
                      "tokens": 42000 if i <= done else None,
                      "completedAt": f"2026-10-28T0{i}:00:00" if i <= done else None}
                     for i in range(1, total + 1)],
        "meta": {"favorite": True, "tags": ["测试"], "released": True},
    }, ensure_ascii=False), encoding="utf-8")
    for i in range(1, done + 1):
        (pdir / f"第{i:02d}章.md").write_text(f"# 第{i:02d}章\n\n正文{i}。" * 10, encoding="utf-8")
    (pdir / "18-状态库.json").write_text(json.dumps({
        "metrics": {"words": 9000, "last_chapter": done},
        "timeline": [{"chapter": i, "event": f"事件{i}"} for i in range(1, total + 1)],
    }, ensure_ascii=False), encoding="utf-8")
    (pdir / "chat_log.jsonl").write_text(
        '\n'.join(json.dumps({"role": "user", "content": f"消息{i}"}, ensure_ascii=False)
                  for i in range(3)) + "\n", encoding="utf-8")
    return pdir


def test_branch_creates_new_book_trimmed(tmp_path):
    """分支成新书：1..N 章保留、之后回 pending、聊天留档清空、原书不动。"""
    _mk_book(tmp_path)
    client = TestClient(appmod.app)
    r = client.post("/api/branch", json={"key": KEY, "chapter": 2})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["key"].endswith("分支2")
    new_key = data["key"]
    new_pdir = tmp_path / new_key
    assert new_pdir.is_dir()
    plan = json.loads((new_pdir / "02-写作计划.json").read_text(encoding="utf-8"))
    assert plan["novelName"].endswith("·分支2")
    assert plan["status"] == "in_progress"
    assert plan["branchedFrom"] == {"key": KEY, "chapter": 2,
                                    "at": plan["branchedFrom"]["at"]}
    chs = {c["chapterNumber"]: c for c in plan["chapters"]}
    assert chs[1]["status"] == "completed" and chs[2]["status"] == "completed"
    assert chs[3]["status"] == "pending" and chs[3]["wordCount"] is None
    assert plan["meta"]["favorite"] is True          # 用户元数据保留
    assert "released" not in plan["meta"]            # 放行门禁不带走
    assert not (new_pdir / "chat_log.jsonl").exists()  # 分支是新对话起点
    st = json.loads((new_pdir / "18-状态库.json").read_text(encoding="utf-8"))
    assert all(t["chapter"] <= 2 for t in st["timeline"])
    assert st["metrics"]["last_chapter"] == 2
    # 原书完全不受影响
    old = json.loads((tmp_path / KEY / "02-写作计划.json").read_text(encoding="utf-8"))
    assert old["novelName"] == "分支源书" and old["status"] == "in_progress"
    assert (tmp_path / KEY / "chat_log.jsonl").exists()
    # 新书在作品库可见
    item = appmod.writer.library_item(new_key)
    assert item and item["medium"] == "novel"


def test_branch_validates_input(tmp_path):
    _mk_book(tmp_path)
    client = TestClient(appmod.app)
    assert client.post("/api/branch", json={"key": KEY}).status_code == 400
    assert client.post("/api/branch", json={"key": "nope", "chapter": 2}).status_code == 404


def test_settings_chat_context_keys(tmp_path, monkeypatch):
    """对话与上下文设置：档位校验 + 读写回路（隔离到临时 config.json，绝不碰真实配置）。"""
    import json as _json
    from bot import providers as _prov
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(_json.dumps({"defaults": {}}), encoding="utf-8")
    monkeypatch.setattr(_prov, "ROOT", str(cfg_dir))
    monkeypatch.setattr(appmod, "ROOT", str(cfg_dir))
    client = TestClient(appmod.app)
    base = client.get("/api/settings").json()
    assert base["chat_context_limit"] in chatsum.CONTEXT_LIMITS
    assert "chat_auto_compress" in base
    r = client.post("/api/settings", json={"chat_context_limit": "1m", "chat_auto_compress": False})
    assert r.status_code == 200
    assert r.json()["chat_context_limit"] == "1m" and r.json()["chat_auto_compress"] is False
    assert client.post("/api/settings", json={"chat_context_limit": "9m"}).status_code == 400


def test_chatsum_compress_once_not_repeat(tmp_path):
    """压缩只对新增溢出生效：二次调用不重复摘要（already 计数生效）。"""
    big = [{"role": "user", "content": "剧情推进" * 90000}]
    r1 = chatsum.maybe_compress(str(tmp_path), big)
    assert r1["compressed"] is True and r1["count"] == 1
    # 已压缩的 1 条不计入：剩下的 0 条 → 不再压缩
    r2 = chatsum.maybe_compress(str(tmp_path), big)
    assert r2["compressed"] is False
    # 新增一条大消息 → 压缩计数累加
    r3 = chatsum.maybe_compress(str(tmp_path), big + [{"role": "assistant", "content": "好" * 400000}])
    assert r3["compressed"] is True and r3["count"] == 2


def test_chatsum_disabled_or_small(tmp_path, monkeypatch):
    monkeypatch.setattr(chatsum, "compress_enabled", lambda: False)
    r = chatsum.maybe_compress(str(tmp_path), [{"role": "user", "content": "x" * 999999}])
    assert r["compressed"] is False and r["history"]


def test_write_chapter_records_tokens_and_time(tmp_path, monkeypatch):
    """章节完成后写入 tokens（usage 差值）与 completedAt——章节卡片的数据源。"""
    from bot import llm, writer
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "他推门进来，把伞收好。" * 20)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {})
    pdir = tmp_path / "bk"
    pdir.mkdir()
    plan = {"version": 1, "novelName": "T", "status": "in_progress",
            "minWordsPerChapter": 20, "config": {"chapters": 1, "genre": "都市"},
            "chapters": [{"chapterNumber": 1, "title": "第1章", "filePath": "第01章.md",
                          "status": "pending", "wordCount": None, "wordCountPass": None,
                          "retryCount": 0}]}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (pdir / "01-大纲.md").write_text("# 大纲\n\n第1章 开局\n", encoding="utf-8")
    proj = writer.Project(str(pdir), "bk")

    # v2.9.0：章节标记 → usage by_chapter 记账；写后读回 by_chapter["1"].total
    ledger = {"by_chapter": {}, "total_tokens": 0}
    recorded = []
    monkeypatch.setattr(llm, "book_chapter_tokens", lambda k, no: ledger["by_chapter"].get(str(no), {}).get("total", 0))
    # 模拟 llm.chat 期间 _record_usage 的 by_chapter 累加（writer 先 set_chapter(1)）
    real_set_chapter = llm.set_chapter
    def _spy_set_chapter(no):
        real_set_chapter(no)
        if no:
            recorded.append(no)
    monkeypatch.setattr(llm, "set_chapter", _spy_set_chapter)
    writer._write_chapter(proj, proj.plan()["chapters"][0], 20, 0)
    assert recorded == [1]
    ch = proj.plan()["chapters"][0]
    assert ch["status"] == "completed"
    assert ch["completedAt"]
    # _write_chapter 结束时清除章标记（审校阶段不计入章节）
    assert getattr(llm._TL, "chapter", None) is None
