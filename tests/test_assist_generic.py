"""v2.2.1：通用副驾引擎（诗词 / 散文 / 剧本共用）的单元测试。

覆盖：文档抽象（诗/散=定稿+草稿，剧本=场次）、目标解析、clarify 不烧模型、
段级 patch 生成、落盘只动目标段、旧 hash 防并发、范围越界回退、版本快照与回退、
对话与改稿留档（.assist/*.jsonl）。

所有用例跑在 tmp_path 的假项目上；LLM 由 tests/conftest.py 全局 mock，
本文件内再按需 monkeypatch 成确定性返回。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from bot import assist, poetry_writer, prose_writer, script_writer

KEY_P = "p_test"
KEY_S = "s_test"

P_FINAL = "第一段：霜落满阶，灯影不动。\n第二句也在这里。"
P_DRAFT = "草稿第一段。\n\n草稿第二段。"
P_FINAL_2 = "第二段：风穿过纸窗，响了一下。"

S_FINAL = "定稿第一段。\n\n定稿第二段。\n\n定稿第三段。"
S_DRAFT = "初稿第一段。\n\n初稿第二段。"

CJK = re.compile(r"[\u4e00-\u9fff]")


def _mk_poetry(root):
    pdir = root / KEY_P
    pdir.mkdir(parents=True)
    plan = {"version": 1, "poemName": "夜雨", "form": "wujue", "status": "drafting",
            "meta": {"stage": "S2", "words": 20}}
    (pdir / poetry_writer.F_PLAN).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (pdir / poetry_writer.F_FINAL).write_text(P_FINAL + "\n\n" + P_FINAL_2 + "\n", encoding="utf-8")
    (pdir / poetry_writer.F_DRAFT).write_text(P_DRAFT, encoding="utf-8")
    return pdir


def _mk_prose(root):
    pdir = root / KEY_S
    pdir.mkdir(parents=True)
    plan = {"version": 1, "proseName": "旧巷", "genre": "essay", "status": "drafting",
            "meta": {"stage": "S2", "words": 20}}
    (pdir / prose_writer.F_PLAN).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (pdir / prose_writer.F_FINAL).write_text(S_FINAL, encoding="utf-8")
    (pdir / prose_writer.F_DRAFT).write_text(S_DRAFT, encoding="utf-8")
    return pdir


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    _mk_poetry(tmp_path)
    _mk_prose(tmp_path)
    monkeypatch.setattr(poetry_writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(prose_writer, "PROJECTS_DIR", str(tmp_path))
    return tmp_path


def _plan(root, name, cls):
    return json.loads((root / name / cls.F_PLAN).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 文档抽象


def test_docs_for_poetry_and_prose(proj):
    pdocs = assist.docs_for(KEY_P, "poetry")
    assert [d["id"] for d in pdocs] == ["final", "draft"]
    assert pdocs[0]["title"] == "定稿"

    sdocs = assist.docs_for(KEY_S, "prose")
    assert [d["id"] for d in sdocs] == ["final", "draft"]
    assert sdocs[0]["title"] == "定稿"
    assert sdocs[1]["title"] == "初稿"


def test_resolve_doc_returns_absolute_path(proj):
    doc = assist.resolve_doc(KEY_P, "poetry", "final")
    assert doc and os.path.isabs(doc["path"])
    assert doc["path"].endswith(poetry_writer.F_FINAL)
    assert assist.resolve_doc(KEY_P, "poetry", "nope") is None


def test_parse_target_for_flat_medium():
    assert assist.parse_target("把定稿第2段改得更有画面感", "poetry") == {"doc": "final", "block": 2}
    assert assist.parse_target("草稿第3段", "prose") == {"doc": "draft", "block": 3}
    assert assist.parse_target("改一下第2段", "prose") == {"doc": None, "block": 2}
    assert assist.parse_target("随便聊聊", "poetry") == {"doc": None, "block": None}


# ---------------------------------------------------------------- 澄清分支


def test_clarify_without_target_does_not_call_llm(proj, monkeypatch):
    calls = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise AssertionError("clarify 分支不应调用 LLM")

    monkeypatch.setattr(assist.llm, "chat", boom)
    r = assist.assist(KEY_P, "poetry", "改得更有画面感")
    assert r["ok"] is True
    assert r["intent"]["kind"] == "clarify"
    assert "第2段" in r["reply"]
    assert not calls


def test_clarify_lists_segments_of_target_doc(proj, monkeypatch):
    monkeypatch.setattr(assist.llm, "chat",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调模型")))
    r = assist.assist(KEY_P, "poetry", "改得更有画面感", focus={"doc": "final"})
    assert r["intent"]["kind"] == "clarify"
    assert "2 段" in r["reply"]


# ---------------------------------------------------------------- 生成 patch


def test_assist_edit_builds_patch_for_poetry(proj, monkeypatch):
    seen = {}

    def fake(msgs, **kwargs):
        seen["msgs"] = msgs
        return "改后的第二段：风穿过纸窗，响了一下。"

    monkeypatch.setattr(assist.llm, "chat", fake)
    r = assist.assist(KEY_P, "poetry", "把定稿第2段改得更有画面感")
    assert r["ok"] is True
    assert r["intent"]["kind"] == "edit"
    assert r["intent"]["doc"] == "final"
    assert r["intent"]["block"] == 2
    patch = r["patch"]
    assert patch["old"] == P_FINAL_2
    assert patch["new"].startswith("改后的第二段")
    assert patch["base_hash"] == assist.block_hash(P_FINAL_2)
    # 诗词的提示词不能带剧本那套场景头禁令
    sys_text = seen["msgs"][0]["content"]
    assert "诗" in sys_text
    assert "场景头" not in sys_text


def test_assist_edit_rejects_empty_model_output(proj, monkeypatch):
    monkeypatch.setattr(assist.llm, "chat", lambda *a, **k: "   \n")
    r = assist.assist(KEY_P, "poetry", "把定稿第2段改好")
    assert r["ok"] is False
    assert r["patch"] is None


def test_assist_edit_rejects_overlong_output(proj, monkeypatch):
    monkeypatch.setattr(assist.llm, "chat", lambda *a, **k: "长" * 4000)
    r = assist.assist(KEY_P, "poetry", "把定稿第2段改好")
    assert r["ok"] is False
    assert "过长" in r["reply"]


def test_discussion_does_not_write_disk(proj, monkeypatch):
    monkeypatch.setattr(assist.llm, "chat", lambda *a, **k: "可以把这句的意象再压一层。")
    before = (proj / KEY_P / poetry_writer.F_FINAL).read_text(encoding="utf-8")
    r = assist.assist(KEY_P, "poetry", "为什么第二段这样写", focus={"doc": "final"})
    assert r["ok"] is True
    assert r["intent"]["kind"] == "discussion"
    assert (proj / KEY_P / poetry_writer.F_FINAL).read_text(encoding="utf-8") == before


# ---------------------------------------------------------------- 落盘


def test_apply_edit_changes_only_target_block(proj):
    path = proj / KEY_P / poetry_writer.F_FINAL
    before = path.read_text(encoding="utf-8")
    res = assist.apply_edit(KEY_P, "poetry", "final", 2, assist.block_hash(P_FINAL_2), "新的第二段。")
    assert res["ok"] is True
    after = path.read_text(encoding="utf-8")
    assert "新的第二段。" in after
    assert P_FINAL_2 not in after
    # 第一段逐字不动
    assert after.split("\n\n")[0] == before.split("\n\n")[0]


def test_apply_edit_rejects_stale_hash(proj):
    path = proj / KEY_P / poetry_writer.F_FINAL
    before = path.read_text(encoding="utf-8")
    res = assist.apply_edit(KEY_P, "poetry", "final", 2, "deadbeef", "新的第二段。")
    assert res["ok"] is False
    assert res["code"] == "stale"
    assert path.read_text(encoding="utf-8") == before


def test_apply_edit_rejects_out_of_range_block(proj):
    res = assist.apply_edit(KEY_P, "poetry", "final", 99, "x", "新的。")
    assert res["ok"] is False
    assert "找不到第 99 段" in res["error"]


def test_apply_edit_rolls_back_when_scope_breached(proj, monkeypatch):
    path = proj / KEY_P / poetry_writer.F_FINAL
    before = path.read_text(encoding="utf-8")
    orig_join = assist._join_blocks

    def bad_join(blocks):
        tampered = list(blocks)
        tampered[0] = "被越界改写的第一段"
        return orig_join(tampered)

    monkeypatch.setattr(assist, "_join_blocks", bad_join)
    res = assist.apply_edit(KEY_P, "poetry", "final", 2, assist.block_hash(P_FINAL_2), "新的第二段。")
    assert res["ok"] is False
    assert res["error"] == "改写范围越界，已回退"
    assert path.read_text(encoding="utf-8") == before


def test_apply_edit_updates_plan_word_count(proj):
    res = assist.apply_edit(KEY_P, "poetry", "final", 2, assist.block_hash(P_FINAL_2), "新的第二段。")
    assert res["ok"] is True
    plan = _plan(proj, KEY_P, poetry_writer)
    assert plan["meta"]["docWords"]["final"] == res["wordCount"]
    assert plan["meta"]["words"] == res["wordCount"]


def test_apply_edit_on_draft_keeps_final_word_count(proj):
    plan = _plan(proj, KEY_P, poetry_writer)
    plan["meta"]["words"] = 999
    (proj / KEY_P / poetry_writer.F_PLAN).write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    res = assist.apply_edit(KEY_P, "poetry", "draft", 1, assist.block_hash("草稿第一段。"), "新的草稿。")
    assert res["ok"] is True
    plan = _plan(proj, KEY_P, poetry_writer)
    assert plan["meta"]["docWords"]["draft"] == res["wordCount"]
    assert plan["meta"]["words"] == 999


# ---------------------------------------------------------------- 版本快照


def test_versions_and_restore_roundtrip(proj):
    res = assist.apply_edit(KEY_P, "poetry", "final", 2, assist.block_hash(P_FINAL_2), "新的第二段。")
    assert res["ok"] is True
    ver = res["version"]
    assert ver.startswith("final-")

    listed = assist.list_versions(KEY_P, "poetry", "final")
    assert listed["ok"] is True
    assert ver in [i["version"] for i in listed["items"]]
    assert listed["items"][0]["at"].count(":") == 2

    # 别的文档的版本不算
    assert assist.list_versions(KEY_P, "poetry", "draft")["items"] == []

    back = assist.restore_version(KEY_P, "poetry", "final", ver)
    assert back["ok"] is True
    assert P_FINAL_2 in (proj / KEY_P / poetry_writer.F_FINAL).read_text(encoding="utf-8")


def test_restore_rejects_path_traversal(proj):
    res = assist.restore_version(KEY_P, "poetry", "final", "../../etc/passwd")
    assert res["ok"] is False
    assert res["error"] == "版本文件名不合法"


def test_restore_rejects_other_doc_version(proj):
    res = assist.apply_edit(KEY_P, "poetry", "draft", 1, assist.block_hash("草稿第一段。"), "新的草稿。")
    assert res["ok"] is True
    bad = assist.restore_version(KEY_P, "poetry", "final", res["version"])
    assert bad["ok"] is False


def test_versions_dir_for_prose_is_project_root(proj):
    res = assist.apply_edit(KEY_S, "prose", "final", 2, assist.block_hash("定稿第二段。"), "新的第二段。")
    assert res["ok"] is True
    vdir = proj / KEY_S / ".versions"
    assert vdir.is_dir()
    assert (vdir / res["version"]).is_file()


# ---------------------------------------------------------------- 留档


def test_chat_and_edit_are_logged(proj, monkeypatch):
    monkeypatch.setattr(assist.llm, "chat", lambda *a, **k: "可以再压一层。")
    assist.log_chat(KEY_P, "poetry", "user", "第二段怎么改", kind="discussion")
    assist.log_chat(KEY_P, "poetry", "assistant", "可以再压一层。", kind="discussion")
    assist.apply_edit(KEY_P, "poetry", "final", 2, assist.block_hash(P_FINAL_2), "新的第二段。")

    h = assist.history(KEY_P, "poetry")
    assert h["ok"] is True
    assert [m["role"] for m in h["messages"]] == ["user", "assistant"]
    assert h["messages"][0]["content"] == "第二段怎么改"
    assert len(h["edits"]) == 1
    assert h["edits"][0]["doc"] == "final"
    assert h["edits"][0]["block"] == 2
    assert h["edits"][0]["old"] == P_FINAL_2
    assert h["edits"][0]["new"] == "新的第二段。"


def test_history_of_other_medium_is_separate(proj):
    assist.log_chat(KEY_P, "poetry", "user", "诗词里的话")
    h = assist.history(KEY_S, "prose")
    assert h["ok"] is True
    assert h["messages"] == []


def test_assist_files_live_in_project_assist_dir(proj):
    assist.log_chat(KEY_P, "poetry", "user", "你好")
    d = proj / KEY_P / ".assist"
    assert (d / "chat.jsonl").is_file()
    rows = [json.loads(x) for x in (d / "chat.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert rows[-1]["content"] == "你好"


# ---------------------------------------------------------------- 剧本通用层


def test_script_docs_map_to_scenes(proj):
    plan = {"version": 1, "scriptName": "雨夜", "scenes": [
        {"no": 1, "kind": "内", "place": "旧公寓", "time": "夜", "fileName": "第001场.md"},
        {"no": 2, "kind": "外", "place": "巷口", "time": "夜", "fileName": "第002场.md"},
    ]}
    body = proj / "s_x"
    body.mkdir()
    (body / script_writer.F_PLAN).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (body / script_writer.F_BODY).mkdir()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(script_writer, "PROJECTS_DIR", str(proj))
    try:
        docs = assist.docs_for("s_x", "script")
        assert [d["id"] for d in docs] == ["1", "2"]
        assert docs[0]["kind"] == "scene"
    finally:
        monkey.undo()
