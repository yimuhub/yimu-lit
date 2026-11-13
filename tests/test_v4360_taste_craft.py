"""v4.36.0：市场标尺 + 工艺体检 + 账本校准 的回归测试。

覆盖四件事：
  1. 双形态（连载 / 出版）的结构参数与写作纪律确实不同；
  2. 工艺检查能抓住真实事故（复读、半句截断、元信息泄漏、计数穿帮、格式污染）
     且**不误报**正常的叙述与合法的中文表达；
  3. 确定性自愈只修该修的，不碰需要创作的部分；
  4. 线索账本按表头解析，不再把「第4章」当描述、不再产生 `:---` 垃圾条目。
"""
from bot import craft_check as cc
from bot import factcard, state, taste


# ---------------- 双形态 ----------------

def test_form_normalize_covers_aliases():
    assert taste.normalize("连载") == taste.FORM_SERIAL
    assert taste.normalize("出版型") == taste.FORM_BOOK
    assert taste.normalize("book") == taste.FORM_BOOK
    assert taste.normalize("") == taste.DEFAULT_FORM
    assert taste.normalize("不认识") == taste.DEFAULT_FORM


def test_serial_and_book_have_different_structure():
    s = taste.spec(taste.FORM_SERIAL)
    b = taste.spec(taste.FORM_BOOK)
    assert s["default_chapters"] > b["default_chapters"]
    assert s["default_chapter_words"] < b["default_chapter_words"]
    # 连载要每章留钩，出版不要求——这是两种形态最本质的差异
    assert s["rhythm"]["hook_every_chapter"] is True
    assert b["rhythm"]["hook_every_chapter"] is False
    # 出版型伏笔必须全回收，连载允许留续作钩
    assert b["foreshadow"]["must_payoff_ratio"] == 1.0
    assert b["ending"]["closed"] is True


def test_discipline_text_differs_between_forms():
    s = taste.writing_discipline({"form": "serial"})
    b = taste.writing_discipline({"form": "book"})
    assert "每章结尾必须留钩" in s
    assert "不要求" in b and "硬拗悬念" in b
    assert s != b


def test_explicit_cfg_wins_over_form_default():
    cfg = {"form": "book", "chapters": 12, "min_words_per_chapter": 3000}
    assert taste.chapter_count_of(cfg) == 12
    assert taste.chapter_words_of(cfg) == 3000


def test_book_structure_template_uses_three_acts():
    tpl = taste.structure_template({"form": "book"})
    assert "第一幕" in tpl and "第二幕" in tpl and "第三幕" in tpl
    assert "25%" in tpl and "50%" in tpl


# ---------------- 工艺检查：能抓到真事故 ----------------

def test_detects_repeat_loop():
    para = "他推开门，冷风灌了进来，烛火猛地一跳，影子在墙上晃了晃。"
    text = "\n".join([para] * 8 + ["她抬起头，看向门外。"])
    finds = cc.check_chapter(text, n=91, cfg={"form": "serial"})
    gates = {f["gate"] for f in finds if f["severity"] == "block"}
    assert "C02" in gates


def test_detects_truncation():
    text = "他握紧刀柄，往前踏了一步，忽然听见身后传来马蹄声，仿佛是"
    finds = cc.check_chapter(text, n=7, cfg={"form": "serial"})
    assert any(f["gate"] == "C01" and f["severity"] == "block" for f in finds)


def test_detects_meta_leak():
    text = "她摸着那枚铜钱，想起自己在第八章的宴席上见过同样的纹路。铜钱冰凉。"
    finds = cc.check_chapter(text, n=13, cfg={"form": "serial"})
    assert any(f["gate"] == "C03" and "第八章" in f["message"] for f in finds)


def test_detects_count_error():
    text = "他盯着碗底，忽然说道：“含笑半步颠。”她在心中默念这三个字。"
    finds = cc.check_chapter(text, n=75, cfg={"form": "serial"})
    hits = [f for f in finds if f["gate"] == "C05"]
    assert hits and "5" in hits[0]["message"]


