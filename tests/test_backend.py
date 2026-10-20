"""v0.7 R3 大厂工程化：后端回归测试（TestClient，不烧 LLM）。

覆盖：路由注册、非 LLM 读端点结构（真实契约：/api/library、/api/quality、
/api/rewrite-plan、/api/book-live、/api/studio-overview、/api/one-shot）、LLM 缓存原语。
跑法：cd novel-bot && .venv/bin/python -m pytest tests/ -q
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from bot import llm, version

import app as appmod
client = TestClient(appmod.app)


def _first_key():
    d = client.get("/api/library").json()
    items = d.get("items") or []
    return items[0]["key"] if items else None


# ---------- 路由 / 版本 ----------
def test_version_v07():
    assert version.VE.startswith("0.7")


def test_v07_routes_registered():
    paths = {getattr(r, "path", "") for r in appmod.app.routes}
    for p in ["/api/one-shot", "/api/studio-overview", "/api/book-live",
              "/api/rewrite-progress", "/api/rewrite-cancel", "/api/blueprint",
              "/api/library", "/api/quality"]:
        assert p in paths, f"缺路由 {p}"


def test_version_endpoint():
    v = client.get("/api/version").json()
    assert v.get("version", "").startswith("0.7")


# ---------- 真实读端点结构 ----------
def test_library_shape():
    d = client.get("/api/library").json()
    assert "items" in d and isinstance(d["items"], list)
    if d["items"]:
        b0 = d["items"][0]
        for f in ["key", "name", "state"]:
            assert f in b0, f"library item 缺字段 {f}"


def test_quality_shape():
    key = _first_key()
    assert key, "无书可测"
    d = client.get("/api/quality", params={"key": key}).json()
    assert d.get("key") == key and "text" in d


def test_studio_overview_shape():
    so = client.get("/api/studio-overview").json()
    assert "books" in so and "active_count" in so
    li = client.get("/api/library").json().get("items", [])
    assert len(so["books"]) == len(li)
    b0 = so["books"][0]
    for f in ["key", "name", "state", "done", "total", "quality", "archived", "running"]:
        assert f in b0, f"studio-overview book 缺字段 {f}"


def test_book_live_shape():
    key = _first_key()
    d = client.get("/api/book-live", params={"key": key}).json()
    assert d.get("key") == key and "phase" in d and "progress" in d and "live_log" in d


def test_book_live_bad_key():
    assert client.get("/api/book-live", params={"key": "no-such-book-xyz"}).status_code == 404


def test_rewrite_plan_shape():
    key = _first_key()
    d = client.post("/api/rewrite-plan", json={"key": key}).json()
    for f in ["before_total", "pass_line", "pass", "weak_dims", "global_directions", "chapters"]:
        assert f in d, f"rewrite-plan 缺字段 {f}"


def test_rewrite_progress_shape():
    key = _first_key()
    d = client.get("/api/rewrite-progress", params={"key": key}).json()
    for f in ["running", "total", "done", "cancelled", "per_chapter", "result"]:
        assert f in d, f"rewrite-progress 缺字段 {f}"


def test_one_shot_bad_key():
    assert client.post("/api/one-shot", json={"key": "no-such-book-xyz"}).json()["ok"] is False


# ---------- LLM 缓存原语（纯函数，不烧 LLM） ----------
def test_llm_cache_roundtrip():
    k = llm._cache_key("MODEL", 0.0, True, [{"role": "user", "content": "hello"}])
    assert k and len(k) == 40
    llm._cache_set(k, "cached")
    assert llm._cache_get(k) == "cached"
    assert llm._cache_get("nonexistent-key") is None


def test_llm_cache_enabled_toggle():
    assert llm._cache_enabled(None, False) is False                       # 显式关
    assert llm._cache_enabled(None, True) is True                          # 显式开
    assert llm._cache_enabled({"defaults": {"llm_cache": True}}, None) is True      # cfg 显式开
    assert llm._cache_enabled({"defaults": {"llm_cache": False}}, None) is False    # cfg 显式关（覆盖全局）
    # 每书 cfg 无该键（defaults 空/缺省）→ 回落全局默认（此处全局=True，config.json 已开）
    assert llm._cache_enabled({"defaults": {}}, None) is bool(llm._GLOBAL_CACHE_DEFAULT)
