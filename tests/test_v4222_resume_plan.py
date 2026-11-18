"""v4.22.2 断点续跑 / 任务档案 / 长篇规划闸门 回归。

三个被用户反复指出的问题，各有一组守门用例：
  ① 「继续」不能重新开始——必须读存档，接着上次那件事、那次中断的位置跑；
  ② 长篇不能没有大纲就写——占位章卡必须先补真卡，补不出来宁可停；
  ③ 预览框复制不能带噪音——章题之前的前言/改写说明一律不算正文。
"""
import json

from fastapi.testclient import TestClient

import app as appmod
from bot import export as export_mod      # noqa: F401  (保持与主测试同一导入面)
from bot import journal, longform, planner, purity, writer

KEY = "book"


# ---------------------------------------------------------------- 夹具

def _proj(tmp_path, monkeypatch, total=6, done=2, cards=None):
    """造一个最小可跑项目：前 done 章已完成，其余 pending。"""
    pdir = tmp_path / KEY
    pdir.mkdir()
    chapters = []
    for n in range(1, total + 1):
        fp = f"第{n:03d}章.md"
        status = "completed" if n <= done else "pending"
        if n <= done:
            (pdir / fp).write_text(f"# 第{n:02d}章 标题{n}\n\n## 正文\n他关掉收音机。\n",
                                   encoding="utf-8")
        chapters.append({"chapterNumber": n, "title": f"标题{n}", "status": status,
                         "wordCount": 12 if n <= done else 0, "filePath": fp})
    json.dump({"novelName": "测试书", "status": "in_progress", "chapters": chapters,
               "totalChapters": total, "meta": {}, "config": {}},
              open(pdir / "02-写作计划.json", "w", encoding="utf-8"), ensure_ascii=False)
    json.dump({"scale": {"target_words": 300000, "chapters": total, "volumes": 1},
               "volumes": [{"no": 1, "title": "第一卷", "start_chapter": 1,
                            "end_chapter": total,
                            "arcs": [{"no": 1, "title": "第一弧", "start_chapter": 1,
                                      "end_chapter": total, "goal": "主角进京站稳脚跟"}]}]},
              open(pdir / "02c-总规划.json", "w", encoding="utf-8"), ensure_ascii=False)
    if cards:
        with open(pdir / "02d-章卡.jsonl", "w", encoding="utf-8") as f:
            for c in cards:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    return pdir


def _placeholder_card(n, arc=1):
    return {"n": n, "arc": arc, "volume": 1, "title": f"第{n}章", "status": "planned",
            "goal": f"承接「第一卷」，完成本弧的小高潮与章末钩子（第1–{n}章）；本章为弧内第{n}/6章",
            "conflict": "", "turn": "", "payoff": "", "cliffhanger": ""}


def _real_card(n, arc=1):
    return {"n": n, "arc": arc, "volume": 1, "title": f"真标题{n}", "status": "planned",
            "goal": "在宫宴上借刀除掉户部侍郎，同时保住自己的赘婿身份不暴露",
            "conflict": "侍郎手握账册当众发难，太子在侧伺机反咬",
            "turn": "账册被调包成假账", "payoff": "兑现第3章的密信",
            "cliffhanger": "太后忽然点名要他留下"}


# ---------------------------------------------------------------- 任务档案

def test_journal_lifecycle(tmp_path):
    pdir = str(tmp_path)
    t = journal.begin(pdir, "writing", "写《测试书》", params={"batch_size": "5"}, total=100)
    assert t["id"] and t["status"] == "running"
    journal.progress(pdir, done=40, current=41, stage="writing")
    cur = journal.current_task(pdir)
    assert cur["done"] == 40 and cur["current"] == 41 and cur["stage"] == "writing"

    txt = journal.recall_text(pdir)
    assert "进行中的任务" in txt and "第 41 章" in txt

    finished = journal.finish(pdir, "done", note="写完")
    assert finished["status"] == "done"
    assert journal.current_task(pdir) is None        # 结束后不再算「可续跑」
    assert journal.last_finished(pdir)["id"] == t["id"]