def test_detects_format_pollution():
    text = "他笑了。**这不对劲。**门外传来脚步声。"
    finds = cc.check_chapter(text, n=100, cfg={"form": "serial"})
    assert any(f["gate"] == "C06" for f in finds)


def test_detects_process_residue():
    """真实事故：重写环节返回的改稿报告把正文整段覆盖掉了。"""
    text = (
        "## 正文\n"
        "修改思路：把沈清漪这场戏改得更冷一些，减少她的心理描写，"
        "让萧珩的压迫感通过动作体现。\n"
        "他推门进来。\n"
    )
    finds = cc.check_chapter(text, n=7, cfg={"form": "serial"})
    assert any(f["gate"] == "C04" for f in finds)


def test_meta_section_is_not_process_residue():
    """`## 章节备注` 是流水线合法字段，导出前会被剥离，不能算过程产物。"""
    text = "## 正文\n他推门进来。\n\n## 章节备注\n下章接密信。"
    finds = cc.check_chapter(text, n=7, cfg={"form": "serial"})
    assert not [f for f in finds if f["gate"] == "C04"]


# ---------------- 工艺检查：不能误报 ----------------

def test_no_false_positive_on_name_reference():
    """「承乾二字」是把前文专名回指，是正确中文，不能判成计数穿帮。"""
    text = "“沈清漪，你比那个蠢货承乾，有趣得多。”\n承乾二字，从李泰口中说出来，像一把钝刀。"
    finds = cc.check_chapter(text, n=25, cfg={"form": "serial"})
    assert not [f for f in finds if f["gate"] == "C05"]


def test_no_false_positive_on_single_char_quote():
    """「刻着一个“沈”字」是单字引用，不是计数穿帮。"""
    text = "那上面，同样刻着一个“沈”字。而那个“沈”字旁，标注着四个字——“毒杀先帝”。"
    finds = cc.check_chapter(text, n=4, cfg={"form": "serial"})
    assert not [f for f in finds if f["gate"] == "C05"]


def test_no_false_positive_on_ellipsis_quote():
    """引号里含省略号属于缩略引用，不能直接数字数。"""
    text = "“替……长孙家。” 这两个字，像是一记重锤砸在他胸口。"
    finds = cc.check_chapter(text, n=57, cfg={"form": "serial"})
    assert not [f for f in finds if f["gate"] == "C05"]


def test_epigraph_quote_block_is_not_pollution():
    text = "> “史书是赢家的笔，但命运是活人的局。”\n\n他推门进来，把密信放在案上。"
    finds = cc.check_chapter(text, n=21, cfg={"form": "serial"})
    assert not [f for f in finds if f["gate"] == "C06"]


def test_standard_section_headings_are_not_pollution():
    text = ("## 本章概要\n她在殿中醒来。\n\n## 正文\n她睁开眼，看见帐顶的绣纹。"
            "门外传来脚步声，一下，又一下。\n\n## 章节备注\n下章接密信。")
    finds = cc.check_chapter(text, n=5, cfg={"form": "serial"})
    assert not [f for f in finds if f["gate"] == "C06"]


# ---------------- 确定性自愈 ----------------

def test_autofix_dedupes_repeat_but_keeps_content():
    para = "他推开门，冷风灌了进来，烛火猛地一跳，影子在墙上晃了晃。"
    text = "\n".join([para] * 8 + ["她抬起头，看向门外。"])
    finds = cc.check_chapter(text, n=1, cfg={"form": "serial"})
    fixed, applied = cc.autofix(text, finds)
    assert "复读去重" in applied
    assert len(fixed.split("\n")) == 2
    assert fixed.count(para) == 1


def test_autofix_fixes_count_and_meta():
    text = "她说：“含笑半步颠。”他在心中默念这三个字，又想起第八章的事。"
    finds = cc.check_chapter(text, n=1, cfg={"form": "serial"})
    fixed, applied = cc.autofix(text, finds)
    assert "数数校正" in applied and "元信息改写" in applied
    assert "五个字" in fixed
    assert "第八章" not in fixed


