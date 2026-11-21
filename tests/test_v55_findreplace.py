"""v5.5.x 全书查找替换（bot/webbench.py + A5 ghost 温度槽）回归。

覆盖（易创对齐清单 A1/A5）：
· GET  /api/book/find  —— 全书扫描按章分组（snippet ≤5/章）、大小写默认不敏感
  （可开精确）、概要小节里的匹配不计入（查不到正文只报告不报错）；
· POST /api/book/replace —— 逐章走既有 saveChapter 链路落盘（book_health.save_chapter
  「替换前」快照 +1、throttled_snapshot 与 PUT /api/chapter 同口径）、概要/备注
  元信息一个字不动（替换词塞进概要不生效）、scope 边界（chapter 需 chapters /
  book 可收窄）、skip 跳过 raw 锁定章、参数边界（空 q / 非法 scope / 未知书 404）；
· bot/ghost.run_stream 温度接 prompts_store 槽（与 run() 同口径）：槽温度生效、
  未覆盖时代码默认不变、清洗输出口径不变。

所有用例不连网；数据落 YIMU_DATA_DIR（conftest 隔离），真实 projects/ 零写入。
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot import ghost, llm, prompts_store, versions, writer, webbench

KEY = "20260101-000000-v55查找替换测试书"

RAW1 = "\n".join([
    "# 第1章 雨夜",
    "",
    "## 本章概要",
    "主角在雨夜里发现了一枚铜钥匙。",
    "",
    "## 章首引子",
    "雨是从傍晚开始下的。",
    "",
    "## 正文",
    "他握着那枚铜钥匙站在屋檐下。",
    "雨幕里，铜钥匙微微发烫，他想起祖母说过的话。",
])

RAW2 = "\n".join([
    "# 第2章 Signal",
    "",
    "## 本章概要",
    "一把钥匙与一盏灯的对话。",
    "",
    "## 正文",
    "His LOCK glows in the dark.",
    "A small lock sits below the lamp.",
])

CH_FILES = {1: "第01-x.md", 2: "第02-x.md"}


def _mk_book() -> writer.Project:
    """一本两章的书：第 1 章正文含「铜钥匙」×2 与概要里的第 3 处；第 2 章含 LOCK/lock。
    每次重建都清掉 .versions（快照断言按「从零开始」口径）。"""
    pdir = os.path.join(writer.PROJECTS_DIR, KEY)
    os.makedirs(pdir, exist_ok=True)
    shutil.rmtree(os.path.join(pdir, ".versions"), ignore_errors=True)
    for n, raw in ((1, RAW1), (2, RAW2)):
        with open(os.path.join(pdir, CH_FILES[n]), "w", encoding="utf-8") as f:
            f.write(raw)
    chapters = []
    for n in (1, 2):
        chapters.append({
            "chapterNumber": n, "title": f"第{n}章", "status": "completed",
            "wordCount": 30, "filePath": CH_FILES[n],
        })
    plan = {"novelName": "查找替换测试书", "status": "in_progress", "totalChapters": 2,
            "minWordsPerChapter": 1000, "chapters": chapters}
    with open(os.path.join(pdir, "02-写作计划.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False)
    return writer.Project(pdir, KEY)


@pytest.fixture()
def book():
    proj = _mk_book()
    yield proj


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(webbench.router)
    return TestClient(app)


def _raw_of(n: int) -> str:
    with open(os.path.join(writer.PROJECTS_DIR, KEY, CH_FILES[n]), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- find：分组 / 纯净域 / 大小写

def test_find_groups_by_chapter_with_snippets(book, client):
    r = client.get("/api/book/find", params={"key": KEY, "q": "铜钥匙"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True and data["total"] == 2 and data["chapters"] == 1
    assert [g["n"] for g in data["groups"]] == [1]
    g = data["groups"][0]
    assert g["count"] == 2 and g["title"] == "第1章"
    assert len(g["snippets"]) <= 5 and len(g["snippets"]) == 2
    for s in g["snippets"]:
        assert s["match"] == "铜钥匙"
        assert isinstance(s["before"], str) and isinstance(s["after"], str)
    assert "铜钥匙" in (g["snippets"][0]["before"] + g["snippets"][0]["match"])


def test_find_summary_match_not_counted_purity_domain(book, client):
    """「主角在雨夜」只出现在 ## 本章概要 里——概要里的匹配不计入正文查找。"""
    r = client.get("/api/book/find", params={"key": KEY, "q": "主角在雨夜"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True           # 查不到正文只报告，不报错
    assert data["groups"] == [] and data["total"] == 0


def test_find_case_insensitive_default_and_exact(book, client):
    r = client.get("/api/book/find", params={"key": KEY, "q": "lock"})
    assert r.status_code == 200
    assert r.json()["total"] == 2       # LOCK + lock（中文场景默认不区分大小写）
    r2 = client.get("/api/book/find", params={"key": KEY, "q": "lock", "case_sensitive": True})
    assert r2.status_code == 200
    assert r2.json()["total"] == 1      # 精确口径只命中小写那处


def test_find_bad_params(book, client):
    assert client.get("/api/book/find", params={"key": KEY, "q": "  "}).status_code == 400
    assert client.get("/api/book/find", params={"key": "no-such-book", "q": "雨"}).status_code == 404


# ---------------------------------------------------------------- replace：saveChapter 链路 + 快照 +1

def test_replace_book_writes_via_save_chain_with_snapshot(book, client):
    r = client.post("/api/book/replace", json={
        "key": KEY, "q": "铜钥匙", "replacement": "银钥匙", "scope": "book"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True and data["total"] == 2 and data["chapters"] == 1
    assert data["changed"][0]["n"] == 1 and data["changed"][0]["count"] == 2

    # 正文域被替换；概要小节原样保留（纯净域红线：替换绝不写进元信息）。
    # 注：merge_meta 链路（与编辑器正文模式保存完全同口径）不回写「## 章首引子/## 正文」
    # 小节标记，只回填概要/备注——引子与正文内容原样保留。
    new_raw = _raw_of(1)
    assert "他握着那枚银钥匙站在屋檐下。" in new_raw
    assert "银钥匙微微发烫" in new_raw
    assert "雨是从傍晚开始下的。" in new_raw                # 引子内容仍在
    assert "## 本章概要" in new_raw
    assert "主角在雨夜里发现了一枚铜钥匙。" in new_raw      # 概要未动
    assert new_raw.count("铜钥匙") == 1                     # 只剩概要里那一处

    # 既有 saveChapter 链路：替换前每章留快照（不受节流限制，原文可恢复）
    # + 节流快照与 PUT /api/chapter 同口径。旧式无 tag 快照在版本列表 label 为空串
    # （与「编辑前」快照同口径，前端回落显示快照 id），内容即替换前的原稿。
    items = versions.list_versions(book.path, 1)
    plain = [v for v in items if not v["id"].endswith("-auto")]
    assert len(plain) == 1
    assert versions.read_version(book.path, 1, plain[0]["id"]) == RAW1
    assert sum(1 for v in items if v["id"].endswith("-auto")) == 1   # 首版 auto（≥50 字）

    # 计划字数/完成态同步（saveChapter 链路的既有语义）
    plan = json.loads(open(os.path.join(book.path, "02-写作计划.json"), encoding="utf-8").read())
    assert plan["chapters"][0]["wordCount"] > 0
    assert plan["chapters"][0]["status"] == "completed"


def test_replace_summary_only_match_reports_without_error(book, client):
    """替换词只出现在概要里 → 不计入替换范围：只报告（no_match），文件一个字不动。"""
    r = client.post("/api/book/replace", json={
        "key": KEY, "q": "主角在雨夜", "replacement": "主角在雪夜", "scope": "book"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["changed"] == []
    assert data["skipped"] and data["skipped"][0]["reason"] == "no_match"
    assert _raw_of(1) == RAW1
    assert versions.list_versions(book.path, 1) == []      # 没有落盘就没有快照


def test_replace_scope_boundaries(book, client):
    # scope=chapter 必须给 chapters
    r = client.post("/api/book/replace", json={"key": KEY, "q": "lock", "replacement": "锁",
                                               "scope": "chapter"})
    assert r.status_code == 400
    # scope=chapter 只动指定章
    r2 = client.post("/api/book/replace", json={"key": KEY, "q": "lock", "replacement": "锁",
                                                "scope": "chapter", "chapters": [2]})
    assert r2.status_code == 200
    assert [c["n"] for c in r2.json()["changed"]] == [2]
    assert _raw_of(1) == RAW1
    # scope=book + chapters 收窄：白名单外（第 2 章有 lock）不被波及
    _mk_book()
    r3 = client.post("/api/book/replace", json={"key": KEY, "q": "lock", "replacement": "锁芯",
                                                "scope": "book", "chapters": [1]})
    assert r3.status_code == 200
    assert r3.json()["changed"] == []                      # 第 1 章没有 lock
    assert _raw_of(2) == RAW2                              # 第 2 章未被波及


def test_replace_skip_raw_locked_chapter(book, client):
    """前端处于「工作文件」视图的章经 skip 交给后端跳过（reason=raw_locked）。"""
    r = client.post("/api/book/replace", json={
        "key": KEY, "q": "铜钥匙", "replacement": "银钥匙", "scope": "book", "skip": [1]})
    assert r.status_code == 200
    data = r.json()
    assert data["changed"] == []
    assert data["skipped"][0]["reason"] == "raw_locked"
    assert _raw_of(1) == RAW1


def test_replace_case_insensitive_and_exact(book, client):
    r = client.post("/api/book/replace", json={
        "key": KEY, "q": "lock", "replacement": "锁", "scope": "chapter", "chapters": [2]})
    assert r.status_code == 200
    assert r.json()["total"] == 2                          # LOCK + lock 都被替换
    raw = _raw_of(2)
    assert "His 锁 glows" in raw and "A small 锁 sits" in raw
    assert "LOCK" not in raw

    # 精确口径：只替换小写那处
    _mk_book()                                             # 重置书（fixture 外重建）
    r2 = client.post("/api/book/replace", json={
        "key": KEY, "q": "lock", "replacement": "锁", "scope": "chapter",
        "chapters": [2], "case_sensitive": True})
    assert r2.status_code == 200
    assert r2.json()["total"] == 1
    raw2 = _raw_of(2)
    assert "His LOCK glows" in raw2 and "A small 锁 sits" in raw2


def test_replace_bad_params(book, client):
    assert client.post("/api/book/replace", json={"key": KEY, "q": "  ", "replacement": "x",
                                                  "scope": "book"}).status_code == 400
    assert client.post("/api/book/replace", json={"key": KEY, "q": "雨", "replacement": "雪",
                                                  "scope": "all"}).status_code == 400
    assert client.post("/api/book/replace", json={"key": "no-such-book", "q": "雨", "replacement": "雪",
                                                  "scope": "book"}).status_code == 404


def test_replace_unknown_chapter_in_whitelist_is_dropped(book, client):
    r = client.post("/api/book/replace", json={
        "key": KEY, "q": "铜钥匙", "replacement": "银钥匙", "scope": "chapter",
        "chapters": [1, 99]})
    assert r.status_code == 200
    assert [c["n"] for c in r.json()["changed"]] == [1]


# ---------------------------------------------------------------- A5 ghost run_stream 温度接槽

def _capture_stream(monkeypatch, text="雨停了。他继续往前走。"):
    seen = {}

    def fake_stream(messages, on_delta=None, **kw):
        seen["temperature"] = kw.get("temperature")
        seen["max_tokens"] = kw.get("max_tokens")
        seen["scene"] = kw.get("scene")
        if on_delta and text:
            on_delta(text)
        return text

    monkeypatch.setattr(llm, "chat_stream", fake_stream, raising=False)
    return seen


def test_ghost_run_stream_temperature_follows_slot(monkeypatch):
    """快捷续写温度与 run() 同口径：接 prompts_store 槽（beat/half 各自生效）。"""
    seen = _capture_stream(monkeypatch)
    captured = {}

    def fake_temperature(group, slot, default):
        captured["args"] = (group, slot, default)
        return 0.11

    monkeypatch.setattr(prompts_store, "temperature", fake_temperature)
    out = ghost.run_stream("雨停了。")
    assert captured["args"] == ("ghost", "beat", ghost.MODES["beat"]["temperature"])
    assert seen["temperature"] == pytest.approx(0.11)

    captured.clear()
    out2 = ghost.run_stream("他说，")
    assert captured["args"] == ("ghost", "half", ghost.MODES["half"]["temperature"])
    assert seen["temperature"] == pytest.approx(0.11)
    # 清洗输出口径不变（正文照常返回）
    assert out == out2 == "雨停了。他继续往前走。"


def test_ghost_run_stream_default_temperature_unchanged(monkeypatch):
    """未覆盖槽时回落代码默认（beat=0.8 / half=0.3），行为与接槽前完全一致。"""
    seen = _capture_stream(monkeypatch)
    ghost.run_stream("雨停了。")
    assert seen["temperature"] == pytest.approx(0.8)
    ghost.run_stream("他说，")
    assert seen["temperature"] == pytest.approx(0.3)


def test_ghost_run_keeps_slot_temperature(monkeypatch):
    """run() 的既有口径不被本次改动影响（回归钉子）。"""
    seen = {}

    def fake_chat(messages, **kw):
        seen["temperature"] = kw.get("temperature")
        return "他抬起头。"

    monkeypatch.setattr(llm, "chat", fake_chat, raising=False)
    ghost.reset_rate_for_tests()
    ghost.run("雨停了。")
    assert seen["temperature"] == pytest.approx(ghost.MODES["beat"]["temperature"])
