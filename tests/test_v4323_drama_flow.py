"""v4.32.3 回归：应用中文名 + 小说转短剧「查看剧本 / 继续创作」全链路。

用户实拍的三个问题：
1. DMG 里应用显示成英文 YiMuLit（卷名 / .app 目录名都还是英文）；
2. 「小说 → AI 短剧」弹窗点「查看剧本」毫无反应（弹窗不关 + 按钮禁用 +
   新项目没进左侧列表 → 路由渲染「作品不存在」）；
3. 改编剧本实际在写作、日志在推进，工作台却 0/0 场；点「继续」提示
   「无法继续，没有未完成的任务」，作品库里点开也一样。

根因：改编生成的剧本 key 是「<小说key>-短剧」，没有 s_ 前缀，而
app.py / v2api.py 多处只按 key 前缀认文体 → 当小说处理：继续走
writer.resume_task 找小说任务档案、/api/plan 用 longform 在剧本目录里
写出 02c-总规划与章卡。同时改编线程只在全部集数跑完后一次性回填 scenes，
进行中永远 0/0，且只有分镜阶段才置 running。
"""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SRC = "20260101-000000-测试小说"
DRAMA = SRC + "-短剧"


def _drama_plan(root, *, done=3, status="writing", storyboard=False):
    """造一个改编剧本项目：前 done 集有正文，其余留 pending。"""
    d = root / DRAMA
    d.mkdir(exist_ok=True)
    body = d / "04-正文"
    body.mkdir(exist_ok=True)
    scenes = []
    for no in range(1, 4):
        ok = no <= done
        fn = f"第{no:03d}场.md"
        if ok:
            (body / fn).write_text(f"内景 某地 日\n\n第 {no} 集正文。", encoding="utf-8")
        scenes.append({"no": no, "kind": "集", "place": f"第 {no} 集",
                       "status": "completed" if ok else "pending",
                       "fileName": fn, "wordCount": 60 if ok else 0})
    plan = {
        "version": 1, "scriptName": "测试小说·短剧", "projectPath": str(d),
        "medium": "script", "form": "short_drama", "formLabel": "AI短剧",
        "entryPath": "adapt", "status": status, "scenes": scenes,
        "adaptedFrom": {"key": SRC, "chapters": [1, 2, 3]},
        "config": {"form": "short_drama", "title": "测试小说·短剧"},
        "meta": {"storyboard_done": bool(storyboard)},
    }
    (d / "剧本计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return d


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from bot import writer, script_writer, security, llm
    import app as appmod

    proot = tmp_path / "projects"
    proot.mkdir()
    # script_writer.PROJECTS_DIR 是 import 时从 writer 复制的，两处都要改
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(proot))
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(proot))
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(llm, "book", lambda *a, **k: None, raising=False)
    appmod.PROJECT_INDEX.clear()
    appmod._ADAPT_RUNNING.clear()

    novel = proot / SRC
    novel.mkdir()
    chapters = [{"chapterNumber": n, "title": f"第{n}章", "status": "completed",
                 "filePath": f"第{n:03d}章.md"} for n in range(1, 4)]
    (novel / "02-写作计划.json").write_text(
        json.dumps({"novelName": "测试小说", "chapters": chapters,
                    "config": {}, "meta": {}}, ensure_ascii=False), encoding="utf-8")
    for n in range(1, 4):
        (novel / f"第{n:03d}章.md").write_text(f"第{n}章正文。" * 30, encoding="utf-8")

    yield {"root": proot, "novel": novel, "client": TestClient(appmod.app)}
    appmod.PROJECT_INDEX.clear()
    appmod._ADAPT_RUNNING.clear()


# ---------------------------------------------------------------- 文体识别

def test_adapted_drama_is_detected_as_script(env):
    """没有 s_ 前缀的改编剧本，必须按计划文件认成 script。"""
    from bot import writer, script_writer, v2api
    import app as appmod

    _drama_plan(env["root"])
    assert writer.medium_of_key(DRAMA) == "script"
    assert v2api._medium_of_key(DRAMA) == "script"
    assert appmod._nonnovel_module(DRAMA) is script_writer
    assert appmod._is_nonnovel(DRAMA) is True
    assert appmod._nonnovel_module(SRC) is None
    assert writer.medium_of_key(SRC) == "novel"


