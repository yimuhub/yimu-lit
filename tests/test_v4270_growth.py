"""v4.27.0：技能进化「越写越强」成长仪表回归。

覆盖：growth_report 跨书聚合（总览/趋势/六维对比）、writing_signal、
propose_from_writing_data（mock LLM 失败时确定性兜底）、端点可达、auto_propose 默认开。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from bot import skill_evo, writer


def _mk_book(tmp_path, name, createdAt, scores):
    pdir = tmp_path / name
    pdir.mkdir(parents=True, exist_ok=True)
    meta = {"chapter_scores": {}, "paused": False}
    chapters = []
    for no, s in scores:
        meta["chapter_scores"][str(no)] = {"score": s, "weak": ["因果可信"] if s < 70 else [],
                                           "dims": {"结构": s / 10, "因果可信": s / 10 - 1,
                                                    "人物": s / 10, "连续性": s / 10 - 1,
                                                    "语言": s / 10, "读者体验": s / 10},
                                           "ts": 1700000000.0 + no}
        chapters.append({"chapterNumber": no, "title": f"第{no}章",
                         "status": "completed", "wordCount": 3000,
                         "filePath": f"第{no:02d}章.md"})
    plan = {"novelName": name, "status": "completed", "createdAt": createdAt,
            "totalChapters": len(scores), "chapters": chapters,
            "config": {}, "meta": meta}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return pdir


@pytest.fixture
def env(tmp_path, monkeypatch):
    _mk_book(tmp_path, "20260101-000000-第一本", "2026-01-01T10:00:00",
             [(i, 55 + i) for i in range(1, 11)])                       # 56-65
    _mk_book(tmp_path, "20260201-000000-第二本", "2026-02-01T10:00:00",
             [(i, 70 + i) for i in range(1, 11)])                       # 71-80
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(skill_evo, "writer_project_dir", lambda: str(tmp_path))
    # 候选/版本/审计全部隔离到 tmp——不能污染真实 data/ 目录（否则重复候选撞车）
    monkeypatch.setattr(skill_evo, "CAND_DIR", str(tmp_path / "cands"))
    monkeypatch.setattr(skill_evo, "VERS_DIR", str(tmp_path / "vers"))
    monkeypatch.setattr(skill_evo, "AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(skill_evo, "EVOLVED_DIR", str(tmp_path / "evolved"))
    monkeypatch.setattr(skill_evo, "_INDEX_CACHE", None, raising=False)
    return tmp_path


def test_growth_report_shows_improvement(env):
    r = skill_evo.growth_report()
    assert r["total"]["books"] == 2
    assert r["total"]["chaptersScored"] == 20
    assert r["total"]["recentAvg"] > r["total"]["earlyAvg"]   # 越写越强
    assert r["total"]["delta"] > 0
    assert len(r["series"]) == 20
    # 六维对比：连续性（低分弱项）应正增长
    assert r["dimsCompare"]["连续性"]["delta"] > 0
    assert r["weakTop"] and r["weakTop"][0][0] == "因果可信"


def test_writing_signal(env):
    s = skill_evo.writing_signal()
    assert s["ok"] is True
    assert s["chaptersScored"] == 20
    assert s["books"] == 2


def test_propose_from_writing_data_llm_down_falls_back(env, monkeypatch):
    from bot import llm
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {}, raising=False)
    r = skill_evo.propose_from_writing_data("novel")
    assert isinstance(r, dict) and r.get("ok"), r
    cand = r.get("candidate") or r
    # 候选进了候选区，且证据来自真实数据
    assert "低分" in json.dumps(cand, ensure_ascii=False)


def test_growth_endpoint(env, monkeypatch):
    monkeypatch.setattr(security_auth(), "AUTH_ENABLED", False) if False else None
    import app as appmod
    from bot import security
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    client = TestClient(appmod.app)
    r = client.get("/api/skills/evolution/growth")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["total"]["books"] == 2


def test_auto_propose_default_on():
    from bot import writer as _wr
    assert (_wr._cfg_defaults().get("skill_evolution") or {}).get("auto_propose", True) is True


def security_auth():
    from bot import security
    return security
