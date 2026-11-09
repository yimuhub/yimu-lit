"""v4.33.4 回归：整集稿不许被「按场批量写」覆盖，单场失败要重试且留痕。

用户实测：改编引擎 17:28 写完第 21 集（6410 字），17:35 被另一条「按场批量写」
的路径重写成 2138 字节 / 483 汉字的场次骨架；第 37 集同样从 2634 字被打成 504 字。
计划里的 wordCount 还是旧值，用户看到的是「稿子被吃了」。

契约：
1. 改编剧（plan 有 adaptedFrom）一律拒绝按场批量写；
2. 写正文这一层兜底：已合格的长稿不许被明显更短的文本顶掉；
3. 旧稿本来就是残片时允许覆盖（要能自我修复）；
4. 单场生成为空必须再试一次，两次都空才标待补，并且要落盘。
"""
import json
import os

from bot import script_writer as sw


class _Proj:
    """最小 proj 替身：只用到 log 与 path。"""

    def __init__(self, path):
        self.path = str(path)
        self.name = "测试剧本"
        self.lines = []

    def log(self, msg):
        self.lines.append(msg)


def _mk(tmp_path, *, adapted=False):
    d = tmp_path / "s_proj"
    (d / sw.F_BODY).mkdir(parents=True)
    plan = {"version": 1, "scriptName": "测试剧本", "medium": "script",
            "status": "in_progress", "config": {}, "meta": {},
            "scenes": [{"no": 1, "status": "pending", "fileName": "第001场.md",
                        "wordCount": 0, "title": "第1集"}]}
    if adapted:
        plan["adaptedFrom"] = {"key": "20260101-000000-原小说"}
        plan["adaptation"] = {"version": 2}
    (d / sw.F_PLAN).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return sw.ScriptProject(str(d), "测试剧本"), d


# ---------------------------------------------------------------- 入口总闸

def test_adapted_drama_rejects_scene_writes(tmp_path, monkeypatch):
    """改编剧不能再走「按场批量写」——这是第 21 集被吃掉的入口。"""
    proj, d = _mk(tmp_path, adapted=True)

    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("改编剧不该进 _write_scenes_inner")

    monkeypatch.setattr(sw, "_write_scenes_inner", _boom)
    r = sw.write_scenes(proj)

    assert r["ok"] is False and r.get("adapted") is True
    assert called["n"] == 0
    assert "拒绝" in r["error"]
    log = (d / "运行日志.txt").read_text(encoding="utf-8")
    assert "拒绝" in log and "改编" in log


def test_plain_script_still_writes(tmp_path, monkeypatch):
    """普通原创剧本不受影响。"""
    proj, _ = _mk(tmp_path)
    monkeypatch.setattr(sw, "_write_scenes_inner",
                        lambda proj, only, batch_size=None: {"ok": True, "written": 1})
    assert sw.write_scenes(proj)["ok"] is True


def test_is_adapted_drama_detects_plan():
    assert sw.is_adapted_drama({"adaptedFrom": {"key": "x"}}) is True
    assert sw.is_adapted_drama({"adaptedFrom": {}}) is False
    assert sw.is_adapted_drama({"adaptedFrom": {"key": "  "}}) is False
    assert sw.is_adapted_drama({"scenes": []}) is False
    assert sw.is_adapted_drama(None) is False


# ---------------------------------------------------------------- 写入兜底

def test_short_draft_cannot_overwrite_long_body(tmp_path):
    """3000 字旧稿，400 字新稿 → 拒绝覆盖，原稿不动。"""
    d = tmp_path / "body"
    d.mkdir()
    fp = d / "第001场.md"
    old = "甲" * 3000
    fp.write_text(old, encoding="utf-8")

    proj = _Proj(tmp_path)
    wrote = sw._write_body_guarded(str(fp), "乙" * 400, proj, 1)

    assert wrote is False
    assert fp.read_text(encoding="utf-8") == old
    assert any("拒绝覆盖" in x for x in proj.lines)


def test_garbage_old_body_can_be_replaced(tmp_path):
    """旧稿本来就是残片（300 字）→ 允许覆盖，要能自我修复。"""
    d = tmp_path / "body"
    d.mkdir()
    fp = d / "第001场.md"
    fp.write_text("甲" * 300, encoding="utf-8")

    proj = _Proj(tmp_path)
    wrote = sw._write_body_guarded(str(fp), "乙" * 1200, proj, 1)

    assert wrote is True
    assert len(fp.read_text(encoding="utf-8")) == 1200


def test_missing_file_is_written(tmp_path):
    d = tmp_path / "body"
    d.mkdir()
    fp = d / "第001场.md"
    proj = _Proj(tmp_path)
    assert sw._write_body_guarded(str(fp), "丙" * 1500, proj, 1) is True
    assert fp.exists()


# ---------------------------------------------------------------- 空稿重试

def test_empty_scene_retries_once(tmp_path, monkeypatch):
    """第一次返回空、第二次有正文 → 用第二次的结果。"""
    proj = _Proj(tmp_path)
    seq = ["", "场1：内 密室 夜\n\n甲：走。"]

    def _gen(sc, extra, cfg):
        return seq.pop(0) if seq else ""

    monkeypatch.setattr(sw, "gen_scene_body", _gen)
    text = sw._gen_scene_body_resilient({}, "extra", {}, proj, 3)

    assert "场1" in text
    assert any("再试一次" in x for x in proj.lines)


def test_scene_gives_up_after_two_empty(tmp_path, monkeypatch):
    """两次都空 → 返回空串，交给上层标待补。"""
    proj = _Proj(tmp_path)
    monkeypatch.setattr(sw, "gen_scene_body", lambda sc, e, c: "")
    assert sw._gen_scene_body_resilient({}, "extra", {}, proj, 3) == ""


def test_empty_first_then_ok_does_not_double_log_success(tmp_path, monkeypatch):
    """第二次成功时不该留「再试一次」以外的噪音，且结果非空。"""
    proj = _Proj(tmp_path)
    seq = ["", "场2：外 山道 昏"]

    def _gen(sc, extra, cfg):
        return seq.pop(0) if seq else ""

    monkeypatch.setattr(sw, "gen_scene_body", _gen)
    assert sw._gen_scene_body_resilient({}, "e", {}, proj, 4) == "场2：外 山道 昏"
    assert len(proj.lines) == 1


# ---------------------------------------------------------------- 文档契约

def test_retry_prompt_asks_for_body_directly():
    """重试提示必须明确要求直接出正文（空响应多半是模型回了空/说明）。"""
    import inspect
    src = inspect.getsource(sw._gen_scene_body_resilient)
    assert "直接输出本场剧本正文" in src
    assert "再试一次" in src


def test_guard_constants_are_sane():
    assert sw._BODY_KEEP_CHARS >= 500, "门槛太低会把残片当成合格稿保住"
    assert os.path.basename(sw.F_BODY) == "04-正文"
