"""v4.33.7 统一交付标准回归：两篇成稿中出现过的每一类不合格内容，
都必须被编号规则稳定检出；规则配置、校验器、流程接入点三者缺一不可。

本文件不修订任何稿件，只锁死「后续每次创作按同一标准执行」的机制：
1. 规格完整性：问题类型都有编号/口径/挂载节点/处置动作；
2. 24 类问题类型各有一条正例与一条干净反例；
3. 导出闸门：enforcing 档有禁止项即拒绝，advisory 只报告；
4. 流程接入：单集门禁自动回灌、全书体检输出 finding、导出报告可读。
"""
import json

import pytest

from bot import continuity, delivery_gate as gate, delivery_spec as spec, drama_adapter, export


def codes(findings):
    return {f["code"] for f in findings}


# ---------------------------------------------------------------- 规格完整性

def test_spec_is_complete_and_consistent():
    s = spec.spec()
    assert s["version"] == spec.SPEC_VERSION
    assert len(s["problem_types"]) >= 20
    for p in s["problem_types"]:
        assert p["code"] and p["rule"] and p["threshold"] and p["action"]
        assert p["stage"] in s["stages"]
        assert p["severity"] in ("block", "warn", "info")
        assert p["scope"] in ("text", "novel", "script")
    for stage, items in s["stages"].items():
        assert all(c in spec.BY_CODE for c in items), stage
    for name, prof in s["profiles"].items():
        assert prof["enforcement"] in ("advisory", "enforcing")
        assert all(c in spec.BY_CODE for c in prof["block_on_export"]), name


def test_rules_for_stage_filters_scope():
    assert all(p["scope"] in ("script", "text") for p in spec.rules_for("draft", "script"))
    assert "N09" in {p["code"] for p in spec.rules_for("final")}


# ---------------------------------------------------------------- 小说问题类型

CLEAN_CH = "林野推开木门，风从石阶上掠过。他把退婚书收进怀里，转身走向广场。远处传来号角声。"

@pytest.mark.parametrize("code,text,kwargs", [
    ("N01", "林野听见 Host 的声音在脑海响起。", {}),
    ("N02", "本章评分：九十五分。", {}),
    ("N03", "他刚要开口，", {}),
    ("N04", CLEAN_CH, {"title": "第50章 第50章"}),
    ("N05", "太短。", {}),
    ("N06", "林野睁开眼，看见广场。\n\n林野睁开眼，看见广场。风从石阶上掠过，他站起身。", {}),
    ("N07", "一丝 仿佛 似乎 缓缓 微微 瞬间 眼中闪过 深邃。", {}),
    ("N08", "他站起身，吹熄了灯。", {}),
])
def test_novel_chapter_problems_detected(code, text, kwargs):
    assert code in codes(gate.check_chapter(text, chapter_no=7, **kwargs))


def test_clean_chapter_has_no_blocking_problem():
    body = CLEAN_CH * 20 + "忽然，远处传来一声低沉的兽吼。"
    found = gate.check_chapter(body, title="弃子与退婚书", chapter_no=1)
    assert not [f for f in found if f["severity"] == "block"]


@pytest.mark.parametrize("code,chapters,kwargs", [
    ("N09", [{"n": 1, "title": "一", "text": "系统已激活，林野握紧拳头。"},
             {"n": 2, "title": "二", "text": "系统已激活，林野握紧拳头。"}], {}),
    ("N10", [{"n": 1, "title": "一", "text": CLEAN_CH},
             {"n": 2, "title": "终章", "text": "他抬起头。游戏才刚刚开始。"}], {}),
    ("N11", [{"n": 1, "title": "一", "text": CLEAN_CH}],
     {"book_title": "收服十万巨兽后，我无敌了"}),
    ("N12", [{"n": 1, "title": "一", "text": "Host 提醒他撤。"}],
     {"entity_aliases": {"小满": ["Host"]}}),
])
def test_novel_book_problems_detected(code, chapters, kwargs):
    rep = gate.check_novel(chapters, **kwargs)
    assert code in codes(rep["findings"])


def test_novel_report_metrics_and_authoritative_structure():
    rep = gate.check_novel([{"n": 1, "title": "一", "text": CLEAN_CH}])
    assert rep["spec_version"] == spec.SPEC_VERSION
    assert rep["kind"] == "novel" and rep["metrics"]["chapters"] == 1
    assert "hook_rate" in rep["metrics"]
    assert isinstance(rep["findings"], list) and "block" in rep["counts"]


