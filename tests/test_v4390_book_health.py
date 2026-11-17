"""v4.39.0：全书健康流水线——体检、还原、写盘门禁、批量入口统一接入。"""
import json
import os
import threading

from bot import book_health, chief, versions, writer


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
        chs.append({"chapterNumber": no, "filePath": f"{no:02d}.md",
                    "status": "completed", "wordCount": None})
    return {"novelName": "体检书", "minWordsPerChapter": 3000,
            "chapters": chs, "config": {}}


def _ch(no, body):
    return f"# 第{no}章\n\n## 正文\n{body}"


def test_audit_flags_meta_truncated_short_missing(tmp_path):
    plan = _plan(str(tmp_path), {
        1: _ch(1, "说" * 3200 + "。"),
        2: _ch(2, "说" * 4000 + "半句"),
        3: _ch(3, "短" * 500 + "。"),
    })
    # 第4章计划里有、磁盘上没有
    plan["chapters"].append({"chapterNumber": 4, "filePath": "04.md",
                             "status": "completed"})
    proj = _Proj(str(tmp_path), plan)
    with open(os.path.join(str(tmp_path), "05.md"), "w", encoding="utf-8") as f:
        f.write("当前诊断信息存在缺项，暂时无法可靠执行整章修订。\n请补发原文。")
    plan["chapters"].append({"chapterNumber": 5, "filePath": "05.md",
                             "status": "completed"})
    a = book_health.audit(proj)
    assert [x["ch"] for x in a["truncated"]] == [2]
    assert [x["ch"] for x in a["short"]] == [3]
    assert [x["ch"] for x in a["missing"]] == [4]
    assert [x["ch"] for x in a["meta"]] == [5]
    assert a["needs_work"] == [2, 3, 4, 5]


def test_repair_restores_complete_versions_and_lists_unrepairable(tmp_path):
    plan = _plan(str(tmp_path), {
        1: "当前诊断信息存在缺项，暂时无法可靠执行整章修订。\n请补发原文。",
        2: _ch(2, "旧" * 4000 + "半句"),
    })
    proj = _Proj(str(tmp_path), plan)
    versions.snapshot(str(tmp_path), 1, _ch(1, "原" * 3300 + "。"), label="完整稿")
    versions.snapshot(str(tmp_path), 2, _ch(2, "旧" * 4000 + "半句"), label="截断稿")
    rep = book_health.repair(proj)
    assert [r["ch"] for r in rep["repaired"]] == [1]
    assert rep["needs_finish"] == [2]
    body = open(os.path.join(str(tmp_path), "01.md"), encoding="utf-8").read()
    assert "原" * 100 in body
    assert plan["chapters"][0]["wordCount"] >= 3300
    assert any("版本库还原" in m for m in proj.logs)


def test_save_gate_rejects_bad_and_accepts_clean(tmp_path):
    plan = _plan(str(tmp_path), {1: _ch(1, "原" * 1200 + "。")})
    proj = _Proj(str(tmp_path), plan)
    fp = os.path.join(str(tmp_path), "01.md")

    r = book_health.save_chapter(proj, 1, "", kind="rewrite")
    assert not r["ok"] and "空" in r["reason"]
    r = book_health.save_chapter(proj, 1, "当前诊断信息存在缺项，请补发原文。",
                                 kind="rewrite")
    assert not r["ok"] and "说明文字" in r["reason"]
    r = book_health.save_chapter(proj, 1, _ch(1, "新" * 100), kind="rewrite",
                                 orig=open(fp, encoding="utf-8").read())
    assert not r["ok"] and ("塌陷" in r["reason"] or "说明文字" in r["reason"])
    r = book_health.save_chapter(proj, 1, _ch(1, "新" * 3200 + "半句"),
                                 kind="rewrite", orig=open(fp, encoding="utf-8").read())
    assert not r["ok"] and "结尾不完整" in r["reason"]

    r = book_health.save_chapter(proj, 1, _ch(1, "新" * 3200 + "。"),
                                 kind="rewrite", label="改稿前",
                                 orig=open(fp, encoding="utf-8").read())
    assert r["ok"] and r["words"] >= 3200
    assert "新" in open(fp, encoding="utf-8").read()
    assert plan["chapters"][0]["wordCount"] >= 3200
    assert versions.list_versions(str(tmp_path), 1)


def test_preflight_logs_and_reports(tmp_path):
    plan = _plan(str(tmp_path), {1: "当前诊断信息存在缺项，请补发原文。"})
    proj = _Proj(str(tmp_path), plan)
    versions.snapshot(str(tmp_path), 1, _ch(1, "原" * 3200 + "。"))
    out = book_health.preflight(proj, "测试")
    assert out["repair"]["repaired"]
    assert any("开工体检" in m for m in proj.logs)


def test_parallel_rewrite_runs_preflight(tmp_path, monkeypatch):
    plan = _plan(str(tmp_path), {})
    proj = _Proj(str(tmp_path), plan)
    calls = []
    monkeypatch.setattr(writer, "Project", lambda *_a, **_k: proj)
    monkeypatch.setattr(writer.book_health, "preflight",
                        lambda p, purpose="", target=None: calls.append(purpose) or {})
    monkeypatch.setattr(writer, "per_chapter_scores", lambda _p: [])
    out = writer.run_parallel_rewrite("k", chapters=[])
    assert calls and "低分改写" in calls[0]
    assert out["ok"] is True


def test_short_expand_queue_includes_truncated_chapter(tmp_path, monkeypatch):
    plan = _plan(str(tmp_path), {1: _ch(1, "半" * 4000 + "半句")})
    proj = _Proj(str(tmp_path), plan)
    monkeypatch.setattr(writer, "Project", lambda *_a, **_k: proj)
    monkeypatch.setattr(writer, "book_health", book_health)
    monkeypatch.setattr(writer.llm, "book", lambda *_a, **_k: None)
    seen = []
    monkeypatch.setattr(writer, "expand_short_chapter",
                        lambda _p, n, target_words=3000: seen.append(n)
                        or {"ch": n, "kept": True, "before_words": 4000,
                            "after_words": 4100})
    writer.run_short_expand("k", [1], 3000, workers=1)
    assert seen == [1]


def test_chief_exposes_health_check():
    assert "health_check" in chief.ACTIONS
    assert chief._ACTION_ALIASES["体检"] == "health_check"
    act = chief._norm_action({"action": "体检"})
    assert act["action"] == "health_check"


def test_final_review_word_pass_goes_through_write_gate(tmp_path, monkeypatch):
    from bot import final_review, workflow
    plan = _plan(str(tmp_path), {1: _ch(1, "原" * 500 + "。")})
    proj = _Proj(str(tmp_path), plan)
    fp = os.path.join(str(tmp_path), "01.md")
    before = open(fp, encoding="utf-8").read()

    monkeypatch.setattr(final_review.book_health, "save_chapter",
                        lambda *a, **k: {"ok": False, "reason": "候选稿结尾不完整（疑似截断）"})
    monkeypatch.setattr(writer, "_build_ctx", lambda _p, _c: {})
    monkeypatch.setattr(workflow, "continue_chapter",
                        lambda body, ctx, need_words=0: "续" * 3000 + "半句")
    monkeypatch.setattr(workflow, "expand_chapter",
                        lambda body, ctx, min_words=0, current_words=0: body)
    out = final_review._word_pass(proj, plan, "k")
    assert out["fixed"] == 0
    assert out["still"] == 1
    assert out["still_chapters"] == [1]
    assert open(fp, encoding="utf-8").read() == before

