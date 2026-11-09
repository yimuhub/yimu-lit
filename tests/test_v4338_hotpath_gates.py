"""v4.33.8 热路径门禁回归：把《收服十万巨兽后，我无敌了》成稿里的每一类真实翻车
样本固化为守卫用例——同类问题不允许再次进入任何作品。

覆盖四道新增闸门：
1. 落盘前三查（N01 非中文残留 / N02 元信息·工作残留）——writer._write_chapter 落盘前；
2. 语句级工作残留模式（META_RESIDUAL_PATTERNS）——比 META_LEAK 词表更隐蔽的生产痕迹；
3. 成书对账（正文/场文件不齐不标 completed）——writer._missing_chapter_nos /
   script_writer._missing_scene_nos；
4. 未达标不放行（放行门禁未过默认拦截导出）——export.release_export_block。
"""
from types import SimpleNamespace

from bot import delivery_gate as gate
from bot import delivery_spec as spec
from bot import export as export_mod
from bot import script_writer, writer


# ------------------------------------------------------- 真实翻车样本（回归语料）

# 来源见《收服十万巨兽后，我无敌了》小说/短剧成稿，每条都是实际漏进正文的生产痕迹
REAL_SAMPLES = [
    # （应命中编号，原文）
    ("N01", "他抬起手，diagonally 指向地面，对角犀说：让他们看看。"),
    ("N01", "青狼也抬头看他，金色的瞳孔中倒映着 colder 的光。"),
    ("N01", "林野想到第23章大统领的自爆，忽然明白了什么。"),   # 章节号元引用（N02 类）
    ("N02", "张野是原主父亲的名字？不对……可能是个错误。我们需要处理……我们不要纠结这个，继续写。"),
    ("N02", "（第7章续写完成，约2276字）"),
    ("N02", "场记：本集完成第 29-30 章主要事件"),
    ("N02", "「在第10章，他入山后收服的。」二长老翻着竹简说。"),
    ("N01", "小满：可以。但你不是在与巨兽战斗，Host。"),
]

CLEAN_TEXTS = [
    "林野握紧木棍，指节泛白。他知道，明天不是生，就是死。",
    "苏清柔把退婚书甩在尘土里：你天生无灵根，无法契约巨兽。",
    "卫兵举起盾牌，广场上响起号角。INT 武器库的门被推开。",
]


def test_real_samples_are_blocked():
    for _code, text in REAL_SAMPLES:
        hits = gate.chapter_purity_blocks(text)
        assert hits, f"真实翻车样本未被拦截：{text[:30]}"
        assert hits[0]["code"] in ("N01", "N02"), text[:30]


def test_clean_texts_pass_purity():
    for text in CLEAN_TEXTS:
        assert gate.chapter_purity_blocks(text) == [], text[:30]


def test_meta_residual_patterns_inventory():
    names = {n for n, _ in spec.META_RESIDUAL_PATTERNS}
    assert {"字数备注", "章节号元引用", "写作过程独白", "工作备注"} <= names


def test_meta_residual_edge_cases():
    # 不误伤：中文数字字数、正文里的普通对话、白名单缩写
    assert gate.chapter_purity_blocks("他数了数，竹简约有三千字。") == []
    assert gate.chapter_purity_blocks("「你继续写你的奏折。」皇帝说。") == []
    # 误伤兜底：正向样本必须命中
    assert gate.chapter_purity_blocks("本章完成，接下来写下一章。")
    assert gate.chapter_purity_blocks("TODO: 补一段打斗")


# ------------------------------------------------------- 章节号元引用的边界

def test_chapter_number_meta_reference():
    hits = gate.chapter_purity_blocks("二长老说：「在第10章，他入山后收服的。」")
    assert hits and hits[0]["code"] == "N02"


# ------------------------------------------------------- 成书对账（0.3）

def _mk_proj(tmp_path, files: dict[str, str]):
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


def test_missing_chapter_nos(tmp_path):
    proj = SimpleNamespace(path=str(tmp_path))
    plan = {"chapters": [
        {"chapterNumber": 1, "filePath": "01-正文/第001章.md"},
        {"chapterNumber": 2, "filePath": "01-正文/第002章.md"},
        {"chapterNumber": 3, "filePath": "01-正文/第003章.md"},
    ]}
    _mk_proj(tmp_path, {
        "01-正文/第001章.md": "# 第001章 开端\n\n正文。",
        "01-正文/第002章.md": "   \n",          # 空文件也算缺
    })
    assert writer._missing_chapter_nos(proj, plan) == [2, 3]


def test_missing_scene_nos(tmp_path):
    body = tmp_path / "04-正文"
    body.mkdir()
    (body / "第001场.md").write_text("场1：外 广场 白天\n\n林野醒来。", encoding="utf-8")
    (body / "第002场.md").write_text("", encoding="utf-8")
    proj = SimpleNamespace(body_dir=lambda: str(body))
    plan = {"scenes": [
        {"no": 1, "fileName": "第001场.md"},
        {"no": 2, "fileName": "第002场.md"},
        {"no": 3, "fileName": ""},                # fileName 缺失 → 兜底命名不存在 → 缺
    ]}
    assert script_writer._missing_scene_nos(proj, plan) == [2, 3]


# ------------------------------------------------------- 未达标不放行（0.1）

def test_release_block_when_not_released():
    plan = {"meta": {"released": False, "final_quality": 59.5},
            "config": {"validation": {"pass_line": 80}}}
    blocked, msg = export_mod.release_export_block(plan)
    assert blocked and "59.5" in msg and "80" in msg


def test_release_not_block_for_history_or_released():
    # 历史作品：没跑过门禁（released 缺失）不拦
    assert export_mod.release_export_block({"meta": {}}) == (False, "")
    assert export_mod.release_export_block({}) == (False, "")
    # 已放行不拦
    assert export_mod.release_export_block(
        {"meta": {"released": True, "final_quality": 82}}) == (False, "")


def test_release_block_default_pass_line():
    plan = {"meta": {"released": False, "final_quality": 70}, "config": {}}
    blocked, msg = export_mod.release_export_block(plan)
    assert blocked and "80" in msg


# ------------------------------------------------------- 导出报告仍然可用

def test_delivery_report_markdown_contains_findings():
    chapters = [{"n": 1, "title": "第1章 开端", "text": "他抬起手，diagonally 指向地面。"}]
    rep = gate.check_delivery(chapters, title="测试书", kind="novel")
    md = gate.report_markdown(rep)
    assert "N01" in md and rep["ready"] is False
