"""v2 原生文体工作台的后端契约测试（质量红线：前端冷启动/轮询所依赖的事实源）。

固化的契约——"v2 原生文体工作台"四种文体（novel/script/poetry/prose）直接消费以下后端事实：
1. GET /api/library/{key} 对四种文体都能返回该项（key/medium/name/state/progress/status_label），
   文体由计划文件判定：小说用 02-写作计划.json，其余用各自引擎模块的 F_PLAN 常量（测试不写死文件名）。
   不存在的 key 必须 404，前端据此判"作品已删"。
2. 各文体 status 端点的 shape 是工作台轮询的解析依据：剧本的 form 必须是中文标签（formLabel）
   而非内部码（film/short/series……这条是回归防线，历史上曾直接返回原始码）；诗词带 verdict；
   散文带 operation 且 total 恒为 6（阶段制）。
3. 文档读取端点对存在的项目返回 {"key","text"}（script/scenes 额外带 scenes 数组，
   poetry/part 与 prose/part 带 part 字段），对不存在的 key 返回 404。
   /api/script/scene 自 v2.1 起与同级端点一致校验项目存在（原先漏校验、ghost key 返回 200 空 text）。
4. 各引擎 status() 在"从未跑过引擎、只有计划文件"的裸目录上不得抛异常
   （新建后规划尚未完成时前端就在轮询状态）。

LLM 由 tests/conftest.py 全局 mock；引擎端点各自的 PROJECTS_DIR 在 import 时已固化，
故每个用例同时把 writer/script/poetry/prose 四个模块的 PROJECTS_DIR 指向 tmp_path，
不依赖仓库 projects/ 里的真实数据。假项目搭建与四引擎重定向见 tests/project_helpers.py。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import poetry_writer, prose_writer, script_writer, writer
from project_helpers import NOVEL_PLAN, PLAN_BY_MEDIUM, mk_project, patch_all_projects

client = TestClient(appmod.app)

SCRIPT_DOC_PARTS = [("bible", script_writer.F_BIBLE), ("chars", script_writer.F_CHARS),
                    ("struct", script_writer.F_STRUCT), ("scenes", script_writer.F_SCENES),
                    ("revisions", script_writer.F_REVISIONS), ("quality", script_writer.F_QUALITY)]
POETRY_DOC_PARTS = [("note", poetry_writer.F_NOTE), ("imagery", poetry_writer.F_IMAGERY),
                    ("draft", poetry_writer.F_DRAFT), ("check", poetry_writer.F_CHECK),
                    ("final", poetry_writer.F_FINAL)]
PROSE_DOC_PARTS = [("brief", prose_writer.F_BRIEF), ("material", prose_writer.F_MATERIAL),
                   ("draft", prose_writer.F_DRAFT), ("slop", prose_writer.F_SLOP),
                   ("final", prose_writer.F_FINAL)]


def _scenes():
    """30 场写完 12；第 1 场带正文文件，供 /api/script/scene 断言。"""
    return ([{"no": i + 1, "fileName": f"第{i + 1}场.md", "status": "completed", "wordCount": 800}
             for i in range(12)]
            + [{"no": i + 13, "status": "pending"} for i in range(18)])


@pytest.fixture()
def four_works(tmp_path, monkeypatch):
    """四文体各一完整假作品，文档文件按端点 part 名写入可寻址内容。"""
    mk_project(tmp_path, "novel_a", NOVEL_PLAN,
               {"novelName": "小说甲", "status": "in_progress",
                "chapters": [{"status": "completed", "wordCount": 1500}, {"status": "pending"}],
                "totalWords": 1500, "config": {"genre": "悬疑/犯罪"}, "meta": {}})
    mk_project(tmp_path, "s_script", script_writer.F_PLAN,
               {"scriptName": "剧本乙", "status": "in_progress", "form": "series",
                "formLabel": script_writer.FORMS["series"]["label"], "config": {"genre": "悬疑/犯罪"},
                "scenes": _scenes(), "meta": {}},
               files=[(fn, f"DOC::{part}") for part, fn in SCRIPT_DOC_PARTS]
                     + [(os.path.join(script_writer.F_BODY, "第1场.md"), "SCENE::1")])
    mk_project(tmp_path, "p_poem", poetry_writer.F_PLAN,
               {"poemName": "七绝一首", "status": "completed", "formLabel": "七绝", "maxRounds": 3,
                "meta": {"rounds": 1, "words": 28, "verdict": "pass"}},
               files=[(fn, f"DOC::{part}") for part, fn in POETRY_DOC_PARTS])
    mk_project(tmp_path, "e_prose", prose_writer.F_PLAN,
               {"proseName": "散文丙", "status": "planning", "genreLabel": "书评",
                "operation": "refactor", "operationLabel": "改写", "meta": {"stageIdx": 2, "words": 1500}},
               files=[(fn, f"DOC::{part}") for part, fn in PROSE_DOC_PARTS])
    patch_all_projects(monkeypatch, tmp_path)
    return tmp_path


@pytest.fixture()
def bare_plans(tmp_path, monkeypatch):
    """从未跑过引擎：目录里只有计划文件、且字段最稀疏（novel 甚至是空对象计划）。"""
    mk_project(tmp_path, "n_bare", NOVEL_PLAN, {})
    mk_project(tmp_path, "s_bare", script_writer.F_PLAN, {"scriptName": "裸剧本", "status": "planning"})
    mk_project(tmp_path, "p_bare", poetry_writer.F_PLAN, {"poemName": "裸诗", "status": "planning"})
    mk_project(tmp_path, "e_bare", prose_writer.F_PLAN, {"proseName": "裸散文", "status": "planning"})
    patch_all_projects(monkeypatch, tmp_path)
    return tmp_path


# ---------------------------------------------------------------- 契约 1：library/{key}

@pytest.mark.parametrize("key,med,name,state,label,progress", [
    ("novel_a", "novel", "小说甲", "in_progress", "创作中", 50),
    ("s_script", "script", "剧本乙", "in_progress", "创作中", 40),
    ("p_poem", "poetry", "七绝一首", "completed", "已完成", 100),
    ("e_prose", "prose", "散文丙", "planning", "规划中", 33),
])
def test_library_item_covers_all_four_mediums(four_works, key, med, name, state, label, progress):
    d = client.get(f"/api/library/{key}").json()
    assert d["key"] == key and d["medium"] == med and d["name"] == name
    assert d["state"] == state and d["status_label"] == label
    assert d["progress"] == progress


@pytest.mark.parametrize("medium", ["novel", "script", "poetry", "prose"])
def test_medium_decided_by_each_engines_own_plan_file(tmp_path, monkeypatch, medium):
    """只有本引擎计划文件的目录必须被判成对应文体——文件名取自 F_PLAN 常量。"""
    mk_project(tmp_path, "one_dir", PLAN_BY_MEDIUM[medium],
               {"status": "planning", "novelName": "n", "chapters": [],
                "scriptName": "s", "poemName": "p", "proseName": "e"})
    patch_all_projects(monkeypatch, tmp_path)
    assert client.get("/api/library/one_dir").json()["medium"] == medium


def test_library_item_unknown_key_is_404(four_works):
    r = client.get("/api/library/ghost_key")
    assert r.status_code == 404
    assert r.json()["error"] == "作品不存在"


# ---------------------------------------------------------------- 契约 2：status shape

def test_script_status_form_is_chinese_label_not_raw_code(four_works):
    """回归防线：剧本 status 曾直接回 form 原始码（film/short/series），前端展示即穿帮。"""
    d = client.get("/api/script/status", params={"key": "s_script"}).json()
    assert d["form"] == script_writer.FORMS["series"]["label"]
    assert d["form"] not in script_writer.FORMS  # 不得是内部码
    assert d["medium"] == "script" and (d["done"], d["total"]) == (12, 30)
    assert d["words"] == 12 * 800


def test_poetry_status_carries_verdict(four_works):
    d = client.get("/api/poetry/status", params={"key": "p_poem"}).json()
    assert d["verdict"] == "pass"
    assert d["medium"] == "poetry" and d["state"] == "completed" and d["form"] == "七绝"


def test_prose_status_carries_operation_and_total_is_six(four_works):
    d = client.get("/api/prose/status", params={"key": "e_prose"}).json()
    assert d["operation"] == "refactor"
    assert d["total"] == 6  # 散文阶段制恒为 6，前端进度条以此为分母
    assert (d["done"], d["medium"]) == (2, "prose")


# ---------------------------------------------------------------- 契约 3：文档读取端点

@pytest.mark.parametrize("part", [p for p, _ in SCRIPT_DOC_PARTS if p != "scenes"])
def test_script_doc_endpoints_shape(four_works, part):
    d = client.get(f"/api/script/{part}", params={"key": "s_script"}).json()
    assert d["key"] == "s_script" and d["text"] == f"DOC::{part}"


def test_script_scenes_endpoint_adds_scenes_array(four_works):
    d = client.get("/api/script/scenes", params={"key": "s_script"}).json()
    assert d["key"] == "s_script" and d["text"] == "DOC::scenes"
    assert isinstance(d["scenes"], list) and d["scenes"] == _scenes()


@pytest.mark.parametrize("part", ["note", "imagery", "draft", "check", "final"])
def test_poetry_part_shape(four_works, part):
    d = client.get("/api/poetry/part", params={"key": "p_poem", "part": part}).json()
    assert d == {"key": "p_poem", "part": part, "text": f"DOC::{part}"}


@pytest.mark.parametrize("part", ["brief", "material", "draft", "slop", "final"])
def test_prose_part_shape(four_works, part):
    d = client.get("/api/prose/part", params={"key": "e_prose", "part": part}).json()
    assert d == {"key": "e_prose", "part": part, "text": f"DOC::{part}"}


def test_script_scene_existing_project_reads_body(four_works):
    d = client.get("/api/script/scene", params={"key": "s_script", "no": 1}).json()
    assert d == {"key": "s_script", "no": 1, "text": "SCENE::1"}


def test_script_scene_ghost_key_is_404(four_works):
    """/api/script/scene 与同级端点同口径：项目不存在即 404，前端据此判"作品已删"。"""
    r = client.get("/api/script/scene", params={"key": "ghost_key", "no": 9})
    assert r.status_code == 404
    assert r.json() == {"error": "项目不存在"}


@pytest.mark.parametrize("url", [
    "/api/script/bible", "/api/script/chars", "/api/script/struct", "/api/script/scenes",
    "/api/script/revisions", "/api/script/quality", "/api/poetry/part", "/api/prose/part",
])
def test_doc_endpoints_unknown_key_is_404(four_works, url):
    r = client.get(url, params={"key": "ghost_key"})
    assert r.status_code == 404
    assert r.json()["error"] == "项目不存在"


# ---------------------------------------------------------------- 契约 4：裸计划目录

def test_status_endpoints_survive_plan_only_dirs(bare_plans):
    """新建后规划尚未完成时前端就在轮询——只有计划文件的目录上 status() 不得抛。"""
    s = client.get("/api/script/status", params={"key": "s_bare"}).json()
    assert s["state"] == "planning" and s["total"] == 0 and s["form"] == ""
    p = client.get("/api/poetry/status", params={"key": "p_bare"}).json()
    assert p["state"] == "planning" and p["total"] == 3 and p["verdict"] == ""
    e = client.get("/api/prose/status", params={"key": "e_bare"}).json()
    assert e["state"] == "planning" and e["total"] == 6 and e["operation"] == ""


def test_engine_status_classes_survive_plan_only_dirs(bare_plans):
    """引擎层兜底：含小说在内四套 status() 直接调用也不抛（空 {} 计划最极端）。"""
    for cls, key in ((writer.Project, "n_bare"), (script_writer.ScriptProject, "s_bare"),
                     (poetry_writer.PoetryProject, "p_bare"), (prose_writer.ProseProject, "e_bare")):
        st = cls(os.path.join(str(bare_plans), key), key).status()
        assert isinstance(st, dict) and "state" in st and "total" in st
