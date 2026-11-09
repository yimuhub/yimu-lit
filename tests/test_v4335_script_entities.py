"""v4.33.5 回归：改编剧的「人物 / 结构 / 修订 / 质量 / 分镜」不许再是空白页。

用户实拍：一本 25 万字小说转成 50 集 AI 短剧之后，工作台的
人物、结构、修订、质量、分镜五个页签**全是空的**；右栏「状态」里的地点列着
「第1集、第2集…第50集」——每一场都没有具体地点，根本没法和正文对照一致性。

根因有两层：
1. 这五份 markdown（01-人物小传 / 02-结构大纲 / 05-修订记录 / 07-质量报告 /
   08-分镜表）只由「通读修订 / 评分 / 分镜」这类模型任务顺手写出，改编剧不走
   那条链，于是永远缺失；
2. 改编剧在计划里「一集」就是一个 scene，`place` 只能填占位「第 N 集」，
   `cast` 为空 → 状态库的人物表 0 条、地点表 50 条占位。

契约（以小说的一致性展示为标准）：
1. 正文里 `场1：外 部落广场 白天` + `林野：台词` 能抽成真实的人物表 / 地点表；
2. 占位地点（第 N 集）绝不进地点表；
3. 文档文件缺失时，五个页签按正文实体**现场生成**，不是空白；
4. 场次清单返回真实地点，计划文件本身不被改写；
5. 分镜单场失败不再整单作废，已拆好的照常落盘。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import llm, script_entities as se, script_state, script_writer as sw
from tests.project_helpers import mk_project, patch_all_projects

client = TestClient(appmod.app)

BODY1 = """# 第1集：弃子觉醒

**场1：外 部落广场 白天**

林野猛地睁眼，后脑勺剧痛。

苏清柔：林野。

林野：你来了。

**场2：内 木屋 夜**

小满：少爷，水。

林野：放那儿。
"""

BODY2 = """# 第2集：入营

场1：外 荒原 黄昏

