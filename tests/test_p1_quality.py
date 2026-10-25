"""P1 回归：Lorebook 检索注入、token 预算、结构化调用日志、评分证据、全局设置。"""
import json

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import llm, lorebook, providers, workflow, writer


def _book_dir(tmp_path, plan=None):
    pdir = tmp_path / "book"
    pdir.mkdir(exist_ok=True)
    json.dump(plan or {"novelName": "测试书", "status": "completed",
                       "chapters": [{"chapterNumber": 1, "title": "开端", "status": "completed",
                                     "wordCount": 10, "filePath": "第001章.md"}],
                       "config": {}, "meta": {}},
              open(pdir / "02-写作计划.json", "w", encoding="utf-8"), ensure_ascii=False)
    return pdir


# ---------------------------------------------------------------- Lorebook

def test_lorebook_parses_table_and_injects_relevant(tmp_path):
    pdir = tmp_path / "book"
    pdir.mkdir()
    (pdir / "09-故事圣经.md").write_text(
        "# 故事圣经\n\n## 设定词典\n| 名称 | 定义 | 首次出现 | 已知 | 真相 | 揭示计划 |\n"
        "|---|---|---|---|---|---|\n"
        "| **真理系统** | 市场推演金手指 | Ch1 | 辅助工具 | 高维文明碎片 | Ch30 揭示 |\n"
        "| **陆沉** | 男主，程序员 | Ch1 | 被甩破产 | 系统宿主 | Ch1 觉醒 |\n"
        "| 无关路人 | 只出现一次 | Ch9 | 无 | 无 | 无 |\n\n## 其他表\n| 名称 | 值 |\n|---|---|\n| 噪声 | 不应被解析 |\n",
        encoding="utf-8")
    items = lorebook.entries(str(pdir))
    assert {e["name"] for e in items} == {"真理系统", "陆沉", "无关路人"}
    assert "噪声" not in {e["name"] for e in items}, "其他表格不应被当成设定词典"
    text = lorebook.relevant(str(pdir), "陆沉觉醒真理系统，推演市场")
    assert "真理系统" in text and "陆沉" in text
    assert text.index("真理系统") < text.index("陆沉"), "命中名称的条目应排前"
    assert lorebook.relevant(str(pdir), "完全无关的天气描写") == ""


def test_build_ctx_injects_lorebook(monkeypatch, tmp_path):
    pdir = _book_dir(tmp_path)
    (pdir / "09-故事圣经.md").write_text(
        "## 设定词典\n| 名称 | 定义 | 已知 | 真相 |\n|---|---|---|---|\n"
        "| 真理系统 | 推演金手指 | 辅助工具 | 高维碎片 |\n", encoding="utf-8")
    (pdir / "01-大纲.md").write_text("# 大纲\n\n第1章 真理系统觉醒\n", encoding="utf-8")
    monkeypatch.setattr(writer, "_cfg_defaults", lambda: {"lorebook": True, "p5_pipeline": False})
    proj = writer.Project(str(pdir), "book")
    ctx = writer._build_ctx(proj, {"chapterNumber": 1, "title": "真理系统觉醒"})
    assert "真理系统" in ctx["lore_relevant"]


# ---------------------------------------------------------------- 预算 / 日志

