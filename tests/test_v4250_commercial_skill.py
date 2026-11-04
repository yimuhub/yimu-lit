"""v4.25.0：商业化连载技能融合回归。

覆盖：4 个新技能文件入库可注入（故事引擎/因果爽点/平台适配/三轮编辑）、
写作系统提示含因果与爽点纪律、评分 rubric 含章节五项快评、
规划 prompt（人物/大纲）含故事引擎与承诺契约要求。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from bot import skillkit


# ---------- 技能库入库 ----------

def test_new_skill_files_indexed():
    names = {sf.name for sf in skillkit.index()}
    for expect in ("novel-story-engine", "novel-causality-satisfaction",
                   "novel-platform-fit", "novel-editor-three-pass"):
        assert expect in names, f"技能库缺 {expect}"


def test_stage_inject_picks_story_engine_for_planning():
    blk = skillkit.stage_inject("P0", "novel", genre="mystery")
    assert "story-engine" in blk or "故事引擎" in blk


def test_stage_inject_picks_causality_for_writing():
    blk = skillkit.stage_inject("P2", "novel", genre="mystery")
    assert "因果" in blk or "causality" in blk


def test_validate_all_no_errors():
    res = skillkit.validate_all()
    assert res.get("ok") is True, res


# ---------- 引擎接线 ----------

def test_writing_system_has_causality_discipline():
    ctx = {"cfg": {"genre": "悬疑/刑侦", "genre_key": "mystery"},
           "chapter_no": 5, "style_text": "文风", "characters_text": "人物",
           "outline_text": "大纲"}
    from bot.workflow import _writing_system
    sp = _writing_system(ctx)
    assert "因果与爽点纪律" in sp
    assert "单章任务底线" in sp


def test_score_rubric_has_five_item_quick_score():
    from bot.workflow import SCORE_RUBRIC
    assert "章节五项快评" in SCORE_RUBRIC
    for kw in ("本章任务", "真实事件变化", "主动行动", "回报", "引出下一步"):
        assert kw in SCORE_RUBRIC, f"rubric 缺 {kw}"


def test_gen_outline_requires_story_promise(monkeypatch):
    import bot.workflow as wf
    captured = {}

    def _fake_chat(msgs, *a, **k):
        captured["sys"] = msgs[0]["content"]
        captured["user"] = msgs[1]["content"]
        return "大纲"

    monkeypatch.setattr(wf.llm, "chat", _fake_chat)
    wf.gen_outline({"genre": "悬疑", "chapters": 3, "medium": "novel"})
    assert "故事承诺" in captured["sys"]
    assert "故事引擎" in captured["user"]
    assert "平台适配" in captured["user"]


def test_gen_characters_requires_five_elements(monkeypatch):
    import bot.workflow as wf
    captured = {}

    def _fake_chat(msgs, *a, **k):
        captured["sys"] = msgs[0]["content"]
        captured["user"] = msgs[1]["content"]
        return "人物档案"

    monkeypatch.setattr(wf.llm, "chat", _fake_chat)
    wf.gen_characters({"genre": "悬疑", "medium": "novel"})
    assert "五要素" in captured["sys"]
    assert "故事引擎" in captured["user"]
