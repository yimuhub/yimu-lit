"""v2.2.0 P3：剧本「世界观串代」修复的回归测试。

背景：古代题材的场次表/正文会整段写成当代医院、手机。修复分三层：
1. 时代判定 + 词表硬约束（script_checker）；
2. 每次生成都注入世界观硬约束（S0/S1/S3/S4/S5）；
3. S4 场次表与 S5 正文落盘前做确定性校验，违规则带反馈重生成一次，
   仍违规则暂停/告警，绝不静默通过。

所有 LLM 调用均 monkeypatch，不联网。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import script_checker as ck
from bot import script_writer as sw

DIRTY = "内景  客栈  日\n\n掌柜掏出手机，冲进医院。"
CLEAN = "内景  客栈  日\n\n掌柜提起灯笼，让店小二去请大夫。"


# ---------------------------------------------------------------- 1 时代判定


def test_guess_era_votes_by_wordlist():
    assert ck.guess_era("古代客栈里，掌柜提着灯笼迎客") == "ancient"
    assert ck.guess_era("他掏出手机，走进医院挂号") == "modern"
    assert ck.guess_era("") == ""
    assert ck.guess_era("一个关于选择的故事") == ""


def test_forbidden_terms_and_scan():
    assert "手机" in ck.forbidden_terms("ancient")
    assert "客栈" in ck.forbidden_terms("modern")
    assert ck.forbidden_terms("fantasy") == [] and ck.forbidden_terms("") == []

    issues = ck.scan("手机响了。他抓起手机，又拿起手机。", "ancient")
    assert len(issues) == 1
    assert issues[0]["term"] == "手机" and issues[0]["count"] == 3
    assert "手机" in issues[0]["snippet"]

    assert ck.check_text(CLEAN, "ancient")["ok"] is True
    bad = ck.check_text(DIRTY, "ancient")
    assert bad["ok"] is False and bad["era"] == "ancient"
    assert {i["term"] for i in bad["issues"]} >= {"手机", "医院"}


# ---------------------------------------------------------------- 2 硬约束与圣经头


def test_world_block_and_bible_header():
    cfg = {"setting_era": "ancient", "setting_world": "明代江南，市井喜剧，科举背景"}
    block = sw._world_block(cfg)
    assert "时代：古代" in block
    assert "明代江南" in block
    assert "本作禁止出现" in block and "手机" in block
    assert "一律视为无效" in block

    bare = sw._world_block({"setting_world": "无超自然设定"})
    assert "时代未指定，但全剧必须沿用同一时代设定" in bare
    assert "本作禁止出现" not in bare

    header = sw._bible_header(cfg, "古代喜剧")
    assert "时代" in header and "古代" in header and "世界观" in header


# ---------------------------------------------------------------- 3 正文生成的上下文


def test_scene_body_prompt_carries_bible_struct_and_world(monkeypatch):
    seen = []

    def _chat(msgs, *a, **k):
        seen.append("\n".join(m.get("content", "") for m in msgs))
        return CLEAN

    monkeypatch.setattr(sw.llm, "chat", _chat, raising=False)
    cfg = {"form": "film", "setting_era": "ancient", "setting_world": "明代市井"}
    scene = {"no": 2, "kind": "内景", "place": "客栈", "time": "日", "cast": "掌柜",
             "props": "茶盏", "setup": "客人上门", "trigger": "讨债", "obstacle": "无凭据",
             "outcome": "转折赢"}
    ctx = "【创作圣经】BIBLE_SENTINEL 控制思想\n\n【结构大纲】STRUCT_SENTINEL 第一幕"
    out = sw.gen_scene_body(scene, ctx, cfg)

    assert out == CLEAN and len(seen) == 1
    prompt = seen[0]
    assert "BIBLE_SENTINEL" in prompt
    assert "STRUCT_SENTINEL" in prompt
    assert "【世界设定·硬约束】" in prompt and "时代：古代" in prompt and "手机" in prompt


# ---------------------------------------------------------------- 4 正文重试与告警


def _make_project(tmp_path, *, era="ancient", scenes=None, bible=None):
    pdir = tmp_path / "s_era_test"
    pdir.mkdir()
    plan = {
        "scriptName": "古代喜剧", "medium": "script", "form": "film",
        "formLabel": "电影长片", "status": "planning",
        "config": {"title": "古代喜剧", "form": "film", "setting_era": era,
                   "setting_world": "明代江南市井"},
        "scenes": scenes or [{"no": 1, "kind": "内景", "place": "客栈", "time": "日",
                              "status": "pending", "fileName": "第001场.md"}],
        "meta": {"setting": {"era": era, "era_label": ck.ERA_LABELS.get(era, ""), "world": "明代江南"}},
    }
    (pdir / sw.F_PLAN).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (pdir / sw.F_BIBLE).write_text(bible or "# 圣经\n\n> 时代：古代 ｜ 世界观：明代江南\n\n古代客栈。",
                                   encoding="utf-8")
    (pdir / sw.F_CHARS).write_text("# 人物小传\n\n掌柜。", encoding="utf-8")
    (pdir / sw.F_STRUCT).write_text("# 结构大纲\n\n## 第一幕\n\n第 1-3 场：开端。", encoding="utf-8")
    return sw.ScriptProject(str(pdir), "古代喜剧")


def test_write_scenes_retries_once_then_clears_warn(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    plan = proj.plan()
    plan["scenes"][0]["warn"] = [{"term": "旧告警", "count": 1, "snippet": "旧"}]
    proj.save_plan(plan)

    calls = []

    def _gen(scene, extra, cfg):
        calls.append(extra)
        return DIRTY if len(calls) == 1 else CLEAN

    monkeypatch.setattr(sw, "gen_scene_body", _gen)
    r = sw.write_scenes(proj)

    assert r["ok"] and r["written"] == 1
    assert len(calls) == 2
    assert "【校验未过·必须修正】" in calls[1] and "医院" in calls[1]
    sc = proj.plan()["scenes"][0]
    assert sc["status"] == "completed"
    assert "warn" not in sc
    body = (tmp_path / "s_era_test" / sw.F_BODY / "第001场.md").read_text(encoding="utf-8")
    assert "手机" not in body and "灯笼" in body


def test_write_scenes_flags_persistent_drift_without_failing(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    calls = []

    def _gen(scene, extra, cfg):
        calls.append(extra)
        return DIRTY

    monkeypatch.setattr(sw, "gen_scene_body", _gen)
    r = sw.write_scenes(proj)

    assert r["ok"] and r["written"] == 1
    assert len(calls) == 2
    sc = proj.plan()["scenes"][0]
    assert sc["status"] == "completed"          # 不静默失败，但要留 warn
    assert sc["warn"] and {i["term"] for i in sc["warn"]} >= {"手机", "医院"}
    assert any("仍有时代偏差" in line for line in proj.events)


# ---------------------------------------------------------------- 5 S4 场次表守护


def test_conflicting_scene_table_pauses_before_auto_write(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(sw, "gen_premise", lambda cfg: "引擎：市井喜剧")
    monkeypatch.setattr(sw, "gen_characters", lambda cfg, premise="": "掌柜 / 店小二")
    monkeypatch.setattr(sw, "gen_structure", lambda cfg, premise="", chars="": "## 第一幕")
    calls = []

    def _gen_scenes(cfg, total, bible="", struct=""):
        calls.append(cfg.get("notes") or "")
        return ("| 场次 | 内/外 | 地点 | 时间 | 出场 | 道具 | 前提 | 触发 | 阻碍 | 结果 | 页数 |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|\n"
                "| 1 | 内景 | 医院 | 日 | 掌柜 | 手机 | 平静 | 电话响 | 无人接 | 得知真相 | 3 |")

    monkeypatch.setattr(sw, "gen_scenes", _gen_scenes)
    wrote = []
    monkeypatch.setattr(sw, "write_scenes", lambda *a, **k: wrote.append(1))

    proj = sw.create_project({"title": "古代喜剧", "idea": "古代客栈的掌柜",
                              "form": "film", "setting_era": "ancient",
                              "scenes": 1, "pages": 10})
    plan = proj.plan()

    assert len(calls) == 2, "场次表冲突必须带反馈重生成一次"
    assert "【校验未过·必须修正】" in calls[1]
    assert plan["meta"]["paused"] is True
    assert plan["meta"]["era_warnings"]
    assert "场次表与时代设定冲突" in "\n".join(proj.events)
    assert not wrote, "暂停状态下不得启动自动写正文"


# ---------------------------------------------------------------- 6 整项目体检


def test_check_project_flags_scene_and_summarises_terms(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "PROJECTS_DIR", str(tmp_path))
    pdir = tmp_path / "s_audit"
    (pdir / sw.F_BODY).mkdir(parents=True)
    plan = {
        "scriptName": "古代喜剧", "status": "in_progress",
        "scenes": [{"no": 1, "status": "completed", "fileName": "第001场.md"},
                   {"no": 2, "status": "completed", "fileName": "第002场.md"}],
        "meta": {"setting": {"era": "ancient"}},
    }
    (pdir / sw.F_PLAN).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (pdir / sw.F_BIBLE).write_text("> 时代：古代\n\n古代客栈。", encoding="utf-8")
    (pdir / sw.F_SCENES).write_text("| 场次 | 地点 |\n|---|---|\n| 1 | 客栈 |", encoding="utf-8")
    (pdir / sw.F_BODY / "第001场.md").write_text(CLEAN, encoding="utf-8")
    (pdir / sw.F_BODY / "第002场.md").write_text(DIRTY, encoding="utf-8")

    out = ck.check_project("s_audit")
    assert out["ok"] is False and out["era"] == "ancient"
    assert [it["scene"] for it in out["items"]] == [1, 2]
    assert out["items"][0]["issues"] == []
    assert out["items"][1]["issues"] and out["items"][1]["words"] > 0
    assert out["summary"] == {"scenes": 2, "flagged": 1,
                              "terms": {"手机": 1, "医院": 1}}

    # 圣经里的时代能兜底判定（计划没有 meta.setting 时）
    plan.pop("meta")
    (pdir / sw.F_PLAN).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    assert ck.check_project("s_audit")["era"] == "ancient"
