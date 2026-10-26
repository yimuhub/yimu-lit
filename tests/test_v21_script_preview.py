"""v2.1 E1：剧本场景正文可经 HTTP 读出（单场 + 整剧预览）。

v2 工作台要能"预览本场 / 预览整剧"，后端必须三件事成立：
1. GET /api/script/scene 与同级文档端点一样校验项目存在，ghost key 返回 404
   （历史上它漏校验，ghost key 拿到 200 空 text，前端无法据此判"作品已删"）。
2. read_scene 对缺/坏 fileName 的场次按冻结命名约定 第NNN场.md 兜底
   （写正文时就是这么落盘的），文件确实不存在时仍返回 ""。
3. GET /api/script/preview 一次往返返回整剧：按场次顺序、标题取自引擎
   scene_heading()、未写场次也占位（text=""），省掉前端 30 次逐场请求。

LLM 由 tests/conftest.py 全局 mock；所有用例跑在 tmp_path 的假项目上，不碰真实 projects/。
假项目搭建与四引擎 PROJECTS_DIR 重定向复用 tests/project_helpers.py。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import script_writer
from project_helpers import mk_project, patch_all_projects

client = TestClient(appmod.app)

KEY = "s_preview"

# 第 4 场计划里没有 fileName，但盘上有 第004场.md —— 供 read_scene 兜底断言。
SCENE4_BODY = "SCENE::4\n"


def _scenes():
    """5 场：2 场有正文（第 4 场靠命名约定兜底），3 场未写；第 3 场连 fileName 都缺。"""
    return [
        {"no": 1, "kind": "内", "place": "陈默公寓", "time": "日",
         "status": "completed", "wordCount": 440, "fileName": "第001场.md"},
        {"no": 2, "kind": "外", "place": "街道", "time": "夜",
         "status": "pending", "fileName": "第002场.md"},
        {"no": 3, "kind": "外", "place": "天台", "time": "夜",
         "status": "pending", "wordCount": 0},
        {"no": 4, "kind": "内", "place": "病房", "time": "日",
         "status": "completed", "wordCount": 120},
        {"no": 5, "heading": "蒙太奇", "status": "pending"},
    ]


@pytest.fixture()
def script_work(tmp_path, monkeypatch):
    """一个 5 场剧本：正文只落盘第 1、4 场（第 4 场计划字段缺 fileName）。"""
    mk_project(tmp_path, KEY, script_writer.F_PLAN,
               {"scriptName": "末班车", "status": "in_progress", "form": "film",
                "formLabel": script_writer.FORMS["film"]["label"],
                "scenes": _scenes(), "meta": {}},
               files=[(os.path.join(script_writer.F_BODY, "第001场.md"), "SCENE::1\n"),
                      (os.path.join(script_writer.F_BODY, "第004场.md"), SCENE4_BODY)])
    patch_all_projects(monkeypatch, tmp_path)
    return tmp_path


# ---------------------------------------------------------------- 单场端点


def test_script_scene_ghost_key_is_404(script_work):
    """ghost key 必须与 /api/script/scenes、/api/script/revisions 等同口径返回 404。"""
    r = client.get("/api/script/scene", params={"key": "ghost_key", "no": 9})
    assert r.status_code == 404
    assert r.json() == {"error": "项目不存在"}


def test_script_scene_returns_exact_body_text(script_work):
    r = client.get("/api/script/scene", params={"key": KEY, "no": 1})
    assert r.status_code == 200
    assert r.json() == {"key": KEY, "no": 1, "text": "SCENE::1\n"}


def test_read_scene_falls_back_to_frozen_naming(script_work):
    """场次 dict 缺 fileName，但 第004场.md 在盘上 → 兜底读出该文件；两处都缺 → ""。"""
    assert script_writer.read_scene(KEY, 4) == SCENE4_BODY
    assert script_writer.read_scene(KEY, 3) == ""  # 无 fileName、无文件
    assert script_writer.read_scene(KEY, 2) == ""  # 有 fileName、但文件确实不存在
    assert script_writer.read_scene("ghost_key", 1) == ""  # 项目不存在仍返回 ""


def test_read_scene_ignores_path_traversal_in_file_name(tmp_path, monkeypatch):
    """fileName 可被用户手改：../../ 会指向 projects 根下，兜底只取末段，读不到项目外的文件。"""
    mk_project(tmp_path, "s_escape", script_writer.F_PLAN,
               {"scriptName": "逃逸", "status": "in_progress",
                "scenes": [{"no": 1, "fileName": "../../secret.md"}], "meta": {}})
    (tmp_path / "secret.md").write_text("LEAK\n", encoding="utf-8")
    patch_all_projects(monkeypatch, tmp_path)
    assert script_writer.read_scene("s_escape", 1) == ""


# ---------------------------------------------------------------- 整剧预览端点


def test_script_preview_whole_script_in_reading_order(script_work):
    d = client.get("/api/script/preview", params={"key": KEY}).json()
    assert d["key"] == KEY and d["name"] == "末班车"
    assert (d["written"], d["total"]) == (2, 5)
    assert [s["no"] for s in d["scenes"]] == [1, 2, 3, 4, 5]  # 顺序即计划顺序
    # 标题必须来自引擎自己的合成器，不得端点内重写
    assert [s["heading"] for s in d["scenes"]] == [
        script_writer.scene_heading(sc) for sc in _scenes()]
    assert [s["heading"] for s in d["scenes"]][:3] == ["内  陈默公寓  日", "外  街道  夜", "外  天台  夜"]
    # words 取计划 wordCount，缺省为 0
    assert [s["words"] for s in d["scenes"]] == [440, 0, 0, 120, 0]
    # 未写场次仍占位，text 为空串
    assert [s["text"] for s in d["scenes"]] == ["SCENE::1\n", "", "", SCENE4_BODY, ""]
    assert [s["status"] for s in d["scenes"]] == [
        "completed", "pending", "pending", "completed", "pending"]
    assert d["scenes"][3]["text"] == script_writer.read_scene(KEY, 4)


def test_script_preview_ghost_key_is_404(script_work):
    r = client.get("/api/script/preview", params={"key": "ghost_key"})
    assert r.status_code == 404
    assert r.json() == {"error": "项目不存在"}
