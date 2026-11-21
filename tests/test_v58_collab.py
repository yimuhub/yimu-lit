"""v5.8.0 人机共同创作模式（逐章确认）回归。

核心契约（对齐易创哲学：AI 只产候选，作者确认才成为正式内容）：
· collab_review 开 → _write_loop 每章写完（写盘+评分/门禁既有链路之后）置
  paused=True + pausedReason=await_collab:<n>，章节卡尾追加协作提示行，循环停；
· collab 关 → 现行为不变（连写不停）；express 极速 / 一键完稿不启用协作停；
· 「继续/采用继续」= resume_book 清 pausedReason 后续写，回复语明确
  「第 n 章已按你确认采用，继续第 n+1 章」；重启后 resume 落在同一待确认章；
· chief collab_mode 开关 + write mode=collab_next；「重写本章」在 await_collab
  状态下重写后仍停在本章待确认（不自动继续）。
全部用例跑在 tmp_path，mock LLM（conftest autouse），不连网、不碰真实数据。
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
from bot import livejob, security, writer

KEY = "20260101-000000-v58协作书"


# ---------------------------------------------------------------- 脚手架

def _mk_novel(tmp_path, total=2, collab=False, express=False, done=0, batch="all"):
    """造一本 v58 测试书：chapters 占位 + config/collab 开关。"""
    pdir = tmp_path / KEY
    pdir.mkdir(parents=True, exist_ok=True)
    chapters = []
    for i in range(1, total + 1):
        completed = i <= done
        fn = f"第{i:02d}章.md"
        chapters.append({"chapterNumber": i, "title": f"第{i}章", "filePath": fn,
                         "status": "completed" if completed else "pending",
                         "wordCount": 3000 if completed else None})
        if completed:
            (pdir / fn).write_text("正文" * 1500, encoding="utf-8")
    meta = {}
    if collab:
        meta["collab_review"] = True
    plan = {"novelName": "v58协作书", "status": "in_progress", "totalChapters": total,
            "minWordsPerChapter": 3000, "chapters": chapters, "meta": meta,
            "config": {"batch_size": batch, "mode": "rolling",
                       **({"express": True} if express else {})}}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False),
                                          encoding="utf-8")
    return writer.Project(str(pdir), KEY)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(writer, "_RENAME_MAP_CACHE", {"mtime": None, "map": {}})
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    writer._RUNNING_BOOKS.clear()
    writer._COLLAB_REWRITE_PENDING.clear()
    livejob.reset_for_tests()
    yield
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    writer._RUNNING_BOOKS.clear()
    writer._COLLAB_REWRITE_PENDING.clear()
    livejob.reset_for_tests()


def _stub_engine(monkeypatch):
    """把写作链路的 LLM/重环节全部钉死：_write_chapter 真实置 completed（让协作
    检查点看到「本章写完」），其余重环节 no-op——与 test_v4342 同款收口。"""
    def _fake_write_chapter(proj, ch, mw, mr):
        no = ch["chapterNumber"]
        with proj.lock:
            plan = proj.plan() or {}
            for c in plan.get("chapters", []):
                if c.get("chapterNumber") == no:
                    c["status"] = "completed"
                    c["wordCount"] = 3200
            proj.save_plan(plan)
        try:
            fn = next(c.get("filePath") for c in (proj.plan() or {}).get("chapters", [])
                      if c.get("chapterNumber") == no)
            with open(os.path.join(proj.path, fn), "w", encoding="utf-8") as f:
                f.write(f"# 第{no}章\n\n正文" * 60)
        except Exception:
            pass

    monkeypatch.setattr(writer, "_write_chapter", _fake_write_chapter)
    monkeypatch.setattr(writer, "_maybe_replan", lambda p, n: None)
    monkeypatch.setattr(writer, "_ensure_card_planned", lambda p, n, tries=2: True)
    monkeypatch.setattr(writer, "_validate_and_finish", lambda p: None)
    monkeypatch.setattr(writer, "_skeleton_refresh", lambda k: None)
    monkeypatch.setattr(writer, "_ensure_layered_plan", lambda p: None)
    monkeypatch.setattr(writer.workflow, "plan_drift_check", lambda *a, **k: None)


def _sync_write_thread(monkeypatch):
    """让 resume_book / start_writing 的写作线程同步跑完（测试内直接执行循环体）。"""
    monkeypatch.setattr(writer, "_write_loop_safe",
                        lambda proj, bs=None: writer._write_loop(proj, bs), raising=False)


def _wait_idle(bk, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not writer._book_running(bk) and not writer.pipeline_running(bk):
            return True
        time.sleep(0.05)
    return False


def _session():
    return {"project": KEY, "session": "s-v58", "title_candidates": None}


def _chief_decision(monkeypatch, actions, reply="收到。"):
    monkeypatch.setattr(chief_mod, "deliberate",
                        lambda msg, ctx: {"understanding": "测试", "reply": reply,
                                          "actions": actions, "confidence": 0.99})


def _plan(proj):
    return proj.plan() or {}


def _meta(proj):
    return (_plan(proj).get("meta") or {})


# ---------------------------------------------------------------- 1) 循环检查点

def test_collab_on_pauses_after_first_chapter(monkeypatch, tmp_path):
    """collab 开 + 批量一口气：第 1 章写完即停——paused + pausedReason=await_collab:1，
    第 2 章未动。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    m = _meta(proj)
    assert m.get("paused") is True
    assert m.get("pausedReason") == "await_collab:1"
    assert _plan(proj).get("status") == "paused"
    chs = {c["chapterNumber"]: c for c in _plan(proj)["chapters"]}
    assert chs[1]["status"] == "completed"
    assert chs[2]["status"] == "pending"


