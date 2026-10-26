"""v2.2 新接口：作品库三视图 / 归档回收站 / 剧本体检 / 场段定向改写 / 建项立即回显。"""
import json
import time

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import script_assist, script_writer, security, writer


def _client(monkeypatch, root):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(root))
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(root))
    monkeypatch.setattr(writer, "SCHEDULES_FILE", str(root / "schedules.json"))
    appmod.PROJECT_INDEX.clear()
    return TestClient(appmod.app)


def _mk_script(root, key, name="古代喜剧", era="ancient", scene_texts=None):
    pdir = root / key
    (pdir / script_writer.F_BODY).mkdir(parents=True)
    texts = scene_texts or {1: "第一段开场。\n\n第二段推进。"}
    scenes = []
    for no, text in texts.items():
        fn = f"第{no:03d}场.md"
        (pdir / script_writer.F_BODY / fn).write_text(text + "\n", encoding="utf-8")
        scenes.append({"no": no, "heading": f"客栈·第{no}场", "fileName": fn,
                       "status": "completed", "wordCount": len(text)})
    plan = {"scriptName": name, "medium": "script", "status": "in_progress",
            "createdAt": "2026-01-01T00:00:00", "updatedAt": "2026-01-01T00:00:00",
            "scenes": scenes,
            "meta": {"setting": {"era": era, "era_label": "古代", "world": "江南小镇"}}}
    (pdir / script_writer.F_PLAN).write_text(json.dumps(plan, ensure_ascii=False),
                                             encoding="utf-8")
    (pdir / script_writer.F_BIBLE).write_text("北宋年间，江南小镇。", encoding="utf-8")
    return pdir


@pytest.fixture()
def root(tmp_path):
    return tmp_path


# ---------------------------------------------------------------- 作品库三视图

def test_library_three_views_and_counts(monkeypatch, root):
    _mk_script(root, "s_keep")
    _mk_script(root, "s_arch")
    _mk_script(root, "s_trash")
    c = _client(monkeypatch, root)
    assert c.post("/api/library/s_arch/archive", json={"on": True}).status_code == 200
    assert c.post("/api/library/s_trash/trash").status_code == 200

    active = c.get("/api/library").json()
    assert {i["key"] for i in active["items"]} == {"s_keep"}
    assert active["counts"] == {"active": 1, "archived": 1, "trash": 1}

    archived = c.get("/api/library", params={"view": "archived"}).json()
    assert {i["key"] for i in archived["items"]} == {"s_arch"}
    trash = c.get("/api/library", params={"view": "trash"}).json()
    assert {i["key"] for i in trash["items"]} == {"s_trash"}

    # 旧前端 status=archived 仍映射到归档视图
    legacy = c.get("/api/library", params={"status": "archived"}).json()
    assert {i["key"] for i in legacy["items"]} == {"s_arch"}


def test_restore_returns_to_active(monkeypatch, root):
    _mk_script(root, "s_a")
    c = _client(monkeypatch, root)
    c.post("/api/library/s_a/trash")
    assert c.get("/api/library").json()["counts"]["trash"] == 1
    assert c.post("/api/library/s_a/restore").json()["ok"] is True
    assert c.get("/api/library").json()["counts"] == {"active": 1, "archived": 0, "trash": 0}


