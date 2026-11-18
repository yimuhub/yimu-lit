"""v4.24.13：极速开关与极速续写的冲突审查。

这两个功能共用同一个 `config.express`，但 v4.24.12 之前是**两个写入者**：

    /api/express-toggle（开关）  写 config.express + 顶层 plan["express"]
    /api/express-write （续写）  写 config.express + config.mode

同一本书因此可能「顶层说开、config 说关」。更实际的三处冲突：

1. 运行规则不一致：续写拒绝在写作中切速度，开关却允许 —— 开关能把标签改掉，
   但写作循环的 batch_limit 是启动时算定的，实际行为不会跟着变。
2. 非小说「续写」在请求线程里同步跑完整条流水线（poetry/prose/thesis/children/
   news/copy/other 的 resume_project 都是同步的），HTTP 几分钟不返回、
   running 还是 false，用户再点一次就是两条流水线改同一批文件。
3. 失败也改状态：已写完的书点续写会先被切成极速，再回一句「没有待写的章节」；
   启动失败（并发抢跑）同样会把开关留在极速。

本文件钉住：单一写入口、运行中一律拒绝、非小说后台执行、
失败回滚、以及「没活可干时不动开关」。
"""
import json
import threading
import time

import pytest

from bot import express, writer

PLAN_FILE = "02-写作计划.json"
SCRIPT_PLAN = "剧本计划.json"


@pytest.fixture()
def client():
    import app as appmod
    from fastapi.testclient import TestClient
    return TestClient(appmod.app)


