"""v4.33.1 回归：点「继续」必须立刻有反馈，九文体统一。

用户实拍：点「继续」后界面一动不动，等 30 秒以上才开始写，中途完全不知道
系统是在工作还是卡死；剧本尤其严重——准备全书素材要 1-2 分钟、单集要 130 秒，
这期间状态还是「0/50 集 · 0 字」。

修复契约（本文件逐条锁死）：
1. 接口在起线程**之前**登记 job 并写项目日志，返回体带 started / job；
2. job 里有具体的「谁在工作」（剧本引擎 / 小说引擎…）与阶段；
3. 失败也要有结论（job.finished=True, ok=False），不能静默；
4. /api/job 与 /api/book-live 都能读到这份登记；
5. 小说总编辑不再接手非小说项目（它会把剧本目录写成小说结构）；
6. 前端日志带按内容刷新，不再用失效的「数组长度」判据。
"""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

NOVEL = "20260101-000000-测试小说"
SCRIPT = "s_20260101-000000-测试剧本"


def _write_script(root: Path, *, status="writing", scenes=3):
    d = root / SCRIPT
    d.mkdir(exist_ok=True)
    rows = [{"no": n, "kind": "集", "place": f"第 {n} 集", "title": f"第{n}章",
             "status": "completed" if n == 1 else "pending",
             "fileName": f"第{n:03d}场.md", "wordCount": 800 if n == 1 else 0}
            for n in range(1, scenes + 1)]
    plan = {"version": 1, "scriptName": "测试剧本", "projectPath": str(d),
            "medium": "script", "form": "short_drama", "formLabel": "AI短剧",
            "entryPath": "idea", "status": status, "scenes": rows,
            "config": {"form": "short_drama", "title": "测试剧本"}, "meta": {}}
    (d / "剧本计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (d / "运行日志.txt").write_text("", encoding="utf-8")
    return d


def _write_novel(root: Path):
    d = root / NOVEL
    d.mkdir(exist_ok=True)
    chapters = [{"chapterNumber": n, "title": f"第{n}章", "status": "completed",
                 "filePath": f"第{n:03d}章.md"} for n in range(1, 4)]
    (d / "02-写作计划.json").write_text(
        json.dumps({"novelName": "测试小说", "chapters": chapters,
                    "config": {}, "meta": {}, "status": "in_progress"},
                   ensure_ascii=False), encoding="utf-8")
    (d / "运行日志.txt").write_text("", encoding="utf-8")
    return d


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from bot import writer, script_writer, security, livejob
    import app as appmod

    proot = tmp_path / "projects"
    proot.mkdir()
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(proot))
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(proot))
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    appmod.PROJECT_INDEX.clear()
    livejob.reset_for_tests()
    writer._PIPELINE_COUNT.clear()

    _write_novel(proot)
    _write_script(proot)
    client = TestClient(appmod.app)
    yield {"root": proot, "client": client, "app": appmod, "writer": writer,
           "script_writer": script_writer, "livejob": livejob}
    livejob.reset_for_tests()
    writer._PIPELINE_COUNT.clear()


# ---------------------------------------------------------------- 作业登记本身

def test_livejob_lifecycle():
    from bot import livejob

    livejob.reset_for_tests()
    assert livejob.get("k1") == {}

    row = livejob.begin("k1", kind="resume", worker="script_writer",
                        worker_label="剧本引擎", detail="正在启动…", total=10, done=2)
    assert row["worker_label"] == "剧本引擎"
    assert row["finished"] is False and row["ok"] is None

    livejob.update("k1", phase="writing", detail="正在写第 3/10 集", done=3)
    got = livejob.get("k1")
    assert got["phase"] == "writing" and got["done"] == 3
    assert got["elapsed"] >= 0

    livejob.finish("k1", ok=False, detail="还有 7 集待写")
    got = livejob.get("k1")
    assert got["finished"] is True and got["ok"] is False
    assert "7 集" in got["detail"]
    assert livejob.is_active("k1") is False

    # 新作业重新计时，不继承上一轮的 done
    row2 = livejob.begin("k1", kind="resume", detail="再来一次")
    assert row2["finished"] is False and row2["done"] == 0


