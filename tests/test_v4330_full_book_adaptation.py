"""v4.33.0 回归：小说 → AI 短剧必须是**全书改编**，不是 12 集残稿。

用户实拍：一本 100 章 / 25.8 万字的小说，转出来的剧本只有 12「场」，
合计不到 1 万字，第 11 场只有 42 字（模型输出中途截断的半句话），却
被标记成「已完成」。

根因（逐条对应下面的用例）：
* api_novel_to_drama 里 ``chs[:12]`` 硬截断，88% 原著凭空消失；
* 1 章 = 1 集 = 1「场」，且只塞 ``src_text[:9000]``；
* 项目里现成的 100 章摘要 / 故事圣经 / 人物档案 / 卷摘要完全没用；
* 只判 ``body.strip()`` 非空，残片照样落盘标 completed。
"""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SRC = "20260101-000000-长篇测试小说"
DRAMA = SRC + "-短剧"
CHAPTER_COUNT = 100

# 用户截图里第 11 集的真实残片：模型撞 max_tokens 后停在半句话
TRUNCATED_11 = "\n\n第11集 长老的密令\n\n[内 长老殿 夜]\n（特写）烛火骤暗。苍老的手指在扶手上"

GOOD = (
    "# 第1集：试炼\n\n"
    "**场1：内 长老殿 夜**\n\n（烛火骤暗，林野推门而入。）\n\n"
    "林野：密令在哪。\n长老：你先看清楚自己是谁。\n苏清柔：别过去。\n\n"
    "**场2：外 山道 晨**\n\n（远处兽吼，三人同时回头。）\n\n"
    "林野：那就从这里开始。\n"
) * 10


def _good_without_tail():
    """字数/场景/台词都够，但结尾是半句话——旧版会当成成品。"""
    return GOOD.rstrip().rstrip("。") + "苍老的手指在扶手上"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from bot import writer, script_writer, security, llm
    import app as appmod

    proot = tmp_path / "projects"
    proot.mkdir()
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(proot))
    monkeypatch.setattr(script_writer, "PROJECTS_DIR", str(proot))
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(llm, "book", lambda *a, **k: None, raising=False)
    appmod.PROJECT_INDEX.clear()
    appmod._ADAPT_RUNNING.clear()

    novel = proot / SRC
    novel.mkdir()
    chapters = []
    for n in range(1, CHAPTER_COUNT + 1):
        chapters.append({"chapterNumber": n, "title": f"第{n}章·试炼",
                         "status": "completed", "wordCount": 2500,
                         "filePath": f"第{n:03d}章.md"})
        (novel / f"第{n:03d}章.md").write_text(
            f"# 第{n}章·试炼\n\n（正文）林野走进第{n}座山谷。\n\n" + "情节推进。" * 500 + "\n\n章末钩子。",
            encoding="utf-8")
    (novel / "02-写作计划.json").write_text(
        json.dumps({"novelName": "长篇测试小说", "chapters": chapters,
                    "config": {}, "meta": {}}, ensure_ascii=False), encoding="utf-8")
    # 全书级素材：章摘要 + 故事圣经 + 人物档案 + 卷摘要
    with (novel / "21-章摘要.jsonl").open("w", encoding="utf-8") as f:
        for n in range(1, CHAPTER_COUNT + 1):
            f.write(json.dumps({"n": n, "title": f"第{n}章·试炼",
                                "summary": f"林野在第{n}座山谷收服巨兽，与苏清柔对峙。",
                                "beats": ["进入山谷", "遭遇巨兽", "收服成功"]},
                               ensure_ascii=False) + "\n")
    (novel / "09-故事圣经.md").write_text("世界观：灵根决定契约能力；主角林野无灵根。",
                                          encoding="utf-8")
    (novel / "00-人物档案.md").write_text("林野：主角。苏清柔：退婚未婚妻。长老：引路人。",
                                          encoding="utf-8")
    (novel / "20-卷摘要.md").write_text("第一卷：弃子觉醒；第二卷：收服十万巨兽。",
                                        encoding="utf-8")

    yield {"root": proot, "novel": novel, "client": TestClient(appmod.app)}
    appmod.PROJECT_INDEX.clear()
    appmod._ADAPT_RUNNING.clear()


