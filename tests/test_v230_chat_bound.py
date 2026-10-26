# v2.3.0：对话绑定当前作品（绝不误起新书）+ 标题体系 + 体检接口。
#
# 回归背景：服务重启后 SESSIONS 清空、state=idle，用户在已打开的书里说「继续」，
# 旧逻辑把它当成新主题走 extract_config → 起了一本全新小说。现在 v2 每条消息带 key，
# idle + 有效 key → 绑定本书（attached），一切消息围绕这本书路由。
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import security, writer

KEY = "20260101-000000-旧书名"


def _mk_proj(tmp_path, name="旧书名", done=3):
    pdir = tmp_path / "20260101-000000-旧书名"
    pdir.mkdir()
    (pdir / "02-写作计划.json").write_text(json.dumps({
        "novelName": name, "status": "in_progress",
        "chapters": [{"chapterNumber": i, "title": f"第{i}章",
                      "status": "completed" if i <= done else "pending",
                      "wordCount": 3000 if i <= done else None}
                     for i in range(1, 6)],
    }, ensure_ascii=False), encoding="utf-8")
    return writer.Project(str(pdir), KEY)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    yield
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """一本真实落盘的书 + _get_project 指向它；extract_config 一旦被调用就炸（新书红线）。"""
    proj = _mk_proj(tmp_path)
    monkeypatch.setattr(appmod, "_get_project",
                        lambda key: proj if key == KEY else None)
    monkeypatch.setattr(appmod.writer, "PROJECTS_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise AssertionError("attached 会话绝不允许进新主题提取")
    monkeypatch.setattr(appmod.workflow, "extract_config", _boom)
    return proj


# ---------------------------------------------------------------- 绑定与路由

def test_idle_with_key_binds_book_not_new_novel(env):
    r = appmod.chat("s1", "继续", KEY)
    assert "《旧书名》" in r
    assert appmod.SESSIONS["s1"]["state"] == "attached"
    assert appmod.SESSIONS["s1"]["project"] == KEY


def test_attached_continue_resumes(env, monkeypatch):
    """重启后 plan 仍是 in_progress 但写作线程已死：说「继续」必须真正续写，
    不得拿残留的 in_progress 当「正在创作中」而不动手（v2.3.0 实测踩过的坑）。"""
    called = {}
    assert env.plan()["status"] == "in_progress"   # 真实场景：状态残留 + 线程已死
    monkeypatch.setattr(appmod.writer, "resume_book",
                        lambda key: called.setdefault("key", key) or True)
    r = appmod.chat("s2", "继续", KEY)
    assert called["key"] == KEY and "继续创作" in r


def test_attached_progress(env):
    r = appmod.chat("s3", "进度", KEY)
    assert "3/5" in r and "旧书名" in r


def test_attached_chapter_rewrite_wins_over_new_book(env, monkeypatch):
    """「第12章重新写一下」是改本书，不是开新书（章号路由先于新建意图）。"""
    seen = {}
    monkeypatch.setattr(appmod, "_start_rewrite",
                        lambda key, chapters=None, dimensions=None, max_rounds=6:
                        seen.update(key=key, chapters=chapters, dims=dimensions) or True)
    r = appmod.chat("s4", "第12章重新写一下，节奏再紧一点", KEY)
    assert seen["key"] == KEY and seen["chapters"] == [12]
    assert "第12章局部重写" in r


def test_attached_cn_chapter_no(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(appmod, "_start_rewrite",
                        lambda key, chapters=None, dimensions=None, max_rounds=6:
                        seen.update(chapters=chapters) or True)
    appmod.chat("s5", "第十二章节奏慢了", KEY)
    assert seen["chapters"] == [12]


def test_attached_new_book_intent_only_guides(env, monkeypatch):
    queued = []
    monkeypatch.setattr(appmod.writer, "queue_msg", lambda key, m: queued.append(m) or True)
    r = appmod.chat("s6", "再来一本末世题材的小说", KEY)
    assert "新建作品" in r and not queued


def test_attached_directive_goes_into_creation(env, monkeypatch):
    queued = []
    monkeypatch.setattr(appmod.writer, "queue_msg", lambda key, m: queued.append(m) or True)
    r = appmod.chat("s7", "给女主加一场反击", KEY)
    assert queued == ["给女主加一场反击"] and "并入创作" in r


def test_bare_jiao_does_not_rename(env, monkeypatch):
    """「女主角叫什么名字」必须走创作指令，绝不能把书改名成「什么名字」。"""
    queued = []
    monkeypatch.setattr(appmod.writer, "queue_msg", lambda key, m: queued.append(m) or True)
    appmod.chat("s8", "女主角叫什么名字来着", KEY)
    assert queued and env.plan()["novelName"] == "旧书名"


def test_no_key_keeps_legacy_new_topic_flow(env, monkeypatch):
    """不带 key（经典版入口）时旧行为不变：idle 消息仍走主题提取。"""
    from bot import providers as prov_mod
    def _boom(*a, **k):
        raise prov_mod.ProviderError("no model")
    monkeypatch.setattr(appmod.workflow, "extract_config", _boom)
    r = appmod.chat("s9", "写一部悬疑小说")
    assert "未配置" in r or "模型" in r


def test_stale_key_falls_back_to_legacy(env):
    """key 指向已删除的作品 → 解绑，不炸、不起新书流程外的东西。"""
    r = appmod.chat("s10", "进度", "20990101-000000-不存在的书")
    assert isinstance(r, str) and r


# ---------------------------------------------------------------- 标题体系

def test_title_via_chat_generate_and_adopt(env, monkeypatch):
    monkeypatch.setattr(appmod.workflow, "gen_titles", lambda cfg, n=5: [
        {"title": "紫禁城的囚徒", "technique": "悬念", "why": "点题身世"},
        {"title": "九王夺嫡", "technique": "冲突", "why": "直给冲突"}])
    r = appmod.chat("s11", "给我几个书名", KEY)
    assert "紫禁城的囚徒" in r and "九王夺嫡" in r
    r2 = appmod.chat("s11", "采用2", KEY)
    assert "已改好" in r2 and "九王夺嫡" in r2
    new_key = "20260101-000000-九王夺嫡"
    assert os.path.isdir(os.path.join(env.path, os.pardir, new_key))
    assert appmod.SESSIONS["s11"]["project"] == new_key
    plan = json.loads(open(os.path.join(env.path, "02-写作计划.json"), encoding="utf-8").read())
    assert plan["novelName"] == "九王夺嫡"


def test_title_api_set_renames_dir(env, tmp_path):
    client = TestClient(appmod.app)
    r = client.post("/api/title/set", json={"key": KEY, "title": "清宫十二载"})
    assert r.status_code == 200 and r.json()["key"] == "20260101-000000-清宫十二载"
    assert os.path.isdir(tmp_path / "20260101-000000-清宫十二载")
    plan = json.loads((tmp_path / "20260101-000000-清宫十二载" / "02-写作计划.json")
                      .read_text(encoding="utf-8"))
    assert plan["novelName"] == "清宫十二载"
    # 非法名与空名 → 400
    assert client.post("/api/title/set", json={"key": KEY, "title": ""}).status_code == 400
    assert client.post("/api/title/set", json={"key": "nope", "title": "X"}).status_code == 404


def test_title_api_candidates_roundtrip(env, monkeypatch, tmp_path):
    client = TestClient(appmod.app)
    assert client.get("/api/title/candidates", params={"key": KEY}).json() == {"items": []}
    monkeypatch.setattr(appmod.workflow, "gen_titles", lambda cfg, n=5: [
        {"title": "紫禁城的囚徒", "technique": "悬念", "why": "点题身世"}])
    r = client.post("/api/title/generate", json={"key": KEY})
    assert r.status_code == 200 and r.json()["items"][0]["title"] == "紫禁城的囚徒"
    fp = os.path.join(env.path, "05-标题候选.json")
    assert json.loads(open(fp, encoding="utf-8").read())["items"][0]["title"] == "紫禁城的囚徒"
    # 再生成同名候选 → 去重不翻倍
    client.post("/api/title/generate", json={"key": KEY})
    items = json.loads(open(fp, encoding="utf-8").read())["items"]
    assert len([x for x in items if x["title"] == "紫禁城的囚徒"]) == 1


def test_audit_api_persists_and_returns(env, monkeypatch):
    client = TestClient(appmod.app)
    from bot import longform as lf
    monkeypatch.setattr(lf, "audit_book", lambda proj: ["第2弧缺少 1 张章卡", "1 条伏笔逾期"])
    r = client.post("/api/audit", json={"key": KEY, "scope": "book"})
    assert r.status_code == 200 and len(r.json()["issues"]) == 2
    saved = json.loads(open(os.path.join(env.path, "22-规划体检.json"), encoding="utf-8").read())
    assert saved["scope"] == "book" and len(saved["issues"]) == 2


def test_state_payload_injects_last_audit(tmp_path, monkeypatch):
    """22-规划体检.json 存在时 /api/state 的 state 里带 last_audit（体检面板数据源）。"""
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(appmod.writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    pdir = tmp_path / KEY
    pdir.mkdir()
    (pdir / "02-写作计划.json").write_text(json.dumps(
        {"novelName": "旧书名", "status": "in_progress",
         "chapters": [{"chapterNumber": 1, "status": "completed", "wordCount": 3000}]},
        ensure_ascii=False), encoding="utf-8")
    (pdir / "22-规划体检.json").write_text(json.dumps(
        {"at": "2026-10-26T10:00:00", "scope": "book", "issues": ["人物A地点未登记"]},
        ensure_ascii=False), encoding="utf-8")
    client = TestClient(appmod.app)
    r = client.get("/api/state", params={"key": KEY})
    assert r.status_code == 200
    la = (r.json().get("state") or {}).get("last_audit")
    assert la and la["scope"] == "book" and la["issues"] == ["人物A地点未登记"]
