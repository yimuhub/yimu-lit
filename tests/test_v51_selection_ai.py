"""v5.1.0 划词 AI（bot/selection_ai.py + POST /api/chapter/selection-ai）回归。

覆盖：六动作提示词与温度映射、上下文 220 字窗口、结果清洗（思考标签/围栏/
客套语）、SSE 流式路由帧结构（start → delta* → done）、未知动作 400。
所有用例不连网：llm.chat / chat_stream 由 conftest 兜底 stub，个别用例自行覆盖。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import llm, selection_ai

SEL = "雨停之前，他把伞收了起来。"


# ---------------------------------------------------------------- 提示词与温度

def test_six_actions_registered_with_spec_temperatures():
    temps = {k: v["temperature"] for k, v in selection_ai.ACTIONS.items()}
    # 逐动作温度：纠错 0.1 / 润色 0.7 / 扩写 0.85 / 去味 0.75 / 口吻 0.7 / 自定义 0.7
    assert temps == {"fix": 0.1, "polish": 0.7, "expand": 0.85,
                     "deslop": 0.75, "tone": 0.7, "custom": 0.7}
    labels = {k: v["label"] for k, v in selection_ai.ACTIONS.items()}
    assert set(labels.values()) == {"纠错", "润色", "扩写", "去 AI 味", "按人物口吻", "自定义指令"}


def test_polish_prompt_does_not_expand_plot():
    p = selection_ai.system_prompt("polish")
    assert "不替用户扩剧情" in p or "不得新增情节" in p
    assert "润色" in p


def test_fix_prompt_only_typos_and_punctuation():
    p = selection_ai.system_prompt("fix")
    assert "错别字" in p and "标点" in p
    assert "不动剧情" in p


def test_deslop_prompt_carries_slop_list_in_repo_style():
    p = selection_ai.system_prompt("deslop")
    # 去味提示词带高频套话清单（自建词表风格，与 slop_checker 同源精神）
    assert "套话" in p and ("眼中闪过" in p or "嘴角勾起" in p or "总而言之" in p)


def test_build_messages_clamps_context_window_to_220():
    msgs = selection_ai.build_messages("polish", SEL,
                                       before="前" * 600, after="后" * 600,
                                       title="第1章 试探", summary="概要" * 300)
    user = msgs[1]["content"]
    assert "【选区前文】" in user and "【选区后文】" in user
    before_part = user.split("【选区前文】…")[1].split("\n")[0]
    assert len(before_part) <= 220
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    assert "【章题】第1章 试探" in user
    # 概要钳制在 400 字内
    summary_part = user.split("【本章概要】")[1].split("\n")[0]
    assert len(summary_part) <= 400


def test_build_messages_rejects_unknown_action_and_empty_selection():
    with pytest.raises(selection_ai.SelectionAIError):
        selection_ai.build_messages("nope", SEL)
    with pytest.raises(selection_ai.SelectionAIError):
        selection_ai.build_messages("polish", "   ")
    with pytest.raises(selection_ai.SelectionAIError):
        selection_ai.build_messages("polish", "字" * 5000)   # 超过 4000 上限


def test_persona_block_falls_back_to_auto_detect():
    p1 = selection_ai.system_prompt("tone", persona="老刑警，说话短促")
    assert "老刑警" in p1
    p2 = selection_ai.system_prompt("tone", persona="")
    assert "自动识别" in p2 or "判断不出来就保持原语气" in p2


# ---------------------------------------------------------------- 结果清洗

def test_clean_result_strips_think_fence_and_ack():
    assert selection_ai.clean_result("<think>推理</think>门后是雨。") == "门后是雨。"
    assert selection_ai.clean_result("```markdown\n门后是雨。\n```") == "门后是雨。"
    assert selection_ai.clean_result("好的，以下是润色后的文字：\n门后是雨。") == "门后是雨。"
    assert selection_ai.clean_result("「门后是雨。」") == "门后是雨。"
    # 元信息行剥离
    assert "修改说明" not in selection_ai.clean_result("门后是雨。\n修改说明：调整了语序")


def test_run_uses_action_temperature_and_cleans_output(monkeypatch):
    seen = {}

    def fake_chat(messages, temperature=None, **kw):
        seen["temperature"] = temperature
        seen["messages"] = messages
        return "好的，以下是改后的文字：\n门后的雨停了。"

    monkeypatch.setattr(llm, "chat", fake_chat, raising=False)
    out = selection_ai.run("polish", SEL, before="前文", after="后文", key="bk", chapter_no=3)
    assert out == "门后的雨停了。"
    assert seen["temperature"] == 0.7


def test_run_stream_streams_deltas_and_returns_full_text(monkeypatch):
    def fake_stream(messages, on_delta=None, temperature=None, **kw):
        for piece in ("门后", "的雨停了。"):
            if on_delta:
                on_delta(piece)
        return "门后的雨停了。"

    monkeypatch.setattr(llm, "chat_stream", fake_stream, raising=False)
    got = []
    out = selection_ai.run_stream("expand", SEL, on_delta=got.append)
    assert out == "门后的雨停了。"
    assert got == ["门后", "的雨停了。"]


# ---------------------------------------------------------------- 路由（SSE）

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


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(appmod, "_get_project", lambda k: None)
    return TestClient(appmod.app)


def test_selection_ai_route_streams_sse(monkeypatch, client):
    def fake_stream(messages, on_delta=None, temperature=None, **kw):
        for piece in ("改后", "第一句。"):
            if on_delta:
                on_delta(piece)
        return "改后第一句。"

    monkeypatch.setattr(llm, "chat_stream", fake_stream, raising=False)
    r = client.post("/api/chapter/selection-ai", json={
        "action": "polish", "selection": SEL, "before": "前", "after": "后",
        "title": "第1章", "summary": "概要", "key": "bk", "n": 3,
    })
    assert r.status_code == 200
    frames = _frames(r.text)
    events = [e for e, _ in frames]
    assert events[0] == "start" and events[-1] == "done"
    deltas = "".join(d["text"] for e, d in frames if e == "delta")
    assert deltas == "改后第一句。"
    done = frames[-1][1]
    assert done["ok"] is True and done["text"] == "改后第一句。"


def test_selection_ai_route_error_frame_on_llm_failure(monkeypatch, client):
    def boom(*a, **k):
        raise llm.LLMError("模型未配置")

    monkeypatch.setattr(llm, "chat_stream", boom, raising=False)
    r = client.post("/api/chapter/selection-ai",
                    json={"action": "fix", "selection": SEL})
    assert r.status_code == 200
    frames = _frames(r.text)
    assert frames[-1][0] == "done" and frames[-1][1]["ok"] is False
    assert any(e == "error" for e, _ in frames)


def test_selection_ai_route_rejects_unknown_action_and_empty(client):
    r = client.post("/api/chapter/selection-ai", json={"action": "wat", "selection": SEL})
    assert r.status_code == 400
    r = client.post("/api/chapter/selection-ai", json={"action": "polish", "selection": "  "})
    assert r.status_code == 400


def test_selection_ai_actions_meta_endpoint(client):
    r = client.get("/api/selection-ai/actions")
    assert r.status_code == 200
    actions = r.json()["actions"]
    assert set(actions) == set(selection_ai.ACTIONS)
    assert actions["fix"]["temperature"] == 0.1