def test_journal_new_task_archives_old(tmp_path):
    pdir = str(tmp_path)
    journal.begin(pdir, "writing", "写")
    journal.begin(pdir, "rewrite", "重写")
    cur = journal.current_task(pdir)
    assert cur["kind"] == "rewrite"
    assert journal.last_finished(pdir, kinds=("writing",))["status"] == "superseded"


def test_journal_survives_broken_file(tmp_path):
    pdir = str(tmp_path)
    (tmp_path / journal.FILE).write_text("{ 不是 json", encoding="utf-8")
    assert journal.current_task(pdir) is None        # 坏档退化为空，不抛异常
    journal.begin(pdir, "writing", "写")
    assert journal.current_task(pdir)["kind"] == "writing"


def test_journal_memory_keeps_recent(tmp_path):
    pdir = str(tmp_path)
    for i in range(60):
        journal.remember(pdir, "user", f"指令{i}")
    mem = journal.memory(pdir, n=5)
    assert [m["text"] for m in mem] == [f"指令{i}" for i in range(55, 60)]


# ---------------------------------------------------------------- 章内草稿检查点

def test_draft_checkpoint_roundtrip(tmp_path, monkeypatch):
    pdir = _proj(tmp_path, monkeypatch, total=4, done=1)
    proj = appmod._get_project(KEY)
    assert writer.draft_info(proj, 2)["has"] is False

    writer._save_draft(proj, 2, "# 第02章 X\n\n## 正文\n他关掉收音机。\n")
    info = writer.draft_info(proj, 2)
    assert info["has"] and info["words"] > 0

    # 草稿同样过纯净域：脏内容不许落进检查点
    writer._save_draft(proj, 3, "# 第03章 Y\n\n## 正文\n他关掉收音机。\n\n## 修改说明\n加强细节。")
    raw = open(pdir / ".draft" / "第03章.partial.md", encoding="utf-8").read()
    assert "修改说明" not in raw

    writer._clear_draft(proj, 2)
    assert writer.draft_info(proj, 2)["has"] is False


def test_resume_plan_reports_draft_position(tmp_path, monkeypatch):
    """「继续」必须能说清：上次在做什么、从第几章第几字接着写。"""
    _proj(tmp_path, monkeypatch, total=10, done=3)
    proj = appmod._get_project(KEY)
    journal.begin(proj.path, "writing", "写《测试书》", total=10)
    journal.progress(proj.path, done=3, current=4, stage="writing")
    writer._save_draft(proj, 4, "# 第04章 X\n\n## 正文\n" + "雨。" * 400)

    rp = writer.resume_plan(KEY)
    assert rp["exists"] and rp["kind"] == "writing"
    assert rp["chapter"] == 4
    assert rp["draft_words"] > 0

    txt = writer._resume_text(KEY)
    assert "第 4 章" in txt and "草稿" in txt


def test_resume_task_honours_paused_rewrite(tmp_path, monkeypatch):
    """上次是批量重写停在半路 → 「继续」恢复重写剩下的章，而不是去写新章。"""
    _proj(tmp_path, monkeypatch, total=6, done=6)
    proj = appmod._get_project(KEY)
    journal.begin(proj.path, "rewrite", "重写未达标章节",
                  params={"chapters": [2, 3, 4, 5]}, total=4)
    journal.progress(proj.path, done=2, current=3, stage="rewriting")

    called = {}

    def _fake_rewrite(key, chapters=None, **kw):
        called["chapters"] = list(chapters or [])

    monkeypatch.setattr(writer, "run_parallel_rewrite", _fake_rewrite)
    r = writer.resume_task(KEY)
    assert r["ok"] and r["kind"] == "rewrite"
    assert "还剩 2 章" in r["text"]
    import time as _t
    for _ in range(50):
        if called:
            break
        _t.sleep(0.02)
    assert called.get("chapters") == [4, 5]          # 只跑没做完的


