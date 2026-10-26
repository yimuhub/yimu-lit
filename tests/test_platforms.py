"""v2.1 P2：平台分类数据（bot/platforms.py）契约测试。

style_preset 必须命中 app.py _ONB["style"]["preset"] 的真实键集合——从 app 导入常量断言，不硬猜。
"""
import re

from app import _ONB
from bot import platforms

PRESET_KEYS = set(_ONB["style"]["preset"])
REQUIRED_FIELDS = {"key", "label", "note", "pay_model", "genres", "hot_tags", "pace", "hook", "style_preset"}
CJK = re.compile(r"[\u4e00-\u9fff]")

EXPECTED_KEYS = {"fanqie", "qidian", "jinjiang", "qimao", "feilu"}


def test_exactly_five_expected_platforms():
    assert len(platforms.PLATFORMS) == 5
    assert {p["key"] for p in platforms.PLATFORMS} == EXPECTED_KEYS


def test_required_fields_complete():
    for p in platforms.PLATFORMS:
        assert REQUIRED_FIELDS <= set(p), p["key"]
        assert p["pay_model"] in {"free", "paid", "both"}, p["key"]
        for f in ("key", "label", "note", "pace", "hook"):
            assert isinstance(p[f], str) and p[f].strip(), (p["key"], f)


def test_genres_nonempty_chinese_lists():
    for p in platforms.PLATFORMS:
        g = p["genres"]
        assert isinstance(g, list) and 12 <= len(g) <= 24, p["key"]
        assert len(set(g)) == len(g), f"{p['key']} 分类重复"
        for name in g:
            assert isinstance(name, str) and CJK.search(name), (p["key"], name)


def test_hot_tags_nonempty():
    for p in platforms.PLATFORMS:
        t = p["hot_tags"]
        assert isinstance(t, list) and t and all(isinstance(x, str) and x.strip() for x in t), p["key"]


def test_style_preset_exists_in_onb_presets():
    for p in platforms.PLATFORMS:
        assert p["style_preset"] in PRESET_KEYS, (p["key"], p["style_preset"])


def test_payload_shape_and_disclaimer():
    d = platforms.payload()
    assert set(d) == {"platforms", "disclaimer"}
    assert d["disclaimer"] and "非官方" in d["disclaimer"]
    assert len(d["platforms"]) == 5


def test_payload_returns_copy_source_untouched():
    d = platforms.payload()
    d["platforms"][0]["genres"].append("临时污染")
    assert "临时污染" not in platforms.PLATFORMS[0]["genres"]