def test_budget_guard(monkeypatch, tmp_path):
    proj = tmp_path / "book"
    proj.mkdir()
    json.dump({"total_tokens": 120}, open(proj / "usage.json", "w", encoding="utf-8"))
    monkeypatch.setattr(llm, "_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "_global_defaults", lambda: {"token_budget": 100})
    assert llm.budget_tokens() == 100
    assert llm.book_tokens("book") == 120
    with pytest.raises(llm.BudgetExceeded):
        llm._check_budget("book")

    monkeypatch.setattr(llm, "_global_defaults", lambda: {"token_budget": 0})
    llm._check_budget("book")   # 0=不限，不抛


def test_write_loop_pauses_on_budget(monkeypatch, tmp_path):
    pdir = _book_dir(tmp_path)
    json.dump({"total_tokens": 999},
              open(pdir / "usage.json", "w", encoding="utf-8"))
    monkeypatch.setattr(llm, "_PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "_global_defaults", lambda: {"token_budget": 10})
    proj = writer.Project(str(pdir), "book")
    msg = writer.budget_blocked(proj)
    assert "预算" in msg
    writer._pause_for_budget(proj, msg)
    plan = json.load(open(pdir / "02-写作计划.json", encoding="utf-8"))
    assert plan["status"] == "paused"
    assert plan["meta"]["paused"] is True
    assert "预算" in plan["meta"]["budget_exceeded"]


def test_structured_trace_has_no_prompt(monkeypatch, tmp_path):
    trace = tmp_path / "llm_calls.jsonl"
    monkeypatch.setattr(llm, "_TRACE_FILE", str(trace))
    monkeypatch.setattr(llm, "_global_defaults", lambda: {"llm_trace": True})
    llm._trace_call(book="b", stage="writing", model="m", ok=True, total_tokens=12, prompt="SECRET")
    rows = llm.recent_calls(5)
    assert rows and rows[0]["model"] == "m"
    # 调用方误传 prompt 也不应落地（日志只用于指标）
    assert "SECRET" not in trace.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 评分证据

def test_score_chapter_stores_evidence(monkeypatch, tmp_path):
    pdir = _book_dir(tmp_path)
    (pdir / "第001章.md").write_text("正文", encoding="utf-8")
    ev = [{"dimension": "语言", "quote": "他微微一笑", "why": "套话"}]
    monkeypatch.setattr(workflow, "chapter_review",
                        lambda body, cfg, ctx="": {"scores": {"语言": 6}, "weak": ["语言"],
                                                   "evidence": ev, "verdict": "一般"})
    proj = writer.Project(str(pdir), "book")
    writer.score_chapter(proj, 1)
    saved = json.load(open(pdir / "02-写作计划.json", encoding="utf-8"))
    rec = saved["meta"]["chapter_scores"]["1"]
    assert rec["evidence"] == ev
    assert writer.per_chapter_scores(proj)[0]["evidence"] == ev


# ---------------------------------------------------------------- 全局设置

def test_settings_roundtrip(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"defaults": {"call_gap": 2}}), encoding="utf-8")
    monkeypatch.setattr(appmod, "ROOT", str(tmp_path))
    monkeypatch.setattr(providers, "ROOT", str(tmp_path))
    c = TestClient(appmod.app)

    assert c.get("/api/settings").json()["quality_mode"] == "report"
    r = c.post("/api/settings", json={"quality_mode": "off", "token_budget": 5000,
                                       "lorebook": False, "llm_trace": False})
    assert r.status_code == 200 and r.json()["quality_mode"] == "off"
    got = c.get("/api/settings").json()
    assert got["token_budget"] == 5000 and got["lorebook"] is False and got["llm_trace"] is False
    saved = json.load(open(cfg, encoding="utf-8"))["defaults"]
    assert saved["validation"] == {"enabled": False, "mode": "report"}
    assert saved["call_gap"] == 2, "保存设置不得丢未知字段"
    assert c.post("/api/settings", json={"quality_mode": "bad"}).status_code == 400
    assert c.post("/api/settings", json={"token_budget": "x"}).status_code == 400


def test_corrupt_usage_ledger_is_tolerated(monkeypatch, tmp_path):
    book = tmp_path / "book"
    book.mkdir()
    (book / "usage.json").write_text('{"total_tokens": 42}{"total_tokens": 99}', encoding="utf-8")
    monkeypatch.setattr(llm, "_PROJECTS_DIR", str(tmp_path))
    u = llm.read_usage("book")
    assert u["total_tokens"] == 42, "损坏账本应抢救第一个 JSON 对象而不是 500"
    assert llm.book_tokens("book") == 42
    (book / "usage.json").write_text("not json", encoding="utf-8")
    assert llm.book_tokens("book") == 0
