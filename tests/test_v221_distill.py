"""v2.2.1：风格蒸馏按文体分流（四文体各一套维度）+ 接口透传 medium。

覆盖：维度选择、卡片落盘带 medium、style_prompt 覆盖该文体全部维度、
短语料拒收、未知文体回落小说、/api/distill 透传 medium、列表返回 medium。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import distill, security, writer

CORPUS = "雨落在瓦上，檐水成线。他把伞收好，放进门口的桶里，谁也没有说话。" * 12


@pytest.fixture()
def styles_dir(tmp_path, monkeypatch):
    d = tmp_path / "styles"
    d.mkdir()
    monkeypatch.setattr(distill, "STYLES_DIR", str(d))
    return d


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(writer, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))
    appmod.PROJECT_INDEX.clear()
    return TestClient(appmod.app)


# ---------------------------------------------------------------- 维度分流


def test_normalize_medium_aliases():
    assert distill.normalize_medium("poetry") == "poetry"
    assert distill.normalize_medium("诗词") == "poetry"
    assert distill.normalize_medium("script") == "script"
    assert distill.normalize_medium("prose") == "prose"
    assert distill.normalize_medium("") == "novel"
    assert distill.normalize_medium("不存在的体裁") == "novel"


def test_each_medium_has_its_own_dims():
    labels = {m: [k for k, _ in spec["dims"]] for m, spec in distill.MEDIUM_SPECS.items()}
    assert set(labels) == {"novel", "script", "poetry", "prose"}
    # 四套维度不能互相串味
    assert "意象系统" in labels["poetry"]
    assert "场景调度" in labels["script"]
    assert "句子语感" in labels["prose"]
    assert "句式句长" in labels["novel"]
    assert not set(labels["novel"]) & set(labels["poetry"])


def test_distill_uses_medium_dims(styles_dir, monkeypatch):
    seen = {}

    def fake_chat_json(msgs, **kwargs):
        seen["msgs"] = msgs
        return {"summary": "冷硬克制", "dims": {"意象系统": "以雨与檐为主轴",
                                              "结句收束": "余味落在物件上"},
                "fingerprint_words": ["檐", "雨线"], "anti_ai": ["不要直抒胸臆"]}

    monkeypatch.setattr(distill.llm, "chat_json", fake_chat_json)
    res = distill.distill(CORPUS, "冷雨风", genre="悬疑", medium="poetry")
    assert res["ok"] is True
    sys_p = seen["msgs"][0]["content"]
    assert "意象系统" in sys_p
    assert "场景调度" not in sys_p

    card = json.loads((styles_dir / "冷雨风" / "style.json").read_text(encoding="utf-8"))
    assert card["medium"] == "poetry"
    # style_prompt 要带上该文体的全部维度
    assert "意象系统" in card["style_prompt"]
    assert "结句收束" in card["style_prompt"]
    assert "冷雨风" in card["style_prompt"]


def test_distill_defaults_to_novel(styles_dir, monkeypatch):
    monkeypatch.setattr(distill.llm, "chat_json", lambda *a, **k: {
        "summary": "s", "dims": {"句式句长": "长短错落"},
        "fingerprint_words": [], "anti_ai": []})
    res = distill.distill(CORPUS, "默认体裁")
    assert res["ok"] is True
    card = json.loads((styles_dir / "默认体裁" / "style.json").read_text(encoding="utf-8"))
    assert card["medium"] == "novel"


def test_distill_rejects_short_corpus(styles_dir):
    res = distill.distill("太短了", "短名")
    assert res["ok"] is False
    assert "语料太短" in res["note"]


def test_list_styles_reports_medium(styles_dir, monkeypatch):
    monkeypatch.setattr(distill.llm, "chat_json", lambda *a, **k: {
        "summary": "s", "dims": {}, "fingerprint_words": ["雨"], "anti_ai": []})
    distill.distill(CORPUS, "带体裁", medium="script")
    items = distill.list_styles()
    assert items and items[0]["name"] == "带体裁"
    assert items[0]["medium"] == "script"


# ---------------------------------------------------------------- 接口


def test_api_distill_passes_medium(client, tmp_path, monkeypatch):
    monkeypatch.setattr(distill, "STYLES_DIR", str(tmp_path / "styles"))
    seen = {}

    def fake_chat_json(msgs, **kwargs):
        seen["msgs"] = msgs
        return {"summary": "克制", "dims": {"场景调度": "空间调度简净"},
                "fingerprint_words": ["夜"], "anti_ai": []}

    monkeypatch.setattr(distill.llm, "chat_json", fake_chat_json)
    res = client.post("/api/distill", json={"name": "接口风格", "text": CORPUS, "medium": "script"})
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert res.json()["style"]["medium"] == "script"
    assert "场景调度" in seen["msgs"][0]["content"]


def test_api_distill_requires_name(client):
    res = client.post("/api/distill", json={"text": CORPUS})
    assert res.status_code == 400


def test_api_styles_lists_medium(client, tmp_path, monkeypatch):
    monkeypatch.setattr(distill, "STYLES_DIR", str(tmp_path / "styles"))
    monkeypatch.setattr(distill.llm, "chat_json", lambda *a, **k: {
        "summary": "s", "dims": {}, "fingerprint_words": [], "anti_ai": []})
    client.post("/api/distill", json={"name": "列表风格", "text": CORPUS, "medium": "prose"})
    items = client.get("/api/styles").json()["items"]
    assert items and items[0]["medium"] == "prose"
