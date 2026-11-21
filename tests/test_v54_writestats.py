"""v5.4.0 码字统计与灵感速记回归（分账口径）。

覆盖：
1. 会话口径：幂等覆盖写会话槽、commit 并入 manual、commit 重放不双记（done 墓碑）、
   删字不回扣已入账（槽位只增不减）、跨天遗留会话结算（崩溃兜底）；
2. AI 基线差：AI 插字 = ai 槽 + 抬章基线；count 口径基线差只记差额（不双记）；
3. 连续天数：今天没写从昨天回数（边界）、写了含今天、断档归零；
4. 目标：日目标落盘，月目标 = 日目标 × 当月天数；
5. HTTP 接口：GET /api/writestats、POST session/commit/baseline/goals、
   灵感速记 GET/POST/DELETE 与 #标签 提取、300 字上限。
"""
import datetime
import json

import pytest
from fastapi.testclient import TestClient

from bot import inspirations, writestats


@pytest.fixture()
def ws_file(tmp_path, monkeypatch):
    """账本与灵感库都指到临时目录，测试互不污染。"""
    f = str(tmp_path / "writestats.json")
    monkeypatch.setattr(writestats, "DATA_FILE", f)
    monkeypatch.setattr(inspirations, "DATA_FILE", str(tmp_path / "inspirations.json"))
    return f


NOW = datetime.datetime(2026, 11, 21, 10, 0, 0)   # 2026-11-21（周六）
TODAY = "2026-11-21"


def _day(date, manual=0, ai=0, key="p-b1"):
    """直接种一天账目（绕开接口，纯账本层测试用）。"""
    d = writestats.load()
    d["days"][date] = {key: {"manual": manual, "ai": ai}}
    writestats.save(d)


# ---------------------------------------------------------------- 1. 会话口径

def test_session_upsert_idempotent_overwrite(ws_file):
    """同一会话重复上报：服务端幂等覆盖（后写覆盖前写），不累加。

    「删字不回扣已入账」的只增不减由客户端在会话累计上保证
    （见 tests/v54_home_test.mjs 对 writestats-client 的契约校验）；
    服务端做纯覆盖是为了配合 AI 插入归账（客户端需要从槽位退回误进手写的 AI 字数）。"""
    r1 = writestats.put_session(TODAY, "p-b1", "s1", 100, now=NOW)
    r2 = writestats.put_session(TODAY, "p-b1", "s1", 100, now=NOW)   # 重放同一报文
    r3 = writestats.put_session(TODAY, "p-b1", "s1", 60, now=NOW)    # AI 归账修正（退回）
    assert r1["chars"] == 100
    assert r2["chars"] == 100
    assert r3["chars"] == 60
    d = writestats.load()
    assert d["sessions"][TODAY]["slots"]["s1"]["chars"] == 60


def test_session_commit_merges_into_manual(ws_file):
    """commit 把会话槽并入当日 manual；重复 commit 幂等（墓碑）。"""
    writestats.put_session(TODAY, "p-b1", "s1", 500, now=NOW)
    r1 = writestats.commit_session(TODAY, "p-b1", "s1", now=NOW)
    assert r1["moved"] == 500
    d = writestats.load()
    assert d["days"][TODAY]["p-b1"]["manual"] == 500
    assert d["sessions"][TODAY]["slots"] == {}
    assert "s1" in d["sessions"][TODAY]["done"]

    # 崩溃重放：先 upsert 再 commit（服务端应靠墓碑拒绝二次入账）
    writestats.put_session(TODAY, "p-b1", "s1", 500, now=NOW)
    r2 = writestats.commit_session(TODAY, "p-b1", "s1", now=NOW)
    assert r2["moved"] == 0
    d = writestats.load()
    assert d["days"][TODAY]["p-b1"]["manual"] == 500  # 仍然只有 500
    assert d["sessions"][TODAY]["slots"] == {}        # 迟到的 upsert 也没有复活槽位


def test_session_commit_without_slot_is_safe(ws_file):
    """没来得及 upsert 就 commit：不丢账（moved 0），并记墓碑防迟到报文双记。"""
    r = writestats.commit_session(TODAY, "p-b1", "s-ghost", now=NOW)
    assert r["moved"] == 0
    d = writestats.load()
    assert "s-ghost" in d["sessions"][TODAY]["done"]


def test_settle_stale_sessions_crash_recovery(ws_file):
    """崩溃兜底：昨天遗留的未并入会话，读取时自动结算进昨天的 manual。"""
    yesterday = "2026-11-20"
    d = writestats.load()
    d["sessions"][yesterday] = {
        "slots": {"s-left": {"key": "p-b1", "chars": 300, "updated": "2026-11-20T23:00:00"}},
        "done": [],
    }
    writestats.save(d)
    rep = writestats.report(7, now=NOW)
    assert rep["ok"] is True
    d2 = writestats.load()
    assert d2["days"][yesterday]["p-b1"]["manual"] == 300      # 并入昨天的账
    assert d2["sessions"][yesterday]["slots"] == {}            # 槽位清空
    assert "s-left" in d2["sessions"][yesterday]["done"]