def test_nonnovel_pause_never_pollutes_novel_project(env):
    """旧 _nonnovel_set_paused 遍历 8 个引擎，会在小说目录写出空的剧本计划。"""
    import app as appmod

    assert appmod._nonnovel_set_paused(SRC, True) is False
    assert not (env["novel"] / "剧本计划.json").exists()


def test_plan_and_state_do_not_write_novel_files_into_drama(env):
    """改编剧本打开后不能触发 longform 迁移（02c/02d/章卡/状态库）。"""
    d = _drama_plan(env["root"])
    r = env["client"].get("/api/plan", params={"key": DRAMA})
    assert r.status_code == 200, r.text
    assert not (d / "02c-总规划.json").exists()
    assert not (d / "02d-章卡.jsonl").exists()
    assert not (d / "18-状态库.json").exists()
    r2 = env["client"].get("/api/state", params={"key": DRAMA})
    assert r2.status_code == 200, r2.text


# ---------------------------------------------------------------- 继续 / 断点恢复

def test_resume_adapted_drama_never_reports_no_task(env, monkeypatch):
    """用户截图里那句「无法继续，没有未完成的任务」必须消失。"""
    import app as appmod

    _drama_plan(env["root"], done=3, storyboard=False)
    calls = []
    monkeypatch.setattr(appmod, "_storyboard_resume", lambda key: calls.append(key))
    r = env["client"].post("/api/resume", json={"key": DRAMA})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "没有未完成的任务" not in body["msg"]
    assert "分镜" in body["msg"]
    assert body["running"] is True and body["state"] == "in_progress"
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)
    assert calls == [DRAMA]


def test_resume_only_reruns_missing_episodes(env, monkeypatch):
    """断点续跑只补缺的集，不把已完成的集重写一遍。"""
    import app as appmod

    _drama_plan(env["root"], done=2, storyboard=False)
    captured = {}

    def fake_impl(src_key, form, chapters, key, src_plan, proj=None, resume=False):
        captured.update(chapters=[c["chapterNumber"] for c in chapters],
                        resume=resume, key=key)
        return {"ok": True}

    monkeypatch.setattr(appmod, "_novel_to_drama_impl", fake_impl)
    r = env["client"].post("/api/resume", json={"key": DRAMA})
    assert r.status_code == 200, r.text
    assert "剩余 1 集" in r.json()["msg"]
    for _ in range(50):
        if captured:
            break
        time.sleep(0.02)
    assert captured.get("chapters") == [3]
    assert captured.get("resume") is True
    assert captured.get("key") == DRAMA


def test_continue_route_uses_adaptation_resume(env, monkeypatch):
    """/api/continue 是 /api/resume 的下游，也必须走改编专用恢复。"""
    import app as appmod

    _drama_plan(env["root"], done=3, storyboard=True)
    seen = []
    monkeypatch.setattr(appmod, "_resume_novel_to_drama",
                        lambda key: seen.append(key) or {"ok": True, "msg": "已完成",
                                                         "running": False,
                                                         "paused": False,
                                                         "state": "completed"})
    r = env["client"].post("/api/continue", json={"key": DRAMA})
    assert r.status_code == 200, r.text
    assert seen == [DRAMA]
    assert r.json()["state"] == "completed"


# ---------------------------------------------------------------- 实时进度 / 防重入

