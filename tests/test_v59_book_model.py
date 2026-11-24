"""v5.9.0 每本书/每篇内容单独绑定创作大模型（死规律）回归。

作者铁律：每一本书、每一篇内容都可以单独选择一个大模型来创作；选择后自动绑定，
创作全程（规划/写章/审校/重写/并行改写/章名/分镜/总编辑）一律用这个模型，不得回落默认。

守门用例：
  ① POST /api/book-model 把绑定写进 plan.config._provider_id/_model（resolve 第一优先级），
     GET 能读回 source=book；provider_id 传空=清除绑定回落全局；
  ② 校验闸门：供应商不存在/停用/模型不在列表 → 400 中文报错，绝不写入半绑状态；
  ③ /api/status 下发本书当前模型口径（绑定值优先，未绑定回落全局），
     严格绑定失效 → 保留绑定 + model_unavailable 标记，绝不静默换模型；
  ④ 引擎旁路：绑定后 llm 调用收到的 cfg 带 _provider_id/_model（章名/改写/并行改写/
     分镜/总编辑/儿童文体），且每轮从 plan 重读——绑定即生效，下一章起用新模型；
  ⑤ AST 静态门禁：引擎文件里每个 llm.chat/chat_json/chat_stream 调用点都必须传 cfg。
"""
import ast
import copy
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import (agents, assist, audit, chatsum, chief, children_writer, drama_adapter,
                 ghost, llm, model_rules, nlu, planner, providers, script_writer, security,
                 selection_ai, webflow, workflow, writer)

KEY = "20260101-090000-绑定书"
SKEY = "s_20260101-090000-绑定剧"
_REAL_CHAT = llm.chat

PROVIDERS_DATA = {
    "default_provider": "prov_a",
    "providers": [
        {"id": "prov_a", "name": "供应商A", "kind": "text", "active": True, "base_url": "http://127.0.0.1:9/v1",
         "models": ["model-a1", "model-a2"], "default_model": "model-a1", "has_key": True},
        {"id": "prov_b", "name": "供应商B", "kind": "text", "active": True, "base_url": "http://127.0.0.1:9/v1",
         "models": ["model-b1"], "default_model": "model-b1", "has_key": True},
        {"id": "prov_off", "name": "停用C", "kind": "text", "active": False, "base_url": "http://127.0.0.1:9/v1",
         "models": ["model-c1"], "default_model": "model-c1", "has_key": True},
        {"id": "prov_img", "name": "图像供应商", "kind": "image", "active": True, "base_url": "http://127.0.0.1:9/v1",
         "models": ["image-xl"], "default_model": "image-xl", "has_key": True},
    ],
}


@pytest.fixture()
def fake_providers(monkeypatch):
    """可控的供应商表（不读真实 providers.json，测试互不串扰）。"""
    state = {"data": copy.deepcopy(PROVIDERS_DATA)}
    monkeypatch.setattr(providers, "load_data", lambda: copy.deepcopy(state["data"]))
    return state


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    return TestClient(appmod.app)


def _mk_novel(tmp_path, monkeypatch, cfg=None, total=3, done=1):
    """一本最小可跑的小说项目（含章文件），config 可注入绑定。"""
    pdir = tmp_path / KEY
    pdir.mkdir()
    chapters = []
    for n in range(1, total + 1):
        fp = f"第{n:03d}章.md"
        status = "completed" if n <= done else "pending"
        if n <= done:
            (pdir / fp).write_text(f"# 第{n:02d}章\n\n## 正文\n他关掉收音机。窗外雨声很大，故事还在继续。\n",
                                   encoding="utf-8")
        chapters.append({"chapterNumber": n, "title": f"第{n}章", "status": status,
                         "wordCount": 30 if n <= done else 0, "filePath": fp})
    plan = {"novelName": "绑定书", "status": "in_progress", "chapters": chapters,
            "totalChapters": total, "minWordsPerChapter": 3000,
            "config": cfg if isinstance(cfg, dict) else {}, "meta": {}}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    return pdir


def _bind(client, provider_id="prov_b", model="model-b1", key=KEY):
    return client.post("/api/book-model", json={"key": key, "provider_id": provider_id, "model": model})


# ---------------------------------------------------------------- ① 绑定端契约


def test_bind_writes_plan_config_and_get_reads_back(client, fake_providers, tmp_path, monkeypatch):
    _mk_novel(tmp_path, monkeypatch)
    r = _bind(client)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["bound"] is True and body["source"] == "book"
    assert body["provider_id"] == "prov_b" and body["model"] == "model-b1"
    assert "创作全程使用" in body["msg"]
    # 绑定真的落在 plan.config（resolve 的第一优先级键）
    plan = json.load(open(tmp_path / KEY / "02-写作计划.json", encoding="utf-8"))
    assert plan["config"]["_provider_id"] == "prov_b"
    assert plan["config"]["_model"] == "model-b1"
    assert plan["config"]["_model_bound"] is True
    # GET 读回绑定值
    g = client.get("/api/book-model", params={"key": KEY}).json()
    assert g["ok"] is True and g["bound"] is True and g["source"] == "book"
    assert g["provider_id"] == "prov_b" and g["model"] == "model-b1"
    assert g["provider_name"] == "供应商B"


def test_bind_model_empty_uses_provider_default(client, fake_providers, tmp_path, monkeypatch):
    _mk_novel(tmp_path, monkeypatch)
    r = _bind(client, provider_id="prov_b", model="")
    assert r.status_code == 200
    assert r.json()["model"] == "model-b1"   # 供应商 default_model 兜底