# ---------------------------------------------------------------- 2. AI 基线差 / 不双记

def test_ai_insert_accounts_slot_and_raises_baseline(ws_file):
    """AI 插字：并入当日 ai 槽 + 抬升章节基线；手写会话随后只记净增，AI 不进手写。"""
    r = writestats.record_ai_insert(TODAY, "p-b1", 7, 800, now=NOW)
    assert r["chars"] == 800
    assert writestats.baseline_of("p-b1", 7) == 800
    d = writestats.load()
    assert d["days"][TODAY]["p-b1"]["ai"] == 800
    # 再插一次：基线继续抬、ai 继续加
    writestats.record_ai_insert(TODAY, "p-b1", 7, 200, now=NOW)
    assert writestats.baseline_of("p-b1", 7) == 1000
    d = writestats.load()
    assert d["days"][TODAY]["p-b1"]["ai"] == 1000


def test_baseline_count_mode_only_records_delta(ws_file, client):
    """count 口径（基线差）：基线抬到 max(旧基线, count)，ai 只记差额——重放不会双记。"""
    # 第一次：基线 0 → count 1500，差额 1500 全记 AI
    r1 = client.post("/api/writestats/baseline", json={"date": TODAY, "key": "p-b1", "chapter": 3, "count": 1500})
    assert r1.status_code == 200
    assert r1.json()["chars"] == 1500
    # 重放同一报文：基线不降，差额 0，AI 不再加
    r2 = client.post("/api/writestats/baseline", json={"date": TODAY, "key": "p-b1", "chapter": 3, "count": 1500})
    assert r2.json()["chars"] == 0
    assert writestats.baseline_of("p-b1", 3) == 1500
    # 用户删字后 count 变小：基线只升不降
    r3 = client.post("/api/writestats/baseline", json={"date": TODAY, "key": "p-b1", "chapter": 3, "count": 900})
    assert r3.json()["chars"] == 0
    assert writestats.baseline_of("p-b1", 3) == 1500
    d = writestats.load()
    assert d["days"][TODAY]["p-b1"]["ai"] == 1500


def test_manual_and_ai_no_double_count(ws_file):
    """端到端分账：会话并入 manual + AI 入账 ai，两侧合计正确、互不重复。"""
    # 作者手打：会话净增 1200 → 定稿
    writestats.put_session(TODAY, "p-b1", "s1", 1200, now=NOW)
    writestats.commit_session(TODAY, "p-b1", "s1", now=NOW)
    # AI 插入 400
    writestats.record_ai_insert(TODAY, "p-b1", 1, 400, now=NOW)
    rep = writestats.report(1, now=NOW)
    today = rep["today"]
    assert today["manual"] == 1200
    assert today["ai"] == 400
    assert rep["totals"]["all"] == 1600


# ---------------------------------------------------------------- 3. 连续天数

def test_streak_counts_back_from_yesterday_when_today_empty(ws_file):
    """边界：今天没写 → 从昨天回数，wrote_today=False。"""
    _day("2026-11-20", manual=100)
    _day("2026-11-19", manual=100)
    s = writestats.streak(today=TODAY)
    assert s == {"days": 2, "wrote_today": False}


def test_streak_includes_today_when_written(ws_file):
    _day("2026-11-20", manual=100)
    _day("2026-11-19", manual=100)
    _day(TODAY, manual=50)
    s = writestats.streak(today=TODAY)
    assert s == {"days": 3, "wrote_today": True}


def test_streak_zero_when_gap(ws_file):
    _day("2026-11-18", manual=100)   # 前天写了，昨天断了
    s = writestats.streak(today=TODAY)
    assert s == {"days": 0, "wrote_today": False}


def test_streak_counts_ai_and_inflight_session(ws_file):
    """AI 口径与进行中会话也算「写了今天」。"""
    _day("2026-11-20", manual=100)
    writestats.put_session(TODAY, "p-b1", "s1", 30, now=NOW)
    s = writestats.streak(today=TODAY)
    assert s == {"days": 2, "wrote_today": True}


# ---------------------------------------------------------------- 4. 目标与月目标

def test_goals_daily_and_monthly(ws_file):
    """日目标落盘；月目标 = 日目标 × 当月天数（11 月 = 30 天）。"""
    r = writestats.set_goals(manual=2000, ai=500)
    assert r["goals"] == {"manual": 2000, "ai": 500}
    rep = writestats.report(7, now=NOW)
    assert rep["goals"]["manual"] == 2000
    assert rep["goals"]["monthly_manual"] == 2000 * 30
    assert rep["goals"]["monthly_ai"] == 500 * 30


def test_month_view_rows_cover_whole_month(ws_file):
    """月视图 rows 覆盖整月（11 月 30 行），历史月可查。"""
    _day("2026-10-15", manual=777)
    rep = writestats.report(1, month="2026-10", now=NOW)
    m = rep["month"]
    assert m["key"] == "2026-10"
    assert m["day_count"] == 31
    assert len(m["rows"]) == 31
    assert m["manual"] == 777