def test_autofix_does_not_touch_truncation():
    """半句截断要补什么句子属于创作，自愈层不能代笔。"""
    text = "他握紧刀柄，往前踏了一步，忽然听见身后传来马蹄声，仿佛是"
    finds = cc.check_chapter(text, n=1, cfg={"form": "serial"})
    fixed, applied = cc.autofix(text, finds)
    assert "半句截断" not in applied
    assert fixed == text


# ---------------- 线索账本解析 ----------------

LEDGER = """# 线索账本

## 登记表

| ID | 线索 | 首次出现(章) | 初始表象 | 真实含义 | 可推理证据 | 误导方式 | 计划兑现(章) | 状态 |
|:---|:---|:---|:---|:---|:---|:---|:---|:---|
| **L1** | 原主身世黑箱 | 第4章 | 太医之女 | 实为被灭门的世家罪孤 | 第5章发现的旧档矛盾 | 身份误导 | 第5-6章（揭示） | open |
| **L2** | 魏王幕后黑手 | 第6章 | 旧部运作 | 李泰借壳上市 | 第7章试探 | 伪装成忠君 | 第7章 | paid |
"""


def test_ledger_parses_by_header_not_position():
    d = state._parse_clue_ledger(LEDGER)
    assert set(d) == {"L1", "L2"}
    # 描述必须是「线索｜真实含义」，绝不能再是「第4章」
    assert "原主身世黑箱" in d["L1"]["description"]
    assert "世家罪孤" in d["L1"]["description"]
    assert d["L1"]["description"] != "第4章"


def test_ledger_range_and_status():
    d = state._parse_clue_ledger(LEDGER)
    assert (d["L1"]["due_from"], d["L1"]["due_to"]) == (5, 6)
    assert (d["L2"]["due_from"], d["L2"]["due_to"]) == (7, 7)
    assert d["L1"]["setup_chapter"] == 4
    assert d["L2"]["status"] == "paid"


def test_ledger_skips_alignment_row():
    d = state._parse_clue_ledger(LEDGER)
    assert ":---" not in d
    assert all(state._is_person_key(k) or k.startswith("L") for k in d)


def test_ledger_survives_unclosed_bracket_name():
    txt = ("| ID | 线索 | 计划兑现(章) | 状态 |\n|:--|:--|:--|:--|\n"
           "| L9 | 断掉的括号 | 第3章 | open |")
    d = state._parse_clue_ledger(txt)
    assert d["L9"]["description"].startswith("断掉的括号")


# ---------------- 状态库清洗 ----------------

def test_normalize_drops_non_character_keys():
    raw = {"characters": {
        "沈清漪": {"status": "在世"},
        "locations": {"status": "已登记"},
        "items": {"status": "已登记"},
    }}
    st = state._normalize_state(raw)
    assert "沈清漪" in st["characters"]
    assert "locations" not in st["characters"]
    assert "items" not in st["characters"]


def test_normalize_merges_parenthesised_duplicate():
    raw = {"characters": {
        "沈清漪（主角）": {"status": "已登记"},
        "沈清漪": {"status": "已执掌尚宫司", "location": "长安"},
    }}
    st = state._normalize_state(raw)
    assert list(st["characters"]) == ["沈清漪"]
    assert st["characters"]["沈清漪"]["status"] == "已执掌尚宫司"


def test_normalize_backfills_importance_and_drops_junk_id():
    raw = {"foreshadowing": {
        ":---": {"description": ":---"},
        "L1": {"description": "信物", "status": "open"},
    }}
    st = state._normalize_state(raw)
    assert ":---" not in st["foreshadowing"]
    assert st["foreshadowing"]["L1"]["importance"] == "B"