def test_clear_binding_falls_back_to_global(client, fake_providers, tmp_path, monkeypatch):
    _mk_novel(tmp_path, monkeypatch)
    assert _bind(client).status_code == 200
    r = client.post("/api/book-model", json={"key": KEY, "provider_id": "", "model": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["bound"] is False and body["source"] == "global"
    assert "回落全局" in body["msg"]
    plan = json.load(open(tmp_path / KEY / "02-写作计划.json", encoding="utf-8"))
    assert "_provider_id" not in plan["config"] and "_model" not in plan["config"]
    assert "_model_bound" not in plan["config"]
    g = client.get("/api/book-model", params={"key": KEY}).json()
    assert g["source"] == "global" and g["bound"] is False


def test_bind_missing_plan_returns_400(client, fake_providers, tmp_path, monkeypatch):
    pdir = tmp_path / KEY
    pdir.mkdir()   # 目录在、计划文件缺失
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    r = _bind(client)
    assert r.status_code == 400
    assert "写作计划缺失" in r.json()["error"]


def test_bind_unknown_project_404(client, fake_providers):
    r = _bind(client, key="not-exists-key")
    assert r.status_code == 404


# ---------------------------------------------------------------- ② 校验闸门


def test_bind_unknown_provider_400(client, fake_providers, tmp_path, monkeypatch):
    pdir = _mk_novel(tmp_path, monkeypatch)
    r = _bind(client, provider_id="prov_none", model="model-x")
    assert r.status_code == 400
    assert "供应商不存在" in r.json()["error"]
    plan = json.load(open(pdir / "02-写作计划.json", encoding="utf-8"))
    assert "_provider_id" not in plan["config"]   # 绝不写半绑状态


def test_bind_inactive_provider_400(client, fake_providers, tmp_path, monkeypatch):
    pdir = _mk_novel(tmp_path, monkeypatch)
    r = _bind(client, provider_id="prov_off", model="model-c1")
    assert r.status_code == 400
    assert "停用" in r.json()["error"]
    plan = json.load(open(pdir / "02-写作计划.json", encoding="utf-8"))
    assert "_provider_id" not in plan["config"]


def test_bind_model_not_in_list_400(client, fake_providers, tmp_path, monkeypatch):
    pdir = _mk_novel(tmp_path, monkeypatch)
    r = _bind(client, provider_id="prov_b", model="model-a1")
    assert r.status_code == 400
    assert "不在供应商" in r.json()["error"] and "模型列表" in r.json()["error"]
    plan = json.load(open(pdir / "02-写作计划.json", encoding="utf-8"))
    assert "_provider_id" not in plan["config"]


def test_bind_image_provider_400(client, fake_providers, tmp_path, monkeypatch):
    pdir = _mk_novel(tmp_path, monkeypatch)
    r = _bind(client, provider_id="prov_img", model="image-xl")
    assert r.status_code == 400
    assert "图像" in r.json()["error"] and "文本" in r.json()["error"]
    plan = json.load(open(pdir / "02-写作计划.json", encoding="utf-8"))
    assert "_provider_id" not in plan["config"]


def test_session_model_choice_validates_before_mutating(client, fake_providers, monkeypatch):
    sid = "s-session-model-check"
    original = {"_provider_id": "prov_a", "_model": "model-a1"}
    monkeypatch.setitem(appmod.SESSIONS, sid, {"id": sid, "state": "idle", "prov": dict(original)})
    for pid, model, word in (("prov_off", "model-c1", "停用"),
                             ("prov_img", "image-xl", "图像"),
                             ("prov_b", "bad-model", "模型列表")):
        with pytest.raises(providers.ProviderError, match=word):
            appmod._remember_session_model(sid, pid, model)
        assert appmod.SESSIONS[sid]["prov"] == original
    appmod._remember_session_model(sid, "prov_b", "")
    assert appmod.SESSIONS[sid]["prov"] == {
        "_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    appmod._remember_session_model(sid, "", "model-b1")
    assert appmod.SESSIONS[sid]["prov"]["_model_bound"] is True


def test_strict_resolve_fails_closed(fake_providers, monkeypatch):
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    assert providers.resolve(cfg)[1] == "model-b1"
    cases = [
        ("missing", "不存在"), ("inactive", "停用"), ("image", "图像"), ("model", "模型列表"),
    ]
    for case, word in cases:
        data = copy.deepcopy(PROVIDERS_DATA)
        bound = next(p for p in data["providers"] if p["id"] == "prov_b")
        if case == "missing":
            data["providers"] = [p for p in data["providers"] if p["id"] != "prov_b"]
        elif case == "inactive":
            bound["active"] = False
        elif case == "image":
            bound["kind"] = "image"
        else:
            bound["models"] = ["model-b2"]
        monkeypatch.setattr(providers, "load_data", lambda data=data: copy.deepcopy(data))
        with pytest.raises(providers.ProviderError, match=word):
            providers.resolve(cfg)


def test_strict_binding_blocks_stage_agent_and_fallback(fake_providers):
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True,
           "defaults": {"stage_models": {"writing": {"provider_id": "prov_a", "model": "model-a2"}}},
           "meta": {"agent_models": {"writer": {"provider_id": "prov_a", "model": "model-a2"}}}}
    assert providers.stage_resolve(cfg, "writing")[1] == "model-b1"
    assert providers.agent_resolve(cfg, "writer", "writing")[1] == "model-b1"
    assert providers.fallback_config(cfg, exclude_provider="prov_b", exclude_model="model-b1") is None


def test_strict_binding_never_invokes_429_fallback(fake_providers, monkeypatch):
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True,
           "defaults": {"llm_cache": False}}

    class Resp:
        status_code = 429
        text = "rate limited"
        headers = {}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return Resp()

    def forbidden(*args, **kwargs):
        raise AssertionError("严格绑定不得尝试切换供应商")

    monkeypatch.setattr(llm.httpx, "Client", Client)
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(model_rules, "pace_rpm", lambda rules: None)
    monkeypatch.setattr(providers, "fallback_config", forbidden)
    with pytest.raises(llm.LLMError, match="429"):
        _REAL_CHAT([{"role": "user", "content": "ping"}], cfg=cfg, retries=1)


# ---------------------------------------------------------------- ③ /api/status 模型口径


def test_status_model_bound_and_global(client, fake_providers, tmp_path, monkeypatch):
    _mk_novel(tmp_path, monkeypatch)
    # 未绑定 → 回落全局默认（prov_a/model-a1）
    st = client.get("/api/status", params={"key": KEY}).json()
    assert st["model_bound"] is False and st["model_source"] == "global"
    assert st["model"] == "model-a1"
    assert st["model_provider_id"] == "prov_a"
    # 绑定后 → 显示绑定值
    assert _bind(client).status_code == 200
    st2 = client.get("/api/status", params={"key": KEY}).json()
    assert st2["model_bound"] is True and st2["model_source"] == "book"
    assert st2["model"] == "model-b1"
    assert st2["model_provider_name"] == "供应商B"
    # 解除绑定 → 回全局
    client.post("/api/book-model", json={"key": KEY, "provider_id": "", "model": ""})
    st3 = client.get("/api/status", params={"key": KEY}).json()
    assert st3["model_bound"] is False and st3["model"] == "model-a1"


def test_status_unavailable_when_bound_provider_removed(client, fake_providers, tmp_path, monkeypatch):
    pdir = _mk_novel(tmp_path, monkeypatch)
    assert _bind(client, provider_id="prov_b").status_code == 200
    fake_providers["data"]["providers"] = [p for p in fake_providers["data"]["providers"]
                                           if p["id"] != "prov_b"]
    st = client.get("/api/status", params={"key": KEY}).json()
    assert st.get("model_fallback") is not True
    assert st["model_unavailable"] is True
    assert st["model_bound"] is True and st["model_source"] == "book"
    assert st["model"] == "model-b1" and st["model_provider_id"] == "prov_b"
    assert "不可用" in (st.get("model_note") or "")
    plan = json.load(open(pdir / "02-写作计划.json", encoding="utf-8"))
    assert plan["config"]["_provider_id"] == "prov_b"
    assert plan["config"]["_model_bound"] is True
    g = client.get("/api/book-model", params={"key": KEY}).json()
    assert g["source"] == "book" and g["model_unavailable"] is True


def test_status_unavailable_when_bound_model_removed(client, fake_providers, tmp_path, monkeypatch):
    pdir = _mk_novel(tmp_path, monkeypatch)
    assert _bind(client, provider_id="prov_b", model="model-b1").status_code == 200
    fake_providers["data"]["providers"][1]["models"] = ["model-b2"]
    st = client.get("/api/status", params={"key": KEY}).json()
    assert st.get("model_fallback") is not True
    assert st["model_unavailable"] is True and st["model_bound"] is True
    assert st["model"] == "model-b1"
    plan = json.load(open(pdir / "02-写作计划.json", encoding="utf-8"))
    assert plan["config"]["_model"] == "model-b1"
    assert plan["config"]["_model_bound"] is True


# ---------------------------------------------------------------- ④ 引擎旁路（绑定即生效）


def test_engine_chapter_naming_uses_bound_cfg(client, fake_providers, tmp_path, monkeypatch):
    """绑定后章名生成（llm.chat_json）收到带 _provider_id/_model 的 cfg；重绑后下一轮用新模型。
    （每轮用不同的默认章名章——_name_chapter 对已定名章会跳过。）"""
    pdir = _mk_novel(tmp_path, monkeypatch, total=3, done=3)
    # 未绑定时 cfg 无绑定键（回落全局由 resolve 决定）
    seen = []
    monkeypatch.setattr(llm, "chat_json",
                        lambda msgs, *a, **k: (seen.append(k.get("cfg")) or {"title": f"章名{len(seen)}"}))
    proj = writer.Project(str(pdir), KEY)
    writer._name_chapter(proj, 1, "这是一段足够长的本章摘要，用于触发章名生成的逻辑分支")
    assert seen and not seen[-1].get("_provider_id")
    # 绑定 → cfg 带绑定键
    assert _bind(client).status_code == 200
    writer._name_chapter(proj, 2, "这是另一段足够长的本章摘要，用于触发章名生成的逻辑分支")
    assert seen[-1].get("_provider_id") == "prov_b"
    assert seen[-1].get("_model") == "model-b1"
    # 重绑 → 下一轮调用立即用新绑定（绑定即生效）
    r = client.post("/api/book-model", json={"key": KEY, "provider_id": "prov_a", "model": "model-a2"})
    assert r.status_code == 200
    writer._name_chapter(proj, 3, "这是第三段足够长的本章摘要，用于触发章名生成的逻辑分支")
    assert seen[-1].get("_provider_id") == "prov_a"
    assert seen[-1].get("_model") == "model-a2"


def test_engine_backfill_naming_uses_bound_cfg(fake_providers, tmp_path, monkeypatch):
    pdir = _mk_novel(tmp_path, monkeypatch)
    (pdir / "21-章摘要.jsonl").write_text(
        json.dumps({"n": 1, "summary": "第一章摘要内容，主角在雨夜追查真相并遭遇伏击"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    seen = []
    monkeypatch.setattr(llm, "chat_json",
                        lambda msgs, *a, **k: (seen.append(k.get("cfg")) or {"names": []}))
    proj = writer.Project(str(pdir), KEY)
    cfg = {"_provider_id": "prov_b", "_model": "model-b1"}
    plan = proj.plan()
    plan["config"] = cfg
    proj.save_plan(plan)
    writer._backfill_chapter_names(proj)
    assert seen and seen[-1].get("_provider_id") == "prov_b"


def test_longform_cfg_rereads_plan_each_chapter(fake_providers, tmp_path, monkeypatch):
    """cfg 每章从 plan 重读（非启动快照）：改绑定 → 下一章立即可见。"""
    pdir = _mk_novel(tmp_path, monkeypatch)
    proj = writer.Project(str(pdir), KEY)
    cfg0, _ = writer._longform_cfg(proj)
    assert not cfg0.get("_provider_id")
    plan = proj.plan()
    plan["config"]["_provider_id"] = "prov_b"
    plan["config"]["_model"] = "model-b1"
    proj.save_plan(plan)
    cfg1, _ = writer._longform_cfg(proj)
    assert cfg1.get("_provider_id") == "prov_b"
    plan = proj.plan()
    plan["config"]["_provider_id"] = "prov_a"
    plan["config"]["_model"] = "model-a2"
    proj.save_plan(plan)
    cfg2, _ = writer._longform_cfg(proj)
    assert cfg2.get("_provider_id") == "prov_a" and cfg2.get("_model") == "model-a2"


def test_rewrite_chain_uses_bound_cfg(fake_providers, tmp_path, monkeypatch):
    """定向重写链（_auto_rewrite_chapter，多岗位并行改写的每章执行核）用本书绑定模型。"""
    pdir = _mk_novel(tmp_path, monkeypatch)
    proj = writer.Project(str(pdir), KEY)
    plan = proj.plan()
    plan["config"] = {"_provider_id": "prov_b", "_model": "model-b1"}
    plan.setdefault("meta", {}).setdefault("chapter_scores", {})["1"] = {
        "score": 60, "weak": ["结构"]}
    proj.save_plan(plan)
    calls = {"n": 0}

    def fake_score(p, no):
        calls["n"] += 1
        return 60 if calls["n"] <= 2 else 85

    seen = []
    monkeypatch.setattr(writer, "score_chapter", fake_score)
    monkeypatch.setattr(writer.workflow, "guided_rewrite",
                        lambda body, fix, cons, cfg: (seen.append(cfg) or "改写后的完整正文。故事继续推进，人物有了新的目标，冲突逐步升级，最终在本章结尾留下钩子。" * 3))
    writer._auto_rewrite_chapter(proj, 1, max_rounds=1, skip_deslop=True)
    assert seen and seen[-1].get("_provider_id") == "prov_b"
    assert seen[-1].get("_model") == "model-b1"


def test_parallel_rewrite_delegates_to_bound_core(fake_providers, tmp_path, monkeypatch):
    """/api/rewrite-parallel（多岗位并行改写）→ writer.run_parallel_rewrite → 绑定的改写核。"""
    pdir = _mk_novel(tmp_path, monkeypatch)
    proj0 = writer.Project(str(pdir), KEY)
    plan = proj0.plan()
    plan["config"] = {"_provider_id": "prov_b", "_model": "model-b1"}
    proj0.save_plan(plan)
    seen = []
    monkeypatch.setattr(writer, "score_chapter", lambda p, no: 60)
    monkeypatch.setattr(writer, "_auto_rewrite_chapter",
                        lambda p, no, **k: (seen.append(no) or 85))
    res = writer.run_parallel_rewrite(KEY, chapters=[1], workers=1, max_rounds=1)
    assert res.get("ok") is True
    assert seen == [1]


def test_chief_deliberate_uses_book_binding(fake_providers, tmp_path, monkeypatch):
    """总编辑理解层（chief.deliberate）在本书绑定了模型时用绑定模型理解。"""
    pdir = _mk_novel(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(llm, "chat_json",
                        lambda msgs, *a, **k: (seen.append(k.get("cfg")) or {"reply": "收到"}))
    ctx = chief.build_unbound_context()
    ctx["pdir"] = str(pdir)
    # 未绑定 → cfg None（回落全局）
    chief.deliberate("继续写下一章", ctx)
    assert seen and not seen[-1]
    # 绑定 → cfg 带绑定键
    plan = json.load(open(pdir / "02-写作计划.json", encoding="utf-8"))
    plan["config"] = {"_provider_id": "prov_b", "_model": "model-b1"}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    chief.deliberate("继续写下一章", ctx)
    assert seen[-1].get("_provider_id") == "prov_b"
    assert seen[-1].get("_model") == "model-b1"


def test_script_storyboard_uses_bound_cfg(fake_providers, tmp_path, monkeypatch):
    """剧本分镜生成（v5.9.0 补的旁路）用本书绑定模型。"""
    pdir = tmp_path / SKEY
    (pdir / "04-正文").mkdir(parents=True)
    plan = {"scriptName": "绑定剧", "status": "in_progress",
            "config": {"_provider_id": "prov_b", "_model": "model-b1"},
            "scenes": [{"no": 1, "status": "completed", "kind": "内", "place": "办公室",
                        "time": "日", "fileName": "第001场.md", "title": "开场"}]}
    (pdir / "剧本计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (pdir / "04-正文" / "第001场.md").write_text("## 第001场 开场\n\n他推门而入。\n", encoding="utf-8")
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(tmp_path))
    seen = []

    def _shots(msgs, *a, **k):
        seen.append(k.get("cfg"))
        return {"shots": [{"shot": 1, "size": "中", "picture": "他推门而入",
                           "lines": "", "sfx": "", "secs": 3}]}

    monkeypatch.setattr(llm, "chat_json", _shots)
    res = script_writer.gen_storyboard(SKEY)
    assert res.get("ok") is True, res
    assert seen and seen[-1].get("_provider_id") == "prov_b"
    assert seen[-1].get("_model") == "model-b1"


def test_children_stage_passes_plan_cfg(fake_providers, tmp_path, monkeypatch):
    """儿童文体（resume 语义：cfg 来自 plan.config）把绑定传进 llm 调用。"""
    pdir = tmp_path / "c_20260101-090000-绑定故事"
    pdir.mkdir()
    plan = {"bookName": "绑定故事", "status": "in_progress",
            "config": {"_provider_id": "prov_b", "_model": "model-b1"},
            "meta": {}, "medium": ""}
    (pdir / "儿童文学计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(children_writer, "PROJECTS_DIR", str(tmp_path))
    seen = []
    monkeypatch.setattr(llm, "chat_json",
                        lambda msgs, *a, **k: (seen.append(k.get("cfg"))
                                               or {"title": "小灯", "sections": ["一个单元"], "outline": "概要"}))
    proj = children_writer.ChildrenProject(str(pdir), "绑定故事")
    cfg = plan["config"]
    children_writer._stage_outline(proj, str(pdir), cfg, plan, "7-9", "寓言故事")
    assert seen and seen[-1].get("_provider_id") == "prov_b"
    assert seen[-1].get("_model") == "model-b1"


# ---------------------------------------------------------------- ⑤ 创建即绑定（选择不落空）


def test_provider_into_writes_live_keys():
    """v5.9.0 修正：创建路径的 provider/model 选择必须写 resolve 活键（_provider_id/_model）。"""
    cfg = appmod._provider_into({}, {"provider_id": "prov_b", "model": "model-b1"})
    assert cfg["_provider_id"] == "prov_b"
    assert cfg["_model"] == "model-b1"
    assert cfg["_model_bound"] is True
    assert cfg["defaults"]["provider_id"] == "prov_b"   # 旧键保留兼容


def test_create_rejects_image_provider_before_creating_directory(client, fake_providers, tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    r = client.post("/api/create", json={
        "medium": "children", "idea": "一只小猫的冒险", "title": "小猫冒险",
        "provider_id": "prov_img", "model": "image-xl"})
    assert r.status_code == 400
    assert "图像" in r.json()["error"] and "文本" in r.json()["error"]
    assert list(tmp_path.iterdir()) == []


def test_create_rejects_half_model_binding_before_creating_directory(client, fake_providers, tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    r = client.post("/api/create", json={
        "medium": "children", "idea": "一只小猫的冒险", "title": "小猫冒险",
        "provider_id": "prov_b"})
    assert r.status_code == 400
    assert "未指定模型" in r.json()["error"]
    assert list(tmp_path.iterdir()) == []


def test_create_children_carries_binding_into_plan(client, fake_providers, tmp_path, monkeypatch):
    """/api/create（children 经 _spawn_simple_writer）把选择写进计划 config——新建即绑定。"""
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(children_writer, "PROJECTS_DIR", str(tmp_path))
    r = client.post("/api/create", json={
        "medium": "children", "idea": "一只小猫的冒险", "title": "小猫冒险",
        "provider_id": "prov_b", "model": "model-b1"})
    assert r.status_code == 200
    key = r.json()["key"]
    import time
    plan_fp = tmp_path / key / "儿童文学计划.json"
    for _ in range(100):
        if plan_fp.exists():
            break
        time.sleep(0.02)
    plan = json.load(open(plan_fp, encoding="utf-8"))
    assert plan["config"]["_provider_id"] == "prov_b"
    assert plan["config"]["_model"] == "model-b1"
    assert plan["config"]["_model_bound"] is True


def test_create_novel_skeleton_carries_binding(client, fake_providers, tmp_path, monkeypatch):
    """/api/create（小说骨架同步落盘）把选择写进 02-写作计划.json config。"""
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(appmod.workflow, "gen_titles",
                        lambda cfg, n=3: [{"title": "雨夜书", "technique": "", "why": ""}])
    r = client.post("/api/create", json={
        "medium": "novel", "idea": "一个关于灯塔的故事", "provider_id": "prov_b", "model": "model-b1"})
    assert r.status_code == 200
    key = r.json().get("key") or r.json().get("pid") or r.json().get("project")
    plan = json.load(open(tmp_path / key / "02-写作计划.json", encoding="utf-8"))
    assert plan["config"]["_provider_id"] == "prov_b"
    assert plan["config"]["_model"] == "model-b1"
    assert plan["config"]["_model_bound"] is True


# ---------------------------------------------------------------- ⑥ AST 静态门禁（死规律护栏）

ENGINE_FILES = [
    "bot/writer.py", "bot/longform.py", "bot/chief.py",
    "bot/children_writer.py", "bot/news_writer.py", "bot/copy_writer.py",
    "bot/script_writer.py", "bot/poetry_writer.py", "bot/prose_writer.py",
    "bot/thesis_writer.py", "bot/other_writer.py", "bot/assist.py",
    "bot/planner.py", "bot/drama_adapter.py", "bot/workflow.py",
    "bot/audit.py", "bot/chatsum.py", "bot/webflow.py", "bot/nlu.py",
]


def test_engine_llm_calls_all_pass_cfg():
    """死规律静态护栏：引擎与各文体 writer 里每个 llm.chat/chat_json/chat_stream 调用点
    都必须传 cfg（书级绑定的唯一入口）。新增调用点漏传 cfg → 本用例红。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    misses = []
    for rel in ENGINE_FILES:
        path = os.path.join(root, rel)
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and f.attr in ("chat", "chat_json", "chat_stream")):
                continue
            if not (isinstance(f.value, ast.Name) and f.value.id == "llm"):
                continue
            if not any(kw.arg == "cfg" for kw in node.keywords):
                misses.append(f"{rel}:{node.lineno}")
    assert not misses, f"引擎 LLM 调用缺 cfg 旁路（死规律违例）：{misses}"


def test_resolve_prefers_book_binding_over_global():
    """providers.resolve 第一优先级消费 cfg._provider_id/_model——书级绑定压过全局默认。"""
    data = copy.deepcopy(PROVIDERS_DATA)
    old = providers.load_data
    try:
        providers.load_data = lambda: copy.deepcopy(data)   # type: ignore
        p, m = providers.resolve({"_provider_id": "prov_b", "_model": "model-b1"})
        assert p["id"] == "prov_b" and m == "model-b1"
        # 供应商被删 → 安全回落全局默认（prov_a），绝不裸抛
        data["providers"] = [x for x in data["providers"] if x["id"] != "prov_b"]
        p2, m2 = providers.resolve({"_provider_id": "prov_b", "_model": "model-b1"})
        assert p2["id"] == "prov_a" and m2 == "model-a1"
    finally:
        providers.load_data = old   # type: ignore


# ---------------------------------------------------------------- ⑦ 旁路链路补齐


def test_chief_cfg_supports_non_novel_and_preserves_strict_flag(tmp_path):
    pdir = tmp_path / SKEY
    pdir.mkdir()
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    (pdir / "剧本计划.json").write_text(json.dumps({"config": cfg}, ensure_ascii=False),
                                      encoding="utf-8")
    assert chief._book_cfg_from_ctx({"pdir": str(pdir)}) == cfg


def test_planner_uses_book_cfg(monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "chat_json", lambda msgs, **kw: (
        seen.append(kw.get("cfg")) or {"steps": [{"action": "query_status"}]}))
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    assert planner.plan_actions("汇报进度", cfg=cfg)["steps"]
    assert seen == [cfg]


def test_planner_does_not_hide_invalid_binding(monkeypatch):
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}

    def invalid_binding(messages, **kw):
        raise providers.ProviderError("作品绑定的供应商已停用：供应商B")

    monkeypatch.setattr(llm, "chat_json", invalid_binding)
    with pytest.raises(providers.ProviderError, match="已停用"):
        planner.plan_actions("汇报进度", cfg=cfg)


def test_nonnovel_attached_planner_uses_plan_cfg(client, tmp_path, monkeypatch):
    key = "c_20260101-090000-绑定故事"
    pdir = tmp_path / key
    pdir.mkdir()
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    (pdir / "儿童文学计划.json").write_text(
        json.dumps({"bookName": "绑定故事", "status": "in_progress", "config": cfg,
                    "outline": {"sections": ["出发"]}, "meta": {}}, ensure_ascii=False),
        encoding="utf-8")
    sid = "s-nonnovel-planner-cfg"
    monkeypatch.setitem(appmod.SESSIONS, sid, {"id": sid, "state": "attached", "project": key})
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    monkeypatch.setattr(appmod.chief_mod, "deliberate", lambda *a, **k: None)
    seen = []

    def fake_plan(msg, ctx, cfg=None):
        seen.append(cfg)
        return {"steps": [{"action": "query_status"}]}

    monkeypatch.setattr(planner, "plan_actions", fake_plan)
    r = client.post("/api/chat", json={"session_id": sid, "message": "汇报进度", "key": key})
    assert r.status_code == 200, r.text
    assert seen == [cfg]


def test_nonnovel_attached_invalid_binding_returns_400(client, tmp_path, monkeypatch):
    key = "c_20260101-090000-失效绑定"
    pdir = tmp_path / key
    pdir.mkdir()
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    (pdir / "儿童文学计划.json").write_text(
        json.dumps({"bookName": "失效绑定", "status": "in_progress", "config": cfg,
                    "outline": {"sections": ["出发"]}, "meta": {}}, ensure_ascii=False),
        encoding="utf-8")
    sid = "s-nonnovel-invalid-binding"
    monkeypatch.setitem(appmod.SESSIONS, sid, {"id": sid, "state": "attached", "project": key})
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    monkeypatch.setattr(appmod.chief_mod, "deliberate", lambda *a, **k: None)
    seen = []

    def invalid_plan(msg, ctx, cfg=None):
        seen.append(cfg)
        if not cfg.get("_model_bound"):
            raise AssertionError("非小说 Planner 未读到书级绑定")
        raise providers.ProviderError("作品绑定的供应商已停用：供应商B")

    monkeypatch.setattr(planner, "plan_actions", invalid_plan)
    monkeypatch.setattr(nlu, "parse_intent", lambda *a, **k: pytest.fail("严格绑定错误不得回退 NLU"))
    r = client.post("/api/chat", json={"session_id": sid, "message": "汇报进度", "key": key})
    assert r.status_code == 400, r.text
    assert "已停用" in r.json()["error"]
    assert seen == [cfg]


def test_nlu_uses_book_cfg_and_does_not_hide_invalid_binding(monkeypatch):
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    seen = []

    def fake_chat_json(messages, **kw):
        seen.append(kw.get("cfg"))
        return {"intent": "query_status", "params": {}, "confidence": 0.9}

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    assert nlu.parse_intent("汇报进度", cfg=cfg)["intent"] == "query_status"
    assert seen == [cfg]

    def invalid_binding(messages, **kw):
        raise providers.ProviderError("作品绑定的供应商已停用：供应商B")

    monkeypatch.setattr(llm, "chat_json", invalid_binding)
    with pytest.raises(providers.ProviderError, match="已停用"):
        nlu.parse_intent("汇报进度", cfg=cfg)


def test_agent_reply_and_group_use_book_cfg(monkeypatch, tmp_path):
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    seen = []
    monkeypatch.setattr(llm, "chat", lambda msgs, **kw: seen.append(kw.get("cfg")) or "建议")
    monkeypatch.setattr(assist.llm, "chat", lambda msgs, **kw: seen.append(kw.get("cfg")) or "建议")
    monkeypatch.setattr(appmod.llm, "chat", lambda msgs, **kw: seen.append(kw.get("cfg")) or "建议")
    monkeypatch.setattr(assist, "_proj", lambda *a: type("P", (), {"plan": lambda self: {"config": cfg}})())
    monkeypatch.setattr(assist, "_medium_context", lambda *a: "上下文")
    monkeypatch.setattr(assist, "_work_name", lambda *a: "绑定书")
    monkeypatch.setattr(agents, "_all_agents", lambda: [{"agent": "editor", "label": "总编"}])
    assert "总编" in assist.agent_reply(KEY, "novel", "@总编 怎么改")
    assert seen and seen[-1].get("_provider_id") == "prov_b"
    seen.clear()
    monkeypatch.setattr(appmod.agents, "bindings_view", lambda: {"agents": [
        {"key": "editor", "label": "总编", "stage": "review", "desc": "审稿"}]})
    appmod._chat_group_llm({}, "上下文", "怎么改", ["总编"], "绑定书", cfg=cfg)
    assert len(seen) == 1
    assert seen[0].get("_provider_id") == "prov_b" and seen[0].get("_model_bound") is True


def test_selection_route_uses_book_cfg(client, tmp_path, monkeypatch):
    _mk_novel(tmp_path, monkeypatch, {"_provider_id": "prov_b", "_model": "model-b1",
                                    "_model_bound": True})
    seen = []
    monkeypatch.setattr(selection_ai, "run_stream",
                        lambda *a, **kw: seen.append(kw.get("cfg")) or "改后")
    r = client.post("/api/chapter/selection-ai", json={
        "key": KEY, "action": "fix", "selection": "他关掉收音机"})
    assert r.status_code == 200
    assert "改后" in r.text
    assert seen[0]["_model_bound"] is True


def test_drama_adapter_llm_calls_use_book_cfg():
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    seen = []

    class FakeLLM:
        def chat(self, msgs, **kw):
            seen.append(kw.get("cfg"))
            if kw.get("stage") == "writing":
                return "**场1：内 房 日**\n甲：开始。\n乙：好。\n**场2：外 街 夜**\n甲：走吧。"
            if kw.get("max_tokens") == 2000:
                return '{"items":[{"item":"核心设定","keyword":"灯塔","kind":"设定"}]}'
            return "# 改编圣经\n" + "灯塔是全书核心设定，人物与世界观不得更改。" * 10

    fake = FakeLLM()
    material = {"digest": "灯塔故事", "bible": "灯塔", "season": "起承转合", "outline": "大纲"}
    bible = drama_adapter.build_bible(fake, material, "灯塔", "短剧", cfg=cfg)
    assert len(bible) > 200
    fidelity = drama_adapter.build_fidelity(fake, material, "灯塔", cfg=cfg)
    assert fidelity and fidelity[0]["keyword"] == "灯塔"
    messages = drama_adapter.build_episode_messages(
        bible=bible, ep={"no": 1, "chapters": [1]}, total=1, source="正文", beat="要点",
        prev_tail="", form_label="短剧", novel_name="灯塔",
        min_chars=1, min_headings=1, min_lines=1)
    body, why, _ = drama_adapter.generate_episode(fake, messages, cfg=cfg,
                                                   min_chars=1, min_headings=1, min_lines=1)
    assert body and not why
    assert seen == [cfg, cfg, cfg]


def test_pause_then_continue_cancels_pending_pause(client, tmp_path, monkeypatch):
    _mk_novel(tmp_path, monkeypatch)
    writer.mark_pipeline(KEY, True)
    writer._set_book_running(KEY, True)
    try:
        assert client.post("/api/pause", json={"key": KEY}).json()["paused"] is True
        st = appmod._get_project(KEY).status()
        assert st["running"] is True and st["paused"] is True
        assert st["pausedReason"] == "pause_requested"
        r = client.post("/api/continue", json={"key": KEY})
        assert r.status_code == 200 and r.json()["started"] is False
        assert r.json()["running"] is True and r.json()["paused"] is False
        st = appmod._get_project(KEY).status()
        assert st["paused"] is False and st["state"] == "in_progress"
    finally:
        writer._set_book_running(KEY, False)
        writer.mark_pipeline(KEY, False)


def test_status_does_not_let_running_journal_clear_explicit_pause(client, tmp_path, monkeypatch):
    _mk_novel(tmp_path, monkeypatch)
    proj = appmod._get_project(KEY)
    plan = proj.plan()
    plan["status"] = "paused"
    plan["meta"] = {"paused": True, "pausedReason": "pause_requested"}
    proj.save_plan(plan)
    appmod._job_start(KEY, kind="resume", detail="测试运行档案")
    st = client.get("/api/status", params={"key": KEY}).json()
    assert st["paused"] is True and st["pausedReason"] == "pause_requested"
    assert st["running"] is False


def test_resume_failure_restores_pause_and_continue_reports_it(tmp_path, monkeypatch):
    _mk_novel(tmp_path, monkeypatch)
    proj = appmod._get_project(KEY)
    plan = proj.plan()
    plan["status"] = "paused"
    plan["meta"] = {"paused": True, "pausedReason": "pause_requested"}
    proj.save_plan(plan)
    monkeypatch.setattr(writer, "start_writing", lambda *a, **k: False)
    assert writer.resume_book(KEY) is False
    restored = proj.plan()
    assert restored["status"] == "paused"
    assert restored["meta"]["paused"] is True
    assert restored["meta"]["pausedReason"] == "pause_requested"
    r = appmod.api_continue({"key": KEY})
    assert r["started"] is False and r["running"] is False
    assert r["paused"] is True and r["state"] == "paused"


def test_continue_does_not_clear_collab_checkpoint_while_thread_finishes(client, tmp_path, monkeypatch):
    _mk_novel(tmp_path, monkeypatch)
    proj = appmod._get_project(KEY)
    plan = proj.plan()
    plan["status"] = "paused"
    plan["meta"] = {"paused": True, "pausedReason": "await_collab:1", "collab_review": True}
    proj.save_plan(plan)
    writer.mark_pipeline(KEY, True)
    writer._set_book_running(KEY, True)
    try:
        r = client.post("/api/continue", json={"key": KEY})
        assert r.status_code == 200 and r.json()["started"] is False
        assert r.json()["running"] is True and r.json()["paused"] is True
        st = proj.status()
        assert st["pausedReason"] == "await_collab:1" and st["paused"] is True
    finally:
        writer._set_book_running(KEY, False)
        writer.mark_pipeline(KEY, False)


def test_continue_adopts_collab_checkpoint_after_thread_exits(client, tmp_path, monkeypatch):
    _mk_novel(tmp_path, monkeypatch)
    proj = appmod._get_project(KEY)
    plan = proj.plan()
    plan["status"] = "paused"
    plan["meta"] = {"paused": True, "pausedReason": "await_collab:1", "collab_review": True}
    proj.save_plan(plan)
    seen = []

    def fake_resume(key, batch_size=None):
        seen.append((key, batch_size))
        return True

    monkeypatch.setattr(writer, "resume_book", fake_resume)
    monkeypatch.setattr(writer, "start_writing", lambda *a, **k: pytest.fail("协作采用必须走 resume_book"))
    r = client.post("/api/continue", json={"key": KEY})
    assert r.status_code == 200 and r.json()["started"] is True
    assert "第 1 章已按你确认采用" in r.json()["msg"]
    assert seen == [(KEY, None)]


def test_nonnovel_status_and_continue_expose_pause_state(client, tmp_path, monkeypatch):
    key = "c_20260101-090000-暂停故事"
    pdir = tmp_path / key
    pdir.mkdir()
    plan = {"bookName": "暂停故事", "status": "in_progress", "config": {}, "meta": {}}
    (pdir / "儿童文学计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(children_writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(appmod, "_nonnovel_module", lambda k: children_writer if k == key else None)
    proj = children_writer.ChildrenProject(str(pdir), "暂停故事")
    monkeypatch.setattr(children_writer, "get_project", lambda k: proj)
    writer.mark_pipeline(key, True)
    try:
        st = proj.status()
        assert st["running"] is True and st["paused"] is False
        assert client.post("/api/pause", json={"key": key}).status_code == 200
        st = children_writer.get_project(key).status()
        assert st["paused"] is True and st["pausedReason"] == "pause_requested"
        r = client.post("/api/continue", json={"key": key})
        assert r.json()["started"] is False and r.json()["paused"] is False
        assert proj.status()["paused"] is False
    finally:
        writer.mark_pipeline(key, False)


def test_nonnovel_auto_stop_label_is_need_continue(client, tmp_path, monkeypatch):
    _mk_novel(tmp_path, monkeypatch)
    key = "c_20260101-090000-自动停机故事"
    pdir = tmp_path / key
    pdir.mkdir()
    plan = {"bookName": "自动停机故事", "status": "paused", "config": {}, "meta": {}}
    (pdir / "儿童文学计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    res = client.get("/api/status", params={"key": key})
    assert res.status_code == 200, res.text
    st = res.json()
    assert st["running"] is False and st["paused"] is False
    assert st["status_label"] == "待继续"


def test_project_instances_share_plan_lock(tmp_path, monkeypatch):
    pdir = _mk_novel(tmp_path, monkeypatch)
    a = writer.Project(str(pdir), "绑定书")
    b = writer.Project(str(pdir), "绑定书")
    assert a.lock is b.lock


def test_project_lock_survives_directory_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    old = tmp_path / "old-key"
    new = tmp_path / "new-key"
    old.mkdir()
    a = writer.Project(str(old), "旧书")
    shared = a.lock
    os.rename(old, new)
    monkeypatch.setattr(writer, "resolve_key", lambda key: "new-key" if key == "old-key" else key)
    b = writer.Project(str(new), "新书")
    assert b.lock is shared
    a._follow_rename()
    assert a.path == str(new) and a.lock is shared


def test_rename_map_records_concurrent_renames_without_lost_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "RENAME_MAP_FILE", str(tmp_path / "rename_map.json"))
    writer._RENAME_MAP_CACHE.update(mtime=None, map={})
    threads = [threading.Thread(target=writer.record_rename, args=(f"old-{i}", f"new-{i}"))
               for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert writer._load_rename_map() == {f"old-{i}": f"new-{i}" for i in range(24)}


def test_set_image_default_does_not_fill_text_default_slot(client, fake_providers):
    r = client.post("/api/providers/prov_img/default")
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "image" and body["default_image_provider"] == "prov_img"
    assert body["default_provider"] == ""


def test_claim_pipeline_is_atomic():
    from concurrent.futures import ThreadPoolExecutor
    key = "s_claim-pipeline"
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed = list(pool.map(lambda _: writer.claim_pipeline(key), range(8)))
        assert claimed.count(True) == 1
    finally:
        writer.mark_pipeline(key, False)


def test_ghost_and_quickwrite_use_book_cfg(client, tmp_path, monkeypatch):
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    _mk_novel(tmp_path, monkeypatch, cfg)
    seen = []
    monkeypatch.setattr(ghost, "check_rate", lambda key: 0.0)
    monkeypatch.setattr(ghost, "run", lambda *a, **kw: seen.append(kw.get("cfg")) or {
        "text": "续写", "mode": "beat", "label": "续一拍", "chars": 2, "temperature": 0.8})
    monkeypatch.setattr(ghost, "run_stream", lambda *a, **kw: (
        kw["on_delta"]("续写") or seen.append(kw.get("cfg")) or "续写"))
    r = client.post("/api/ghost", json={"key": KEY, "text": "他推开门，"})
    assert r.status_code == 200 and seen[-1] == cfg
    r = client.post("/api/quickwrite", json={"key": KEY, "text": "他推开门，"})
    assert r.status_code == 200 and '"ok": true' in r.text
    assert seen[-1] == cfg


def test_flow_expand_uses_book_cfg(client, tmp_path, monkeypatch):
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    _mk_novel(tmp_path, monkeypatch, cfg)
    seen = []

    def fake_chat(messages, **kw):
        seen.append(kw.get("cfg"))
        return '{"items":[{"title":"新想法","text":"推进冲突"}]}'

    monkeypatch.setattr(webflow.llm, "chat", fake_chat)
    r = client.post("/api/flow/expand", json={"key": KEY, "text": "雨夜钥匙"})
    assert r.status_code == 200 and seen == [cfg]


def test_audit_arbitration_uses_book_cfg(tmp_path, monkeypatch):
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    _mk_novel(tmp_path, monkeypatch, cfg)
    seen = []

    def fake_arbitrate(*args, **kw):
        seen.append(kw.get("cfg"))
        return {}

    monkeypatch.setattr(audit, "_llm_arbitrate", fake_arbitrate)
    monkeypatch.setattr(audit, "_save_report", lambda *a: None)
    from bot import continuity, engine_check, steps
    monkeypatch.setattr(continuity, "run_project", lambda pdir: {"findings": []})
    monkeypatch.setattr(engine_check, "scan", lambda proj: {})
    monkeypatch.setattr(steps, "emit", lambda *a, **kw: None)
    assert audit.run_audit(KEY) is not None
    assert seen == [cfg]


def test_chat_summary_uses_book_cfg(tmp_path, monkeypatch):
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    seen = []

    class FakeLLM:
        def chat(self, messages, **kw):
            seen.append(kw.get("cfg"))
            return "创作进度摘要"

    monkeypatch.setattr(chatsum, "_llm_available", lambda: FakeLLM())
    r = chatsum.maybe_compress(str(tmp_path), [{"role": "user", "content": "剧情" * 400000}],
                               cfg=cfg)
    assert r["compressed"] is True and seen == [cfg]


def test_chat_stream_passes_book_cfg_to_summary(client, tmp_path, monkeypatch):
    from bot import v2api

    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True}
    _mk_novel(tmp_path, monkeypatch, cfg)
    monkeypatch.setattr(v2api, "_follow_project", lambda *a, **kw: None)
    endpoint = next(r.endpoint for r in appmod.app.routes
                    if getattr(r, "path", "") == "/api/chat/stream")
    cells = dict(zip(endpoint.__code__.co_freevars, endpoint.__closure__ or (), strict=False))
    monkeypatch.setattr(cells["chat_func"], "cell_contents",
                        lambda sid, message, key=None, **kw: "收到")
    monkeypatch.setattr(providers, "usable", lambda: True)
    seen = []

    def fake_compress(pdir, history, cfg=None):
        seen.append(cfg)
        return {"compressed": False, "history": history}

    monkeypatch.setattr(chatsum, "maybe_compress", fake_compress)
    r = client.post("/api/chat/stream", json={
        "key": KEY, "message": "继续创作", "session_id": "s-v59-summary-cfg"})
    assert r.status_code == 200 and '"ok": true' in r.text
    assert seen == [cfg]


def test_score_override_never_strips_strict_binding():
    cfg = {"_provider_id": "prov_b", "_model": "model-b1", "_model_bound": True,
           "defaults": {"score_provider_id": "prov_a", "score_model": "model-a1"}}
    assert workflow.score_model_cfg(cfg) is None
    legacy = {"defaults": {"score_provider_id": "prov_a", "score_model": "model-a1"}}
    assert workflow.score_model_cfg(legacy) == {
        "defaults": {"provider_id": "prov_a", "model": "model-a1"}}
