"""v2.1 P1 题材映射补全 回归防护（不烧 LLM、不起 app）。

覆盖用户反馈「不同题材的技能在新版界面里看不到」的后端根因：
1. _genre_key 覆盖全部实际存在的 novel craft 文件（中文题材名 + 别名）；
2. bot/skill.py 与 bot/skillkit.py 收敛为单一映射实现；
3. 11 个 genre craft 文件 frontmatter 带 genre key，索引层解析；
4. list_skills(genre=/agent=)、tree()、read_skill()、stats()["by_genre"] 契约；
5. read_skill 路径安全（../、绝对路径、NUL 全部拒绝）；
6. stage_inject(genre="历史") 真实注入 历史.md 正文（最关键回归）。
"""
import os
import re

import pytest

from bot import medium as medium_mod
from bot import skill, skillkit

CRAFT_FILES = {  # genre key → skillkit/novel/references/genre/ 下的实际文件名
    "mystery": "mystery.md", "romance": "romance.md", "horror": "horror.md",
    "xuanhuan": "xuanhuan.md", "urban": "urban.md", "history": "历史.md",
    "scifi": "科幻.md", "military": "军事.md", "workplace": "职场.md",
    "xianxia": "仙侠.md", "_index": "_index.md",   # 11 个 craft 文件
}
GENRE_DIR = "novel" + os.sep + "references" + os.sep + "genre" + os.sep


@pytest.fixture(autouse=True)
def _fresh_index():
    skillkit.reload()
    yield


# ---------- 1. _genre_key 映射表 ----------

def test_genre_key_full_chinese_coverage():
    for name, key in [
        ("悬疑", "mystery"), ("推理", "mystery"),
        ("言情", "romance"), ("古言", "romance"), ("甜宠", "romance"),
        ("恐怖", "horror"), ("灵异", "horror"),
        ("玄幻", "xuanhuan"), ("修真", "xuanhuan"),
        ("都市", "urban"),
        ("历史", "history"), ("架空历史", "history"),
        ("科幻", "scifi"), ("星际", "scifi"),
        ("军事", "military"), ("战争", "military"),
        ("职场", "workplace"), ("行业", "workplace"),
        ("仙侠", "xianxia"),
        ("武侠", "wuxia"),
    ]:
        assert skillkit._genre_key(name) == key, name


def test_genre_key_aliases_and_roundtrip():
    assert skillkit._genre_key("都市情感") == "romance"     # 不能被「都市」吞成 urban
    assert skillkit._genre_key("都市爽文") == "urban"
    assert skillkit._genre_key("推理悬疑") == "mystery"
    assert skillkit._genre_key(" 历史 ") == "history"
    for key in CRAFT_FILES:                                  # 英文 key 无损往返
        assert skillkit._genre_key(key) == key
    assert skillkit._genre_key("") == "_index"
    assert skillkit._genre_key("不存在的题材") == "_index"


def test_genre_key_single_implementation():
    """bot/skill.py 的重复实现已删除：两处必须是同一个函数对象。"""
    assert skill._genre_key is skillkit._genre_key
    assert skill.genre("历史") != skill.genre("科幻")
    assert "在那个约束下" in skill.genre("历史")            # 小说实际注入路径（workflow.py）


# ---------- 2. frontmatter genre 元数据 + 索引解析 ----------

def test_craft_frontmatter_genre_parsed():
    items = {i["relpath"]: i for i in skillkit.list_skills()}
    for key, fname in CRAFT_FILES.items():
        rel = GENRE_DIR + fname
        assert rel in items, f"{rel} 未进索引"
        assert items[rel]["genre"] == key, (rel, items[rel]["genre"])
        assert items[rel]["title"], rel
        assert items[rel]["evolved"] is False


# ---------- 3. list_skills(genre=...) ----------

def test_list_skills_genre_covers_11_crafts():
    """11 个题材 craft 都能按题材名（中文别名或 key）被 list_skills 取到。"""
    queries = [("悬疑推理", "mystery"), ("言情", "romance"), ("恐怖", "horror"),
               ("玄幻", "xuanhuan"), ("都市", "urban"), ("历史", "history"),
               ("科幻", "scifi"), ("军事", "military"), ("职场", "workplace"),
               ("仙侠", "xianxia"), ("通用默认", "_index")]
    for q, key in queries:
        items = skillkit.list_skills(genre=q)
        rels = {i["relpath"] for i in items}
        assert GENRE_DIR + CRAFT_FILES[key] in rels, (q, key, rels)
        assert all(i["genre"] == key for i in items), q
    # 武侠暂无 craft 文件：过滤结果优雅为空（_genre_key 仍给 wuxia，资产补齐即生效）
    assert skillkit.list_skills(genre="武侠") == []


def test_list_skills_item_fields_and_agent_filter():
    item = skillkit.list_skills(genre="历史")[0]
    for f in ("relpath", "title", "kind", "weight", "evolved", "genre", "name"):
        assert f in item, f
    # agent 参数：bot/agents.skills_for 缺席/未绑定时静默跳过或按绑定过滤，不得抛异常
    out = skillkit.list_skills(med="novel", agent="writer")
    assert isinstance(out, list)
    bound = skillkit._agent_skill_names("writer")
    if bound:
        assert {i["name"] for i in out} == {i["name"] for i in skillkit.list_skills() if i["name"] in bound}


