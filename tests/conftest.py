"""tests/conftest.py —— v0.7 R3 大厂工程化：隔离外部 LLM 边界。

所有测试不烧真 LLM：monkeypatch bot.llm 的 chat / chat_json 为确定性 stub。
（rewrite-plan 等端点内部会触发 five_role_review → chat_json；mock 后返回 {} 即可走通。）
同时关闭本地一次性令牌鉴权：TestClient 不带浏览器 Cookie，否则所有 /api/* 都会 401。
"""
import os

os.environ.setdefault("SHENBI_AUTH", "0")

import pytest
from bot import llm


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "", raising=False)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {}, raising=False)

    def _chat_stream_stub(messages, *a, **k):
        # chat_stream 缺省 mock：委托给当前生效的 llm.chat（测试可自行覆盖 chat），整段回调一次，绝不连网。
        kw = {x: v for x, v in k.items() if x != "on_delta"}
        text = llm.chat(messages, *a, **kw)
        cb = k.get("on_delta")
        if callable(cb) and text:
            cb(text)
        return text

    monkeypatch.setattr(llm, "chat_stream", _chat_stream_stub, raising=False)
