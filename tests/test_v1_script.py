"""v1.0.0：剧本流水线 回归测试（mock LLM，不烧 token）。

覆盖：场次解析、场景标题合成、项目创建、逐场写作、修订评分、
     导出、API 端点结构、前端剧本工作台挂载。
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from bot import llm, script_writer as sw

import app as appmod

client = TestClient(appmod.app)

SCENE_MD = """| 场次 | 内/外 | 地点 | 时间 | 出场 | 道具 | 前提 | 触发 | 阻碍 | 结果 | 页数 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 内景 | 警局办公室 | 日 | 老陈 | 档案袋 | 平静的一天 | 电话响 | 电话内容 | 得知旧案重启 | 3 |
| 2 | 外景 | 停车场 | 晨 | 老陈、小周 | 车钥匙 | 疑虑 | 遇到旧识 | 旧识回避 | 更加确信 | 2 |
| 3 | 内景 | 审讯室 | 日 | 老陈、嫌疑人 | 烟 | 对峙 | 嫌疑人开口 | 沉默 | 得到线索 | 4 |"""


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "内景  地点  日\n\n某人走进来。", raising=False)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {
        "scores": {"前提": 8, "结构": 7, "人物": 8, "对白": 7, "格式": 8, "可行性": 8},
        "weak": ["对白"], "verdict": "尚可", "fixes": ["补潜台词"]}, raising=False)
    yield


# ---------- 解析 ----------

def test_parse_scenes():
    scenes = sw.parse_scenes(SCENE_MD)
    assert len(scenes) == 3
    s0 = scenes[0]
    assert s0["no"] == 1
    assert s0["kind"] == "内景"
    assert s0["place"] == "警局办公室"
    assert s0["time"] == "日"
    assert s0["cast"] == "老陈"
    assert s0["obstacle"] == "电话内容"
    assert s0["outcome"] == "得知旧案重启"
    assert s0["pages"] == "3"


def test_parse_scenes_empty():
    assert sw.parse_scenes("") == []
    assert sw.parse_scenes("随便一段文字没有表格") == []


def test_scene_heading():
    sc = sw.parse_scenes(SCENE_MD)[0]
    head = sw.scene_heading(sc)
    assert head == "内景  警局办公室  日"
    assert "。" not in head


def test_scene_heading_fallback():
    assert sw.scene_heading({"no": 7, "heading": "场景 7"}) == "场景 7"


# ---------- 项目 ----------

def test_create_project_writes_files():
    cfg = {"title": "测试剧本", "idea": "退休刑警被诬陷", "form": "film",
           "scenes": 3, "pages": 20}
    proj = sw.create_project(cfg)
    try:
        assert os.path.isdir(proj.path)
        assert os.path.basename(proj.path).startswith("s_")
        for fn in (sw.F_BIBLE, sw.F_CHARS, sw.F_STRUCT, sw.F_SCENES, sw.F_PLAN):
            assert os.path.exists(os.path.join(proj.path, fn)), f"缺 {fn}"
        plan = proj.plan()
        assert plan["medium"] == "script"
        assert plan["form"] == "film"
        assert plan["status"] == "planning"
    finally:
        shutil.rmtree(proj.path, ignore_errors=True)


def test_write_scenes_and_read():
    cfg = {"title": "写戏测试", "idea": "x", "form": "film", "scenes": 1, "pages": 10}
    proj = sw.create_project(cfg)
    try:
        plan = proj.plan()
        plan["scenes"] = sw.parse_scenes(SCENE_MD)[:1]
        for i, sc in enumerate(plan["scenes"], 1):
            sc["no"] = i
            sc["status"] = "pending"
            sc["fileName"] = f"第{i:03d}场.md"
            sc["wordCount"] = None
        proj.save_plan(plan)

        r = sw.write_scenes(proj)
        assert r["ok"] and r["written"] == 1
        st = proj.status()
        assert st["done"] == 1 and st["total"] == 1
        assert st["medium"] == "script"

        key = os.path.basename(proj.path)
        body = sw.read_scene(key, 1)
        assert body.strip()
        assert "内景" in body
    finally:
        shutil.rmtree(proj.path, ignore_errors=True)


def test_revision_and_score():
    cfg = {"title": "修订测试", "idea": "x", "form": "film", "scenes": 1, "pages": 10}
    proj = sw.create_project(cfg)
    try:
        plan = proj.plan()
        plan["scenes"] = sw.parse_scenes(SCENE_MD)[:1]
        for i, sc in enumerate(plan["scenes"], 1):
            sc["no"] = i
            sc["status"] = "completed"
            sc["fileName"] = f"第{i:03d}场.md"
            sw._write(os.path.join(proj.path, sw.F_BODY, sc["fileName"]), "内景  地点  日\n\n内容。")
        proj.save_plan(plan)

        r = sw.run_revision(proj)
        assert r["ok"]
        assert r["score"]["total"] > 0
        assert r["score"]["pass_line"] == 85
        assert "passed" in r["score"]
        assert os.path.exists(os.path.join(proj.path, sw.F_QUALITY))
        assert os.path.exists(os.path.join(proj.path, sw.F_REVISIONS))
    finally:
        shutil.rmtree(proj.path, ignore_errors=True)


def test_export_text():
    cfg = {"title": "导出测试", "idea": "x", "form": "film", "scenes": 1, "pages": 10}
    proj = sw.create_project(cfg)
    try:
        key = os.path.basename(proj.path)
        sw._write(os.path.join(proj.path, sw.F_BODY, "第001场.md"), "内景  地点  日\n\n内容。")
        out = sw.export_text(key)
        assert out
        assert "修订日期" in out
    finally:
        shutil.rmtree(proj.path, ignore_errors=True)


# ---------- API ----------

def test_script_routes_registered():
    paths = {getattr(r, "path", "") for r in appmod.app.routes}
    for p in ["/api/script/projects", "/api/script/create", "/api/script/status",
              "/api/script/bible", "/api/script/scenes", "/api/script/scene",
              "/api/script/write", "/api/script/revise", "/api/script/export",
              "/api/script/forms"]:
        assert p in paths, f"缺路由 {p}"


def test_script_forms_api():
    d = client.get("/api/script/forms").json()
    assert "forms" in d and "entryPaths" in d
    assert len(d["forms"]) >= 5
    assert any(f["key"] == "film" for f in d["forms"])
    assert len(d["entryPaths"]) == 5


def test_script_create_api():
    r = client.post("/api/script/create", json={
        "title": "接口测试", "idea": "一个故事", "form": "film",
        "entryPath": "idea", "scenes": 2, "pages": 10}).json()
    assert "error" not in r, r
    key = r["key"]
    try:
        assert r["medium"] == "script"
        assert client.get("/api/script/status", params={"key": key}).status_code == 200
        assert client.get("/api/script/bible", params={"key": key}).json()["text"]
        items = client.get("/api/script/projects").json()["items"]
        assert any(i["key"] == key for i in items)
    finally:
        shutil.rmtree(os.path.join(sw.PROJECTS_DIR, key), ignore_errors=True)


def test_script_missing_key_404():
    assert client.get("/api/script/status", params={"key": "s_not_exist"}).status_code == 404
    assert client.get("/api/script/bible", params={"key": "s_not_exist"}).status_code == 404


# ---------- 前端 ----------

def test_frontend_script_workbench_present():
    """v1.0 Agent 风格 UI：剧本走 openBench('script',...) 工作台 + 向导 wizardScript。"""
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "static", "index.html"), encoding="utf-8").read()
    assert "wizardScript" in html          # 剧本向导
    assert "openBench" in html             # 剧本工作台
    assert "api/script/create" in html     # 创建调用
    assert "script/" in html and "tabs" in html   # 场次等工作台页签
