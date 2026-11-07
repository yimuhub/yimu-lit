"""v4.31.0 回归：/api/export 全文体导出。

此前非小说一律按剧本取场次，散文/诗词/论文等单文档文体导出必 400，
前端 <a href> 直跳下载地址还会把整个窗口导航到裸 JSON 错误页。
"""
import json

import pytest
from fastapi.testclient import TestClient

PROSE_KEY = "20260101-000000-测试散文"
POETRY_KEY = "20260101-000001-测试诗"
EMPTY_KEY = "20260101-000002-空散文"
NOVEL_KEY = "20260101-000003-测试小说"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from bot import writer, security
    import app as appmod

    proot = tmp_path / "projects"
    proot.mkdir()
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(proot))
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    appmod.PROJECT_INDEX.clear()

    # 散文：计划 + 定稿
    prose = proot / PROSE_KEY
    prose.mkdir()
    (prose / "散文计划.json").write_text(
        json.dumps({"proseName": "秋天的第一场雨"}, ensure_ascii=False), encoding="utf-8")
    (prose / "04-定稿.md").write_text(
        "# 秋天的第一场雨\n\n正文第一段。\n\n正文第二段。", encoding="utf-8")

    # 诗词：只有初稿（无定稿 → 兜底取初稿）
    poetry = proot / POETRY_KEY
    poetry.mkdir()
    (poetry / "诗词计划.json").write_text("{}", encoding="utf-8")
    (poetry / "02-初稿.md").write_text("灯下孤影瘦，一夜雨声凉。", encoding="utf-8")

    # 有计划无正文 → 400 + 明确错误信息（绝不能再把窗口导航走）
    empty = proot / EMPTY_KEY
    empty.mkdir()
    (empty / "散文计划.json").write_text("{}", encoding="utf-8")

    # 小说回归：02-写作计划.json 不得被误判为单文档文体（小说走章节导出）
    novel = proot / NOVEL_KEY
    novel.mkdir()
    (novel / "ch1.md").write_text("小说正文第一段。", encoding="utf-8")
    (novel / "02-写作计划.json").write_text(json.dumps({
        "novelName": "测试小说",
        "chapters": [{"chapterNumber": 1, "title": "第一章 测试", "filePath": "ch1.md"}],
    }, ensure_ascii=False), encoding="utf-8")

    yield TestClient(appmod.app)
    appmod.PROJECT_INDEX.clear()


def test_prose_export_md(client):
    r = client.get("/api/export", params={"key": PROSE_KEY, "fmt": "md"})
    assert r.status_code == 200, r.text
    assert "秋天的第一场雨" in r.text      # 标题取自计划里的 proseName
    assert "正文第一段" in r.text


def test_prose_export_docx(client):
    r = client.get("/api/export", params={"key": PROSE_KEY, "fmt": "docx"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert len(r.content) > 1000   # docx zip 实体


def test_poetry_export_falls_back_to_draft(client):
    r = client.get("/api/export", params={"key": POETRY_KEY, "fmt": "md"})
    assert r.status_code == 200, r.text
    assert "灯下孤影瘦" in r.text


def test_empty_medium_exports_clear_error(client):
    """还没有正文时返回 JSON 错误（前端走 toast），不再是导航到错误页的半途状态。"""
    r = client.get("/api/export", params={"key": EMPTY_KEY, "fmt": "docx"})
    assert r.status_code == 400
    assert "还没有可导出的正文" in r.json()["error"]


def test_novel_export_not_misclassified(client):
    """小说的 02-写作计划.json 不许被当成单文档文体的计划。"""
    r = client.get("/api/export", params={"key": NOVEL_KEY, "fmt": "md"})
    assert r.status_code == 200, r.text
    assert "第一章 测试" in r.text


def test_script_fountain_gated_to_script(client):
    """fountain/fdx 只对剧本开放；散文请求 fountain 走通用格式而不是 500。"""
    r = client.get("/api/export", params={"key": PROSE_KEY, "fmt": "fountain"})
    assert r.status_code == 400
