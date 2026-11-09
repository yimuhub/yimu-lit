"""v4.33.6 回归：改编剧的「继续」只能走集级续跑，不能掉进按场写。

用户实测（2026-11-09 21:38–22:16）一部 48/50 的短剧：
  · 21:38、21:39、21:46、22:01 四次「继续」，运行日志全是
    「🛑 这是小说改编剧，已拒绝「按场批量写」」——一个字都没写；
  · 22:16 总编辑报「创作停滞，但自动恢复预算已用完（24h 内 3 次）」后彻底不动；
  · 界面一直显示「创作中」，因为作品库拿 plan.status=in_progress 当运行状态。

根因：改编续跑只在 /api/continue 里判了一次。supervisor 的自动恢复和
「极速续写」都直接调 script_writer.resume_project → write_scenes → 被守卫拒绝。
三次自动恢复额度就这样被白烧掉。

本文件锁死：
1. resume_project 遇到改编剧先转派注册器，不落按场写、不打「剩余场次」日志；
2. 没有注册器时明确报「只能按集续跑」，同样不按场写；
3. /api/resume、/api/express-write 对改编剧都走改编引擎（不当场写正文）；
4. 手动点「继续」清零自动恢复预算；
5. 没在跑但没写完的作品，状态文案是「待继续」而不是「创作中」。
"""
import json

import pytest
from fastapi.testclient import TestClient

from bot import script_writer as sw
from bot import supervisor as sv
from bot import writer

NOVEL = "20260101-000000-原小说"
DRAMA = "20260101-000000-原小说-短剧"


def _write_drama(root, *, adapted=True, status="in_progress", scenes=3, done=1):
    d = root / DRAMA
    d.mkdir(exist_ok=True)
    rows = [{"no": n, "kind": "集", "place": f"第{n}集", "title": f"第{n}集",
             "status": "completed" if n <= done else "pending",
             "fileName": f"第{n:03d}场.md", "wordCount": 900 if n <= done else 0}
            for n in range(1, scenes + 1)]
    plan = {"version": 1, "scriptName": "测试短剧", "medium": "script",
            "form": "short_drama", "formLabel": "AI短剧", "status": status,
            "scenes": rows, "config": {"form": "short_drama"}, "meta": {},
            "adaptation": {"version": 2, "chaptersPerEpisode": 2,
                           "blueprint": [{"no": 1, "chapters": [1, 2]}]}}
    if adapted:
        plan["adaptedFrom"] = {"key": NOVEL, "chapters": [1, 2]}
    (d / "剧本计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (d / "运行日志.txt").write_text("", encoding="utf-8")
    return d


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from bot import security, livejob
    import app as appmod

    proot = tmp_path / "projects"
    proot.mkdir()
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(proot))
    monkeypatch.setattr(sw, "PROJECTS_DIR", str(proot))
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    appmod.PROJECT_INDEX.clear()
    livejob.reset_for_tests()
    writer._PIPELINE_COUNT.clear()
    d = _write_drama(proot)
    client = TestClient(appmod.app)
    yield {"root": proot, "dir": d, "client": client, "app": appmod}
    livejob.reset_for_tests()
    writer._PIPELINE_COUNT.clear()


# ---------------------------------------------------------------- 引擎层：单一出口

def test_resume_project_hands_adapted_drama_to_resumer(tmp_path, monkeypatch):
    """改编剧在打「剩余场次」日志、起按场线程之前就被接走。"""
    _write_drama(tmp_path)
    monkeypatch.setattr(sw, "PROJECTS_DIR", str(tmp_path))
    seen = {}

    def _resumer(key):
        seen["key"] = key
        return True

    monkeypatch.setattr(sw, "_ADAPT_RESUMER", _resumer)

    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("改编剧不该进 _write_scenes_inner")

    monkeypatch.setattr(sw, "_write_scenes_inner", _boom)
    sw.resume_project(DRAMA)

    assert seen["key"] == DRAMA
    assert called["n"] == 0
    log = (tmp_path / DRAMA / "运行日志.txt").read_text(encoding="utf-8")
    assert "剩余场次" not in log      # 不再误报「继续写作剩余场次」


