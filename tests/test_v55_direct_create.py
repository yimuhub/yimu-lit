"""v5.5.2 「直接开聊」建书闭环回归：无绑定会话一段话 → 真的建书 → 会话绑定新书 →
后台规划自动定名开写 → 交接汇报；已绑书防呆拒绝；失败走 v5.5.1 韧性自愈；
mock 模式（?mock=1）不穿透到真实 LLM。

背景：v5.5.1 已有前端双入口，「直接开聊」进入无绑定会话（payload 不带 key），
用户说「我想写一本XX」时服务端只引导不建书。v5.5.2 打通：
· chief 新动作 create_book（未绑定作品时可用，idea/偏好抽取，缺省用合理默认）；
· app.py::_chat_unbound —— idle 无绑定会话先过总编辑，create_book 决策 →
  _create_book_from_chat（复用 /api/create 的骨架 + 后台规划链 + 极速自动定名约定）；
· 会话立即重绑新书占位 key（pick_title 改名后走 rename 别名自动跟随）；
· 直聊轮次（无 key）补记进新书 chat_log，保住总编辑上下文与历史回放。

全部用例跑在 tmp_path，不碰真实数据、不连网（conftest 已 stub llm.*）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import chief as chief_mod
from bot import steps as steps_mod
from bot import writer

PLAN_FILE = "02-写作计划.json"
TS = "20261121-210000"

IDEA_MSG = "我想写一本都市悬疑小说，主角是夜班出租车司机，每一单载客都藏着一个秘密"

_CREATE_DECISION = {
    "understanding": "作者在直接开聊里表达了想写的书：夜班出租车司机的都市悬疑",
    "reply": "好，我来安排——先按都市悬疑建书，缺的设定用合理默认。",
    "actions": [{"action": "create_book",
                 "params": {"idea": "夜班出租车司机，每一单载客都藏着一个秘密",
                            "preferences": {"genre": "都市悬疑", "chapters": 45}}}],
    "confidence": 0.9}


# ---------------------------------------------------------------- 脚手架

@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """projects/ 与 data/rename_map.json 全部落 tmp；会话/索引/缓存隔离。"""
    projects = tmp_path / "projects"
    projects.mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir(parents=True)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(projects))
    monkeypatch.setattr(writer, "RENAME_MAP_FILE", str(data / "rename_map.json"))
    monkeypatch.setattr(writer, "_RENAME_MAP_CACHE", {"mtime": None, "map": {}})
    yield projects


@pytest.fixture()
def client(sandbox):
    return TestClient(appmod.app)


def _plan_of(projects, key):
    return json.load(open(projects / key / PLAN_FILE, encoding="utf-8"))


def _chat_rows(projects, key):
    fp = projects / key / "chat_log.jsonl"
    if not fp.exists():
        return []
    return [json.loads(x) for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()]


def _stub_planning_chain(monkeypatch, gen_titles_error=None, total=45):
    """把 /api/create 后台规划链里的 LLM 环节换成确定性 stub（不连网、不等 1-2 分钟）。

    · fill_defaults → 原样透传（只补 chapters）
    · gen_titles → 固定候选（或抛错模拟 LLM 524）
    · writer.create_project → 在骨架目录里写一份带 total 章 pending 章卡的完整计划
    · writer.start_writing → 只登记落在哪个 key 上（pick_title 改名后会以新 key 调用）
    """
    monkeypatch.setattr(
        appmod.workflow, "fill_defaults",
        lambda cfg: {**cfg, "chapters": int(cfg.get("chapters") or total)})

    def _gen_titles(cfg, n=8):
        if gen_titles_error:
            raise gen_titles_error
        return [{"title": "夜班出租车司机掉的每一个秘密", "technique": "悬念", "why": ""},
                {"title": "后座没有人", "technique": "反常", "why": ""}]

    monkeypatch.setattr(appmod.workflow, "gen_titles", _gen_titles)

    def _fake_create(cfg, titles, pdir=None, name=None):
        assert pdir and os.path.isdir(pdir), "create_project 必须写回骨架目录（不另起书）"
        plan = {"novelName": (titles[0]["title"] if titles else "未命名"),
                "status": "planning", "totalChapters": total, "totalWords": 0,
                "minWordsPerChapter": 3000, "config": dict(cfg), "titles": titles,
                "meta": {"planning_running": True},
                "chapters": [{"chapterNumber": n, "title": f"第{n}章", "status": "pending",
                              "wordCount": 0} for n in range(1, total + 1)]}
        with open(os.path.join(pdir, PLAN_FILE), "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False)
        return writer.Project(pdir, os.path.basename(pdir))

    monkeypatch.setattr(writer, "create_project", _fake_create)

    started = []
    monkeypatch.setattr(writer, "start_writing",
                        lambda proj, batch_size=None:
                        started.append(os.path.basename(proj.path)) or True)
    return started


def _wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------- 1. 直聊建书闭环

def test_unbound_direct_message_creates_book_binds_session_and_auto_titles(sandbox, monkeypatch):
    """核心闭环：无绑定会话说「我想写一本XX」→ create_book → tmp 目录建书 →
    会话重绑新 key → 后台规划自动采用首个候选定名（rename 别名）→ 自动开写 →
    chat_log 落作者原话 + 建书交接。"""
    started = _stub_planning_chain(monkeypatch)
    monkeypatch.setattr(chief_mod, "deliberate", lambda msg, ctx: dict(_CREATE_DECISION))
    monkeypatch.setattr(steps_mod, "emit", lambda *a, **k: None)

    s_key = "sid-dc-create"
    reply = appmod.chat(s_key, IDEA_MSG, None)

    # 回复：已建书 + 书名（占位短标题）+ 规划章数 + 下一步动作
    assert "已建书" in reply and "《夜班出租车司机》" in reply
    assert "45" in reply
    assert "继续" in reply and "改名" in reply
    # 会话立即重绑：占位 key + attached（不再是无绑定的 idle）
    s = appmod.SESSIONS[s_key]
    assert s["state"] == "attached" and s.get("project")
    base = s["project"]

    # 后台规划线程（stub 后毫秒级）：gen_titles → create_project 写回同一目录
    # → 极速约定：自动采用首个候选 pick_title（改名 + 别名）→ start_writing 落新 key
    assert _wait_for(lambda: bool(started)), "规划完成后必须自动开写"
    assert not (sandbox / base).exists()              # 占位目录已被 pick_title os.rename 搬走
    new_key = started[-1]
    assert (sandbox / new_key / PLAN_FILE).exists()   # 骨架→完整计划都落在新家
    assert new_key != base and new_key.endswith("夜班出租车司机掉的每一个秘密")
    assert writer._load_rename_map().get(base) == new_key
    # 旧占位 key 的请求走别名落新家（缓存 Project 实例读盘时 _follow_rename 懒跟随）
    proj = appmod._get_project(base)
    assert proj is not None
    proj.plan()
    assert os.path.basename(proj.path) == new_key
    plan = _plan_of(sandbox, new_key)
    assert plan["novelName"] == "夜班出租车司机掉的每一个秘密"
    assert len(plan.get("chapters") or []) == 45      # 45 章 pending
    assert plan["meta"]["awaiting_title"] is False    # 不拦选名
    assert len(plan["meta"].get("title_candidates") or []) == 2   # 候选照常落盘供改名

    # 交接留档：作者原话 + 建书回复 + pick_title 的 handoff 三样都在新书的 chat_log 里
    rows = _chat_rows(sandbox, new_key)
    texts = [str(r.get("content") or "") for r in rows]
    assert any(t == IDEA_MSG for t in texts)
    assert any("已建书" in t for t in texts)
    handoff = [r for r in rows if r.get("handoff")]
    assert handoff and "夜班出租车司机掉的每一个秘密" in handoff[-1]["content"]

    # 同一会话下一条消息（仍然无 key）：落到新书 attached 路由，总编辑看到的是真书
    seen = {}
    monkeypatch.setattr(chief_mod, "build_context",
                        lambda proj, st, key, **kw: seen.update(key=key, name=st.get("name")) or {})
    monkeypatch.setattr(chief_mod, "deliberate",
                        lambda msg, ctx: {"understanding": "进度", "reply":
                                          f"《{seen.get('name')}》正在准备", "actions": [],
                                          "confidence": 0.9})
    appmod.SESSIONS[s_key]["project"] = base           # 模拟直聊会话仍持占位 key
    reply2 = appmod.chat(s_key, "进度", None)
    assert seen.get("key") == new_key                  # canonical key 规范化生效
    assert "夜班出租车司机掉的每一个秘密" in reply2


def test_unbound_discuss_reply_creates_nothing(sandbox, monkeypatch):
    """直接开聊里的纯讨论/问用法：总编辑只说话，不建书，会话保持无绑定。"""
    monkeypatch.setattr(chief_mod, "deliberate",
                        lambda msg, ctx: {"understanding": "问用法", "reply":
                                          "直接说想写什么就行，比如「写一本悬疑小说」。",
                                          "actions": [], "confidence": 0.9})
    reply = appmod.chat("sid-dc-chat", "你们这里能写什么类型的书？", None)
    assert "写一本悬疑小说" in reply
    s = appmod.SESSIONS["sid-dc-chat"]
    assert s["state"] == "idle" and not s.get("project")
    assert list(sandbox.iterdir()) == []               # 一本书都没建


def test_unbound_chief_unavailable_falls_back_to_options_flow(sandbox, monkeypatch):
    """总编辑不可用（无模型/解析失败 → None）：原样回落旧的主题提取配置菜单，
    直接开聊功能绝不能把老流程弄坏。"""
    monkeypatch.setattr(chief_mod, "deliberate", lambda msg, ctx: None)
    monkeypatch.setattr(appmod.workflow, "extract_config",
                        lambda msg, cfg=None: {"genre": "悬疑", "idea": "退休刑警被诬陷杀人",
                                               "chapters": 12})
    reply = appmod.chat("sid-dc-fallback", "写一部悬疑小说，退休刑警被诬陷杀人", None)
    assert "题材：悬疑" in reply and "【1】" in reply   # 老的 options 菜单
    s = appmod.SESSIONS["sid-dc-fallback"]
    assert s["state"] == "options" and not s.get("project")


def test_chief_create_book_prompt_contract():
    """提示词契约：create_book 已进动作表与系统提示词——模型必须知道何时建书、
    缺省怎么填、已绑书时禁用。钉住三处，防止提示词回退。"""
    assert "create_book" in chief_mod.ACTIONS
    desc = chief_mod.ACTIONS["create_book"]["desc"]
    assert "未绑定" in desc or "直接开聊" in desc
    assert "禁止" in desc                              # 已绑书禁用写进动作说明
    assert "直接开聊建书" in chief_mod._SYSTEM_PROMPT
    assert "合理默认" in chief_mod._SYSTEM_PROMPT
    # 参数归一：别名 + 平铺参数都能落进 params
    na = chief_mod._norm_action({"action": "create_novel", "idea": "x", "genre": "悬疑",
                                 "chapters": 45,
                                 "preferences": {"tone": "冷峻"}})
    assert na and na["action"] == "create_book"
    assert na["params"]["idea"] == "x" and na["params"]["genre"] == "悬疑"
    assert na["params"]["chapters"] == 45
    assert na["params"]["preferences"] == {"tone": "冷峻"}
    # 无绑定上下文：unbuilt 空书口径不进提示词，直聊指令进提示词
    txt = chief_mod._context_text(chief_mod.build_unbound_context())
    assert "直接开聊" in txt and "create_book" in txt
    assert "《未命名》" not in txt


# ---------------------------------------------------------------- 2. 已绑书防呆

def _mk_bound_book(projects, key=TS + "-旧书一本"):
    pdir = projects / key
    pdir.mkdir(parents=True)
    plan = {"novelName": "旧书一本", "status": "in_progress", "totalChapters": 10,
            "config": {"genre": "都市", "chapters": 10, "batch_size": "2"},
            "chapters": [{"chapterNumber": n, "title": f"第{n}章", "status": "pending",
                          "wordCount": 0} for n in range(1, 11)],
            "meta": {}}
    with open(pdir / PLAN_FILE, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False)
    return key


def test_bound_session_create_book_is_refused(sandbox, monkeypatch):
    """防呆：已绑定书时 create_book 一律拒绝并给指引——绝不能把当前会话悄悄换绑到新书。"""
    bound_key = _mk_bound_book(sandbox)
    monkeypatch.setattr(chief_mod, "deliberate",
                        lambda msg, ctx: {"understanding": "想再写一本",
                                          "reply": "我来建一本新书。",
                                          "actions": [{"action": "create_book",
                                                       "params": {"idea": "另一本书"}}],
                                          "confidence": 0.9})
    monkeypatch.setattr(steps_mod, "emit", lambda *a, **k: None)
    s = {"id": "s0", "state": "attached", "project": bound_key}
    appmod.SESSIONS["s0"] = s
    proj = appmod._get_project(bound_key)

    before = set(os.listdir(sandbox))
    reply = appmod.chat("s0", "我想再写一本职场小说", bound_key)
    after = set(os.listdir(sandbox))

    assert before == after                             # 没有建出任何新书
    assert s["project"] == bound_key                   # 会话绑定纹丝没动
    assert "旧书一本" in reply                         # 拒绝时点名当前书
    assert "直接开聊" in reply or "新建作品" in reply   # 给出替代路径


# ---------------------------------------------------------------- 3. 失败韧性

def test_direct_create_failure_self_heals_via_retry_planning(sandbox, monkeypatch):
    """建书规划被 LLM 524 打断：书落 failed + meta.error；作者说「继续」→
    复用 v5.5.1 _retry_planning_bg 在同一本书上重跑，绝不空书回答。"""
    _stub_planning_chain(monkeypatch, gen_titles_error=RuntimeError("provider 524 timeout"))
    monkeypatch.setattr(chief_mod, "deliberate", lambda msg, ctx: dict(_CREATE_DECISION))
    monkeypatch.setattr(steps_mod, "emit", lambda *a, **k: None)

    s_key = "sid-dc-fail"
    reply = appmod.chat(s_key, IDEA_MSG, None)
    assert "已建书" in reply                           # 建书请求本身成功（骨架落盘）
    base = appmod.SESSIONS[s_key]["project"]
    assert base

    # 后台线程把失败落盘（failed + error），随后 livejob 收口
    def _failed():
        try:
            p = _plan_of(sandbox, base)
        except Exception:
            return False
        return p.get("status") == "failed" and bool((p.get("meta") or {}).get("error"))
    assert _wait_for(_failed), "规划失败必须落盘 failed 状态（自愈的判据）"

    # 作者说「继续」：v5.5.1 韧性层接手（这里只验证接了 _retry_planning_bg，
    # 其本体行为已在 test_v55_title_handoff / test_v55_resilience 钉住）
    called = []
    monkeypatch.setattr(appmod, "_retry_planning_bg", lambda key: called.append(key) or True)
    reply2 = appmod.chat(s_key, "继续", None)
    assert called == [base]
    assert "重新启动" in reply2 and "规划" in reply2


# ---------------------------------------------------------------- 4. mock 模式不穿透

def test_mock_mode_direct_chat_never_reaches_backend(sandbox, client, monkeypatch):
    """?mock=1 的直接开聊必须止步于前端 mock 适配层：
    · api.js 的 streamChat 在 mock 开启时路由到 mockChatStream（请求根本不出浏览器）；
    · mock.js 覆盖了 /api/create（演示模式的建书走假数据）；
    · 即便有请求穿透到后端（无可用供应商），/api/chat/stream 也只回兜底话术，不建书。
    三条任一缺失都算「静默穿透真实 LLM」。"""
    static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "static", "v2")
    api_js = open(os.path.join(static_dir, "js", "core", "api.js"), encoding="utf-8").read()
    assert "mockChatStream" in api_js
    mock_js = open(os.path.join(static_dir, "mock", "mock.js"), encoding="utf-8").read()
    assert "mockChatStream" in mock_js                 # 直聊流式请求在 mock 层被拦截
    assert "/api\\/create$" in mock_js                 # 建书也走 mock 假数据（正则字面量 \/api\/create$）

    monkeypatch.setattr("bot.v2api._provider_ready", lambda: False)
    before = set(os.listdir(sandbox))
    res = client.post("/api/chat/stream", json={"message": IDEA_MSG, "session_id": "s-mock"})
    body = res.text
    assert "未配置大模型" in body                       # 兜底话术，不进 chat_func
    assert set(os.listdir(sandbox)) == before           # 没有建书、没有真实 LLM 调用


# ---------------------------------------------------------------- 5. 建书入口直连 /api/create 语义

def test_create_book_reuses_api_create_chain_semantics(sandbox, monkeypatch):
    """_create_book_from_chat 与 /api/create 同一条链：express 折入、idea→theme、
    章数进 totalChapters、供应商选择随会话贯穿。不复制建书逻辑。"""
    captured = []
    real_api_create = appmod.api_create

    def _spy(payload):
        captured.append(dict(payload))
        return real_api_create(payload)

    monkeypatch.setattr(appmod, "api_create", _spy)
    started = _stub_planning_chain(monkeypatch)
    monkeypatch.setattr(chief_mod, "deliberate", lambda msg, ctx: dict(_CREATE_DECISION))
    monkeypatch.setattr(steps_mod, "emit", lambda *a, **k: None)

    s = {"id": "sid-reuse", "state": "idle", "cfg": None, "project": None,
         "titles": None, "busy": False,
         "prov": {"_provider_id": "prov-1", "_model": "model-9"}}
    appmod.SESSIONS["sid-reuse"] = s
    reply, new_key = appmod._create_book_from_chat(
        s, {"idea": "夜班出租车司机，每一单载客都藏着一个秘密",
            "preferences": {"genre": "都市悬疑", "chapters": 45}},
        user_msg=IDEA_MSG)
    assert new_key and "已建书" in reply
    payload = captured[0]
    assert payload["idea"].startswith("夜班出租车司机")
    assert payload["genre"] == "都市悬疑"
    assert payload["total_chapters"] == 45
    assert payload["express"] == "express"             # 极速约定：不拦 awaiting_title
    assert payload["provider_id"] == "prov-1" and payload["model"] == "model-9"
    assert _wait_for(lambda: bool(started))
    plan = _plan_of(sandbox, started[-1])
    assert plan["config"].get("theme") == payload["idea"]   # idea 落 config.theme（/api/create 语义）
    assert int(plan.get("totalChapters") or 0) == 45


def test_create_book_without_idea_asks_instead_of_guessing(sandbox):
    """连「想写什么」都没有（create_book 决策缺 idea）：反问一句，绝不拿空 idea 建书。"""
    s = {"id": "sid-noidea", "state": "idle", "cfg": None, "project": None,
         "titles": None, "busy": False}
    appmod.SESSIONS["sid-noidea"] = s
    reply, key = appmod._create_book_from_chat(s, {"preferences": {"genre": "悬疑"}})
    assert key is None and "一句话" in reply
    assert s.get("project") is None                    # 会话没有被绑定
    assert list(sandbox.iterdir()) == []               # 目录一个都没建


# ---------------------------------------------------------------- 6. SSE 全链路

def test_stream_endpoint_direct_chat_creates_book_end_to_end(sandbox, client, monkeypatch):
    """生产链路原样走一遍：POST /api/chat/stream（无 key，directChat payload）
    → chat_func → 无绑定 chief → create_book → SSE done 帧给出建书回复。"""
    _stub_planning_chain(monkeypatch)
    monkeypatch.setattr(chief_mod, "deliberate", lambda msg, ctx: dict(_CREATE_DECISION))
    monkeypatch.setattr(steps_mod, "emit", lambda *a, **k: None)

    with client.stream("POST", "/api/chat/stream",
                       json={"message": IDEA_MSG, "session_id": "sid-sse"}) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())
    assert "event: start" in body and "event: done" in body
    assert "已建书" in body and "《夜班出租车司机》" in body
    s = appmod.SESSIONS["sid-sse"]
    assert s["state"] == "attached" and s.get("project")   # 会话已重绑新书

    def _planned_or_renamed():
        try:
            if not (sandbox / s["project"]).exists():
                return True            # 占位目录已被 pick_title 改名搬走
            return bool(_plan_of(sandbox, s["project"]).get("chapters"))
        except OSError:
            return True
    assert _wait_for(_planned_or_renamed)

# ---------------------------------------------------------------- 7. 规划在途时改名不被覆盖

def test_rename_during_planning_survives_auto_title(sandbox, monkeypatch):
    """直聊建书后、规划还在跑时作者说「改名X」（chief set_title → _apply_title 换 key）：
    规划完成必须沿用作者定名开写，绝不被首个引擎候选覆盖。
    注：直聊骨架 awaiting_title=False，_apply_title 只换 key 改计划书名；且真实
    create_project 的全新计划会冲掉 novelName/meta——作者定名的唯一可靠信号是
    create_project._sync_home 跟随改名后留下的目录名。"""
    started = []

    def _gen_titles_user_rename(cfg, n=8):
        # 模拟并发：规划线程跑到 gen_titles 时，作者已在聊天里抢先定名
        keys = [d for d in os.listdir(str(sandbox)) if not d.startswith(".")]
        assert len(keys) == 1
        res = appmod._apply_title(keys[0], "作者中途定名")
        assert res.get("ok") is True
        return [{"title": "引擎候选一", "technique": "", "why": ""},
                {"title": "引擎候选二", "technique": "", "why": ""}]

    monkeypatch.setattr(
        appmod.workflow, "fill_defaults",
        lambda cfg: {**cfg, "chapters": 45})
    monkeypatch.setattr(appmod.workflow, "gen_titles", _gen_titles_user_rename)

    def _fake_create(cfg, titles, pdir=None, name=None):
        # 复刻真实 create_project：_sync_home 跟随改名（这里直接落新家），
        # 全新计划 novelName=titles[0] 且不带 meta。
        proj = writer.Project(pdir, name or (titles[0]["title"] if titles else "未命名"))
        keys = [d for d in os.listdir(str(sandbox)) if not d.startswith(".")]
        proj.path = os.path.join(str(sandbox), keys[0])
        plan = {"novelName": (titles[0]["title"] if titles else "未命名"),
                "status": "planning", "totalChapters": 45,
                "config": dict(cfg), "titles": titles,
                "chapters": [{"chapterNumber": n, "title": f"第{n}章", "status": "pending",
                              "wordCount": 0} for n in range(1, 46)]}
        with open(os.path.join(proj.path, PLAN_FILE), "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False)
        return proj

    monkeypatch.setattr(writer, "create_project", _fake_create)
    monkeypatch.setattr(writer, "start_writing",
                        lambda proj, batch_size=None:
                        started.append(os.path.basename(proj.path)) or True)
    monkeypatch.setattr(chief_mod, "deliberate", lambda msg, ctx: dict(_CREATE_DECISION))
    monkeypatch.setattr(steps_mod, "emit", lambda *a, **k: None)

    s_key = "sid-dc-rename"
    reply = appmod.chat(s_key, IDEA_MSG, None)
    assert "已建书" in reply
    base = appmod.SESSIONS[s_key]["project"]

    assert _wait_for(lambda: bool(started))
    new_key = started[-1]
    assert new_key.endswith("作者中途定名")           # 作者定名存活
    plan = _plan_of(sandbox, new_key)
    assert plan["novelName"] == "作者中途定名"        # 没有被「引擎候选一」覆盖
    assert len(plan["meta"].get("title_candidates") or []) == 2   # 候选仍落盘备选
    assert plan["meta"]["awaiting_title"] is False
    # 作者在规划在途时的改名已把会话换绑到新 key；旧占位 key 仍是别名
    assert appmod.SESSIONS[s_key]["project"] == new_key
    assert writer._load_rename_map().get(base) == new_key
