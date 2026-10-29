"""v2.1 P1：Agent 岗位 × 技能绑定测试。

全部写路径 monkeypatch 到 tmp_path 下的临时 config.json，
绝不污染用户真实的 config.json（测试末尾用 mtime 校验真实文件未被触碰）。
"""
import json
import os

import pytest

from bot import agents, providers

REAL_CONFIG = os.path.join(providers.ROOT, "config.json")


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """把 agents._config_path 指到临时文件：所有读写都发生在 tmp 里。"""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"defaults": {"agent_models": {}}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(agents, "_config_path", lambda: str(p))
    return p


def test_skills_for_unbound_returns_empty(tmp_config):
    # v3.0.0：未配置的内置岗位 → 自动填默认技能包（不再返回空）
    assert agents.skills_for("writer") == agents.DEFAULT_AGENT_SKILLS["writer"]
    assert agents.skills_for("not-an-agent") == []
    # 显式清空 → 尊重用户配置返回空
    agents.set_skills("writer", [])
    assert agents.skills_for("writer") == []
    # 坏 JSON 也永不抛异常（此时回落默认包）
    tmp_config.write_text("{broken", encoding="utf-8")
    assert agents.skills_for("writer") == agents.DEFAULT_AGENT_SKILLS["writer"]


def test_set_skills_validates_agent(tmp_config):
    with pytest.raises(ValueError):
        agents.set_skills("ghost", ["某技能"])
    for a in agents.AGENTS:  # 全部岗位合法
        agents.set_skills(a, [f"{a}-skill"])
    assert agents.skills_for("planner") == ["planner-skill"]


def test_set_skills_validates_names_and_dedups_sorts(tmp_config):
    with pytest.raises(ValueError):
        agents.set_skills("writer", "不是列表")
    with pytest.raises(ValueError):
        agents.set_skills("writer", [1, 2])
    agents.set_skills("writer", ["去AI味", "悬念钩子", "去AI味", " 悬念钩子 ", "", "大纲节奏"])
    assert agents.skills_for("writer") == sorted(["大纲节奏", "悬念钩子", "去AI味"])
    # 空列表 = 取消绑定
    agents.set_skills("writer", [])
    assert agents.skills_for("writer") == []


def test_set_skills_lazy_validation_never_blocks_save(tmp_config):
    # 技能库里不存在的名字：只警告、照常保存（懒校验，不阻止、不崩溃）
    agents.set_skills("reviewer", ["不存在的技能xyz-9527"])
    assert agents.skills_for("reviewer") == ["不存在的技能xyz-9527"]


def test_bindings_written_to_tmp_config_only(tmp_config):
    agents.set_skills("editor", ["红线扫描"])
    saved = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert saved["defaults"]["agent_skills"]["editor"] == ["红线扫描"]


def test_real_config_untouched(tmp_config):
    if os.path.exists(REAL_CONFIG):
        before = os.stat(REAL_CONFIG).st_mtime_ns
        agents.set_skills("writer", ["某技能"])
        agents.bindings_view()
        assert os.stat(REAL_CONFIG).st_mtime_ns == before


def test_bindings_view_fields(tmp_config, monkeypatch):
    agents.set_skills("planner", ["世界观构建"])
    agents.set_binding("writer", "prov-x", "model-y")
    bv = agents.bindings_view()
    assert set(bv) == {"agents", "max_global_agents"}
    assert isinstance(bv["max_global_agents"], int)
    assert len(bv["agents"]) == len(agents.AGENTS)   # v4.12.4：21 岗位（7 通用 + 14 文体专属）
    need = {"agent", "label", "stage", "desc", "provider_id", "model", "effective", "skills"}
    for item in bv["agents"]:
        assert need <= set(item)
        assert isinstance(item["skills"], list)
    by = {i["agent"]: i for i in bv["agents"]}
    assert by["planner"]["skills"] == ["世界观构建"]
    assert by["writer"]["provider_id"] == "prov-x" and by["writer"]["model"] == "model-y"
    assert by["writer"]["effective"].startswith("已绑定")
    # v3.0.0：title 未显式配置 → 默认技能包自动填入
    assert by["title"]["skills"] == agents.DEFAULT_AGENT_SKILLS["title"]
    assert by["title"]["effective"] == "回落 stage/默认"


def test_view_keeps_original_fields_and_adds_skills(tmp_config, monkeypatch):
    monkeypatch.setattr(providers, "active_model_options", lambda: [], raising=False)
    agents.set_skills("deslopper", ["去AI腔"])
    v = agents.view()
    assert set(v) == {"agents", "options", "max_global_agents"}  # 原字段不变
    for item in v["agents"]:
        assert {"agent", "label", "stage", "desc", "provider_id", "model", "effective"} <= set(item)
    by = {i["agent"]: i for i in v["agents"]}
    assert by["deslopper"]["skills"] == ["去AI腔"]