def test_collab_pause_appends_chat_hint_and_journal(monkeypatch, tmp_path):
    """协作停时：chat_log 尾追加协作提示行（type=collab_review 带章号），
    任务档案标 paused/await_collab，运行日志有「等待你把关」。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    log = os.path.join(proj.path, "chat_log.jsonl")
    rows = [json.loads(x) for x in open(log, encoding="utf-8") if x.strip()]
    hint = [r for r in rows if (r.get("type") or r.get("extra", {}).get("type")) == "collab_review"]
    assert hint, f"章节卡尾没有协作提示行：{rows[-1:]}"
    assert hint[-1]["no"] == 1 and "等待你把关" in hint[-1]["content"]
    assert "继续" in hint[-1]["content"] and "重写本章" in hint[-1]["content"]
    jd = json.load(open(os.path.join(proj.path, "任务档案.json"), encoding="utf-8"))
    assert jd["active"]["status"] == "paused"
    assert "await_collab" in str(jd["active"].get("stage") or "") or "确认" in str(jd["active"].get("note") or "")
    runlog = open(os.path.join(proj.path, "运行日志.txt"), encoding="utf-8").read()
    assert "等待你把关" in runlog


def test_collab_off_writes_through(monkeypatch, tmp_path):
    """collab 关：连写两章不停，行为与旧版完全一致。"""
    proj = _mk_novel(tmp_path, total=2, collab=False)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    m = _meta(proj)
    assert not m.get("paused") and not m.get("pausedReason")
    chs = {c["chapterNumber"]: c for c in _plan(proj)["chapters"]}
    assert chs[1]["status"] == "completed" and chs[2]["status"] == "completed"


def test_express_ignores_collab(monkeypatch, tmp_path):
    """express 极速模式不启用协作停：collab 开也一口气直出（可后开）。"""
    proj = _mk_novel(tmp_path, total=2, collab=True, express=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    m = _meta(proj)
    assert not m.get("paused") and not m.get("pausedReason")
    chs = {c["chapterNumber"]: c for c in _plan(proj)["chapters"]}
    assert chs[1]["status"] == "completed" and chs[2]["status"] == "completed"


def test_collab_dynamic_toggle_applies_next_chapter(monkeypatch, tmp_path):
    """动态语义：写到一半（两批之间）开协作，下一章检查点即生效。"""
    proj = _mk_novel(tmp_path, total=2, collab=False, done=1)
    _stub_engine(monkeypatch)
    monkeypatch.setattr(writer, "_write_loop_safe", lambda *a, **k: None, raising=False)
    # 第一批（batch=1）写第 2 章前把协作打开
    orig = writer._write_chapter

    def _toggle_then_write(p, ch, mw, mr):
        plan = p.plan() or {}
        plan.setdefault("meta", {})["collab_review"] = True
        p.save_plan(plan)
        orig(p, ch, mw, mr)

    monkeypatch.setattr(writer, "_write_chapter", _toggle_then_write)
    writer._write_loop(proj, batch_size=1)
    assert _meta(proj).get("pausedReason") == "await_collab:2"


# ---------------------------------------------------------------- 2) resume 语义

def test_resume_adopts_awaiting_chapter_and_writes_next(monkeypatch, tmp_path):
    """协作停在第 1 章 → resume_book = 采用并续写：日志明确「已按你确认采用」，
    第 2 章写完又停（await_collab:2）。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    assert _meta(proj).get("pausedReason") == "await_collab:1"
    _sync_write_thread(monkeypatch)
    assert writer.resume_book(KEY) is True
    assert _wait_idle(os.path.basename(proj.path))
    m = _meta(proj)
    runlog = open(os.path.join(proj.path, "运行日志.txt"), encoding="utf-8").read()
    assert "第 1 章已按你确认采用，继续第 2 章" in runlog
    chs = {c["chapterNumber"]: c for c in _plan(proj)["chapters"]}
    assert chs[1]["status"] == "completed" and chs[2]["status"] == "completed"
    assert m.get("paused") is True and m.get("pausedReason") == "await_collab:2"


