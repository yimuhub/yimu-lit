"""v5.3.0 书架升级回归：分组 CRUD / 移动分组 / 封面上传校验 / 回收站复用既有语义。

覆盖：
1. 分组 CRUD（data/book-groups.json：锁 + 原子写）：create/rename/delete/reorder，
   校验空名/超长/重名/上限 50/未知 action；删除分组时组内作品映射一并清掉（回「未分组」）；
2. 移动分组（POST /api/library/assign-group）：换组/移出（groupId=null），
   非法 key（越界路径）与不存在的分组拒绝；作品不存在拒绝；
3. 封面（projects/<key>/cover.png|jpg|webp）：multipart 与 base64/dataUrl 双通道上传，
   ≤5MB、按文件头（magic bytes）校验图型、换封面时清理旧封面、DELETE 移除、
   GET 读取（无封面 404）、损坏的 key 拒绝；
4. 回收站恢复**调用既有归档/生命周期语义**：trash_book → restore_book 既有生命周期
   （restore 同时清 trashed_at 与 archived_at，归档态同理），archive_book(on=False) 与
   restore 等价；彻底删除仍走既有 purge（confirm 需完整作品名）——weblib 不新增任何
   软删标记，不发明第二套回收站机制；
5. 简介（GET /api/library/intros）：config.idea → config.theme 兜底、超长截断。

所有落盘都指到 tmp_path，不碰真实 projects/ 与 data/。LLM 由 conftest 全局 mock。
"""
import base64
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot import weblib, writer
from project_helpers import NOVEL_PLAN, mk_project

# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def groups_file(tmp_path, monkeypatch):
    """分组账本指到临时目录，测试互不污染。"""
    f = str(tmp_path / "book-groups.json")
    monkeypatch.setattr(weblib, "DATA_FILE", f)
    return f


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    """两本可操作的作品（一小说一剧本）+ 分组账本全部指到 tmp。"""
    monkeypatch.setattr(weblib, "DATA_FILE", str(tmp_path / "book-groups.json"))
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    mk_project(tmp_path, "novel_a", NOVEL_PLAN,
               {"novelName": "小说甲", "status": "in_progress",
                "totalWords": 4200, "config": {"genre": "悬疑", "idea": "典当行里的万物流转"},
                "meta": {}})
    mk_project(tmp_path, "novel_b", NOVEL_PLAN,
               {"novelName": "小说乙", "status": "completed",
                "config": {"genre": "玄幻"}, "meta": {}})
    return tmp_path


@pytest.fixture()
def client(lib):
    """说明：app.py 的挂载由主会话完成（本任务不许改 app.py），这里用独立 FastAPI 实例
    直接挂 weblib.router 测接口契约，与生产挂载（app.include_router）完全同一条路。"""
    app = FastAPI()
    app.include_router(weblib.router)
    return TestClient(app)


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _jpg() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"JFIF" + b"\x00" * 32


def _webp() -> bytes:
    return b"RIFF\x2c\x00\x00\x00WEBPVP8 " + b"\x00" * 24


# ---------------------------------------------------------------- 1. 分组 CRUD


def test_groups_create_rename_reorder_delete(client, groups_file):
    d0 = client.get("/api/library/groups").json()
    assert d0["ok"] is True and d0["groups"] == [] and d0["assign"] == {}

    r1 = client.post("/api/library/groups", json={"action": "create", "name": "玄幻"})
    assert r1.status_code == 200
    g1 = r1.json()["group"]
    assert g1["name"] == "玄幻" and g1["order"] == 1 and re.match(r"^g-[0-9a-f]{8}$", g1["id"])

    r2 = client.post("/api/library/groups", json={"action": "create", "name": "已完本"})
    g2 = r2.json()["group"]
    assert g2["order"] == 2

    # 重命名：组 id 不变
    r3 = client.post("/api/library/groups", json={"action": "rename", "id": g1["id"], "name": "东方玄幻"})
    assert r3.status_code == 200 and r3.json()["group"]["name"] == "东方玄幻"
    assert r3.json()["group"]["id"] == g1["id"]

    # 重排：把 g2 提到最前
    r4 = client.post("/api/library/groups", json={"action": "reorder", "ids": [g2["id"], g1["id"]]})
    assert r4.status_code == 200
    orders = [(g["id"], g["order"]) for g in r4.json()["groups"]]
    assert orders == [(g2["id"], 1), (g1["id"], 2)]

    # 落盘形状（锁 + 原子写产物可直接读回）
    ondisk = json.load(open(groups_file, encoding="utf-8"))
    assert ondisk["version"] == 1 and len(ondisk["groups"]) == 2
    assert [g["name"] for g in ondisk["groups"]] == ["已完本", "东方玄幻"]

    # 删除分组：列表减少
    r5 = client.post("/api/library/groups", json={"action": "delete", "id": g1["id"]})
    assert r5.status_code == 200 and r5.json()["ok"] is True
    names = [g["name"] for g in client.get("/api/library/groups").json()["groups"]]
    assert names == ["已完本"]


