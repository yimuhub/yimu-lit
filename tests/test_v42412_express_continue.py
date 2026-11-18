"""v4.24.12：未完成章节直接用极速模式续写。

用户需求原文：「在极速模式和普通模式下还未完成的章节也可以直接采用极速模式进行创作」。

这句话里有个容易被做窄的地方：极速模式常被当成「这本书永久的属性」——
于是只在建书向导里有极速选项，老书想用极速就只能重开一本。
真正的语义是：**极速是「这一批怎么写」的选择**。普通模式的书有未完成章节时，
应当能一键切成极速并把剩余章节一口气写完；反过来也能切回标准补审校。

本文件钉住四件事：

1. 开关口径唯一（bot/express.py）
   `config.express` 是唯一判据；小说的 `config.mode`（创作模式卡片）必须同步，
   否则界面上的模式标签与实际写作行为对不上。

2. 切回标准要真的关掉（express.on 的优先级）
   `on()` 优先看 `express` 键——只把 mode 改回 rolling 而留着 express=True，
   老书会「切不回标准」。

3. 小说路径能启动（/api/express-write）
   有未完成章节 → 切极速 + 启动续写；全部写完 / 正在写 → 明确拒绝，不假装成功。

4. 非小说路径能启动（8 个引擎共用）
   key 前缀 → 引擎模块的映射只有一份（_nonnovel_module），
   开关写进 plan.config，引擎自己读它。
"""
import json
import re
import time

import pytest

from bot import express, writer

PLAN_FILE = "02-写作计划.json"