def test_restart_resume_lands_on_same_awaiting_chapter(monkeypatch, tmp_path):
    """断点恢复（重启后）：pausedReason 落盘 → 新 Project 实例（进程重启模拟）
    仍停在待确认章；resume 落在同一章的确认位上继续。"""
    proj = _mk_novel(tmp_path, total=3, collab=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size=1)
    assert _meta(proj).get("pausedReason") == "await_collab:1"
    # 重启模拟：全新实例 + 共享注册表清空（conftest 已清，这里再显式一次）
    writer._RUNNING_BOOKS.clear()
    proj2 = writer.Project(writer._project_dir(KEY), KEY)
    assert writer.collab_awaiting_key(KEY) == 1
    st = proj2.status()
    assert st["paused"] is True and st["pausedReason"] == "await_collab:1"
    _sync_write_thread(monkeypatch)
    assert writer.resume_book(KEY) is True
    assert _wait_idle(os.path.basename(proj2.path))
    chs = {c["chapterNumber"]: c for c in (proj2.plan() or {})["chapters"]}
    assert chs[2]["status"] == "completed"
    assert (proj2.plan() or {}).get("meta", {}).get("pausedReason") == "await_collab:2"


def test_collab_awaiting_key_requires_paused_and_reason(monkeypatch, tmp_path):
    """collab_awaiting_key 的判定口径：paused+await_collab:<n> 才算待确认；
    人工暂停/普通 paused/未暂停都不算（防「继续」误当采用）。"""
    proj = _mk_novel(tmp_path, total=2)
    assert writer.collab_awaiting_key(KEY) == 0
    # paused 但原因是人工暂停
    plan = _plan(proj)
    plan.setdefault("meta", {})["paused"] = True
    plan["meta"]["pausedReason"] = ""
    plan["status"] = "paused"
    proj.save_plan(plan)
    assert writer.collab_awaiting_key(KEY) == 0
    # 协作停
    plan["meta"]["pausedReason"] = "await_collab:1"
    proj.save_plan(plan)
    assert writer.collab_awaiting_key(KEY) == 1
    # paused=False（如协作重写进行中）不算
    plan["meta"]["paused"] = False
    proj.save_plan(plan)
    assert writer.collab_awaiting_key(KEY) == 0


# ---------------------------------------------------------------- 3) chief 动作

def test_chief_collab_mode_on_and_off(monkeypatch, tmp_path):
    """「以后每章写完都停下来等我确认」→ collab_mode(enabled=true) 落 meta；
    「不用逐章确认了，自动写」→ 关。"""
    proj = _mk_novel(tmp_path, total=2)
    _chief_decision(monkeypatch, [{"action": "collab_mode", "params": {"enabled": True}}])
    out = appmod._chat_attached(_session(), proj, "以后每章写完都停下来等我确认")
    assert _meta(proj).get("collab_review") is True
    assert "逐章确认" in out
    _chief_decision(monkeypatch, [{"action": "collab_mode", "params": {"enabled": False}}])
    out2 = appmod._chat_attached(_session(), proj, "不用逐章确认了，自动写")
    assert _meta(proj).get("collab_review") is False
    assert "自动连续写作" in out2


