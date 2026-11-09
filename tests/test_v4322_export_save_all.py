"""v4.32.2 回归：全系统「向外导出」统一弹原生「存储」对话框 + 进度按磁盘对账。

背景（用户实拍）：Mac 桌面客户端点「导出系统日志/诊断包」，前端用
`<a download>` + blob 触发下载，WKWebView 不支持 download 属性，于是把整份
诊断包纯文本当成一次页面导航渲染进窗口——没有保存位置、没有返回按键。
修法：桌面端所有导出都走 `POST /api/save` → 服务端 save_dialog 写盘；
网页端返回 {desktop:false}，前端才走浏览器 blob 下载。
"""
import json

import pytest
from fastapi.testclient import TestClient

PROSE_KEY = "20260101-000000-测试散文"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from bot import security, writer, desktop_bridge
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


def _desktop(monkeypatch, target):
    """把桌面桥切成「已弹对话框、用户选了 target」。"""
    from bot import desktop_bridge
    seen = {}

    def fake(name):
        seen["default_name"] = name
        return str(target)

    monkeypatch.setattr(desktop_bridge, "WINDOW", object(), raising=False)
    monkeypatch.setattr(desktop_bridge, "save_dialog", fake, raising=False)
    return seen


# ---------------------------------------------------------------- /api/save 路由

def test_all_kinds_fall_back_on_browser(client):
    """网页端四种导出都不写盘、不返回文件内容，只给降级信号。"""
    for kind in ("export", "diagnostics", "download", "script"):
        r = client.post("/api/save", json={"kind": kind, "key": PROSE_KEY, "fmt": "docx"})
        assert r.status_code == 200, (kind, r.text)
        assert r.json() == {"desktop": False}


def test_unknown_kind_rejected(client):
    r = client.post("/api/save", json={"kind": "anything-else"})
    assert r.status_code == 400
    assert "不支持的导出类型" in r.json()["error"]


def test_diagnostics_save_opens_native_dialog(client, tmp_path, monkeypatch):
    """截图里的那个坑：诊断包必须弹「存储」对话框，且响应体不含正文。"""
    target = tmp_path / "诊断包.txt"
    seen = _desktop(monkeypatch, target)

    r = client.post("/api/save", json={"kind": "diagnostics",
                                       "sections": ["works"], "logs": False})
    assert r.status_code == 200, r.text
    assert r.json().get("saved") == str(target), r.json()
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# 一幕·YIMU 神笔 诊断包")
    assert "== 各作品状态 ==" in text
    assert seen["default_name"].endswith(".txt")
    # 绝不能把内容回灌给 webview（那正是「无返回键内容页」的成因）
    assert set(r.json().keys()) == {"saved"}


def test_diagnostics_cancel_closes_cleanly(client, monkeypatch):
    from bot import desktop_bridge
    monkeypatch.setattr(desktop_bridge, "WINDOW", object(), raising=False)
    monkeypatch.setattr(desktop_bridge, "save_dialog", lambda name: None, raising=False)
    r = client.post("/api/save", json={"kind": "diagnostics"})
    assert r.json() == {"cancelled": True}


def test_download_save_uses_same_dialog(client, tmp_path, monkeypatch):
    """classic 的「下载成书」也走统一写盘，不再 window.open 导航。"""
    import app as appmod
    target = tmp_path / "整本.md"
    seen = _desktop(monkeypatch, target)
    monkeypatch.setattr(appmod, "_build_download",
                        lambda key: ("# 测试书\n\n正文".encode(), "测试书名"))
    r = client.post("/api/save", json={"kind": "download", "key": PROSE_KEY})
    assert r.status_code == 200, r.text
    assert r.json().get("saved") == str(target)
    assert target.read_bytes().startswith(b"# ")
    assert seen["default_name"].endswith(".md")


def test_script_save_uses_same_dialog(client, tmp_path, monkeypatch):
    from bot import script_writer
    target = tmp_path / "剧本.txt"
    seen = _desktop(monkeypatch, target)
    monkeypatch.setattr(script_writer, "export_text", lambda key: "INT. 咖啡馆 - 日\n\n对白一句。")
    r = client.post("/api/save", json={"kind": "script", "key": PROSE_KEY})
    assert r.status_code == 200, r.text
    assert r.json().get("saved") == str(target)
    assert "INT." in target.read_text(encoding="utf-8")
    assert seen["default_name"].endswith(".txt")


