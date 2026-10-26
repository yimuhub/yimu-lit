"""v2.2.0 P5：剧本场/段级定向改写引擎的单元测试。

覆盖：目标解析（含中文数字）、分块与 hash、clarify 不烧模型、段级 patch 生成、
场景头拒绝、落盘只动目标段、旧 hash 防并发、范围越界回退、版本恢复与路径穿越防护。

所有用例跑在 tmp_path 的假项目上；LLM 由 tests/conftest.py 全局 mock，
本文件内再按需 monkeypatch 成确定性返回。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from bot import assist as assist_mod
from bot import script_assist, script_writer
from bot.script_assist import (
    apply_edit,
    assist,
    block_hash,
    list_versions,
    parse_target,
    restore_version,
    split_blocks,
)

KEY = "s_test"
B1 = "第一段：他把伞收好，放进门口的桶里。\n雨还在下。"
B2 = "第二段：他听见楼上有脚步声，停了很久。"
B3 = "第三段：\n他压低声音问了一句。\n没人回答。"
B4 = "第四段：灯灭了。"
BLOCKS = [B1, B2, B3, B4]
BODY = "\n\n".join(BLOCKS) + "\n"
CJK = re.compile(r"[\u4e00-\u9fff]")


def _scenes():
    return [
        {"no": 1, "kind": "内", "place": "旧公寓", "time": "夜",
         "status": "completed", "wordCount": 888, "fileName": "第001场.md"},
        {"no": 2, "kind": "外", "place": "巷口", "time": "夜",
         "status": "completed", "wordCount": 16, "fileName": "第002场.md"},
        {"no": 3, "kind": "内", "place": "车站", "time": "日",
         "status": "pending", "wordCount": None, "fileName": "第003场.md"},
    ]


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """假剧本项目：第 1 场 4 段（其中两段含段内换行），第 2 场 2 段，第 3 场未写。"""
    pdir = tmp_path / KEY
    body = pdir / script_writer.F_BODY
    body.mkdir(parents=True)
    plan = {
        "version": 1, "scriptName": "雨夜", "medium": "script", "status": "in_progress",
        "scenes": _scenes(),
        "meta": {"stage": "S5", "setting": {"era": "contemporary", "era_label": "当代",
                                            "world": "南方小城，雨季连绵"}},
    }
    (pdir / script_writer.F_PLAN).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (body / "第001场.md").write_text(BODY, encoding="utf-8")
    (body / "第002场.md").write_text("第二场第一段。\n\n第二场第二段。\n", encoding="utf-8")
    (pdir / script_writer.F_CHARS).write_text(
        "# 雨夜 人物小传\n\n* **姓名/年龄职业/外形**：林默，42岁，夜班调度员。\n"
        "* **姓名/年龄职业/外形**：周岚，38岁，护士。\n", encoding="utf-8")
    (pdir / script_writer.F_BIBLE).write_text("# 雨夜 · 创作圣经\n\n小城雨季，所有人都欠着一句道歉。\n",
                                             encoding="utf-8")
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(tmp_path))
    return pdir


def _scene_path(project):
    return project / script_writer.F_BODY / "第001场.md"


def _plan(project):
    return json.loads((project / script_writer.F_PLAN).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 解析


@pytest.mark.parametrize("text,scene,block", [
    ("把第3场第2段改得更紧张", 3, 2),
    ("第三场第二段", 3, 2),
    ("第 3 场 2 段", 3, 2),
    ("场3段2", 3, 2),
    ("第3场", 3, None),
    ("第2段", None, 2),
    ("第三场", 3, None),
    ("第十二场第三段", 12, 3),
    ("第二十三段", None, 23),
    ("第二十场", 20, None),
    ("把第二场第四段删掉最后一句", 2, 4),
    ("随便聊聊这场戏", None, None),
])
def test_parse_target(text, scene, block):
    assert parse_target(text) == {"scene": scene, "block": block}


def test_parse_target_never_reads_scene_as_block():
    """「第3场第2段」的 block 必须是 2，不能把场号 3 当成段号。"""
    parsed = parse_target("把第3场第2段改得更紧张")
    assert parsed == {"scene": 3, "block": 2}


def test_parse_target_cn_zero_and_liang():
    assert parse_target("第一场第二段") == {"scene": 1, "block": 2}
    assert parse_target("第两百场") == {"scene": 200, "block": None}
    assert parse_target("第一百零二场第一段") == {"scene": 102, "block": 1}


# ---------------------------------------------------------------- 分块 / hash


def test_split_blocks_keeps_inner_newlines():
    text = "A\n第二行\n\n\nB  \n\n\nC\n"
    assert split_blocks(text) == ["A\n第二行", "B", "C"]
    assert split_blocks("") == []
    assert split_blocks("\n \n\t\n") == []


def test_block_hash_is_stable_and_whitespace_insensitive():
    assert block_hash(" 同一段 \n") == block_hash("同一段")
    assert len(block_hash("同一段")) == 16
    assert block_hash(B1) != block_hash(B2)


# ---------------------------------------------------------------- assist


def test_clarify_scene_only_does_not_call_llm(project, monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("clarify 分支不应调用 LLM")

    monkeypatch.setattr(script_assist.llm, "chat", boom)

    r = assist(KEY, "把第1场改得更紧张")
    assert r["ok"] is True
    assert r["intent"] == {"kind": "clarify", "scene": 1, "block": None}
    assert r["patch"] is None
    assert "4 段" in r["reply"] and "第1段" in r["reply"]

    r2 = assist(KEY, "改得更紧张")
    assert r2["intent"] == {"kind": "clarify", "scene": None, "block": None}
    assert "第3场第2段" in r2["reply"]
    assert calls == []


def test_clarify_block_out_of_range(project, monkeypatch):
    monkeypatch.setattr(script_assist.llm, "chat",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 LLM")))
    r = assist(KEY, "第1场第9段改得更紧张")
    assert r["intent"]["kind"] == "clarify"
    assert r["intent"]["block"] == 9
    assert "只有 4 段" in r["reply"]


def test_discussion_gets_bible_scene_and_setting(project, monkeypatch):
    seen = {}

    def fake(msgs, **kwargs):
        seen["msgs"] = msgs
        seen["kwargs"] = kwargs
        return "建议把第二段的脚步声提前到第一句，让悬念更早出现。"

    monkeypatch.setattr(script_assist.llm, "chat", fake)
    r = assist(KEY, "为什么第1场要这样写", focus={"scene": 1, "block": 2})
    assert r["ok"] is True
    assert r["intent"] == {"kind": "discussion", "scene": 1, "block": 2}
    assert r["patch"] is None
    assert "脚步声" in r["reply"]
    system = seen["msgs"][0]["content"]
    assert "没有修改任何文件" in system
    assert "小城雨季" in system          # 圣经前 2500 字
    assert "南方小城，雨季连绵" in system  # 世界观硬约束
    assert B2 in system                  # 当前场正文前 2500 字
    assert seen["msgs"][-1] == {"role": "user", "content": "为什么第1场要这样写"}


def test_assist_edit_builds_patch_for_target_block(project, monkeypatch):
    captured = {}
    new_block = "第二段：他听见楼上有人下来，脚步踩在积水里。"

    def fake(msgs, **kwargs):
        captured["msgs"] = msgs
        captured["kwargs"] = kwargs
        return "```\n好的，以下是改写：\n" + new_block + "\n```"

    monkeypatch.setattr(script_assist.llm, "chat", fake)
    r = assist(KEY, "把第1场第2段改得更紧张", cfg={"temperature": 0.2})

    assert r["ok"] is True
    assert r["intent"] == {"kind": "edit", "scene": 1, "block": 2}
    patch = r["patch"]
    assert patch["scene"] == 1 and patch["block"] == 2
    assert patch["old"] == B2
    assert patch["new"] == new_block        # 围栏与「好的，以下是」都被清掉
    assert patch["base_hash"] == block_hash(B2)
    assert "旧公寓" in patch["scene_title"]

    system = captured["msgs"][0]["content"]
    user = captured["msgs"][1]["content"]
    assert "只输出替换后的这一段" in system
    assert "不要场景头" in system
    assert "林默" in system                  # 人物名取自 01-人物小传.md
    assert "南方小城，雨季连绵" in system     # 世界观硬约束
    assert B1 in user and B2 in user and B3 in user  # 前一段 / 原文 / 后一段
    assert "把第1场第2段改得更紧张" in user
    assert captured["kwargs"]["stage"] == "writing"


def test_assist_edit_rejects_heading_and_empty(project, monkeypatch):
    monkeypatch.setattr(script_assist.llm, "chat",
                        lambda *a, **k: "内景  天台  夜\n他站在雨里。")
    r = assist(KEY, "把第1场第2段改得更紧张")
    assert r["ok"] is False
    assert r["patch"] is None
    assert r["intent"]["kind"] == "edit"
    assert "场景头" in r["reply"]

    monkeypatch.setattr(script_assist.llm, "chat", lambda *a, **k: "   \n")
    r2 = assist(KEY, "把第1场第2段改得更紧张")
    assert r2["ok"] is False and r2["patch"] is None
    assert r2["intent"] == {"kind": "edit", "scene": 1, "block": 2}


def test_assist_edit_llm_failure_returns_error(project, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("429 限流")

    monkeypatch.setattr(script_assist.llm, "chat", boom)
    r = assist(KEY, "把第1场第2段改得更紧张")
    assert r["ok"] is False
    assert r["patch"] is None
    assert r["reply"].startswith("模型调用失败：")
    assert r["intent"]["kind"] == "edit"


# ---------------------------------------------------------------- apply_edit


def test_apply_edit_changes_only_target_block(project):
    new_block = "第二段：楼道里传来第二声脚步。"
    r = apply_edit(KEY, 1, 2, block_hash(B2), new_block)

    assert r["ok"] is True
    assert r["scene"] == 1 and r["block"] == 2
    assert r["wordCount"] == len(CJK.findall(r["text"]))

    blocks = split_blocks(_scene_path(project).read_text(encoding="utf-8"))
    assert blocks[1] == new_block
    assert blocks[0] == B1
    assert blocks[2] == B3
    assert blocks[3] == B4

    plan = _plan(project)
    assert plan["scenes"][0]["wordCount"] == len(CJK.findall(
        _scene_path(project).read_text(encoding="utf-8")))
    assert plan["updatedAt"]

    assert script_assist.VERSION_RE.match(r["version"])
    snap = project / script_writer.F_BODY / script_assist.VERSIONS_DIR / r["version"]
    assert snap.exists()
    assert split_blocks(snap.read_text(encoding="utf-8")) == BLOCKS


def test_apply_edit_stale_hash_does_not_write(project):
    before = _scene_path(project).read_text(encoding="utf-8")
    r = apply_edit(KEY, 1, 2, "deadbeefdeadbeef", "第二段：随便改改。")
    assert r["ok"] is False
    assert r["code"] == "stale"
    assert r["error"] == "这段内容已被修改，请刷新后重试"
    assert _scene_path(project).read_text(encoding="utf-8") == before


def test_apply_edit_rolls_back_when_scope_breached(project, monkeypatch):
    """join 阶段越界改动别的段：写后被自证发现，必须恢复旧全文。"""
    before = _scene_path(project).read_text(encoding="utf-8")
    orig_join = script_assist._join_blocks

    def bad_join(blocks):
        tampered = list(blocks)
        tampered[0] = "被越界改写的第一段"
        return orig_join(tampered)

    # 引擎本体在 bot/assist.py，patch 那里才是真正被调用的那份
    monkeypatch.setattr(assist_mod, "_join_blocks", bad_join)
    r = apply_edit(KEY, 1, 2, block_hash(B2), "第二段：新内容。")

    assert r["ok"] is False
    assert r["error"] == "改写范围越界，已回退"
    assert _scene_path(project).read_text(encoding="utf-8") == before
    assert _plan(project)["scenes"][0]["wordCount"] == 888


# ---------------------------------------------------------------- 版本


def test_list_versions_and_restore_roundtrip(project):
    new_block = "第二段：改过的内容。"
    applied = apply_edit(KEY, 1, 2, block_hash(B2), new_block)
    assert applied["ok"] is True

    items = list_versions(KEY, 1)["items"]
    assert [it["version"] for it in items] == [applied["version"]]
    assert items[0]["at"].count(":") == 2
    assert items[0]["words"] == len(CJK.findall(BODY))

    vdir = project / script_writer.F_BODY / script_assist.VERSIONS_DIR
    (vdir / "not-a-version.md").write_text("坏文件", encoding="utf-8")
    (vdir / "第002场-20260101-000000.md").write_text("别的场", encoding="utf-8")
    assert [it["version"] for it in list_versions(KEY, 1)["items"]] == [applied["version"]]

    r = restore_version(KEY, 1, applied["version"])
    assert r["ok"] is True
    assert split_blocks(r["text"]) == BLOCKS
    assert split_blocks(_scene_path(project).read_text(encoding="utf-8")) == BLOCKS
    assert r["wordCount"] == len(CJK.findall(BODY))
    assert _plan(project)["scenes"][0]["wordCount"] == r["wordCount"]
    # 回退前也快照了「改后」版本，所以现在有两个快照
    assert len(list_versions(KEY, 1)["items"]) == 2


@pytest.mark.parametrize("bad", [
    "../第001场-20260101-000000.md",
    "/tmp/第001场-20260101-000000.md",
    "sub/第001场-20260101-000000.md",
    "第001场-20260101-000000.exe",
    "第002场-20260101-000000.md",
])
def test_restore_version_rejects_unsafe_names(project, bad):
    before = _scene_path(project).read_text(encoding="utf-8")
    r = restore_version(KEY, 1, bad)
    assert r["ok"] is False
    assert "不合法" in r["error"] or "不存在" in r["error"]
    assert _scene_path(project).read_text(encoding="utf-8") == before
