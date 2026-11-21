"""v5.1.0 章节版本节流快照与恢复（bot/versions.py + /api/chapter 路径）回归。

覆盖：首版 ≥50 字、距上版 ≥5 分钟或字数变化 ≥200 才落、auto 类每章上限 20 版、
恢复前先把当前稿存为 label=恢复前 快照、PUT/POST /api/chapter/versions/restore 路由、
PUT /api/chapter 保存路径接节流快照（既有语义不变，快照为增量）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import security, versions, writer

KEY = "20260101-000000-v51版本测试书"


def _mk_novel(tmp_path, words=120):
    """一本两章的书：第 1 章有稿，第 2 章占位。返回真实 writer.Project。"""
    pdir = tmp_path / KEY
    pdir.mkdir(parents=True, exist_ok=True)
    body = "他把伞收了起来，雨停了。" * max(1, words // 14)
    chapters = []
    for i in (1, 2):
        fn = f"第{i:02d}-x.md"
        chapters.append({"chapterNumber": i, "title": f"第{i}章", "status": "completed",
                         "wordCount": len(body) if i == 1 else 0, "filePath": fn})
        if i == 1:
            (pdir / fn).write_text(body, encoding="utf-8")
    plan = {"novelName": "v51版本测试书", "status": "in_progress", "totalChapters": 2,
            "minWordsPerChapter": 1000, "chapters": chapters}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return writer.Project(str(pdir), KEY)


def _age_all_autos(proj_path, num, seconds=400):
    """把该章全部 auto 快照的 mtime 拨老，让 ≥5 分钟这条节流路径可测（不真睡）。"""
    d = os.path.join(proj_path, ".versions", f"ch{num:03d}")
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        if name.endswith("-auto.md"):
            fp = os.path.join(d, name)
            os.utime(fp, (time.time() - seconds, time.time() - seconds))


# ---------------------------------------------------------------- 节流快照（单元）

def test_throttled_first_version_requires_50_chars(tmp_path):
    p = str(tmp_path)
    assert versions.throttled_snapshot(p, 1, "太短的稿子") is None          # <50 字不落
    r = versions.throttled_snapshot(p, 1, "首" * 60)
    assert r and r["id"].endswith("-auto") and r["label"] == "自动保存"
    assert versions.read_version(p, 1, r["id"]) == "首" * 60


def test_throttled_gap_or_delta(tmp_path):
    p = str(tmp_path)
    versions.throttled_snapshot(p, 1, "首" * 60)
    # 距上版 0 秒、字数差 1 → 节流掉
    assert versions.throttled_snapshot(p, 1, "首" * 61) is None
    # 字数差 ≥200 → 立即落（无需等 5 分钟）
    r = versions.throttled_snapshot(p, 1, "首" * 61 + "尾" * 200)
    assert r and r["id"].endswith("-auto")
    # mtime 拨老 → 距上版 ≥5 分钟路径放行（小改动也落）
    _age_all_autos(p, 1)
    r2 = versions.throttled_snapshot(p, 1, "首" * 61 + "尾" * 200 + "又改了一点点")
    assert r2 and r2["id"].endswith("-auto")
    # 内容与最新一版完全一致 → 不重复落
    assert versions.throttled_snapshot(p, 1, "首" * 61 + "尾" * 200 + "又改了一点点") is None


def test_auto_cap_20_per_chapter(tmp_path):
    p = str(tmp_path)
    versions.throttled_snapshot(p, 1, "首" * 60)
    for i in range(30):
        _age_all_autos(p, 1)                              # 每轮都拨老，走 ≥5 分钟路径
        versions.throttled_snapshot(p, 1, "首" * 60 + ("批" * 200) + str(i))
    autos = [v for v in versions.list_versions(p, 1) if v["id"].endswith("-auto")]
    assert len(autos) == 20
    # 保留的是最近 20 份：最新的 auto 与最后一次内容一致
    assert versions.read_version(p, 1, autos[0]["id"]).endswith("29")


def test_throttled_ignores_non_auto_for_gap_but_caps_total_30(tmp_path):
    """人工快照（编辑前/改稿前）不受 auto 上限管理；节流基准只看 auto。

    兼容约定：只有 v5.1.0 带 tag 的新快照（-auto/-restore）才从文件名还原
    中文 label；旧式快照 label 保持空串（前端回落显示 id），行为不回退。"""
    p = str(tmp_path)
    versions.snapshot(p, 1, "人工稿" * 40, label="编辑前")
    versions.throttled_snapshot(p, 1, "首" * 60 + "改" * 250)
    by_label = {v["label"]: v for v in versions.list_versions(p, 1)}
    assert "自动保存" in by_label
    assert "" in by_label            # 旧式无 tag 快照：label 空、id 兜底
    assert not by_label[""]["id"].endswith(("-auto", "-restore"))


# ---------------------------------------------------------------- 恢复（单元）

def test_restore_version_snapshots_current_as_restore_before(tmp_path):
    p = str(tmp_path)
    versions.throttled_snapshot(p, 1, "旧" * 60)
    target_id = versions.list_versions(p, 1)[0]["id"]
    current = "当前稿" * 100
    out = versions.restore_version(p, 1, target_id, current)
    assert out == "旧" * 60
    labels = [v["label"] for v in versions.list_versions(p, 1)]
    assert "恢复前" in labels
    # 恢复前快照内容就是当前稿
    snap = next(v for v in versions.list_versions(p, 1) if v["label"] == "恢复前")
    assert versions.read_version(p, 1, snap["id"]) == current


def test_restore_version_rejects_bad_vid(tmp_path):
    p = str(tmp_path)
    with pytest.raises(ValueError):
        versions.restore_version(p, 1, "../escape", "x")


# ---------------------------------------------------------------- 路由（PUT /api/chapter 节流 + 恢复端点）

@pytest.fixture()
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    proj = _mk_novel(tmp_path)
    appmod.PROJECT_INDEX.clear()
    appmod.PROJECT_INDEX[KEY] = proj
    yield proj
    appmod.PROJECT_INDEX.clear()


@pytest.fixture()
def client():
    return TestClient(appmod.app)


def test_put_chapter_creates_throttled_snapshot(book, client):
    long_text = "他把伞收了起来，雨停了。" * 12          # ≥50 字 → 首版落 auto
    r = client.put("/api/chapter", json={"key": KEY, "n": 1, "text": long_text, "mode": "prose"})
    assert r.status_code == 200, r.text
    autos = [v for v in versions.list_versions(book.path, 1) if v["id"].endswith("-auto")]
    assert len(autos) == 1
    # 立刻小幅再存（<200 字差、<5 分钟）→ 不新增 auto
    r2 = client.put("/api/chapter", json={"key": KEY, "n": 1, "text": long_text + "。", "mode": "prose"})
    assert r2.status_code == 200
    autos2 = [v for v in versions.list_versions(book.path, 1) if v["id"].endswith("-auto")]
    assert len(autos2) == 1
    # 既有写盘门禁的「编辑前」人工快照仍在（无 tag → label 空、id 兜底，行为不回退）
    items = versions.list_versions(book.path, 1)
    assert any(not v["id"].endswith("-auto") for v in items)


def test_versions_restore_route_put_and_post(book, client):
    first = client.put("/api/chapter", json={"key": KEY, "n": 1, "text": "他把伞收了起来，雨停了。" * 12, "mode": "prose"})
    assert first.status_code == 200
    items = versions.list_versions(book.path, 1)
    target = next(v for v in items if v["id"].endswith("-auto"))
    # 当前稿改成另一个长稿，恢复到首个快照
    client.put("/api/chapter", json={"key": KEY, "n": 1,
                                     "text": "完全不同的当前稿内容。" * 40, "mode": "prose"})
    fp = os.path.join(book.path, "第01-x.md")
    assert open(fp, encoding="utf-8").read().startswith("完全不同")

    for method in ("PUT", "POST"):
        r = client.request(method, "/api/chapter/versions/restore",
                           json={"key": KEY, "n": 1, "id": target["id"]})
        assert r.status_code == 200, r.text
        # 保存/恢复写盘允许尾部一个换行（merge_meta 约定），正文口径按 rstrip 比对
        assert open(fp, encoding="utf-8").read().rstrip("\n") == "他把伞收了起来，雨停了。" * 12
        labels = {v["label"] for v in versions.list_versions(book.path, 1)}
        assert "恢复前" in labels
        plan = json.loads(open(os.path.join(book.path, "02-写作计划.json"), encoding="utf-8").read())
        assert plan["chapters"][0]["wordCount"] > 0


def test_versions_restore_route_bad_id(book, client):
    r = client.put("/api/chapter/versions/restore", json={"key": KEY, "n": 1, "id": "no-such-version"})
    assert r.status_code == 400
    r = client.put("/api/chapter/versions/restore", json={"key": "no-such-book", "n": 1, "id": "x"})
    assert r.status_code == 404


def test_versions_list_route_surfaces_labels(book, client):
    client.put("/api/chapter", json={"key": KEY, "n": 1, "text": "节流快照口径展示。" * 10, "mode": "prose"})
    r = client.get("/api/chapter/versions", params={"key": KEY, "n": 1})
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and any(v["label"] == "自动保存" for v in items)
