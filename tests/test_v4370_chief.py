# v4.37.0：总编辑中枢回归。
#
# 这一版的核心不是「多认几个关键词」，而是：作者说人话 → 总编辑先理解 →
# 翻译成各岗位能执行的导演工作单。测试重点：
#   1. 意见会被翻译成工作单（不是原样丢进创作队列）；
#   2. 点名改稿时，改写师收到的是翻译后的验收标准；
#   3. 模型不可用时，旧的标题/规划兜底仍然可用。
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import app as appmod
from bot import brief as brief_mod
from bot import chief as chief_mod
from bot import security, writer

KEY = "20260101-000000-旧书名"


def _mk_proj(tmp_path):
    pdir = tmp_path / KEY
    pdir.mkdir()
    (pdir / "02-写作计划.json").write_text(json.dumps({
        "novelName": "旧书名", "status": "in_progress",
        "chapters": [{"chapterNumber": i, "title": f"第{i}章",
                      "status": "completed" if i <= 3 else "pending",
                      "wordCount": 3000 if i <= 3 else None}
                     for i in range(1, 6)],
    }, ensure_ascii=False), encoding="utf-8")
    return writer.Project(str(pdir), KEY)


def _clean():
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    writer._RUNNING_BOOKS.clear()


@pytest.fixture
def env(monkeypatch, tmp_path):
    proj = _mk_proj(tmp_path)
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(appmod, "_get_project",
                        lambda key: proj if key == KEY else None)
    monkeypatch.setattr(appmod.writer, "PROJECTS_DIR", str(tmp_path))
    _clean()
    yield proj
    _clean()


def test_brief_scope_and_role_rendering(tmp_path):
    pdir = tmp_path / "book"
    pdir.mkdir()
    brief_mod.add(str(pdir), {
        "goal": "女主不能为了推进剧情降智",
        "must_keep": ["她的判断力"],
        "must_change": ["为冲突强行让她犯错"],
        "acceptance": ["关键选择有她自己的动机"],
        "avoid": ["降智推动剧情"],
        "scope": "chapters", "chapters": [7],
    }, source_text="第7章女主太蠢了")
    brief_mod.add(str(pdir), {
        "goal": "全书的雨景不要写成天气播报",
        "scope": "book",
    }, source_text="别老写雨")

    got7 = [x["goal"][:2] for x in brief_mod.applicable(str(pdir), 7)]
    got8 = [x["goal"][:2] for x in brief_mod.applicable(str(pdir), 8)]
    assert sorted(got7) == ["全书", "女主"]
    assert got8 == ["全书"]

    writer_block = brief_mod.render_block(str(pdir), "writer", chapter=7)
    reviewer_block = brief_mod.render_block(str(pdir), "reviewer", chapter=7)
    planner_block = brief_mod.render_block(str(pdir), "planner", chapter=7)
    assert "总编辑工作单" in writer_block and "降智" in writer_block
    assert "审稿依据" in reviewer_block and "验收标准" in reviewer_block
    assert "结构要求" in planner_block


def test_chief_turns_author_opinion_into_work_order(env, monkeypatch):
    decision = {
        "understanding": "作者认为女主为了推剧情被写蠢了，这是长期人物要求",
        "reply": "我明白：你要的是她始终按自己的动机做选择，不能再为了赶剧情降智。",
        "actions": [{
            "action": "directive",
            "params": {"brief": {
                "goal": "女主的判断力始终在线，不得为了推进剧情降智",
                "must_keep": ["女主前期建立的判断力"],
                "must_change": ["为了制造冲突让她做蠢决定"],
                "acceptance": ["关键选择能看出她自己的动机"],
                "avoid": ["降智推动剧情"],
                "scope": "book",
            }},
        }],
        "confidence": 0.9,
    }
    monkeypatch.setattr(chief_mod, "deliberate", lambda text, ctx: decision)
    _clean()

    r = appmod.chat("s-chief", "女主越来越不像她了，别让她为了推进剧情降智", KEY)

    assert "我明白" in r and "记下了" in r
    items = brief_mod.load(env.path)
    assert len(items) == 1
    assert items[0]["scope"] == "book"
    assert "降智" in items[0]["goal"]
    assert "总编辑工作单" in brief_mod.render_block(env.path, "writer", chapter=4)


def test_chief_rewrite_passes_translated_criteria(env, monkeypatch):
    seen = {}

    def _fake_rewrite(key, chapters=None, dimensions=None, max_rounds=6):
        seen.update(key=key, chapters=chapters, dims=dimensions)
        return True

    decision = {
        "understanding": "作者点名第3章，嫌节奏拖、女主反应不真实",
        "reply": "第3章我按「节奏紧、女主反应像她本人」这两条来改。",
        "actions": [{
            "action": "rewrite_chapters",
            "params": {"chapters": [3], "brief": {
                "goal": "第3章节奏收紧，女主的反应必须符合她前两章的性格",
                "must_keep": ["女主已有的说话方式"],
                "must_change": ["拖沓的过场描写"],
                "acceptance": ["开篇 300 字内进入冲突", "女主的选择有前置动机"],
                "scope": "chapters", "chapters": [3],
            }},
        }],
        "confidence": 0.88,
    }
    monkeypatch.setattr(chief_mod, "deliberate", lambda text, ctx: decision)
    monkeypatch.setattr(appmod, "_start_rewrite", _fake_rewrite)
    _clean()

    r = appmod.chat("s-chief2", "第3章太拖了，女主反应也不像她", KEY)

    assert seen["chapters"] == [3]
    assert any("总编辑工作单" in d for d in seen["dims"])
    assert any("开篇 300 字内进入冲突" in d for d in seen["dims"])
    assert "第3章局部重写" in r
    assert brief_mod.open_items(env.path)[0]["scope"] == "chapters"


def test_chief_can_revoke_a_work_order(env, monkeypatch):
    add = {
        "understanding": "作者要求全书不要写成天气播报",
        "reply": "记下了。",
        "actions": [{"action": "directive", "params": {"brief": {
            "goal": "雨景不要写成天气播报", "scope": "book"}}}],
        "confidence": 0.8,
    }
    monkeypatch.setattr(chief_mod, "deliberate", lambda text, ctx: add)
    _clean()
    appmod.chat("s-revoke", "别老写雨", KEY)
    assert len(brief_mod.open_items(env.path)) == 1

    drop = {
        "understanding": "作者撤回了刚才那条雨景要求",
        "reply": "好，撤掉。",
        "actions": [{"action": "revoke_briefs", "params": {"index": 1}}],
        "confidence": 0.9,
    }
    monkeypatch.setattr(chief_mod, "deliberate", lambda text, ctx: drop)
    r = appmod.chat("s-revoke", "刚才那条不算了", KEY)
    assert "撤掉了" in r
    assert brief_mod.open_items(env.path) == []


def test_chief_unavailable_falls_back_to_title_rule(env, monkeypatch):
    monkeypatch.setattr(chief_mod, "deliberate", lambda text, ctx: None)
    monkeypatch.setattr(appmod, "_chat_title_cmd",
                        lambda s, key, msg: "TITLE_FALLBACK")
    _clean()
    assert appmod.chat("s-fallback", "把书名改成《长夜》", KEY) == "TITLE_FALLBACK"
