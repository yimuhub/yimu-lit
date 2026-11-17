"""v4.38.3：坏稿识别、版本库还原、重写写盘护栏、长章不再截断输入。"""
import os
import threading

from bot import versions, writer


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


def _plan(path, entries):
    chs = []
    for no, body in entries.items():
        fp = os.path.join(path, f"{no:02d}.md")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(body)
        chs.append({"chapterNumber": no, "filePath": f"{no:02d}.md", "status": "completed"})
    return {"novelName": "测试书", "chapters": chs, "config": {}}


_NOTE = ("当前诊断信息存在缺项，暂时无法可靠执行整章修订：\n"
         "1. 第335行 blocking 只标注了「空洞形容」，修改意见为空。\n请补充原始诊断记录。")


def test_looks_like_model_note_and_guard():
    assert writer.looks_like_model_note(_NOTE) is True
    assert writer.looks_like_model_note("") is True
    assert writer.looks_like_model_note("我可以按“只改指定位置、其余正文原样保留”的原则处理。") is True
    good = "# 第1章 交易\n\n## 章首引子\n引子。\n\n## 正文\n" + "陆沉" * 600
    assert writer.looks_like_model_note(good) is False
    assert writer.rewrite_reject_reason(good, _NOTE)
    assert writer.rewrite_reject_reason(good, "# 第1章\n\n" + "短" * 50)
    assert writer.rewrite_reject_reason(good, good) == ""


def test_repair_restores_even_when_note_is_longer(tmp_path):
    good = "# 第1章 原文\n\n## 正文\n" + "原" * 3200
    long_note = _NOTE + "\n- 说明行\n" * 800   # 长篇追问，字符数超过原稿
    plan = _plan(str(tmp_path), {1: long_note})
    proj = _Proj(str(tmp_path), plan)
    versions.snapshot(str(tmp_path), 1, good, label="原稿")
    out = writer.repair_corrupted_chapters(proj, min_words=3000)
    assert [r["ch"] for r in out["repaired"]] == [1]
    assert "原" * 100 in open(os.path.join(str(tmp_path), "01.md"), encoding="utf-8").read()


def test_repair_restores_newest_good_version(tmp_path):
    good_old = "# 第1章 旧稿\n\n## 正文\n" + "旧" * 3200
    good_new = "# 第1章 新稿\n\n## 正文\n" + "新" * 3400
    plan = _plan(str(tmp_path), {1: _NOTE})
    proj = _Proj(str(tmp_path), plan)
    versions.snapshot(str(tmp_path), 1, good_old, label="更早")
    versions.snapshot(str(tmp_path), 1, good_new, label="较新")
    out = writer.repair_corrupted_chapters(proj, min_words=3000)
    assert [r["ch"] for r in out["repaired"]] == [1]
    body = open(os.path.join(str(tmp_path), "01.md"), encoding="utf-8").read()
    assert "新" * 100 in body and "旧" * 100 not in body
    assert any("还原" in m for m in proj.logs)
    # 坏稿也留了快照，可回退
    assert any("请补充" in versions.read_version(str(tmp_path), 1, v["id"])
               for v in versions.list_versions(str(tmp_path), 1))


def test_expand_restores_note_before_expanding(tmp_path, monkeypatch):
    good = "# 第1章 交易\n\n## 正文\n" + "原" * 1000
    plan = _plan(str(tmp_path), {1: _NOTE})
    proj = _Proj(str(tmp_path), plan)
    versions.snapshot(str(tmp_path), 1, good, label="原稿")
    monkeypatch.setattr(writer, "score_chapter", lambda _p, _n: 80)
    monkeypatch.setattr(writer, "_build_ctx", lambda _p, _c: {})
    monkeypatch.setattr(writer.workflow, "expand_chapter",
                        lambda body, ctx, min_words=3000, current_words=0:
                        "# 第1章 交易\n\n## 正文\n" + "扩" * 3200)
    r = writer.expand_short_chapter(proj, 1, target_words=3000)
    assert r["kept"] is True
    body = open(os.path.join(str(tmp_path), "01.md"), encoding="utf-8").read()
    assert "扩" * 100 in body
    assert any("还原" in m for m in proj.logs)


def test_expand_restored_long_version_needs_no_llm(tmp_path, monkeypatch):
    good = "# 第1章 交易\n\n## 正文\n" + "原" * 3200
    plan = _plan(str(tmp_path), {1: _NOTE})
    proj = _Proj(str(tmp_path), plan)
    versions.snapshot(str(tmp_path), 1, good, label="原稿")

    def _boom(*_a, **_k):
        raise AssertionError("已达字数不应再调扩写")

    monkeypatch.setattr(writer.workflow, "expand_chapter", _boom)
    r = writer.expand_short_chapter(proj, 1, target_words=3000)
    assert r["kept"] is True and "还原" in r["note"]


