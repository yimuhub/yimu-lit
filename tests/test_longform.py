"""长篇内核测试：迁移 / 1500 章规模 / 状态增量 / 伏笔到期 / 重规划 / 上下文预算。

所有测试用 tmp_path + 轻量 FakeProject，不访问网络；LLM 由 tests/conftest.py
统一 stub，本文件只在需要"合法 JSON"时局部覆盖。
"""
from __future__ import annotations

import json
import os
import threading
import time

from bot import context, llm, longform, state
from bot.safeio import atomic_write_json


class FakeProject:
    """只提供 writer.Project 里长篇内核实际会用的接口。"""

    def __init__(self, path, name="测试书"):
        self.path = str(path)
        os.makedirs(self.path, exist_ok=True)
        self.name = name
        self.lock = threading.RLock()
        self.logs: list[str] = []

    def log(self, msg):
        self.logs.append(str(msg))

    def plan(self):
        p = os.path.join(self.path, "02-写作计划.json")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def save_plan(self, plan):
        atomic_write_json(os.path.join(self.path, "02-写作计划.json"), plan)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def make_project(tmp_path, total=10, done=5, name="测试书", with_files=True):
    """构造一个旧式扁平项目（含 00/01/03 与已完成章节文件）。"""
    p = FakeProject(tmp_path / "proj", name)
    plan = {
        "version": 1, "novelName": name, "totalChapters": total,
        "minWordsPerChapter": 3000, "status": "in_progress",
        "writingMode": "serial",
        "config": {"genre": "悬疑", "chapters": total, "tone": "冷峻"},
        "titles": [], "chapters": [],
    }
    for i in range(1, total + 1):
        finished = i <= done
        plan["chapters"].append({
            "chapterNumber": i, "title": f"原题{i}",
            "filePath": f"第{i:02d}章-原题{i}.md",
            "status": "completed" if finished else "pending",
            "wordCount": 3200 if finished else None,
            "wordCountPass": True if finished else None, "retryCount": 0,
        })
        if finished and with_files:
            _write(os.path.join(p.path, f"第{i:02d}章-原题{i}.md"),
                   f"# 第{i:02d}章 原题{i}\n\n## 本章概要\n第{i}章发生了关键转折。\n\n"
                   f"## 正文\n雨下得很大。他走进巷子，听见身后的脚步。\n")
    p.save_plan(plan)
    _write(os.path.join(p.path, "00-人物档案.md"),
           "# 人物档案\n\n## 人物：林川\n主角，前刑警。\n\n## 人物：老陈\n导师。\n")
    _write(os.path.join(p.path, "01-大纲.md"),
           "# 大纲\n\n| 第1章 | 开端 |\n| 第2章 | 入局 |\n\n### 第6章 危机\n主角陷入危机。\n")
    _write(os.path.join(p.path, "03-文风基准.md"),
           "## 文风指纹\n冷峻、克制。\n\n## 基准段落\n雨落在铁皮棚上，像有人在外面数数。\n")
    return p


def _card_bytes(p):
    with open(os.path.join(p.path, "02d-章卡.jsonl"), encoding="utf-8") as f:
        return f.read()


def _plan_bytes(p):
    with open(os.path.join(p.path, "02c-总规划.json"), encoding="utf-8") as f:
        return f.read()


def _log_lines(path):
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return len(f.read().splitlines())


# ---------- 1. 旧 10 章扁平计划迁移 ----------

def test_migrate_flat_plan_10_chapters_idempotent(tmp_path):
    p = make_project(tmp_path, total=10, done=5)
    plan = longform.ensure_plan(p)

    assert plan["version"] == 2
    assert plan["volumes"] and plan["volumes"][0]["arcs"]
    assert plan["scale"]["chapters"] == 10

    cards = longform._load_cards(p)
    assert len(cards) == 10
    for n in range(1, 6):
        assert cards[n]["status"] == "done"
        assert cards[n]["title"] == f"原题{n}"
    for n in range(6, 11):
        assert cards[n]["status"] == "planned"

    # 旧计划里已完成章节的状态/字数一个字都不能变
    flat = p.plan()
    for ch in flat["chapters"][:5]:
        assert ch["status"] == "completed"
        assert ch["wordCount"] == 3200
        assert ch["filePath"] == f"第{ch['chapterNumber']:02d}章-原题{ch['chapterNumber']}.md"

    # 重复迁移幂等：内容与章卡字节都不变
    first_plan = _plan_bytes(p)
    first_cards = _card_bytes(p)
    again = longform.ensure_plan(p)
    assert again == plan
    assert _plan_bytes(p) == first_plan
    assert _card_bytes(p) == first_cards


