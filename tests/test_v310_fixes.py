# v3.1.0：章名回流/大纲标题解析、校验卡死恢复、差一章有界化、cleanText、剧本场次解析。
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import security, writer

KEY = "20260101-000000-卡死书"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(appmod.writer, "PROJECTS_DIR", str(tmp_path))
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()
    yield
    appmod.SESSIONS.clear()
    appmod.PROJECT_INDEX.clear()


# ---------------- 大纲标题解析 ----------------

def test_parse_outline_titles_numeric_table():
    md = ("| 章节 | 标题 | 核心事件 |\n| :--- | :--- | :--- |\n"
          "| 01 | 穿越大唐 | 主角醒来 |\n| 02 | 长安迷局 | 初入官场 |")
    assert writer._parse_outline_titles(md, 3) == ["穿越大唐", "长安迷局", "第3章"]


def test_parse_outline_titles_classical():
    md = "| 第1章 | 风雪夜归人 |\n| 第2章 | 故人来信 |"
    assert writer._parse_outline_titles(md, 2) == ["风雪夜归人", "故人来信"]


def test_clean_chapter_text_summary_only():
    """仅有概要无引子的章节：概要必须被剥掉（旧实现会带进成书）。"""
    t = ("# 第01章 某章\n\n## 本章概要\n主角干了什么。\n\n## 正文\n正文第一段。")
    out = writer._clean_chapter_text(t)
    assert "本章概要" not in out and "主角干了什么" not in out and "正文第一段" in out


# ---------------- 陈旧状态恢复 ----------------

def _mk_book(tmp_path, status="validating", chapters=None):
    pdir = tmp_path / KEY
    # v4.23.2：本函数会被同一用例重复调用（改 status 再建一次）。沙箱把 mkdir 代理给
    # 宿主进程执行时，EEXIST 会直接抛成 FileExistsError、绕过 exist_ok 语义，
    # 于是第二次调用直接炸。这里按「已存在且是目录」视为成功，语义与 exist_ok 一致。
    try:
        pdir.mkdir(exist_ok=True)
    except FileExistsError:
        if not pdir.is_dir():
            raise
    chs = chapters if chapters is not None else [
        {"chapterNumber": i, "title": f"第{i}章", "filePath": f"第{i:02d}章.md",
         "status": "completed" if i < 3 else "in_progress", "wordCount": 3000}
        for i in range(1, 4)]
    (pdir / "02-写作计划.json").write_text(json.dumps(
        {"novelName": "卡死书", "status": status, "config": {"chapters": 3, "genre": "都市"},
         "minWordsPerChapter": 3000, "chapters": chs}, ensure_ascii=False), encoding="utf-8")
    # 第3章文件有实质内容
    (pdir / "第03章.md").write_text("正文" * 1500, encoding="utf-8")
    # v4.33.8 成书对账：成书 = 所有正文文件齐备，夹具必须把 3 章正文都落盘
    for i in (1, 2):
        (pdir / f"第{i:02d}章.md").write_text("正文" * 1500, encoding="utf-8")
    return pdir


def test_recover_stale_book(tmp_path):
    _mk_book(tmp_path)
    n = writer.recover_stale_book(KEY)
    assert n == 1   # in_progress 但文件有内容 → 复位 completed
    plan = json.loads((tmp_path / KEY / "02-写作计划.json").read_text(encoding="utf-8"))
    assert plan["status"] == "in_progress"
    assert plan["chapters"][2]["status"] == "completed"
    # 幂等：再次恢复返回 None
    assert writer.recover_stale_book(KEY) is None


def test_recover_ignores_healthy_or_running(tmp_path, monkeypatch):
    _mk_book(tmp_path, status="in_progress")
    assert writer.recover_stale_book(KEY) is None
    # validating 但线程活着：不动
    _mk_book(tmp_path, status="validating")
    proj = appmod._get_project(KEY)
    proj.running = True
    try:
        assert writer.recover_stale_book(KEY, live_proj=proj) is None
    finally:
        proj.running = False


