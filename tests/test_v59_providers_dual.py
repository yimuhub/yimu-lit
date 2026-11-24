"""v5.9.0 模型供应商文本/图像双区回归（bot/providers.py kind 体系 + webcover.py 新结构）。

覆盖：
1. kind 字段补全迁移：旧 providers.json（无 kind）→ load_data 一次性补 "text"，幂等；
2. v5.7.0 顶层 image_provider 小节自动迁移：→ 一条 kind=image 条目（保留
   base_url/api_key/model→default_model/size），设 default_image_provider，旧小节保留
   只读不删（回滚安全），幂等（二次 load 不重复迁移）；空小节不迁移但置标记；
3. resolve 文本绝不落 image 条目：只有 image 条目 → ProviderError；text+image 共存 →
   只解析文本；cfg 显式 _provider_id 指到 image 条目 → 回落文本默认；
4. image_resolve 双默认链：default_image_provider → 第一条 active image → None；
5. 双默认槽互不干扰：set_default 按 kind 归槽；删除/停用默认条目时各自回落同类；
6. add_provider kind=image（size 校验、首个图像条目自动设默认）、update size、
   set_global_model 拒绝图像条目、list_providers 带 default_image_provider；
7. webcover 新结构读写：GET /config 返回兼容字段+图像条目列表；POST 保存到指定条目 /
   新建 / 清除（兼容 v5.7.0 字段口径）；/ai/test 按条目探活；
8. AI 封面生成端到端：mock generate_image → 走 kind=image 条目生成落盘。

所有落盘指到 tmp_path，不碰真实 providers.json / projects/；不发真实网络请求。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot import providers, webcover, writer
from project_helpers import NOVEL_PLAN, mk_project

# ---------------------------------------------------------------- 工具 / fixtures


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _write_providers(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


def _read_providers(path):
    return json.load(open(path, encoding="utf-8"))


def _text_entry(pid="prov-a", model="m-1", key="sk-key-abcdef123456", active=True):
    return {"id": pid, "name": "文本供应商", "preset": False, "active": active,
            "base_url": "https://api.example.com/v1", "api_key": key,
            "models": [model], "default_model": model, "temperature": 0.85,
            "max_tokens": 12000, "timeout": 900}


@pytest.fixture()
def prov_file(tmp_path, monkeypatch):
    """providers.json 指到临时文件（默认空供应商列表）。"""
    f = str(tmp_path / "providers.json")
    _write_providers(f, {"version": 2, "default_provider": "", "providers": []})
    monkeypatch.setattr(providers, "PROVIDERS_FILE", f)
    return f


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    mk_project(tmp_path, "novel_a", NOVEL_PLAN,
               {"novelName": "长夜灯河", "status": "in_progress",
                "config": {"genre": "悬疑", "idea": "典当行里的万物流转"}, "meta": {}})
    return tmp_path


@pytest.fixture()
def client(lib, prov_file):
    app = FastAPI()
    app.include_router(webcover.router)
    return TestClient(app)


# ---------------------------------------------------------------- 1. kind 补全迁移


def test_kind_backfill_migration(prov_file):
    _write_providers(prov_file, {"version": 2, "default_provider": "prov-a",
                                 "providers": [_text_entry(), _text_entry("prov-b")]})
    data = providers.load_data()
    assert [p["kind"] for p in data["providers"]] == ["text", "text"]
    ondisk = _read_providers(prov_file)
    assert all(p["kind"] == "text" for p in ondisk["providers"])
    # 幂等：再读一次不产生变更（kind 不变、无重复条目）
    data2 = providers.load_data()
    assert [p["kind"] for p in data2["providers"]] == ["text", "text"]
    assert len(data2["providers"]) == 2


def test_kind_backfill_preserves_keys_and_defaults(prov_file):
    """kind 迁移绝不动密钥/默认模型（v4.19.1 口径延续）。"""
    _write_providers(prov_file, {"version": 2, "default_provider": "prov-a",
                                 "providers": [_text_entry()]})
    providers.load_data()
    ondisk = _read_providers(prov_file)
    assert ondisk["providers"][0]["api_key"] == "sk-key-abcdef123456"
    assert ondisk["providers"][0]["default_model"] == "m-1"
    assert ondisk["default_provider"] == "prov-a"


# ---------------------------------------------------------------- 2. 旧 image_provider 小节迁移


def test_legacy_image_provider_migrated_once(prov_file):
    legacy = {"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-img-1234567890",
              "model": "fooocus-xl", "size": "768x1152"}
    _write_providers(prov_file, {"version": 2, "default_provider": "", "providers": [],
                                 "image_provider": legacy})
    data = providers.load_data()
    imgs = [p for p in data["providers"] if p.get("kind") == "image"]
    assert len(imgs) == 1
    e = imgs[0]
    assert e["base_url"] == "http://127.0.0.1:9/v1"
    assert e["api_key"] == "sk-img-1234567890"
    assert e["default_model"] == "fooocus-xl"
    assert e["size"] == "768x1152"
    assert data["default_image_provider"] == e["id"]
    # 旧小节保留只读不删（回滚安全）
    ondisk = _read_providers(prov_file)
    assert ondisk["image_provider"] == legacy
    assert ondisk["image_provider_migrated"] is True
    # 幂等：二次 load 不再迁出第二条
    data2 = providers.load_data()
    assert len([p for p in data2["providers"] if p.get("kind") == "image"]) == 1


def test_legacy_image_provider_empty_section_only_marks(prov_file):
    _write_providers(prov_file, {"version": 2, "default_provider": "",
                                 "providers": [], "image_provider": {}})
    data = providers.load_data()
    assert [p for p in data["providers"] if p.get("kind") == "image"] == []
    assert data["image_provider_migrated"] is True
    data2 = providers.load_data()
    assert data2["image_provider_migrated"] is True


def test_legacy_image_provider_bad_fields_never_crash(prov_file):
    _write_providers(prov_file, {"version": 2, "default_provider": "", "providers": [],
                                 "image_provider": {"base_url": "ftp://bad", "model": "坏模型名!",
                                                    "api_key": "sk-x"}})
    data = providers.load_data()
    e = next(p for p in data["providers"] if p.get("kind") == "image")
    assert e["base_url"] == ""          # 坏地址置空，让用户在新 UI 重填，绝不抛错
    assert e["default_model"] == ""
    assert e["models"] == []            # 非法旧模型不得留在清单里供 _image_model_of 回落


def test_existing_image_models_are_sanitized_on_load(prov_file):
    entry = _text_entry("img-old", "img-good")
    entry.update({"kind": "image", "models": ["img-good", "坏模型!", ""],
                  "default_model": "坏模型!"})
    _write_providers(prov_file, {"version": 2, "default_provider": "", "providers": [entry]})
    data = providers.load_data()
    img = data["providers"][0]
    assert img["models"] == ["img-good"]
    assert img["default_model"] == ""
    assert providers._image_model_of(img) == "img-good"
    ondisk = _read_providers(prov_file)["providers"][0]
    assert ondisk["models"] == ["img-good"] and ondisk["default_model"] == ""


@pytest.mark.parametrize("raw_models", ["img-good", 7, {"img-good": True}, None])
def test_existing_image_models_non_list_are_sanitized(prov_file, raw_models):
    entry = _text_entry("img-old", "img-good")
    entry.update({"kind": "image", "models": raw_models, "default_model": "img-good"})
    _write_providers(prov_file, {"version": 2, "default_provider": "", "providers": [entry]})
    data = providers.load_data()
    img = data["providers"][0]
    assert img["models"] == (["img-good"] if isinstance(raw_models, str) else [])
    assert providers._image_model_of(img) == (img["default_model"] or (img["models"][0] if img["models"] else ""))
    ondisk = _read_providers(prov_file)["providers"][0]
    assert ondisk["models"] == img["models"]


def test_migrated_image_provider_drives_cover_runtime(prov_file):
    """迁移出的图像条目直接可被图像链路消费（image_resolve/runtime）。"""
    _write_providers(prov_file, {"version": 2, "default_provider": "", "providers": [],
                                 "image_provider": {"base_url": "http://127.0.0.1:9/v1",
                                                    "api_key": "sk-img-1234567890",
                                                    "model": "fooocus-xl", "size": "768x1152"}})
    rt = providers.image_provider_runtime()
    assert rt["configured"] is True
    assert rt["base_url"] == "http://127.0.0.1:9/v1"
    assert rt["model"] == "fooocus-xl"
    assert rt["size"] == "768x1152"
    assert rt["api_key"] == "sk-img-1234567890"


# ---------------------------------------------------------------- 3. resolve 文本绝不落 image


def test_resolve_never_lands_on_image_entry(prov_file):
    providers.add_provider({"kind": "image", "name": "图像供应商",
                            "base_url": "http://127.0.0.1:9/v1", "models": ["img-xl"],
                            "api_key": "sk-img"})
    with pytest.raises(providers.ProviderError):
        providers.resolve()                 # 只有图像条目：文本链路必须给引导而非乱落


def test_resolve_text_and_image_coexist(prov_file):
    img = providers.add_provider({"kind": "image", "name": "图像供应商",
                                  "base_url": "http://127.0.0.1:9/v1", "models": ["img-xl"]})
    providers.add_provider({"name": "文本供应商", "base_url": "https://api.example.com/v1",
                            "models": ["m-1", "m-2"]})
    p, m = providers.resolve()
    assert p["kind"] != "image" and m == "m-1"
    # cfg 显式指到 image 条目 → 视为不存在，回落文本默认
    p2, m2 = providers.resolve({"_provider_id": img["id"]})
    assert p2["kind"] != "image" and m2 == "m-1"
    # cfg 只有模型：模型匹配也只搜文本条目
    p3, m3 = providers.resolve({"_model": "img-xl"})
    assert p3["kind"] != "image" and m3 != "img-xl"


def test_strict_binding_rejects_image_entry(prov_file):
    providers.add_provider({"name": "文本A", "base_url": "https://api.example.com/v1",
                            "models": ["m-1"]})
    img = providers.add_provider({"kind": "image", "name": "图像供应商",
                                  "base_url": "http://127.0.0.1:9/v1", "models": ["img-xl"]})
    with pytest.raises(providers.ProviderError, match="图像"):
        providers.resolve({"_provider_id": img["id"], "_model": "img-xl", "_model_bound": True})


def test_usable_and_model_options_skip_image(prov_file):
    providers.add_provider({"kind": "image", "name": "图像供应商",
                            "base_url": "http://127.0.0.1:9/v1", "models": ["img-xl"]})
    assert providers.usable() is False               # 图像条目不救文本可用性
    assert providers.active_model_options() == []
    providers.add_provider({"name": "文本供应商", "base_url": "https://api.example.com/v1",
                            "models": ["m-1"]})
    assert providers.usable() is True
    assert [o["id"] for o in providers.active_model_options()] != []
    assert all(o["id"] != "image" for o in providers.active_model_options())


# ---------------------------------------------------------------- 4. image_resolve 双默认链


def test_image_resolve_default_chain(prov_file):
    assert providers.image_resolve() is None          # 无图像条目
    a = providers.add_provider({"kind": "image", "name": "图像A",
                                "base_url": "http://127.0.0.1:9/v1", "models": ["img-a"]})
    b = providers.add_provider({"kind": "image", "name": "图像B",
                                "base_url": "http://127.0.0.2:9/v1", "models": ["img-b"]})
    # 无 default_image_provider 时（fixture 空）→ 首条自动成为默认
    data = providers.load_data()
    assert data["default_image_provider"] == a["id"]
    p, m = providers.image_resolve()
    assert p["id"] == a["id"] and m == "img-a"
    # 指到 B → B 生效
    providers.set_default(b["id"])
    p2, m2 = providers.image_resolve()
    assert p2["id"] == b["id"] and m2 == "img-b"
    # B 停用 → 回落 A（active 链）
    providers.set_active(b["id"], False)
    p3, _ = providers.image_resolve()
    assert p3["id"] == a["id"]


def test_text_and_image_defaults_independent(prov_file):
    """双默认槽各自独立设置互不干扰（核心验收）。"""
    t = providers.add_provider({"name": "文本A", "base_url": "https://api.example.com/v1",
                                "models": ["m-1"]})
    t2 = providers.add_provider({"name": "文本B", "base_url": "https://api.b.com/v1",
                                 "models": ["m-2"]})
    i = providers.add_provider({"kind": "image", "name": "图像A",
                                "base_url": "http://127.0.0.1:9/v1", "models": ["img-a"]})
    i2 = providers.add_provider({"kind": "image", "name": "图像B",
                                 "base_url": "http://127.0.0.2:9/v1", "models": ["img-b"]})
    providers.set_default(t2["id"])
    providers.set_default(i2["id"])
    data = providers.load_data()
    assert data["default_provider"] == t2["id"]
    assert data["default_image_provider"] == i2["id"]
    p, _ = providers.resolve()
    ip, im = providers.image_resolve()
    assert p["id"] == t2["id"] and im == "img-b"
    # 文本默认变化不影响图像默认
    providers.set_default(t["id"])
    data2 = providers.load_data()
    assert data2["default_provider"] == t["id"]
    assert data2["default_image_provider"] == i2["id"]
    # 图像默认变化不影响文本默认
    providers.set_default(i["id"])
    data3 = providers.load_data()
    assert data3["default_provider"] == t["id"]
    assert data3["default_image_provider"] == i["id"]


def test_remove_and_deactivate_fallbacks_are_kind_scoped(prov_file):
    t = providers.add_provider({"name": "文本A", "base_url": "https://api.example.com/v1",
                                "models": ["m-1"]})
    i = providers.add_provider({"kind": "image", "name": "图像A",
                                "base_url": "http://127.0.0.1:9/v1", "models": ["img-a"]})
    providers.set_default(t["id"])
    providers.set_default(i["id"])
    # 删除图像默认：图像默认回落空，文本默认不受影响
    providers.remove_provider(i["id"])
    data = providers.load_data()
    assert data["default_provider"] == t["id"]
    assert data["default_image_provider"] == ""
    # 再建一条图像（自动设默认）后删除文本默认：文本默认回落空、绝不指到图像条目
    i2 = providers.add_provider({"kind": "image", "name": "图像B",
                                 "base_url": "http://127.0.0.2:9/v1", "models": ["img-b"]})
    providers.remove_provider(t["id"])
    data2 = providers.load_data()
    assert data2["default_provider"] == ""
    assert data2["default_image_provider"] == i2["id"]


def test_deactivate_default_reassigns_within_kind(prov_file):
    t = providers.add_provider({"name": "文本A", "base_url": "https://api.example.com/v1",
                                "models": ["m-1"]})
    t2 = providers.add_provider({"name": "文本B", "base_url": "https://api.b.com/v1",
                                 "models": ["m-2"]})
    i = providers.add_provider({"kind": "image", "name": "图像A",
                                "base_url": "http://127.0.0.1:9/v1", "models": ["img-a"]})
    i2 = providers.add_provider({"kind": "image", "name": "图像B",
                                 "base_url": "http://127.0.0.2:9/v1", "models": ["img-b"]})
    providers.set_default(t2["id"])
    providers.set_default(i2["id"])
    providers.set_active(t2["id"], False)
    providers.set_active(i2["id"], False)
    data = providers.load_data()
    assert data["default_provider"] == t["id"]            # 回落到剩余 active 文本条目
    assert data["default_image_provider"] == i["id"]      # 回落到剩余 active 图像条目


def test_remove_last_active_guard_still_blocks(prov_file):
    p = providers.add_provider({"name": "文本A", "base_url": "https://api.example.com/v1",
                                "models": ["m-1"]})
    with pytest.raises(providers.ProviderError):
        providers.remove_provider(p["id"])


# ---------------------------------------------------------------- 6. add/update 校验与视图


def test_add_image_provider_size_and_validation(prov_file):
    with pytest.raises(providers.ProviderError):
        providers.add_provider({"kind": "image", "name": "图像X",
                                "base_url": "http://127.0.0.1:9/v1", "models": ["img-x"],
                                "size": "1024*1536"})
    p = providers.add_provider({"kind": "image", "name": "图像X",
                                "base_url": "http://127.0.0.1:9/v1", "models": ["img-x"],
                                "size": "832x1248"})
    assert p["kind"] == "image" and p["size"] == "832x1248"
    assert p["default_model"] == "img-x"
    # kind 非法 → 按 text
    p2 = providers.add_provider({"kind": "audio", "name": "文本Y",
                                 "base_url": "https://api.y.com/v1", "models": ["m-y"]})
    assert p2["kind"] == "text"


def test_update_image_size_and_models(prov_file):
    p = providers.add_provider({"kind": "image", "name": "图像X",
                                "base_url": "http://127.0.0.1:9/v1", "models": ["img-x"]})
    got = providers.update_provider(p["id"], {"size": "768x1152", "models": ["img-x", "img-y"]})
    assert got["size"] == "768x1152"
    with pytest.raises(providers.ProviderError):
        providers.update_provider(p["id"], {"size": "nope"})


def test_set_global_model_rejects_image_entry(prov_file):
    p = providers.add_provider({"kind": "image", "name": "图像X",
                                "base_url": "http://127.0.0.1:9/v1", "models": ["img-x"]})
    with pytest.raises(providers.ProviderError):
        providers.set_global_model(p["id"], "img-x")


def test_list_providers_carries_dual_defaults(prov_file):
    providers.add_provider({"name": "文本A", "base_url": "https://api.example.com/v1",
                            "models": ["m-1"]})
    providers.add_provider({"kind": "image", "name": "图像A",
                            "base_url": "http://127.0.0.1:9/v1", "models": ["img-a"]})
    d = providers.list_providers()
    assert d["default_provider"] and d["default_image_provider"]
    kinds = {p["id"]: p.get("kind") for p in d["providers"]}
    assert "image" in set(kinds.values()) and "text" in set(kinds.values())


# ---------------------------------------------------------------- 7. webcover 新结构读写


def test_config_get_returns_compat_and_entries(client, prov_file):
    d0 = client.get("/api/library/cover/ai/config").json()
    assert d0["ok"] is True and d0["configured"] is False
    assert d0["default_image_provider"] == "" and d0["providers"] == []
    assert "api_key" not in d0
    providers.add_provider({"kind": "image", "name": "硅基流动-图像",
                            "base_url": "http://127.0.0.1:9/v1", "models": ["Kolors"],
                            "api_key": "sk-img-1234567890", "size": "1024x1536"})
    d1 = client.get("/api/library/cover/ai/config").json()
    assert d1["configured"] is True and d1["model"] == "Kolors"
    assert d1["key_display"].startswith("已设置")
    assert len(d1["providers"]) == 1
    ent = d1["providers"][0]
    assert ent["kind"] == "image" and ent["model"] == "Kolors" and ent["size"] == "1024x1536"
    assert "api_key" not in ent


def test_config_post_updates_selected_entry_and_sets_default(client, prov_file):
    providers.add_provider({"kind": "image", "name": "图像A",
                            "base_url": "http://127.0.0.1:9/v1", "models": ["img-a"]})
    b = providers.add_provider({"kind": "image", "name": "图像B",
                                "base_url": "http://127.0.0.2:9/v1", "models": ["img-b"]})
    r = client.post("/api/library/cover/ai/config",
                    json={"provider_id": b["id"], "model": "img-b2", "size": "768x1152"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["model"] == "img-b2" and d["size"] == "768x1152"
    data = providers.load_data()
    assert data["default_image_provider"] == b["id"]          # 显式 provider_id = 设为图像默认
    assert providers.image_resolve()[1] == "img-b2"


def test_config_post_v57_shape_creates_entry(client, prov_file):
    """v5.7.0 旧客户端口径：不带 provider_id 的 {base_url, api_key, model, size} 仍可用。"""
    r = client.post("/api/library/cover/ai/config",
                    json={"base_url": "http://127.0.0.1:9/v1", "model": "dall-e-3",
                          "api_key": "sk-secret-1234567890", "size": "768x1152"})
    assert r.status_code == 200 and r.json()["configured"] is True
    data = providers.load_data()
    imgs = [p for p in data["providers"] if p.get("kind") == "image"]
    assert len(imgs) == 1 and imgs[0]["api_key"] == "sk-secret-1234567890"
    assert data["default_image_provider"] == imgs[0]["id"]
    # 旧小节镜像（回滚安全）
    ondisk = _read_providers(prov_file)
    assert ondisk["image_provider"]["model"] == "dall-e-3"
    assert ondisk["image_provider"]["api_key"] == "sk-secret-1234567890"
    # api_key 留空 = 保留旧值
    client.post("/api/library/cover/ai/config", json={"api_key": ""})
    assert client.get("/api/library/cover/ai/config").json()["has_key"] is True


def test_config_post_clear_keeps_unconfigured(client, prov_file):
    providers.save_image_provider({"base_url": "http://127.0.0.1:9/v1", "model": "dall-e-3"})
    assert client.get("/api/library/cover/ai/config").json()["configured"] is True
    r = client.post("/api/library/cover/ai/config", json={"base_url": "", "model": ""})
    assert r.status_code == 200
    d = client.get("/api/library/cover/ai/config").json()
    assert d["configured"] is False
    rt = providers.image_provider_runtime()
    assert rt["configured"] is False


def test_config_post_validation_chinese(client, prov_file):
    r = client.post("/api/library/cover/ai/config", json={"base_url": "ftp://x", "model": "m"})
    assert r.status_code == 400 and "base_url" in r.json()["error"]
    r2 = client.post("/api/library/cover/ai/config", json={"model": "bad model name!"})
    assert r2.status_code == 400 and "模型名" in r2.json()["error"]
    r3 = client.post("/api/library/cover/ai/config",
                     json={"base_url": "http://x/v1", "model": "m", "size": "1024*1536"})
    assert r3.status_code == 400 and "尺寸" in r3.json()["error"]


class _FakeResp:
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


def test_probe_endpoint_per_entry(client, prov_file, monkeypatch):
    import base64 as _b64

    providers.save_image_provider({"base_url": "http://127.0.0.1:9/v1", "model": "img-a",
                                   "api_key": "sk-a-1234567890"})
    b = providers.add_provider({"kind": "image", "name": "图像B",
                                "base_url": "http://127.0.0.2:9/v1", "models": ["img-b"],
                                "api_key": "sk-b-1234567890"})
    seen = {}

    def _ok(url, headers, json):
        seen.update({"url": url, "auth": headers.get("Authorization", ""), "json": json})
        return _FakeResp(200, {"data": [{"b64_json": _b64.b64encode(_png()).decode()}]})

    monkeypatch.setattr(providers.httpx, "Client", lambda **kw: _FakeClient(_ok))
    # 缺省 → 当前默认（save_image_provider 建的首条）
    d = client.post("/api/library/cover/ai/test", json={}).json()
    assert d["ok"] is True and seen["json"]["model"] == "img-a"
    # 指定条目 → B
    d2 = client.post("/api/library/cover/ai/test", json={"provider_id": b["id"]}).json()
    assert d2["ok"] is True and seen["json"]["model"] == "img-b"
    assert seen["auth"] == "Bearer sk-b-1234567890"
    # 文本条目 → 拒绝
    t = providers.add_provider({"name": "文本", "base_url": "https://api.example.com/v1",
                                "models": ["m-1"]})
    d3 = client.post("/api/library/cover/ai/test", json={"provider_id": t["id"]}).json()
    assert d3["ok"] is False and "图像" in d3["detail"]


def test_probe_endpoint_unconfigured(client, prov_file):
    d = client.post("/api/library/cover/ai/test", json={}).json()
    assert d["ok"] is False and "尚未配置" in d["detail"]


# ---------------------------------------------------------------- 8. AI 封面端到端（mock 图像供应商）


def test_cover_generate_end_to_end_with_image_entry(lib, prov_file, monkeypatch):
    providers.add_provider({"kind": "image", "name": "图像供应商",
                            "base_url": "http://127.0.0.1:9/v1", "models": ["fooocus-xl"],
                            "api_key": "sk-e2e-1234567890", "size": "1024x1536"})

    def _fake(rt, prompt, timeout=webcover.IMAGE_TIMEOUT):
        assert rt["model"] == "fooocus-xl" and rt["size"] == "1024x1536"
        assert rt["api_key"] == "sk-e2e-1234567890"
        assert "《长夜灯河》" in prompt
        return _png()

    monkeypatch.setattr(webcover, "generate_image", _fake)
    app = FastAPI()
    app.include_router(webcover.router)
    c = TestClient(app)
    r = c.post("/api/library/cover/ai", json={"key": "novel_a", "style": "水墨"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["cover"] == "cover.png" and d["mock"] is False
    with open(os.path.join(str(lib), "novel_a", "cover.png"), "rb") as f:
        assert f.read()[:8] == b"\x89PNG\r\n\x1a\n"


def test_cover_generate_without_image_entry_chinese(lib, prov_file):
    app = FastAPI()
    app.include_router(webcover.router)
    c = TestClient(app)
    r = c.post("/api/library/cover/ai", json={"key": "novel_a"})
    assert r.status_code == 400
    msg = r.json()["error"]
    assert "请先在设置→供应商配置图像模型" in msg
    assert "图像" in msg and "供应商" in msg


def test_text_chain_untouched_by_image_entries(lib, prov_file):
    """llm 链路用的 resolve/fallback_config 在混入图像条目后行为不变。"""
    providers.add_provider({"kind": "image", "name": "图像供应商",
                            "base_url": "http://127.0.0.1:9/v1", "models": ["img-xl"]})
    providers.add_provider({"name": "文本A", "base_url": "https://api.example.com/v1",
                            "models": ["m-1"]})
    p, m = providers.resolve()
    assert p["kind"] == "text" and m == "m-1"
    fb = providers.fallback_config({}, exclude_provider=p["id"], exclude_model=m)
    assert fb is None or fb["_provider_id"] != "图像供应商"
    assert providers.global_model()["provider_id"] == p["id"]
