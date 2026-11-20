"""v4.41.1：导出补全批次必须真的处理 N03 指定章。

run_short_expand 只挑「低于目标字数」的章；N03 常常字数够但停在半句，
走那条路会被整批跳过，导出于是永远复检同一份内容。
"""
import app
from bot import writer


def test_cut_chapter_keeps_completed_continuation_even_if_score_drops():
    # 半句截断原稿的分数没有完整度含义：补全后略降也必须保留
    assert writer._expand_should_revert(True, 80, 61) is False
    assert writer._expand_should_revert(True, 80, 40) is False


def test_normal_chapter_still_reverts_on_clear_score_drop():
    assert writer._expand_should_revert(False, 80, 61) is True
    assert writer._expand_should_revert(False, 80, 90) is False


def test_finish_batch_uses_local_completion_for_every_requested_chapter(monkeypatch):
    """v4.41.4：截断补全走「只补结尾」的 finish_truncated_chapter，
    不再整章扩写（老路模型只回半篇，三轮全废）。"""
    calls = []

    class FakeProject:
        def __init__(self, *a, **k):
            pass

    def _fake_finish(proj, n, max_rounds=2):
        calls.append((n, max_rounds))
        return {"ch": n, "kept": True, "note": "已局部补全结尾"}

    monkeypatch.setattr(app.writer, "Project", FakeProject)
    monkeypatch.setattr(app.writer, "_project_dir", lambda key: "/tmp/fake")
    monkeypatch.setattr(app.writer, "finish_truncated_chapter", _fake_finish)
    monkeypatch.setattr(app, "_rw_release", lambda *a: None)

    res = app._rw_finish_batch("k", [116, 290], 3200, "job-1")

    assert sorted(c[0] for c in calls) == [116, 290]
    assert all(c[1] == 2 for c in calls)
    assert {r["ch"] for r in res} == {116, 290}
    assert all(r["kept"] for r in res)
