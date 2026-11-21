"""v5.5.1 写作韧性层回归：524/超时重试降载、章卡闸门自愈闭环、停摆注册表收口、
「继续」自检清残留、repair_stalled_book 工具体检/修复幂等。

实测背景（桌面端 2026-11-21《实习生掉马那天》）：
· 供应商代理 120s 读超时 → planning 调用 524 → longform._llm_json 吞成 {} →
  章卡永远占位 → v5 写前闸门拒收 → _ensure_card_planned False → plan_blocked 停批；
· 批量写作线程死亡后注册表残留 →「继续」被防重入拦死。
全部用例跑在 tmp_path，不碰真实数据、不连网。
"""
import json
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import httpx
from bot import llm, model_rules, story_kernel as v5
from bot import writer

# conftest 的 autouse fixture 会把 llm.chat 换成 stub；524 用例要在测试内还原真身。
_REAL_CHAT = llm.chat

TS = "20261121-190000"
TOOL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "tools", "repair_stalled_book.py")
TOOLS_DIR = os.path.dirname(TOOL_PATH)


# ---------------------------------------------------------------- 脚手架

class _P:
    """轻量 Project 壳（story_kernel / writer 自愈只用 path/plan/save_plan/log/lock）。"""

    def __init__(self, path, plan):
        self.path = path
        self.name = plan.get("novelName", "书")
        self.lock = threading.RLock()
        self.running = False
        self._plan = plan

    def plan(self):
        return self._plan

    def save_plan(self, plan):
        self._plan = plan
        with open(os.path.join(self.path, "02-写作计划.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False)

    def log(self, msg):
        with open(os.path.join(self.path, "运行日志.txt"), "a", encoding="utf-8") as f:
            f.write(f"{msg}\n")


def _setup_book(tmp_path, chapters=3, v5_on=True):
    """造一本 v5 新书：扩展头人物档案 + 空章卡占位（复刻《掉马那天》形态）。"""
    pdir = tmp_path / f"{TS}-测试书"
    pdir.mkdir(parents=True, exist_ok=True)
    chars = """# 人物档案

## 一、主角：沈砚
- **身份/资源**：远星资本实际控制人，表面是普通顾问
- **性格内核**：克制，越被轻视越冷静
- **外在目标**：查清父亲旧案
- **缺陷**：习惯隐瞒底牌
- **说话方式**：短句，先用事实堵人

## 二、二号主角：陆之遥
- **身份/资源**：战投部总监
- **性格内核**：嘴上不饶人，手上不误事
- **外在目标**：守住部门
- **缺陷**：靠怼筛人

## 三、反派：顾承泽
- **身份/资源**：对手方副总裁
- **缺陷**：迷信报表
"""
    (pdir / "00-人物档案.md").write_text(chars, encoding="utf-8")
    plan = {"novelName": "测试书", "totalChapters": chapters, "minWordsPerChapter": 3000,
            "config": {"v5_kernel": v5_on, "genre": "都市", "longform": {"enabled": True}},
            "chapters": [{"chapterNumber": i, "title": f"第{i}章",
                          "filePath": f"第{i:02d}章.md", "status": "pending"}
                         for i in range(1, chapters + 1)]}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    p = _P(str(pdir), plan)
    v5.ensure_project(p, plan["config"], name="测试书", characters_text=chars)
    return p


def _placeholder_card(n=1):
    """占位卡：goal 是弧目标套话、硬字段全空（《掉马那天》实测形态）。"""
    return {"n": n, "volume": 1, "arc": 1, "title": f"第{n}章",
            "goal": "承接「第一卷」，完成本弧的小高潮（第1–3章）；本章为弧内第1/3章",
            "conflict": "", "turn": "", "payoff": "", "cliffhanger": "",
            "cast": [], "locations": [], "scenes": [], "scene_type": "",
            "story_time": "", "protagonist_action": "", "threads_advance": [],
            "word_target": 3000, "status": "planned", "updated_at": ""}


def _write_cards(pdir, cards):
    with open(os.path.join(pdir, "02d-章卡.jsonl"), "w", encoding="utf-8") as f:
        for n in sorted(cards):
            f.write(json.dumps(cards[n], ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 1) 524/超时重试降载

class _FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://unit-test/v1/chat/completions")
            resp = httpx.Response(self.status_code, request=req, text=self.text)
            raise httpx.HTTPStatusError(f"Server error '{self.status_code}'",
                                        request=req, response=resp)


def _snap(b):
    """请求体深拷贝快照（body 会被降载改写，必须存当时的样子）。"""
    try:
        return json.loads(json.dumps(b, ensure_ascii=False))
    except Exception:
        return None


class _FlakyClient:
    """按脚本逐次返回 524/200 的假 httpx.Client；记录每次请求体快照。"""

    script = []
    bodies = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        _FlakyClient.bodies.append(_snap(json))
        st = _FlakyClient.script.pop(0) if _FlakyClient.script else 200
        if st == 200:
            return _FakeResp(200, {"choices": [{"message": {"content": "ok"}}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        return _FakeResp(st, text="proxy 120s timeout")


class _NoSleepTime:
    """time 模块替身：chat() 的重试 sleep 立即返回，time.time 原样。"""

    def __init__(self, real):
        self._real = real

    def sleep(self, _s):
        pass

    def time(self):
        return self._real.time()

    def strftime(self, *a, **k):
        return self._real.strftime(*a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def fake_llm_env(monkeypatch):
    """隔离 524 用例：假供应商路由 + 真 chat + 假 httpx + 免 sleep。"""
    prov = {"id": "unit", "base_url": "http://127.0.0.1:1/v1", "timeout": 5,
            "max_tokens": 500, "temperature": 0.3}
    monkeypatch.setattr(llm, "chat", _REAL_CHAT, raising=False)
    monkeypatch.setattr(llm.providers, "agent_resolve",
                        lambda cfg, agent=None, stage=None: (prov, "unit-model"))
    monkeypatch.setattr(model_rules, "effective_rules", lambda p, o: {"backoff_base": 0})
    monkeypatch.setattr(model_rules, "pace_rpm", lambda rules: None)
    monkeypatch.setattr(model_rules, "clamp_context", lambda msgs, ctx: msgs)
    monkeypatch.setattr(llm, "_max_global", lambda: 1)
    monkeypatch.setattr(llm, "_throttle", lambda cfg: None)
    monkeypatch.setattr(llm, "_trace_enabled", lambda: False)
    monkeypatch.setattr(llm, "_log_llm_error", lambda *a, **k: None)
    monkeypatch.setattr(llm, "time", _NoSleepTime(time))
    monkeypatch.setattr(llm.httpx, "Client", _FlakyClient)
    _FlakyClient.script = []
    _FlakyClient.bodies = []
    return {"defaults": {"call_gap": 0, "llm_cache": False}}


def test_524_retry_shrinks_input_then_succeeds(fake_llm_env):
    """524 → 重试 1 次原样 → 再失败缩小输入 → 成功；第 3 次请求体确实变小。"""
    big = "上下文" * 900
    messages = [{"role": "system", "content": "你是规划师。" + big},
                {"role": "user", "content": "规划第1章。" + big}]
    _FlakyClient.script = [524, 524, 200]
    out = llm.chat(messages, cfg=fake_llm_env, stage="planning", retries=3)
    assert out == "ok"
    assert len(_FlakyClient.bodies) == 3
    sizes = [len(json.dumps(b["messages"], ensure_ascii=False)) for b in _FlakyClient.bodies]
    assert sizes[0] == sizes[1]          # 第 2 次尝试原样
    assert sizes[2] < sizes[0]           # 第 3 次尝试已缩小输入


def test_524_final_failure_raises_actionable_chinese(fake_llm_env):
    """全链路失败：抛中文可行动报错（含 524/缩小输入/换模型建议），保留原始错误。"""
    _FlakyClient.script = [524, 524, 524]
    messages = [{"role": "system", "content": "你是规划师。" + "上下文" * 900},
                {"role": "user", "content": "规划第1章。" + "上下文" * 900}]
    with pytest.raises(llm.LLMError) as ei:
        llm.chat(messages, cfg=fake_llm_env, stage="planning", retries=3)
    msg = str(ei.value)
    assert "524" in msg and "缩小输入" in msg and "模型" in msg


def test_524_non_planning_does_not_shrink_but_humanizes(fake_llm_env):
    """非 planning 环节：原样重试到底（请求体不变），报错仍是中文可行动版。"""
    _FlakyClient.script = [524, 524, 524]
    messages = [{"role": "user", "content": "写正文。" + "情节" * 900}]
    with pytest.raises(llm.LLMError) as ei:
        llm.chat(messages, cfg=fake_llm_env, stage="writing", retries=3)
    assert len(_FlakyClient.bodies) == 3
    sizes = {len(json.dumps(b["messages"], ensure_ascii=False)) for b in _FlakyClient.bodies}
    assert len(sizes) == 1
    assert "524" in str(ei.value)


def test_shrink_messages_keeps_head_tail_and_recent():
    """缩输入：保留 system 头尾与最近消息，折叠更早历史，总量减半以上。"""
    msgs = [{"role": "system", "content": "SYS-" + "A" * 4000},
            {"role": "user", "content": "旧历史" + "B" * 3000},
            {"role": "assistant", "content": "次新" + "C" * 100},
            {"role": "user", "content": "最新" + "D" * 100}]
    out = llm._shrink_messages(msgs)
    assert out[0]["role"] == "system"
    assert "SYS-" in out[0]["content"]
    assert out[-1]["content"].startswith("最新")
    assert len(json.dumps(out, ensure_ascii=False)) < \
        len(json.dumps(msgs, ensure_ascii=False)) / 2
    assert "已省略" in json.dumps(out, ensure_ascii=False)


def test_is_load_timeout_marks():
    assert llm._is_load_timeout(llm.LLMError("Server error '524 ...'"))
    assert llm._is_load_timeout(llm.LLMError("LLM 调用总时长超过 120s（wall-clock 守卫强制中断）"))
    assert llm._is_load_timeout(llm.LLMError("LLM 线程池排队超过 60s"))
    assert not llm._is_load_timeout(llm.LLMError("API 密钥无效（HTTP 401）"))


# ---------------------------------------------------------------- 2) 拒绝原因结构化

def test_reject_fields_maps_codes():
    blocks = [{"code": "V5C03", "message": "章卡缺少核心冲突"},
              {"code": "V5C11", "message": "cast 少于 2 人"},
              {"code": "V5C13", "message": "locations 为空"}]
    s = v5.rejection_summary(blocks)
    assert set(s["fields"]) == {"conflict", "cast", "locations"}
    assert "[V5C03]" in s["prompt"] and "irreversible" in s["prompt"]


# ---------------------------------------------------------------- 3) 章卡闸门自愈闭环

def test_gate_selfheal_deterministic_fill_passes(tmp_path):
    """拒收占位卡 → 自愈第一轮确定性补全 → 同一套 card_lint 复检通过（零 LLM）。"""
    p = _setup_book(tmp_path)
    card = _placeholder_card(1)
    _write_cards(p.path, {1: card})
    blocks = [x for x in v5.card_lint(p, card, 1, {1: card}) if x.get("level") == "block"]
    assert blocks
    res = writer.card_gate_selfheal(p, 1, {"genre": "都市"}, blocks, card=card, rounds=2)
    assert res["ok"] and not res["degraded"]
    assert res["via"] == "deterministic_fill"
    after = [x for x in v5.card_lint(p, res["card"], 1, {1: res["card"]})
             if x.get("level") == "block"]
    assert not after, after
    with open(os.path.join(p.path, "02d-章卡.jsonl"), encoding="utf-8") as f:
        saved = json.loads(f.readline())
    assert saved["conflict"] and saved["cast"] and saved["locations"]
    # 事实源顺带修复：占位主角 → 扩展头真名
    assert v5.load_facts(p)["protagonist"] == "沈砚"


def test_gate_selfheal_downgrades_to_legacy_after_rounds(tmp_path, monkeypatch):
    """确定性补全修不掉的拒收（V5C01 goal 原始倾倒）→ 两轮后降级放行 legacy + 记账。"""
    p = _setup_book(tmp_path)
    card = _placeholder_card(1)
    card["goal"] = "核心事件展开**：把场景分镜铺完"   # V5C01 且确定性补全不会改写（短）
    _write_cards(p.path, {1: card})
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {}, raising=False)
    blocks = [x for x in v5.card_lint(p, card, 1, {1: card}) if x.get("level") == "block"]
    assert any(x["code"] == "V5C01" for x in blocks)
    res = writer.card_gate_selfheal(p, 1, {}, blocks, card=card, rounds=2)
    assert res["ok"] and res["degraded"]
    assert res["rounds"] == 2
    assert writer._gate_legacy_exempt(p, 1)          # 幂等放行登记
    ledger = open(os.path.join(p.path, v5.LEDGER_FILE), encoding="utf-8").read()
    assert "gate_fallback" in ledger                  # 质量账本留痕（不静默）


def test_ensure_card_planned_no_longer_stalls_batch(tmp_path, monkeypatch):
    """《掉马那天》停摆根因回归：LLM 补规划两连败 → 自愈兜底 → 返回 True 不再停批。"""
    p = _setup_book(tmp_path)
    from bot import longform as lf
    _write_cards(p.path, {1: _placeholder_card(1)})
    monkeypatch.setattr(lf, "plan_chapter_now", lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {}, raising=False)
    assert writer._ensure_card_planned(p, 1) is True
    cards = lf.load_cards(p)
    assert cards.get(1, {}).get("conflict")


def test_preflight_downgrade_records_legacy(tmp_path):
    """legacy 放行登记：登记章豁免、未登记章不豁免（幂等边界）。"""
    p = _setup_book(tmp_path)
    _write_cards(p.path, {1: _placeholder_card(1)})
    writer._mark_gate_legacy(p, 1, "测试登记")
    assert writer._gate_legacy_exempt(p, 1)
    assert not writer._gate_legacy_exempt(p, 2)


def test_degenerate_facts_repaired(tmp_path):
    """事实源退化修补：扩展头人物登记真名、书名对齐、占位人物清理。"""
    p = _setup_book(tmp_path)
    facts = v5.load_facts(p)
    facts["protagonist"] = "主角"
    facts["core_cast"] = [{"name": "主角", "role": "主角"}]
    facts["locations"] = []
    facts["title"] = "旧书名占位"
    facts["aliases"] = []
    from bot.safeio import atomic_write_json
    atomic_write_json(v5.facts_path(p), facts)
    fixed = writer.repair_degenerate_facts(p)
    facts2 = v5.load_facts(p)
    assert facts2["protagonist"] == "沈砚"
    assert facts2["deuteragonist"] == "陆之遥"
    names = [c["name"] for c in facts2["core_cast"]]
    assert "沈砚" in names and "主角" not in names
    assert facts2["locations"]
    assert facts2["title"] == "测试书"
    assert "旧书名占位" in facts2["aliases"]
    assert any("protagonist" in x for x in fixed["fixed"])


def test_extract_locations_no_verb_phrases():
    locs = writer.extract_locations("她必须在现场现编谎言，为由封锁病房，报销流和茶水间情报网，"
                                    "深夜进过董事长办公室，笑完躲进楼梯间。")
    assert "现场" in locs and "病房" in locs and "茶水间" in locs
    assert not any(x.startswith("她必须") for x in locs)
    assert not any(x.startswith("为由") for x in locs)
    assert "董事长办公室" in locs


# ---------------------------------------------------------------- 4) 停摆注册表收口

def test_registry_selfcheck_clears_dead_thread_residue(tmp_path):
    """线程已死但 _RUNNING_BOOKS/pipeline/livejob 残留 → 自检全部收口。"""
    from bot import livejob
    pdir = str(tmp_path / "book1")
    os.makedirs(pdir, exist_ok=True)
    bk = "book1"
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    with writer._RUNNING_LOCK:
        writer._RUNNING_THREADS[bk] = dead
    writer._set_book_running(bk, True)
    writer.mark_pipeline(bk, True)
    livejob.begin(bk, kind="resume")
    assert writer._book_running(bk)
    assert writer.registry_selfcheck(bk) is True
    assert not writer._book_running(bk)
    assert not writer.pipeline_running(bk)
    assert not livejob.is_active(bk)
    with writer._RUNNING_LOCK:
        assert bk not in writer._RUNNING_THREADS


def test_registry_selfcheck_keeps_live_thread(tmp_path):
    """线程活着 → 自检不动注册表（防误杀在途写作）。"""
    bk = "book-live"
    ev = threading.Event()

    def _spin():
        ev.wait(3)

    t = threading.Thread(target=_spin, daemon=True)
    t.start()
    with writer._RUNNING_LOCK:
        writer._RUNNING_THREADS[bk] = t
    writer._set_book_running(bk, True)
    try:
        assert writer.registry_selfcheck(bk) is False
        assert writer._book_running(bk)
    finally:
        ev.set()
        t.join(3)
        writer._set_book_running(bk, False)
        with writer._RUNNING_LOCK:
            writer._RUNNING_THREADS.pop(bk, None)


def test_start_writing_recovers_from_residue(tmp_path, monkeypatch):
    """注册表残留时 start_writing 先清再启（不再被防重入拦死）。"""
    p = _setup_book(tmp_path)
    bk = os.path.basename(p.path)
    writer._set_book_running(bk, True)          # 模拟残留（无线程登记 → 已死）
    monkeypatch.setattr(writer, "_write_loop_safe", lambda *a, **k: time.sleep(0.3),
                        raising=False)
    ok = writer.start_writing(p)
    assert ok is True
    assert writer._book_running(bk)
    # 收尾：等假线程退出后自检清残留（不污染其他用例）
    time.sleep(0.5)
    writer.registry_selfcheck(bk, p)
    writer._set_book_running(bk, False)


def test_write_loop_outer_shell_closes_registry(tmp_path, monkeypatch):
    """外层收口壳：内层线程崩溃（逃过内层 finally）→ 注册表/实时作业全部收口。"""
    p = _setup_book(tmp_path)
    bk = os.path.basename(p.path)

    def _boom(*a, **k):
        raise RuntimeError("inner exploded")

    monkeypatch.setattr(writer, "_write_loop", _boom, raising=False)
    writer._write_loop_safe(p)
    assert not writer._book_running(bk)
    assert p.running is False
    assert writer.pipeline_running(bk) is False
    with writer._RUNNING_LOCK:
        assert bk not in writer._RUNNING_THREADS


def test_resume_book_selfcheck_clears_residue(tmp_path, monkeypatch):
    """resume_book 入口自检：注册表残留被清后正常启动写作线程。"""
    projects = tmp_path / "projects"
    projects.mkdir()
    p = _setup_book(projects)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(projects))
    monkeypatch.setattr(writer, "_RENAME_MAP_CACHE", {"mtime": None, "map": {}})
    bk = os.path.basename(p.path)
    writer._set_book_running(bk, True)          # 残留
    ev = threading.Event()
    monkeypatch.setattr(writer, "_write_loop_safe",
                        lambda proj, bs=None: ev.set(), raising=False)

    class _FakeSem:
        def acquire(self):
            pass

        def release(self):
            pass

    monkeypatch.setattr(writer, "_write_semaphore", lambda: _FakeSem(), raising=False)
    assert writer.resume_book(bk) is True
    assert ev.wait(3)
    # 假线程同步结束 → 自检可清（真线程活着时自检不会清）
    time.sleep(0.05)
    writer.registry_selfcheck(bk, p)
    assert not writer._book_running(bk)


# ---------------------------------------------------------------- 5) repair 工具

def _run_tool(args, root):
    import subprocess
    cmd = [sys.executable, "-I", TOOL_PATH] + args + ["--root", root]
    return subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ, "YIMU_DATA_DIR": root})


def _corrupt_to_stalled(pdir):
    """把健康书改坏成《掉马那天》停摆形态：占位卡 + 残留标志 + 占位主角 + 书名错位。"""
    from bot.safeio import atomic_write_json
    _write_cards(pdir, {i: _placeholder_card(i) for i in (1, 2, 3)})
    fp = os.path.join(pdir, "02-写作计划.json")
    plan = json.load(open(fp, encoding="utf-8"))
    plan.setdefault("meta", {})["plan_blocked"] = True
    plan["meta"]["planning_running"] = True
    atomic_write_json(fp, plan)
    fp = os.path.join(pdir, "00-作品事实源.json")
    facts = json.load(open(fp, encoding="utf-8"))
    facts["protagonist"] = "主角"
    facts["core_cast"] = [{"name": "主角", "role": "主角"}]
    facts["locations"] = []
    facts["title"] = "旧名"
    atomic_write_json(fp, facts)
    atomic_write_json(os.path.join(pdir, "任务档案.json"),
                      {"active": {"id": "t1", "kind": "writing", "title": "写《测试书》",
                                  "status": "running", "stage": "plan_blocked", "done": 0,
                                  "total": 3, "started_at": "2026-11-21T10:00:00",
                                  "updated_at": "2026-11-21T10:00:00"},
                       "history": [], "memory": [], "fails": {}})


def test_repair_tool_checkup_then_apply_then_idempotent(tmp_path):
    """体检（只读）→ --apply 修复 → 复检健康 → 再跑幂等。"""
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    seed = _setup_book(tmp_path / "seed")
    pdir = os.path.join(root, "projects", f"{TS}-测试书")
    shutil.copytree(seed.path, pdir)
    _corrupt_to_stalled(pdir)

    # ① 只读体检：报卡点、不写盘
    out = _run_tool(["--book", "测试书"], root)
    assert out.returncode == 0
    assert "章卡闸门拒收" in out.stdout and "停摆" in out.stdout
    plan = json.load(open(os.path.join(pdir, "02-写作计划.json"), encoding="utf-8"))
    assert plan["meta"]["plan_blocked"] is True          # 未被写盘

    # ② --apply 修复
    out = _run_tool(["--book", "测试书", "--apply"], root)
    assert out.returncode == 0
    assert "复检通过" in out.stdout and "续跑预检：可继续" in out.stdout
    plan = json.load(open(os.path.join(pdir, "02-写作计划.json"), encoding="utf-8"))
    assert not plan["meta"].get("plan_blocked")
    jd = json.load(open(os.path.join(pdir, "任务档案.json"), encoding="utf-8"))
    assert jd["active"]["status"] == "paused" and jd["active"]["stage"] != "plan_blocked"
    facts = json.load(open(os.path.join(pdir, "00-作品事实源.json"), encoding="utf-8"))
    assert facts["protagonist"] == "沈砚" and facts["title"] == "测试书"
    cards = {json.loads(x)["n"]: json.loads(x)
             for x in open(os.path.join(pdir, "02d-章卡.jsonl"), encoding="utf-8") if x.strip()}
    assert cards[1]["conflict"] and cards[1]["cast"]
    assert os.path.exists(os.path.join(pdir, "26-停摆修复报告.json"))

    # ③ 幂等：再跑一次 --apply，无新修复动作、结论健康
    out = _run_tool(["--book", "测试书", "--apply"], root)
    assert "健康" in out.stdout and "全部可写" in out.stdout


def test_repair_tool_lock_discipline(tmp_path, monkeypatch):
    """写盘纪律：桌面端真在跑 → 拒绝写盘并给指引；锁残留（PID 已死）→ 判可写。"""
    sys.path.insert(0, TOOLS_DIR)
    try:
        import repair_stalled_book as tool
        root = str(tmp_path / "lockroot")
        os.makedirs(root, exist_ok=True)
        lockp = os.path.join(root, "instance.lock")
        with open(lockp, "w") as f:
            f.write("424242")
        # 真在跑：PID 活着、命令行像本应用、且 lsof 报告持有锁
        monkeypatch.setattr(tool, "_pid_command",
                            lambda pid: "/Applications/YiMuLit.app run.py")
        monkeypatch.setattr(tool, "_lock_holders", lambda path: [424242])
        st = tool.instance_lock_status(root)
        assert st["writable"] is False and "退出桌面端" in st["reason"]
        # 残留锁：PID 不在任何进程
        monkeypatch.setattr(tool, "_pid_command", lambda pid: "")
        monkeypatch.setattr(tool, "_lock_holders", lambda path: [])
        st2 = tool.instance_lock_status(root)
        assert st2["writable"] is True and "残留" in st2["reason"]
    finally:
        sys.path.pop(0)


def test_repair_tool_phantom_cleanup_merges_then_deletes(tmp_path):
    """幻影目录：先合并聊天/事件历史进真书再删除；有正文的绝不删。"""
    root = str(tmp_path / "root2")
    projects = os.path.join(root, "projects")
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    os.makedirs(projects, exist_ok=True)
    real = os.path.join(projects, f"{TS}-真书")
    ghost = os.path.join(projects, f"{TS}-幻影")
    with_content = os.path.join(projects, f"{TS}-有正文")
    for d in (real, ghost, with_content):
        os.makedirs(d, exist_ok=True)
    with open(os.path.join(root, "data", "rename_map.json"), "w", encoding="utf-8") as f:
        json.dump({f"{TS}-幻影": f"{TS}-真书"}, f, ensure_ascii=False)
    with open(os.path.join(ghost, "chat_log.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "幻影里的对话", "ts": 1},
                           ensure_ascii=False) + "\n")
    with open(os.path.join(real, "chat_log.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "assistant", "content": "真书原有", "ts": 0},
                           ensure_ascii=False) + "\n")
    with open(os.path.join(with_content, "第01章.md"), "w", encoding="utf-8") as f:
        f.write("有正文的目录绝不删")
    sys.path.insert(0, TOOLS_DIR)
    try:
        import repair_stalled_book as tool
        ph = tool.plan_phantoms(root)
        assert ph["delete"] == [f"{TS}-幻影"]
        assert f"{TS}-有正文" in [x["key"] for x in ph["keep_with_content"]]
        cl = tool.clean_phantoms(root, ph)
        assert cl["removed"] == [f"{TS}-幻影"]
        assert not os.path.isdir(ghost)
        rows = [json.loads(x) for x in open(os.path.join(real, "chat_log.jsonl"),
                                            encoding="utf-8") if x.strip()]
        assert any(r.get("content") == "幻影里的对话" for r in rows)   # 历史已合并
        assert os.path.exists(os.path.join(with_content, "第01章.md"))  # 有正文不动
    finally:
        sys.path.pop(0)
