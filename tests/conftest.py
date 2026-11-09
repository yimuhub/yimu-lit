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
    # v4.26.0：测试进程禁孵总编辑看护线程（tick_once 仍可显式调用做断言）
    from bot import supervisor as _sv
    monkeypatch.setattr(_sv, "TEST_MODE", True, raising=False)


@pytest.fixture(autouse=True)
def _clean_shared_state():
    """v4.24.16：测试间共享全局状态统一清理——此前只有个别文件自己清（test_v230 的
    _RUNNING_BOOKS 随机红教训），其余文件共享 SESSIONS/PROJECT_INDEX/各运行标志注册表，
    同一 pytest 进程里前一个用例的状态会串到后一个（顺序依赖）。
    名字唯一（_clean_shared_state），不会被模块级同名 fixture 遮蔽。"""
    import app as _app
    from bot import writer as _writer
    from bot import livejob as _livejob
    regs = [
        (_app, "SESSIONS"), (_app, "PROJECT_INDEX"),
        (_writer, "_RUNNING_BOOKS"), (_writer, "_PARA_RW_PROGRESS"),
        # v4.33.3：v4.33.1 新增的两份全局状态也要登记，否则前一个用例遗留的
        # 「正在跑」会让 /api/status 的 running 断言串味（本地跑两遍才现形，
        # CI 上表现为莫名其妙的红）。
        (_writer, "_PIPELINE_COUNT"),
    ]
    for owner, name in regs:
        d = getattr(owner, name, None)
        if isinstance(d, dict):
            d.clear()
    _livejob.reset_for_tests()
    # app.py 的运行标志注册表（_PARALLEL_RUNNING/_REWRITING/_RELEASE_RUNNING/_ONESHOT_RUNNING/_PARA_RW_RUNNING）
    for name in ("_PARALLEL_RUNNING", "_REWRITING", "_RELEASE_RUNNING",
                 "_ONESHOT_RUNNING", "_PARA_RW_RUNNING"):
        d = getattr(_app, name, None)
        if isinstance(d, dict):
            d.clear()
    yield
    for owner, name in regs:
        d = getattr(owner, name, None)
        if isinstance(d, dict):
            d.clear()
    _livejob.reset_for_tests()
    for name in ("_PARALLEL_RUNNING", "_REWRITING", "_RELEASE_RUNNING",
                 "_ONESHOT_RUNNING", "_PARA_RW_RUNNING"):
        d = getattr(_app, name, None)
        if isinstance(d, dict):
            d.clear()
