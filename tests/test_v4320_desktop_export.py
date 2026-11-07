"""v4.32.0 回归：桌面端导出走原生「存储」对话框（/api/export/save）。

桌面 webview 不支持 <a download>，blob 下载会导航到文件内容页（用户实测的
乱码正文页）；桌面模式必须由服务端弹原生对话框写盘，网页端回退 blob 下载。
"""
import json

import pytest
from fastapi.testclient import TestClient

PROSE_KEY = "20260101-000000-测试散文"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from bot import security, writer
    from bot import desktop_bridge
    import app as appmod

    proot = tmp_path / "projects"
    proot.mkdir()
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(proot))
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(desktop_bridge, "WINDOW", None, raising=False)
    appmod.PROJECT_INDEX.clear()

    prose = proot / PROSE_KEY
    prose.mkdir()
    (prose / "散文计划.json").write_text(
        json.dumps({"proseName": "立冬随笔"}, ensure_ascii=False), encoding="utf-8")
    (prose / "04-定稿.md").write_text("# 立冬随笔\n\n正文一段。", encoding="utf-8")

    yield TestClient(appmod.app)
    appmod.PROJECT_INDEX.clear()


def test_version_has_desktop_flag(client):
    body = client.get("/api/version").json()
    assert "desktop" in body
    assert body["desktop"] is False   # 测试环境无窗口


def test_save_endpoint_non_desktop_fallback(client):
    """非桌面模式返回 desktop=False，前端据此走浏览器 blob 下载。"""
    r = client.post("/api/export/save", json={"key": PROSE_KEY, "fmt": "docx"})
    assert r.status_code == 200
    assert r.json() == {"desktop": False}


def test_save_endpoint_writes_chosen_file(client, tmp_path, monkeypatch):
    """桌面模式：弹对话框选位置 → 服务端把导出产物写到所选路径。"""
    from bot import desktop_bridge

    target = tmp_path / "选的位置" / "我的散文.docx"
    monkeypatch.setattr(desktop_bridge, "WINDOW", object(), raising=False)
    monkeypatch.setattr(desktop_bridge, "save_dialog", lambda name: str(target), raising=False)

    r = client.post("/api/export/save", json={"key": PROSE_KEY, "fmt": "docx"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("saved") == str(target), body
    assert target.exists() and target.stat().st_size > 1000   # docx 实体写入


def test_save_endpoint_cancelled(client, monkeypatch):
    from bot import desktop_bridge

    monkeypatch.setattr(desktop_bridge, "WINDOW", object(), raising=False)
    monkeypatch.setattr(desktop_bridge, "save_dialog", lambda name: None, raising=False)
    r = client.post("/api/export/save", json={"key": PROSE_KEY, "fmt": "md"})
    assert r.status_code == 200
    assert r.json() == {"cancelled": True}


def test_save_endpoint_extension_appended(client, tmp_path, monkeypatch):
    """用户在对话框里没填扩展名 → 自动补 .docx。"""
    from bot import desktop_bridge

    target = tmp_path / "没写扩展名"
    monkeypatch.setattr(desktop_bridge, "WINDOW", object(), raising=False)
    monkeypatch.setattr(desktop_bridge, "save_dialog", lambda name: str(target), raising=False)
    r = client.post("/api/export/save", json={"key": PROSE_KEY, "fmt": "docx"})
    saved = r.json()["saved"]
    assert saved.endswith(".docx")
    import os
    assert os.path.exists(saved)


def test_save_endpoint_empty_medium_clear_error(client, monkeypatch):
    from bot import desktop_bridge

    monkeypatch.setattr(desktop_bridge, "WINDOW", object(), raising=False)
    monkeypatch.setattr(desktop_bridge, "save_dialog", lambda name: "/tmp/x.docx", raising=False)
    r = client.post("/api/export/save", json={"key": "20260101-999999-空空", "fmt": "docx"})
    assert r.status_code == 404   # 项目不存在 → 明确错误，不再有任何静默导航