# ---------- 2. 1500 章规模 ----------

def test_plan_book_1500_chapters_fast_and_small(tmp_path):
    p = FakeProject(tmp_path / "big")
    t0 = time.time()
    digest = longform.plan_book(p, {"scale": {"target_words": 5_000_000}})
    elapsed = time.time() - t0

    assert elapsed < 5.0, f"1500 章规划耗时 {elapsed:.2f}s"
    assert os.path.getsize(os.path.join(p.path, "02c-总规划.json")) < 1_000_000
    with open(os.path.join(p.path, "02d-章卡.jsonl"), encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert len(lines) == 1500
    assert json.loads(lines[0])["n"] == 1
    assert json.loads(lines[-1])["n"] == 1500

    assert digest["scale"]["chapters"] == 1500
    assert digest["target_words"] == 5_000_000
    assert digest["chapters_planned"] == 1500
    assert digest["chapters_written"] == 0
    assert digest["volumes"] >= 20
    assert digest["arcs"] >= digest["volumes"]

    # 旧写作循环也能看到 1500 章
    flat = p.plan()
    assert flat["totalChapters"] == 1500
    assert len(flat["chapters"]) == 1500


# ---------- 3. 状态增量 / 幂等 / 校验 ----------

def test_apply_delta_merge_idempotent_and_validate(tmp_path):
    p = FakeProject(tmp_path / "state")

    delta = {
        "characters": {"林川": {"status": "逃亡", "location": "码头",
                              "goal": "洗清冤屈", "knowledge": ["旧案卷存在"],
                              "relations": {"老陈": "师徒"}}},
        "locations": {"码头": {"description": "废弃码头", "status": "雨夜"}},
        "foreshadowing": {"L1": {"description": "第十三份案卷", "due_from": 3, "due_to": 8}},
        "timeline": [{"chapter": 1, "time": "夜", "event": "林川逃到码头", "characters": ["林川"]}],
    }
    st = state.apply_delta(p.path, 1, delta, source="writer")
    assert st["characters"]["林川"]["status"] == "逃亡"
    assert st["characters"]["林川"]["first_seen"] == 1
    assert st["locations"]["码头"]["description"] == "废弃码头"
    assert st["foreshadowing"]["L1"]["status"] == "open"
    assert st["timeline"][0]["event"] == "林川逃到码头"
    assert st["metrics"]["last_chapter"] == 1
    assert json.load(open(os.path.join(p.path, "18-状态库.json"), encoding="utf-8"))

    # 同章同 source：幂等，不重复追加 timeline，也不改已有字段
    st2 = state.apply_delta(p.path, 1,
                           {"characters": {"林川": {"status": "重生"}},
                            "timeline": [{"chapter": 1, "event": "林川逃到码头"}]},
                           source="writer")
    assert st2["characters"]["林川"]["status"] == "逃亡"
    assert len(st2["timeline"]) == 1

    # 非法 delta 不破坏状态
    before = json.dumps(state.load(p.path), ensure_ascii=False, sort_keys=True)
    assert state.apply_delta(p.path, 2, "不是对象", source="bad")["characters"]
    after = json.dumps(state.load(p.path), ensure_ascii=False, sort_keys=True)
    assert before == after

    # validate：悬空关系 + 伏笔 due 反转
    bad = state.empty_state()
    state.upsert_character(bad, "甲", relations={"乙": "朋友"})
    state.upsert_foreshadow(bad, "F", description="坏伏笔", due_from=10, due_to=5)
    issues = state.validate(bad)
    assert any("不存在" in x and "乙" in x for x in issues)
    assert any("反转" in x for x in issues)


# ---------- 4. 伏笔到期分类 ----------

def test_foreshadow_due_buckets(tmp_path):
    st = state.empty_state()
    state.upsert_foreshadow(st, "due", description="当期", due_from=45, due_to=55)
    state.upsert_foreshadow(st, "overdue", description="逾期", due_from=30, due_to=40)
    state.upsert_foreshadow(st, "upcoming", description="临近", due_from=52, due_to=60)
    state.upsert_foreshadow(st, "paid", description="已兑现", due_from=30, due_to=40, status="paid")
    state.upsert_foreshadow(st, "dropped", description="已放弃", due_from=30, due_to=40, status="dropped")
    state.upsert_foreshadow(st, "far", description="太远", due_from=100, due_to=110)

    out = state.foreshadow_due(st, 50, lookahead=15)
    assert [x["id"] for x in out["due"]] == ["due"]
    assert [x["id"] for x in out["overdue"]] == ["overdue"]
    assert [x["id"] for x in out["upcoming"]] == ["upcoming"]
    assert all(x["id"] not in ("paid", "dropped", "far")
               for x in out["due"] + out["overdue"] + out["upcoming"])


# ---------- 5. 重规划提案 / 应用边界 ----------

def test_replan_proposal_and_apply_only_future(tmp_path):
    p = make_project(tmp_path, total=20, done=10)
    longform.ensure_plan(p)

    plan_before = _plan_bytes(p)
    cards_before = _card_bytes(p)

    # book 级：覆盖已写章节 → 默认拒绝；replan 本身不改 02c/02d
    book = longform.replan(p, scope="book", reason="test-book")
    assert book["requires_past_rewrite"] is True
    assert _plan_bytes(p) == plan_before
    assert _card_bytes(p) == cards_before
    denied = longform.apply_replan(p, book["id"])
    assert denied["ok"] is False
    assert "allow_past" in denied["error"]
    assert _plan_bytes(p) == plan_before
    assert _card_bytes(p) == cards_before

    # window 级：只影响未来章卡
    proposal = longform.replan(p, scope="window", instruction="加快冲突", reason="test-window")
    assert proposal["requires_past_rewrite"] is False
    assert proposal["affected_chapters"]
    assert min(proposal["affected_chapters"]) > 10
    assert _plan_bytes(p) == plan_before
    assert _card_bytes(p) == cards_before
    assert any("加快冲突" in x for x in proposal["diff"])
    assert os.path.exists(os.path.join(p.path, "02e-规划提案", f"{proposal['id']}.json"))
    assert any(x["id"] == proposal["id"] for x in longform.list_proposals(p, status="pending"))

    done_card_before = longform._load_cards(p)[1]
    applied = longform.apply_replan(p, proposal["id"])
    assert applied["ok"] is True
    assert applied["applied_cards"]
    cards_after = longform._load_cards(p)
    assert cards_after[1] == done_card_before
    assert "加快冲突" in cards_after[min(proposal["affected_chapters"])]["goal"]
    assert longform.apply_replan(p, proposal["id"])["ok"] is False  # 不可重复应用

    # 审计日志（提案 + 应用）至少两行
    assert _log_lines(os.path.join(p.path, "22-重规划记录.jsonl")) >= 2
    assert any(x["id"] == proposal["id"] for x in longform.list_proposals(p, status="applied"))

    # 驳回
    another = longform.replan(p, scope="chapter", reason="test-reject")
    rej = longform.reject_replan(p, another["id"], note="不采纳")
    assert rej["ok"] is True
    assert any(x["id"] == another["id"] for x in longform.list_proposals(p, status="rejected"))


def test_multi_replan_two_roles_parallel(tmp_path):
    """多角色同时规划：规划师与总编各出一份提案，互不覆盖，可选一份应用。"""
    p = make_project(tmp_path, total=20, done=10)
    longform.ensure_plan(p)
    plan_before = _plan_bytes(p)

    cards = longform.multi_replan(p, scope="window", instruction="把追兵提前")
    assert [c["agent"] for c in cards] == ["planner", "editor"]
    assert all(c["ok"] for c in cards), cards
    ids = [c["proposal_id"] for c in cards]
    assert len(set(ids)) == 2                      # 两份提案各自落盘
    assert all(os.path.exists(os.path.join(p.path, "02e-规划提案", f"{i}.json")) for i in ids)
    assert all(c["label"] for c in cards)          # 前端对比卡需要角色名
    assert all(min(c["affected_chapters"]) > 10 for c in cards)
    assert plan_before == _plan_bytes(p)           # 规划本身仍未被修改

    stored = {x["id"]: x for x in longform.list_proposals(p, status="pending")}
    assert stored[ids[0]]["agent"] == "planner"
    assert stored[ids[1]]["role"] == "总编"

    applied = longform.apply_replan(p, ids[1])
    assert applied["ok"] is True
    assert any(x["id"] == ids[0] for x in longform.list_proposals(p, status="pending"))

    # 只跑一个岗位也行；非法岗位被忽略
    one = longform.multi_replan(p, scope="window", roles=["editor"])
    assert [c["agent"] for c in one] == ["editor"]
    none = longform.multi_replan(p, scope="window", roles=["ghost"])
    assert [c["agent"] for c in none] == ["planner"]


# ---------- 6. 重规划触发 ----------

def test_should_replan_triggers(tmp_path):
    p = make_project(tmp_path, total=130, done=0, with_files=False)
    plan = longform.ensure_plan(p)
    quiet = {"replan_interval": 1000, "low_score": 60}

    assert longform.should_replan(p, 11)[0] is True
    assert "10" in longform.should_replan(p, 11)[1]

    arcs = longform._all_arcs(plan)
    vol_starts = {v["start_chapter"] for v in plan["volumes"]}
    arc_edge = next(a["start_chapter"] for a in arcs
                    if a["start_chapter"] > 1
                    and a["start_chapter"] not in vol_starts
                    and (a["start_chapter"] - 1) % 10 != 0)
    assert longform.should_replan(p, arc_edge, quiet)[0] is True
    assert "弧" in longform.should_replan(p, arc_edge, quiet)[1]

    vol_edge = sorted(vol_starts)[1]
    assert longform.should_replan(p, vol_edge, quiet)[0] is True
    assert "卷" in longform.should_replan(p, vol_edge, quiet)[1]

    # 伏笔逾期 ≥3
    st = state.apply_delta(p.path, 1, {"foreshadowing": {
        "F1": {"description": "一", "due_from": 1, "due_to": 2},
        "F2": {"description": "二", "due_from": 1, "due_to": 2},
        "F3": {"description": "三", "due_from": 1, "due_to": 3},
    }}, source="test")
    assert len(state.foreshadow_due(st, 4)["overdue"]) >= 3
    hit, reason = longform.should_replan(p, 5, quiet)
    assert hit is True and "逾期" in reason


def test_should_replan_low_scores(tmp_path):
    p = make_project(tmp_path, total=30, done=0, with_files=False)
    longform.ensure_plan(p)
    rows = [{"n": n, "title": f"第{n}章", "summary": "x", "quality_signals": {"score": 40}}
            for n in (1, 2, 3)]
    _write(os.path.join(p.path, "21-章摘要.jsonl"),
           "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    hit, reason = longform.should_replan(p, 5, {"replan_interval": 1000, "low_score": 60})
    assert hit is True and "低分" in reason


# ---------- 7. 摘要抽取 ----------

def test_summarize_fallback_and_llm_delta(tmp_path, monkeypatch):
    p = make_project(tmp_path, total=10, done=0, with_files=False)
    longform.ensure_plan(p)

    # LLM 返回空：仍写 21、更新章卡、不抛异常
    text = ("## 本章概要\n林川在码头找到旧案卷。\n\n"
            "## 正文\n雨下得很大。他打开案卷，发现了第十三份证词。")
    res = longform.summarize_and_extract(p, 1, text)
    assert res["fallback"] is True
    assert res["summary"]
    assert os.path.exists(os.path.join(p.path, "21-章摘要.jsonl"))
    rows = [json.loads(x) for x in
            open(os.path.join(p.path, "21-章摘要.jsonl"), encoding="utf-8").read().splitlines()]
    assert rows[0]["n"] == 1 and rows[0]["summary"] == res["summary"]
    assert longform._load_cards(p)[1]["status"] == "done"

    # 合法 JSON：正确应用状态 delta 与伏笔兑现
    state.apply_delta(p.path, 1, {"foreshadowing": {
        "L1": {"description": "旧案卷", "due_from": 1, "due_to": 5}}}, source="setup")
    payload = {
        "summary": "林川拿到案卷，确认了父亲当年被构陷。",
        "beats": ["拿到案卷", "确认构陷"],
        "state_delta": {
            "characters": {"林川": {"status": "受伤", "location": "码头"}},
            "timeline": [{"chapter": 2, "event": "确认构陷", "characters": ["林川"]}],
        },
        "foreshadow_ops": [{"id": "L1", "action": "pay", "payoff_chapter": 2}],
        "issues": ["时间线略紧"],
        "quality_signals": {"score": 88, "hook": "强"},
    }
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(payload, ensure_ascii=False))
    out = longform.summarize_and_extract(p, 2, "林川翻开案卷。")
    assert out["fallback"] is False
    assert out["summary"].startswith("林川拿到案卷")
    st = state.load(p.path)
    assert st["characters"]["林川"]["status"] == "受伤"
    assert st["foreshadowing"]["L1"]["status"] == "paid"
    assert st["foreshadowing"]["L1"]["payoff_chapter"] == 2
    assert st["chapter_summaries"]["2"]["summary"].startswith("林川拿到案卷")
    assert longform._load_cards(p)[2]["status"] == "done"


# ---------- 8. 上下文分层 / 预算 / 不调用 LLM ----------

def test_context_build_keys_budget_and_no_llm(tmp_path, monkeypatch):
    p = make_project(tmp_path, total=10, done=5)
    # 旧项目（无 02c）：必须回退旧文件并返回全部兼容键
    ch = p.plan()["chapters"][5]
    ctx = context.build(p, ch, {"context_budget": 8000})

    required = {
        "chapter_no", "chapter_title", "outline_text", "chapter_plan", "characters_text",
        "style_text", "prev_tail", "cfg", "continuation", "story_bible", "clue_ledger",
        "chapter_summaries", "timeline", "lore_relevant", "graph_ctx", "retrieval_ctx",
        "blueprint", "queue_note", "state_excerpt", "longform_card", "plan_digest",
        "references_block", "continuity_warnings", "style_anchor", "context_meta",
    }
    assert required <= set(ctx)
    assert ctx["chapter_no"] == 6
    assert "第6章" in ctx["chapter_plan"]
    assert ctx["context_meta"]["used"] <= 8000
    assert isinstance(ctx["context_meta"]["dropped"], list)

    # 新项目：章卡 + 相关人物进入 L0
    longform.ensure_plan(p)
    state.apply_delta(p.path, 1, {"characters": {"林川": {
        "status": "逃亡", "location": "码头", "goal": "洗清冤屈"}}}, source="ctx")
    ctx2 = context.build(p, ch, {"context_budget": 12000})
    assert ctx2["longform_card"]["n"] == 6
    assert "林川" in ctx2["state_excerpt"]
    assert ctx2["plan_digest"]["chapters_planned"] == 10
    assert ctx2["context_meta"]["used"] <= 12000

    # 长文本按段落边界截断 + 有丢弃记录
    _write(os.path.join(p.path, "03-文风基准.md"),
           "## 文风指纹\n" + "\n\n".join(f"第{i}段：雨落在铁皮棚上，像有人在外面数数。" * 12
                                          for i in range(60)))
    _write(os.path.join(p.path, "09-故事圣经.md"), "设定。" * 4000)
    _write(os.path.join(p.path, "12-时间线与连续性.md"), "时间线。" * 4000)
    _write(os.path.join(p.path, "11-章节摘要.md"), "摘要。" * 4000)
    refs = [{"id": f"R{i}", "title": f"资料{i}", "excerpt": "参考内容。" * 200} for i in range(8)]
    ctx3 = context.build(p, ch, {"context_budget": 9000, "references": refs})
    assert ctx3["context_meta"]["used"] <= 9000
    assert "……（已压缩）" in ctx3["style_text"]
    assert ctx3["context_meta"]["dropped"], "应记录被丢弃的低优先级块"
    assert ctx3["references_block"]

    def boom(*a, **k):
        raise AssertionError("context.build 不应调用 LLM")

    monkeypatch.setattr(llm, "chat", boom)
    monkeypatch.setattr(llm, "chat_json", boom)
    ctx4 = context.build(p, ch, {"context_budget": 9000})
    assert ctx4["chapter_no"] == 6
    rendered = context.render_context_block(ctx4)
    assert "长篇连续性简报" in rendered and "本章章卡" in rendered


# ---------- 9. 并发安全 ----------

def test_apply_delta_concurrent_writes(tmp_path):
    p = make_project(tmp_path, total=20, done=0, with_files=False)
    longform.ensure_plan(p)
    errors = []
    barrier = threading.Barrier(8)

    def worker(i):
        try:
            barrier.wait(timeout=5)
            state.apply_delta(p.path, i, {
                "characters": {f"角色{i}": {"status": f"状态{i}", "location": "现场"}},
                "timeline": [{"chapter": i, "event": f"事件{i}"}],
            }, source=f"thread{i}")
        except Exception as e:  # pragma: no cover - 失败时便于定位
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    st = state.load(p.path)
    # 迁移会先把 00-人物档案.md 里的既有角色并入状态库；8 个并发写入必须全部保留。
    assert {f"角色{i}" for i in range(1, 9)} <= set(st["characters"])
    assert len(st["timeline"]) == 8
    assert st["metrics"]["last_chapter"] == 8
    # JSON 可解析、日志 8 条 applied
    with open(os.path.join(p.path, "18-状态库.json"), encoding="utf-8") as f:
        assert isinstance(json.load(f), dict)
    log = [json.loads(x) for x in
           open(os.path.join(p.path, "18b-状态日志.jsonl"), encoding="utf-8").read().splitlines()]
    assert sum(1 for x in log if x.get("result") == "applied") == 8


# ---------- 10. 旧扁平计划投影 ----------

def test_plan_to_flat_preserves_written_chapters(tmp_path):
    p = make_project(tmp_path, total=10, done=5)
    longform.ensure_plan(p)
    before = [dict(c) for c in p.plan()["chapters"] if c["chapterNumber"] <= 5]

    longform.plan_book(p, {"chapters": 15})
    flat = p.plan()
    after = [dict(c) for c in flat["chapters"] if c["chapterNumber"] <= 5]

    assert after == before, "已完成章节不能被改写或删除"
    assert flat["totalChapters"] == 15
    assert len(flat["chapters"]) == 15
    assert flat["chapters"][-1]["chapterNumber"] == 15
    assert all(c["status"] == "pending" for c in flat["chapters"][-5:])


# ---------- 11. 旧 Markdown 状态迁移 + 审计/反向摘要 ----------

def test_state_migration_and_audits(tmp_path):
    p = make_project(tmp_path, total=12, done=6)
    st = state.migrate_from_markdown(p.path)
    assert state.exists(p.path)
    assert "1" in st["chapter_summaries"]
    assert "关键转折" in st["chapter_summaries"]["1"]["summary"]
    assert st["metrics"]["last_chapter"] == 6

    plan = longform.ensure_plan(p)
    assert isinstance(longform.audit_book(p), list)
    assert isinstance(longform.audit_volume(p, plan["volumes"][0]["no"]), list)
    assert isinstance(longform.audit_arc(p, longform._all_arcs(plan)[0]["no"]), list)
    assert "当前故事进度" in longform.reverse_outline(p)
    digest = longform.plan_digest(p)
    assert digest["chapters_written"] == 6
    assert context.estimate_tokens("这是一段中文正文。" * 20) > 0