林野：走。
"""


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    se.reset_cache()
    yield
    se.reset_cache()


def _plan(scenes, name="测试短剧"):
    return {"version": 1, "scriptName": name, "medium": "script",
            "status": "in_progress", "scenes": scenes, "config": {}, "meta": {}}


def _scene(no, fn, status="completed", place=None):
    return {"no": no, "status": status, "kind": "集", "place": place or f"第 {no} 集",
            "fileName": fn, "wordCount": len(BODY1), "attempts": 1, "error": ""}


def _mk(tmp_path, monkeypatch, name="s_drama"):
    """改编剧形态：计划里 place 是占位、cast 为空，正文有场标题与对白。"""
    plan = _plan([_scene(1, "第001场.md"), _scene(2, "第002场.md")])
    plan["adaptation"] = {"version": 1, "chaptersPerEpisode": 2}
    plan["form"] = "short_drama"
    pdir = mk_project(tmp_path, name, sw.F_PLAN, plan,
                      [("04-正文/第001场.md", BODY1), ("04-正文/第002场.md", BODY2)])
    patch_all_projects(monkeypatch, tmp_path)
    return pdir


# ------------------------------------------------------------------ 解析

def test_parse_body_handles_both_heading_forms():
    """`场1：` 与 `第2场：` 两种写法都要认，加粗符号不能挡住识别。"""
    scenes = se.parse_body("场1：外 部落广场 白天\n\n**第2场：内 木屋 夜**\n")
    assert [s["no"] for s in scenes] == [1, 2]
    assert scenes[0]["place"] == "部落广场" and scenes[0]["time"] == "白天"
    assert scenes[1]["place"] == "木屋" and scenes[1]["time"] == "夜"
    assert [s["io"] for s in scenes] == ["外", "内"]


def test_parse_body_extracts_cast_and_lines():
    scenes = se.parse_body(BODY1)
    assert [s["place"] for s in scenes] == ["部落广场", "木屋"]
    assert "苏清柔" in scenes[0]["cast"] and "林野" in scenes[0]["cast"]
    assert scenes[0]["lines"] == 2


def test_place_and_time_split_for_unknown_time_word():
    """`未知时间` 要当时间词切掉，不能粘进地点名（用户界面上就是垃圾地名）。"""
    s = se.parse_body("场1：外 秘境通道 未知时间\n\n林野：走。\n")[0]
    assert s["place"] == "秘境通道" and s["time"] == "未知时间"


def test_extract_is_cached_by_body_signature(tmp_path, monkeypatch):
    pdir = _mk(tmp_path, monkeypatch)
    a = se.extract(pdir)
    b = se.extract(pdir)
    assert a is b                      # 命中缓存，右栏 5 秒轮询不会反复读全文
    with open(os.path.join(pdir, "04-正文", "第002场.md"), "a", encoding="utf-8") as f:
        f.write("\n小满：等等。\n")
    se.reset_cache()
    c = se.extract(pdir)
    assert "小满" in c["characters"]


# ------------------------------------------------------------------ 状态库

def test_build_state_ignores_placeholder_places(tmp_path, monkeypatch):
    """核心回归：地点的「第 1 集」不是地名，绝对不能进地点表。"""
    pdir = _mk(tmp_path, monkeypatch)
    st = script_state.build_state(pdir)
    assert "第 1 集" not in st["locations"] and "第 2 集" not in st["locations"]
    assert {"部落广场", "木屋", "荒原"} <= set(st["locations"])
    assert "林野" in st["characters"] and "小满" in st["characters"]
    assert st["metrics"]["sceneCount"] == 3


def test_build_state_keeps_plan_cast_for_classic_script(tmp_path, monkeypatch):
    """常规剧本（计划里有 cast/place）仍以计划为准，抽取只补缺。"""
    plan = _plan([{"no": 1, "status": "completed", "kind": "场", "place": "警局办公室",
                   "cast": "老陈、小周", "fileName": "第001场.md", "wordCount": 10}])
    pdir = mk_project(tmp_path, "s_classic", sw.F_PLAN, plan,
                      [("04-正文/第001场.md", "场1：内 警局办公室 日\n\n老陈：来了。\n")])
    patch_all_projects(monkeypatch, tmp_path)
    st = script_state.build_state(pdir)
    assert "警局办公室" in st["locations"]
    assert st["characters"]["老陈"].get("from") != "body"


# ------------------------------------------------------------------ 五个页签

def test_all_five_tabs_have_content_when_files_missing(tmp_path, monkeypatch):
    """五个页签在没有任何引擎 markdown 的情况下也要有内容。"""
    pdir = _mk(tmp_path, monkeypatch)
    plan = json.load(open(os.path.join(pdir, sw.F_PLAN), encoding="utf-8"))
    ext = se.extract(pdir, plan)
    assert "林野" in se.chars_md(ext, plan)
    assert "第 1 集" in se.struct_md(ext, plan)
    assert "部落广场" in se.struct_md(ext, plan)
    assert "进度" in se.quality_md(pdir, ext, plan)
    assert se.revisions_md(pdir, ext, plan).strip()
    sb = se.storyboard_md(pdir, ext, plan)
    assert "尚未生成" in sb and "3" in sb      # 3 场待拆


@pytest.mark.parametrize("tab,needle", [
    ("chars", "林野"), ("struct", "部落广场"), ("quality", "进度"),
    ("revisions", "修订"), ("storyboard", "分镜"),
])
def test_endpoints_fall_back_to_generated_docs(tmp_path, monkeypatch, tab, needle):
    pdir = _mk(tmp_path, monkeypatch)
    key = os.path.basename(pdir)
    r = client.get(f"/api/script/{tab}", params={"key": key})
    assert r.status_code == 200, r.text
    data = r.json()
    assert needle in (data.get("text") or "")
    assert data.get("generated") is True


def test_endpoint_prefers_real_file_when_present(tmp_path, monkeypatch):
    pdir = _mk(tmp_path, monkeypatch)
    with open(os.path.join(pdir, sw.F_CHARS), "w", encoding="utf-8") as f:
        f.write("# 人物小传\n\n## 林野\n手写的，引擎产出。\n")
    r = client.get("/api/script/chars", params={"key": os.path.basename(pdir)})
    assert "引擎产出" in r.json()["text"]
    assert r.json()["generated"] is False


def test_scenes_endpoint_shows_real_places(tmp_path, monkeypatch):
    """场次卡片不能再显示「第 1 集」，要给正文里的真实地点。"""
    pdir = _mk(tmp_path, monkeypatch)
    key = os.path.basename(pdir)
    rows = client.get("/api/script/scenes", params={"key": key}).json()["scenes"]
    assert rows[0]["place"].startswith("部落广场")
    assert "林野" in rows[0]["cast"]
    # 计划文件本身不被改写
    plan = json.load(open(os.path.join(pdir, sw.F_PLAN), encoding="utf-8"))
    assert plan["scenes"][0]["place"] == "第 1 集"


def test_state_pane_uses_real_places(tmp_path, monkeypatch):
    """右栏状态：地点是真地名，时间线里也带真实地点。"""
    pdir = _mk(tmp_path, monkeypatch)
    from bot import v2api
    proj = sw.get_project(os.path.basename(pdir))
    payload = v2api._script_state_payload(proj, os.path.basename(pdir))
    names = [x["name"] for x in payload["locations"]]
    assert "部落广场" in names and "第 1 集" not in names
    assert "部落广场" in payload["timeline"][0]["location"]


def test_audit_flags_episode_without_scene_heading(tmp_path, monkeypatch):
    plan = _plan([_scene(1, "第001场.md")])
    pdir = mk_project(tmp_path, "s_bad", sw.F_PLAN, plan,
                      [("04-正文/第001场.md", "# 第1集\n\n没有场标题的一段残片。\n")])
    patch_all_projects(monkeypatch, tmp_path)
    rep = script_state.audit_script(pdir)
    assert any("场标题" in i for i in rep["issues"])


# ------------------------------------------------------------------ 分镜

def test_storyboard_continues_after_one_scene_fails(tmp_path, monkeypatch):
    """单场分镜失败不许整单作废：已拆好的场次必须留在 08-分镜表.json。"""
    pdir = _mk(tmp_path, monkeypatch)
    key = os.path.basename(pdir)
    calls = {"n": 0}

    def fake_json(messages, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模型超时")
        return {"shots": [{"shot": 1, "size": "中", "picture": "林野抬手",
                           "lines": "你来了。", "sfx": "", "secs": 3}]}

    monkeypatch.setattr(llm, "chat_json", fake_json, raising=False)
    res = sw.gen_storyboard(key)
    assert res["ok"] is True
    assert res["failed"] == ["第1场"]
    data = json.load(open(os.path.join(pdir, sw.F_STORYBOARD_JSON), encoding="utf-8"))
    assert data["shots"] and all(sh["scene"] == 2 for sh in data["shots"])
    assert os.path.exists(os.path.join(pdir, sw.F_STORYBOARD))