def test_resume_project_without_resumer_refuses_instead_of_scene_write(tmp_path, monkeypatch):
    """没装配注册器时（独立进程 / 单测）也要说清楚，不能静默按场写。"""
    _write_drama(tmp_path)
    monkeypatch.setattr(sw, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(sw, "_ADAPT_RESUMER", None)
    monkeypatch.setattr(sw, "_write_scenes_inner",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该按场写")))
    sw.resume_project(DRAMA)
    log = (tmp_path / DRAMA / "运行日志.txt").read_text(encoding="utf-8")
    assert "按集续跑" in log


def test_plain_script_still_uses_scene_pipeline(tmp_path, monkeypatch):
    """原创剧本不受影响：仍然按场写。"""
    d = _write_drama(tmp_path, adapted=False)
    monkeypatch.setattr(sw, "PROJECTS_DIR", str(tmp_path))
    hit = {"n": 0}
    done = {"e": None}
    import threading

    def _inner(proj, *a, **k):
        hit["n"] += 1
        done["e"].set()
        return {"ok": True}

    monkeypatch.setattr(sw, "_write_scenes_inner", _inner)
    done["e"] = threading.Event()
    sw.resume_project(DRAMA)
    assert done["e"].wait(3), "原创剧本应照常起按场写线程"
    assert hit["n"] == 1
    assert "剩余场次" in (d / "运行日志.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 接口层：两条入口

def test_resume_api_routes_adapted_to_adapter_not_scene_write(env):
    """/api/resume → /api/continue：改编剧走改编引擎，不写场次正文。"""
    r = env["client"].post("/api/resume", json={"key": DRAMA})
    assert r.status_code == 200
    body = r.json()
    # 原小说不存在 → 改编引擎明确失败；关键是没走「按场批量写」那条拒绝
    assert body.get("ok") is False
    assert "原小说" in (body.get("msg") or "")
    log = (env["dir"] / "运行日志.txt").read_text(encoding="utf-8")
    assert "按场批量写" not in log


def test_express_write_routes_adapted_to_adapter(env):
    r = env["client"].post("/api/express-write", json={"key": DRAMA, "mode": "express"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False
    assert "原小说" in (body.get("msg") or "")
    log = (env["dir"] / "运行日志.txt").read_text(encoding="utf-8")
    assert "按场批量写" not in log


# ---------------------------------------------------------------- 断路器与文案

def test_manual_continue_resets_resume_budget(monkeypatch):
    plan = {"meta": {"supervisor": {"resumes": [1.0, 2.0, 3.0]}}}
    monkeypatch.setattr(sv, "_load", lambda k: ("script", "/tmp/x", "", plan))
    saved = {}
    monkeypatch.setattr(sv, "_save_budget", lambda *a, **k: saved.update(ok=True))
    sv.reset_resume_budget_by_key(DRAMA)
    assert plan["meta"]["supervisor"]["resumes"] == []
    assert saved.get("ok") is True


def test_idle_unfinished_reads_as_need_continue():
    """引擎没在跑时，未写完的作品是「待继续」，不是「创作中」。"""
    assert writer.display_state_label("in_progress", False, False, 48, 50) == "待继续"
    assert writer.display_state_label("in_progress", True, False, 48, 50) == "创作中"
    assert writer.display_state_label("in_progress", False, True, 48, 50) == "已暂停"
    assert writer.display_state_label("completed", False, False, 50, 50) == "已完成"
    # 集齐了但还没收尾 → 待校对，而不是继续创作
    assert writer.display_state_label("in_progress", False, False, 50, 50) == "待校对"


def test_library_item_labels_adapted_drama_need_continue(env):
    """作品库接口实打实下发「待继续」+ running=false。"""
    items = env["client"].get("/api/library").json()
    rows = items if isinstance(items, list) else (items.get("items") or [])
    row = next(i for i in rows if i.get("key") == DRAMA)
    assert row["state"] == "in_progress"
    assert row["running"] is False
    assert row["status_label"] == "待继续"


def test_library_item_label_is_running_honest(env, monkeypatch):
    """真的在跑时必须显示「创作中」——不能为了修文案把在跑的也改成待继续。"""
    from bot import livejob
    livejob.begin(DRAMA, kind="adapt", detail="正在写第 2 集")
    items = env["client"].get("/api/library").json()
    rows = items if isinstance(items, list) else (items.get("items") or [])
    row = next(i for i in rows if i.get("key") == DRAMA)
    assert row["running"] is True
    assert row["status_label"] == "创作中"
