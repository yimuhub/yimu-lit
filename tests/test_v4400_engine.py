"""v4.40.0：正典体检 / 场景级章卡 / 引擎对账 / 文书密度 / 旧书整改。

这些测试锁的不是文笔，而是**事实链**：
  书名承诺、登记人物、对手、章卡、正文，必须是同一本书。
"""
import json
import os
import threading

from bot import canon, craft_check, engine_check, longform, retrofit, state, taste


class _Proj:
    def __init__(self, path, plan):
        self.path = path
        self._plan = plan
        self.lock = threading.RLock()
        self.logs = []

    def plan(self):
        return self._plan

    def save_plan(self, plan):
        self._plan = plan

    def log(self, msg, *_a, **_k):
        self.logs.append(msg)


def _write(path, name, text):
    with open(os.path.join(path, name), "w", encoding="utf-8") as f:
        f.write(text)


def _project(tmp_path, chapters: dict, cards: list | None = None, bible: str = "",
             chars: str = "") -> _Proj:
    pdir = str(tmp_path)
    rows = []
    for n, text in chapters.items():
        fp = f"{n:02d}.md"
        _write(pdir, fp, f"# 第{n}章\n\n## 正文\n{text}")
        rows.append({"chapterNumber": n, "filePath": fp,
                     "status": "completed", "wordCount": None})
    plan = {"novelName": "民国神棍：我在桥头炸出工业革命",
            "minWordsPerChapter": 3000, "chapters": rows, "config": {}}
    if cards:
        _write(pdir, "02d-章卡.jsonl", "\n".join(json.dumps(x, ensure_ascii=False) for x in cards))
    if bible:
        _write(pdir, "09-故事圣经.md", bible)
    if chars:
        _write(pdir, "00-人物档案.md", chars)
    return _Proj(pdir, plan)


# ---------------- 正典体检 ----------------

def test_title_motifs_extract_theme_nouns():
    m = canon.title_motifs("民国神棍：我在桥头炸出工业革命")
    assert "神棍" in m and "桥头" in m and "工业革命" in m


def test_canon_flags_split_fact_sources(tmp_path):
    proj = _project(
        tmp_path,
        {1: "陆沉走进桥头，掏出罗盘看了看。" * 3,
         2: "陆沉在桥头遇见巡警。",
         3: "陆沉回到桥头。"},
        cards=[{"n": 1, "goal": "查账", "conflict": "时代压力", "cast": ["陆沉"]},
               {"n": 2, "goal": "", "conflict": "", "cast": ["陆沉"]}],
        bible=(
            "# 故事圣经\n\n## 人物\n- 沈默：主角\n- 陆青藤：女主\n\n"
            "## 对手行动表\n| 对手 | 要什么 | 施压手段 | 首次出手 |\n|---|---|---|---|\n"
            "| 沈默 | 夺权 | 逼债 | 第2章 |\n"),
        chars="# 人物档案\n\n## 主要人物\n- 赵云舒：账房\n- 陈铁生：巡警\n",
    )
    data = canon.scan(proj)
    assert data["title_decision"] == "rename_or_strengthen"
    motifs = {x["motif"]: x for x in data["title_motifs"]}
    assert motifs["神棍"]["mentions"] == 0
    assert motifs["工业革命"]["mentions"] == 0
    # 圣经/档案里那套名字在正文从未出现
    assert {r["name"] for r in data["missing_registry"]} >= {"沈默", "陆青藤", "赵云舒", "陈铁生"}
    # 正文高频的陆沉反而没登记
    assert any(x["name"] == "陆沉" for x in data["unregistered_names"])
    # 章卡有冲突为空
    codes = {x["code"] for x in data["issues"]}
    assert "K01" in codes and "K04" in codes and "K06" in codes


