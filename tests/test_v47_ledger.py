# v4.7.0（借鉴 reveric）：手法账本——最近 k 章钩子/情绪/紧张度轨迹，防模式收敛。
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.v2api import _chapter_ledger


def test_chapter_ledger_tracks_recent(tmp_path):
    fp = tmp_path / "21-章摘要.jsonl"
    rows = []
    for i, (t, emo) in enumerate([(6, "紧张"), (7, "紧张"), (9, "爆点"), (4, "释然"), (5, "压抑"), (6, "悬疑")], 1):
        rows.append(json.dumps({"n": i, "quality_signals": {
            "hook": f"钩子{i}", "tension": t, "emotion": emo,
            "hook_type": ["危机", "谜题", "反转", "情感", "抉择", "危机"][i - 1]}}, ensure_ascii=False))
    fp.write_text("\n".join(rows) + "\n", encoding="utf-8")
    led = _chapter_ledger(str(tmp_path), 5)
    assert len(led["recent"]) == 5
    assert led["recent"][0]["n"] == 2            # 尾部 5 章
    assert led["tension_track"] == [7, 9, 4, 5, 6]
    assert led["since_peak"] == 3                # 峰值 9（紧张≥8）之后已过 3 章
    assert led["recent"][-1]["hook_type"] == "危机"


def test_chapter_ledger_empty_or_broken(tmp_path):
    assert _chapter_ledger(str(tmp_path), 5) == {}
    (tmp_path / "21-章摘要.jsonl").write_text("{broken\n", encoding="utf-8")
    led = _chapter_ledger(str(tmp_path), 5)
    assert not led.get("recent") and not led.get("tension_track")


def test_strip_chatter_variants():
    from bot.writer import _strip_chatter
    # 思考标签
    assert "推理" not in _strip_chatter("<think>推理过程</think>正文")
    # 客套开场白
    t = _strip_chatter("好的，以下是第3章：\n正文内容")
    assert not t.startswith("好的") and "正文内容" in t
    # 代码围栏
    t = _strip_chatter("```markdown\n正文X\n```")
    assert "```" not in t and "正文X" in t
    # 正文中间重复章题行被剥（首行保留）
    t = _strip_chatter("# 第03章 标题\n正文A\n# 第03章 标题\n正文B")
    assert t.count("第03章") == 1 and "正文A" in t and "正文B" in t


def test_cache_key_distinguishes_chapters():
    from bot.llm import _cache_key
    base = "X" * 9000
    k1 = _cache_key("m", 0.7, False, [{"role": "user", "content": base + "第1章"}])
    k2 = _cache_key("m", 0.7, False, [{"role": "user", "content": base + "第2章"}])
    assert k1 and k2 and k1 != k2