def _mk_book(tmp_path, monkeypatch, key="book", status="in_progress",
             done=5, total=10, config=None, paused=False):
    pdir = tmp_path / key
    pdir.mkdir(parents=True, exist_ok=True)
    chapters = [
        {"chapterNumber": n, "title": f"第{n}章", "status": "completed", "wordCount": 100}
        for n in range(1, done + 1)
    ] + [
        {"chapterNumber": n, "title": f"第{n}章", "status": "pending", "wordCount": 0}
        for n in range(done + 1, total + 1)
    ]
    plan = {"novelName": "测试书", "status": status, "chapters": chapters,
            "totalChapters": total, "meta": {"paused": True} if paused else {},
            "config": dict(config or {})}
    fp = pdir / PLAN_FILE
    json.dump(plan, open(fp, "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    return pdir, fp


def _mk_script(tmp_path, monkeypatch, key="s_demo", status="writing", config=None):
    from bot import script_writer
    pdir = tmp_path / key
    pdir.mkdir(parents=True, exist_ok=True)
    plan = {"scriptName": "演示剧本", "status": status, "scenes": [],
            "config": dict(config or {}), "meta": {}}
    fp = pdir / SCRIPT_PLAN
    json.dump(plan, open(fp, "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(tmp_path))
    return pdir, fp


def _read(fp):
    return json.load(open(fp, encoding="utf-8"))


# ---------------------------------------------------------------- 单一写入口

def test_set_plan_flag_mirrors_top_level():
    """config.express 与顶层 express 必须永远同值——两份状态是冲突的源头。"""
    plan = {"config": {"mode": "rolling"}}
    express.set_plan_flag(plan, True)
    assert plan["config"]["express"] is True
    assert plan["express"] is True
    express.set_plan_flag(plan, False)
    assert plan["config"]["express"] is False
    assert plan["express"] is False


def test_set_plan_flag_restores_previous_mode():
    """开极速不能把用户选过的「大纲优先」永久改成 rolling；切回来要还原。"""
    plan = {"config": {"mode": "outline"}}
    express.set_plan_flag(plan, True)
    assert plan["config"]["mode"] == "express"
    assert plan["config"]["_mode_before_express"] == "outline"
    express.set_plan_flag(plan, False)
    assert plan["config"]["mode"] == "outline"
    assert "_mode_before_express" not in plan["config"]


def test_set_plan_flag_keeps_mode_when_already_express():
    """重复开极速不能把「切换前的模式」覆盖成 express 本身。"""
    plan = {"config": {"mode": "outline"}}
    express.set_plan_flag(plan, True)
    express.set_plan_flag(plan, True)
    express.set_plan_flag(plan, False)
    assert plan["config"]["mode"] == "outline"


def test_set_plan_flag_can_skip_mode_for_nonnovel():
    """非小说没有「创作模式」卡片，不该被塞一个无意义的 config.mode。"""
    plan = {"config": {"form": "film"}}
    express.set_plan_flag(plan, True, sync_mode=False)
    assert plan["config"]["express"] is True
    assert "mode" not in plan["config"]


def test_toggle_uses_single_writer(client, tmp_path, monkeypatch):
    """开关与续写写出来的字段必须完全一致（不再各写一半）。"""
    import app as appmod
    _pdir, fp = _mk_book(tmp_path, monkeypatch, config={"mode": "rolling"})
    appmod.PROJECT_INDEX.pop("book", None)
    body = client.post("/api/express-toggle", json={"key": "book", "on": True}).json()
    assert body.get("ok") is True, body
    plan = _read(fp)
    assert plan["config"]["express"] is True
    assert plan["express"] is True
    assert plan["config"]["mode"] == "express", "小说的创作模式卡片要跟着同步"

    body = client.post("/api/express-toggle", json={"key": "book", "on": False}).json()
    assert body.get("ok") is True, body
    plan = _read(fp)
    assert plan["config"]["express"] is False
    assert plan["express"] is False
    assert plan["config"]["mode"] == express.STANDARD_MODE


# ---------------------------------------------------------------- 运行中：小说可切 / 非小说拒绝

def test_toggle_works_while_running_novel(client, tmp_path, monkeypatch):
    """v4.24.15：小说写作中**允许**切开关——写作线程每章重读 config.express，
    切档只影响还没写的章。v4.24.13 那版「先暂停再切」把用户卡死在正在写的书里。"""
    import app as appmod
    _pdir, fp = _mk_book(tmp_path, monkeypatch, config={"express": False, "mode": "rolling"})
    appmod.PROJECT_INDEX.pop("book", None)
    monkeypatch.setattr(appmod.writer.Project, "status",
                        lambda self: {"running": True, "paused": False, "state": "in_progress",
                                      "done": 1, "total": 10, "words": 100})
    body = client.post("/api/express-toggle", json={"key": "book", "on": True}).json()
    assert body.get("ok") is True, body
    assert body.get("running") is True
    assert "下一章" in (body.get("msg") or "") or "写完即生效" in (body.get("msg") or "")
    plan = _read(fp)
    assert plan["config"]["express"] is True, "运行中点了开关却没落盘"
    assert plan["config"]["mode"] == "express"

    body = client.post("/api/express-toggle", json={"key": "book", "on": False}).json()
    assert body.get("ok") is True, body
    assert _read(fp)["config"]["express"] is False


def test_toggle_refuses_while_running_nonnovel(client, tmp_path, monkeypatch):
    import app as appmod
    _pdir, fp = _mk_script(tmp_path, monkeypatch, config={"express": False})
    monkeypatch.setattr(appmod.writer, "pipeline_running", lambda key: True)
    body = client.post("/api/express-toggle", json={"key": "s_demo", "on": True}).json()
    assert body.get("ok") is False, body
    assert _read(fp)["config"]["express"] is False


# ---------------------------------------------------------------- 非小说：别写错文件

def test_toggle_nonnovel_writes_medium_plan_not_novel_plan(client, tmp_path, monkeypatch):
    """非小说开关必须落在剧本计划里；绝不能往剧本目录写 02-写作计划.json。"""
    pdir, fp = _mk_script(tmp_path, monkeypatch, config={"express": False})
    body = client.post("/api/express-toggle", json={"key": "s_demo", "on": True}).json()
    assert body.get("ok") is True, body
    assert _read(fp)["config"]["express"] is True
    assert not (pdir / PLAN_FILE).exists(), \
        "往剧本目录写了小说的 02-写作计划.json —— 会把这本书的文体判定弄脏"


def test_toggle_nonnovel_missing_plan_does_not_create_one(client, tmp_path, monkeypatch):
    """目录在、计划缺失：返回 404，不能凭空写一份只有 express 的计划。"""
    from bot import script_writer
    pdir = tmp_path / "s_empty"
    pdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(tmp_path))
    resp = client.post("/api/express-toggle", json={"key": "s_empty", "on": True})
    assert resp.status_code == 404, resp.text[:200]
    assert not (pdir / SCRIPT_PLAN).exists(), "计划文件被凭空创建了"


# ---------------------------------------------------------------- 非小说：后台执行

def test_express_write_nonnovel_runs_in_background(client, tmp_path, monkeypatch):
    """7 个引擎的 resume_project 是同步的：接口必须立刻返回，不能挂在请求线程里。"""
    from bot import script_writer
    _pdir, fp = _mk_script(tmp_path, monkeypatch, config={"express": False})
    gate = threading.Event()
    started = threading.Event()

    def _slow(key):
        started.set()
        gate.wait(5)

    monkeypatch.setattr(script_writer, "resume_project", _slow)
    t0 = time.time()
    body = client.post("/api/express-write", json={"key": "s_demo"}).json()
    elapsed = time.time() - t0
    try:
        assert body.get("ok") is True, body
        assert elapsed < 1.0, f"接口被同步流水线堵了 {elapsed:.1f}s"
        assert started.wait(2), "后台线程没有真正启动 resume_project"
        assert writer.pipeline_running("s_demo") is True, "后台跑着但 running 标记没置上"
        assert _read(fp)["config"]["express"] is True
    finally:
        gate.set()
        for _ in range(100):
            if not writer.pipeline_running("s_demo"):
                break
            time.sleep(0.02)


def test_express_write_nonnovel_completed_refuses(client, tmp_path, monkeypatch):
    """已定稿的剧本没有「续写」可言，别返回假装在跑。"""
    from bot import script_writer
    _pdir, fp = _mk_script(tmp_path, monkeypatch, status="completed", config={"express": False})
    called = []
    monkeypatch.setattr(script_writer, "resume_project", lambda key: called.append(key))
    body = client.post("/api/express-write", json={"key": "s_demo"}).json()
    assert body.get("ok") is False, body
    assert not called, "已定稿还启动了流水线"
    assert _read(fp)["config"]["express"] is False


def test_express_write_nonnovel_refuses_while_running(client, tmp_path, monkeypatch):
    import app as appmod
    _pdir, fp = _mk_script(tmp_path, monkeypatch, config={"express": False})
    monkeypatch.setattr(appmod.writer, "pipeline_running", lambda key: True)
    body = client.post("/api/express-write", json={"key": "s_demo"}).json()
    assert body.get("ok") is False, body
    assert "创作中" in (body.get("msg") or "")
    assert _read(fp)["config"]["express"] is False


# ---------------------------------------------------------------- 失败不改状态

def test_express_write_rolls_back_when_start_fails(client, tmp_path, monkeypatch):
    """启动失败（并发抢跑）不能把书留在极速模式。"""
    import app as appmod
    _pdir, fp = _mk_book(tmp_path, monkeypatch, config={"express": False, "mode": "rolling"})
    appmod.PROJECT_INDEX.pop("book", None)
    monkeypatch.setattr(appmod.writer, "resume_book", lambda key, batch_size=None: False)
    body = client.post("/api/express-write", json={"key": "book"}).json()
    assert body.get("ok") is False, body
    plan = _read(fp)
    assert plan["config"]["express"] is False, "启动失败却把开关留在极速"
    assert plan["config"]["mode"] == "rolling"
    assert plan.get("express") is False


def test_express_write_no_pending_does_not_flip_flag(client, tmp_path, monkeypatch):
    """全部写完的书点续写：拒绝，且不能先切成极速再拒绝。"""
    import app as appmod
    _pdir, fp = _mk_book(tmp_path, monkeypatch, done=10, total=10,
                         status="completed", config={"express": False, "mode": "rolling"})
    appmod.PROJECT_INDEX.pop("book", None)
    body = client.post("/api/express-write", json={"key": "book"}).json()
    assert body.get("ok") is False, body
    plan = _read(fp)
    assert plan["config"]["express"] is False


# ---------------------------------------------------------------- 前端：同一套状态

def test_frontend_switch_exported_and_shared():
    src = open("static/v2/js/components/planbar.js", encoding="utf-8").read()
    assert "export function createExpressSwitch" in src
    assert "api.expressToggle" in src
    # 开关与续写必须都走同一个后端字段口径
    assert "api.expressWrite" in src
    assert "if (btn.disabled) return;" in src, "开关没有防连点"
    assert "busy = true" in src, "续写没有请求在飞保护"


def test_frontend_switch_stays_clickable_while_running():
    """v4.24.15：开关不再因「正在写」而禁用——切档只影响还没写的章。

    v4.24.13 曾在这里要求 `disabled: running` + 「先暂停，切完再继续」，
    实际把用户卡死在正在写的书里：想换速度必须先停下整条流水线。
    """
    src = open("static/v2/js/components/planbar.js", encoding="utf-8").read()
    assert "disabled: running" not in src, "运行中又把开关禁用了"
    assert "当前这一章写完即生效" in src, "运行中切换的生效时机没说清楚"
    assert "先暂停，切完再继续" not in src


def test_rightpanel_uses_shared_switch_in_both_panes():
    src = open("static/v2/js/components/rightpanel.js", encoding="utf-8").read()
    assert src.count("createExpressSwitch({ work, ctx") == 2, \
        "小说页与非小说页都该有极速开关"
    assert "planNow.express" not in src, "旧的开关实现应被工厂替换"