def test_seed_state_promotes_title_and_skips_ghost_opponents(tmp_path):
    bible = (
        "# 故事圣经\n\n## 书名\n民国神棍：我在桥头炸出工业革命\n\n"
        "## 标题承诺表\n| 关键词 | 兑现事件 | 最早 | 最晚 |\n|---|---|---|---|\n"
        "| 神棍 | 用风水局撑起第一笔生意 | 第2章 | 第6章 |\n\n"
        "## 对手行动表\n| 对手 | 目标 | 施压手段 | 首次出手 |\n|---|---|---|---|\n"
        "| 王虎 | 抢码头 | 派打手砸摊 | 第3章 |\n"
        "| 幽灵客 | 夺秘本 | 夜里潜入 | 第9章 |\n\n"
        "## 人性命题\n| 命题 | 选择 | 代价 |\n|---|---|---|\n"
        "| 救人还是保生意 | 先救人 | 丢掉一笔订单 |\n"
    )
    proj = _project(tmp_path, {1: "陆沉在桥头摆摊，王虎的人来砸摊。"}, bible=bible)
    seeded = canon.seed_state(proj)
    assert seeded["promises"] >= 2      # 书名母题 + 表里「神棍」
    assert seeded["opponents"] == 1     # 幽灵客没在正文出现，不许进状态
    st = state.load(str(tmp_path))
    assert "王虎" in st["opponents"]
    assert "幽灵客" not in st["opponents"]
    assert any(p.get("kind") == "title" for p in st["promises"].values())
    # 重复执行不会重复注入
    again = canon.seed_state(proj)
    assert again["promises"] == 0 and again["opponents"] == 0


# ---------------- 状态库：承诺 / 对手 / 生死 ----------------

def test_state_delta_merges_promises_and_opponents():
    st = state.empty_state()
    st = state.merge_state_delta(st, {
        "promise_ops": [{"action": "setup", "text": "神棍名号必须靠一场局兑现", "kind": "title"}],
        "opponent_ops": [{"action": "add", "name": "王虎", "goal": "抢码头",
                          "pressure": "派打手砸摊"}],
        "humanity_ops": [{"dilemma": "救不救仇人的孩子", "choice": "救", "cost": "被自己人骂"}],
    }, chapter=7)
    assert st["promises"]
    assert st["opponents"]["王虎"]["goal"] == "抢码头"
    assert st["humanity"] and st["humanity"][-1]["chapter"] == 7


def test_life_of_prefers_explicit_field():
    assert state.life_of({"life": "dead", "status": "在世"}) == "dead"
    assert state.life_of({"status": "已阵亡"}) == "dead"
    assert state.life_of({"status": "失踪"}) == "gone"
    assert state.life_of({"status": "尚未登场"}) == "not_yet"


def test_excerpt_surfaces_opponents_and_promises():
    st = state.empty_state()
    st["metrics"]["last_chapter"] = 10
    st["characters"]["陆沉"] = {"life": "alive", "status": "在世", "last_seen": 10}
    st["opponents"]["王虎"] = {"name": "王虎", "goal": "抢码头", "pressure": "派打手", "status": "active"}
    st["promises"]["title:神棍"] = {"text": "书名承诺「神棍」必须兑现", "kind": "title", "status": "open"}
    txt = state.excerpt(st, max_chars=6000)
    assert "在册对手" in txt and "王虎" in txt
    assert "未兑现承诺" in txt and "神棍" in txt


# ---------------- 场景级章卡 ----------------

def test_scene_and_engine_are_normalized():
    scenes = longform._norm_scenes([
        {"want": "拿到批文", "obstacle": "王虎卡着", "choice": "当众对质",
         "cost": "得罪商会", "outcome": "批文拿到但被记账", "value_shift": "负转正"},
        None, {"want": "x"},
    ])
    assert len(scenes) == 2
    assert scenes[0]["cost"] == "得罪商会"
    assert scenes[1]["want"] == "x"
    eng = longform._norm_engine({"mystery_move": "埋", "title_hit": "神棍",
                                 "opponent_action": "王虎派人砸摊"})
    assert eng["mystery_move"] == "setup"
    assert eng["opponent_action"].startswith("王虎")


# ---------------- 引擎对账 ----------------

def _card(engine=None, scenes=None):
    return {"n": 5, "engine": engine or {}, "scenes": scenes or []}


def test_engine_flags_abstract_opponent():
    finds = engine_check.check_chapter(
        "局势越来越紧张，时代洪流推着每个人往前走。" * 5, 5,
        _card({"opponent_action": "时代压力与局势逼迫"}),
        ["神棍"], ["王虎"])
    assert any(f["code"] == "E02" for f in finds)


