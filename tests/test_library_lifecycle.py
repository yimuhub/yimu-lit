"""v2.2 P2：归档 / 回收站（软删）语义拆分。

归档 = archived_at，侧栏与默认列表收起，作品库「归档」页可见可恢复；
回收站 = trashed_at，默认全隐藏，可恢复也可彻底删除。
旧数据 meta.archived=true 按回收站兼容（旧「归档」实际就是前端的回收站），
读取时惰性判定、不写盘。全部用例跑在 tmp_path，不碰真实 projects/。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from bot import script_writer, writer


def _mk_project(root, key, name, meta=None):
    pdir = os.path.join(str(root), key)
    os.makedirs(pdir, exist_ok=True)
    plan = {"scriptName": name, "medium": "script", "status": "planning",
            "createdAt": "2026-01-01T00:00:00", "updatedAt": "2026-01-01T00:00:00",
            "scenes": [], "meta": meta or {}}
    with open(os.path.join(pdir, script_writer.F_PLAN), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False)
    return pdir


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    """一个待操作剧本 + 一个始终活跃的对照剧本；所有落盘路径指向 tmp。"""
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(writer, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))
    _mk_project(tmp_path, "s_test", "测试剧")
    _mk_project(tmp_path, "s_keep", "对照剧")
    return tmp_path


def _keys(view="active", **kw):
    return {i["key"] for i in writer.library_all(view=view, **kw)}


def _plan_path(root, key="s_test"):
    return os.path.join(str(root), key, script_writer.F_PLAN)


def _meta(root, key="s_test"):
    return json.load(open(_plan_path(root, key), encoding="utf-8")).get("meta") or {}


def test_default_active_excludes_archived_and_trashed(lib):
    assert _keys() == {"s_test", "s_keep"}
    assert _keys("archived") == set()
    assert _keys("trash") == set()
    item = writer.library_item("s_test")
    assert item["archived"] is False and item["trashed"] is False
    assert item["archived_at"] is None and item["trashed_at"] is None


def test_archive_hides_from_active_but_visible_in_archived_view(lib):
    res = writer.archive_book("s_test")
    assert res["ok"] is True
    item = res["item"]
    assert item["archived"] is True and item["archived_at"]
    assert item["trashed"] is False and item["trashed_at"] is None
    assert _keys() == {"s_keep"}
    assert _keys("archived") == {"s_test"}
    assert _keys("trash") == set()
    assert _meta(lib)["archived_at"]

    # restore 与 archive(on=False) 都应回到 active
    assert writer.restore_book("s_test")["ok"] is True
    assert _keys() == {"s_test", "s_keep"} and _keys("archived") == set()
    writer.archive_book("s_test")
    assert writer.archive_book("s_test", on=False)["ok"] is True
    assert _keys() == {"s_test", "s_keep"} and _keys("archived") == set()
    assert "archived_at" not in _meta(lib)


def test_trash_hidden_everywhere_except_trash_view_and_restorable(lib):
    res = writer.trash_book("s_test")
    assert res["ok"] is True
    item = res["item"]
    assert item["trashed"] is True and item["trashed_at"]
    assert item["archived"] is False and item["archived_at"] is None
    assert _keys() == {"s_keep"}
    assert _keys("archived") == set()
    assert _keys("trash") == {"s_test"}

    assert writer.restore_book("s_test")["ok"] is True
    assert _keys() == {"s_test", "s_keep"} and _keys("trash") == set()
    assert "trashed_at" not in _meta(lib)

    # 旧 delete_book 软删仍可用，但语义是进回收站
    assert writer.delete_book("s_test")["ok"] is True
    assert _keys("trash") == {"s_test"} and _keys("archived") == set()


def test_legacy_archived_flag_reads_as_trash_without_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(writer, "SCHEDULES_FILE", str(tmp_path / "schedules.json"))
    pdir = _mk_project(tmp_path, "s_test", "测试剧", {"archived": True})
    before = open(os.path.join(pdir, script_writer.F_PLAN), encoding="utf-8").read()

    item = writer.library_item("s_test")
    assert item["archived"] is False and item["archived_at"] is None
    assert item["trashed"] is True and item["trashed_at"] is None
    assert _keys("archived") == set()
    assert _keys("trash") == {"s_test"}
    assert _keys() == set()
    # 读取只做惰性判定，不得把兼容字段写回磁盘
    after = open(os.path.join(pdir, script_writer.F_PLAN), encoding="utf-8").read()
    assert after == before


def test_purge_requires_exact_name_then_removes_dir(lib):
    pdir = os.path.join(str(lib), "s_test")
    res = writer.purge_book("s_test", "不对的名字")
    assert res == {"ok": False, "note": "请完整输入作品名以确认彻底删除"}
    assert os.path.isdir(pdir)

    res = writer.purge_book("s_test", "  测试剧  ")
    assert res == {"ok": True, "note": "已彻底删除（不可恢复）"}
    assert not os.path.exists(pdir)


def test_include_archived_returns_everything(lib):
    writer.archive_book("s_test")
    assert {i["key"] for i in writer.library_all(include_archived=True)} == {"s_test", "s_keep"}
    writer.trash_book("s_test")
    assert {i["key"] for i in writer.library_all(include_archived=True)} == {"s_test", "s_keep"}