def test_purge_requires_exact_name(monkeypatch, root):
    pdir = _mk_script(root, "s_gone", name="测试剧")
    c = _client(monkeypatch, root)
    bad = c.delete("/api/library/s_gone/purge", params={"confirm": "测试"})
    assert bad.status_code == 400 and bad.json()["ok"] is False
    assert pdir.is_dir()

    ok = c.delete("/api/library/s_gone/purge", params={"confirm": "测试剧"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert not pdir.exists()


def test_hard_delete_flag_gates_purge(monkeypatch, root):
    pdir = _mk_script(root, "s_h", name="硬删书")
    c = _client(monkeypatch, root)
    soft = c.delete("/api/library/s_h")
    assert soft.status_code == 200 and pdir.is_dir()
    assert c.get("/api/library").json()["counts"]["trash"] == 1

    bad = c.delete("/api/library/s_h", params={"hard": "true", "confirm": "硬删"})
    assert bad.status_code == 400 and pdir.is_dir()
    good = c.delete("/api/library/s_h", params={"hard": "true", "confirm": "硬删书"})
    assert good.status_code == 200 and not pdir.exists()


def test_batch_never_purges(monkeypatch, root):
    pdir = _mk_script(root, "s_b")
    c = _client(monkeypatch, root)
    res = c.post("/api/library/batch", json={"keys": ["s_b"], "action": "purge"}).json()
    assert res["ok"] is True
    assert res["results"][0]["ok"] is False
    assert pdir.is_dir()


# ---------------------------------------------------------------- 剧本体检

def test_script_check_flags_modern_terms(monkeypatch, root):
    _mk_script(root, "s_era", scene_texts={
        1: "客栈里点着灯。",
        2: "他掏出手机给律师打电话，然后坐电梯去医院。",
    })
    c = _client(monkeypatch, root)
    d = c.post("/api/script/check", json={"key": "s_era"}).json()
    assert d["era"] == "ancient"
    assert d["summary"]["flagged"] == 1
    assert {"手机", "律师", "电梯", "医院"} <= set(d["summary"]["terms"])
    flagged = [i for i in d["items"] if i["issues"]]
    assert flagged[0]["scene"] == 2

    assert c.post("/api/script/check", json={"key": "nope"}).status_code == 404


def test_script_check_clean_project_ok(monkeypatch, root):
    _mk_script(root, "s_clean", scene_texts={1: "茶博士提着铜壶，穿过长街。"})
    c = _client(monkeypatch, root)
    d = c.post("/api/script/check", json={"key": "s_clean"}).json()
    assert d["ok"] is True and d["summary"]["flagged"] == 0


# ---------------------------------------------------------------- 场段定向改写

def _base_hash(key, scene, block):
    return script_assist.block_hash(
        script_assist.split_blocks(script_writer.read_scene(key, scene))[block - 1])


def test_assist_apply_only_touches_target_block(monkeypatch, root):
    _mk_script(root, "s_edit", scene_texts={
        1: "第一段。\n\n第二段。\n\n第三段。",
        2: "别的场次。",
    })
    c = _client(monkeypatch, root)
    body = {"key": "s_edit", "scene": 1, "block": 2,
            "base_hash": _base_hash("s_edit", 1, 2), "new_text": "改过的第二段。"}
    res = c.post("/api/script/assist/apply", json=body)
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True and res.json()["version"]

    blocks = script_assist.split_blocks(script_writer.read_scene("s_edit", 1))
    assert blocks[1] == "改过的第二段。"
    assert blocks[0] == "第一段。" and blocks[2] == "第三段。"
    assert script_writer.read_scene("s_edit", 2).strip() == "别的场次。"

    # 同一条旧 hash 再提交 → 409 stale
    stale = c.post("/api/script/assist/apply", json=body)
    assert stale.status_code == 409 and stale.json()["code"] == "stale"


def test_assist_apply_rejects_bad_input(monkeypatch, root):
    _mk_script(root, "s_bad")
    c = _client(monkeypatch, root)
    assert c.post("/api/script/assist/apply",
                  json={"key": "s_bad", "scene": 1}).status_code == 400
    assert c.post("/api/script/assist/apply",
                  json={"key": "s_bad", "scene": 99, "block": 1,
                        "base_hash": "x", "new_text": "y"}).status_code == 400
    assert c.post("/api/script/assist/apply",
                  json={"key": "nope", "scene": 1, "block": 1,
                        "base_hash": "x", "new_text": "y"}).status_code == 404


def test_assist_versions_and_restore(monkeypatch, root):
    _mk_script(root, "s_ver", scene_texts={1: "旧的第一段。\n\n旧的第二段。"})
    c = _client(monkeypatch, root)
    old = script_writer.read_scene("s_ver", 1)
    body = {"key": "s_ver", "scene": 1, "block": 2,
            "base_hash": _base_hash("s_ver", 1, 2), "new_text": "新的第二段。"}
    ver = c.post("/api/script/assist/apply", json=body).json()["version"]
    assert ver.startswith("第001场-")

    listed = c.get("/api/script/assist/versions",
                   params={"key": "s_ver", "scene": 1}).json()
    assert listed["ok"] is True
    assert ver in [i["version"] for i in listed["items"]]

    res = c.post("/api/script/assist/restore",
                 json={"key": "s_ver", "scene": 1, "version": ver})
    assert res.status_code == 200, res.text
    assert script_writer.read_scene("s_ver", 1).strip() == old.strip()


# ---------------------------------------------------------------- P4 建项立即回显

def test_create_script_returns_planning_immediately(monkeypatch, root):
    seen = {}

    def _fake_create(cfg, base_key=None):
        seen.update(cfg)
        time.sleep(0.05)

    monkeypatch.setattr(script_writer, "create_project", _fake_create)
    c = _client(monkeypatch, root)
    r = c.post("/api/create", json={"medium": "script", "title": "霓虹夜雨",
                                    "setting_era": "ancient", "setting_world": "北宋汴京"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "planning" and d["async"] is True
    assert d["name"] == "霓虹夜雨" and d["key"].startswith("s_")
    time.sleep(0.15)
    assert seen.get("setting_era") == "ancient"
    assert seen.get("setting_world") == "北宋汴京"


def test_create_script_auto_era_is_dropped(monkeypatch, root):
    seen = {}

    def _fake_create(cfg, base_key=None):
        seen.update(cfg)

    monkeypatch.setattr(script_writer, "create_project", _fake_create)
    c = _client(monkeypatch, root)
    r = c.post("/api/create", json={"medium": "script", "title": "古装探案",
                                    "setting_era": "auto"})
    assert r.status_code == 200
    time.sleep(0.1)
    assert "setting_era" not in seen
    assert seen.get("title") == "古装探案"
