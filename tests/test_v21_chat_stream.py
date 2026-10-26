"""v2.1 F1：/api/chat/stream 等待期心跳 + 未配置模型时的友好兜底。

要点：
- 心跳：chat_func 阻塞期间每 ~2s 补一帧 status，防止前端看着像卡死。
- 兜底：没有任何可用供应商时，消息照收，回固定文案，且**不调用** chat_func。
- 不吞真错：供应商在但调用失败，仍是 error 帧 + done.ok=false。

路由由 `register_v2_routes(app, _get_project, chat)` 在 import 时注册，
`chat` 已被闭包捕获成 chat_func，所以改 app.chat 无效，只能改闭包里的那个单元格。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import providers, security

FALLBACK = "您的消息已收到。由于未配置大模型，请配置模型。"
URL = "/api/chat/stream"

client = TestClient(appmod.app)


def _frames(body: str):
    """把 SSE 正文拆成 [(event, data)]；帧以空行分隔，event/data 各占一行。"""
    out = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        event, data = "message", None
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                raw = line[len("data:"):].strip()
                try:
                    data = json.loads(raw)
                except Exception:
                    data = raw
        out.append((event, data))
    return out


def _stream_endpoint():
    for r in appmod.app.routes:
        if getattr(r, "path", "") == URL and "POST" in getattr(r, "methods", ()):
            return r.endpoint
    raise AssertionError("流式路由未注册")


def _chat_cell():
    ep = _stream_endpoint()
    cells = dict(zip(ep.__code__.co_freevars, ep.__closure__ or (), strict=False))
    assert "chat_func" in cells, f"闭包里没有 chat_func：{list(cells)}"
    return cells["chat_func"]


@pytest.fixture
def patch_chat():
    """替换路由闭包捕获的 chat_func（并负责还原）。"""
    cell = _chat_cell()
    old = cell.cell_contents
    try:
        yield lambda fn: setattr(cell, "cell_contents", fn)
    finally:
        cell.cell_contents = old


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)


def test_heartbeat_while_waiting(patch_chat, monkeypatch):
    monkeypatch.setattr(providers, "usable", lambda: True)
    seen = []

    def fake_chat(sid, message):
        seen.append((sid, message))
        time.sleep(2.4)          # 跨过一整个心跳周期，但整轮仍远低于 15s 预算
        return "心跳测试正文"

    patch_chat(fake_chat)
    r = client.post(URL, json={"message": "写个开头", "session_id": "s-hb"})
    assert r.status_code == 200
    frames = _frames(r.text)
    events = [e for e, _ in frames]

    assert events[0] == "start"
    status = [d for e, d in frames if e == "status"]
    assert len(status) >= 2, f"status 帧不足：{status}"
    assert status[0]["text"] == "正在理解你的需求…"
    assert any("模型正在思考中（已等待" in (d.get("text") or "") for d in status[1:]), status

    assert events[-1] == "done"
    done = frames[-1][1]
    assert done["ok"] is True
    assert done["text"] == "心跳测试正文"
    assert done["session_id"] == "s-hb" and "elapsed" in done
    assert "".join(d["text"] for e, d in frames if e == "delta") == "心跳测试正文"
    assert seen == [("s-hb", "写个开头")]


def test_no_provider_returns_friendly_fallback(patch_chat, monkeypatch):
    monkeypatch.setattr(providers, "usable", lambda: False)
    called = []
    patch_chat(lambda sid, message: called.append((sid, message)) or "不该出现")

    r = client.post(URL, json={"message": "你好", "session_id": "s-fb"})
    assert r.status_code == 200
    frames = _frames(r.text)
    events = [e for e, _ in frames]

    assert events == ["start", "status", "delta", "done"]
    assert frames[0][1]["session_id"] == "s-fb"
    assert frames[1][1] == {"stage": "thinking", "text": "正在检查模型配置…"}
    assert [d["text"] for e, d in frames if e == "delta"] == [FALLBACK]
    done = frames[-1][1]
    assert done["ok"] is True and done["fallback"] is True
    assert done["text"] == FALLBACK and done["session_id"] == "s-fb" and "elapsed" in done
    assert called == [], "无可用供应商时决不能调用 chat_func"


def test_provider_present_error_is_not_masked(patch_chat, monkeypatch):
    monkeypatch.setattr(providers, "usable", lambda: True)

    def boom(sid, message):
        raise RuntimeError("供应商 401 鉴权失败")

    patch_chat(boom)
    r = client.post(URL, json={"message": "你好", "session_id": "s-err"})
    frames = _frames(r.text)
    err = [d for e, d in frames if e == "error"]
    assert err and "供应商 401 鉴权失败" in err[0]["error"]
    done = frames[-1][1]
    assert done["ok"] is False
    assert not [d for e, d in frames if e == "delta"]
    assert FALLBACK not in r.text


# ---------------------------------------------------------------- 旧版非流式入口


def test_legacy_chat_says_missing_model_instead_of_generic_failure(monkeypatch):
    """旧版 /api/chat 也要说同一句兜底，而不是「⚠️ 理解主题失败（…）」+ 500。"""

    def boom(*a, **k):
        raise providers.ProviderError("尚未绑定任何可用的模型供应商，请先绑定一个供应商")

    monkeypatch.setattr(appmod.workflow, "extract_config", boom)
    r = client.post("/api/chat", json={"session_id": "s-legacy-nomodel", "message": "写一部悬疑小说"})
    assert r.status_code == 200
    reply = r.json()["reply"]
    assert reply.startswith(FALLBACK)
    assert "理解主题失败" not in reply


def test_no_model_copy_is_shared_by_both_entries():
    """两处文案可以各自加指引，但首句必须一致，否则用户看到两种说法。"""
    assert appmod._NO_MODEL_REPLY.startswith(FALLBACK)

