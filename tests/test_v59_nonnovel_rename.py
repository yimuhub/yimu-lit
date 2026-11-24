import json
import os

import pytest

from bot import (
    children_writer,
    copy_writer,
    mini_writer_base,
    news_writer,
    other_writer,
    poetry_writer,
    prose_writer,
    safeio,
    script_writer,
    thesis_writer,
    writer,
    livejob,
)


PROJECT_CASES = [
    (children_writer.ChildrenProject, children_writer.F_PLAN),
    (news_writer.NewsProject, news_writer.F_PLAN),
    (copy_writer.CopyProject, copy_writer.F_PLAN),
    (other_writer.OtherProject, other_writer.F_PLAN),
    (script_writer.ScriptProject, script_writer.F_PLAN),
    (poetry_writer.PoetryProject, poetry_writer.F_PLAN),
    (prose_writer.ProseProject, prose_writer.F_PLAN),
    (thesis_writer.ThesisProject, thesis_writer.F_PLAN),
]


@pytest.fixture
def rename_env(tmp_path, monkeypatch):
    rename_file = tmp_path / "rename_map.json"
    monkeypatch.setattr(writer, "RENAME_MAP_FILE", str(rename_file))
    writer._RENAME_MAP_CACHE.update(mtime=None, map={})
    monkeypatch.setattr(mini_writer_base.MiniWriterProject, "PROJECTS_DIR", str(tmp_path))
    for module in (script_writer, poetry_writer, prose_writer, thesis_writer):
        monkeypatch.setattr(module, "PROJECTS_DIR", str(tmp_path))
    return tmp_path


def _rename_project(root, prefix="c"):
    old = root / f"{prefix}_old"
    new = root / f"{prefix}_new"
    old.mkdir()
    os.rename(old, new)
    writer.record_rename(old.name, new.name)
    return old, new


@pytest.mark.parametrize("project_cls,plan_file", PROJECT_CASES)
def test_project_log_plan_and_live_path_follow_rename(rename_env, project_cls, plan_file):
    old, new = _rename_project(rename_env, project_cls.__name__[0].lower())
    proj = project_cls(str(old), "旧名")
    proj.save_plan({"name": "旧名"})
    proj.log("改名后的日志")
    proj.save_plan({"name": "新名", "config": {"_model_bound": True}})

    assert not old.exists()
    assert "改名后的日志" in (new / "运行日志.txt").read_text(encoding="utf-8")
    assert json.loads((new / plan_file).read_text(encoding="utf-8"))["name"] == "新名"

    live_path = safeio.ProjectPath(proj)
    safeio.atomic_write_text(os.path.join(live_path, "阶段产物.md"), "新目录内容")
    assert (new / "阶段产物.md").read_text(encoding="utf-8") == "新目录内容"
    assert not old.exists()


def test_script_body_dir_does_not_recreate_old_directory(rename_env):
    old, new = _rename_project(rename_env, "s")
    proj = script_writer.ScriptProject(str(old), "旧剧本")
    assert proj.body_dir() == os.path.join(str(new), script_writer.F_BODY)
    assert os.path.isdir(proj.body_dir())
    assert not old.exists()


@pytest.mark.parametrize(
    "project_cls,module,plan_file",
    [(case[0], case[0].__module__, case[1]) for case in PROJECT_CASES],
)
def test_nonnovel_get_project_resolves_rename_alias(rename_env, project_cls, module, plan_file):
    module = __import__(module, fromlist=["get_project"])
    old, new = _rename_project(rename_env, project_cls.__name__[0].lower())
    (new / plan_file).write_text('{"name":"测试"}', encoding="utf-8")

    proj = module.get_project(old.name)
    assert proj is not None
    assert os.path.realpath(proj.path) == os.path.realpath(new)
    assert not old.exists()


