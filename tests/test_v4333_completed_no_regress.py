"""v4.33.3 回归：计划里的「已完成」不许被旧快照打回去。

用户实拍：改编跑到 33/50 集、87875 字，点了一次「继续」之后，计划文件里的
completed 掉回 19 集、字数少了 3 万——正文文件全都还在磁盘上，只是状态被
另一条写入路径用**比磁盘旧的快照**整份覆盖了。用户看到的是「白写了」。

契约：
1. 磁盘上已 completed 且正文文件存在的集，任何保存都不许降级；
2. 显式重写（scene 带 rewriting 标记）放行，不能把重写卡死；
3. 正文文件已被删掉时放行，允许重新生成；
4. 改编循环里失败集不能算进「已完成 N 集」。
"""
import json
import os

from bot import script_writer as sw


def _scene(no, status, *, words=1500, rewriting=False):
    row = {"no": no, "kind": "集", "place": f"第 {no} 集", "title": f"第{no}集",
           "status": status, "fileName": f"第{no:03d}场.md", "wordCount": words if status == "completed" else 0}
    if rewriting:
        row["rewriting"] = True
    return row


def _mkproj(tmp_path):
    d = tmp_path / "s_proj"
    (d / sw.F_BODY).mkdir(parents=True)
    proj = sw.ScriptProject(str(d), "测试剧本")
    return proj, d


def _write_body(d, no, chars=1200):
    (d / sw.F_BODY / f"第{no:03d}场.md").write_text("场" * chars, encoding="utf-8")


def _plan(scenes):
    return {"version": 1, "scriptName": "测试剧本", "medium": "script",
            "status": "in_progress", "scenes": scenes, "config": {}, "meta": {}}


def test_stale_snapshot_cannot_reset_completed(tmp_path):
    """核心场景：旧快照只有第 1 集完成，磁盘上已有 3 集完成 → 不许退回 1 集。"""
    proj, d = _mkproj(tmp_path)
    for n in (1, 2, 3):
        _write_body(d, n)
    proj.save_plan(_plan([_scene(n, "completed") for n in (1, 2, 3)]))

    stale = _plan([_scene(1, "completed"),
                   _scene(2, "pending"), _scene(3, "pending")])
    proj.save_plan(stale)

    got = json.load(open(os.path.join(str(d), sw.F_PLAN), encoding="utf-8"))
    assert [s["status"] for s in got["scenes"]] == ["completed"] * 3
    assert all(s["wordCount"] for s in got["scenes"]), "字数也要留住，不然进度条还是掉"


def test_failed_cannot_overwrite_completed(tmp_path):
    """失败的集也不能盖掉已完成的集（改编线程偶发失败时常见）。"""
    proj, d = _mkproj(tmp_path)
    _write_body(d, 1)
    proj.save_plan(_plan([_scene(1, "completed")]))

    proj.save_plan(_plan([_scene(1, "failed", words=0)]))

    got = json.load(open(os.path.join(str(d), sw.F_PLAN), encoding="utf-8"))
    assert got["scenes"][0]["status"] == "completed"
    assert "error" not in got["scenes"][0]


def test_rewrite_marker_is_allowed_to_regress(tmp_path):
    """显式重写要能降级，否则「重写这一集」永远点不动。"""
    proj, d = _mkproj(tmp_path)
    _write_body(d, 1)
    proj.save_plan(_plan([_scene(1, "completed")]))

    proj.save_plan(_plan([_scene(1, "pending", rewriting=True)]))

    got = json.load(open(os.path.join(str(d), sw.F_PLAN), encoding="utf-8"))
    assert got["scenes"][0]["status"] == "pending"


def test_missing_body_file_is_allowed_to_regress(tmp_path):
    """正文被删掉时允许降级——否则重新生成会被完成态挡住。"""
    proj, d = _mkproj(tmp_path)
    _write_body(d, 1)
    proj.save_plan(_plan([_scene(1, "completed")]))
    os.remove(os.path.join(str(d), sw.F_BODY, "第001场.md"))

    proj.save_plan(_plan([_scene(1, "pending")]))

    got = json.load(open(os.path.join(str(d), sw.F_PLAN), encoding="utf-8"))
    assert got["scenes"][0]["status"] == "pending"


def test_status_counts_completed_from_disk(tmp_path):
    """对外的 status 只认磁盘上的真实完成态。"""
    proj, d = _mkproj(tmp_path)
    for n in (1, 2):
        _write_body(d, n)
    proj.save_plan(_plan([_scene(n, "completed") for n in (1, 2)]
                         + [_scene(3, "pending", words=0)]))
    st = proj.status()
    assert st["done"] == 2 and st["total"] == 3
    assert st["medium"] == "script"