# ---------------------------------------------------------------- 规划闸门

def test_card_is_placeholder_detects_fallback_cards():
    assert longform.card_is_placeholder(_placeholder_card(41)) is True
    assert longform.card_is_placeholder({"goal": "", "conflict": "x"}) is True
    assert longform.card_is_placeholder({"goal": "推进", "conflict": ""}) is True
    assert longform.card_is_placeholder(_real_card(41)) is False


def test_placeholder_chapters_lists_dirty_ones(tmp_path, monkeypatch):
    _proj(tmp_path, monkeypatch, total=6, done=2,
          cards=[_real_card(3), _placeholder_card(4), _placeholder_card(5), _real_card(6)])
    proj = appmod._get_project(KEY)
    assert longform.placeholder_chapters(proj) == [4, 5]


def test_plan_chapter_now_without_llm_returns_empty(tmp_path, monkeypatch):
    """没模型时补不出真卡，必须返回空，而不是拿占位卡冒充成功。"""
    _proj(tmp_path, monkeypatch, total=6, done=2, cards=[_placeholder_card(3)])
    monkeypatch.setattr(longform, "_llm_on", lambda cfg: False)
    proj = appmod._get_project(KEY)
    assert longform.plan_chapter_now(proj, 3) == {}


def test_ensure_card_planned_blocks_when_unplannable(tmp_path, monkeypatch):
    """补不出真卡 → 返回 False（上层据此暂停），绝不静默硬写。"""
    _proj(tmp_path, monkeypatch, total=6, done=2, cards=[_placeholder_card(3)])
    monkeypatch.setattr(longform, "_llm_on", lambda cfg: False)
    proj = appmod._get_project(KEY)
    assert writer._ensure_card_planned(proj, 3, tries=1) is False


def test_ensure_card_planned_accepts_real_card(tmp_path, monkeypatch):
    _proj(tmp_path, monkeypatch, total=6, done=2, cards=[_real_card(3)])
    proj = appmod._get_project(KEY)
    assert writer._ensure_card_planned(proj, 3) is True


def test_ensure_card_planned_fills_from_llm(tmp_path, monkeypatch):
    """LLM 能补出来 → 章卡落盘为真卡，后续写作有据可依。"""
    _proj(tmp_path, monkeypatch, total=6, done=2, cards=[_placeholder_card(3)])
    monkeypatch.setattr(longform, "_llm_on", lambda cfg: True)

    def _fake_json(system, user, cfg, stage, max_tokens=2000, agent=None):
        return {"title": "宫宴发难", "goal": "在宫宴上借刀除掉户部侍郎",
                "conflict": "侍郎当众发难，太子伺机反咬", "turn": "账册被调包",
                "payoff": "兑现第3章密信", "cliffhanger": "太后点名留人"}

    monkeypatch.setattr(longform, "_llm_json", _fake_json)
    proj = appmod._get_project(KEY)
    assert writer._ensure_card_planned(proj, 3) is True
    cards = longform.load_cards(proj)
    assert longform.card_is_placeholder(cards[3]) is False
    assert "宫宴" in cards[3]["title"]


# ---------------------------------------------------------------- 接口口径

def test_chapter_api_exposes_rawtext_and_clean_text(tmp_path, monkeypatch):
    _proj(tmp_path, monkeypatch, total=3, done=1)
    pdir = tmp_path / KEY
    (pdir / "第002章.md").write_text(
        "这是一份针对《第02章 X》的最小改写。\n\n```markdown\n# 第02章 X\n\n"
        "## 章首引子\n雨。\n\n## 正文\n他关掉收音机。\n\n## 修改说明\n加强细节。\n```",
        encoding="utf-8")
    c = TestClient(appmod.app)
    j = c.get(f"/api/chapter?key={KEY}&n=2").json()
    for junk in ("最小改写", "修改说明", "```", "本章概要"):
        assert junk not in j["cleanText"], junk
    assert "他关掉收音机。" in j["cleanText"]
    assert j["rawText"] and "最小改写" in j["rawText"]   # 原文件仍可核对