def test_pipeline_and_livejob_follow_project_rename(rename_env, monkeypatch):
    old = rename_env / "c_old"
    new = rename_env / "c_new"
    old.mkdir()
    proj = children_writer.ChildrenProject(str(old), "旧故事")
    monkeypatch.setattr(livejob, "_ACTIVE", {})
    writer.mark_pipeline(old.name, True)
    livejob.begin(old.name, detail="改名测试")
    try:
        os.rename(old, new)
        writer.record_rename(old.name, new.name)
        assert proj.path == str(new)
        assert writer._PIPELINE_COUNT.get(old.name, 0) == 0
        assert writer._PIPELINE_COUNT.get(new.name) == 1
        assert livejob.is_active(new.name)
        assert not livejob.is_active(old.name)
        writer.mark_pipeline(old.name, False)
        assert writer._PIPELINE_COUNT.get(new.name, 0) == 0
    finally:
        writer.mark_pipeline(old.name, False)
        writer.mark_pipeline(new.name, False)
        livejob.clear(old.name)
        livejob.clear(new.name)


def test_children_stage_write_refreshes_body_path_after_mid_call_rename(rename_env, monkeypatch):
    old = rename_env / "c_old"
    new = rename_env / "c_new"
    old.mkdir()
    proj = children_writer.ChildrenProject(str(old), "儿童故事")
    plan = {"outline": {"sections": ["小猫出发"], "outline": "小猫去森林"}, "meta": {}}

    def fake_chat(*args, **kwargs):
        os.rename(old, new)
        writer.record_rename(old.name, new.name)
        return "小猫沿着森林小路勇敢前进，帮助迷路的小鸟找到家人。" * 8

    monkeypatch.setattr(children_writer.llm, "chat", fake_chat)
    children_writer._stage_write(
        proj, safeio.ProjectPath(proj), {}, plan, "7-9", "寓言故事"
    )

    body = new / children_writer.F_BODY_DIR / "01-小猫出发.md"
    assert body.exists()
    assert not old.exists()


def test_script_scene_write_refreshes_body_path_after_mid_call_rename(rename_env, monkeypatch):
    old = rename_env / "s_old"
    new = rename_env / "s_new"
    old.mkdir()
    proj = script_writer.ScriptProject(str(old), "剧本")
    plan = {
        "scriptName": "剧本",
        "config": {},
        "scenes": [{"no": 1, "heading": "雨夜", "fileName": "第001场.md", "status": "pending"}],
        "meta": {},
    }
    proj.save_plan(plan)

    def fake_scene(*args, **kwargs):
        os.rename(old, new)
        writer.record_rename(old.name, new.name)
        return "雨夜，侦探走进车站，发现一封没有署名的信。" * 20

    monkeypatch.setattr(script_writer, "_gen_scene_body_resilient", fake_scene)
    monkeypatch.setattr(script_writer, "_maybe_final_review", lambda *args: None)
    script_writer.write_scenes(proj, batch_size="all")

    assert (new / script_writer.F_BODY / "第001场.md").exists()
    assert not old.exists()


def test_poetry_create_writes_to_new_path_after_mid_call_rename(rename_env, monkeypatch):
    from bot import final_review

    old = rename_env / "p_old"
    new = rename_env / "p_new"
    poem = "月光落在安静的小城，晚风把思念吹成星河。" * 8

    def fake_note(*args, **kwargs):
        os.rename(old, new)
        writer.record_rename(old.name, new.name)
        return "从一条旧街开始写这首诗。"

    monkeypatch.setattr(poetry_writer, "gen_note", fake_note)
    monkeypatch.setattr(poetry_writer, "gen_plan", lambda *a, **k: "以月光和旧街为核心意象。")
    monkeypatch.setattr(poetry_writer, "gen_draft", lambda *a, **k: poem)
    monkeypatch.setattr(poetry_writer, "gen_critique", lambda *a, **k: {})
    monkeypatch.setattr(final_review, "auto", lambda *a, **k: None)

    poetry_writer.create_project(
        {"title": "旧街月光", "form": "modern", "metrical_check": False,
         "maxRounds": 0}, base_key=old.name)

    assert (new / poetry_writer.F_FINAL).exists()
    assert not old.exists()