def _plan(env):
    return json.loads((env["root"] / DRAMA / "剧本计划.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 分集蓝图

def test_all_100_chapters_are_grouped_without_loss():
    """旧版 chs[:12] 只覆盖 12 章；新蓝图必须覆盖全书 100 章且集号连续。"""
    from bot import drama_adapter as da

    chapters = [{"chapterNumber": n, "title": f"第{n}章"} for n in range(1, 101)]
    outline = da.group_chapters(chapters, per_episode=2)
    assert len(outline) == 50
    covered = [n for ep in outline for n in ep["chapters"]]
    assert covered == list(range(1, 101))
    assert [ep["no"] for ep in outline] == list(range(1, 51))
    assert outline[0]["chapters"] == [1, 2] and outline[-1]["chapters"] == [99, 100]


def test_target_episodes_splits_evenly_and_keeps_ending():
    """指定目标集数时也不能丢尾章（旧版会整段丢掉）。"""
    from bot import drama_adapter as da

    chapters = [{"chapterNumber": n} for n in range(1, 101)]
    for target in (12, 24, 60, 100, 120):
        outline = da.group_chapters(chapters, target_episodes=target)
        assert len(outline) <= target
        covered = [n for ep in outline for n in ep["chapters"]]
        assert covered == list(range(1, 101)), target


def test_book_digest_samples_whole_book_not_just_head():
    from bot import drama_adapter as da

    summaries = {n: {"title": f"第{n}章", "summary": "故事推进" * 40}
                 for n in range(1, 101)}
    digest = da.build_book_digest(summaries, budget=4000)
    assert "第1章" in digest and "第100章" in digest
    assert len(digest) <= 4000


# ---------------------------------------------------------------- 质量门禁

def test_42_char_truncated_episode_is_rejected():
    """用户截图里第 11 集那 42 字残片，必须被拦下而不是标 completed。"""
    from bot import drama_adapter as da

    ok, why = da.validate_episode(TRUNCATED_11)
    assert ok is False
    assert "42" in why or "截断" in why


def test_long_but_unfinished_sentence_is_rejected():
    """字数够但停在半句话（撞 max_tokens）也必须判不合格。"""
    from bot import drama_adapter as da

    ok, why = da.validate_episode(_good_without_tail())
    assert ok is False and "收束标点" in why


def test_unclosed_markup_is_rejected():
    from bot import drama_adapter as da

    ok, why = da.validate_episode(GOOD[:-2])
    assert ok is False
    assert "截断" in why


def test_complete_episode_passes():
    from bot import drama_adapter as da

    ok, why = da.validate_episode(GOOD)
    assert ok is True, why


def test_inspect_existing_body_flags_legacy_trash(env):
    from bot import drama_adapter as da

    fp = env["root"] / DRAMA / "04-正文" / "第011场.md"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(TRUNCATED_11, encoding="utf-8")
    assert da.inspect_existing_body(str(fp))[0] is False


# ---------------------------------------------------------------- 源文预算

def test_episode_source_covers_all_chapters_and_keeps_chapter_tail(env):
    """每集 2 章，两章都要给模型；长章也不能把章末钩子砍掉。"""
    from bot import drama_adapter as da
    import app as appmod

    proj = appmod._get_project(SRC)
    plan = proj.plan()
    rows = plan["chapters"][:2]
    text = da.episode_source_text(proj, rows, budget=6000)
    assert "【第1章" in text and "【第2章" in text
    assert len(text) > 4500          # 旧实现只给单章前 9000 字，这里两章全文都到位
    assert text.count("章末钩子") == 2

    # 预算收紧时按章均分，且每章首/尾都保住（旧实现一刀切只留开头）
    tight = da.episode_source_text(proj, rows, budget=1000)
    assert "【第1章" in tight and "【第2章" in tight
    assert tight.count("章末钩子") == 2
    assert len(tight) <= 1200


# ---------------------------------------------------------------- API 全书改编

def test_api_adapts_all_100_chapters_not_twelve(env, monkeypatch):
    """核心回归：/api/novel-to-drama 不再把 100 章截成 12 章。"""
    import app as appmod

    captured = {}
    monkeypatch.setattr(appmod, "_novel_to_drama_impl",
                        lambda *a, **k: captured.update(k=k, a=a))
    r = env["client"].post("/api/novel-to-drama", json={"key": SRC})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chapter_count"] == 100
    assert body["episode_count"] == 50
    assert "全书 100 章 → 50 集" in body["msg"]
    assert len(captured["k"]["outline"]) == 50
    assert len(captured["a"][2]) == 100
    for _ in range(50):
        if not appmod._adapt_busy(DRAMA):
            break
        time.sleep(0.02)


def test_api_density_and_selected_chapters_are_honoured(env, monkeypatch):
    import app as appmod

    captured = {}
    monkeypatch.setattr(appmod, "_novel_to_drama_impl",
                        lambda *a, **k: captured.update(k=k))
    r = env["client"].post("/api/novel-to-drama",
                           json={"key": SRC, "chapters_per_episode": 4,
                                 "chapters": list(range(1, 21))})
    assert r.status_code == 200, r.text
    assert r.json()["chapter_count"] == 20
    assert r.json()["episode_count"] == 5
    assert len(captured["k"]["outline"]) == 5
    for _ in range(50):
        if not appmod._adapt_busy(DRAMA):
            break
        time.sleep(0.02)


def test_existing_new_blueprint_is_not_silently_overwritten(env, monkeypatch):
    import app as appmod

    monkeypatch.setattr(appmod, "_novel_to_drama_impl", lambda *a, **k: None)
    d = env["root"] / DRAMA
    d.mkdir()
    (d / "剧本计划.json").write_text(json.dumps({
        "scriptName": "长篇测试小说·短剧", "scenes": [],
        "adaptation": {"version": 2}}, ensure_ascii=False), encoding="utf-8")
    r = env["client"].post("/api/novel-to-drama", json={"key": SRC})
    assert r.json().get("already_exists") is True
    assert "重新改编全书" in r.json()["msg"]


# ---------------------------------------------------------------- 逐集写作

def _patch_llm(monkeypatch, sequence):
    """按调用顺序返回剧本稿；创作圣经（planning）单独返回，不计入序列。"""
    from bot import llm

    calls = {"n": 0}

    def fake_chat(messages, *a, **k):
        if k.get("stage") == "planning":
            return "# 改编创作圣经\n\n" + "林野无灵根，苏清柔退婚，全书设定不可改。" * 30
        i = calls["n"]
        calls["n"] += 1
        return sequence[min(i, len(sequence) - 1)]

    monkeypatch.setattr(llm, "chat", fake_chat)
    return calls


def test_quality_gate_retries_and_only_writes_good_episode(env, monkeypatch):
    """第一稿是残片 → 重试 → 第二稿合格才落盘；绝不写 42 字残片。"""
    import app as appmod
    from bot import script_writer

    calls = _patch_llm(monkeypatch, [TRUNCATED_11, GOOD])
    monkeypatch.setattr(script_writer, "gen_storyboard",
                        lambda key: {"ok": True, "shots": 3, "scenes": 1})
    plan = json.loads((env["root"] / SRC / "02-写作计划.json").read_text(encoding="utf-8"))
    proj = appmod._get_project(SRC)
    appmod._novel_to_drama_impl(
        SRC, "short_drama", plan["chapters"][:2], DRAMA, plan, proj,
        outline=[{"no": 1, "chapters": [1, 2], "titles": ["第1章", "第2章"],
                  "title": "第1章"}],
        settings={"min_chars": 700, "max_attempts": 3})
    p = _plan(env)
    sc = p["scenes"][0]
    assert sc["status"] == "completed"
    assert sc["attempts"] == 2
    assert sc["wordCount"] > 700
    body = (env["root"] / DRAMA / "04-正文" / "第001场.md").read_text(encoding="utf-8")
    assert body == GOOD.strip()
    assert calls["n"] == 2
    assert (env["root"] / DRAMA / "00-创作圣经.md").exists()
    assert (env["root"] / DRAMA / "06-季弧与分集.md").exists()
    assert p["adaptedFrom"]["totalChapters"] == 2
    assert p["adaptation"]["version"] == 2


def test_persistent_trash_is_never_marked_completed(env, monkeypatch):
    """三次都拿残片 → 该集标待重跑、不留正文，且不误报全书完成。"""
    import app as appmod
    from bot import script_writer

    _patch_llm(monkeypatch, [TRUNCATED_11])
    monkeypatch.setattr(script_writer, "gen_storyboard", lambda key: {"ok": False})
    plan = json.loads((env["root"] / SRC / "02-写作计划.json").read_text(encoding="utf-8"))
    proj = appmod._get_project(SRC)
    res = appmod._novel_to_drama_impl(
        SRC, "short_drama", plan["chapters"][:2], DRAMA, plan, proj,
        outline=[{"no": 1, "chapters": [1, 2], "titles": ["第1章", "第2章"],
                  "title": "第1章"}],
        settings={"min_chars": 700, "max_attempts": 3})
    p = _plan(env)
    sc = p["scenes"][0]
    assert sc["status"] == "failed"
    assert not (env["root"] / DRAMA / "04-正文" / "第001场.md").exists()
    assert "截断" in sc["error"] or "下限" in sc["error"]
    assert p["status"] == "writing"
    assert res["pending"] == 1


def test_blueprint_is_written_into_plan_and_scene_list(env, monkeypatch):
    """100 章整书跑通后，计划里的蓝图/覆盖章号/分集清单都必须落盘。"""
    import app as appmod
    from bot import script_writer

    _patch_llm(monkeypatch, [GOOD])
    monkeypatch.setattr(script_writer, "gen_storyboard", lambda key: {"ok": True})
    plan = json.loads((env["root"] / SRC / "02-写作计划.json").read_text(encoding="utf-8"))
    proj = appmod._get_project(SRC)
    # 只跑 3 章（2 集），验证蓝图字段而非烧 50 集
    appmod._novel_to_drama_impl(
        SRC, "short_drama", plan["chapters"][:3], DRAMA, plan, proj,
        settings={"chapters_per_episode": 2})
    p = _plan(env)
    assert p["adaptedFrom"]["chapters"] == [1, 2, 3]
    assert p["adaptation"]["totalEpisodes"] == 2
    assert p["adaptation"]["blueprint"] == [
        {"no": 1, "chapters": [1, 2]}, {"no": 2, "chapters": [3]}]
    assert p["totalScenes"] == 2 and p["episodes"] == 2
    listing = (env["root"] / DRAMA / "03-场次清单.md").read_text(encoding="utf-8")
    assert "第1集" in listing and "第 1-2 章" in listing
    assert "已完成" in listing
    # 首列沿用「第N场」，常规剧本工作台的场次解析器仍能读出每集
    from bot import script_writer
    assert len(script_writer.parse_scenes(listing)) == 2


# ---------------------------------------------------------------- 旧剧本升级

def test_legacy_12_episode_drama_is_upgraded_to_full_book(env, monkeypatch):
    """用户桌面那部旧剧本点「继续」：自动升级全书蓝图，不再对着 12 集空转。"""
    import app as appmod

    d = env["root"] / DRAMA
    d.mkdir()
    (d / "04-正文").mkdir()
    for n in range(1, 13):
        (d / "04-正文" / f"第{n:03d}场.md").write_text(
            TRUNCATED_11 if n == 11 else GOOD, encoding="utf-8")
    scenes = [{"no": n, "status": "completed", "fileName": f"第{n:03d}场.md",
               "wordCount": 42 if n == 11 else 900} for n in range(1, 13)]
    (d / "剧本计划.json").write_text(json.dumps({
        "scriptName": "长篇测试小说·短剧", "scenes": scenes, "status": "completed",
        "adaptedFrom": {"key": SRC, "chapters": list(range(1, 13))},
        "meta": {"storyboard_done": False}}, ensure_ascii=False), encoding="utf-8")

    captured = {}
    monkeypatch.setattr(appmod, "_novel_to_drama_impl",
                        lambda *a, **k: captured.update(k=k, a=a))
    r = env["client"].post("/api/resume", json={"key": DRAMA})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert "升级为全书 50 集" in r.json()["msg"]
    for _ in range(50):
        if captured:
            break
        time.sleep(0.02)
    assert len(captured["k"]["outline"]) == 50
    assert captured["k"]["only_episodes"] is None
    assert len(captured["a"][2]) == 100
    for _ in range(50):
        if not appmod._adapt_busy(DRAMA):
            break
        time.sleep(0.02)


def test_legacy_bodies_are_archived_not_deleted(env):
    import app as appmod

    d = env["root"] / DRAMA
    d.mkdir()
    (d / "04-正文").mkdir()
    for n in range(1, 4):
        (d / "04-正文" / f"第{n:03d}场.md").write_text(TRUNCATED_11, encoding="utf-8")
    (d / "剧本计划.json").write_text(json.dumps({"scenes": []}), encoding="utf-8")
    moved = appmod._adapt_archive_all_bodies(str(d))
    assert moved == 3
    assert not (d / "04-正文" / "第001场.md").exists()
    assert (d / "04-正文" / "旧版备份" / "第002场.md").read_text(
        encoding="utf-8") == TRUNCATED_11
    appmod._adapt_backup_plan(str(d))
    assert (d / "剧本计划.json.旧版备份.json").exists()


# ---------------------------------------------------------------- 前端接线

def test_frontend_adapt_dialog_exposes_scope_and_density():
    root = Path(__file__).resolve().parents[1]
    rp = (root / "static/v2/js/components/rightpanel.js").read_text(encoding="utf-8")
    assert "chaptersPerEpisode" in rp
    assert "episodeCount" in rp
    assert "重新改编全书" in rp
    api_js = (root / "static/v2/js/core/api.js").read_text(encoding="utf-8")
    assert "novelToDrama" in api_js
