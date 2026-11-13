"""v4.35.0 导出恢复通道：被闸门拦下时，前端要能拿到「为什么拦、差多少」，
并把「强制导出 / 重写低评分章节」两条路摆给作者。

背景：《复仇太妃：改写贞观》31 万字写完，整书导出一直失败。后端其实留了
`force=1` 的显式取回通道，但错误体只有一句文案，前端没有入口，作者在导出页
无路可走。本次把错误写成结构化数据（code + final_quality/pass_line/below +
禁止项明细），前端据此弹恢复弹窗。

锁死的契约：
1. 放行门禁拦截 → 409 + code=not_released + 分数/达标线/未达标章数；
2. 有禁止项 → 409 + code=not_ready + 明细（章节号/问题名/口径）；
3. force=1 是显式通道，正文完整，书名标注「未放行初稿」；
4. 普通失败（项目不存在等）不带 code，前端不会误弹恢复窗。
"""
import json

from bot import delivery_spec as spec, writer

BODY = ("林野推开木门，风从石阶上掠过，他把退婚书收进怀里，转身走向广场。" * 40
        + "远处忽然传来一声号角。")


def _make_book(tmp_path, *, released=False, final_quality=35.5, body=BODY,
               chapters=None, meta_extra=None):
    pdir = tmp_path / "book"
    pdir.mkdir(exist_ok=True)
    (pdir / "第001章.md").write_text(body, encoding="utf-8")
    meta = {"released": released, "final_quality": final_quality}
    meta.update(meta_extra or {})
    plan = {
        "novelName": "测试书",
        "status": "completed",
        "deliverySpecVersion": spec.SPEC_VERSION,
        "deliveryProfile": "novel_submission",
        "chapters": chapters or [{
            "chapterNumber": 1, "title": "开端", "status": "completed",
            "filePath": "第001章.md",
        }],
        "meta": meta,
    }
    (pdir / "02-写作计划.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return pdir


def _client(monkeypatch, tmp_path):
    import app as appmod
    from fastapi.testclient import TestClient
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    return appmod, TestClient(appmod.app)


def test_not_released_error_is_structured(monkeypatch, tmp_path):
    _make_book(tmp_path, released=False, final_quality=35.5)
    _, c = _client(monkeypatch, tmp_path)

    r = c.get("/api/export?key=book&fmt=md")
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "not_released"
    assert body["final_quality"] == 35.5
    assert body["pass_line"] == 80
    assert body["chapters"] == 1
    assert isinstance(body["below"], int)
    assert "放行" in body["error"]


def test_force_export_marks_unreleased_draft(monkeypatch, tmp_path):
    _make_book(tmp_path, released=False, final_quality=35.5)
    _, c = _client(monkeypatch, tmp_path)

    r = c.get("/api/export?key=book&fmt=md&force=1")
    assert r.status_code == 200
    first = r.text.splitlines()[0]
    assert first.startswith("# 测试书（未放行初稿35.5分）"), first


def test_block_findings_are_structured(monkeypatch, tmp_path):
    # 正文里出现英文 → N01 非中文残留（block），先于放行门禁拦下
    _make_book(tmp_path, released=True, body="林野听见 Host 的声音。")
    _, c = _client(monkeypatch, tmp_path)

    r = c.get("/api/export?key=book&fmt=md")
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "not_ready"
    assert body["blocking"] >= 1
    assert body["findings"], "禁止项明细必须回给前端，否则用户不知道该改哪一章"
    one = body["findings"][0]
    assert {"code", "name", "location", "detail"} <= set(one)
    assert one["code"].startswith("N")


def test_released_book_exports_without_force(monkeypatch, tmp_path):
    _make_book(tmp_path, released=True, final_quality=88.0)
    _, c = _client(monkeypatch, tmp_path)

    r = c.get("/api/export?key=book&fmt=md")
    assert r.status_code == 200
    assert r.text.splitlines()[0] == "# 测试书"


def test_plain_error_has_no_recovery_code(monkeypatch, tmp_path):
    """项目不存在这类普通失败不该带 code，否则前端会误弹恢复窗。"""
    _make_book(tmp_path, released=False)
    _, c = _client(monkeypatch, tmp_path)

    r = c.get("/api/export?key=nope&fmt=md")
    assert r.status_code == 404
    body = r.json()
    assert "code" not in body
    assert body["error"]


def test_save_endpoint_returns_same_structure(monkeypatch, tmp_path):
    """桌面端走 POST /api/save（kind=export），错误结构必须与 GET 一致。"""
    pdir = _make_book(tmp_path, released=False, final_quality=35.5)
    appmod, c = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(appmod.desktop_bridge, "is_desktop", lambda: True)

    r = c.post("/api/save", json={"kind": "export", "key": "book", "fmt": "md"})
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "not_released"
    assert body["final_quality"] == 35.5
    assert (pdir / "22-交付就绪报告.md").exists()
