"""v4.33.9 全书账本与语义门禁回归。

覆盖：
1. 数值口径冲突（N13）：口径打架（4/5 vs 27/100000）、进度回退（5/5→4/5）、百分比回退；
2. 世界观违禁物（N15）：项目红线词表进落盘三查；
3. 剧本格式补全：S13 括号体系混用 / S14 场号重复 / S15 动作句写进台词；
4. 全书口癖账本：记账 → 热度榜 → 禁用注入块 → 停用词不误报；
5. 复述前情检测：_prev_body_overlap 抓正文中部整段复述；
6. 阶段体检：foreshadow_health / unregistered_candidates；
7. 保真清单：verify_fidelity 缺项判定；
8. lorebook 别名列 → N12 实体漂移检测；
9. 评分语义哨兵：chapter_review 返回 conflicts。
"""
import json


from bot import checkup, delivery_gate as gate, delivery_spec as spec
from bot import lexicon_ledger as lex
from bot import lorebook, writer


# ------------------------------------------------------- N13 数值口径

def _ch(n, text):
    return {"n": n, "title": f"第{n}章", "text": text}


def test_numeric_denominator_conflict():
    rows = [_ch(1, "系统提示：收服进度 4/5。"),
            _ch(2, "面板显示：收服进度 27/100000。")]
    codes = [f["code"] for f in gate.numeric_findings(rows)]
    assert "N13" in codes


def test_numeric_progress_regression():
    rows = [_ch(1, "收服进度 5/5。"),
            _ch(2, "收服进度 4/5。")]
    findings = gate.numeric_findings(rows)
    assert any("回退" in f["detail"] for f in findings)


def test_numeric_percent_regression():
    rows = [_ch(1, "契约进度 90%。"),
            _ch(2, "契约进度 70%。")]
    assert gate.numeric_findings(rows)


def test_numeric_normal_progress_passes():
    rows = [_ch(1, "收服进度 4/5。"),
            _ch(2, "收服进度 5/5。"),
            _ch(3, "他买了 3/4 斤猪肉，进度没提。")]
    assert gate.numeric_findings(rows) == []


def test_n13_wired_into_check_novel():
    rep = gate.check_novel([_ch(1, "收服进度 4/5。"), _ch(2, "收服进度 27/100000。")])
    assert any(f["code"] == "N13" for f in rep["findings"])


# ------------------------------------------------------- N15 世界观违禁物

def test_n15_banned_terms_in_purity_blocks():
    hits = gate.chapter_purity_blocks("他掏出打火机点了火。", banned_terms=["打火机"])
    assert hits and hits[0]["code"] == "N15"


def test_n15_clean_without_banned():
    assert gate.chapter_purity_blocks("他钻木取火。", banned_terms=["打火机"]) == []


def test_writer_world_banned_terms_helper():
    plan = {"config": {"world_rules": {"era": "ancient", "banned_terms": ["打火机", " 无人机 ", ""]}}}
    assert writer._world_banned_terms(plan) == ["打火机", "无人机"]
    assert writer._world_banned_terms({"config": {}}) == []


# ------------------------------------------------------- 剧本格式补全

def test_s13_bracket_system_mixing():
    eps = [{"n": 1, "text": "**场1：内 屋 夜**\n（林野推门。）\n林野：谁。\n\n【烛火骤暗】\n长老：坐。"},
           {"n": 2, "text": "**场1：内 屋 夜**\n（灯灭。）\n林野：走。"}]
    rep = gate.check_script(eps)
    assert any(f["code"] == "S13" for f in rep["findings"])


def test_s14_duplicate_scene_number():
    text = "**场1：内 屋 夜**\n林野：谁。\n\n**场1：外 山道 晨**\n林野：走。\n\n**场2：内 殿 夜**\n长老：坐。"
    findings = gate.check_episode(text, ep_no=1)
    assert any(f["code"] == "S14" for f in findings)


def test_s15_action_line_in_dialogue():
    text = "**场1：内 屋 夜**\n林野：后退一步。\n长老：站住。\n**场2：外 桥 晨**\n苏红绫：愣了一下。\n林野：走。"
    findings = gate.check_episode(text, ep_no=1)
    assert any(f["code"] == "S15" for f in findings)


def test_s15_real_dialogue_not_flagged():
    text = "**场1：内 屋 夜**\n林野：你退后，我来。\n**场2：外 桥 晨**\n苏红绫：好。"
    assert not any(f["code"] == "S15" for f in gate.check_episode(text, ep_no=1))


# ------------------------------------------------------- 口癖账本

def test_lexicon_record_and_hot(tmp_path):
    for i in range(1, 11):
        lex.record(str(tmp_path), i, f"林野握紧木棍，指节泛白。第{i}章。")
    hot = lex.hot_items(str(tmp_path), units_done=10, min_units=8, ratio=0.5)
    grams = {h["gram"] for h in hot}
    assert "握紧" in grams and "泛白" in grams


def test_lexicon_stopgrams_not_hot(tmp_path):
    for i in range(1, 11):
        lex.record(str(tmp_path), i, f"他们自己看着，什么都没有。第{i}章。")
    grams = {h["gram"] for h in lex.hot_items(str(tmp_path), units_done=10, min_units=8, ratio=0.5)}
    assert "他们" not in grams and "自己" not in grams


def test_lexicon_banned_block(tmp_path):
    for i in range(1, 9):
        lex.record(str(tmp_path), i, "兽核在掌心发烫。")
    block = lex.banned_block(str(tmp_path), units_done=10, min_units=6, ratio=0.5)
    assert "口癖账本" in block and "兽核" in block


