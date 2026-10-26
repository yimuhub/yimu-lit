"""v2.1 C：作品库认全四种文体。

背景：/api/create 按文体分发之后，剧本/诗词/散文不再是「假装是小说」的项目，
它们的计划文件名各不相同；库若只认 02-写作计划.json，新建的非小说作品就会凭空消失。
本文件把这条契约钉住：库按计划文件判文体、进度用各引擎自己的语义，
并且任何库操作都不得往非小说目录里造小说计划文件。

LLM 由 tests/conftest.py 全局 mock；所有用例都跑在 tmp_path 的假项目上，不碰真实作品。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import writer
from project_helpers import NOVEL_PLAN, PLAN_BY_MEDIUM, mk_project, patch_all_projects

client = TestClient(appmod.app)


@pytest.fixture()
def four_works(tmp_path, monkeypatch):
    """一小说 + 一剧本（30 场写完 12）+ 一诗词（1/3 轮已定稿）+ 一散文（B→S 走到第 2 阶段）。"""
    mk_project(tmp_path, "novel_a", NOVEL_PLAN,
               {"novelName": "小说甲", "status": "in_progress", "chapters": [{"status": "completed"}],
                "totalWords": 3000, "config": {"genre": "悬疑/犯罪"}, "meta": {}})
    mk_project(tmp_path, "s_script", PLAN_BY_MEDIUM["script"],
               {"scriptName": "剧本乙", "status": "in_progress", "form": "series", "formLabel": "剧集",
                "config": {"genre": "悬疑/犯罪"},
                "scenes": [{"status": "completed"} for _ in range(12)] + [{"status": "pending"} for _ in range(18)],
                "meta": {}})
    mk_project(tmp_path, "p_poem", PLAN_BY_MEDIUM["poetry"],
               {"poemName": "七绝一首", "status": "completed", "formLabel": "七绝", "maxRounds": 3,
                "meta": {"rounds": 1, "words": 28, "verdict": "pass"}})
    mk_project(tmp_path, "e_prose", PLAN_BY_MEDIUM["prose"],
               {"proseName": "散文丙", "status": "planning", "genreLabel": "书评", "operationLabel": "改写",
                "meta": {"stageIdx": 2, "words": 1500}})
    # 四个引擎的 PROJECTS_DIR 全部指向 tmp_path：比只 patch writer 更严格地隔离真实 projects/，
    # 本文件的断言只依赖 writer 视角，端口行为不变。
    patch_all_projects(monkeypatch, tmp_path)
    return tmp_path


def _by_medium(items):
    return {i["medium"]: i for i in items}


def test_library_lists_all_four_mediums(four_works):
    got = _by_medium(writer.library_all())
    assert set(got) == {"novel", "script", "poetry", "prose"}
    assert got["script"]["name"] == "剧本乙" and got["poetry"]["name"] == "七绝一首"


def test_progress_uses_each_medium_own_unit(four_works):
    got = _by_medium(writer.library_all())
    assert (got["script"]["done"], got["script"]["total"], got["script"]["progress"]) == (12, 30, 40)
    # 诗词的 rounds 只反映改稿轮数，定稿即 100%
    poem = got["poetry"]
    assert poem["state"] == "completed" and poem["progress"] == 100
    assert poem["total_words"] == 28 and poem["verdict"] == "pass"
    assert (got["prose"]["done"], got["prose"]["total"], got["prose"]["progress"]) == (2, 6, 33)


def test_form_label_not_raw_code(four_works):
    got = _by_medium(writer.library_all())
    assert got["script"]["form"] == "剧集"
    # 剧本另有题材；诗词/散文的体裁即其 genre，筛选下拉不会出现 film 这类码
    assert got["script"]["genre"] == "悬疑/犯罪"
    assert got["poetry"]["genre"] == "七绝" and got["prose"]["genre"] == "书评"
    assert all(i["genre"] != "film" for i in got.values())


def test_dir_without_any_plan_is_not_a_work(tmp_path, monkeypatch):
    os.makedirs(os.path.join(str(tmp_path), "junk_dir"))
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    assert writer.library_all() == []


def test_library_ops_never_fabricate_a_novel_plan(four_works):
    """收藏/改名都写各引擎自己的计划；造出 02-写作计划.json 会让剧本被当成小说。"""
    writer.set_meta("s_script", favorite=True, tags=["测试"])
    assert writer.rename_book("s_script", "剧本乙·改名")["ok"] is True
    pdir = os.path.join(str(four_works), "s_script")
    plan = json.loads(open(os.path.join(pdir, "剧本计划.json"), encoding="utf-8").read())
    assert plan["meta"]["favorite"] is True and plan["meta"]["tags"] == ["测试"]
    assert plan["scriptName"] == "剧本乙·改名" and "novelName" not in plan
    assert not os.path.exists(os.path.join(pdir, NOVEL_PLAN))
    assert writer.library_item("s_script")["medium"] == "script"


def test_api_library_filters_by_medium_and_exposes_facet(four_works):
    d = client.get("/api/library").json()
    assert set(d["facets"]["mediums"]) == {"novel", "script", "poetry", "prose"}
    only_poem = client.get("/api/library", params={"medium": "poetry"}).json()
    assert [i["key"] for i in only_poem["items"]] == ["p_poem"]
    assert only_poem["filters"]["medium"] == "poetry"


def test_api_library_status_filter_covers_planning(four_works):
    d = client.get("/api/library", params={"status": "planning"}).json()
    assert {i["key"] for i in d["items"]} == {"e_prose"}


def test_studio_overview_stays_novel_only(four_works):
    """工作室总览挂的是小说的并行/重写/放行动作，非小说项目不该混进来。"""
    so = client.get("/api/studio-overview").json()
    assert [b["key"] for b in so["books"]] == ["novel_a"]


def test_frontend_opens_every_medium_in_v2():
    """静态资源契约：v2 是默认界面，四种文体都在 v2 内打开，不做经典版跳转。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def read(*parts):
        with open(os.path.join(root, *parts), encoding="utf-8") as f:
            return f.read()

    router = read("static", "v2", "js", "core", "router.js")
    assert "openProject" in router
    assert "/classic" not in router, "打开作品不应把用户送出 v2"
    for rel in (("static", "v2", "js", "views", "library.js"),
                ("static", "v2", "js", "components", "sidebar.js")):
        assert "openProject(item)" in read(*rel), rel
    # 侧栏保留一个手动进旧版的入口，但作品库/侧栏的作品项本身绝不跳出去
    assert "/classic" not in read("static", "v2", "js", "views", "library.js")

    # 路由先解析文体再选视图：非小说走文体工作台，小说走对话/章节工作台
    appjs = read("static", "v2", "js", "app.js")
    assert "isMediumBench" in appjs and "renderMediumBench" in appjs
    assert "api.project" in appjs
    bench = read("static", "v2", "js", "views", "mediumbench.js")
    for med in ("script", "poetry", "prose"):
        assert f"{med}:" in bench, med
    # 经典版不再接 v2 的深链接
    assert "openFromHash" not in read("static", "index.html")