# ---------------------------------------------------------------- 剧本问题类型

CLEAN_EP_HEAD = "**场1：外 部落广场 白天**\n\n林野猛地睁眼，撑地起身。\n\n"

@pytest.mark.parametrize("code,text", [
    ("S01", CLEAN_EP_HEAD + "林野：（深吸一口气）我们走。\n\n忽然，号角响起。"),
    ("S02", CLEAN_EP_HEAD + "林野咬牙：好。\n\n忽然，号角响起。"),
    ("S03", CLEAN_EP_HEAD + "林野：" + "这句台词实在太长需要拆成两句才适合配音朗读" * 2 + "。"),
    ("S05", "**场1：外 广场 日**\n\n**第2场：内 木屋 夜**\n\n林野：走。"),
    ("S07", CLEAN_EP_HEAD + "他心头一紧，眼神复杂。\n\n忽然，号角响起。"),
    ("S09", CLEAN_EP_HEAD + "苏清柔：你来了。\n林野：你来了。\n\n忽然，号角响起。"),
    ("S10", "林野站在那里。\n\n林野：走吧。"),
    ("S11", CLEAN_EP_HEAD + "小满：Host 已上线。\n\n忽然，号角响起。"),
])
def test_script_episode_problems_detected(code, text):
    assert code in codes(gate.check_episode(text, ep_no=1))


def test_episode_budget_and_long_line_are_separate():
    long_line = "林野：" + "他必须立刻离开这片山谷否则追兵就会赶到" * 12 + "。\n"
    text = CLEAN_EP_HEAD + long_line * 3
    found = codes(gate.check_episode(text, ep_no=1))
    assert {"S03", "S04"} <= found


def test_script_final_problems_detected():
    eps = [
        {"n": 1, "text": "**场1：外 古营外 夜**\n\n林野：走。\n\n忽然，号角响起。"},
        {"n": 2, "text": "**场1：外 古营外围 夜**\n\n林野：撤。\n\n忽然，号角响起。"},
    ]
    rep = gate.check_script(eps, assets={})
    found = codes(rep["findings"])
    assert {"S06", "S08"} <= found
    assert rep["metrics"]["episodes"] == 2


def test_script_duration_over_profile():
    line = "林野：" + "我们必须在天亮之前穿过这片裂谷并找到源点入口" * 2 + "。\n"
    eps = [{"n": 1, "text": CLEAN_EP_HEAD + line * 12}]
    assert "S12" in codes(gate.check_script(eps, profile="script_production")["findings"])


def test_clean_episode_has_no_blocking_problem():
    body = CLEAN_EP_HEAD + "林野：走。\n苏红绫：等等。\n\n忽然，远处号角响起。\n\n" + \
           "**场2：内 木屋 夜**\n\n林野推门而入。\n\n林野：灯还亮着。\n\n黑屏，出集名。"
    assert not [f for f in gate.check_episode(body, ep_no=1) if f["severity"] == "block"]


# ---------------------------------------------------------------- 闸门与流程接入

def test_export_gate_enforcing_vs_advisory():
    rep = gate.check_novel([{"n": 1, "text": "林野听见 Host 的声音。"}],
                           profile="novel_submission")
    assert rep["blocking_count"] > 0
    ok, _ = gate.export_ready(rep, enforcement="advisory")
    assert ok
    ok, msg = gate.export_ready(rep, enforcement="enforcing")
    assert not ok and "N01" in msg


def test_report_and_rewrite_directives_are_machine_readable():
    rep = gate.check_novel([{"n": 1, "text": "林野听见 Host 的声音。"}],
                           profile="novel_submission")
    md = gate.report_markdown(rep)
    assert "交付就绪报告" in md and "N01" in md
    directives = gate.rewrite_directives(rep["findings"])
    assert directives and all(d.startswith("[") for d in directives)
    assert all(spec.BY_CODE[d[1:4]]["severity"] == "block" for d in directives)


def _compliant_episode(extra=""):
    filler = "他站在广场中央，风从石阶上掠过，尘土扬起又落下。" * 32
    return ("**场1：外 部落广场 白天**\n\n" + filler + "\n\n林野：走。\n苏红绫：等等。\n\n" +
            "**场2：内 木屋 夜**\n\n林野推门而入。\n\n林野：灯还亮着。\n\n" + extra +
            "忽然，远处号角响起。")