def test_validate_loop_skips_soft_pass(tmp_path, monkeypatch):
    """校验循环只重写真失败章：soft-pass（完成但 wcp=False）不再触发整章重写。"""
    from bot import llm
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "")
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {})
    pdir = _mk_book(tmp_path, status="in_progress")
    plan = json.loads((pdir / "02-写作计划.json").read_text(encoding="utf-8"))
    # 第1章 completed 但 wcp=False（soft-pass）；第2章 completed wcp=True；无 failed 章
    plan["chapters"][0]["wordCountPass"] = False
    plan["chapters"][1]["wordCountPass"] = True
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proj = appmod._get_project(KEY)
    called = []
    monkeypatch.setattr(writer, "_write_chapter",
                        lambda *a, **k: called.append(a[1].get("chapterNumber")))
    monkeypatch.setattr(writer, "_run_layered", lambda *a, **k: {"total": 100, "pass": True, "fixed": 0})
    monkeypatch.setattr(writer, "_maybe_backflow_titles", lambda *a, **k: None)
    writer._validate_and_finish(proj)
    assert called == []   # soft-pass 不重写
    assert proj.plan()["status"] == "completed"


# ---------------- 剧本场次解析 ----------------

def test_parse_scenes_cn_and_fullwidth():
    from bot.script_writer import parse_scenes
    md = "| 场次 | 内/外 | 地点 |\n| :--- | :--- | :--- |\n| 第1场 | 内 | 公交车内 |\n| 第２场 | 外 | 车站 |"
    sc = parse_scenes(md)
    assert [x["no"] for x in sc] == [1, 2]
    assert sc[0]["place"] == "公交车内"


def test_script_scenes_reparse_fallback(tmp_path, monkeypatch):
    """plan.scenes 空但场次清单 md 可解析 → 预览接口现场解析回填。"""
    _mk_book(tmp_path)
    client = TestClient(appmod.app)
    monkeypatch.setattr(appmod.script_writer, "get_project", lambda key: appmod._get_project(KEY))
    appmod._get_project(KEY)   # 触发项目加载（副作用：读到 app 的项目表）
    # 把小说项目伪装成剧本：给 read_file_part 打桩返回场次表
    monkeypatch.setattr(appmod.script_writer, "read_file_part",
                        lambda key, part: "| 场次 | 内/外 | 地点 |\n| :--- | :--- | :--- |\n| 第1场 | 内 | 室内 |"
                        if part == "scenes" else "")
    r = client.get("/api/script/scenes", params={"key": KEY})
    assert r.status_code == 200
    scenes = r.json()["scenes"]
    assert len(scenes) == 1 and scenes[0]["place"] == "室内"
    # 已回填进计划
    plan = json.loads((tmp_path / KEY / "02-写作计划.json").read_text(encoding="utf-8"))
    # 断言信息带上 plan 的键与条数：这条以前只在 3.14 的 CI 上偶发红过一次，
    # 当时拿到的是光秃秃的 KeyError: 'scenes'，完全看不出是没写盘还是写到别处去了。
    assert "scenes" in plan, (
        f"场次回填没落盘：接口返回了 {len(scenes)} 场，但计划文件里没有 scenes。"
        f"若写盘失败，stderr 会有 `[script] 场次回填写盘失败`；plan keys={sorted(plan)}"
    )
    assert plan["scenes"][0]["no"] == 1


# ---------------- 章名回流 ----------------

def test_set_chapter_title_updates_plan_and_file(tmp_path):
    pdir = _mk_book(tmp_path)
    proj = appmod._get_project(KEY)
    (pdir / "第03章.md").write_text("# 第03章 第3章\n\n正文", encoding="utf-8")
    assert writer._set_chapter_title(proj, 3, "夜行者") is True
    plan = json.loads((pdir / "02-写作计划.json").read_text(encoding="utf-8"))
    assert plan["chapters"][2]["title"] == "夜行者"
    head = (pdir / "第03章.md").read_text(encoding="utf-8").splitlines()[0]
    assert head == "# 第03章 夜行者"


def test_pipeline_counter(tmp_path):
    writer.mark_pipeline(KEY, True)
    assert writer.pipeline_running(KEY) is True
    writer.mark_pipeline(KEY, False)
    assert writer.pipeline_running(KEY) is False
    # 防负
    writer.mark_pipeline(KEY, False)
    assert writer.pipeline_running(KEY) is False
