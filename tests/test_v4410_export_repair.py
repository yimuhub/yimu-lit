"""v4.41.0：导出前确定性清理与假阳性回归。

锁的是「导出反复被同一批问题拦住」的根因：
  - 纯格式残留必须由规则清掉，不能交给会回滚的整章重写；
  - 「得分出」「得分成」「有权重新核算」「质量报告」「本章程」不是元信息；
  - 只写「第204章」的章名不是编号回显；
  - 模型诊断/修订清单混进正文，要在导出前剥离。
"""
import os
import threading

from bot import book_health, craft_check, delivery_gate, export_repair, purity


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


def test_score_words_inside_normal_prose_are_not_residue():
    for text in ("连买料的钱都得分出好几本账。",
                 "冷机、预热、正式切削，得分成几个阶段。",
                 "他有权重新核算。"):
        assert craft_check.check_process_residue(text, n=1) == []
        assert delivery_gate._meta_hits(text) == []


def test_real_score_field_is_still_residue():
    assert craft_check.check_process_residue("本章评分：九十五分。", n=1)
    assert craft_check.check_process_residue("得分 85，合格。", n=1)
    assert craft_check.check_process_residue("权重：0.3", n=1)


def test_meta_words_need_chapter_context():
    assert craft_check.check_meta_leak("我敲了敲那本章程。", n=1) == []
    assert craft_check.check_meta_leak("后面会出问题。", n=2) == []
    assert craft_check.check_meta_leak("上一章已经查到。", n=3)
    # 正文里的叙事性质量报告不是流程残留，字段式才是
    assert delivery_gate._meta_hits("这份质量报告上的合格两个字。") == []


def test_bare_chapter_title_is_not_number_echo():
    bare = delivery_gate.check_chapter("正文。", title="第204章", chapter_no=204)
    assert not [f for f in bare if f["code"] == "N04"]
    dup = delivery_gate.check_chapter("正文。", title="第204章 第204章", chapter_no=204)
    assert [f for f in dup if f["code"] == "N04"]


def test_clean_chapter_text_removes_format_residue():
    raw = (
        "# 第3章 第一次交易与打脸\n\n"
        "## 正文\n"
        "第三章 第一次交易与打脸\n\n"
        "上一章留下的疑问，到这里有了答案。\n\n"
        "#<escape>【缺失：稳定动力来源。】</escape>\n\n"
        "> 我没有马上收起名单。\n\n"
        "### 重新开机\n\n"
        "**铁水**终于稳了。\n"
    )
    new = export_repair.clean_chapter_text(raw, 3, "第一次交易与打脸")
    body = purity.chapter_body(new)
    assert "<escape>" not in new and "【缺失" not in new
    assert "第三章 第一次交易与打脸" not in body
    assert "上一章" not in body and "先前留下的疑问" in body
    assert "> 我" not in body and "我没有马上收起名单" in body
    assert "###" not in body and "重新开机" in body
    assert "**" not in body


def test_clean_chapter_text_strips_model_diagnosis():
    raw = (
        "目前文本存在一个影响完整修订的问题：**章节备注在第二条中途截断**。\n\n"
        "已能确认的 blocking 项：\n\n"
        "原句：\n> 上午的调查从账房延伸到了作坊。\n\n"
        "建议替换为：\n> 午前，老周带着采购记录去找行商。\n\n"
        "请补充两项材料：\n1. 第217章剩余的章节备注。\n\n"
        "账房里的算盘声渐渐稀了。\n\n"
        "孙掌柜把一册蓝皮货簿推到桌子中央，手指压着其中一页，没有急着翻开。\n\n"
        "“陆先生，您要查的轴料，我这里确实经手过几批。不过先说清楚，我是做运输的，不是替人查账的。”\n\n"
        "我没有伸手拿货簿，只把阿成手里的两张货单摊在桌上。\n\n"
        "周师傅站在炉台边，手里还攥着那张旧配方。他没有问我这批铁料从哪里来，只问明天能不能准时开工。我点了点头，又摇了摇头。\n\n"
        "孙掌柜看了一眼货单上的规格，沉默了很久，才让伙计去后间取另一本旧账。\n\n"
        "伙计把旧账搬出来时，封皮上落着一层灰。孙掌柜亲手翻开，指尖停在三月那一页，没有再往下翻。\n"
    )
    new = export_repair.clean_chapter_text(raw, 217, "第217章")
    body = purity.chapter_body(new)
    assert "blocking" not in body and "请补充" not in body
    assert "账房里的算盘声渐渐稀了" in body
    assert "孙掌柜" in body


def test_model_note_detection_covers_diagnosis_variants():
    assert book_health.looks_like_model_note(
        "目前文本存在一个影响完整修订的问题：章节备注在第二条中途截断。\n"
        "已能确认的 blocking 项：\n建议替换为：……")
    assert not book_health.looks_like_model_note(
        "# 第9章 试车\n\n## 正文\n" + "铁水顺着槽口流下来。" * 40)


def test_repair_writes_cleaned_chapter_and_gate_passes(tmp_path):
    pdir = str(tmp_path)
    text = (
        "# 第7章 秘密兵工厂\n\n## 正文\n"
        "第七章 秘密兵工厂\n\n"
        "<text>【重工文明系统】</text>\n\n"
        "上一章说过，赵四不会善罢甘休。\n\n"
        "“本金、利息、已经还过多少，都得分开核对。”\n\n"
        "连着买料的钱都得分出好几本账。\n\n"
        "铁水终于稳了。\n\n"
        "老周把第一炉铁水浇进砂模，抬手擦掉额角的汗，没有说一句多余的话。\n\n"
        "赵四的人还守在巷口，可今天谁也没有往里闯。\n\n"
        "我知道，这只是第一关。下一批货交不出来，巷口那些人就会换个说法进来。\n\n"
        "老周把炉门重新关上，火光从他脸上退下去。他说，机器能转，人心还得再稳一稳。\n\n"
        "我让人把今天出的件数记在墙上，每一件后面都写上经手人的名字。巷口的人要看，就让他们看。\n\n"
        "天黑之前，第一张完整的生产记录贴了上去。纸不厚，却比任何一句保证都管用。\n"
    )
    _write(pdir, "第07章-秘密兵工厂.md", text)
    plan = {"novelName": "测试书", "minWordsPerChapter": 300,
            "chapters": [{"chapterNumber": 7, "title": "秘密兵工厂",
                          "filePath": "第07章-秘密兵工厂.md", "wordCount": 0}]}
    proj = _Proj(pdir, plan)
    res = export_repair.repair(proj)
    assert res["changed"] == [7]
    new = open(os.path.join(pdir, "第07章-秘密兵工厂.md"), encoding="utf-8").read()
    body = purity.chapter_body(new)
    assert "<text>" not in new and "第七章 秘密兵工厂" not in body
    assert "上一章" not in body and "得分出" in body
    blocks = [f for f in delivery_gate.check_chapter(body, title="秘密兵工厂", chapter_no=7)
              if f["severity"] == "block"]
    assert not blocks
    # 版本库保留清理前原文
    assert os.listdir(os.path.join(pdir, ".versions", "ch007"))