# ---------------------------------------------------------------- 5. HTTP 接口
# 说明：app.py 的挂载由主会话完成（本任务不许改 app.py），这里用独立 FastAPI 实例
# 直接挂 webstats.router 测接口契约，与生产挂载（app.include_router）完全同一条路。


@pytest.fixture()
def client():
    from fastapi import FastAPI

    from bot import webstats

    app = FastAPI()
    app.include_router(webstats.router)
    return TestClient(app)


def test_api_writestats_report_shape(client, ws_file):
    r = client.get("/api/writestats?days=30")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert len(d["days"]) == 30
    assert {"manual", "ai", "session", "books"} <= set(d["days"][-1].keys())
    assert {"days", "wrote_today"} <= set(d["streak"].keys())
    assert {"manual", "ai", "monthly_manual", "monthly_ai"} <= set(d["goals"].keys())
    assert d["month"]["day_count"] == 30   # 2026-11


def test_api_session_commit_flow(client, ws_file):
    r = client.post("/api/writestats/session", json={"date": TODAY, "key": "p-x", "session": "s9", "chars": 42})
    assert r.status_code == 200 and r.json()["chars"] == 42
    r2 = client.post("/api/writestats/commit", json={"date": TODAY, "key": "p-x", "session": "s9"})
    assert r2.status_code == 200 and r2.json()["moved"] == 42
    # 幂等重放
    r3 = client.post("/api/writestats/commit", json={"date": TODAY, "key": "p-x", "session": "s9"})
    assert r3.json()["moved"] == 0
    d = writestats.load()
    assert d["days"][TODAY]["p-x"]["manual"] == 42


def test_api_session_requires_key_and_session(client, ws_file):
    assert client.post("/api/writestats/session", json={"chars": 1}).status_code == 400
    assert client.post("/api/writestats/session", json={"key": "p-x", "chars": 1}).status_code == 400
    assert client.post("/api/writestats/commit", json={"key": "p-x"}).status_code == 400
    assert client.post("/api/writestats/baseline", json={"chars": 1}).status_code == 400


def test_api_goals_validation(client, ws_file):
    assert client.post("/api/writestats/goals", json={}).status_code == 400
    r = client.post("/api/writestats/goals", json={"manual": 1500, "ai": 300})
    assert r.status_code == 200
    assert r.json()["goals"] == {"manual": 1500, "ai": 300}


# ---------------------------------------------------------------- 6. 灵感速记

def test_inspiration_tags_and_limit():
    assert inspirations.extract_tags("记一条 #灵感 关于开头的写法 #人物-林晚") == ["灵感", "人物-林晚"]
    assert inspirations.extract_tags("没有标签的普通句子") == []
    assert inspirations.extract_tags("#重复 #重复 #重复") == ["重复"]
    long_tag = "x" * 21
    assert inspirations.extract_tags(f"#{long_tag}") == []       # 超过 20 字不收
    many = " ".join(f"#标{i}" for i in range(10))
    assert len(inspirations.extract_tags(many)) == 8             # 最多 8 个


def test_inspiration_add_list_delete(ws_file):
    item = inspirations.add("主角第一次说谎 #人物-林晚 #转折")
    assert item["tags"] == ["人物-林晚", "转折"]
    assert len(item["text"]) <= inspirations.MAX_LEN
    items = inspirations.list_items()
    assert [x["id"] for x in items] == [item["id"]]
    assert inspirations.delete(item["id"]) is True
    assert inspirations.list_items() == []
    assert inspirations.delete(item["id"]) is False              # 再删一次：不存在


def test_inspiration_length_guard(ws_file):
    with pytest.raises(ValueError):
        inspirations.add("")
    with pytest.raises(ValueError):
        inspirations.add("好" * (inspirations.MAX_LEN + 1))
    item = inspirations.add("恰好在三百字以内 " + "写" * (inspirations.MAX_LEN - 10))
    assert item["id"]


def test_api_inspirations_crud(client, ws_file):
    r = client.post("/api/inspirations", json={"text": "钩子要挂在章尾 #技巧"})
    assert r.status_code == 200
    item = r.json()["item"]
    assert item["tags"] == ["技巧"]
    lst = client.get("/api/inspirations?limit=10").json()
    assert any(x["id"] == item["id"] for x in lst["items"])
    assert lst["max_len"] == 300
    # 超长 400
    assert client.post("/api/inspirations", json={"text": "字" * 301}).status_code == 400
    # 删除
    assert client.delete(f"/api/inspirations/{item['id']}").status_code == 200
    assert client.delete(f"/api/inspirations/{item['id']}").status_code == 404


def test_ledger_file_shape(ws_file):
    """账本落盘结构符合设计约定（days/chapterBase/goals 顶层字段）。"""
    writestats.put_session(TODAY, "p-b1", "s1", 10, now=NOW)
    writestats.record_ai_insert(TODAY, "p-b1", 2, 100, now=NOW)
    raw = json.load(open(ws_file, encoding="utf-8"))
    assert set(raw.keys()) >= {"days", "sessions", "chapterBase", "goals"}
    assert raw["days"][TODAY]["p-b1"] == {"manual": 0, "ai": 100}
    assert raw["chapterBase"]["p-b1"]["2"] == 100