def test_adaptation_writes_progress_after_every_episode(env, monkeypatch):
    """改编进行中必须看到 x/3 而不是 0/0；每集写完立即落盘。"""
    from bot import llm, script_writer
    import app as appmod

    proot = env["root"]
    drama = proot / DRAMA
    snapshots = []

    def fake_chat(messages, *a, **k):
        fp = drama / "剧本计划.json"
        if fp.exists():
            p = json.loads(fp.read_text(encoding="utf-8"))
            snapshots.append(sum(1 for s in p.get("scenes") or []
                                 if s.get("status") == "completed"))
        return "内景 某地 日\n\n张三：走。" * 20

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(script_writer, "gen_storyboard",
                        lambda key: {"ok": True, "shots": 3, "scenes": 3})
    plan = json.loads((proot / SRC / "02-写作计划.json").read_text(encoding="utf-8"))
    src_proj = appmod._get_project(SRC)
    res = appmod._novel_to_drama_impl(SRC, "short_drama", plan["chapters"],
                                      DRAMA, plan, src_proj)
    assert snapshots == [0, 1, 2], snapshots   # 每次 LLM 调用前，前几集已落盘
    assert res["done"] == 3 and res["pending"] == 0
    final = json.loads((drama / "剧本计划.json").read_text(encoding="utf-8"))
    assert len(final["scenes"]) == 3
    assert final["totalScenes"] == 3
    assert all(s["status"] == "completed" for s in final["scenes"])
    assert final["status"] == "completed"
    assert (final.get("meta") or {}).get("storyboard_done") is True
    assert (drama / "04-正文" / "第001场.md").exists()
    assert "已完成" in (drama / "03-场次清单.md").read_text(encoding="utf-8")


def test_adaptation_claim_is_exclusive(env):
    import app as appmod

    assert appmod._adapt_claim(DRAMA) is True
    assert appmod._adapt_claim(DRAMA) is False
    assert appmod._adapt_busy(DRAMA) is True
    appmod._adapt_release(DRAMA)
    assert appmod._adapt_busy(DRAMA) is False


def test_novel_to_drama_rejects_duplicate_run(env, monkeypatch):
    """连点两次「转短剧」不能起两条线程写同一份计划。"""
    import app as appmod

    monkeypatch.setattr(appmod, "_novel_to_drama_impl", lambda *a, **k: time.sleep(0.3))
    r1 = env["client"].post("/api/novel-to-drama", json={"key": SRC})
    assert r1.status_code == 200, r1.text
    assert r1.json().get("already_running") is not True
    r2 = env["client"].post("/api/novel-to-drama", json={"key": SRC})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("already_running") is True
    assert r2.json()["key_hint"] == DRAMA
    for _ in range(50):
        if not appmod._adapt_busy(DRAMA):
            break
        time.sleep(0.02)


# ---------------------------------------------------------------- 前端与打包接线

def test_view_script_button_creates_new_session_and_closes_modal():
    root = Path(__file__).resolve().parents[1]
    rp = (root / "static/v2/js/components/rightpanel.js").read_text(encoding="utf-8")
    assert "openDramaSession" in rp
    assert "dramaModal.close()" in rp
    assert "clearInterval(pollInterval)" in rp
    assert "addPendingWork({ key: newKey, name: dramaName, medium: 'script' })" in rp
    assert "navigate(routePath('work', { key: newKey }))" in rp
    assert "disabled: true" not in rp[rp.index("const statusBtn"):rp.index("const body")]

    sidebar = (root / "static/v2/js/components/sidebar.js").read_text(encoding="utf-8")
    assert "res.ok === false" in sidebar
    app_js = (root / "static/v2/js/app.js").read_text(encoding="utf-8")
    assert "!mediumBench && ctx.refreshPlan()" in app_js
    assert "ctx.refreshWork()" in app_js


def test_desktop_app_name_is_chinese_everywhere():
    root = Path(__file__).resolve().parents[1]
    yml = (root / ".github/workflows/desktop.yml").read_text(encoding="utf-8")
    assert '"$STAGE/一幕神笔.app"' in yml
    assert '-volname "一幕神笔"' in yml
    assert "Print :CFBundleName" in yml and "Print :CFBundleDisplayName" in yml
    spec = (root / "desktop/yimulit_mac.spec").read_text(encoding="utf-8")
    assert '"CFBundleName": "一幕神笔"' in spec
    assert '"CFBundleDisplayName": "一幕神笔"' in spec
    launcher = (root / "desktop/launcher.py").read_text(encoding="utf-8")
    assert '"一幕神笔",' in launcher
    iss = (root / "desktop/windows/installer.iss").read_text(encoding="utf-8-sig")
    assert '#define AppName "一幕神笔"' in iss