def test_save_prose_mode_keeps_meta_and_drops_pollution(tmp_path, monkeypatch):
    """编辑器只编辑正文时：保存后概要/备注保留，历史污染丢失。"""
    _proj(tmp_path, monkeypatch, total=3, done=1)
    pdir = tmp_path / KEY
    old = ("这是一份针对《第02章 X》的最小改写。\n\n# 第02章 X\n\n"
           "## 本章概要\n周望载到红雨衣。\n\n## 章首引子\n雨。\n\n## 正文\n旧正文。\n\n"
           "## 章节备注\n下章回收车票。\n\n## 审核评分\n综合 86 分。")
    (pdir / "第002章.md").write_text(old, encoding="utf-8")

    c = TestClient(appmod.app)
    r = c.put("/api/chapter", json={"key": KEY, "n": 2, "mode": "prose",
                                    "text": "# 第02章 X\n\n## 章首引子\n雨。\n\n## 正文\n新正文。"})
    assert r.status_code == 200, r.text
    saved = (pdir / "第002章.md").read_text(encoding="utf-8")
    assert "新正文。" in saved
    assert "## 本章概要" in saved and "## 章节备注" in saved    # 元信息保住了
    assert "最小改写" not in saved and "审核评分" not in saved   # 污染没回来


def test_save_legacy_mode_sanitizes(tmp_path, monkeypatch):
    """旧客户端不带 mode → 按整文件处理，但必须去污染。"""
    _proj(tmp_path, monkeypatch, total=3, done=1)
    pdir = tmp_path / KEY
    c = TestClient(appmod.app)
    r = c.put("/api/chapter", json={
        "key": KEY, "n": 2,
        "text": "这是一份针对《第02章 X》的最小改写。\n\n# 第02章 X\n\n## 正文\n新正文。\n\n"
                "## 修改说明\n加强细节。"})
    assert r.status_code == 200, r.text
    saved = (pdir / "第002章.md").read_text(encoding="utf-8")
    assert "新正文。" in saved
    assert "最小改写" not in saved and "修改说明" not in saved


# ---------------------------------------------------------------- 理解层

def test_planner_context_carries_memory_and_draft(tmp_path, monkeypatch):
    """理解层必须看到「上次在做什么」与章内草稿，否则「继续」只能瞎猜。"""
    _proj(tmp_path, monkeypatch, total=10, done=3)
    proj = appmod._get_project(KEY)
    journal.begin(proj.path, "writing", "写《测试书》", total=10)
    journal.progress(proj.path, done=3, current=4, stage="writing")
    journal.remember(proj.path, "system", "上游 502，写作中断")
    writer._save_draft(proj, 4, "# 第04章 X\n\n## 正文\n" + "雨。" * 400)

    ctx = planner.build_context(proj, proj.status(), KEY)
    assert ctx["draft"]["has"] is True
    text = planner._ctx_text(ctx)
    assert "项目记忆" in text
    assert "上游 502" in text
    assert "草稿" in text


def test_planner_action_resume_task_registered():
    assert "resume_task" in planner.ACTIONS
    assert "继续" in planner.ACTIONS["resume_task"]["desc"]


# ---------------------------------------------------------------- 纯净域补充

def test_lead_preamble_is_not_body():
    raw = ("这是一份针对《第09章 黑纹共振》的最小改写。\n**主要修改逻辑：**\n"
           "1.  **去重高频词**：将“没有”替换为“缺失”。\n\n"
           "```markdown\n# 第09章 黑纹共振\n\n## 章首引子\n雨。\n\n## 正文\n他关掉收音机。\n```")
    ct = purity.chapter_text(raw)
    assert ct.startswith("# 第09章 黑纹共振")
    for junk in ("最小改写", "主要修改逻辑", "去重高频词", "```"):
        assert junk not in ct, junk
    # 章题不再被误删（旧实现会丢掉非首行的章题）
    assert purity.chapter_title(raw) == "第09章 黑纹共振"

    san = purity.sanitize_chapter(raw)
    assert "最小改写" not in san and "第09章" in san