# ------------------------------------------------------- 复述前情

def test_prev_body_overlap_catches_midbody_retell():
    prev = "开头不一样。" + ("他们翻过矮坡，看到三个苏家护卫正围着青铜色的身影，护卫手持麻醉箭。" * 10)
    body = "本章全新开局。" + ("林野握紧木棍。他们翻过矮坡，看到三个苏家护卫正围着青铜色的身影，护卫手持麻醉箭。" * 8)
    assert writer._prev_body_overlap(body, prev) > 0.55


def test_prev_body_overlap_clean_chapter_passes():
    prev = "上一章讲他在森林里猎狼。" * 30
    body = "这一章讲她夜里缝补斗篷，第二天去了集市，遇到卖花的老人，买下一束栀子。" * 8
    assert writer._prev_body_overlap(body, prev) < 0.3


# ------------------------------------------------------- 阶段体检

def test_foreshadow_health(tmp_path):
    (tmp_path / "19-伏笔账本.json").write_text(json.dumps({
        "version": 1, "items": {
            "胸口发热": {"description": "", "setup_chapter": 1, "due_to": 5,
                        "status": "open", "notes": "系统激活前兆"},
            "预言": {"description": "", "setup_chapter": 1, "due_to": 0,
                     "status": "open", "notes": ""},
            "铜币": {"description": "已回收", "status": "paid"},
        }}, ensure_ascii=False), encoding="utf-8")
    h = checkup.foreshadow_health(str(tmp_path), current_no=20)
    assert h["open"] == 2 and h["empty_desc"] == 1
    assert any("胸口发热" in x for x in h["overdue"])


def test_unregistered_candidates():
    texts = ["青铜角犀撞碎了栅栏。青铜角犀低吼。"] * 8
    known = {"林野", "苍莽大陆"}
    cands = checkup.unregistered_candidates(texts, known, min_count=6)
    assert any(c["gram"] == "青铜角犀" for c in cands)


def test_stage_checkup_writes_report(tmp_path):
    class _P:
        path = str(tmp_path)
        def log(self, msg):
            self.msg = msg
    proj = _P()
    rows = [{"n": i, "title": f"第{i}章", "text": f"第{i}章正文。林野握紧木棍。"} for i in range(1, 11)]
    rep = checkup.stage_checkup(proj, rows, kind="novel")
    assert (tmp_path / "24-阶段体检.json").exists()
    assert rep["at_unit"] == 10


# ------------------------------------------------------- 保真清单

def test_verify_fidelity_missing(tmp_path):
    body = tmp_path / "04-正文"
    body.mkdir()
    (body / "第001场.md").write_text("林野在广场被退婚。", encoding="utf-8")
    plan = {"adaptation": {"fidelity": [
        {"item": "退婚", "keyword": "退婚", "kind": "承诺"},
        {"item": "万兽来朝", "keyword": "万兽来朝", "kind": "设定"},
    ]}}
    fr = __import__("bot.drama_adapter", fromlist=["verify_fidelity"]).verify_fidelity(str(tmp_path), plan)
    assert fr["total"] == 2 and fr["missing"] == ["万兽来朝"]
    assert (tmp_path / "25-改编保真清单.md").exists()


# ------------------------------------------------------- lorebook 别名 + N12

def test_lorebook_alias_column(tmp_path):
    (tmp_path / "09-故事圣经.md").write_text(
        "# 设定词典\n\n| 名称 | 定义 | 已知 | 真相 | 揭示计划 | 别名 |\n|---|---|---|---|---|---|\n"
        "| 青铜角犀 | 凡兽 | 无 | - | 待定 | 铁甲、纹角犀 |\n", encoding="utf-8")
    al = lorebook.entity_aliases(str(tmp_path))
    assert al == {"青铜角犀": ["铁甲", "纹角犀"]}


def test_n12_detects_alias_drift():
    rows = [_ch(1, "铁甲低吼一声，撞碎了栅栏。")]
    rep = gate.check_novel(rows, entity_aliases={"青铜角犀": ["铁甲"]})
    assert any(f["code"] == "N12" for f in rep["findings"])


# ------------------------------------------------------- 评分语义哨兵

def test_chapter_review_returns_conflicts(monkeypatch):
    from bot import workflow, llm
    monkeypatch.setattr(workflow, "_agent_disabled", lambda name: False)
    monkeypatch.setattr(workflow, "agent_skills_block", lambda *a, **k: "")
    monkeypatch.setattr(workflow, "_evt", lambda *a, **k: None)
    captured = {}

    def fake_chat_json(msgs, **kw):
        captured["prompt"] = msgs[0]["content"]
        return {"scores": {"结构": 8, "因果可信": 8, "人物": 8, "连续性": 8, "语言": 8, "读者体验": 8},
                "weak": ["语言"], "evidence": [], "verdict": "达标",
                "conflicts": ["收服进度与第10章矛盾"]}
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    r = workflow.chapter_review("第十一章正文……", {})
    assert r["conflicts"] == ["收服进度与第10章矛盾"]
    assert "conflicts" in captured["prompt"]


# ------------------------------------------------------- 规格完整性

def test_spec_v11_new_codes_registered():
    assert spec.SPEC_VERSION == "1.1"
    for code in ("N13", "N15", "S13", "S14", "S15"):
        assert code in spec.BY_CODE
    assert "N15" in spec.STAGES["draft"]
    assert "N13" in spec.STAGES["final"]