def test_diagnostics_browser_endpoint_still_works(client):
    """网页降级路径保留：/api/diagnostics/export 仍是文本附件。"""
    r = client.post("/api/diagnostics/export", json={"sections": ["works"], "logs": False})
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.text.startswith("# 一幕·YIMU 神笔 诊断包")


# ---------------------------------------------------------------- 前端接线

def _read(rel):
    return open(rel, encoding="utf-8").read()


def test_api_has_single_save_entry_point():
    src = _read("static/v2/js/core/api.js")
    assert "async saveAny(" in src
    assert "'/api/save'" in src
    assert "this.saveAny('export'" in src, "作品导出也必须委托统一入口"


def test_settings_diagnostics_no_longer_blob_in_webview():
    src = _read("static/v2/js/views/settings.js")
    assert "api.saveAny('diagnostics'" in src
    assert "fallback: async ()" in src           # blob 只允许出现在网页降级里
    assert "window.open" not in src


def test_classic_never_opens_export_url_in_new_window():
    src = _read("static/index.html")
    assert "async function yimuSave(" in src
    assert 'window.open("/api/download' not in src
    assert 'window.open("/api/export' not in src
    assert 'window.open("/api/script/export' not in src
    assert "window.open('/api/download" not in src
    assert "yimuSave('download'" in src


# ---------------------------------------------------------------- 进度按磁盘对账

def _plan():
    return {"status": "completed", "novelName": "测试书", "chapters": [
        {"chapterNumber": 1, "filePath": "第01章.md", "status": "completed", "wordCount": 3},
        {"chapterNumber": 2, "filePath": "第02章.md", "status": "pending", "wordCount": None},
        {"chapterNumber": 3, "filePath": "第03章.md", "status": "failed", "wordCount": 100},
    ]}


def test_done_chapters_uses_disk_as_fallback(tmp_path):
    from bot import writer
    (tmp_path / "第01章.md").write_text("正文一", encoding="utf-8")
    (tmp_path / "第02章.md").write_text("正文二", encoding="utf-8")
    plan = _plan()   # 第03章没有文件
    assert writer.done_chapters(plan) == 1           # 无 pdir：退化成纯状态计数
    assert writer.done_chapters(plan, str(tmp_path)) == 2


def test_reconcile_backfills_status_and_words_without_touching_files(tmp_path):
    from bot import writer
    (tmp_path / "第01章.md").write_text("正文一", encoding="utf-8")
    (tmp_path / "第02章.md").write_text("一二三四五六七八", encoding="utf-8")
    plan = _plan()
    fixed = writer.reconcile_completed_chapters(plan, str(tmp_path))
    assert fixed == 1                                  # 只修第 2 章
    assert all(c["status"] == "completed" for c in plan["chapters"][:2])
    assert plan["chapters"][1]["wordCount"] == 8       # 用文件实测字数补 None
    assert plan["chapters"][2]["status"] == "failed"   # 没文件的章不动
    assert (tmp_path / "第02章.md").read_text(encoding="utf-8") == "一二三四五六七八"


def test_project_status_reports_disk_progress(tmp_path):
    """100 章文件齐备但状态被打回 → /api/status 的 done 必须是 100，不是 74。"""
    from bot import writer
    chapters = []
    for i in range(1, 101):
        (tmp_path / f"第{i:02d}章.md").write_text("正文" * 50, encoding="utf-8")
        chapters.append({"chapterNumber": i, "filePath": f"第{i:02d}章.md",
                         "status": "completed" if i <= 74 else "pending",
                         "wordCount": 200 if i <= 74 else None})
    plan = {"status": "completed", "novelName": "测试书", "chapters": chapters}
    (tmp_path / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    st = writer.Project(str(tmp_path), "测试书").status()
    assert st["total"] == 100
    assert st["done"] == 100
    assert writer.reconcile_completed_chapters(plan, str(tmp_path)) == 26
