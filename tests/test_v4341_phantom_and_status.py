"""v4.34.1：幻影双书根治 + 系统停状态语义回归。

覆盖：Project 跟随改名（在途线程写盘不再复活旧目录）、
_apply_title 规划在途时置 title_picked 不抢跑、
批量停/预算停/规划卡停不再冒充用户暂停、supervisor 文案如实、
resume 清系统停标记、rename_map 僵尸清扫。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import app as appmod
from bot import livejob, security, supervisor, writer


KEY = "20260101-000000-v4341测试书"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    writer._RUNNING_BOOKS.clear()
    livejob.reset_for_tests()
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(writer, "RENAME_MAP_FILE",
                        os.path.join(str(tmp_path), "rename_map.json"))
    yield
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    writer._RUNNING_BOOKS.clear()
    livejob.reset_for_tests()


def _mk_proj(tmp_path, name="旧书名", done=0, total=3, status="planning", meta=None):
    pdir = tmp_path / f"20260101-000000-{name}"
    pdir.mkdir(parents=True, exist_ok=True)
    chapters = []
    for i in range(1, total + 1):
        ch = {"chapterNumber": i, "title": f"第{i}章", "status": "pending",
              "wordCount": None, "filePath": f"第{i:02d}章-x.md"}
        chapters.append(ch)
    plan = {"novelName": name, "status": status, "totalChapters": total,
            "chapters": chapters, "config": {"batch_size": "1"},
            "meta": dict(meta or {})}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return writer.Project(str(pdir), os.path.basename(pdir))


# ---------- 修复A：Project 跟随改名 ----------

def test_project_follows_rename(tmp_path, monkeypatch):
    """改名后，握着旧路径的在途实例写盘必须落到新目录，且旧目录绝不复活。"""
    proj = _mk_proj(tmp_path, name="旧书名")
    old_path = proj.path
    new_key = "20260101-000000-新书上架"
    new_path = os.path.join(str(tmp_path), new_key)
    os.rename(old_path, new_path)                     # 模拟 _apply_title 的 os.rename
    writer.record_rename(os.path.basename(old_path), new_key)
    # 在途实例（不知道已改名）继续写盘
    plan = proj.plan()
    plan["novelName"] = "新书上架"
    proj.save_plan(plan)
    proj.log("迁移后的日志行")
    assert os.path.basename(proj.path) == new_key     # 实例已重定向
    assert not os.path.exists(old_path)               # 旧目录绝不复活
    plan2 = json.load(open(os.path.join(new_path, "02-写作计划.json"), encoding="utf-8"))
    assert plan2["novelName"] == "新书上架"
    assert "迁移后的日志行" in open(os.path.join(new_path, "运行日志.txt"), encoding="utf-8").read()


def test_project_no_false_redirect_without_alias(tmp_path):
    """目录消失但没有别名记录（如被删除）→ 不做任何重定向假设。"""
    proj = _mk_proj(tmp_path, name="被删的书")
    import shutil as _sh
    _sh.rmtree(proj.path)
    proj._follow_rename()
    assert proj.path.endswith("被删的书")   # 原地不动


# ---------- 修复B+C：选名与在途规划的协同 ----------

def test_apply_title_while_planning_sets_title_picked(tmp_path, monkeypatch):
    """规划在途（livejob 登记）→ 只置 title_picked，绝不抢跑 start_writing。"""
    proj = _mk_proj(tmp_path, name="原始任务描述", status="planning",
                    meta={"awaiting_title": True})
    appmod.PROJECT_INDEX[os.path.basename(proj.path)] = proj
    livejob.begin(os.path.basename(proj.path), kind="planning")   # 模拟 /api/create _bg 在途
    started = {}
    monkeypatch.setattr(writer, "start_writing",
                        lambda *a, **kw: started.setdefault("x", True) or True)
    res = appmod._apply_title(os.path.basename(proj.path), "复仇新书名")
    assert res["ok"]
    assert started == {}                              # 没有抢跑开写
    plan = json.load(open(os.path.join(proj.path, "02-写作计划.json"), encoding="utf-8"))
    assert plan["meta"]["title_picked"] is True       # 交给规划线程收尾开写
    assert plan["meta"]["awaiting_title"] is False
    assert os.path.basename(proj.path).endswith("复仇新书名")   # 目录已改名


def test_apply_title_without_planning_starts_writing(tmp_path, monkeypatch):
    """规划已结束 → 维持 v4.26.0 行为：直接开写（「选完书名就停」修复不回退）。"""
    proj = _mk_proj(tmp_path, name="待选名书", status="awaiting_title",
                    meta={"awaiting_title": True})
    appmod.PROJECT_INDEX[os.path.basename(proj.path)] = proj
    started = {}
    monkeypatch.setattr(writer, "start_writing",
                        lambda *a, **kw: started.setdefault("x", True) or True)
    res = appmod._apply_title(os.path.basename(proj.path), "定名之书")
    assert res["ok"] and res["started"] is True
    assert started.get("x")


# ---------- 修复E：系统停不再冒充用户暂停 ----------

def _mk_writable_proj(tmp_path, total=3):
    return _mk_proj(tmp_path, name="批量停测试", total=total, status="in_progress",
                    meta={"planning_running": False})


def test_budget_stop_is_not_user_pause(tmp_path):
    proj = _mk_writable_proj(tmp_path)
    writer._pause_for_budget(proj, "已达 token 预算上限（已用 100/100）")
    plan = json.load(open(os.path.join(proj.path, "02-写作计划.json"), encoding="utf-8"))
    assert plan["status"] == "in_progress"
    assert not plan["meta"].get("paused")             # 不再冒充用户暂停
    assert "预算上限" in plan["meta"].get("budget_stopped", "")


def test_resume_clears_system_stop_flags(tmp_path, monkeypatch):
    proj = _mk_writable_proj(tmp_path, total=2)
    plan_path = os.path.join(proj.path, "02-写作计划.json")
    plan = json.load(open(plan_path, encoding="utf-8"))
    plan["meta"].update({"batch_stopped": True, "plan_blocked": True,
                         "budget_stopped": "x"})
    json.dump(plan, open(plan_path, "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(writer, "start_writing", lambda *a, **kw: False)   # 防真起线程
    writer.resume_book(os.path.basename(proj.path))
    plan = json.load(open(plan_path, encoding="utf-8"))
    for flag in ("batch_stopped", "plan_blocked", "budget_stopped"):
        assert flag not in plan["meta"]


def test_supervisor_batch_stop_message(tmp_path, monkeypatch):
    """batch_stopped 的书：总编辑如实说「本批写完」，绝不说用户暂停。"""
    proj = _mk_proj(tmp_path, name="监工书", status="in_progress",
                    meta={"batch_stopped": True})
    monkeypatch.setattr(supervisor, "_load",
                        lambda k: ("novel", str(proj.path),
                                   os.path.join(str(proj.path), "02-写作计划.json"), proj.plan()))
    events = []
    monkeypatch.setattr(supervisor, "_evt",
                        lambda key, level, title, output="": events.append((title, output)))
    stopped = {}
    monkeypatch.setattr(supervisor, "stop", lambda k: stopped.setdefault(k, True))
    r = supervisor.tick_once(os.path.basename(proj.path))
    assert r["action"] == "batch_done"
    assert any("本批" in t for t, _ in events)
    assert not any("你点的暂停" in (t + o) for t, o in events)
    assert stopped


def test_supervisor_paused_message_is_honest(tmp_path, monkeypatch):
    """真暂停（meta.paused）：文案不再替用户认领「你点的暂停」。"""
    proj = _mk_proj(tmp_path, name="监工书2", status="paused", meta={"paused": True})
    monkeypatch.setattr(supervisor, "_load",
                        lambda k: ("novel", str(proj.path),
                                   os.path.join(str(proj.path), "02-写作计划.json"), proj.plan()))
    events = []
    monkeypatch.setattr(supervisor, "_evt",
                        lambda key, level, title, output="": events.append((title, output)))
    r = supervisor.tick_once(os.path.basename(proj.path))
    assert r["action"] == "standby"
    joined = " ".join(t + o for t, o in events)
    assert "你点的暂停" not in joined
    assert "已暂停" in joined


# ---------- 修复D：僵尸清扫 ----------

def test_sweep_removes_zero_chapter_zombie(tmp_path):
    """别名源目录与目标并存、0 完成章 → 自动删除（幻影残留）。"""
    old = _mk_proj(tmp_path, name="原始任务描述", status="awaiting_title",
                   meta={"awaiting_title": True})
    new_key = "20260101-000000-真书"
    new_dir = os.path.join(str(tmp_path), new_key)
    os.makedirs(new_dir, exist_ok=True)
    with open(os.path.join(new_dir, "02-写作计划.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"novelName": "真书", "status": "in_progress"}, ensure_ascii=False))
    writer.record_rename(os.path.basename(old.path), new_key)
    removed = writer.sweep_rename_zombies()
    assert os.path.basename(old.path) in removed
    assert not os.path.exists(old.path)               # 幻影已清
    assert os.path.isdir(new_dir)                     # 真书无恙


def test_sweep_quarantines_zombie_with_body(tmp_path):
    """残留有正文 → 隔离为 .zombie- 前缀，绝不静默删除。"""
    old = _mk_proj(tmp_path, name="有正文残留", status="in_progress")
    with open(os.path.join(old.path, "第01章-x.md"), "w", encoding="utf-8") as f:
        f.write("正文")
    new_key = "20260101-000000-改名真书"
    os.makedirs(os.path.join(str(tmp_path), new_key), exist_ok=True)
    writer.record_rename(os.path.basename(old.path), new_key)
    removed = writer.sweep_rename_zombies()
    assert any(r.startswith(".zombie-") for r in removed)
    assert not os.path.exists(old.path)


def test_sweep_keeps_books_without_alias_conflict(tmp_path):
    """旧目录不存在（正常改名）或目标不存在 → 什么都不动。"""
    old = _mk_proj(tmp_path, name="正常改名书")
    writer.record_rename(os.path.basename(old.path), "20260101-000000-不存在的目标")
    assert writer.sweep_rename_zombies() == []
    assert os.path.isdir(old.path)
