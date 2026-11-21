"""v5.5.0 开书流程「选完书名任务断裂」全链路回归（核心流程断层修复）。

实测断层（桌面端 2026-11-21）：向导以 idea 名建书（awaiting_title）→ 规划在途时
用户选书名 / 总编辑拍板定名（chief set_title，含极速模式 writer.pick_title）→
目录 os.rename 换 key + record_rename 记别名 → 在途线程写盘复活旧目录 →
会话与前端都停在旧 key，总编辑读到复活空目录，回答《未命名》空书；
点「继续」无从继续，会话死寂。

钉住四条契约：
1. 换 key 必须换会话：_apply_title / /api/pick-title 后 SESSIONS 全部重绑新 key；
   _chat_attached 把 canonical key 规范化；同轮 [set_title, write] 决策里的 write
   步骤必须落到新书（不再用旧 key 查状态/续写）。
2. 别名读：旧 key 的一切请求（plan / 聊天历史 / 状态）落新家；/api/status 带
   key=canonical + followed_from（前端跳转依据，接线断点修复）。
3. 交接语：选名后 chat_log 落「书名 / 进度 / 下一步」handoff，总编辑读历史接得住「继续」。
4. awaiting_title 自愈：规划 failed / 线程死亡后说「继续」→ 重跑规划而不是空书回答。

另回归：express 自动定名路径（pick_title 交接语 + 会话重绑）、幻影目录历史合并、
旧 key 删除不误删真书。全部用例跑在 tmp_path，rename_map 也指向 tmp，不碰真实数据。
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
TS = "20261121-172954"
OLD_KEY = f"{TS}-海归投行精英沈知遥隐瞒身份"
NEW_KEY = f"{TS}-实习生掉马那天"


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


def _mk_idea_book(projects, key=OLD_KEY, meta=None, status="planning", chapters=None):
    """等价 /api/create 落的骨架：idea 名目录 + awaiting_title 骨架计划。"""
    pdir = projects / key
    pdir.mkdir(parents=True, exist_ok=True)
    chs = chapters if chapters is not None else []
    plan = {"version": 1, "novelName": key.split("-", 2)[-1], "projectPath": str(pdir),
            "status": status, "totalChapters": 45, "totalWords": 0,
            "minWordsPerChapter": 3000,
            "config": {"genre": "都市", "chapters": 45, "batch_size": "2"},
            "titles": [{"title": "实习生掉马那天", "technique": "候选", "why": ""},
                       {"title": "马甲藏不住了", "technique": "候选", "why": ""}],
            "meta": {"awaiting_title": True, "title_candidates": [
                {"title": "实习生掉马那天", "technique": "候选", "why": ""},
                {"title": "马甲藏不住了", "technique": "候选", "why": ""},
            ], "planning_running": False},
            "createdAt": "2026-11-21T17:29:54",
            "chapters": chs}
    if meta:
        plan["meta"].update(meta)
    json.dump(plan, open(pdir / PLAN_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    # 预置一段改名前就写进 chat_log 的历史（随 os.rename 一起搬到新家）
    with open(pdir / "chat_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "开新书：海归投行精英沈知遥隐瞒身份。",
                            "ts": 1795253394.0}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "给你几个书名候选：1《实习生掉马那天》…",
                            "ts": 1795253409.0}, ensure_ascii=False) + "\n")
    return pdir


def _plan_of(projects, key):
    return json.load(open(projects / key / PLAN_FILE, encoding="utf-8"))


def _chat_rows(projects, key):
    fp = projects / key / "chat_log.jsonl"
    if not fp.exists():
        return []
    return [json.loads(x) for x in fp.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------------------------------------------------------------- 1. 换 key 必须换会话

def test_pick_title_rebinds_sessions_records_alias_and_handoff(sandbox):
    """writer.pick_title（极速自动定名 / /api/pick-title 底层）：
    目录改名 + 别名入档 + 计划落新家 + 交接语落 chat_log。"""
    _mk_idea_book(sandbox)
    monkey_calls = []
    orig_start = writer.start_writing
    writer.start_writing = lambda proj, batch_size=None: monkey_calls.append(
        os.path.basename(proj.path)) or False
    try:
        newkey = writer.pick_title(OLD_KEY, "实习生掉马那天")
    finally:
        writer.start_writing = orig_start
    assert newkey == NEW_KEY
    assert (sandbox / OLD_KEY).exists() is False          # os.rename 不留旧目录
    assert (sandbox / NEW_KEY / PLAN_FILE).exists()
    m = writer._load_rename_map()
    assert m.get(OLD_KEY) == NEW_KEY                       # 旧 key 别名入档
    plan = _plan_of(sandbox, NEW_KEY)
    assert plan["novelName"] == "实习生掉马那天"
    rows = _chat_rows(sandbox, NEW_KEY)
    handoff = [r for r in rows if r.get("handoff")]
    assert handoff and "实习生掉马那天" in handoff[-1]["content"]
    assert "45" in handoff[-1]["content"]                 # 交接语带真实进度（0/45 章）


def test_api_pick_title_rebinds_all_sessions(sandbox, client):
    """/api/pick-title：返回新 key，且所有绑旧 key 的会话立即重绑。"""
    _mk_idea_book(sandbox)
    appmod.SESSIONS["s1"] = {"id": "s1", "state": "attached", "project": OLD_KEY}
    appmod.SESSIONS["s2"] = {"id": "s2", "state": "attached", "project": OLD_KEY}
    orig_start = writer.start_writing
    writer.start_writing = lambda proj, batch_size=None: False
    try:
        res = client.post("/api/pick-title", json={"key": OLD_KEY, "title": "实习生掉马那天"})
    finally:
        writer.start_writing = orig_start
    assert res.status_code == 200
    assert res.json()["key"] == NEW_KEY
    assert appmod.SESSIONS["s1"]["project"] == NEW_KEY
    assert appmod.SESSIONS["s2"]["project"] == NEW_KEY


def test_apply_title_rebinds_sessions_and_chat_flow(sandbox):
    """会话路径：awaiting_title 书上「改名为X」→ _apply_title → 会话重绑 + 别名 + 交接。"""
    _mk_idea_book(sandbox)
    s = {"id": "s0", "state": "attached", "project": OLD_KEY}
    appmod.SESSIONS["s0"] = s
    orig_start = writer.start_writing
    writer.start_writing = lambda proj, batch_size=None: False
    try:
        res = appmod._apply_title(OLD_KEY, "实习生掉马那天")
    finally:
        writer.start_writing = orig_start
    assert res.get("ok") and res["key"] == NEW_KEY
    assert s["project"] == NEW_KEY                         # 会话跟着换 key
    assert appmod.SESSIONS["s0"]["project"] == NEW_KEY
    assert writer._load_rename_map().get(OLD_KEY) == NEW_KEY
    handoff = [r for r in _chat_rows(sandbox, NEW_KEY) if r.get("handoff")]
    assert handoff and "实习生掉马那天" in handoff[-1]["content"]


def test_same_turn_set_title_then_write_uses_new_key(sandbox, monkeypatch):
    """核心断层：同一次总编辑决策 actions=[set_title, write] —— write 步骤必须落新书。

    旧实现 _run 闭包握着进函数时的旧 key，改名后 write/状态查询仍在旧 key 上跑，
    总编辑把新书看成《未命名》空书。（实测场景：规划已完成、45 章 pending，
    作者没挑书名直接说「继续」，总编辑拍板定名并接着开写。）"""
    chapters = [{"chapterNumber": n, "title": f"第{n}章", "status": "pending", "wordCount": 0}
                for n in range(1, 46)]
    _mk_idea_book(sandbox, status="in_progress", chapters=chapters,
                  meta={"awaiting_title": True, "planning_running": False})
    s = {"id": "s0", "state": "attached", "project": OLD_KEY}
    appmod.SESSIONS["s0"] = s
    proj = appmod._get_project(OLD_KEY)
    assert os.path.basename(proj.path) == OLD_KEY

    used_keys = []

    def fake_resume_book(key, batch_size=None):
        used_keys.append(key)
        return True

    monkeypatch.setattr(writer, "resume_book", fake_resume_book)
    monkeypatch.setattr(writer, "start_writing", lambda proj, batch_size=None: False)
    monkeypatch.setattr(steps_mod, "emit", lambda *a, **k: None)

    decision = {"understanding": "作者没挑书名，直接说继续——拍板定名并推进",
                "reply": "书名就定为《实习生掉马那天》，马上安排创作。",
                "actions": [{"action": "set_title", "params": {"title": "实习生掉马那天"}},
                            {"action": "write", "params": {}}],
                "confidence": 0.9}
    monkeypatch.setattr(chief_mod, "deliberate", lambda msg, ctx: decision)
    # 总编辑上下文构建照常跑（校验它读到的是旧 key 入口的真实状态，不连网）
    reply = appmod._chat_attached(s, proj, "继续")
    assert used_keys == [NEW_KEY], f"write 步骤必须用新 key，实际 {used_keys}"
    assert s["project"] == NEW_KEY
    assert "实习生掉马那天" in reply


def test_chief_context_injects_real_plan_after_follow(sandbox):
    """换 key 后总编辑上下文必须看到真书：45 章 pending 的章节账本 + 新书名，
    绝不允许再出现《未命名》空书口径。"""
    chapters = [{"chapterNumber": n, "title": f"第{n}章", "status": "pending", "wordCount": 0}
                for n in range(1, 46)]
    pdir = _mk_idea_book(sandbox, chapters=chapters)
    # 直接走一次改名（等价选名完成）
    orig_start = writer.start_writing
    writer.start_writing = lambda proj, batch_size=None: False
    try:
        res = appmod._apply_title(OLD_KEY, "实习生掉马那天")
    finally:
        writer.start_writing = orig_start
    assert res.get("ok")
    proj = appmod._get_project(NEW_KEY)
    st = proj.status()
    ctx = chief_mod.build_context(proj, st, os.path.basename(proj.path))
    assert ctx.get("name") == "实习生掉马那天"
    assert len(ctx.get("chapter_rows") or []) == 45
    text = chief_mod._context_text(ctx)
    assert "实习生掉马那天" in text and "《未命名》" not in text
    assert "pending" in text                             # 章节账本按真实 plan 状态注入


# ---------------------------------------------------------------- 2. 别名读 + /api/status 跳转字段

def test_old_key_requests_follow_alias(sandbox):
    """旧 key 的 plan/项目定位/聊天历史一律落新家（别名读，不做双写）。"""
    _mk_idea_book(sandbox)
    orig_start = writer.start_writing
    writer.start_writing = lambda proj, batch_size=None: False
    try:
        assert appmod._apply_title(OLD_KEY, "实习生掉马那天").get("ok")
    finally:
        writer.start_writing = orig_start
    # writer 层
    assert writer._project_dir(OLD_KEY) == str(sandbox / NEW_KEY)
    # app 层
    proj = appmod._get_project(OLD_KEY)
    assert proj is not None and os.path.basename(proj.path) == NEW_KEY
    # 旧 key 读历史：读到的是随 rename 迁走的聊天 + 交接语
    res = client_probe_history(sandbox, OLD_KEY)
    contents = [str(r.get("content") or "") for r in res]
    assert any("开新书" in c for c in contents)
    assert any("书名已确定为《实习生掉马那天》" in c for c in contents)


def client_probe_history(sandbox, key):
    """绕开 TestClient 直接走 v2api 的历史读取口径（等价 /api/chat/history）。"""
    from bot.v2api import _load_chat_log
    proj = appmod._get_project(key)
    assert proj is not None
    return _load_chat_log(proj, limit=5000)


def test_api_status_returns_canonical_key_and_followed_from(sandbox, client):
    """/api/status?旧key → 200 + key=canonical + followed_from=请求 key
    （前端跳转依据；此前注释写了「前端旧请求自动跳转」但链路从未接通）。"""
    _mk_idea_book(sandbox)
    orig_start = writer.start_writing
    writer.start_writing = lambda proj, batch_size=None: False
    try:
        assert appmod._apply_title(OLD_KEY, "实习生掉马那天").get("ok")
    finally:
        writer.start_writing = orig_start
    res = client.get("/api/status", params={"key": OLD_KEY})
    assert res.status_code == 200
    body = res.json()
    assert body["key"] == NEW_KEY
    assert body.get("followed_from") == OLD_KEY
    # canonical key 直查没有跳转字段
    res2 = client.get("/api/status", params={"key": NEW_KEY})
    assert res2.status_code == 200 and res2.json()["key"] == NEW_KEY
    assert "followed_from" not in res2.json()


# ---------------------------------------------------------------- 3. 交接语

def test_handoff_message_carries_title_progress_next(sandbox):
    """交接语三要素：书名 / 进度（含已写章）/ 下一步动作。"""
    chapters = [{"chapterNumber": 1, "title": "第1章", "status": "completed", "wordCount": 3000},
                {"chapterNumber": 2, "title": "第2章", "status": "pending", "wordCount": 0}]
    _mk_idea_book(sandbox, chapters=chapters)
    orig_start = writer.start_writing
    writer.start_writing = lambda proj, batch_size=None: False
    try:
        assert appmod._apply_title(OLD_KEY, "实习生掉马那天").get("ok")
    finally:
        writer.start_writing = orig_start
    rows = _chat_rows(sandbox, NEW_KEY)
    handoff = [r for r in rows if r.get("handoff")]
    assert handoff
    text = handoff[-1]["content"]
    assert "实习生掉马那天" in text
    assert "1/2" in text            # 真实进度按磁盘口径
    assert "第 2 章" in text        # 下一步 = 接着写第 2 章


def test_awaiting_title_pick_reply_includes_progress(sandbox):
    """待选书上回复「1」采纳候选：回复里带进度尾巴（下一步可继续）。"""
    chapters = [{"chapterNumber": 1, "title": "第1章", "status": "pending", "wordCount": 0}]
    _mk_idea_book(sandbox, status="awaiting_title", chapters=chapters)
    s = {"id": "s0", "state": "attached", "project": OLD_KEY}
    appmod.SESSIONS["s0"] = s
    proj = appmod._get_project(OLD_KEY)
    monkeypatch_fail = writer.start_writing
    writer.start_writing = lambda p, batch_size=None: False
    try:
        reply = appmod._chat_attached(s, proj, "1")
    finally:
        writer.start_writing = monkeypatch_fail
    assert "实习生掉马那天" in reply
    assert "0/1" in reply


# ---------------------------------------------------------------- 4. awaiting_title 自愈

def test_planning_interrupted_detection(sandbox):
    """_planning_interrupted 三种形态：failed / 无线程无标志 / 标志残留且文件久未动。"""
    pdir = _mk_idea_book(sandbox, meta={"planning_running": False},
                         status="planning")
    # ① 账面 failed + error
    plan = _plan_of(sandbox, OLD_KEY)
    plan["status"] = "failed"
    plan["meta"]["error"] = "provider 524 timeout"
    json.dump(plan, open(pdir / PLAN_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    proj = appmod._get_project(OLD_KEY)
    assert appmod._planning_interrupted(proj) is True
    # ② planning/awaiting_title 且无线程无标志
    plan["status"] = "awaiting_title"
    plan["meta"].pop("error", None)
    json.dump(plan, open(pdir / PLAN_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    assert appmod._planning_interrupted(proj) is True
    # ③ 已有章卡 → 不是中断（走正常写作路由）
    plan["chapters"] = [{"chapterNumber": 1, "title": "第1章", "status": "pending"}]
    json.dump(plan, open(pdir / PLAN_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    assert appmod._planning_interrupted(proj) is False


def test_failed_planning_continue_triggers_retry_not_empty_book(sandbox, monkeypatch):
    """规划失败后说「继续」→ 重跑规划；绝不允许《未命名》空书回答。"""
    pdir = _mk_idea_book(sandbox, meta={"planning_running": False, "error": "524"},
                         status="failed")
    s = {"id": "s0", "state": "attached", "project": OLD_KEY}
    appmod.SESSIONS["s0"] = s
    proj = appmod._get_project(OLD_KEY)
    called = []

    def fake_retry(key):
        called.append(key)
        return True

    monkeypatch.setattr(appmod, "_retry_planning_bg", fake_retry)
    monkeypatch.setattr(steps_mod, "emit", lambda *a, **k: None)
    reply = appmod.chat("sid-retry", "继续", OLD_KEY)
    assert called == [OLD_KEY]
    assert "重新启动" in reply and "规划" in reply


def test_retry_planning_bg_reuses_same_dir_and_finishes(sandbox, monkeypatch):
    """_retry_planning_bg 本体：不换目录、不另起书；已选名时规划完自动开写。"""
    pdir = _mk_idea_book(sandbox, meta={"planning_running": False,
                                        "title_picked": True, "selected_title": "实习生掉马那天"},
                         status="failed")
    plan0 = _plan_of(sandbox, OLD_KEY)
    plan0["meta"]["planning_running"] = False
    json.dump(plan0, open(pdir / PLAN_FILE, "w", encoding="utf-8"), ensure_ascii=False)

    monkeypatch.setattr(appmod.workflow, "fill_defaults",
                        lambda cfg: {**cfg, "chapters": 45})
    monkeypatch.setattr(appmod.workflow, "gen_titles",
                        lambda cfg, n=8: [{"title": "实习生掉马那天", "technique": "", "why": ""}])

    created = {}

    def fake_create(cfg, titles, pdir=None, name=None):
        created["pdir"] = pdir
        created["name"] = name
        plan = {"novelName": name or "实习生掉马那天", "status": "planning",
                "totalChapters": 45, "config": cfg, "titles": titles,
                "meta": {"title_picked": True, "selected_title": "实习生掉马那天"},
                "chapters": [{"chapterNumber": n, "title": f"第{n}章", "status": "pending",
                              "wordCount": 0} for n in range(1, 46)]}
        fp = os.path.join(pdir, PLAN_FILE)
        json.dump(plan, open(fp, "w", encoding="utf-8"), ensure_ascii=False)
        return appmod._get_project(OLD_KEY)

    monkeypatch.setattr(writer, "create_project", fake_create)
    started = []
    monkeypatch.setattr(writer, "start_writing",
                        lambda proj, batch_size=None: started.append(os.path.basename(proj.path)) or True)

    assert appmod._retry_planning_bg(OLD_KEY) is True
    # 收口：账面 in_progress、写线程落在新书 key 上（本例书名未改，key 不变）
    for _ in range(80):
        st = appmod._get_project(OLD_KEY).status()
        if st.get("state") == "in_progress" or started:
            break
        time.sleep(0.05)
    assert created["pdir"] == str(pdir)          # 同一目录，不另起书
    assert started == [OLD_KEY]                  # 自动开写落在本书 key 上


# ---------------------------------------------------------------- 5. 幻影历史合并 + 删除安全

def test_sweep_merges_ghost_chat_history_before_cleanup(sandbox, monkeypatch):
    """改名后旧目录被在途写盘复活（幻影）：sweep 先把聊天/事件历史并进真书目录，
    再清幻影——用户的对话记录不能跟着 rmtree 一起丢。"""
    _mk_idea_book(sandbox)
    orig_start = writer.start_writing
    writer.start_writing = lambda proj, batch_size=None: False
    try:
        assert appmod._apply_title(OLD_KEY, "实习生掉马那天").get("ok")
    finally:
        writer.start_writing = orig_start
    # 幻影复活：改名瞬间之后写进旧路径的对话（实测劈成两半的那部分）
    ghost = sandbox / OLD_KEY
    ghost.mkdir(parents=True)
    with open(ghost / "chat_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "继续", "ts": 1795256267.0},
                           ensure_ascii=False) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "这本书目前还是空的……",
                            "ts": 1795256283.0}, ensure_ascii=False) + "\n")
    with open(ghost / "创作事件.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"no": 1, "agent": "planner", "title": "生成人物档案", "ts": 1.0},
                           ensure_ascii=False) + "\n")
    removed = writer.sweep_rename_zombies()
    assert OLD_KEY in removed
    assert ghost.exists() is False
    rows = _chat_rows(sandbox, NEW_KEY)
    texts = [str(r.get("content") or "") for r in rows]
    assert any(c == "继续" for c in texts)
    assert any("这本书目前还是空的" in c for c in texts)
    assert (sandbox / NEW_KEY / "创作事件.jsonl").exists()


def test_sweep_merge_is_idempotent(sandbox):
    """重复 sweep 不产生重复行（按整行 JSON 去重）。"""
    new_dir = sandbox / NEW_KEY
    new_dir.mkdir(parents=True)
    old_dir = sandbox / OLD_KEY
    old_dir.mkdir(parents=True)
    row = {"role": "user", "content": "继续", "ts": 1.0}
    with open(old_dir / "chat_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    writer._merge_ghost_history(str(old_dir), str(new_dir))
    writer._merge_ghost_history(str(old_dir), str(new_dir))
    rows = _chat_rows(sandbox, NEW_KEY)
    assert len(rows) == 1


def test_delete_old_alias_key_never_kills_real_book(sandbox):
    """旧 key 的硬删除只删旧目录本身，绝不顺着别名把真书删掉。"""
    _mk_idea_book(sandbox)
    orig_start = writer.start_writing
    writer.start_writing = lambda proj, batch_size=None: False
    try:
        assert appmod._apply_title(OLD_KEY, "实习生掉马那天").get("ok")
    finally:
        writer.start_writing = orig_start
    # 复活一个旧目录（等价在途残留），随后按旧 key 硬删
    ghost = sandbox / OLD_KEY
    ghost.mkdir(parents=True)
    (ghost / "残.txt").write_text("x", encoding="utf-8")
    res = writer.delete_book(OLD_KEY, hard=True)
    assert res.get("ok")
    assert ghost.exists() is False
    assert (sandbox / NEW_KEY).exists()          # 真书安然无恙


# ---------------------------------------------------------------- 6. express 模式回归

def test_express_pick_title_chain_regression(sandbox, client, monkeypatch):
    """极速模式链路回归：骨架书 → pick_title 自动定名 → 别名/交接/旧 key 状态跳转/
    旧 key 会话下一条消息自动规范到 canonical key，一条龙都不破。"""
    _mk_idea_book(sandbox, meta={"planning_running": False})
    orig_start = writer.start_writing
    writer.start_writing = lambda proj, batch_size=None: False
    try:
        # express _bg 的原句：newkey = writer.pick_title(base, best)
        newkey = writer.pick_title(OLD_KEY, "实习生掉马那天")
        proj = writer.Project(writer._project_dir(newkey), newkey)
    finally:
        writer.start_writing = orig_start
    assert newkey == NEW_KEY
    assert proj.plan()["meta"]["awaiting_title"] is False
    handoff = [r for r in _chat_rows(sandbox, NEW_KEY) if r.get("handoff")]
    assert handoff                                       # 极速路径也要有交接语
    # 旧 key 的 status 请求带回跳转字段（前端据此切路由）
    res = client.get("/api/status", params={"key": OLD_KEY})
    assert res.status_code == 200
    body = res.json()
    assert body["key"] == NEW_KEY
    assert body.get("followed_from") == OLD_KEY
    # 旧 key 会话的下一条消息：chat() 绑定即规范化，总编辑回答带新书名
    monkeypatch.setattr(chief_mod, "deliberate", lambda msg, ctx: None)
    import bot.nlu as nlu_mod
    monkeypatch.setattr(nlu_mod, "parse_intent",
                        lambda msg, project_status=None: {
                            "intent": "query_status", "params": {},
                            "confidence": 1.0, "source": "test"})
    monkeypatch.setattr(steps_mod, "emit", lambda *a, **k: None)
    reply = appmod.chat("sid-express", "进度", OLD_KEY)
    assert appmod.SESSIONS["sid-express"]["project"] == NEW_KEY
    assert "实习生掉马那天" in reply


def test_rename_map_cache_invalidation(sandbox):
    """rename_map 缓存按 mtime 失效：record_rename 后立刻能解析新别名。"""
    assert writer.resolve_key(OLD_KEY) == OLD_KEY
    writer.record_rename(OLD_KEY, NEW_KEY)
    assert writer.resolve_key(OLD_KEY) == NEW_KEY
    writer.record_rename(NEW_KEY, f"{TS}-第三版")
    assert writer.resolve_key(NEW_KEY) == f"{TS}-第三版"
