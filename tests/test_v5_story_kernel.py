"""v5.0 质量内核回归：事实源、章卡契约、写前/写后闸门、结构体检。"""
import json
import os
import threading

from bot import story_kernel as v5


class _P:
    def __init__(self, path, plan):
        self.path = path
        self.name = plan.get("novelName", "书")
        self.lock = threading.RLock()
        self._plan = plan

    def plan(self):
        return self._plan

    def save_plan(self, plan):
        self._plan = plan
        with open(os.path.join(self.path, "02-写作计划.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False)

    def log(self, msg):
        pass


def _setup(tmp_path):
    pdir = tmp_path / "book"
    pdir.mkdir()
    chars = """# 人物档案

## 人物：沈砚
- 身份/资源：远星资本实际控制人，表面是普通顾问
- 性格内核：克制，越被轻视越冷静
- 想要 Want：查清父亲旧案
- 欠缺 Need：学会让别人参与
- 致命缺陷：习惯隐瞒底牌
- 说话方式：短句，先用事实堵人；生气时反而更客气

## 人物：苏晚晴
- 身份/资源：苏氏集团执行副总裁
- 性格内核：外冷内硬，先算责任再谈感情
- 想要 Want：守住苏氏经营权
- 欠缺 Need：学会相信一个人
- 致命缺陷：害怕感情影响判断
- 说话方式：正式、克制，先问证据；被逼急时会短句连问
"""
    (pdir / "00-人物档案.md").write_text(chars, encoding="utf-8")
    plan = {"novelName": "测试书", "totalChapters": 20, "minWordsPerChapter": 3000,
            "config": {"v5_kernel": True, "genre": "都市情感"},
            "chapters": [{"chapterNumber": i, "title": f"第{i}章", "filePath": f"第{i:02d}章.md",
                          "status": "pending"} for i in range(1, 21)]}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    p = _P(str(pdir), plan)
    v5.ensure_project(p, plan["config"], name="测试书", characters_text=chars)
    return p


def _card(n=1, scene_type="谈判"):
    return {
        "n": n, "title": f"第{n}章 测试",
        "goal": "沈砚在谈判桌上逼顾承泽交出原始授权。",
        "conflict": "顾承泽用融资续期逼苏晚晴签个人担保。",
        "turn": "苏晚晴拒签，沈砚拿出银行回执。",
        "payoff": "收回上一章的假授权线索。",
        "cliffhanger": "顾承泽说，真正签字的人不是他。",
        "cast": ["沈砚", "苏晚晴", "顾承泽"],
        "locations": ["苏氏会议室"],
        "scene_type": scene_type, "story_time": "第3日 上午",
        "protagonist_action": "沈砚主动调取银行回执，逼顾承泽当场对时间。",
        "relationship_delta": "苏晚晴第一次在董事面前公开站到沈砚身边。",
        "threads_advance": [{"thread": "主线", "change": "假授权被拆穿", "irreversible": True}],
        "scenes": [
            {"want": "拿到原始授权", "obstacle": "顾承泽拒绝", "choice": "当众调回执",
             "cost": "暴露远星关系", "outcome": "顾承泽改口", "value_shift": "攻守逆转"},
            {"want": "逼苏晚晴签字", "obstacle": "苏晚晴拒签", "choice": "加码监管",
             "cost": "失去董事信任", "outcome": "方案搁置", "value_shift": "施压失败"},
            {"want": "稳住融资", "obstacle": "时间不够", "choice": "接受双重审核",
             "cost": "让出一部分权限", "outcome": "融资暂缓", "value_shift": "以退为进"},
        ],
        "word_target": 3000,
    }


def test_ensure_and_fact_lint(tmp_path):
    p = _setup(tmp_path)
    facts = v5.load_facts(p)
    assert facts["title"] == "测试书"
    assert facts["protagonist"] == "沈砚"
    assert facts["deuteragonist"] == "苏晚晴"
    assert len(facts["core_cast"]) >= 2
    assert len(facts["ending_contract"]) >= 5
    blocks = [x for x in v5.fact_lint(p) if x["level"] == "block"]
    assert not blocks, blocks


def test_fact_lint_catches_other_book_bible(tmp_path):
    p = _setup(tmp_path)
    with open(os.path.join(p.path, "09-故事圣经.md"), "w", encoding="utf-8") as f:
        f.write("# 退婚当天，我的马甲曝光了\n\n## 一、故事内核\n")
    codes = {x["code"] for x in v5.fact_lint(p) if x["level"] == "block"}
    assert "V5F02" in codes


def test_card_lint_blocks_empty_card(tmp_path):
    p = _setup(tmp_path)
    blocks = [x for x in v5.card_lint(p, {}, 1, {}) if x["level"] == "block"]
    codes = {x["code"] for x in blocks}
    assert {"V5C03", "V5C08", "V5C11", "V5C13", "V5C14", "V5C15"} & codes


def test_card_lint_accepts_full_card(tmp_path):
    p = _setup(tmp_path)
    blocks = [x for x in v5.card_lint(p, _card(1), 1, {}) if x["level"] == "block"]
    assert not blocks, blocks


def test_card_lint_blocks_three_same_scene_types(tmp_path):
    p = _setup(tmp_path)
    cards = {1: _card(1, "核验"), 2: _card(2, "核验")}
    blocks = [x for x in v5.card_lint(p, _card(3, "核验"), 3, cards) if x["level"] == "block"]
    assert any(x["code"] == "V5C21" for x in blocks)


def test_post_gate_blocks_protagonist_absence(tmp_path):
    p = _setup(tmp_path)
    text = "苏晚晴走进会议室。" * 400
    blocks = [x for x in v5.post_gate(p, 1, text, _card(1))["blocks"] if x["code"] == "V5Q02"]
    assert blocks


def test_post_gate_accepts_present_protagonist(tmp_path):
    p = _setup(tmp_path)
    text = ("沈砚走进会议室，把银行回执放在桌上。" * 200)
    g = v5.post_gate(p, 1, text, _card(1))
    assert not [x for x in g["blocks"] if x["code"] in ("V5Q01", "V5Q02", "V5Q03")], g["blocks"]


def test_structure_check_flags_scene_loop(tmp_path):
    p = _setup(tmp_path)
    cards = {n: _card(n, "核验") for n in range(1, 4)}
    with open(os.path.join(p.path, "02d-章卡.jsonl"), "w", encoding="utf-8") as f:
        for c in cards.values():
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    for n in range(1, 4):
        with open(os.path.join(p.path, f"第{n:02d}章.md"), "w", encoding="utf-8") as f:
            f.write("沈砚走进会议室。" * 300)
    st = v5.structure_check(p, upto=3, window=3)
    codes = {x["code"] for x in st["issues"]}
    assert "V5S01" in codes
    assert st["should_replan"] is True


def test_preflight_accepts_full_card_and_context(tmp_path):
    p = _setup(tmp_path)
    c = _card(1)
    with open(os.path.join(p.path, "02d-章卡.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(c, ensure_ascii=False) + "\n")
    assert v5.preflight(p, c, 1)["ok"]
    from bot import context
    ctx = context.build(p, {"chapterNumber": 1, "title": "第1章"}, {"context_budget": 40000})
    assert "v5_kernel" in ctx
    assert "作品事实源" in ctx["v5_kernel"]


def test_ending_evidence_needs_three_hits(tmp_path):
    p = _setup(tmp_path)
    chapters = [{"n": n, "text": "沈砚和苏晚晴走进会议室，顾承泽在等。"} for n in range(1, 10)]
    chapters.append({"n": 10, "text": "沈砚当众揭开旧案真相，顾承泽失去授权，苏晚晴选择和他在一起。"})
    end = v5.ending_evidence(p, chapters)
    assert end["ok"] is True
    assert end["hit"] >= 3