# ---------- 4. read_skill 路径安全 ----------

@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "/etc/passwd",
    "novel/../../../etc/passwd",
    "novel/references/genre/../../../../../tmp/x.md",
    "\x00",
    "novel/\x00evil.md",
    "~/../.ssh/id_rsa",
    "",
    "   ",
])
def test_read_skill_rejects(bad):
    with pytest.raises(ValueError):
        skillkit.read_skill(bad)


def test_read_skill_ok():
    d = skillkit.read_skill("novel/references/genre/历史.md")
    assert d["relpath"] == GENRE_DIR + "历史.md"
    assert d["name"] == "novel-genre-history"
    assert d["title"] == "历史 · 架空历史"
    assert d["frontmatter"]["genre"] == "history"
    assert d["frontmatter"]["medium"] == "[novel]" or d["frontmatter"]["medium"] == ["novel"]
    assert d["content"].lstrip().startswith("# 历史")
    assert "---" not in d["content"][:20]                     # frontmatter 已剥离


# ---------- 5. tree() ----------

def test_tree_structure_and_labels():
    t = skillkit.tree()
    assert [x["medium"] for x in t] == list(medium_mod.MEDIUMS)
    assert [x["label"] for x in t] == [medium_mod.label(m) for m in medium_mod.MEDIUMS]
    novel = t[0]
    gmap = {g["genre"]: g for g in novel["groups"]}
    assert gmap["history"]["label"] == "历史"
    assert gmap["history"]["count"] == 1
    assert gmap["history"]["skills"][0]["relpath"].endswith("历史.md")
    assert gmap["_index"]["label"] == "通用"
    for g in novel["groups"]:
        assert set(g) == {"genre", "label", "count", "skills"}
        assert g["count"] == len(g["skills"])
    # 单文体过滤 + 非 novel 文体按子目录分组
    assert len(skillkit.tree(medium="novel")) == 1
    script = next(x for x in t if x["medium"] == "script")
    assert {"剧集", "戏曲", "通用"} <= {g["genre"] for g in script["groups"]}


# ---------- 6. stats().by_genre ----------

def test_stats_by_genre():
    s = skillkit.stats()
    for f in ("files", "bytes", "evolved", "by_media", "by_kind", "by_stage", "stage_gaps"):
        assert f in s, f                                        # 原有字段不变
    for key in CRAFT_FILES:
        assert s["by_genre"].get(key, 0) >= 1, key
    assert s["by_genre"]["history"] == 1


# ---------- 7. 排序 + 注入（最关键回归） ----------

def test_pick_genre_hit_first():
    p = skillkit.pick("novel", "P2", genre="历史")
    assert p[0].relpath == GENRE_DIR + "历史.md"
    assert all(os.sep + "genre" + os.sep not in sf.relpath or sf.genre in ("history",)
               for sf in p[:1])


def test_stage_inject_history_real_content():
    """回归防护：真实写作注入里必须有 历史.md 正文，且不混入其它题材 craft。"""
    ctx = skillkit.stage_inject("P2", "novel", genre="历史")
    assert GENRE_DIR + "历史.md" in ctx.replace(os.sep, "/")
    assert "在那个约束下，普通人怎么办" in ctx                  # 历史.md 独有正文
    assert "官制、税制、军制" in ctx
    injected = re.findall(r"技能：.*?（(.+?)）=====", ctx)
    craft = [f.replace(os.sep, "/") for f in injected if "references/genre/" in f]
    assert craft == ["novel/references/genre/历史.md"], craft   # 不注入其它题材
    assert len(ctx) <= 26000 + 2000                             # 字节上限约束未破坏


def test_stage_inject_new_genres_all_hit():
    """5 个旧版永远命中不到的题材，现在各自命中各自的文件。"""
    markers = {
        "历史": ("历史.md", "官制、税制、军制"),
        "科幻": ("科幻.md", None), "军事": ("军事.md", None),
        "职场": ("职场.md", None), "仙侠": ("仙侠.md", None),
    }
    for genre, (fname, _m) in markers.items():
        ctx = skillkit.stage_inject("P2", "novel", genre=genre)
        assert fname in ctx.replace(os.sep, "/"), genre


def test_stage_inject_agent_and_genre_backcompat():
    no_agent = skillkit.stage_inject("P2", "novel", genre="历史")
    with_agent = skillkit.stage_inject("P2", "novel", genre="历史", agent="writer")
    assert isinstance(with_agent, str) and with_agent            # 未绑定岗位时行为不变
    bound = skillkit._agent_skill_names("writer")
    if not bound:
        assert no_agent == with_agent
    # 武侠（无 craft）回落 _index 通用默认，不空注入
    ctx = skillkit.stage_inject("P2", "novel", genre="武侠")
    assert "_index.md" in ctx.replace(os.sep, "/")
