"""tests/conftest.py —— v0.7 R3 大厂工程化：隔离外部 LLM 边界。

所有测试不烧真 LLM：monkeypatch bot.llm 的 chat / chat_json 为确定性 stub。
（rewrite-plan 等端点内部会触发 five_role_review → chat_json；mock 后返回 {} 即可走通。）
"""
import pytest
from bot import llm


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "", raising=False)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {}, raising=False)