def test_engine_flags_missing_emotion_and_cost():
    text = "他签了字，把文件收进抽屉，然后回家吃饭。"
    finds = engine_check.check_chapter(
        text, 5,
        _card({"emotion_beat": "憋屈", "humanity_choice": "救还是保项目"},
              [{"want": "签单", "obstacle": "对手加价", "choice": "签", "cost": "", "outcome": ""}]),
        ["神棍"], [])
    codes = {f["code"] for f in finds}
    assert "E03" in codes and "E04" in codes and "E05" in codes and "E06" in codes


def test_engine_mystery_setup_must_close_in_window():
    chapters = [{"n": i, "text": "。"} for i in range(1, 20)]
    cards = {1: _card({"mystery_move": "setup"}), 5: _card({"mystery_move": "advance"})}
    finds = engine_check.check_mystery(chapters, cards)
    assert not [f for f in finds if f["code"] == "E08"]   # 窗口内推进过
    cards2 = {1: _card({"mystery_move": "setup"})}
    finds2 = engine_check.check_mystery(chapters, cards2)
    assert [f for f in finds2 if f["code"] == "E08"]


def test_engine_book_level_flags_unpaid_title():
    chapters = [{"n": i, "text": "陆沉在码头搬货。" * 200} for i in range(1, 31)]
    proj = _Proj("", {"novelName": "民国神棍：我在桥头炸出工业革命", "chapters": []})
    eng = engine_check.scan(proj, chapters=chapters, cards={})
    assert any(f["code"] == "E00" and f["level"] == "high" for f in eng["findings"])


# ---------------- 文书密度 ----------------

def test_paperwork_density_triggers_on_forms_not_story():
    doc = ("他填写档案，核对台账，提交表格，走审批流程，按工序验收，"
           "签批文，做报表，统计检验，办手续，完成备案登记。" * 3)
    finds = craft_check.check_paperwork_density(doc, n=142)
    assert finds and finds[0]["gate"] == "C24"
    assert finds[0]["metric"]["per_k"] >= craft_check.TH["paperwork_per_k"]
    # 正常叙事不能误报
    normal = "他推开门，看见王虎坐在太师椅上，手里把玩着一枚铜钱。"
    assert craft_check.check_paperwork_density(normal, n=3) == []


def test_engine_scan_survives_craft_finding_schema(tmp_path):
    """引擎对账并入工艺结果时必须换字段名，否则 densest 章直接 500。"""
    doc = ("他填写档案，核对台账，提交表格，走审批流程，按工序验收，"
           "签批文，做报表，统计检验，办手续，完成备案登记。" * 3)
    proj = _project(tmp_path, {142: doc})
    data = engine_check.scan(proj, chapters=[{"n": 142, "text": doc}], cards={})
    c24 = [f for f in data["findings"] if f["code"] == "C24"]
    assert c24 and all(f["level"] == "info" for f in c24)
    assert data["counts"]["info"] >= 1


# ---------------- 题材修饰 ----------------

def test_genre_modifiers_deep_merge():
    base = taste.spec("serial", {})
    hist = taste.spec("serial", {"genre": "历史"})
    assert hist["rhythm"]["small_payoff_every"] == 2
    assert hist["rhythm"]["hook_every_chapter"] == base["rhythm"]["hook_every_chapter"]  # 未被覆盖
    assert hist["emotion"]["density"] == "high"
    assert taste.gate("C24") is not None


# ---------------- 旧书整改 ----------------

def test_retrofit_orders_surgery_by_author_priority(tmp_path):
    chapters = {
        1: "短。",                                   # 字数不足
        2: "正常一章。" * 800 + "。",                 # 有评分 50
        3: "他填档案，核对台账，走审批流程，做报表，办手续。" * 60,
        4: "陆沉在桥头搬货。" * 600,
        5: "陆沉回家。" * 600,
    }
    proj = _project(tmp_path, chapters)
    proj._plan["meta"] = {"chapter_scores": {"2": {"score": 50}, "4": {"score": 82}}}
    data = retrofit.plan(proj)
    assert data["title_decision"] == "rename_or_strengthen"
    assert data["surgical"][0]["tier"] == 1
    assert any(x["tier"] == 2 and x["n"] == 2 for x in data["surgical"])
    md = retrofit.render(data)
    assert "先改这 20 章" in md and "书名" in md
    assert os.path.exists(os.path.join(str(tmp_path), "27-旧书整改方案.md")) is False  # plan() 只算不写
    retrofit.run(proj, quiet=True)
    assert os.path.exists(os.path.join(str(tmp_path), "27-旧书整改方案.md"))