def test_groups_validation_rejects(client, groups_file):
    # 空名 / 超长 / 重名 / 未知 action / 不存在的组
    assert client.post("/api/library/groups", json={"action": "create", "name": "   "}).status_code == 400
    assert client.post("/api/library/groups", json={"action": "create", "name": "x" * 31}).status_code == 400
    assert client.post("/api/library/groups", json={"action": "create", "name": "玄幻"}).status_code == 200
    r = client.post("/api/library/groups", json={"action": "create", "name": "玄幻"})
    assert r.status_code == 400 and "同名" in r.json()["error"]
    r = client.post("/api/library/groups", json={"action": "rename", "id": "g-deadbeef", "name": "新名"})
    assert r.status_code == 400 and "不存在" in r.json()["error"]
    r = client.post("/api/library/groups", json={"action": "delete", "id": "g-deadbeef"})
    assert r.status_code == 400
    r = client.post("/api/library/groups", json={"action": "nope"})
    assert r.status_code == 400 and "未知操作" in r.json()["error"]
    # 内部空白收敛：'  玄 幻  ' 与 '玄 幻' 视为同名
    client.post("/api/library/groups", json={"action": "create", "name": "  玄 幻  "})
    assert client.post("/api/library/groups", json={"action": "create", "name": "玄 幻"}).status_code == 400


def test_groups_cap_50(client, groups_file):
    for i in range(50):
        assert client.post("/api/library/groups", json={"action": "create", "name": f"组{i:02d}"}).status_code == 200
    r = client.post("/api/library/groups", json={"action": "create", "name": "第五十一个"})
    assert r.status_code == 400 and "最多" in r.json()["error"]


def test_groups_delete_cleans_assign(client, lib):
    gid = client.post("/api/library/groups", json={"action": "create", "name": "悬疑组"}).json()["group"]["id"]
    assert client.post("/api/library/assign-group",
                       json={"key": "novel_a", "groupId": gid}).status_code == 200
    assert client.get("/api/library/groups").json()["assign"] == {"novel_a": gid}
    # 删除分组 → novel_a 自动回「未分组」
    client.post("/api/library/groups", json={"action": "delete", "id": gid})
    assert client.get("/api/library/groups").json()["assign"] == {}


# ---------------------------------------------------------------- 2. 移动分组


def test_assign_group_flow(client, lib):
    gid = client.post("/api/library/groups", json={"action": "create", "name": "追更"}).json()["group"]["id"]
    gid2 = client.post("/api/library/groups", json={"action": "create", "name": "完结"}).json()["group"]["id"]

    # 移入 → 换组 → 移出
    assert client.post("/api/library/assign-group", json={"key": "novel_a", "groupId": gid}).json()["groupId"] == gid
    assert client.post("/api/library/assign-group", json={"key": "novel_a", "groupId": gid2}).json()["groupId"] == gid2
    r = client.post("/api/library/assign-group", json={"key": "novel_a", "groupId": None})
    assert r.status_code == 200 and r.json()["groupId"] is None
    assert client.get("/api/library/groups").json()["assign"] == {}

    # 幂等：移出一个没分过组的作品也 ok
    assert client.post("/api/library/assign-group", json={"key": "novel_a", "groupId": None}).json()["ok"] is True


def test_assign_group_rejects_bad_inputs(client, lib):
    gid = client.post("/api/library/groups", json={"action": "create", "name": "组"}).json()["group"]["id"]
    # 作品不存在
    r = client.post("/api/library/assign-group", json={"key": "novel_zzz", "groupId": gid})
    assert r.status_code == 400 and "作品不存在" in r.json()["error"]
    # 缺 key
    assert client.post("/api/library/assign-group", json={"groupId": gid}).status_code == 400
    # 分组不存在
    r = client.post("/api/library/assign-group", json={"key": "novel_a", "groupId": "g-00000000"})
    assert r.status_code == 400 and "不存在" in r.json()["error"]
    # 路径越界的 key 一律拒绝（safe_join 兜底）
    r = client.post("/api/library/assign-group", json={"key": "../evil", "groupId": gid})
    assert r.status_code == 400


