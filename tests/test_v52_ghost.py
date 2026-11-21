"""v5.2.0 幽灵字（bot/ghost.py + POST /api/ghost）回归。

覆盖：模式自判（句尾标点收住→续一拍 0.8 / 句中逗号→补半句 0.3）、参数门
（服务端 8s 间隔限流 → 429 + 冷却秒数）、上下文组装（人物卡按正文命中 /
场景锚点末 2 句 / 前文末 3 段）、产出清洗（纯净域口径：思考标签/客套语/
元信息小节截断/字数截断）、scene 中文场景记账字段（账本行新增可选字段，
既有字段一律保留）、路由层（/api/ghost 429 与 502、/api/quickwrite SSE 帧）。
所有用例不连网：llm.chat / chat_stream 由 monkeypatch 兜底。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot import ghost, llm, webai

BODY = "\n\n".join([
    "雨是从傍晚开始下的。",
    "他收住了脚步，回头望了一眼。",
    "巷子深处没有灯，只有积水里的倒影。",
    "沈默把伞收了起来。",
])


# ---------------------------------------------------------------- 模式自判

def test_detect_mode_sentence_end_beats_with_08():
    for tail in ("他收住了脚步。", "天边亮起一道闪电！", "门外有人吗？", "雨停了……"):
        assert ghost.detect_mode(tail) == "beat", tail
    m = ghost.mode_of("他收住了脚步。")
    assert m["temperature"] == 0.8
    assert m["max_chars"] == 60
    assert m["label"] == "续一拍"


def test_detect_mode_mid_sentence_half_with_03():
    for tail in ("他说，", "手里的活没停、", "灯影摇曳；", "他看着那扇门："):
        assert ghost.detect_mode(tail) == "half", tail
    # 无标点收尾（话说一半）→ 更保守的补半句
    assert ghost.detect_mode("天色渐渐暗") == "half"
    m = ghost.mode_of("他说，")
    assert m["temperature"] == 0.3
    assert m["max_chars"] == 25


def test_detect_mode_empty_text_defaults_to_beat():
    assert ghost.detect_mode("") == "beat"
    assert ghost.detect_mode("   ") == "beat"


# ---------------------------------------------------------------- 参数门（服务端 8s 间隔限流）

def test_rate_gate_allows_first_and_blocks_rapid_second():
    assert ghost.check_rate("k1") == 0.0
    with pytest.raises(ghost.GhostRateLimited) as ei:
        ghost.check_rate("k1")
    assert 0 < ei.value.cooldown <= ghost.MIN_INTERVAL_SECS


def test_rate_gate_releases_after_interval(monkeypatch):
    monkeypatch.setattr(ghost, "MIN_INTERVAL_SECS", 0.01)
    assert ghost.check_rate("k2") == 0.0
    time.sleep(0.02)
    assert ghost.check_rate("k2") == 0.0


def test_rate_gate_isolated_per_book():
    assert ghost.check_rate("ka") == 0.0
    assert ghost.check_rate("kb") == 0.0          # 不同书互不影响
    with pytest.raises(ghost.GhostRateLimited):
        ghost.check_rate("ka")


# ---------------------------------------------------------------- 上下文组装

@pytest.fixture()
def proj_with_cards(tmp_path, monkeypatch):
    """临时书目录：00-人物档案.md 两个人物，正文只命中其一。"""
    pdir = tmp_path / "projects" / "bk1"
    pdir.mkdir(parents=True)
    (pdir / "00-人物档案.md").write_text(
        "# 人物档案\n\n"
        "- 沈默：主角，刑警，说话短促，惯用反问。\n"
        "  口头禅：「证据呢？」\n"
        "- 陆青藤：法医，冷静克制，口头禅「数据不会说谎」。\n",
        encoding="utf-8")
    monkeypatch.setattr(ghost, "DATA_ROOT", str(tmp_path))
    return pdir


def test_build_messages_carries_character_cards_by_body_hits(proj_with_cards):
    msgs, meta = ghost.build_messages(BODY, key="bk1", title="第1章 试探")
    user = msgs[1]["content"]
    # 命中正文的人物卡进入上下文
    assert "沈默" in user and "刑警" in user
    # 未在正文出现的人物不进上下文（按正文命中挑取）
    assert "陆青藤" not in user
    assert "【场景锚点" in user and "沈默把伞收了起来。" in user   # 末 2 句
    # 末 3 段：BODY 共 4 段，第一段应被截掉
    assert "【前文" in user and "他收住了脚步，回头望了一眼。" in user
    prev_section = user.split("【前文")[1]
    assert "雨是从傍晚开始下的。" not in prev_section
    assert "【章题】第1章 试探" in user
    assert meta["mode"] == "beat" and meta["temperature"] == 0.8


def test_build_messages_last_paragraphs_clamped_to_three(proj_with_cards):
    body = "\n\n".join(f"第{i}段的内容。" for i in range(1, 9))
    _, _meta = ghost.build_messages(body, key="bk1")
    # 直接验证工具口径：末 3 段
    assert ghost.last_paragraphs(body, 3) == "\n\n".join(f"第{i}段的内容。" for i in range(6, 9))
    assert ghost.last_sentences("昨夜有雨。他睡得很沉。梦里全是水声，", 2) == "他睡得很沉。梦里全是水声，"


def test_character_cards_empty_without_file_or_hits(tmp_path, monkeypatch):
    monkeypatch.setattr(ghost, "DATA_ROOT", str(tmp_path))
    assert ghost.character_cards("none", BODY) == ""
    (tmp_path / "projects" / "bk2").mkdir(parents=True)
    (tmp_path / "projects" / "bk2" / "00-人物档案.md").write_text("- 王五：路人。", encoding="utf-8")
    assert ghost.character_cards("bk2", BODY) == ""   # 名字不在正文 → 不挑


def test_system_prompt_demands_direct_continuation_and_no_meta():
    for mode in ("beat", "half"):
        p = ghost.system_prompt(mode)
        assert "直接输出续写正文" in p
        assert "不要任何解释" in p or "禁止出现" in p
        assert "元信息" in p
    assert "续一拍" in ghost.system_prompt("beat")
    assert "补半句" in ghost.system_prompt("half")
    assert "≤25 字" in ghost.system_prompt("half") or "25 字" in ghost.system_prompt("half")


# ---------------------------------------------------------------- 调用封装（scene 记账 / 清洗）

def test_run_uses_mode_temperature_tight_tokens_and_scene_tag(monkeypatch):
    seen = {}

    def fake_chat(messages, temperature=None, max_tokens=None, **kw):
        seen.update(temperature=temperature, max_tokens=max_tokens, scene=kw.get("scene"),
                    messages=messages)
        return "他抬头看了看天。"

    monkeypatch.setattr(llm, "chat", fake_chat, raising=False)
    r = ghost.run("他收住了脚步。", key="bk1")
    assert seen["temperature"] == 0.8
    assert seen["scene"] == "幽灵字"           # scene 中文场景标签进记账
    assert 0 < seen["max_tokens"] <= 200       # max_tokens 收紧（默认关思考兜底）
    assert r["mode"] == "beat" and r["chars"] == len("他抬头看了看天。")
    assert r["text"] == "他抬头看了看天。"


def test_run_half_mode_uses_lower_temperature(monkeypatch):
    seen = {}

    def fake_chat(messages, temperature=None, **kw):
        seen["temperature"] = temperature
        seen["scene"] = kw.get("scene")
        return "声音低了下去。"

    monkeypatch.setattr(llm, "chat", fake_chat, raising=False)
    r = ghost.run("他说，", key="bk1", mode="half")
    assert seen["temperature"] == 0.3
    assert seen["scene"] == "幽灵字"
    assert r["mode"] == "half"


def test_trace_call_scene_field_optional_and_kept_in_record(tmp_path, monkeypatch):
    """账本行：传 scene 时落 scene 字段；不传时既有字段原样（scene 缺席）。"""
    f = tmp_path / "llm_calls.jsonl"
    monkeypatch.setattr(llm, "_TRACE_FILE", str(f))
    monkeypatch.setattr(llm, "_trace_enabled", lambda: True, raising=False)
    llm._trace_call(book="bk1", stage="writing", ok=True, total_tokens=10,
                    provider="p", model="m", cached=False, latency_ms=5, scene="幽灵字")
    llm._trace_call(book="bk1", stage="writing", ok=True, total_tokens=10,
                    provider="p", model="m", cached=False, latency_ms=5)
    lines = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines[0]["scene"] == "幽灵字"
    assert "scene" not in lines[1]                       # 可选字段：缺席不写 null
    for keep in ("book", "stage", "ok", "total_tokens", "ts"):   # 既有字段一律保留
        assert keep in lines[0] and keep in lines[1]


def test_clean_suggestion_strips_noise_and_meta_sections():
    assert ghost.clean_suggestion("<think>推理</think>他抬头看了看天。", 60) == "他抬头看了看天。"
    assert ghost.clean_suggestion("好的，以下是续写：\n他抬头看了看天。", 60) == "他抬头看了看天。"
    # 元信息小节即截断（纯净域铁律 0）
    dirty = "他抬头看了看天。\n## 修改说明\n加强了雨声。"
    assert ghost.clean_suggestion(dirty, 60) == "他抬头看了看天。"
    # 建议本身全是元信息小节 → 整段放弃；正文小节头（## 正文）按 purity 口径剥壳留内容
    assert ghost.clean_suggestion("## 修改说明\n加强了雨声。", 60) == ""
    assert ghost.clean_suggestion("## 本章概要\n雨夜追凶。", 60) == ""
    assert ghost.clean_suggestion("## 正文\n他抬头看了看天。", 60) == "他抬头看了看天。"
    # 只取第一段（幽灵字不跨段）
    assert ghost.clean_suggestion("他抬头。\n\n第二段开始了。", 60) == "他抬头。"


def test_clean_suggestion_caps_chars_at_punctuation():
    long_suggestion = "他抬头看了看天，雨水顺着屋檐滴落，" + "打湿了肩膀。" * 3
    out = ghost.clean_suggestion(long_suggestion, 25)
    assert len(out) <= 25
    assert out.endswith(("。", "，", "、", "；", "：", "！", "？", "…"))
    assert out.startswith("他抬头看了看天，")


def test_run_raises_llm_error_when_cleaned_empty(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "<think>只肯思考不肯写字</think>", raising=False)
    with pytest.raises(llm.LLMError):
        ghost.run("他收住了脚步。", key="bk1")


# ---------------------------------------------------------------- 路由（scratch FastAPI + webai router）

@pytest.fixture()
def client(monkeypatch):
    ghost.reset_rate_for_tests()
    app = FastAPI()
    app.include_router(webai.router)
    c = TestClient(app)
    yield c
    ghost.reset_rate_for_tests()


def test_ghost_route_ok(client, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "他抬头看了看天。", raising=False)
    r = client.post("/api/ghost", json={"key": "bk1", "text": "他收住了脚步。"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["text"] == "他抬头看了看天。"
    assert body["mode"] == "beat" and body["chars"] == 8


def test_ghost_route_429_with_cooldown_seconds(client):
    r1 = client.post("/api/ghost", json={"key": "rapid", "text": "他收住了脚步。"})
    assert r1.status_code in (200, 502)          # 首次放行（502=未配模型，与限流无关）
    r2 = client.post("/api/ghost", json={"key": "rapid", "text": "他收住了脚步。"})
    assert r2.status_code == 429
    payload = r2.json()
    assert "cooldown" in payload and payload["cooldown"] > 0
    assert "幽灵字" in payload["error"] or "过快" in payload["error"]


def test_ghost_route_llm_error_maps_to_502(client, monkeypatch):
    def boom(*a, **k):
        raise llm.LLMError("模型未配置")
    monkeypatch.setattr(llm, "chat", boom, raising=False)
    r = client.post("/api/ghost", json={"key": "bk1", "text": "他收住了脚步。"})
    assert r.status_code == 502
    assert "模型未配置" in r.json()["error"]


def _frames(body: str):
    out = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        event, data = "message", None
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[len("data:"):].strip())
                except Exception:
                    data = line[len("data:"):].strip()
        out.append((event, data))
    return out


def test_quickwrite_route_streams_sse_and_cleans_final_text(client, monkeypatch):
    def fake_stream(text, *, on_delta=None, **kw):
        for piece in ("夜色", "沉了下来。", "他数着脚步。"):
            if on_delta:
                on_delta(piece)
        return "夜色沉了下来。他数着脚步。"

    monkeypatch.setattr(ghost, "run_stream", fake_stream, raising=False)
    r = client.post("/api/quickwrite", json={"key": "bk1", "text": "巷子深处没有灯。"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    frames = _frames(r.text)
    events = [e for e, _ in frames]
    assert events[0] == "start" and events[-1] == "done"
    deltas = "".join(d["text"] for e, d in frames if e == "delta")
    assert deltas == "夜色沉了下来。他数着脚步。"
    done = frames[-1][1]
    assert done["ok"] is True and done["text"] == "夜色沉了下来。他数着脚步。"


def test_quickwrite_route_error_frame_on_failure(client, monkeypatch):
    def boom(*a, **k):
        raise llm.LLMError("流式连接中断")

    monkeypatch.setattr(ghost, "run_stream", boom, raising=False)
    r = client.post("/api/quickwrite", json={"key": "bk1", "text": "正文"})
    assert r.status_code == 200
    frames = _frames(r.text)
    assert frames[-1][0] == "done" and frames[-1][1]["ok"] is False
    assert any(e == "error" for e, _ in frames)


# ---------------------------------------------------------------- 敏感词词库路由

def test_sensitive_words_get_seeds_builtin_list(tmp_path, monkeypatch):
    path = tmp_path / "sensitive-words.txt"
    monkeypatch.setattr(webai, "SENSITIVE_PATH", str(path))
    app = FastAPI()
    app.include_router(webai.router)
    client = TestClient(app)
    r = client.get("/api/sensitive-words")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["count"] >= 10
    assert path.exists()                       # 缺文件自动播种内置基础词表
    assert path.read_text(encoding="utf-8").lstrip().startswith("#")


def test_sensitive_words_post_roundtrip_ignores_comments(tmp_path, monkeypatch):
    path = tmp_path / "sensitive-words.txt"
    monkeypatch.setattr(webai, "SENSITIVE_PATH", str(path))
    app = FastAPI()
    app.include_router(webai.router)
    client = TestClient(app)
    r = client.post("/api/sensitive-words", json={"words": ["甲词", "乙词", "甲词", ""]})
    assert r.status_code == 200
    assert r.json()["items"] == ["甲词", "乙词"]          # 去重保序
    text = path.read_text(encoding="utf-8")
    assert "甲词" in text and "乙词" in text
    # 整份文件写回：注释行与空行在读取侧被跳过
    r2 = client.post("/api/sensitive-words", json={"text": "# 注释\n\n丙词\n\n# 尾注\n丁词\n"})
    assert r2.status_code == 200
    r3 = client.get("/api/sensitive-words")
    assert r3.json()["items"] == ["丙词", "丁词"]


def test_sensitive_words_post_rejects_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(webai, "SENSITIVE_PATH", str(tmp_path / "sw.txt"))
    app = FastAPI()
    app.include_router(webai.router)
    client = TestClient(app)
    r = client.post("/api/sensitive-words", json={"text": "# 只有注释"})
    assert r.status_code == 400
