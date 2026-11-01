# -*- coding: utf-8 -*-
"""v4.19.1 回归：供应商密钥在「编辑保存」后必须原样保留。

背景 bug：编辑已保存供应商时密钥框按设计不回显（留空=不修改），此前两条路径
会让密钥丢失 / 看似丢失，导致所有模型调用失败：
1) 编辑弹窗拉模型列表把空密钥发给接口 → 401，用户误以为密钥失效
   （/api/providers/fetch-models 现支持 provider_id 回落已存密钥）；
2) 任何调用方把 api_key="" 传给 update_provider → 已存密钥被覆盖为空，
   所有调用立即 401（update_provider 现把空串视为不修改）。
"""

import json

import pytest

from bot import providers


@pytest.fixture()
def isolated_providers(tmp_path, monkeypatch):
    """providers.json 指到临时目录，预置一条带密钥的供应商。"""
    pf = tmp_path / "providers.json"
    data = {
        "version": 2,
        "default_provider": "prov-a",
        "providers": [{
            "id": "prov-a", "name": "供应商A", "preset": False, "active": True,
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-original-key-123",
            "models": ["m-1"], "default_model": "m-1",
        }],
    }
    pf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(providers, "PROVIDERS_FILE", str(pf))
    return pf


def _stored_key(pf):
    return json.loads(pf.read_text(encoding="utf-8"))["providers"][0]["api_key"]


def test_update_without_api_key_keeps_key(isolated_providers):
    providers.update_provider("prov-a", {"name": "改名", "models": ["m-1", "m-2"]})
    assert _stored_key(isolated_providers) == "sk-original-key-123"


def test_update_with_empty_api_key_keeps_key(isolated_providers):
    """关键回归：api_key 传空串/空白绝不能把已存密钥清掉。"""
    providers.update_provider("prov-a", {"name": "改名", "api_key": ""})
    assert _stored_key(isolated_providers) == "sk-original-key-123"
    providers.update_provider("prov-a", {"api_key": "   "})
    assert _stored_key(isolated_providers) == "sk-original-key-123"


def test_update_with_new_api_key_replaces(isolated_providers):
    providers.update_provider("prov-a", {"api_key": "sk-new-key-456"})
    assert _stored_key(isolated_providers) == "sk-new-key-456"


def test_fetch_models_falls_back_to_saved_key(isolated_providers, monkeypatch):
    """编辑态密钥留空 + provider_id：后端自动改用已保存密钥拉取（而非空密钥 401）。"""
    from fastapi.testclient import TestClient
    import urllib.request as _ur

    import app as app_mod

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return json.dumps({"data": [{"id": "m-1"}, {"id": "m-2"}]}).encode()

    def fake_urlopen(req, timeout=12):
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp()

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)

    client = TestClient(app_mod.app)
    r = client.post("/api/providers/fetch-models",
                    json={"base_url": "https://api.example.com/v1",
                          "api_key": "", "provider_id": "prov-a"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["used_saved_key"] is True
    assert body["models"] == ["m-1", "m-2"]
    assert captured["auth"] == "Bearer sk-original-key-123"


def test_fetch_models_prefers_fresh_key_over_saved(isolated_providers, monkeypatch):
    """用户在弹窗里重填了新密钥时，必须用新密钥而不是已存的旧密钥。"""
    from fastapi.testclient import TestClient
    import urllib.request as _ur

    import app as app_mod

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return json.dumps({"data": [{"id": "m-9"}]}).encode()

    def fake_urlopen(req, timeout=12):
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp()

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)

    client = TestClient(app_mod.app)
    r = client.post("/api/providers/fetch-models",
                    json={"base_url": "https://api.example.com/v1",
                          "api_key": "sk-typed-fresh", "provider_id": "prov-a"})
    assert r.status_code == 200
    assert r.json()["used_saved_key"] is False
    assert captured["auth"] == "Bearer sk-typed-fresh"
