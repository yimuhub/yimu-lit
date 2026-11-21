"""v5.4.0 拆书工作台（bot/breakdown.py + bot/webbreakdown.py）回归测试。

覆盖（docs/系统化升级方案 8.1 验收标准 + 任务书要求）：
- 切章防误切雷例：正文中提「第三章」、带句号标题行、中文数字含万、无章作品、
  超长标题、序号不增的正文回指、卷标题；
- 拆解 JSON 容错（越界段落号/倒挂区间/缺字段→整形；非法输出→自动重试一次→失败章）；
- 全书报告证据逐字校验（命中保留 + 段落区间定位；未命中丢弃；太短丢弃）；
- 手法入账本（复用 lexicon_ledger 数据结构；描述携带原文片段按纯净域拒绝）；
- 项目存储轮转（MAX_PROJECTS 上限删最旧）；
- HTTP 路由（独立 FastAPI 应用挂 webbreakdown.router，不依赖 app.py）。

全部用例跑在 tmp_path，不碰真实 data/breakdowns/，不烧真 LLM（stub llm.chat_json）。
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import breakdown as bd
from bot import lexicon_ledger


# ---------------------------------------------------------------- 基础设施

@pytest.fixture()
def bddir(tmp_path, monkeypatch):
    """DATA_DIR 指向临时目录（项目 JSON 与手法账本目录全部隔离）。"""
    d = tmp_path / "breakdowns"
    d.mkdir()
    monkeypatch.setattr(bd, "DATA_DIR", str(d))
    return d


def _mk_stub(responses):
    """按调用序出队的 chat_json stub；返回 (stub, 调用记录)。"""
    calls = []
    queue = list(responses)

    def _stub(messages, **kw):
        calls.append({"system": messages[0]["content"], "user": messages[-1]["content"]})
        item = queue.pop(0) if queue else {}
        if isinstance(item, Exception):
            raise item
        return item

    return _stub, calls


def _patch_llm(monkeypatch, responses):
    stub, calls = _mk_stub(responses)
    monkeypatch.setattr(bd.llm, "chat_json", stub)
    return calls


def _mk_project(bddir, text, title="测试书"):
    r = bd.create_project(title, text, "paste")
    assert r["ok"], r
    return r["project"]


def _wait_done(bd_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = bd.load_project(bd_id)
        if p and not bd.is_running(bd_id) and p.get("status") in ("done", "partial"):
            return p
        time.sleep(0.05)
    raise AssertionError("拆解任务超时未结束")


BOOK = (
    "第一章 入局\n\n林然睁开眼，想起第三章里的那句话，冷汗直流。他缓缓坐起。\n\n"
    "窗外传来更夫的敲梆声。\n\n"
    "第二章 深巷\n\n巷子很深，青石板上积着雨水。\n\n"
    "第三章 反杀\n\n夜色压下来。\n\n他握紧了刀。刀刃在月光下泛着冷光。\n\n"
    "第四章 收网\n\n风停了。\n\n更夫的敲梆声停在了巷口。\n\n他数到三。\n"
)


# ---------------------------------------------------------------- 切章防误切

class TestSplit:
    def test_body_reference_not_split(self, bddir):
        """第一章正文里提「第三章」：带标点的长行必须被句读守卫拦下。"""
        r = bd.split_chapters(BOOK)
        assert r["ok"]
        nums = [c["num"] for c in r["chapters"]]
        assert nums == [1, 2, 3, 4]
        # 回指文本留在第一章正文里
        assert any("第三章里的那句话" in p for p in r["chapters"][0]["paras"])

    def test_standalone_reference_loses_to_real_chain(self, bddir):
        """独立成行、无标点的「第二章的旧账」出现在第四章后：单调链 + 独立行加分选出真章序列。"""
        text = (
            "第一章 起\n\n正文甲。\n\n第二章 承\n\n正文乙。\n\n第三章 转\n\n正文丙。\n\n"
            "第四章 合\n\n第二章的旧账\n\n正文继续。\n\n第五章 收\n\n正文丁。\n"
        )
        r = bd.split_chapters(text)
        assert [c["num"] for c in r["chapters"]] == [1, 2, 3, 4, 5]
        # 回指行留在第四章正文里
        assert any("第二章的旧账" in p for p in r["chapters"][3]["paras"])

    def test_punctuated_title_line_rejected(self):
        """带句号/叹号/问号的行不算标题。"""
        text = "第一章 开端\n\n正文。\n\n第二章。风起。\n\n这不是标题。\n\n第三章 真章\n\n来了。\n"
        r = bd.split_chapters(text)
        assert [c["num"] for c in r["chapters"]] == [1, 3]
        assert all("。" not in c["title"] for c in r["chapters"])

    def test_cn_number_with_wan(self):
        """中文数字含万（第一万二千三百章）。"""
        text = "第一章 起\n\n甲。\n\n第二掌\n\n占位。\n"  # 干扰项：不是章
        r = bd.split_chapters(text)
        assert [c["num"] for c in r["chapters"]] == [1]
        big = "第一章 起\n\n甲。\n\n第一万二千三百章 终\n\n乙。\n"
        r2 = bd.split_chapters(big)
        # 12300 与 1 之间 gap > 60 → 大章号被判定为回指，不串链
        assert [c["num"] for c in r2["chapters"]] == [1]

    def test_cn_number_wan_sequence(self):
        """万级章号自身成序列（第一万章 → 第一万零一章）能正常切。"""
        text = "第一万章 起\n\n甲。\n\n第一万零一章 承\n\n乙。\n"
        r = bd.split_chapters(text)
        assert [c["num"] for c in r["chapters"]] == [10000, 10001]

    def test_no_chapters_single_fallback(self):
        """识别不到任何章：整本作为单章并给警告。"""
        r = bd.split_chapters("这是一篇没有章节标记的作品。\n\n第二段正文。就这样。\n\n第三段。")
        assert r["ok"]
        assert len(r["chapters"]) == 1
        assert r["chapters"][0]["golden"] is True
        assert r["warnings"] and "单章" in r["warnings"][0]

    def test_overlong_title_rejected(self):
        """章名 ≤60 字：超长标题行不算标题。"""
        long_title = "头" * 61
        text = f"序章一空。\n\n第一章 {long_title}\n\n正文甲。\n\n第二章 正常\n\n正文乙。\n"
        r = bd.split_chapters(text)
        nums = [c["num"] for c in r["chapters"]]
        assert nums == [2]   # 第一章的超长标题行被拒（前置文本不给警告说明体量小）
        assert all(len(c["title"]) <= 80 for c in r["chapters"])

    def test_overlong_title_keeps_in_front_matter_warning(self):
        """前置文本体量大时如实提醒（防「内容少了」被当成丢稿）。"""
        front = "这是书名页。" + "简介内容。" * 40
        text = f"{front}\n\n第一章 起\n\n正文。\n"
        r = bd.split_chapters(text)
        assert len(r["chapters"]) == 1
        assert any("前置文本" in w for w in r["warnings"])

    def test_increasing_reference_inside_body(self):
        """序号递增的回指（第一章体内出现独立行「第三章的约定」）敌不过真章链。"""
        text = (
            "第一章 起\n\n想起往事。\n\n第三章的约定\n\n第一章正文继续。\n\n"
            "第二章 承\n\n正文。\n\n第三章 真·转\n\n正文。\n\n第四章 合\n\n正文。\n"
        )
        r = bd.split_chapters(text)
        assert [c["num"] for c in r["chapters"]] == [1, 2, 3, 4]
        assert any("第三章的约定" in p for p in r["chapters"][0]["paras"])

    def test_volumes_grouping(self):
        """卷标题分组展示，不影响切章；带句号的卷回指被拒。"""
        text = (
            "第一卷 风起\n\n第一章 起\n\n正文一。\n\n第二卷 云涌\n\n第三章 转\n\n正文三。\n"
            "他在第三卷的回忆里提到过这件事。\n"
        )
        r = bd.split_chapters(text)
        assert [v["num"] for v in r["volumes"]] == [1, 2]
        assert [c["num"] for c in r["chapters"]] == [1, 3]
        assert r["chapters"][0]["volume"] == "第一卷 风起"
        assert r["chapters"][1]["volume"] == "第二卷 云涌"

    def test_empty_text(self):
        assert bd.split_chapters("   ")["ok"] is False
        assert bd.split_chapters("")["ok"] is False

    def test_overlong_text_rejected(self, monkeypatch):
        monkeypatch.setattr(bd, "MAX_TEXT_CHARS", 100)
        r = bd.split_chapters("字" * 200)
        assert r["ok"] is False and "上限" in r["error"]

    def test_max_chapters_truncated(self, monkeypatch):
        monkeypatch.setattr(bd, "MAX_CHAPTERS", 5)
        text = "\n\n".join(f"第{i}章 标题{i}\n\n正文{i}。" for i in range(1, 9))
        r = bd.split_chapters(text)
        assert len(r["chapters"]) == 5
        assert any("上限" in w for w in r["warnings"])


# ---------------------------------------------------------------- 拆解 JSON 容错与重试

ANALYSIS_OK = {
    "brief": "主角醒来，发现被囚。",
    "rhythm": [{"para": 1, "type": "hook", "note": "开局悬念"},
               {"para": 99, "type": "twist", "note": "越界段号"},   # 越界 → clamp
               {"para": "x", "type": "twist", "note": "非法段号"}],  # 非法 → 丢弃
    "conflict": "人身自由 vs 生存",
    "value_shift": "安全→危险",
    "hook": {"type": "危机钩", "text": "门外传来脚步声"},
    "outline": [{"startPara": 5, "endPara": 1, "title": "倒挂", "summary": "自动交换"},
                {"startPara": 1, "endPara": 2, "title": "开端", "summary": "醒来"},
                {"startPara": 0, "endPara": 9, "title": "越界", "summary": "clamp"}],
    "techniques": ["以景写情", "X" * 40],   # 超长截断
    "characters": ["林然", "", "路人甲"],
}


class TestAnalyze:
    def test_coerce_clamps_and_drops(self, bddir, monkeypatch):
        _patch_llm(monkeypatch, [ANALYSIS_OK])
        proj = _mk_project(bddir, BOOK)
        ch = proj["chapters"][0]
        assert bd.analyze_chapter(proj, ch) is True
        a = ch["analysis"]
        n = len(ch["paras"])
        assert a["brief"] == "主角醒来，发现被囚。"
        assert [r["para"] for r in a["rhythm"]] == [1, min(99, n)]     # 越界 clamp，非法丢弃
        assert all(1 <= r["para"] <= n for r in a["rhythm"])
        assert [(o["startPara"], o["endPara"]) for o in a["outline"]] == [(1, 2), (1, 2), (1, 2)]
        assert a["techniques"][1] == "X" * 24
        assert a["characters"] == ["林然", "路人甲"]
        assert a["para_count"] == n

    def test_golden_only_first_three(self, bddir, monkeypatch):
        _patch_llm(monkeypatch, [ANALYSIS_OK] * 4)
        proj = _mk_project(bddir, BOOK)
        assert [c["golden"] for c in proj["chapters"]] == [True, True, True, False]
        for ch in proj["chapters"]:
            assert bd.analyze_chapter(proj, ch) is True
        assert proj["chapters"][0]["analysis"]["golden"] is not None
        assert proj["chapters"][3]["analysis"]["golden"] is None

    def test_json_failure_retry_then_fail(self, bddir, monkeypatch):
        """JSON 解析失败自动重试一次；仍失败 → 失败章（可重试）。"""
        calls = _patch_llm(monkeypatch, [ValueError("不是 JSON"), ValueError("仍不是 JSON")])
        proj = _mk_project(bddir, BOOK)
        ch = proj["chapters"][0]
        assert bd.analyze_chapter(proj, ch) is False
        assert len(calls) == 2                       # 自动重试了一次
        assert ch["state"] != "done" and ch["error"]
        assert "JSON" in ch["error"]

    def test_retry_second_attempt_succeeds(self, bddir, monkeypatch):
        calls = _patch_llm(monkeypatch, [{"garbage": "非 dict 返回"}, ANALYSIS_OK])
        proj = _mk_project(bddir, BOOK)
        ch = proj["chapters"][0]
        assert bd.analyze_chapter(proj, ch) is True
        assert len(calls) == 2
        assert ch["analysis"]["brief"]

    def test_empty_dict_counts_as_failure(self, bddir, monkeypatch):
        """mock/异常路径返回 {} → 视为解析失败（重试后仍失败）。"""
        _patch_llm(monkeypatch, [{}, {}])
        proj = _mk_project(bddir, BOOK)
        assert bd.analyze_chapter(proj, proj["chapters"][0]) is False

    def test_scene_label(self, bddir, monkeypatch):
        """LLM 调用带「拆书」场景标签。"""
        seen = {}

        def _stub(messages, **kw):
            seen.update(kw)
            return ANALYSIS_OK

        monkeypatch.setattr(bd.llm, "chat_json", _stub)
        proj = _mk_project(bddir, BOOK)
        bd.analyze_chapter(proj, proj["chapters"][0])
        assert seen.get("scene") == "拆书"

    def test_numbered_paras_capped(self, bddir, monkeypatch):
        """超长章取前 400 段送拆。"""
        monkeypatch.setattr(bd, "MAX_PARAS_PER_CH", 5)
        calls = _patch_llm(monkeypatch, [ANALYSIS_OK])
        long_book = "第一章 长章\n\n" + "\n\n".join(f"第{i}段正文内容。" for i in range(1, 9))
        proj = _mk_project(bddir, long_book)
        assert len(proj["chapters"][0]["paras"]) == 8
        bd.analyze_chapter(proj, proj["chapters"][0])
        assert "[5] 第5段正文内容。" in calls[0]["user"]
        assert "[6]" not in calls[0]["user"]


class TestRunPipeline:
    def test_run_updates_progress_and_status(self, bddir, monkeypatch):
        _patch_llm(monkeypatch, [ANALYSIS_OK] * 4)
        proj = _mk_project(bddir, BOOK)
        res = bd.run_breakdown(proj["id"])
        assert res["ok"] and res["pending"] == 4
        p = _wait_done(proj["id"])
        assert p["status"] == "done"
        assert p["progress"]["done"] == 4 and p["progress"]["failed"] == 0
        assert all(c["state"] == "done" for c in p["chapters"])
        assert p["characters"] and p["characters"][0]["name"] == "林然"

    def test_run_refuses_double_start(self, bddir, monkeypatch):
        _patch_llm(monkeypatch, [ANALYSIS_OK] * 4)
        proj = _mk_project(bddir, BOOK)
        assert bd.run_breakdown(proj["id"])["ok"]
        res = bd.run_breakdown(proj["id"])
        assert res["ok"] is False and "进行中" in res["note"]
        _wait_done(proj["id"])

    def test_failed_chapter_marks_partial(self, bddir, monkeypatch):
        _patch_llm(monkeypatch, [ValueError("x"), ValueError("y"),
                                 ANALYSIS_OK, ANALYSIS_OK, ANALYSIS_OK, ANALYSIS_OK])
        proj = _mk_project(bddir, BOOK)
        assert bd.run_breakdown(proj["id"])["ok"]
        p = _wait_done(proj["id"])
        assert p["status"] == "partial"
        assert p["chapters"][0]["state"] == "failed"

    def test_retry_failed_chapter(self, bddir, monkeypatch):
        _patch_llm(monkeypatch, [ValueError("x"), ValueError("y"),
                                 ANALYSIS_OK, ANALYSIS_OK, ANALYSIS_OK, ANALYSIS_OK])
        proj = _mk_project(bddir, BOOK)
        bd.run_breakdown(proj["id"])
        _wait_done(proj["id"])
        calls = _patch_llm(monkeypatch, [ANALYSIS_OK])
        res = bd.retry_chapter(proj["id"], 1)
        assert res["ok"] and res["chapter"]["state"] == "done"
        assert len(calls) == 1
        p = bd.load_project(proj["id"])
        assert p["status"] == "done" and p["chapters"][0]["state"] == "done"

    def test_retry_unknown_chapter(self, bddir):
        proj = _mk_project(bddir, BOOK)
        assert bd.retry_chapter(proj["id"], 99)["ok"] is False

    def test_run_missing_project(self, bddir):
        assert bd.run_breakdown("bd-20260101000000-abcdef")["ok"] is False

    def test_no_rerun_when_all_done(self, bddir, monkeypatch):
        _patch_llm(monkeypatch, [ANALYSIS_OK] * 4)
        proj = _mk_project(bddir, BOOK)
        bd.run_breakdown(proj["id"])
        _wait_done(proj["id"])
        calls = _patch_llm(monkeypatch, [])
        assert bd.run_breakdown(proj["id"])["ok"] is False
        assert calls == []


# ---------------------------------------------------------------- 全书报告与证据校验

REPORT_OK = {
    "editor_comment": "节奏扎实，钩子到位。",
    "outline_restore": "- 第一幕：入局\n- 第二幕：反杀",
    "character_arcs": [{"name": "林然", "arc": "从懦弱到担当"}],
    "foreshadow": [{"item": "断刀", "plant": "第1章", "payoff": "未回收"}],
    "pacing_curve": [{"no": 1, "score": 3}, {"no": 2, "score": 5}, {"no": 9, "score": 1}],
    "techniques": [
        {"name": "章末反转钩", "desc": "章末一句反转制造悬念", "evidence": "刀刃在月光下泛着冷光", "chapter": 3},
        {"name": "跨段证据", "desc": "证据跨两段", "evidence": "更夫的敲梆声停在了巷口。他数到三。", "chapter": 0},
        {"name": "假手法", "desc": "证据没命中", "evidence": "这句话并不在原文的任何角落出现", "chapter": 1},
        {"name": "短证据", "desc": "太短无法定位", "evidence": "冷光"},
        {"name": "缺描述", "desc": "", "evidence": "刀刃在月光下泛着冷光"},
    ],
}


def _analyze_all(proj):
    """逐章拆解并落盘（analyze_chapter 只改内存，报告测试需要磁盘状态）。"""
    for ch in proj["chapters"]:
        assert bd.analyze_chapter(proj, ch) is True
    bd.save_project(proj)


class TestReport:
    def _prepared(self, bddir, monkeypatch):
        _patch_llm(monkeypatch, [ANALYSIS_OK] * 4)
        proj = _mk_project(bddir, BOOK)
        _analyze_all(proj)
        return proj

    def test_evidence_verbatim_and_para_locate(self, bddir, monkeypatch):
        proj = self._prepared(bddir, monkeypatch)
        _patch_llm(monkeypatch, [REPORT_OK])
        res = bd.build_report(proj["id"])
        assert res["ok"], res
        techs = res["report"]["techniques"]
        assert [t["name"] for t in techs] == ["章末反转钩", "跨段证据"]
        # 证据逐字命中 + 段落区间定位（第三章第 2 段「他握紧了刀。刀刃在月光下泛着冷光。」）
        assert techs[0]["chapter"] == 3 and techs[0]["startPara"] == techs[0]["endPara"] == 2
        # 跨段证据：chapter=0 非法 → 全书扫描命中第四章（第2段+第3段）
        assert techs[1]["chapter"] == 4 and techs[1]["startPara"] == 2 and techs[1]["endPara"] == 3
        # 未命中 / 太短 / 缺描述 全部丢弃
        assert {d["name"] for d in res["dropped"]} == {"假手法", "短证据", "缺描述"}

    def test_report_stored_and_curve_coerced(self, bddir, monkeypatch):
        proj = self._prepared(bddir, monkeypatch)
        _patch_llm(monkeypatch, [REPORT_OK])
        res = bd.build_report(proj["id"])
        p = bd.load_project(proj["id"])
        assert p["report"]["editor_comment"].startswith("节奏扎实")
        # pacing_curve：越界 score clamp 到 1-5，非法章号丢弃
        assert res["report"]["pacing_curve"] == [{"no": 1, "score": 3}, {"no": 2, "score": 5}]

    def test_report_needs_analysis(self, bddir, monkeypatch):
        proj = _mk_project(bddir, BOOK)
        _patch_llm(monkeypatch, [REPORT_OK])
        res = bd.build_report(proj["id"])
        assert res["ok"] is False and "已拆解" in res["note"]

    def test_report_retry_once(self, bddir, monkeypatch):
        proj = self._prepared(bddir, monkeypatch)
        calls = _patch_llm(monkeypatch, [ValueError("bad"), REPORT_OK])
        res = bd.build_report(proj["id"])
        assert res["ok"] and len(calls) == 2

    def test_report_double_failure(self, bddir, monkeypatch):
        proj = self._prepared(bddir, monkeypatch)
        _patch_llm(monkeypatch, [ValueError("bad"), ValueError("bad2")])
        res = bd.build_report(proj["id"])
        assert res["ok"] is False and "重试一次" in res["note"]
        p = bd.load_project(proj["id"])
        assert p["report"] is None

    def test_empty_report_counts_as_failure(self, bddir, monkeypatch):
        proj = self._prepared(bddir, monkeypatch)
        _patch_llm(monkeypatch, [{}, {}])
        assert bd.build_report(proj["id"])["ok"] is False

    def test_characters_aggregated(self, bddir, monkeypatch):
        proj = self._prepared(bddir, monkeypatch)
        chars = bd.aggregate_characters(bd.load_project(proj["id"]))
        assert chars and chars[0]["name"] == "林然" and chars[0]["count"] >= 4


# ---------------------------------------------------------------- 手法入账本（复用 lexicon_ledger）

class TestAdopt:
    def _prepared_with_report(self, bddir, monkeypatch):
        _patch_llm(monkeypatch, [ANALYSIS_OK] * 4)
        proj = _mk_project(bddir, BOOK)
        _analyze_all(proj)
        _patch_llm(monkeypatch, [REPORT_OK])
        assert bd.build_report(proj["id"])["ok"]
        return bd.load_project(proj["id"])

    def test_adopt_writes_ledger_without_evidence(self, bddir, monkeypatch):
        proj = self._prepared_with_report(bddir, monkeypatch)
        res = bd.adopt_techniques(proj["id"], [0])
        assert res["ok"] and res["adopted"] == 1 and res["dropped"] == []
        # 手法卡：只带手法描述 + 证据位置，不带原文片段
        card = bd.load_project(proj["id"])["adopted_techniques"][0]
        assert card["name"] == "章末反转钩"
        assert "刀刃" not in json.dumps(card, ensure_ascii=False)      # 纯净域：无原文片段
        assert card["where"].startswith("第3章 段")
        # lexicon_ledger 数据结构复用：{gram: {count, chapters, last}}
        ledger_file = bddir / proj["id"] / lexicon_ledger.LEDGER_NAME
        assert ledger_file.exists()
        data = json.loads(ledger_file.read_text(encoding="utf-8"))
        assert data and all(
            set(v.keys()) >= {"count", "chapters", "last"} for v in data.values())

    def test_adopt_rejects_evidence_in_desc(self, bddir, monkeypatch):
        """手法描述携带原文片段 → 按纯净域拒绝。"""
        proj = self._prepared_with_report(bddir, monkeypatch)
        p = bd.load_project(proj["id"])
        p["report"]["techniques"].append({
            "name": "带原文的假手法", "desc": "章末一句反转：刀刃在月光下泛着冷光",
            "evidence": "刀刃在月光下泛着冷光", "chapter": 3})
        bd.save_project(p)
        res = bd.adopt_techniques(proj["id"], [2])   # 追加的假手法在 techniques[2]
        assert res["adopted"] == 0
        assert any("原文片段" in d["reason"] for d in res["dropped"])

    def test_adopt_drops_evidence_miss(self, bddir, monkeypatch):
        proj = self._prepared_with_report(bddir, monkeypatch)
        p = bd.load_project(proj["id"])
        p["report"]["techniques"][0]["evidence"] = "这句证据是编造的并不存在于原文"
        bd.save_project(p)
        res = bd.adopt_techniques(proj["id"], [0])
        assert res["adopted"] == 0 and res["dropped"][0]["reason"].startswith("证据")

    def test_adopt_dedup(self, bddir, monkeypatch):
        proj = self._prepared_with_report(bddir, monkeypatch)
        assert bd.adopt_techniques(proj["id"], [0, 0])["adopted"] == 1
        res = bd.adopt_techniques(proj["id"], [0])
        assert res["adopted"] == 0 and any("已入账" in d["reason"] for d in res["dropped"])

    def test_adopt_into_real_book(self, bddir, tmp_path, monkeypatch):
        """projectKey 指向真实作品目录 → 入作品账本（与现有体系闭环）。"""
        book_dir = tmp_path / "projects" / "novel_demo"
        book_dir.mkdir(parents=True)
        monkeypatch.setattr(bd, "_book_project_dir", lambda key: str(book_dir) if key == "novel_demo" else None)
        proj = self._prepared_with_report(bddir, monkeypatch)
        res = bd.adopt_techniques(proj["id"], [0], "novel_demo")
        assert res["adopted"] == 1
        assert (book_dir / lexicon_ledger.LEDGER_NAME).exists()

    def test_adopt_invalid_index(self, bddir, monkeypatch):
        proj = self._prepared_with_report(bddir, monkeypatch)
        res = bd.adopt_techniques(proj["id"], [999, "x", None])
        assert res["ok"] and res["adopted"] == 0


# ---------------------------------------------------------------- 项目存储与轮转

class TestStorage:
    def test_create_and_load_roundtrip(self, bddir):
        proj = _mk_project(bddir, BOOK, "回环书")
        p = bd.load_project(proj["id"])
        assert p["title"] == "回环书"
        assert p["chapters"][0]["paras"][0].startswith("林然")

    def test_list_and_delete(self, bddir):
        a = _mk_project(bddir, BOOK, "甲")
        b = _mk_project(bddir, BOOK, "乙")
        items = bd.list_projects()
        assert {x["title"] for x in items} == {"甲", "乙"}
        assert bd.delete_project(a["id"])["ok"]
        assert bd.load_project(a["id"]) is None
        assert {x["title"] for x in bd.list_projects()} == {"乙"}

    def test_delete_running_refused(self, bddir, monkeypatch):
        _patch_llm(monkeypatch, [ANALYSIS_OK] * 4)
        proj = _mk_project(bddir, BOOK)
        bd.run_breakdown(proj["id"])
        res = bd.delete_project(proj["id"])
        assert res["ok"] is False and "进行中" in res["note"]
        _wait_done(proj["id"])
        assert bd.delete_project(proj["id"])["ok"]

    def test_rotation_keeps_cap(self, bddir, monkeypatch):
        """项目存储轮转：超出上限删最旧。"""
        monkeypatch.setattr(bd, "MAX_PROJECTS", 5)
        # 时间戳推进，保证 updated_at 可分先后（同一秒内创建时排序不稳定）
        counter = [0]

        def _tick():
            counter[0] += 1
            return f"2026-01-01T00:00:{counter[0]:02d}"

        monkeypatch.setattr(bd, "_now", _tick)
        ids = []
        for i in range(7):
            p = _mk_project(bddir, BOOK, f"书{i}")
            ids.append(p["id"])
        items = bd.list_projects()
        assert len(items) == 5
        got_ids = {x["id"] for x in items}
        assert not (set(ids[:2]) & got_ids)      # 最旧的两个被轮转掉
        assert set(ids[2:]) <= got_ids

    def test_resplit_resets(self, bddir, monkeypatch):
        _patch_llm(monkeypatch, [ANALYSIS_OK] * 4)
        proj = _mk_project(bddir, BOOK)
        bd.run_breakdown(proj["id"])
        p = _wait_done(proj["id"])
        assert p["chapters"][0]["state"] == "done"
        assert bd.resplit_project(proj["id"])["ok"]
        p2 = bd.load_project(proj["id"])
        assert all(c["state"] == "pending" for c in p2["chapters"])
        assert p2["report"] is None and p2["status"] == "draft"

    def test_resplit_running_refused(self, bddir, monkeypatch):
        _patch_llm(monkeypatch, [ANALYSIS_OK] * 4)
        proj = _mk_project(bddir, BOOK)
        bd.run_breakdown(proj["id"])
        assert bd.resplit_project(proj["id"])["ok"] is False
        _wait_done(proj["id"])

    def test_bad_id_rejected(self, bddir):
        assert bd.load_project("../../etc/passwd") is None
        with pytest.raises(ValueError):
            bd._bd_path("x-y-z")


# ---------------------------------------------------------------- HTTP 路由（独立 FastAPI 应用）

@pytest.fixture()
def client(bddir, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from bot import webbreakdown

    app = FastAPI()
    app.include_router(webbreakdown.router)
    return TestClient(app)


def _analyze_stub_factory():
    def _stub(messages, **kw):
        return ANALYSIS_OK
    return _stub


class TestHttp:
    def test_paste_run_progress_get_delete(self, client, monkeypatch):
        monkeypatch.setattr(bd.llm, "chat_json", _analyze_stub_factory())
        r = client.post("/api/breakdown/paste", json={"title": "粘贴书", "text": BOOK})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] and d["chapterCount"] == 4
        assert len(d["preview"]) == 4 and d["preview"][0]["title"] == "第一章 入局"
        bid = d["id"]

        # run（后台线程）
        r = client.post(f"/api/breakdown/{bid}/run")
        assert r.status_code == 200 and r.json()["ok"]
        # progress 轮询直到完成
        deadline = time.time() + 10
        status = None
        while time.time() < deadline:
            pr = client.get(f"/api/breakdown/{bid}/progress").json()
            if not pr.get("running") and pr.get("status") in ("done", "partial"):
                status = pr["status"]
                break
            time.sleep(0.05)
        assert status == "done"

        # 全量
        full = client.get(f"/api/breakdown/{bid}").json()["project"]
        assert full["chapters"][0]["analysis"]["brief"]
        assert full["progress"]["done"] == 4

        # 列表
        lst = client.get("/api/breakdowns").json()
        assert any(x["id"] == bid for x in lst["projects"])

        # 删除
        assert client.delete(f"/api/breakdown/{bid}").json()["ok"]
        assert client.get(f"/api/breakdown/{bid}").status_code == 404

    def test_paste_too_short(self, client):
        r = client.post("/api/breakdown/paste", json={"title": "t", "text": "太短了"})
        assert r.status_code == 400 and "太短" in r.json()["error"]

    def test_report_endpoint(self, client, monkeypatch):
        # 报告调用的 system 提示含「全书拆书报告」，章节拆解调用含「拆书」结构分析——按内容区分
        monkeypatch.setattr(bd.llm, "chat_json", lambda m, **kw: (
            REPORT_OK if "全书拆书报告" in m[0]["content"] else ANALYSIS_OK))
        bid = client.post("/api/breakdown/paste", json={"title": "t", "text": BOOK}).json()["id"]
        client.post(f"/api/breakdown/{bid}/run")
        deadline = time.time() + 10
        while time.time() < deadline:
            pr = client.get(f"/api/breakdown/{bid}/progress").json()
            if not pr.get("running"):
                break
            time.sleep(0.05)
        r = client.post(f"/api/breakdown/{bid}/report")
        assert r.status_code == 200 and r.json()["ok"]
        assert client.post(f"/api/breakdown/{bid}/report").status_code in (200, 409)

    def test_adopt_endpoint(self, client, monkeypatch):
        def _stub(messages, **kw):
            return REPORT_OK if "全书拆书报告" in messages[0]["content"] else ANALYSIS_OK
        monkeypatch.setattr(bd.llm, "chat_json", _stub)
        bid = client.post("/api/breakdown/paste", json={"title": "t", "text": BOOK}).json()["id"]
        client.post(f"/api/breakdown/{bid}/run")
        deadline = time.time() + 10
        while time.time() < deadline:
            if not client.get(f"/api/breakdown/{bid}/progress").json().get("running"):
                break
            time.sleep(0.05)
        assert client.post(f"/api/breakdown/{bid}/report").status_code == 200
        r = client.post(f"/api/breakdown/{bid}/adopt-techniques", json={"indices": [0]})
        assert r.status_code == 200 and r.json()["adopted"] == 1
        # 空勾选
        assert client.post(f"/api/breakdown/{bid}/adopt-techniques",
                           json={"indices": "bad"}).status_code == 400

    def test_resplit_endpoint(self, client, monkeypatch):
        monkeypatch.setattr(bd.llm, "chat_json", _analyze_stub_factory())
        bid = client.post("/api/breakdown/paste", json={"title": "t", "text": BOOK}).json()["id"]
        client.post(f"/api/breakdown/{bid}/run")
        deadline = time.time() + 10
        while time.time() < deadline:
            if not client.get(f"/api/breakdown/{bid}/progress").json().get("running"):
                break
            time.sleep(0.05)
        r = client.post(f"/api/breakdown/{bid}/resplit")
        assert r.status_code == 200 and r.json()["ok"]
        assert client.get(f"/api/breakdown/{bid}").json()["project"]["status"] == "draft"

    def test_upload_rejects_bad_ext(self, client):
        r = client.post("/api/breakdown/upload",
                        files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
        assert r.status_code == 400 and "不支持" in r.json()["error"]

    def test_upload_txt(self, client, monkeypatch):
        monkeypatch.setattr(bd.llm, "chat_json", _analyze_stub_factory())
        r = client.post(
            "/api/breakdown/upload",
            files={"file": ("三国演义.txt", BOOK.encode("utf-8"), "text/plain")},
            data={"title": "上传书"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] and d["chapterCount"] == 4 and d["title"] == "上传书"
        assert d["preview"][0]["chars"] > 0

    def test_run_404(self, client):
        assert client.post("/api/breakdown/bd-20260101000000-abcdef/run").status_code == 404

    def test_get_bad_id(self, client):
        assert client.get("/api/breakdown/not-an-id").status_code == 400

    def test_double_run_conflict(self, client, monkeypatch):
        monkeypatch.setattr(bd.llm, "chat_json", _analyze_stub_factory())
        bid = client.post("/api/breakdown/paste", json={"title": "t", "text": BOOK}).json()["id"]
        assert client.post(f"/api/breakdown/{bid}/run").status_code == 200
        assert client.post(f"/api/breakdown/{bid}/run").status_code == 409
        deadline = time.time() + 10
        while time.time() < deadline:
            if not client.get(f"/api/breakdown/{bid}/progress").json().get("running"):
                break
            time.sleep(0.05)