def test_prose_create_writes_to_new_path_after_mid_call_rename(rename_env, monkeypatch):
    from bot import final_review

    old = rename_env / "e_old"
    new = rename_env / "e_new"
    text = "旧街的灯一盏盏亮起，晚风里有槐花的香气，也有回家的人。" * 12

    def fake_brief(*args, **kwargs):
        os.rename(old, new)
        writer.record_rename(old.name, new.name)
        return "写一个雨夜归乡的人。"

    monkeypatch.setattr(prose_writer, "gen_brief", fake_brief)
    monkeypatch.setattr(prose_writer, "gen_material", lambda *a, **k: "旧街、晚风、槐花。")
    monkeypatch.setattr(prose_writer, "gen_draft", lambda *a, **k: text)
    monkeypatch.setattr(prose_writer, "gen_polish", lambda *a, **k: text)
    monkeypatch.setattr(prose_writer, "slop_pass", lambda value, cfg: (value, {}))
    monkeypatch.setattr(final_review, "auto", lambda *a, **k: None)

    prose_writer.create_project({"title": "雨夜归乡", "target_words": 0}, base_key=old.name)

    assert (new / prose_writer.F_FINAL).exists()
    assert not old.exists()


def test_thesis_chapter_write_does_not_recreate_old_path_after_rename(rename_env, monkeypatch):
    from bot import final_review

    old = rename_env / "t_old"
    new = rename_env / "t_new"
    old.mkdir()
    proj = thesis_writer.ThesisProject(str(old), "论文")
    plan = {
        "thesisName": "论文",
        "outline": {"title": "论文", "abstract": "研究摘要",
                    "chapters": [{"no": 1, "title": "绪论", "sections": ["背景"],
                                  "target_words": 20}]},
        "refs": [{"no": 1, "entry": "研究文献"}],
        "meta": {},
    }

    def fake_chapter(*args, **kwargs):
        os.rename(old, new)
        writer.record_rename(old.name, new.name)
        return "本章围绕研究背景展开论述，并引用相关文献说明问题。[1]" * 12

    monkeypatch.setattr(thesis_writer, "write_chapter", fake_chapter)
    monkeypatch.setattr(final_review, "auto", lambda *a, **k: None)

    thesis_writer._pipeline(proj, safeio.ProjectPath(proj),
                            {"title": "论文", "humanize": False}, plan=plan)

    assert (new / thesis_writer.F_BODY_DIR / "01-绪论.md").exists()
    assert (new / thesis_writer.F_FINAL).exists()
    assert not old.exists()


def test_thesis_humanize_refreshes_chapter_path_after_mid_call_rename(rename_env, monkeypatch):
    from bot import final_review

    old = rename_env / "t_old"
    new = rename_env / "t_new"
    proj = thesis_writer.ThesisProject(str(old), "论文")
    chapter_path = old / thesis_writer.F_BODY_DIR / "01-绪论.md"
    chapter_path.parent.mkdir(parents=True)
    chapter_path.write_text("原始章节内容。[1]", encoding="utf-8")
    plan = {
        "thesisName": "论文",
        "outline": {"title": "论文", "abstract": "研究摘要",
                    "chapters": [{"no": 1, "title": "绪论", "sections": ["背景"],
                                  "target_words": 20}]},
        "refs": [{"no": 1, "entry": "研究文献"}],
        "chapterStatus": {"1": "completed"},
        "meta": {},
    }

    def fake_humanize(*args, **kwargs):
        os.rename(old, new)
        writer.record_rename(old.name, new.name)
        return "改名后完成的章节改写。[1]", "已改写"

    monkeypatch.setattr(thesis_writer, "humanize_chapter", fake_humanize)
    monkeypatch.setattr(final_review, "auto", lambda *a, **k: None)

    thesis_writer._pipeline(proj, safeio.ProjectPath(proj),
                            {"title": "论文", "humanize": True}, plan=plan)

    assert (new / thesis_writer.F_BODY_DIR / "01-绪论.md").read_text(encoding="utf-8") == \
        "改名后完成的章节改写。[1]"
    assert not old.exists()
