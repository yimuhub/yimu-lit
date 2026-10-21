"""v1.0.0：文体路由 + 技能库最小上下文注入 回归测试（不烧 LLM）。

覆盖：medium 注册表、detect 判定、skillkit 索引、stage_inject 行为、
     题材定向、API 端点结构。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from bot import medium, skillkit

import app as appmod

client = TestClient(appmod.app)


# ---------- 文体注册表 ----------

def test_medium_registry_four():
    """四种文体齐备，每种都要有阶段表 + 评估维度 + 及格线。"""
    assert set(medium.MEDIUMS.keys()) == {"novel", "script", "poetry", "prose"}
    for key, m in medium.MEDIUMS.items():
        assert m["stages"], f"{key} 缺阶段表"
        assert m["score_dims"], f"{key} 缺评估维度"
        assert m["pass_line"] > 0, f"{key} 缺及格线"
        assert sum(w for _n, w in m["score_dims"]) == 100, f"{key} 评估维度权重和应为 100"


def test_medium_registry_api():
    d = client.get("/api/mediums").json()
    assert "mediums" in d and len(d["mediums"]) == 4
    for m in d["mediums"]:
        assert {"key", "label", "icon", "stages", "score_dims", "pass_line"} <= set(m)


def test_medium_detect():
    """常见说法要判对文体。"""
    cases = [
        ("写一部悬疑小说，退休刑警被诬陷杀人，10章", "novel"),
        ("帮我写个电影剧本，三幕", "script"),
        ("写一首七律，咏梅", "poetry"),
        ("来篇散文随笔", "prose"),
        ("写个分集大纲", "script"),
        ("咏雪一首", "poetry"),
        ("写一阙水调歌头", "poetry"),
        ("写个舞台剧", "script"),
    ]
    for text, expect in cases:
        got = medium.detect(text)
        assert got == expect, f"「{text}」判成 {got}，应为 {expect}"


def test_medium_detect_api():
    d = client.get("/api/medium/detect", params={"text": "写一首七律"}).json()
    assert d["medium"] == "poetry" and d["label"] == "诗词"


def test_medium_normalize_alias():
    assert medium.normalize("剧本") == "script"
    assert medium.normalize("poetry") == "poetry"
    assert medium.normalize("散文") == "prose"
    assert medium.normalize("不存在的文体") == ""


# ---------- 技能库 ----------

def test_skillkit_index_has_novel():
    items = skillkit.index()
    assert items, "技能库索引为空"
    for sf in items:
        assert sf.name, f"{sf.relpath} 缺 name"
        assert sf.media, f"{sf.relpath} 缺 medium"
        assert sf.kind in ("method", "case", "data", "template"), f"{sf.relpath} kind 非法"


def test_skillkit_stats_api():
    d = client.get("/api/skillkit/stats").json()
    assert d["files"] > 0 and d["bytes"] > 0


def test_skillkit_list_filter():
    all_items = client.get("/api/skillkit/list").json()["skills"]
    assert all_items
    novel = client.get("/api/skillkit/list", params={"medium": "novel"}).json()["skills"]
    assert 0 < len(novel) <= len(all_items)


def test_stage_inject_is_minimal():
    """最小上下文注入：不能把整库塞进去。"""
    total = skillkit.stats()["bytes"]
    ctx = skillkit.stage_inject("P2", "novel")
    assert 0 < len(ctx) < total * 0.9, f"注入量 {len(ctx)} 相对全库 {total} 太大"
    assert "技能" in ctx


def test_stage_inject_respects_max_bytes():
    ctx = skillkit.stage_inject("P2", "novel", max_bytes=4000)
    assert len(ctx) <= 4000 + 2000  # 允许头部说明的少量溢出


def test_stage_inject_genre_targeting():
    """题材定向：注推理时不应把都市题材文件也带进来。"""
    import re
    ctx = skillkit.stage_inject("P2", "novel", genre="推理悬疑", limit=12)
    # 按注入的「文件路径行」判定，而不是正文里是否提到别的题材名
    injected = re.findall(r"技能：.*?（(.+?)）=====", ctx)
    genre_files = [f for f in injected if "references/genre/" in f]
    assert any("mystery.md" in f for f in genre_files), f"未注入推理题材：{injected}"
    assert not any("urban.md" in f or "xuanhuan.md" in f for f in genre_files), \
        f"混入了其它题材：{injected}"


def test_stage_inject_case_excluded_by_default():
    """案例库默认不注入（体积大 + 部分受版权约束）。"""
    ctx = skillkit.stage_inject("P2", "novel", include_case=False)
    assert "reference-" not in ctx


def test_stage_inject_all_stages_nonempty():
    """每个文体的每个阶段都要能拿到上下文（至少不崩）。"""
    for key in medium.MEDIUMS:
        for code in medium.all_stage_codes(key):
            ctx = skillkit.stage_inject(code, key)
            assert isinstance(ctx, str), f"{key}/{code} 注入异常"


def test_skillkit_preview_api():
    d = client.get("/api/skillkit/preview",
                   params={"stage": "P2", "medium": "novel", "genre": "都市"}).json()
    assert d["bytes"] > 0 and "context" in d
    assert "urban.md" in d["context"]


def test_skillkit_preview_rejects_bad_medium():
    r = client.get("/api/skillkit/preview", params={"stage": "P2", "medium": "nope"})
    assert r.status_code == 400


# ---------- 向后兼容 ----------

def test_novel_skill_still_loadable():
    """旧 API 不能坏：skill.skill_md / genre / golden_rules 仍可用。"""
    from bot import skill
    assert len(skill.skill_md()) > 100
    assert len(skill.genre("都市爽文")) > 50
    assert skill.golden_rules()
    assert len(skill.anti_slop()) > 50
    assert len(skill.golden3()) > 50


def test_version_is_v1_line():
    import re
    from bot import version
    assert re.match(r"^1\.\d+\.\d+", version.VE), f"版本应为 1.x：{version.VE}"