def test_truncated_short_chapter_restores_longer_version(tmp_path, monkeypatch):
    truncated = "# 第1章 交易\n\n## 正文\n" + "短" * 1200 + "后面跟"
    complete = "# 第1章 交易\n\n## 正文\n" + "完整" * 1600 + "。"
    plan = _plan(str(tmp_path), {1: truncated})
    proj = _Proj(str(tmp_path), plan)
    versions.snapshot(str(tmp_path), 1, complete, label="完整稿")

    def _boom(*_a, **_k):
        raise AssertionError("有完整历史稿时不应调扩写")

    monkeypatch.setattr(writer.workflow, "expand_chapter", _boom)
    r = writer.expand_short_chapter(proj, 1, target_words=3000)
    assert r["kept"] is True and "还原" in r["note"]
    assert r["after_words"] >= 3000


def test_truncated_long_version_is_not_restored(tmp_path, monkeypatch):
    current = "# 第1章 交易\n\n## 正文\n" + "短" * 1200 + "。"
    truncated_old = "# 第1章 交易\n\n## 正文\n" + "旧" * 4000 + "半句"
    plan = _plan(str(tmp_path), {1: current})
    proj = _Proj(str(tmp_path), plan)
    versions.snapshot(str(tmp_path), 1, truncated_old, label="截断旧稿")
    monkeypatch.setattr(writer, "score_chapter", lambda _p, _n: 80)
    monkeypatch.setattr(writer, "_build_ctx", lambda _p, _c: {})
    called = {}

    def fake_expand(body, ctx, min_words=3000, current_words=0):
        called["yes"] = True
        return "# 第1章 交易\n\n## 正文\n" + "扩" * 3200 + "。"

    monkeypatch.setattr(writer.workflow, "expand_chapter", fake_expand)
    r = writer.expand_short_chapter(proj, 1, target_words=3000)
    assert called.get("yes")
    assert r["kept"] is True
    assert "旧" * 100 not in open(os.path.join(str(tmp_path), "01.md"), encoding="utf-8").read()


def test_expand_keeps_small_score_dip(tmp_path, monkeypatch):
    plan = _plan(str(tmp_path), {1: "# 第1章 交易\n\n## 正文\n" + "原" * 1200})
    proj = _Proj(str(tmp_path), plan)
    scores = iter([72.0, 69.0])   # 降 3 分，属于评分波动，不应还原
    monkeypatch.setattr(writer, "score_chapter", lambda _p, _n: next(scores))
    monkeypatch.setattr(writer, "_build_ctx", lambda _p, _c: {})
    monkeypatch.setattr(writer.workflow, "expand_chapter",
                        lambda body, ctx, min_words=3000, current_words=0:
                        "# 第1章 交易\n\n## 正文\n" + "扩" * 3200)
    r = writer.expand_short_chapter(proj, 1, target_words=3000)
    assert r["kept"] is True


def test_expand_retries_after_no_growth_round(tmp_path, monkeypatch):
    body = "# 第1章 交易\n\n## 正文\n" + "原" * 2800
    plan = _plan(str(tmp_path), {1: body})
    proj = _Proj(str(tmp_path), plan)
    calls = []

    def fake_expand(cur, ctx, min_words=3000, current_words=0):
        calls.append(1)
        if len(calls) == 1:
            return cur                       # 模型第一轮只回等长改写
        return "# 第1章 交易\n\n## 正文\n" + "扩" * 3300

    monkeypatch.setattr(writer, "score_chapter", lambda _p, _n: 80)
    monkeypatch.setattr(writer, "_build_ctx", lambda _p, _c: {})
    monkeypatch.setattr(writer.workflow, "expand_chapter", fake_expand)
    r = writer.expand_short_chapter(proj, 1, target_words=3000)
    assert len(calls) == 2
    assert r["kept"] is True and r["after_words"] >= 3000


def test_fix_chapter_sends_full_chapter_body(monkeypatch):
    seen = {}

    def fake_chat(messages, **kwargs):
        seen["user"] = messages[-1]["content"]
        return "# 第1章\n\n## 正文\n修好了"

    monkeypatch.setattr(writer.workflow, "_agent_disabled", lambda *_a, **_k: False)
    monkeypatch.setattr(writer.workflow.llm, "chat", fake_chat)
    long_body = "# 第1章 长章\n\n## 正文\n" + "文" * 12000
    writer.workflow.fix_chapter(long_body, "修一下", {"cfg": {}})
    assert long_body[-200:] in seen["user"]
    assert "禁止以缺页为由要求补发" in seen["user"]


def test_failed_expand_chapter_is_not_marked_done(tmp_path, monkeypatch):
    plan = _plan(str(tmp_path), {1: "# 第1章\n\n## 正文\n" + "短" * 500})
    proj = _Proj(str(tmp_path), plan)
    monkeypatch.setattr(writer, "Project", lambda *_a, **_k: proj)
    monkeypatch.setattr(writer, "repair_corrupted_chapters",
                        lambda *_a, **_k: {"repaired": [], "skipped": []})
    monkeypatch.setattr(writer, "expand_short_chapter",
                        lambda *_a, **_k: {"ch": 1, "kept": False,
                                           "after_words": 500, "note": "扩写没有增加有效字数"})
    marked = []
    monkeypatch.setattr(writer.journal, "mark_chapter_done",
                        lambda _path, ch, **_k: marked.append(ch))
    monkeypatch.setattr(writer.llm, "book", lambda *_a, **_k: None)
    writer.run_short_expand("k", [1], 3000, workers=1)
    assert marked == []
    assert any("补字未成功" in m for m in proj.logs)
