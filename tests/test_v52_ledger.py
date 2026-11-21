"""v5.2.0 AI 调用账本（GET /api/ai-ledger）聚合口径回归。

口径（对筛选后的全量记录）：
  · total = 总次数；ok = 成功数；failed = 失败数；
  · tokens = total_tokens 求和（缺 token 按 0 计，不做字数估算）；
  · by_scene = 按场景标签计数；scenes = 全量去重场景（供筛选下拉）；
  · 明细按时间倒序（最新在前）分页返回。
筛选：scene 精确 / status（ok|failed）/ date_from..date_to（YYYY-MM-DD 闭区间）/
q 关键词（全字段子串，含模型/书 key/错误摘要）。
数据源：bot/llm.py 的 _TRACE_FILE（llm_calls.jsonl），测试用临时文件替换。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot import llm, webai

RECORDS = [
    {"ts": "2026-11-18T09:00:00", "scene": "划词", "ok": True, "total_tokens": 50,
     "prompt_tokens": 30, "completion_tokens": 20, "model": "m2", "provider": "p1",
     "stage": "review", "latency_ms": 800, "book": "bkA"},
    {"ts": "2026-11-19T09:00:00", "scene": "幽灵字", "ok": False, "total_tokens": 0,
     "model": "m1", "provider": "p1", "stage": "review", "latency_ms": 120,
     "error": "429 限流: rate exceeded", "book": "bkA"},
    {"ts": "2026-11-20T10:00:00", "scene": "写作", "ok": True, "total_tokens": 100,
     "prompt_tokens": 60, "completion_tokens": 40, "model": "m1", "provider": "p1",
     "stage": "writing", "latency_ms": 500, "book": "bkB"},
    {"ts": "2026-11-20T11:00:00", "scene": "幽灵字", "ok": True, "total_tokens": 30,
     "prompt_tokens": 20, "completion_tokens": 10, "model": "m1", "provider": "p1",
     "stage": "review", "latency_ms": 90, "book": "bkB"},
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时账本文件 + scratch FastAPI（webai.router 两行挂载的等价形态）。"""
    f = tmp_path / "llm_calls.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in RECORDS) + "\n",
                 encoding="utf-8")
    monkeypatch.setattr(llm, "_TRACE_FILE", str(f))
    app = FastAPI()
    app.include_router(webai.router)
    return TestClient(app)


def test_ledger_aggregates_total_ok_failed_and_token_sum(client):
    r = client.get("/api/ai-ledger")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    stats = body["stats"]
    assert stats["total"] == 4
    assert stats["ok"] == 3
    assert stats["failed"] == 1
    assert stats["tokens"] == 50 + 0 + 100 + 30          # token 求和
    assert stats["prompt_tokens"] == 110
    assert stats["completion_tokens"] == 70


def test_ledger_aggregate_is_over_filtered_records(client):
    """聚合口径作用在「筛选后」的全量记录上，不是当前页。"""
    r = client.get("/api/ai-ledger", params={"scene": "幽灵字"})
    body = r.json()
    assert body["stats"]["total"] == 2
    assert body["stats"]["ok"] == 1 and body["stats"]["failed"] == 1
    assert body["stats"]["tokens"] == 30
    assert body["total"] == 2
    assert body["by_scene"] == {"幽灵字": 2}


def test_ledger_scene_filter_exact_match(client):
    r = client.get("/api/ai-ledger", params={"scene": "写作"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["scene"] == "写作"


def test_ledger_status_filter(client):
    ok_r = client.get("/api/ai-ledger", params={"status": "ok"})
    bad_r = client.get("/api/ai-ledger", params={"status": "failed"})
    assert ok_r.json()["stats"]["total"] == 3
    assert bad_r.json()["stats"]["total"] == 1
    assert bad_r.json()["items"][0]["error"].startswith("429 限流")


def test_ledger_date_range_closed_interval(client):
    r = client.get("/api/ai-ledger", params={"date_from": "2026-11-19"})
    assert r.json()["total"] == 3
    r2 = client.get("/api/ai-ledger", params={"date_from": "2026-11-19", "date_to": "2026-11-19"})
    assert r2.json()["total"] == 1
    r3 = client.get("/api/ai-ledger", params={"date_to": "2026-11-18"})
    assert r3.json()["total"] == 1


def test_ledger_keyword_searches_all_fields(client):
    r = client.get("/api/ai-ledger", params={"q": "m2"})
    assert r.json()["total"] == 1
    r2 = client.get("/api/ai-ledger", params={"q": "bkb"})      # 大小写不敏感命中书 key
    assert r2.json()["total"] == 2
    r3 = client.get("/api/ai-ledger", params={"q": "rate exceeded"})
    assert r3.json()["total"] == 1


def test_ledger_pagination_newest_first(client):
    r = client.get("/api/ai-ledger", params={"page": 1, "page_size": 2})
    body = r.json()
    assert body["total"] == 4 and body["pages"] == 2 and body["page"] == 1
    assert [it["ts"] for it in body["items"]] == ["2026-11-20T11:00:00", "2026-11-20T10:00:00"]
    r2 = client.get("/api/ai-ledger", params={"page": 2, "page_size": 2})
    assert [it["ts"] for it in r2.json()["items"]] == ["2026-11-19T09:00:00", "2026-11-18T09:00:00"]


def test_ledger_scene_dropdown_lists_all_distinct_scenes(client):
    r = client.get("/api/ai-ledger")
    assert set(r.json()["scenes"]) == {"写作", "幽灵字", "划词"}
    assert r.json()["scenes"] == sorted(r.json()["scenes"])   # 去重且有序（供下拉）


def test_ledger_records_without_scene_are_counted_as_unlabeled(tmp_path, monkeypatch):
    f = tmp_path / "llm_calls.jsonl"
    rows = [
        {"ts": "2026-11-20T08:00:00", "ok": True, "total_tokens": 7, "model": "m"},   # 旧格式：无 scene
        {"ts": "2026-11-20T08:01:00", "ok": True, "total_tokens": 3, "scene": "续写", "model": "m"},
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(llm, "_TRACE_FILE", str(f))
    app = FastAPI()
    app.include_router(webai.router)
    c = TestClient(app)
    body = c.get("/api/ai-ledger").json()
    assert body["stats"]["total"] == 2 and body["stats"]["tokens"] == 10
    assert body["by_scene"] == {"（未标注）": 1, "续写": 1}
    assert body["scenes"] == ["续写"]          # 下拉只列真实场景标签


def test_ledger_missing_file_returns_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_TRACE_FILE", str(tmp_path / "nope.jsonl"))
    app = FastAPI()
    app.include_router(webai.router)
    c = TestClient(app)
    body = c.get("/api/ai-ledger").json()
    assert body["ok"] is True and body["stats"]["total"] == 0 and body["items"] == []


def test_ledger_broken_lines_are_skipped(tmp_path, monkeypatch):
    f = tmp_path / "llm_calls.jsonl"
    f.write_text(
        json.dumps({"ts": "2026-11-20T08:00:00", "ok": True, "total_tokens": 7}) + "\n"
        + "{broken json line\n"
        + json.dumps({"ts": "2026-11-20T08:05:00", "ok": False, "total_tokens": 0}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(llm, "_TRACE_FILE", str(f))
    app = FastAPI()
    app.include_router(webai.router)
    c = TestClient(app)
    body = c.get("/api/ai-ledger").json()
    assert body["stats"]["total"] == 2 and body["stats"]["failed"] == 1