def test_validate_episode_blocks_latin_pollution_and_passes_clean():
    ok, _ = drama_adapter.validate_episode(_compliant_episode())
    assert ok is True
    ok, why = drama_adapter.validate_episode(_compliant_episode("小满：Host 已上线。\n\n"))
    assert ok is False and "S11" in why


def test_continuity_exposes_delivery_findings(tmp_path):
    pdir = tmp_path / "book"
    pdir.mkdir()
    (pdir / "第1章.md").write_text("林野听见 Host 的声音，握紧了拳头。", encoding="utf-8")
    (pdir / "02-写作计划.json").write_text(json.dumps({
        "chapters": [{"chapterNumber": 1, "title": "一", "filePath": "第1章.md"}]},
        ensure_ascii=False), encoding="utf-8")
    out = continuity.run_project(str(pdir))
    assert any("交付·N01" in f["check"] and f["severity"] == "critical"
               for f in out["findings"])


class _FakeProj:
    def __init__(self, path, name="测试小说"):
        self.path = str(path)
        self.name = name

    def plan(self):
        return {"novelName": "测试小说",
                "chapters": [{"chapterNumber": 1, "title": "一", "filePath": "第01章.md"}]}

    def body_dir(self):
        return self.path


def test_export_delivery_report_is_shared_entry(tmp_path):
    (tmp_path / "第01章.md").write_text(CLEAN_CH, encoding="utf-8")
    rep = export.delivery_report(_FakeProj(tmp_path))
    assert rep["kind"] in ("novel", "mini")
    assert "spec_version" in rep and "findings" in rep


def test_stamp_plan_only_marks_new_works():
    fresh = {"status": "planning"}
    spec.stamp_plan(fresh, "novel")
    assert fresh["deliverySpecVersion"] == spec.SPEC_VERSION
    assert fresh["deliveryProfile"] == "novel_submission"
    legacy = {"status": "in_progress"}
    spec.stamp_plan(legacy, "novel", plan_exists=True)
    assert "deliverySpecVersion" not in legacy, "已有在创作品不得追溯盖章"
    assert spec.stamp_plan({}, "script")["deliveryProfile"] == "script_production"
    assert spec.stamp_plan({}, "mini")["deliveryProfile"] == "universal"
    stale = {"status": "in_progress"}
    spec.stamp_plan(stale, "novel", plan_exists=True,
                    existing={"deliverySpecVersion": spec.SPEC_VERSION,
                              "deliveryProfile": "novel_submission"})
    assert stale["deliverySpecVersion"] == spec.SPEC_VERSION, "旧快照不得冲掉规格章"


def test_export_gate_blocks_new_works_but_not_legacy(monkeypatch, tmp_path):
    """盖章新作品整书导出必过闸；历史作品只报告；force=1 是显式取回通道。"""
    import app as appmod
    from fastapi.testclient import TestClient
    from bot import writer

    pdir = tmp_path / "book"
    pdir.mkdir()
    (pdir / "第001章.md").write_text("林野听见 Host 的声音，握紧了拳头。", encoding="utf-8")
    plan = {"novelName": "测试书", "status": "completed",
            "deliverySpecVersion": spec.SPEC_VERSION,
            "deliveryProfile": "novel_submission",
            "chapters": [{"chapterNumber": 1, "title": "开端", "status": "completed",
                          "filePath": "第001章.md"}], "meta": {}}
    plan_path = pdir / "02-写作计划.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    c = TestClient(appmod.app)

    assert c.get("/api/export?key=book&fmt=md").status_code == 409
    assert c.get("/api/export?key=book&fmt=md&force=1").status_code == 200
    assert "交付就绪报告" in (pdir / "22-交付就绪报告.md").read_text(encoding="utf-8")

    plan.pop("deliverySpecVersion")
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    appmod.PROJECT_INDEX.clear()
    assert c.get("/api/export?key=book&fmt=md").status_code == 200


def test_check_delivery_routes_by_kind():
    mini = gate.check_delivery([{"n": 1, "text": "林野听见 Host 的声音。"}], kind="mini")
    assert mini["kind"] == "mini" and "N01" in codes(mini["findings"])
    script = gate.check_delivery([{"n": 1, "text": "**场1：外 广场 日**\n\n林野：走。"}],
                                 kind="script")
    assert script["kind"] == "script" and "metrics" in script
