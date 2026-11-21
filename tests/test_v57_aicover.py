"""v5.7.0 AI 生成封面回归（bot/webcover.py + providers.py 图像小节 + prompts/cover-art.md）。

覆盖：
1. 无图像供应商 → 400 + 中文引导「请先在设置→供应商配置图像模型」；
2. mock 生成（body mock=true / ?mock=1）→ 内置占位 PNG 走 weblib.save_cover 落盘
   cover.png → 旧封面（cover.jpg）清理同口径；
3. 提示词组装走 prompts_store 开放体系：system+user 两槽可被 data/prompts/cover-art.md
   用户层覆盖；内置 md 与代码默认逐字一致、frontmatter base == 当前指纹；
4. b64_json / url 双通道解析（url 兜底回捞下载）；
5. 坏 key（401/403）、429、超时、404、模型不存在 → 全中文报错；
6. 图像供应商配置端点：GET 打码（不回传明文 key）、POST 保存、api_key 留空保留旧值、
   base_url/size 校验中文报错；/ai/test 探活成功与鉴权失败文案。

所有落盘指到 tmp_path，不碰真实 projects/ 与 providers.json；不发真实网络请求
（httpx 层全部 monkeypatch）。LLM 由 conftest 全局 mock。
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot import providers, prompts_store, webcover, writer
from project_helpers import NOVEL_PLAN, mk_project

# ---------------------------------------------------------------- 工具 / fixtures


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _jpg() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"JFIF" + b"\x00" * 32


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class _FakeResp:
    """httpx.Response 形状的最小桩。"""

    def __init__(self, status_code=200, payload=None, text="", content=b""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.content = content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    """httpx.Client 形状的最小桩（providers.test_image_provider 用）。"""

    def __init__(self, responder):
        self._responder = responder
        self.last = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        self.last = {"url": url, "headers": headers or {}, "json": json}
        return self._responder(url, headers or {}, json)


def _configure_image_provider(**over):
    """给当前（已 monkeypatch 的）providers.json 写一份图像供应商配置。"""
    payload = {"base_url": "http://127.0.0.1:9/v1", "model": "fooocus-xl",
               "api_key": "sk-test-1234567890", "size": "1024x1536"}
    payload.update(over)
    return providers.save_image_provider(payload)


@pytest.fixture()
def prov_file(tmp_path, monkeypatch):
    """providers.json 指到临时文件（从空供应商列表开始，image_provider 未配置）。"""
    f = str(tmp_path / "providers.json")
    with open(f, "w", encoding="utf-8") as fh:
        json.dump({"version": 2, "default_provider": "", "providers": []}, fh)
    monkeypatch.setattr(providers, "PROVIDERS_FILE", f)
    return f


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    """一本可操作的作品（《长夜灯河》，题材悬疑）。"""
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    mk_project(tmp_path, "novel_a", NOVEL_PLAN,
               {"novelName": "长夜灯河", "status": "in_progress",
                "config": {"genre": "悬疑", "idea": "典当行里的万物流转"}, "meta": {}})
    return tmp_path


@pytest.fixture()
def client(lib, prov_file):
    """app.py 的挂载由主会话完成（本任务不许改 app.py），这里用独立 FastAPI 实例
    直接挂 webcover.router 测接口契约，与生产挂载（app.include_router）完全同一条路。"""
    app = FastAPI()
    app.include_router(webcover.router)
    return TestClient(app)


@pytest.fixture()
def clean_cover_prompts():
    """用户层提示词覆盖用完即删，避免串到其他用例。"""
    yield
    prompts_store.delete_user_md("cover-art")


# ---------------------------------------------------------------- 1. 无供应商 → 400 中文引导


def test_no_image_provider_400_chinese(client, lib):
    r = client.post("/api/library/cover/ai", json={"key": "novel_a"})
    assert r.status_code == 400
    msg = r.json()["error"]
    assert "设置" in msg and "图像模型" in msg, msg
    assert "供应商" in msg, msg


def test_missing_or_bad_key_rejected(client, lib):
    r = client.post("/api/library/cover/ai", json={})
    assert r.status_code == 400 and "key" in r.json()["error"]
    # mock 也一样要走作品存在校验（落盘链路复用 weblib）
    r2 = client.post("/api/library/cover/ai", json={"key": "novel_zzz", "mock": True})
    assert r2.status_code == 400 and "作品不存在" in r2.json()["error"]
    r3 = client.post("/api/library/cover/ai", json={"key": "../evil", "mock": True})
    assert r3.status_code == 400
    r4 = client.post("/api/library/cover/ai", json={"key": "   ", "mock": True})
    assert r4.status_code == 400 and "缺少作品 key" in r4.json()["error"]


# ---------------------------------------------------------------- 2. mock 生成 → 落盘 → 旧封面清理


def test_mock_generate_saves_cover_and_cleans_old(client, lib):
    pdir = os.path.join(str(lib), "novel_a")
    with open(os.path.join(pdir, "cover.jpg"), "wb") as f:
        f.write(_jpg())
    r = client.post("/api/library/cover/ai", json={"key": "novel_a", "mock": True})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["cover"] == "cover.png" and d["mock"] is True
    assert d["removed_old"] == 1                      # 旧 cover.jpg 已清理
    assert "/api/library/cover?key=" in d["url"]
    cover_path = os.path.join(pdir, "cover.png")
    with open(cover_path, "rb") as f:
        raw = f.read()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert sniff_ok(raw)
    covers = sorted(n for n in os.listdir(pdir) if n.startswith("cover."))
    assert covers == ["cover.png"]                    # 同目录最多一张封面（weblib 同口径）


def sniff_ok(data: bytes) -> bool:
    """复用 weblib 的文件头校验口径（import，不复制逻辑）。"""
    from bot.weblib import sniff_image
    return sniff_image(data) == "png"


def test_mock_generate_via_query_param(client, lib):
    r = client.post("/api/library/cover/ai?mock=1", json={"key": "novel_a"})
    assert r.status_code == 200 and r.json()["mock"] is True
    assert os.path.exists(os.path.join(str(lib), "novel_a", "cover.png"))


def test_mock_png_passes_weblib_validation():
    """内置占位 PNG 必须能通过 weblib 的文件头校验（否则 save_cover 会拒收）。"""
    from bot.weblib import sniff_image
    assert sniff_image(webcover.mock_cover_png()) == "png"


# ---------------------------------------------------------------- 3. 提示词组装走 prompts_store（槽可覆盖）


def _capture_generate(monkeypatch, store):
    """把 webcover.generate_image 换成捕获提示词的桩。"""

    def _fake(rt, prompt, timeout=webcover.IMAGE_TIMEOUT):
        store["rt"] = dict(rt)
        store["prompt"] = prompt
        return _png()

    monkeypatch.setattr(webcover, "generate_image", _fake)


def test_prompt_assembly_uses_prompts_store(lib, prov_file, monkeypatch):
    _configure_image_provider()
    store = {}
    _capture_generate(monkeypatch, store)
    r = client_post_generate({"key": "novel_a", "prompt": "雨夜当铺门口一盏暖灯", "style": "水墨"})
    assert r.status_code == 200, r.text
    p = store["prompt"]
    # system 槽（美术指导人设 + 禁文字铁则）
    assert "美术指导" in p and "不得出现任何文字" in p
    # user 槽变量填充：书名（仅作题材气质参考）/题材/风格/用户画面
    assert "《长夜灯河》" in p and "悬疑" in p and "水墨" in p and "雨夜当铺门口一盏暖灯" in p
    # 画面描述 ≤180 字上限（用户输入超长会被裁剪）
    r2 = client_post_generate({"key": "novel_a", "prompt": "夜" * 500})
    assert r2.status_code == 200
    scene = store["prompt"]
    assert ("夜" * 181) not in scene and ("夜" * 180) in scene


def client_post_generate(payload):
    """独立 FastAPI 实例同一条路（与 client fixture 同构）。"""
    app = FastAPI()
    app.include_router(webcover.router)
    c = TestClient(app)
    return c.post("/api/library/cover/ai", json=payload)


def test_prompt_slot_overridable_via_user_layer(lib, prov_file, monkeypatch, clean_cover_prompts):
    _configure_image_provider()
    md = (
        "---\nid: cover-art\nlabel: AI 封面\nbase: %s\n---\n\n"
        "## system\n覆盖系统槽：OVERRIDE-SYS 铁则不变：不得出现文字\n\n"
        "## user\n覆盖模板：OVERRIDE-TPL 题材={{题材}} 画面={{画面}} 结束\n"
    ) % prompts_store.fingerprint("cover-art")
    assert prompts_store.save_user_md("cover-art", md)["ok"] is True
    store = {}
    _capture_generate(monkeypatch, store)
    r = client_post_generate({"key": "novel_a", "style": "水墨"})
    assert r.status_code == 200
    p = store["prompt"]
    assert "OVERRIDE-SYS" in p and "OVERRIDE-TPL" in p
    assert "悬疑" in p                      # {{题材}} 仍在替换
    assert "美术指导" not in p               # 默认 system 槽已被用户层覆盖


def test_builtin_cover_art_md_matches_code_defaults():
    """prompts/cover-art.md 与代码默认值逐字一致；frontmatter base == 当前指纹。

    代码默认换代时此测试必红——提示重新生成 prompts/cover-art.md（指纹打进断言信息）。
    """
    parsed = prompts_store.parse_prompt_md(
        open(prompts_store.builtin_path("cover-art"), encoding="utf-8").read())
    assert parsed["errors"] == [], parsed["errors"]
    meta = prompts_store.group_meta("cover-art")
    assert meta and set(meta["slots"]) == {"system", "user"}
    for name, s in meta["slots"].items():
        ent = parsed["slots"].get(name)
        assert ent is not None, f"cover-art.{name} 缺槽"
        assert ent["text"] == s["default"], f"cover-art.{name} 内置 md 与代码默认不一致"
    assert parsed["base"] == prompts_store.fingerprint("cover-art"), (
        f"cover-art frontmatter base 过期：应为 {prompts_store.fingerprint('cover-art')}")


# ---------------------------------------------------------------- 4. b64 / url 双通道


def test_b64_json_channel(lib, prov_file, monkeypatch):
    _configure_image_provider()
    seen = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        seen.update({"url": url, "json": json, "auth": headers.get("Authorization", "")})
        return _FakeResp(200, {"data": [{"b64_json": _b64(_png())}]})

    monkeypatch.setattr(webcover.httpx, "post", _fake_post)
    r = client_post_generate({"key": "novel_a"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["cover"] == "cover.png" and d["mock"] is False
    # 请求形状：images/generations + Bearer key + b64_json + 默认竖版尺寸
    assert seen["url"].endswith("/images/generations")
    assert seen["auth"].startswith("Bearer sk-test-")
    assert seen["json"]["response_format"] == "b64_json"
    assert seen["json"]["model"] == "fooocus-xl"
    assert seen["json"]["size"] == "1024x1536"
    with open(os.path.join(str(lib), "novel_a", "cover.png"), "rb") as f:
        assert f.read() == _png()


def test_url_channel_fallback_download(lib, prov_file, monkeypatch):
    """响应只给 url → 兜底回捞下载 bytes 再落盘。"""
    _configure_image_provider()
    monkeypatch.setattr(
        webcover.httpx, "post",
        lambda *a, **k: _FakeResp(200, {"data": [{"url": "http://img.test/x.png"}]}))
    monkeypatch.setattr(
        webcover.httpx, "get",
        lambda url, timeout=None, follow_redirects=False: _FakeResp(200, content=_png()))
    r = client_post_generate({"key": "novel_a"})
    assert r.status_code == 200 and r.json()["cover"] == "cover.png"
    with open(os.path.join(str(lib), "novel_a", "cover.png"), "rb") as f:
        assert f.read() == _png()


def test_url_download_failure_chinese(lib, prov_file, monkeypatch):
    _configure_image_provider()
    monkeypatch.setattr(
        webcover.httpx, "post",
        lambda *a, **k: _FakeResp(200, {"data": [{"url": "http://img.test/x.png"}]}))
    monkeypatch.setattr(
        webcover.httpx, "get",
        lambda url, timeout=None, follow_redirects=False: _FakeResp(500, text="boom"))
    r = client_post_generate({"key": "novel_a"})
    assert r.status_code == 400 and "下载" in r.json()["error"]


# ---------------------------------------------------------------- 5. 错误码全中文


@pytest.mark.parametrize("builder,needle", [
    (lambda: _FakeResp(401, text='{"error":"bad key"}'), "鉴权失败"),
    (lambda: _FakeResp(403, text='forbidden'), "鉴权失败"),
    (lambda: _FakeResp(429, text="rate limited"), "频繁"),
    (lambda: _FakeResp(404, text="not found"), "404"),
    (lambda: _FakeResp(400, text='{"error":"model fooocus-xl not found"}'), "模型不存在"),
    (lambda: _FakeResp(500, text="boom"), "图像接口返回 500"),
])
def test_api_error_messages_chinese(lib, prov_file, monkeypatch, builder, needle):
    _configure_image_provider()
    monkeypatch.setattr(webcover.httpx, "post", lambda *a, **k: builder())
    r = client_post_generate({"key": "novel_a"})
    assert r.status_code == 400
    assert needle in r.json()["error"], r.json()["error"]


def test_timeout_error_chinese(lib, prov_file, monkeypatch):
    _configure_image_provider()

    def _raise(*a, **k):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(webcover.httpx, "post", _raise)
    r = client_post_generate({"key": "novel_a"})
    assert r.status_code == 400 and "超时" in r.json()["error"]


def test_connection_error_chinese(lib, prov_file, monkeypatch):
    _configure_image_provider()

    def _raise(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(webcover.httpx, "post", _raise)
    r = client_post_generate({"key": "novel_a"})
    assert r.status_code == 400 and "无法连接" in r.json()["error"]


# ---------------------------------------------------------------- 6. 配置端点（打码/保留旧 key/校验）


def test_config_roundtrip_masks_key(client, prov_file):
    d0 = client.get("/api/library/cover/ai/config").json()
    assert d0["ok"] is True and d0["configured"] is False and d0["has_key"] is False
    assert "api_key" not in d0

    r = client.post("/api/library/cover/ai/config",
                    json={"base_url": "http://127.0.0.1:9/v1", "model": "dall-e-3",
                          "api_key": "sk-secret-1234567890", "size": "768x1152"})
    assert r.status_code == 200
    d1 = r.json()
    assert d1["ok"] is True and d1["configured"] is True and d1["has_key"] is True
    assert "api_key" not in d1 and "secret" not in json.dumps(d1)
    assert d1["size"] == "768x1152"

    d2 = client.get("/api/library/cover/ai/config").json()
    assert d2["configured"] is True and d2["base_url"] == "http://127.0.0.1:9/v1"
    assert d2["key_display"].startswith("已设置")

    # api_key 留空 = 保留旧值（与文本供应商 v4.19.1 同口径）
    client.post("/api/library/cover/ai/config", json={"api_key": ""})
    d3 = client.get("/api/library/cover/ai/config").json()
    assert d3["has_key"] is True

    # 落盘形状：providers.json 顶层独立小节 image_provider
    ondisk = json.load(open(prov_file, encoding="utf-8"))
    assert ondisk["image_provider"]["model"] == "dall-e-3"
    assert ondisk["image_provider"]["api_key"] == "sk-secret-1234567890"
    assert ondisk["image_provider"]["size"] == "768x1152"


def test_config_validation_chinese(client, prov_file):
    r = client.post("/api/library/cover/ai/config",
                    json={"base_url": "ftp://x", "model": "m"})
    assert r.status_code == 400 and "base_url" in r.json()["error"]
    r2 = client.post("/api/library/cover/ai/config", json={"model": "bad model name!"})
    assert r2.status_code == 400 and "模型名" in r2.json()["error"]
    r3 = client.post("/api/library/cover/ai/config",
                     json={"base_url": "http://x/v1", "model": "m", "size": "1024*1536"})
    assert r3.status_code == 400 and "尺寸" in r3.json()["error"]


def test_clearing_config_makes_it_unconfigured(client, prov_file):
    _configure_image_provider()
    assert client.get("/api/library/cover/ai/config").json()["configured"] is True
    client.post("/api/library/cover/ai/config", json={"base_url": "", "model": ""})
    d = client.get("/api/library/cover/ai/config").json()
    assert d["configured"] is False


# ---------------------------------------------------------------- 7. 探活端点


def test_probe_endpoint_unconfigured(client):
    r = client.post("/api/library/cover/ai/test", json={})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False and "尚未配置" in d["detail"]


def test_probe_endpoint_success_and_auth_failure(client, prov_file, monkeypatch):
    _configure_image_provider()

    def _ok(url, headers, json):
        assert json["size"] == "512x512" and json["response_format"] == "b64_json"
        assert url.endswith("/images/generations")
        return _FakeResp(200, {"data": [{"b64_json": _b64(_png())}]})

    monkeypatch.setattr(providers.httpx, "Client", lambda **kw: _FakeClient(_ok))
    d = client.post("/api/library/cover/ai/test", json={}).json()
    assert d["ok"] is True and "已连通" in d["detail"]

    monkeypatch.setattr(
        providers.httpx, "Client",
        lambda **kw: _FakeClient(lambda *a: _FakeResp(401, text="bad key")))
    d2 = client.post("/api/library/cover/ai/test", json={}).json()
    assert d2["ok"] is False and "鉴权失败" in d2["detail"]


# ---------------------------------------------------------------- 8. 路由契约（不遮既有书架路由）


def test_webcover_routes_are_cover_ai_only():
    """webcover 只新增 cover/ai 两段路径——不会被 weblib 的 /api/library/cover
    或 app.py 的 /api/library/{key} 单段通配遮住。"""
    routes = sorted({r.path for r in webcover.router.routes})
    assert routes == ["/api/library/cover/ai", "/api/library/cover/ai/config",
                      "/api/library/cover/ai/test"]