def test_chief_collab_mode_off_while_awaiting_resumes(monkeypatch, tmp_path):
    """停在待确认章时关闭协作 = 作者放行：直接续写完余下章节。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    assert _meta(proj).get("pausedReason") == "await_collab:1"
    _sync_write_thread(monkeypatch)
    _chief_decision(monkeypatch, [{"action": "collab_mode", "params": {"enabled": False}}])
    out = appmod._chat_attached(_session(), proj, "不用逐章确认了，自动写")
    assert _wait_idle(os.path.basename(proj.path))
    assert "视为已确认" in out
    chs = {c["chapterNumber"]: c for c in _plan(proj)["chapters"]}
    assert chs[2]["status"] == "completed"


def test_chief_write_collab_next_adopts_and_continues(monkeypatch, tmp_path):
    """「继续」= write(mode=collab_next)：采用待确认章并续写下一章，回复点明章号。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    assert _meta(proj).get("pausedReason") == "await_collab:1"
    _sync_write_thread(monkeypatch)
    _chief_decision(monkeypatch, [{"action": "write", "params": {"mode": "collab_next"}}])
    out = appmod._chat_attached(_session(), proj, "可以，继续")
    assert "第 1 章已按你确认采用" in out
    assert _wait_idle(os.path.basename(proj.path))
    chs = {c["chapterNumber"]: c for c in _plan(proj)["chapters"]}
    assert chs[1]["status"] == "completed" and chs[2]["status"] == "completed"
    assert _meta(proj).get("pausedReason") == "await_collab:2"


def test_chief_collab_next_without_awaiting(monkeypatch, tmp_path):
    """没有待确认章时 collab_next 如实提示，不乱启动写作。"""
    proj = _mk_novel(tmp_path, total=2)
    _chief_decision(monkeypatch, [{"action": "write", "params": {"mode": "collab_next"}}])
    out = appmod._chat_attached(_session(), proj, "继续")
    assert "没有停在待确认" in out
    chs = {c["chapterNumber"]: c for c in _plan(proj)["chapters"]}
    assert chs[1]["status"] == "pending"    # 没有偷偷开写


# ---------------------------------------------------------------- 4) 协作重写

def test_collab_rewrite_still_awaits_after_finish(monkeypatch, tmp_path):
    """await_collab 状态下「重写本章」：begin 解暂停位让重写能跑，end 恢复
    「仍停在本章待确认」——不自动继续写下一章。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    assert writer.collab_awaiting_key(KEY) == 1
    n = writer.begin_collab_rewrite(KEY)
    assert n == 1
    assert writer.collab_awaiting_key(KEY) == 0      # 重写期间不拦（暂停位已解）
    writer.end_collab_rewrite(KEY, {"ok": True})
    assert writer.collab_awaiting_key(KEY) == 1      # 重写完仍停在本章待确认
    m = _meta(proj)
    assert m.get("paused") is True and m.get("pausedReason") == "await_collab:1"


def test_collab_rewrite_cancelled_does_not_restore(monkeypatch, tmp_path):
    """重写被新指令取代（superseded/cancelled）→ 不恢复旧待确认位。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    assert writer.begin_collab_rewrite(KEY) == 1
    writer.end_collab_rewrite(KEY, {"cancelled": True})
    assert writer.collab_awaiting_key(KEY) == 0


def test_start_rewrite_wraps_collab_restore(monkeypatch, tmp_path):
    """/api/rewrite → _start_rewrite 全链路：协作待确认章重写后自动恢复待确认位。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    monkeypatch.setattr(writer, "run_guided_rewrite",
                        lambda key, **k: {"ok": True, "changed": [1]})
    ok = appmod._start_rewrite(KEY, chapters=[1], dimensions=["改写"], max_rounds=1,
                               supersede=False)
    assert ok is True
    deadline = time.time() + 5
    while time.time() < deadline and appmod._REWRITING.get(KEY, {}).get("running"):
        time.sleep(0.05)
    # end_collab_rewrite 在 finally 里跑（running 翻 False 之后一瞬），轮询等它落位
    deadline = time.time() + 3
    restored = 0
    while time.time() < deadline:
        restored = writer.collab_awaiting_key(KEY)
        if restored == 1:
            break
        time.sleep(0.05)
    assert restored == 1
    assert _meta(proj).get("pausedReason") == "await_collab:1"


# ---------------------------------------------------------------- 5) 状态/接口

def test_project_status_passthrough(monkeypatch, tmp_path):
    """Project.status 透传 pausedReason / collab_review —— /api/status → 前端
    store.work 的唯一数据源，message.js 协作条靠它渲染。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    plan = _plan(proj)
    plan.setdefault("meta", {}).update(paused=True, pausedReason="await_collab:1")
    plan["status"] = "paused"
    proj.save_plan(plan)
    st = proj.status()
    assert st["paused"] is True
    assert st["pausedReason"] == "await_collab:1"
    assert st["collab_review"] is True