def test_effective_foreshadow_excludes_empty_shells():
    st = state.empty_state()
    st["foreshadowing"] = {
        "empty_shell": {"description": "", "due_from": 0, "due_to": 0, "status": "open"},
        "real_one": {"description": "密信", "due_from": 10, "due_to": 12, "status": "open"},
    }
    eff = state.effective_foreshadow(st)
    assert list(eff) == ["real_one"]


# ---------------- 事实卡 ----------------

def test_factcard_render_lists_limits():
    fc = {
        "chapter": 12,
        "allowed": ["沈清漪"],
        "forbidden": [{"name": "李建成", "reason": "已退场（身死）", "names": ["李建成"]}],
        "alias_table": {"李承乾": {"aliases": ["太子"], "status": "在位", "location": "东宫"}},
        "must_pay": [{"id": "L1", "desc": "身世", "due_to": 14}],
        "items": [{"name": "铜钱", "holder": "沈清漪"}],
        "new_name_limit": 1,
    }
    txt = factcard.render(fc)
    assert "禁出场" in txt and "李建成" in txt
    assert "太子＝李承乾" in txt
    assert "最多新增 1 个" in txt


def test_factcard_validate_flags_dead_character():
    fc = {"chapter": 5, "allowed": ["沈清漪"],
          "forbidden": [{"name": "李建成", "reason": "已退场（身死）", "names": ["李建成"]}]}
    hits = factcard.validate(fc, "李建成推门进来，看着沈清漪。", 5)
    assert any(h["code"] == "F01" and h["level"] == "block" for h in hits)


def test_factcard_validate_allows_listed_character():
    fc = {"chapter": 5, "allowed": ["沈清漪"],
          "forbidden": [{"name": "李建成", "reason": "已退场（身死）", "names": ["李建成"]}]}
    assert factcard.validate(fc, "沈清漪推门进来。", 5) == []


# ---------------- 交付闸门：不误伤题材、不重复计数 ----------------

def test_ambiguous_meta_word_does_not_block_export():
    """系统流小说里「系统核心自检报告弹出来了」是故事内设定，不能判成元信息泄漏。

    《入赘三年》实拍：这类词把 40 章整书导出全拦下来，而它根本不是流程残留。
    现在降级为 N19 告警，报告里仍然看得见，但不阻断交付。
    """
    from bot import delivery_gate as dg
    rep = dg.check_novel([{"n": 1, "title": "第1章",
                           "text": "玉佩发光时，系统核心自检报告弹出来了。他浑身一震。"}])
    assert not [f for f in rep["findings"] if f["code"] == "N02"]
    assert [f for f in rep["findings"] if f["code"] == "N19"]


def test_real_meta_leak_still_blocks():
    from bot import delivery_gate as dg
    rep = dg.check_novel([{"n": 3, "title": "第3章",
                           "text": "他想起第20章的宴席，那枚玉片还在袖中。"}])
    assert [f for f in rep["findings"] if f["code"] == "N02"]
    assert rep["blocking_count"] >= 1


def test_same_chapter_meta_leak_counted_once():
    """check_chapter 与工艺闸门说的是同一件事，只能记一项——否则阻断数虚高。"""
    from bot import delivery_gate as dg
    rep = dg.check_novel([{"n": 3, "title": "第3章",
                           "text": "他想起第3章那把剑。第3章他就注意到了，那剑从不离身。"}])
    assert len([f for f in rep["findings"] if f["code"] == "N02"]) == 1


def test_report_notes_lead_with_yardstick():
    """报告开头必须先给形态与平台基准，再列禁止项。"""
    notes = taste.report_notes({"form": "book", "platform": "fanqie",
                                "chapters": 45, "min_words_per_chapter": 4200})
    assert notes[0].startswith("作品形态：出版型")
    assert "番茄口径" in notes[1]


def test_benchmark_note_accepts_cn_platform_name():
    assert "番茄口径" in taste.benchmark_note("番茄小说")
    assert "起点口径" in taste.benchmark_note("起点读书")
    # 没有公开基准的平台不编数字，只承认「没有口径」
    assert "暂无公开量化基准" in taste.benchmark_note("七猫")