def test_livejob_unknown_key_is_silent():
    from bot import livejob
    livejob.reset_for_tests()
    assert livejob.update("nobody", detail="x") == {}
    assert livejob.finish("nobody") == {}
    assert livejob.is_active("nobody") is False


# ---------------------------------------------------------------- /api/resume

def test_resume_script_returns_job_and_logs_before_thread(env, monkeypatch):
    """剧本：接口返回前就必须有「谁在工作」，并写进项目运行日志。"""
    from bot import script_writer

    calls = {}

    def fake_resume(key, batch_size=None):
        calls["key"] = key
        return script_writer.get_project(key)

    monkeypatch.setattr(script_writer, "resume_project", fake_resume)
    r = env["client"].post("/api/resume", json={"key": SCRIPT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["started"] is True
    job = body["job"]
    assert job["kind"] == "resume"
    assert job["worker_label"] == "剧本引擎"
    assert job["finished"] is False

    # 日志在接口返回时已经落盘（不等后台线程）
    log = (env["root"] / SCRIPT / "运行日志.txt").read_text(encoding="utf-8")
    assert "▶" in log and "正在恢复创作" in log

    for _ in range(60):
        if calls:
            break
        time.sleep(0.02)
    assert calls.get("key") == SCRIPT


def test_resume_script_already_running_still_reports(env, monkeypatch):
    """已经在跑也要有反馈，不能静默返回。"""
    env["writer"].mark_pipeline(SCRIPT, True)
    try:
        r = env["client"].post("/api/resume", json={"key": SCRIPT})
        body = r.json()
        assert body["ok"] is True and body["started"] is False
        assert body["job"]["finished"] is False
        assert body["job"]["worker_label"] == "剧本引擎"
    finally:
        env["writer"].mark_pipeline(SCRIPT, False)


def test_resume_novel_success_and_failure_are_both_visible(env, monkeypatch):
    """小说：成功有 job，失败有结论——两种都不能「点了没反应」。"""
    from bot import writer

    monkeypatch.setattr(writer, "resume_task",
                        lambda key, batch_size=None: {"ok": True, "kind": "writing",
                                                      "text": "继续写作第 2 章"})
    body = env["client"].post("/api/resume", json={"key": NOVEL}).json()
    assert body["started"] is True
    assert body["job"]["worker_label"] == "小说引擎"
    assert "继续写作" in body["job"]["detail"]

    env["livejob"].reset_for_tests()
    monkeypatch.setattr(writer, "resume_task",
                        lambda key, batch_size=None: {"ok": False, "text": "没有未完成的任务"})
    body = env["client"].post("/api/resume", json={"key": NOVEL}).json()
    assert body["ok"] is False
    assert body["started"] is False
    assert body["job"]["finished"] is True and body["job"]["ok"] is False
    assert "没有未完成" in body["job"]["detail"]


def test_pause_updates_job_phase(env, monkeypatch):
    """暂停也要立刻改文案：用户点了必须有反应。"""
    env["livejob"].begin(SCRIPT, kind="resume", worker_label="剧本引擎", detail="正在写第 3 集")
    r = env["client"].post("/api/pause", json={"key": SCRIPT})
    assert r.status_code == 200, r.text
    assert r.json()["job"]["phase"] == "pausing"
    assert "暂停" in r.json()["job"]["detail"]


# ---------------------------------------------------------------- 查询接口

def test_job_endpoint_and_book_live_expose_job(env):
    env["livejob"].begin(SCRIPT, kind="resume", worker_label="剧本引擎",
                         detail="正在写第 3/10 集", total=10, done=2)
    r = env["client"].get("/api/job", params={"key": SCRIPT})
    assert r.status_code == 200
    assert r.json()["job"]["worker_label"] == "剧本引擎"

    r = env["client"].get("/api/book-live", params={"key": SCRIPT})
    assert r.status_code == 200, r.text
    assert r.json()["job"]["detail"] == "正在写第 3/10 集"


def test_job_endpoint_self_heals_dead_worker(env, monkeypatch):
    """线程异常退出没走到收口：项目确实不在跑时，/api/job 要主动结束，别让前端永远转圈。"""
    env["livejob"].begin(SCRIPT, kind="resume", worker_label="剧本引擎", detail="正在写…")
    # 把开始时间往前拨 60 秒
    from bot import livejob
    with livejob._LOCK:
        livejob._ACTIVE[SCRIPT]["started_at"] -= 60
    monkeypatch.setattr(env["app"], "_medium_running", lambda key: False)
    body = env["client"].get("/api/job", params={"key": SCRIPT}).json()
    assert body["job"]["finished"] is True


# ---------------------------------------------------------------- 总编辑只服务小说

def test_mark_pipeline_only_watches_novel(env, monkeypatch):
    """非小说不再孵化小说总编辑——它会在剧本目录写章卡/状态库/伏笔账本。"""
    from bot import supervisor

    seen = []
    monkeypatch.setattr(supervisor, "ensure", lambda key: seen.append(("ensure", key)))
    monkeypatch.setattr(supervisor, "pipeline_stopped", lambda key: seen.append(("stop", key)))

    env["writer"].mark_pipeline(SCRIPT, True)
    env["writer"].mark_pipeline(SCRIPT, False)
    assert seen == [], "剧本不该孵化小说总编辑"

    env["writer"].mark_pipeline(NOVEL, True)
    env["writer"].mark_pipeline(NOVEL, False)
    assert ("ensure", NOVEL) in seen and ("stop", NOVEL) in seen


def test_mark_pipeline_finishes_job_on_stop(env):
    """流水线下车要收口 job，否则按钮一直「启动中…」。"""
    env["livejob"].begin(SCRIPT, kind="resume", worker_label="剧本引擎", detail="正在写…")
    env["writer"].mark_pipeline(SCRIPT, True)
    env["writer"].mark_pipeline(SCRIPT, False)
    row = env["livejob"].get(SCRIPT)
    assert row["finished"] is True and row["ok"] is True


# ---------------------------------------------------------------- 前端接线

FRONT = Path(__file__).resolve().parents[1] / "static" / "v2" / "js"


def test_jobfeed_module_is_wired_into_both_entry_points():
    planbar = (FRONT / "components" / "planbar.js").read_text(encoding="utf-8")
    sidebar = (FRONT / "components" / "sidebar.js").read_text(encoding="utf-8")
    for src in (planbar, sidebar):
        assert "jobfeed.js" in src
        assert "startJobFeedback" in src and "attachJobFeedback" in src
    # 「继续」分支必须在 await 之前先弹卡片
    i = planbar.index("} else if (st === 'resume') {\n      optimistic = 'resuming';")
    head = planbar[i:i + 1200]
    assert "startJobFeedback" in head
    assert head.index("startJobFeedback") < head.index("await api.resume")


def test_live_log_refreshes_by_content_not_length():
    app_js = (FRONT / "app.js").read_text(encoding="utf-8")
    assert "liveSig" in app_js
    assert "arr.length >= lines.length" not in app_js


def test_jobfeed_has_no_blocking_modal():
    """卡片必须是非阻塞浮层：不能有遮罩，不能锁页面滚动。"""
    src = (FRONT / "core" / "jobfeed.js").read_text(encoding="utf-8")
    assert "position: 'fixed'" in src
    assert "pointerEvents: 'none'" in src
    assert "document.body.style.overflow" not in src
    assert "backdrop" not in src.lower()