def test_assign_group_works_for_trashed_book(client, lib):
    """分组与生命周期正交：回收站里的作品也能挂分组（恢复后归位不变）。"""
    gid = client.post("/api/library/groups", json={"action": "create", "name": "冷藏"}).json()["group"]["id"]
    writer.trash_book("novel_a")
    assert client.post("/api/library/assign-group", json={"key": "novel_a", "groupId": gid}).status_code == 200
    assert client.get("/api/library/groups").json()["assign"] == {"novel_a": gid}
    writer.restore_book("novel_a")
    assert client.get("/api/library/groups").json()["assign"] == {"novel_a": gid}


# ---------------------------------------------------------------- 3. 封面


def test_cover_upload_multipart_and_get(client, lib):
    # PNG 走 multipart：写 cover.png
    r = client.post("/api/library/cover",
                    data={"key": "novel_a"},
                    files={"file": ("cover.png", _png(), "image/png")})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["cover"] == "cover.png"
    assert "/api/library/cover?key=" in d["url"]
    assert os.path.getsize(os.path.join(str(lib), "novel_a", "cover.png")) == len(_png())

    # GET 读回：字节与 media type 一致
    g = client.get("/api/library/cover", params={"key": "novel_a"})
    assert g.status_code == 200
    assert g.headers["content-type"].startswith("image/png")
    assert g.content == _png()


def test_cover_replace_cleans_old_covers(client, lib):
    """换封面：新图写入后旧封面必须清掉（同目录最多只有一张 cover.*）。"""
    client.post("/api/library/cover", data={"key": "novel_a"},
                files={"file": ("a.png", _png(), "image/png")})
    client.post("/api/library/cover", data={"key": "novel_a"},
                files={"file": ("b.jpg", _jpg(), "image/jpeg")})
    pdir = os.path.join(str(lib), "novel_a")
    covers = sorted(n for n in os.listdir(pdir) if n.startswith("cover."))
    assert covers == ["cover.jpg"]
    g = client.get("/api/library/cover", params={"key": "novel_a"})
    assert g.headers["content-type"].startswith("image/jpeg")

    # 再换成 webp：旧 jpg 清掉
    client.post("/api/library/cover", data={"key": "novel_a"},
                files={"file": ("c.webp", _webp(), "image/webp")})
    covers = sorted(n for n in os.listdir(pdir) if n.startswith("cover."))
    assert covers == ["cover.webp"]


def test_cover_upload_base64_dataurl(client, lib):
    """JSON 通道：dataUrl / 裸 base64 都收。"""
    url = "data:image/png;base64," + base64.b64encode(_png()).decode()
    r1 = client.post("/api/library/cover", json={"key": "novel_a", "dataUrl": url})
    assert r1.status_code == 200 and r1.json()["cover"] == "cover.png"

    r2 = client.post("/api/library/cover",
                     json={"key": "novel_b", "base64": base64.b64encode(_jpg()).decode()})
    assert r2.status_code == 200 and r2.json()["cover"] == "cover.jpg"
    assert os.path.exists(os.path.join(str(lib), "novel_b", "cover.jpg"))


def test_cover_validation_size_header(client, lib):
    # 超过 5MB → 413
    big = _png() + b"\x00" * (5 * 1024 * 1024)
    r = client.post("/api/library/cover", data={"key": "novel_a"},
                    files={"file": ("big.png", big, "image/png")})
    assert r.status_code == 413 and "5MB" in r.json()["error"]

    # 文件头不对（伪装成 png 的文本）→ 400：扩展名可伪造，内容不会
    r = client.post("/api/library/cover", data={"key": "novel_a"},
                    files={"file": ("fake.png", b"GIF89a" + b"\x00" * 40, "image/png")})
    assert r.status_code == 400 and "PNG" in r.json()["error"]

    # 空文件 / 空数据
    r = client.post("/api/library/cover", data={"key": "novel_a"},
                    files={"file": ("empty.png", b"", "image/png")})
    assert r.status_code == 400
    assert client.post("/api/library/cover", json={"key": "novel_a", "base64": ""}).status_code == 400
    # 缺 key / 缺文件
    assert client.post("/api/library/cover", files={"file": ("a.png", _png(), "image/png")}).status_code == 400
    assert client.post("/api/library/cover", data={"key": "novel_a"}).status_code == 400
    # 坏 base64
    assert client.post("/api/library/cover", json={"key": "novel_a", "dataUrl": "data:image/png;base64,!!!"}).status_code == 400


