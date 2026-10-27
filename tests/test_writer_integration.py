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


def test_empty_plan_rebuilds_instead_of_finishing(tmp_path, monkeypatch):
    """回归：规划线程被中断后计划里 0 章，「继续」必须重建规划，
    绝不能直接审校、成书 0 字、把书标记 completed（v2.7 E2E 实测踩坑）。"""
    pdir = tmp_path / "empty"
    pdir.mkdir()
    (pdir / "02-写作计划.json").write_text(json.dumps({
        "version": 1, "novelName": "空计划", "status": "planning",
        "chapters": [], "totalChapters": 2,
        "config": {"chapters": 2, "genre": "都市"},
    }, ensure_ascii=False), encoding="utf-8")
    proj = writer.Project(str(pdir), "empty")

    rebuilt = []

    def _fake_create(cfg, titles, pdir=None, name=None):
        rebuilt.append(name)
        plan = proj.plan()
        plan["chapters"] = [{"chapterNumber": i, "title": f"第{i}章",
                             "filePath": f"第{i:02d}章.md", "status": "completed",
                             "wordCount": 3000, "wordCountPass": True, "retryCount": 0}
                            for i in range(1, 3)]
        proj.save_plan(plan)
        return proj

    monkeypatch.setattr(writer, "create_project", _fake_create)
    finished = []
    monkeypatch.setattr(writer, "_validate_and_finish", lambda p: finished.append(p.name))

    plan = writer._ensure_writable_plan(proj)
    assert plan and len(plan["chapters"]) == 2
    assert rebuilt == ["空计划"]
    assert not finished   # 守卫路径不得触发完结
    monkeypatch.undo()    # 恢复真实的 _validate_and_finish
    writer._validate_and_finish(proj)
    assert proj.plan()["status"] == "completed"   # 有章节时正常完结不受影响


def test_validate_and_finish_refuses_zero_chapters(tmp_path):
    """0 章的书绝不完结：状态回 planning，日志留痕，不生成成书文件。"""
    pdir = tmp_path / "zero"
    pdir.mkdir()
    (pdir / "02-写作计划.json").write_text(json.dumps({
        "version": 1, "novelName": "零章", "status": "in_progress",
        "chapters": [], "config": {},
    }, ensure_ascii=False), encoding="utf-8")
    proj = writer.Project(str(pdir), "zero")
    writer._validate_and_finish(proj)
    assert proj.plan()["status"] == "planning"
    assert not list(pdir.glob("成书-*.md"))
