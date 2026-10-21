"""PR2 小说增强：新题材 craft + 追读/书名指南 + slop 接入 + 新 agent 岗位。"""
from bot import skillkit, agents, workflow, slop_checker


def test_genre_skills_indexed():
    skillkit.reload()
    names = {x["name"] for x in skillkit.list_skills()}
    for n in ["novel-genre-history", "novel-genre-scifi", "novel-genre-xianxia",
              "novel-genre-workplace", "novel-genre-military"]:
        assert n in names, n


def test_new_guide_skills_indexed():
    skillkit.reload()
    names = {x["name"] for x in skillkit.list_skills()}
    assert "novel-hangup-curve" in names
    assert "novel-title-blurb" in names


def test_genre_injection_history():
    skillkit.reload()
    ctx = skillkit.stage_inject("P2", "novel", genre="历史")
    assert "制度" in ctx or "历史" in ctx


def test_genre_injection_scifi():
    skillkit.reload()
    ctx = skillkit.stage_inject("P2", "novel", genre="科幻")
    assert "代价" in ctx or "推演" in ctx


def test_agents_new_posts():
    assert "deslopper" in agents.AGENTS
    assert "editor" in agents.AGENTS
    assert agents.AGENTS["deslopper"]["stage"] == "review"


def test_deslop_no_findings_keeps_original():
    clean = "她把椅子拉开一点，坐在靠窗的位置。下午的光落在桌面上。"
    out = workflow.deslop_chapter(clean, {"cfg": {}})
    assert out["cleaned"] == clean
    assert out["findings"] == []


def test_deslop_with_findings_has_report():
    # 构造必然命中词表的文本
    bad = "总而言之，这一切都彰显了我们的成功，彰显了伟大的力量，彰显了不可估量的价值。" * 3
    out = workflow.deslop_chapter(bad, {"cfg": {}})
    assert out["report"]["verdict"] in ("pass", "review", "fail", "blocking")
    assert isinstance(out["findings"], list)
    assert out["report"]["stats"]["chars"] > 0


def test_chapter_review_accepts_review():
    # LLM 在 conftest 里被 mock，直接调，验证不抛异常即可
    r = workflow.chapter_review("测试章节内容。", {"genre": "都市"})
    assert isinstance(r, dict)


def test_slop_scan_blocking_sample():
    bad = "这一切证明了我们的成功，彰显了伟大的力量，不可估量的价值，意义重大。" * 4
    r = slop_checker.scan(bad)
    assert r["stats"]["blocking"] > 0
