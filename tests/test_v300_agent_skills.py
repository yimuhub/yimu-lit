# v3.0.0：Agent 技能体系——默认技能包 + 全局/按作品双维度绑定 + 注入合并。
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import agents, security, workflow

KEY = "20260101-000000-技能书"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(appmod.writer, "PROJECTS_DIR", str(tmp_path))
    # 隔离全局 config（默认包初始化会写盘，绝不碰真实配置）
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"defaults": {}}), encoding="utf-8")
    monkeypatch.setattr(agents, "_config_path", lambda: str(cfg_dir / "config.json"))
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    yield
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()


def _mk_book(tmp_path, skills=None):
    pdir = tmp_path / KEY
    pdir.mkdir()
    plan = {"novelName": "技能书", "status": "in_progress",
            "config": {"chapters": 3, "genre": "都市"},
            "chapters": [{"chapterNumber": 1, "title": "第1章", "status": "pending"}],
            "meta": {}}
    if skills is not None:
        plan["meta"]["agent_skills"] = skills
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return pdir


def test_default_skills_autofill(tmp_path):
    """内置岗位首次读取自动填默认技能包并落盘；清空过的岗位不被覆盖。"""
    binds = agents.get_skill_bindings()
    for agent, defaults in agents.DEFAULT_AGENT_SKILLS.items():
        assert binds.get(agent) == defaults, agent
    # 已显式配置（含清空）的岗位不动
    agents.set_skills("writer", [])
    binds2 = agents.get_skill_bindings()
    assert binds2["writer"] == []
    assert binds2["planner"] == agents.DEFAULT_AGENT_SKILLS["planner"]


def test_book_skills_api_roundtrip(tmp_path):
    """/api/book-skills：写入 → 读取 → 清空；非法岗位 400。"""
    _mk_book(tmp_path)
    client = TestClient(appmod.app)
    r = client.post("/api/book-skills", json={"key": KEY, "agent": "writer",
                                              "skills": ["novel-hangup-curve", "core-anti-slop"]})
    assert r.status_code == 200
    assert r.json()["book"]["writer"] == ["core-anti-slop", "novel-hangup-curve"]
    g = client.get("/api/book-skills", params={"key": KEY}).json()
    assert g["book"]["writer"] and isinstance(g["global"], dict)
    # 清空
    client.post("/api/book-skills", json={"key": KEY, "agent": "writer", "skills": []})
    g2 = client.get("/api/book-skills", params={"key": KEY}).json()
    assert g2["book"]["writer"] == []
    assert client.post("/api/book-skills", json={"key": KEY, "agent": "nope", "skills": []}).status_code == 400
    assert client.post("/api/book-skills", json={"key": "nope", "agent": "writer", "skills": []}).status_code == 404


def test_agent_skills_block_merges_book_and_global(tmp_path, monkeypatch):
    """注入合并：本书绑定 ∪ 全局绑定（本书置顶）；两者皆空 → 空串。"""
    pdir = _mk_book(tmp_path, skills={"writer": ["novel-hangup-curve"]})
    # 全局绑定
    agents.set_skills("writer", ["core-anti-slop"])
    cfg = {"medium": "novel", "genre": "都市",
           "_book_agent_skills": agents.book_skill_bindings(pdir)}
    block = workflow.agent_skills_block("writer", cfg, "P2", max_bytes=40000)
    assert block, "应注入技能"
    assert "novel-hangup-curve" in block and "core-anti-slop" in block
    # 两者皆空 → 空串
    agents.set_skills("reviewer", [])
    cfg2 = {"medium": "novel", "_book_agent_skills": {}}
    assert workflow.agent_skills_block("reviewer", cfg2, "P4") == ""


def test_build_ctx_injects_book_skills(tmp_path, monkeypatch):
    """写作路径：_build_ctx 把计划 meta.agent_skills 注入 cfg。"""
    from bot import writer
    skills = {"writer": ["novel-hangup-curve"]}
    pdir = _mk_book(tmp_path, skills=skills)
    proj = appmod._get_project(KEY)
    assert proj is not None
    monkeypatch.setattr(workflow, "gen_chapter_plan", lambda *a, **k: "", raising=False)
    ctx = writer._build_ctx(proj, proj.plan()["chapters"][0])
    assert ctx["cfg"]["_book_agent_skills"] == skills
