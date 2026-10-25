"""长篇内核接入写作引擎：章卡上下文、状态入库、超大规模折算。"""
import json

from bot import llm, longform, writer


def _make_book(tmp_path, total=6, config=None):
    pdir = tmp_path / "book"
    pdir.mkdir(exist_ok=True)
    plan = {
        "version": 1, "novelName": "接入测试", "totalChapters": total,
        "minWordsPerChapter": 20, "status": "pending", "writingMode": "serial",
        "config": config or {"chapters": total, "genre": "都市"},
        "chapters": [
            {"chapterNumber": i, "title": f"第{i}章", "filePath": f"第{i:02d}章.md",
             "status": "pending", "wordCount": None, "wordCountPass": None, "retryCount": 0}
            for i in range(1, total + 1)
        ],
    }
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (pdir / "01-大纲.md").write_text("# 大纲\n\n第1章 开局\n主角醒来。\n", encoding="utf-8")
    return writer.Project(str(pdir), "book")


def test_scale_presets_expand_to_chapters():
    sc = longform.resolve_scale({"scale": "3m"})
    assert sc["target_words"] >= 3_000_000
    assert sc["chapters"] >= 900
    assert sc["volumes"] >= 10


def test_build_ctx_uses_chapter_card(tmp_path):
    proj = _make_book(tmp_path)
    longform.ensure_plan(proj, proj.plan()["config"])
    longform.plan_window(proj, 1, 6)
    ctx = writer._build_ctx(proj, {"chapterNumber": 1, "title": "第1章"})
    assert ctx["longform_card"]["n"] == 1
    assert "本章章卡" in ctx["chapter_plan"]
    assert ctx["context_meta"]["budget"] > 0
    assert "plan_digest" in ctx


def test_write_chapter_indexes_state_and_cards(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "他推门进来，把伞收好。" * 20)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {})
    proj = _make_book(tmp_path, total=1, config={"chapters": 1, "genre": "都市"})
    longform.ensure_plan(proj, proj.plan()["config"])
    ch = proj.plan()["chapters"][0]
    writer._write_chapter(proj, ch, 20, 0)
    pdir = tmp_path / "book"
    assert (pdir / "18-状态库.json").exists()
    assert (pdir / "21-章摘要.jsonl").exists()
    assert proj.plan()["chapters"][0]["status"] == "completed"
    st = json.loads((pdir / "18-状态库.json").read_text(encoding="utf-8"))
    assert st["metrics"]["last_chapter"] == 1
    cards = [json.loads(x) for x in (pdir / "02d-章卡.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert cards[0]["status"] == "done"