def test_cover_get_delete_and_missing(client, lib):
    # 无封面：404（前端 <img onerror> 据此回退字形占位）
    assert client.get("/api/library/cover", params={"key": "novel_a"}).status_code == 404

    client.post("/api/library/cover", data={"key": "novel_a"},
                files={"file": ("a.png", _png(), "image/png")})
    r = client.delete("/api/library/cover", params={"key": "novel_a"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert not os.path.exists(os.path.join(str(lib), "novel_a", "cover.png"))
    # 移除后 GET 404；再次 DELETE 也 ok（幂等）
    assert client.get("/api/library/cover", params={"key": "novel_a"}).status_code == 404
    assert client.delete("/api/library/cover", params={"key": "novel_a"}).status_code == 200


def test_cover_rejects_unsafe_key(client, lib):
    r = client.post("/api/library/cover", data={"key": "../../etc"},
                    files={"file": ("a.png", _png(), "image/png")})
    assert r.status_code == 400
    assert client.get("/api/library/cover", params={"key": "../out"}).status_code == 404
    r = client.delete("/api/library/cover", params={"key": "../out"})
    assert r.status_code == 400


# ---------------------------------------------------------------- 4. 回收站 = 既有生命周期语义（不新增第二套软删）


def test_trash_restore_uses_existing_lifecycle(lib):
    """恢复沿用 restore_book：同时清 trashed_at / archived_at，归档与回收站两态都归位。"""
    item = writer.trash_book("novel_a")["item"]
    assert item["trashed"] is True and item["trashed_at"] and item["archived"] is False
    assert {i["key"] for i in writer.library_all(view="trash")} == {"novel_a"}
    assert {i["key"] for i in writer.library_all(view="active")} == {"novel_b"}

    res = writer.restore_book("novel_a")
    assert res["ok"] is True
    it = res["item"]
    assert it["trashed"] is False and it["trashed_at"] is None
    assert it["archived"] is False and it["archived_at"] is None
    assert {i["key"] for i in writer.library_all(view="active")} == {"novel_a", "novel_b"}
    # meta 里不再残留任何生命周期标记
    plan = json.load(open(os.path.join(str(lib), "novel_a", NOVEL_PLAN), encoding="utf-8"))
    assert not ({"trashed_at", "archived_at", "archived"} & set(plan.get("meta") or {}))


def test_restore_from_archived_state(lib):
    """归档态同样走 restore：恢复 = 既有归档语义的一部分（archive_book(on=False) 等价）。"""
    writer.archive_book("novel_a")
    assert {i["key"] for i in writer.library_all(view="archived")} == {"novel_a"}
    writer.restore_book("novel_a")
    assert writer.library_item("novel_a")["archived"] is False
    writer.archive_book("novel_a")
    assert writer.archive_book("novel_a", on=False)["item"]["archived"] is False


def test_purge_still_requires_existing_confirm(lib):
    """彻底删除仍走既有 purge_book：confirm 必须是完整作品名，错名不动任何文件。"""
    writer.trash_book("novel_a")
    pdir = os.path.join(str(lib), "novel_a")
    assert os.path.isdir(pdir)
    assert writer.purge_book("novel_a", "错误的confirm")["ok"] is False
    assert os.path.isdir(pdir)
    assert writer.purge_book("novel_a", "小说甲")["ok"] is True
    assert not os.path.isdir(pdir)


def test_weblib_adds_no_second_soft_delete():
    """边界钉死：weblib 只管封面/分组/简介——路由面不含任何软删/恢复端点，
    模块内也没有 trash/archive/restore/purge 语义的函数（回收站复用 writer 既有生命周期）。"""
    routes = sorted({r.path for r in weblib.router.routes})
    assert routes == ["/api/library/assign-group", "/api/library/cover",
                      "/api/library/groups", "/api/library/intros"]
    banned = ("trash", "archive", "restore", "purge")
    assert not any(b in name.lower() for name in dir(weblib) for b in banned)


# ---------------------------------------------------------------- 5. 简介（本地搜索字段）


def test_book_intros_idea_then_theme_fallback(lib):
    # theme 兜底：没写 idea 只写了 theme 的作品也能被简介搜索覆盖
    mk_project(lib, "novel_c", NOVEL_PLAN,
               {"novelName": "小说丙", "status": "planning", "config": {"theme": "商战风云"}, "meta": {}})
    intros = weblib.book_intros()
    assert intros.get("novel_a") == "典当行里的万物流转"   # config.idea 优先
    assert intros.get("novel_c") == "商战风云"            # config.theme 兜底
    assert "novel_b" not in intros                        # 无 idea 也无 theme → 不下发


def test_book_intros_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(weblib, "DATA_FILE", str(tmp_path / "book-groups.json"))
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    mk_project(tmp_path, "novel_long", NOVEL_PLAN,
               {"novelName": "长简介", "status": "planning",
                "config": {"idea": "一" * 300}, "meta": {}})
    intros = weblib.book_intros()
    assert len(intros["novel_long"]) == weblib.INTRO_MAX
