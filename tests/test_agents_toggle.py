"""v4.20 创作岗位启停/删除 + 全局默认模型 回归测试。

覆盖：
- set_agent_enabled：writer 拒绝停用（作家是创作引擎）；deslopper 可停用/再启用，
  且 view() 里 enabled 字段随动；
- remove_custom_agent：自定义岗位可删（绑定一并清除）、内置岗位/未知岗位拒绝；
- 岗位停用 → 流水线跳过（deslop_chapter 原样返回，不调 LLM）；
- providers.set_global_model / global_model：非法 provider/model 报错，合法设置后
  读写一致（providers.json 指到 tmp，参照 test_provider_key_retention.py）。
"""

import json

import pytest

from bot import agents, providers


@pytest.fixture()
def isolated_agents_config(tmp_path, monkeypatch):
    """agents 的 config.json 指到临时文件；providers.json 也指到 tmp，避免迁移写真实文件。"""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"defaults": {}}), encoding="utf-8")
    monkeypatch.setattr(agents, "_config_path", lambda: str(p))
    pf = tmp_path / "providers.json"
    pf.write_text(json.dumps({"version": 2, "default_provider": "prov-a",
                              "providers": [{"id": "prov-a", "name": "A", "active": True,
                                             "base_url": "https://a.example.com/v1",
                                             "models": ["m-1"], "default_model": "m-1"}]}),
                  encoding="utf-8")
    monkeypatch.setattr(providers, "PROVIDERS_FILE", str(pf))
    return p


def test_writer_cannot_disable(isolated_agents_config):
    with pytest.raises(ValueError):
        agents.set_agent_enabled("writer", False)
    assert "writer" not in agents.disabled_agents()


def test_toggle_deslopper_and_view(isolated_agents_config):
    agents.set_agent_enabled("deslopper", False)
    assert "deslopper" in agents.disabled_agents()
    row = next(a for a in agents.view()["agents"] if a["agent"] == "deslopper")
    assert row["enabled"] is False
    agents.set_agent_enabled("deslopper", True)
    assert "deslopper" not in agents.disabled_agents()
    row = next(a for a in agents.view()["agents"] if a["agent"] == "deslopper")
    assert row["enabled"] is True


def test_toggle_rejects_unknown_agent(isolated_agents_config):
    with pytest.raises(ValueError):
        agents.set_agent_enabled("no_such_agent", False)


def test_remove_custom_agent(isolated_agents_config):
    agents.add_agent("fact_checker", "核对历史细节与器物名物", stage="review",
                     skills=["core-anti-slop"], label="考据师")
    agents.set_agent_enabled("fact_checker", False)
    removed = agents.remove_custom_agent("fact_checker")
    assert removed["label"] == "考据师"
    assert "fact_checker" not in agents._load_custom_agents()
    cfg = json.loads(isolated_agents_config.read_text(encoding="utf-8"))
    d = cfg.get("defaults", {})
    assert "fact_checker" not in (d.get("agent_disabled") or [])
    assert "fact_checker" not in (d.get("agent_skills") or {})
    # 内置岗位不能删，只能停用
    with pytest.raises(ValueError):
        agents.remove_custom_agent("planner")
    with pytest.raises(ValueError):
        agents.remove_custom_agent("no_such_agent")


def test_is_known_agent(isolated_agents_config):
    assert agents.is_known_agent("planner")
    agents.add_agent("fact_checker", "考据师", stage="review")
    assert agents.is_known_agent("fact_checker")
    assert not agents.is_known_agent("ghost")


def test_deslop_skipped_when_deslopper_disabled(isolated_agents_config, monkeypatch):
    """去味师停用 → deslop_chapter 原样返回，绝不调 LLM。"""
    from bot import workflow
    agents.set_agent_enabled("deslopper", False)
    called = {"n": 0}

    def _boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("deslopper 已停用时不应调用 LLM")

    monkeypatch.setattr(workflow.llm, "chat", _boom)
    body = "# 第01章 试\n\n他不由得愣了一下，仿佛瞬间明白了什么。"
    res = workflow.deslop_chapter(body, {"cfg": {}})
    assert res["cleaned"] == body
    assert called["n"] == 0


def test_chapter_review_skipped_when_reviewer_disabled(isolated_agents_config, monkeypatch):
    """审稿人停用 → chapter_review 返回 {}（评分 None、自动重写自然跳过）。"""
    from bot import workflow
    agents.set_agent_enabled("reviewer", False)
    monkeypatch.setattr(workflow.llm, "chat_json",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应调用 LLM")))
    assert workflow.chapter_review("正文", {}) == {}


def test_guided_rewrite_skipped_when_rewriter_disabled(isolated_agents_config, monkeypatch):
    from bot import workflow
    agents.set_agent_enabled("rewriter", False)
    monkeypatch.setattr(workflow.llm, "chat",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应调用 LLM")))
    assert workflow.guided_rewrite("正文", "要求", "约束", {}) == ""


# ---------- 全局默认模型 ----------

@pytest.fixture()
def two_providers(tmp_path, monkeypatch):
    pf = tmp_path / "providers.json"
    data = {
        "version": 2,
        "default_provider": "prov-a",
        "providers": [
            {"id": "prov-a", "name": "供应商A", "preset": False, "active": True,
             "base_url": "https://a.example.com/v1", "models": ["m-1", "m-2"],
             "default_model": "m-1"},
            {"id": "prov-b", "name": "供应商B", "preset": False, "active": True,
             "base_url": "https://b.example.com/v1", "models": ["x-1"],
             "default_model": "x-1"},
        ],
    }
    pf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(providers, "PROVIDERS_FILE", str(pf))
    return pf


def test_set_global_model_validates(two_providers):
    with pytest.raises(providers.ProviderError):
        providers.set_global_model("nope", "m-1")           # provider 不存在
    with pytest.raises(providers.ProviderError):
        providers.set_global_model("prov-a", "not-in-list")  # model 不在该供应商列表


def test_set_global_model_roundtrip(two_providers):
    res = providers.set_global_model("prov-b", "x-1")
    assert res == {"provider_id": "prov-b", "model": "x-1"}
    assert providers.global_model() == {"provider_id": "prov-b", "model": "x-1"}
    stored = json.loads(two_providers.read_text(encoding="utf-8"))
    assert stored["default_provider"] == "prov-b"
    assert stored["providers"][1]["default_model"] == "x-1"


def test_global_model_defaults_to_first_model(two_providers):
    """model 留空 = 取该供应商 default_model；global_model() 读回一致。"""
    res = providers.set_global_model("prov-a", "")
    assert res["provider_id"] == "prov-a"
    assert res["model"] in ("m-1", "m-2")
    assert providers.global_model()["model"] == res["model"]