def test_merge_meta_keeps_summary_and_notes():
    raw = ("# 第02章 X\n\n## 本章概要\n周望载到红雨衣。\n\n## 正文\n旧。\n\n"
           "## 章节备注\n下章回收车票。\n\n## 审核评分\n86 分。")
    out = purity.merge_meta("# 第02章 X\n\n## 正文\n新正文。", raw)
    assert "新正文。" in out and "## 本章概要" in out and "## 章节备注" in out
    assert "审核评分" not in out


# ---------------------------------------------------------------- 正文契约标记

MARKED = """## 本章概要
周望载到红雨衣，留下旧车票。

=== 一幕·YIMU 神笔工作台·开篇 ===
## 章首引子

雨是从傍晚开始下的。

## 正文

他关掉收音机。

=== 一幕·YIMU 神笔工作台·结尾 ===

## 章节备注
下章回收车票。

## 修改说明
本次加强了环境描写，弱化对话比例。综合评分 86 分。
"""


def test_marked_block_defines_body():
    """框内=正文，框外一律不算——这是最强的一条判定，不依赖任何词表。"""
    inner = purity.chapter_body(MARKED)
    assert "他关掉收音机。" in inner and "雨是从傍晚开始下的。" in inner
    for junk in ("本章概要", "章节备注", "修改说明", "综合评分", "神笔工作台"):
        assert junk not in inner, junk

    ct = purity.chapter_text(MARKED, fallback_title="第02章 旧车票")
    assert ct.startswith("# 第02章 旧车票")     # 文件无题头时用计划章名兜底
    assert "修改说明" not in ct


def test_marked_block_excludes_arbitrary_outside_text():
    """框外写什么都不能进正文，哪怕它长得像正文。"""
    raw = ("=== 一幕·YIMU 神笔工作台·开篇 ===\n## 正文\n他在雨里站了很久。\n"
           "=== 一幕·YIMU 神笔工作台·结尾 ===\n"
           "这段是在框外多写的正文，本不该出现。")
    body = purity.chapter_body(raw)
    assert "他在雨里站着" not in body and "他在雨里站了很久。" in body
    assert "框外多写" not in body


def test_marker_tolerates_decoration():
    for open_line in ("=== 一幕·YIMU 神笔工作台·开篇 ===",
                      "--- 一幕·YIMU 神笔工作台·开篇 ---",
                      "【一幕·YIMU 神笔工作台·开篇】",
                      "## 一幕·YIMU 神笔工作台·开篇",
                      "一幕·YIMU·开篇"):
        raw = f"{open_line}\n## 正文\n他关掉收音机。\n=== 一幕·YIMU 神笔工作台·结尾 ==="
        assert "他关掉收音机。" in purity.chapter_body(raw), open_line


def test_marker_open_only_takes_rest():
    raw = "前言说明。\n=== 一幕·YIMU 神笔工作台·开篇 ===\n## 正文\n他关掉收音机。"
    body = purity.chapter_body(raw)
    assert "他关掉收音机。" in body and "前言说明" not in body


def test_sanitize_keeps_meta_but_drops_marks():
    """工作文件要保留概要与备注，但契约标记本身不该留在盘上。"""
    san = purity.sanitize_chapter(MARKED)
    assert "## 本章概要" in san and "## 章节备注" in san
    assert "他关掉收音机。" in san
    assert "神笔工作台" not in san
    assert "修改说明" not in san and "综合评分" not in san


def test_marked_roundtrip_is_idempotent():
    once = purity.sanitize_chapter(MARKED)
    twice = purity.sanitize_chapter(once)
    assert once == twice