def test_collab_review_api_toggle(monkeypatch, tmp_path):
    """设置页数据源：GET/POST /api/collab-review 开关本书逐章确认。"""
    proj = _mk_novel(tmp_path, total=2)
    appmod.PROJECT_INDEX[KEY] = proj
    c = TestClient(appmod.app)
    r0 = c.get("/api/collab-review", params={"key": KEY})
    assert r0.status_code == 200 and r0.json()["enabled"] is False
    r1 = c.post("/api/collab-review", json={"key": KEY, "enabled": True})
    assert r1.status_code == 200 and r1.json()["ok"] is True
    assert _meta(proj).get("collab_review") is True
    r2 = c.get("/api/collab-review", params={"key": KEY})
    assert r2.json()["enabled"] is True and r2.json()["awaiting"] == 0
    r3 = c.post("/api/collab-review", json={"key": KEY, "enabled": False})
    assert _meta(proj).get("collab_review") is False
    # 不存在的书 → 404
    assert c.get("/api/collab-review", params={"key": "no-such-book"}).status_code == 404
    assert c.post("/api/collab-review", json={"key": "no-such-book",
                                              "enabled": True}).status_code == 404


def test_status_endpoint_reports_await_collab(monkeypatch, tmp_path):
    """/api/status 带出 pausedReason —— 前端章节卡协作条的判定来源。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    appmod.PROJECT_INDEX[KEY] = proj
    c = TestClient(appmod.app)
    r = c.get("/api/status", params={"key": KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["paused"] is True and body["pausedReason"] == "await_collab:1"


def test_write_mode_dispatch_collab_next(monkeypatch, tmp_path):
    """总编辑 write 动作 mode=collab_next 分发（不经 chief deliberate 的直连路径）。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    _sync_write_thread(monkeypatch)
    out = appmod._chat_attached(_session(), proj, "继续")
    assert _wait_idle(os.path.basename(proj.path))
    chs = {c["chapterNumber"]: c for c in _plan(proj)["chapters"]}
    assert chs[2]["status"] == "completed"


# ---------------------------------------------------------------- 6) 边界

def test_user_pause_wins_over_collab_checkpoint(monkeypatch, tmp_path):
    """作者在写作途中请求暂停：本章写完后按「人工暂停」口径收口，
    绝不被协作检查点改挂成 await_collab（否则「继续」会被当成采用）。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    orig = writer._write_chapter

    def _pause_then_complete(p, ch, mw, mr):
        orig(p, ch, mw, mr)     # 章写完（评分/门禁链路进行中，用户此刻点下暂停）
        with p.lock:
            plan = p.plan() or {}
            plan.setdefault("meta", {})["paused"] = True
            p.save_plan(plan)

    monkeypatch.setattr(writer, "_write_chapter", _pause_then_complete)
    monkeypatch.setattr(writer, "_maybe_replan", lambda p, n: None)
    monkeypatch.setattr(writer, "_ensure_card_planned", lambda p, n, tries=2: True)
    monkeypatch.setattr(writer, "_validate_and_finish", lambda p: None)
    monkeypatch.setattr(writer, "_skeleton_refresh", lambda k: None)
    monkeypatch.setattr(writer, "_ensure_layered_plan", lambda p: None)
    monkeypatch.setattr(writer.workflow, "plan_drift_check", lambda *a, **k: None)
    writer._write_loop(proj, batch_size="all")
    m = _meta(proj)
    assert m.get("paused") is True
    assert not m.get("pausedReason")               # 不是 await_collab 位
    runlog = open(os.path.join(proj.path, "运行日志.txt"), encoding="utf-8").read()
    assert "已暂停（停在当前进度" in runlog          # 走人工暂停口径
    assert "等待你把关" not in runlog                # 没有被改挂成协作待确认位


def test_collab_review_api_off_while_awaiting_resumes(monkeypatch, tmp_path):
    """设置页关闭开关时正停在待确认章：与总编辑关闭同款——视为放行并续写。"""
    proj = _mk_novel(tmp_path, total=2, collab=True)
    _stub_engine(monkeypatch)
    writer._write_loop(proj, batch_size="all")
    assert _meta(proj).get("pausedReason") == "await_collab:1"
    appmod.PROJECT_INDEX[KEY] = proj
    _sync_write_thread(monkeypatch)
    c = TestClient(appmod.app)
    r = c.post("/api/collab-review", json={"key": KEY, "enabled": False})
    assert r.status_code == 200 and r.json()["resumed"] is True
    assert _wait_idle(os.path.basename(proj.path))
    chs = {x["chapterNumber"]: x for x in _plan(proj)["chapters"]}
    assert chs[2]["status"] == "completed"
    assert _meta(proj).get("collab_review") is False