def _mk_book(tmp_path, monkeypatch, key="book", status="in_progress", done=50, total=100,
             config=None, paused=False):
    """造一本有 progress 的小说，返回 (项目目录, 计划文件路径)。"""
    pdir = tmp_path / key
    pdir.mkdir(parents=True, exist_ok=True)
    chapters = [
        {"chapterNumber": n, "title": f"第{n}章", "status": "completed", "wordCount": 100}
        for n in range(1, done + 1)
    ] + [
        {"chapterNumber": n, "title": f"第{n}章", "status": "pending", "wordCount": 0}
        for n in range(done + 1, total + 1)
    ]
    meta = {"paused": True} if paused else {}
    plan = {"novelName": "测试书", "status": status, "chapters": chapters,
            "totalChapters": total, "meta": meta, "config": dict(config or {})}
    fp = pdir / PLAN_FILE
    json.dump(plan, open(fp, "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    return pdir, fp


def _read_plan(fp):
    return json.load(open(fp, encoding="utf-8"))


# ---------------------------------------------------------------- 开关口径

def test_set_plan_flag_writes_express_true():
    """切极速：config.express=True，且小说的 config.mode 同步成 express。"""
    plan = {"config": {"mode": "rolling", "batch_size": "1"}}
    express.set_plan_flag(plan, True)
    assert plan["config"]["express"] is True
    assert plan["config"]["mode"] == "express"
    assert express.on(plan["config"]) is True


def test_set_plan_flag_standard_resets_mode_and_disables():
    """切回标准：express=False，mode 从 express 还原成 rolling。"""
    plan = {"config": {"express": True, "mode": "express"}}
    express.set_plan_flag(plan, False)
    assert plan["config"]["express"] is False
    assert plan["config"]["mode"] == express.STANDARD_MODE
    assert express.on(plan["config"]) is False


def test_set_plan_flag_keeps_other_mode_when_standard():
    """原本就是 rolling 的书切「标准」时不要把 mode 改成别的东西。"""
    plan = {"config": {"mode": "outline"}}
    express.set_plan_flag(plan, False)
    assert plan["config"]["mode"] == "outline"


def test_set_plan_flag_creates_config_when_missing():
    """老书没有 config 键 → 也要能写开关（不能因为 dict 不存在就静默跳过）。"""
    plan = {"chapters": []}
    express.set_plan_flag(plan, True)
    assert plan["config"]["express"] is True


def test_express_on_prefers_express_key_over_mode():
    """回归：express 键优先于 mode。

    这是「切回标准」能不能生效的关键——若 on() 改看 mode，
    只写 express=False 而 mode 还留着 express 的老书就切不回去。
    """
    assert express.on({"express": False, "mode": "express"}) is False
    assert express.on({"express": True, "mode": "rolling"}) is True
    assert express.on({"mode": "express"}) is True


def test_plan_flag_reads_config():
    """plan_flag 与 on() 同一口径（前端拿到的 express 标识要一致）。"""
    assert express.plan_flag({"config": {"express": True}}) is True
    assert express.plan_flag({"config": {"mode": "express"}}) is True
    assert express.plan_flag({"config": {}}) is False
    assert express.plan_flag(None) is False


# ---------------------------------------------------------------- 接口：小说路径

@pytest.fixture()
def client():
    import app as appmod
    from fastapi.testclient import TestClient
    return TestClient(appmod.app)


def test_api_express_write_switches_and_starts(client, tmp_path, monkeypatch):
    """核心用例：普通模式的书有未完成章节 → 一键切极速并启动续写。"""
    import app as appmod
    _pdir, fp = _mk_book(tmp_path, monkeypatch, config={"mode": "rolling", "batch_size": "1"})
    appmod.PROJECT_INDEX.pop("book", None)
    called = {}
    monkeypatch.setattr(appmod.writer, "resume_book",
                        lambda key, batch_size=None: called.update(key=key, batch=batch_size) or True)
    resp = client.post("/api/express-write", json={"key": "book"})
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body.get("ok") is True, body
    assert body.get("express") is True
    plan = _read_plan(fp)
    assert plan["config"]["express"] is True, "极速开关没写进 plan.config"
    assert plan["config"]["mode"] == "express"
    assert called.get("key") == "book", "没启动续写"
    # 一口气写完：batch_size 必须传 all（极速模式会忽略它，但语义要对）
    assert str(called.get("batch")).lower() == "all", f"batch={called.get('batch')}"


def test_api_express_write_works_for_express_book(client, tmp_path, monkeypatch):
    """已经是极速模式的书，再点一次也要能继续（不能因为「已经是极速」就拒绝）。"""
    import app as appmod
    _pdir, fp = _mk_book(tmp_path, monkeypatch, config={"express": True, "mode": "express"})
    appmod.PROJECT_INDEX.pop("book", None)
    called = {}
    monkeypatch.setattr(appmod.writer, "resume_book",
                        lambda key, batch_size=None: called.update(key=key) or True)
    body = client.post("/api/express-write", json={"key": "book"}).json()
    assert body.get("ok") is True, body
    assert called.get("key") == "book"
    assert _read_plan(fp)["config"]["express"] is True


def test_api_express_write_standard_only_switches(client, tmp_path, monkeypatch):
    """切回标准：只改开关，不启动写作。"""
    import app as appmod
    _pdir, fp = _mk_book(tmp_path, monkeypatch, config={"express": True, "mode": "express"})
    appmod.PROJECT_INDEX.pop("book", None)
    called = []
    monkeypatch.setattr(appmod.writer, "resume_book", lambda key, batch_size=None: called.append(key) or True)
    body = client.post("/api/express-write", json={"key": "book", "mode": "standard"}).json()
    assert body.get("ok") is True, body
    assert body.get("express") is False
    assert not called, "切回标准不该启动写作"
    plan = _read_plan(fp)
    assert plan["config"]["express"] is False
    assert plan["config"]["mode"] == express.STANDARD_MODE


def test_api_express_write_rejects_when_running(client, tmp_path, monkeypatch):
    """正在写的时候不切速度——否则会和在跑的线程抢口径。"""
    import app as appmod
    _pdir, _fp = _mk_book(tmp_path, monkeypatch)
    appmod.PROJECT_INDEX.pop("book", None)
    proj = appmod.writer.Project(str(tmp_path / "book"), "book")
    monkeypatch.setattr(appmod.writer.Project, "status",
                        lambda self, _p=proj: {"running": True, "paused": False, "state": "in_progress",
                                               "done": 50, "total": 100, "words": 0})
    body = client.post("/api/express-write", json={"key": "book"}).json()
    assert body.get("ok") is False, body
    assert "创作中" in (body.get("msg") or "")


def test_api_express_write_rejects_when_nothing_pending(client, tmp_path, monkeypatch):
    """全部写完 → 明确说没得写，不假装启动。"""
    import app as appmod
    _pdir, _fp = _mk_book(tmp_path, monkeypatch, done=100, total=100)
    appmod.PROJECT_INDEX.pop("book", None)
    body = client.post("/api/express-write", json={"key": "book"}).json()
    assert body.get("ok") is False, body
    assert "没有待写" in (body.get("msg") or "") or "章节" in (body.get("msg") or "")


def test_api_express_write_clears_pause(client, tmp_path, monkeypatch):
    """暂停着的书点「极速续写」→ 暂停位要清掉，否则线程一起来就又停了。"""
    import app as appmod
    _pdir, fp = _mk_book(tmp_path, monkeypatch, paused=True, status="paused")
    appmod.PROJECT_INDEX.pop("book", None)
    monkeypatch.setattr(appmod.writer, "resume_book", lambda key, batch_size=None: True)
    body = client.post("/api/express-write", json={"key": "book"}).json()
    assert body.get("ok") is True, body
    plan = _read_plan(fp)
    assert (plan.get("meta") or {}).get("paused") is False, "暂停位没清"


def test_api_express_write_missing_book(client):
    """不存在的书 → 404，别 500。"""
    resp = client.post("/api/express-write", json={"key": "no-such-book-xyz"})
    assert resp.status_code == 404, resp.text[:200]


def test_api_express_write_standard_keeps_pause(client, tmp_path, monkeypatch):
    """「切回标准」只改速度，不替用户解除暂停。

    否则会出现最难解释的状态：界面显示没暂停，实际没有任何线程在跑。
    """
    import app as appmod
    _pdir, fp = _mk_book(tmp_path, monkeypatch, paused=True, status="paused",
                         config={"express": True, "mode": "express"})
    appmod.PROJECT_INDEX.pop("book", None)
    monkeypatch.setattr(appmod.writer, "resume_book", lambda key, batch_size=None: True)
    body = client.post("/api/express-write", json={"key": "book", "mode": "standard"}).json()
    assert body.get("ok") is True, body
    assert body.get("paused") is True, "切回标准不该把暂停位一起清掉"
    plan = _read_plan(fp)
    assert (plan.get("meta") or {}).get("paused") is True
    assert plan.get("status") == "paused", "切回标准不该把状态改成进行中"
def test_api_express_write_rejects_bad_mode(client, tmp_path, monkeypatch):
    """非法 mode → 400。"""
    import app as appmod
    _mk_book(tmp_path, monkeypatch)
    appmod.PROJECT_INDEX.pop("book", None)
    resp = client.post("/api/express-write", json={"key": "book", "mode": "turbo"})
    assert resp.status_code == 400, resp.text[:200]


def test_api_express_write_missing_key(client):
    """缺 key → 400。"""
    resp = client.post("/api/express-write", json={})
    assert resp.status_code == 400, resp.text[:200]


# ---------------------------------------------------------------- 接口：非小说路径

def test_nonnovel_module_covers_all_eight():
    """8 个非小说引擎都要能按 key 前缀取到模块（加文体时漏一处就静默走小说分支）。"""
    import app as appmod
    for prefix in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
        mod = appmod._nonnovel_module(prefix + "xxx")
        assert mod is not None, f"前缀 {prefix} 取不到引擎模块"
        assert callable(getattr(mod, "resume_project", None)), f"{prefix} 缺 resume_project"
    assert appmod._nonnovel_module("book") is None


def test_api_express_write_nonnovel(client, tmp_path, monkeypatch):
    """非小说：开关写进 plan.config，引擎自己读它；并启动 resume_project。"""
    from bot import script_writer
    pdir = tmp_path / "s_demo"
    pdir.mkdir(parents=True, exist_ok=True)
    json.dump({"scriptName": "演示剧本", "status": "writing", "scenes": [],
               "config": {"express": False}, "meta": {"paused": False}},
              open(pdir / "剧本计划.json", "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(tmp_path))
    called = {}
    monkeypatch.setattr(script_writer, "resume_project",
                        lambda key: called.update(key=key) or None)
    body = client.post("/api/express-write", json={"key": "s_demo"}).json()
    assert body.get("ok") is True, body
    assert body.get("express") is True
    plan = json.load(open(pdir / "剧本计划.json", encoding="utf-8"))
    assert plan["config"]["express"] is True, "非小说的极速开关没写进 plan.config"
    # v4.24.13：resume_project 改到后台线程跑（同步实现会堵住整个请求），
    # 断言前给它一个短暂窗口落地。
    for _ in range(100):
        if called.get("key"):
            break
        time.sleep(0.02)
    assert called.get("key") == "s_demo", "非小说没启动 resume_project"
    # 等后台线程收尾，避免污染下一个用例的运行标记
    for _ in range(100):
        if not writer.pipeline_running("s_demo"):
            break
        time.sleep(0.02)


def test_api_express_write_nonnovel_standard(client, tmp_path, monkeypatch):
    """非小说切回标准：只改开关，不开写。"""
    from bot import script_writer
    pdir = tmp_path / "s_demo"
    pdir.mkdir(parents=True, exist_ok=True)
    json.dump({"scriptName": "演示剧本", "status": "writing", "scenes": [],
               "config": {"express": True}, "meta": {}},
              open(pdir / "剧本计划.json", "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(script_writer, "resume_project", lambda key: called.append(key) or None)
    body = client.post("/api/express-write", json={"key": "s_demo", "mode": "standard"}).json()
    assert body.get("ok") is True, body
    assert not called
    plan = json.load(open(pdir / "剧本计划.json", encoding="utf-8"))
    assert plan["config"]["express"] is False


# ---------------------------------------------------------------- 引擎行为：极速不受批量限制

def test_express_ignores_batch_size_in_write_loop():
    """小说：极速模式必须忽略 batch_size——「一口气写完」是它的定义。

    v4.24.15：判据从启动快照 `_expr` 改成实时值 `_live_express()`——
    用户写到一半才开极速，这一批也不该再被 batch_size 卡住。
    """
    src = open("bot/writer.py", encoding="utf-8").read()
    m = re.search(r"def _write_loop\(proj: Project.*?\n(?=def |\Z)", src, re.S)
    assert m, "找不到 _write_loop"
    body = m.group(0)
    assert "if batch_size is not None and not _live_express():" in body, \
        "极速模式仍会被 batch_size 卡在半路——它必须一口气写完"
    assert "and not _live_express():" in body, \
        "批次上限检查没读实时开关：中途开启极速后仍会按批停"


def test_express_ignores_batch_size_in_script_writer():
    """剧本：同样忽略批量。"""
    src = open("bot/script_writer.py", encoding="utf-8").read()
    assert "if batch_size is not None and not express.on(cfg):" in src


def test_express_skips_validation_and_rewrite():
    """极速模式跳过失败章重写与五角色审校（初稿语义）。"""
    src = open("bot/writer.py", encoding="utf-8").read()
    assert "if val.get(\"enabled\", True) and not _expr:" in src
    assert "跳过失败章重写与全书审校" in src


# ---------------------------------------------------------------- 前端

def _read(rel):
    with open(rel, encoding="utf-8") as f:
        return f.read()


def test_frontend_exports_express_button():
    """planbar.js 必须导出极速入口工厂。"""
    src = _read("static/v2/js/components/planbar.js")
    assert "export function createExpressButton" in src


def test_frontend_express_button_only_when_pending():
    """没有待写章节 / 正在写时不给入口。"""
    src = _read("static/v2/js/components/planbar.js")
    m = re.search(r"export function createExpressButton.*?\n\}", src, re.S)
    assert m
    body = m.group(0)
    assert "total <= done" in body, "缺「没有待写章节就不给入口」的判断"
    assert "work.running" in body, "缺「正在写时不切速度」的判断"


def test_frontend_express_button_calls_api():
    """按钮必须真的调后端接口（不能只是个摆设）。"""
    src = _read("static/v2/js/components/planbar.js")
    assert "api.expressWrite" in src


def test_frontend_express_switch_stays_clickable_while_running():
    """v4.24.15：开关在「正在写」时也必须可点——否则用户只能先暂停才能切速度。

    防连点仍然要留（请求在飞时 btn.disabled = true）。
    """
    src = _read("static/v2/js/components/planbar.js")
    m = re.search(r"export function createExpressSwitch.*?\n\}", src, re.S)
    assert m, "找不到 createExpressSwitch"
    body = m.group(0)
    assert "disabled: running" not in body, "运行中又把开关禁用了"
    assert "btn.disabled = true" in body, "少了请求在飞时的防连点"


def test_api_client_has_express_write():
    """api.js 要有 expressWrite 封装。"""
    src = _read("static/v2/js/core/api.js")
    assert "expressWrite" in src
    assert "/api/express-write" in src


def test_express_button_mounted_in_planbar_and_rightpanel():
    """目标条与右栏两个入口都要挂上极速按钮（否则某些界面就找不到）。"""
    planbar = _read("static/v2/js/components/planbar.js")
    right = _read("static/v2/js/components/rightpanel.js")
    assert "createExpressButton({ work, ctx, compact: true, plan })" in planbar, "目标条没挂极速入口"
    assert right.count("createExpressButton(") >= 2, "右栏两个作品页签都要挂极速入口"
    # v4.24.13：导入列表加了 createExpressSwitch，不再按整行字面量断言
    assert "createExpressButton" in right, "右栏没导入极速按钮"


def test_frontend_express_button_reads_plan_payload():
    """前端拿到的 plan 是 /api/plan 的载荷（{plan:{...}}），不是裸 plan。

    读错层级 → 「切回标准」永远显示为可用/不可用的旧状态（不报错，只是不对）。
    """
    src = _read("static/v2/js/components/planbar.js")
    m = re.search(r"export function createExpressButton.*?\n\}", src, re.S)
    assert m
    assert "plan.plan" in m.group(0), "极速状态读错层级：应当取 payload.plan.express"


def test_plan_redraw_signature_includes_express():
    """重绘签名要带极速状态——否则切回标准后菜单项还是旧状态。"""
    src = _read("static/v2/js/views/session.js")
    assert "plan.express" in src, "目标条签名缺 express，切速后界面不更新"


def test_right_panel_signature_includes_express():
    """右栏重绘签名同样要带极速状态。"""
    src = _read("static/v2/js/app.js")
    m = re.search(r"const rightSig = \[(.*?)\]\.join\('\|'\);", src, re.S)
    assert m, "找不到 rightSig"
    assert "plan.express" in m.group(1), "右栏签名缺 express，切速后界面不更新"
