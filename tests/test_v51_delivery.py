"""v5.1 修复回归：标题身份、N01 语境白名单、导出互斥、正文口径。"""


class _FakeProj:
    def __init__(self, path):
        self.path = path
        self._plan = {"chapters": [
            {"chapterNumber": 1, "title": "被覆盖的名字", "filePath": "第01章-旧名.md"},
        ]}

    def plan(self):
        return self._plan

    def save_plan(self, plan):
        self._plan = plan

    def log(self, msg):
        pass


def test_chapter_identity_renames_and_writes_h1(tmp_path):
    from bot import chapter_identity
    fp = tmp_path / "第01章-旧名.md"
    fp.write_text("## 章首引子\n\n正文", encoding="utf-8")
    proj = _FakeProj(str(tmp_path))
    res = chapter_identity.sync_project(proj, rename=True, persist=True)
    assert res["renamed"] == 1
    new = tmp_path / "第01章-被覆盖的名字.md"
    assert new.exists() and not fp.exists()
    assert new.read_text(encoding="utf-8").splitlines()[0] == "# 第01章 被覆盖的名字"
    assert proj.plan()["chapters"][0]["filePath"] == new.name


def test_n01_allows_declared_proper_noun():
    from bot import delivery_gate
    text = "他输入 Linjx2014，屏幕亮了。"
    assert any(f["code"] == "N01" for f in delivery_gate.check_chapter(text, chapter_no=1))
    assert not any(f["code"] == "N01" for f in delivery_gate.check_chapter(
        text, chapter_no=1, allowed_latin={"LINJX", "LINJX2014"}))


def test_export_busy_reason_blocks_active_review(monkeypatch):
    import app as appmod
    from bot import livejob
    monkeypatch.setattr(livejob, "get", lambda key: {"kind": "review", "finished": False})
    assert "终审中" in appmod._export_busy_reason("book")
    monkeypatch.setattr(livejob, "get", lambda key: {"kind": "review", "finished": True})
    monkeypatch.setattr(appmod.writer, "pipeline_running", lambda key: False)
    assert appmod._export_busy_reason("book") == ""


def test_check_words_excludes_meta(tmp_path):
    from bot import skill
    fp = tmp_path / "ch.md"
    fp.write_text("# 第01章 测试\n\n## 本章概要\n\n概要不算。\n\n## 正文\n\n" + "字" * 100
                  + "\n\n## 章节备注\n\n备注不算。\n", encoding="utf-8")
    ok, words = skill.check_words(str(fp), 100)
    assert ok and words == 100


def test_parse_cast_supports_renamed_character_headings():
    from bot import story_kernel
    text = ("## 人物档案：沈默（主角）\n\n- 身份/资源：刑警\n\n"
            "## 人物档案：顾维庸（反派）\n\n- 身份/资源：法学家\n")
    cast = story_kernel._parse_cast(text)
    assert [c["name"] for c in cast] == ["沈默", "顾维庸"]


def test_rubric_empty_response_is_unscored_not_zero(monkeypatch):
    from bot import final_review
    monkeypatch.setattr(final_review.llm, "chat_json", lambda *a, **k: {})
    r = final_review._rubric_score("正文", "news", {})
    assert r["total"] is None
    assert r["verdict"] == "评分失败"


def test_non_episodic_script_skips_episode_budget_and_once_ratio():
    from bot import delivery_gate
    text = ("第1场 婚礼现场 夜 内\n\n"
            "李雷把请柬翻过来。\n"
            "韩梅梅：这个名字不该出现在这里。\n"
            "李雷：我知道。\n")
    r = delivery_gate.check_delivery(
        [{"n": 1, "text": text, "title": "第1场"}],
        title="暴雨夜", kind="script", profile="universal", script_form="film")
    assert r["counts"]["block"] == 0
    assert not [f for f in r["findings"] if f["code"] in ("S02", "S04")]
