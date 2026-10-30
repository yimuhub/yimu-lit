"""v2.1 A+B：新建向导的 schema 事实源与 /api/create 按文体分发。

原则：向导里问到的每个字段，都必须落到目标引擎真正读取的 cfg key 上；
纯说明性字段只能是 note/preview。本文件把这条约束写成门禁（ALLOWED_KEYS），
新增字段若不注明落点，测试直接失败。

LLM 由 tests/conftest.py 全局 mock；建项目的后台线程在这里换成捕获 cfg 的替身。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import medium as medium_mod, poetry_writer, prose_writer, script_writer, security, writer

client = TestClient(appmod.app)

# 每个字段 key 的落点：引擎读的 cfg key / 前端专用 / 接口层参数
ALLOWED_KEYS = {
    "novel": {
        # workflow/skillkit 读 cfg.genre，platforms.find 读 platform，longform 读 scale+total_chapters
        "platform", "genre", "genre_key", "tone", "perspective", "mode",
        "title", "idea", "scale", "total_chapters", "min_words_per_chapter",
        "sells", "style_preset", "style_id",
        "agents",   # v4.13.0：向导选择的专属 Agent（写入 config.agents）
        "batch_size",   # v4.15.0：批量写作（写N章/场后停在当前进度）
        "penname",   # v4.15.1：作者笔名（写入 plan.config.penname）
    },
    "script": {
        # script_writer.create_project 读 form/entryPath/idea/notes/scenes/pages/episodes/title
        # 与时代设定（setting_era/setting_world → meta.setting，贯穿 S0–S5）；
        # genre 与 format_spec 在 app._create_script_project 里折进 notes；
        # *_scale 只是前端选中态的 token 槽位，API 层按白名单丢弃
        "form", "entryPath", "genre", "title", "idea",
        "episodes_scale", "scenes_scale", "pages_scale",
        "notes", "format_spec", "style_id", "setting_era", "setting_world",
        "agents",   # v4.13.0：向导选择的专属 Agent（写入 config.agents）
        "batch_size",   # v4.15.0：批量写作（写N章/场后停在当前进度）
        "penname",   # v4.15.1：作者笔名（写入 plan.config.penname）
    },
    "poetry": {
        # poetry_writer 读 form/title/idea/maxRounds/cipai/metrical_check/with_notes/with_translation；
        # emotion 在 app._create_poetry_project 里折进 idea
        "form", "cipai", "maxRounds", "title", "idea", "emotion",
        "metrical_check", "with_notes", "with_translation", "style_id",
        "agents",   # v4.13.0：向导选择的专属 Agent（写入 config.agents）
        "penname",   # v4.15.1：作者笔名（写入 plan.config.penname）
    },
    "prose": {
        # prose_writer 读 genre/operation/sourceText/title/idea + audience/purpose/tone/
        # target_words/slop_depth/fact_check（brief、素材与去味阶段消费）
        "genre", "operation", "title", "idea", "sourceText", "audience", "purpose",
        "tone", "words", "target_words", "slop_depth", "fact_check", "style_id",
        "agents",   # v4.13.0：向导选择的专属 Agent（写入 config.agents）
        "penname",   # v4.15.1：作者笔名（写入 plan.config.penname）
    },
    "thesis": {
        # v4.6.0：thesis_writer 读 degree/field/title/idea/target_words/cite_style/humanize
        "degree", "field", "title", "idea", "target_words", "cite_style", "humanize", "style_id",
        "words_scale",   # scale 的 token 槽位（数字落点是 target_words）
        "agents",   # v4.13.0：向导选择的专属 Agent（写入 config.agents）
        "batch_size",   # v4.15.0：批量写作（写N章/场后停在当前进度）
        "penname",   # v4.15.1：作者笔名（写入 plan.config.penname）
    },
    "children": {
        # v4.10.0：children_writer 读 ctype/age/title/idea/target_words/values_note/with_interactive/with_illustration
        "ctype", "age", "title", "idea", "target_words", "values_note",
        "with_interactive", "with_illustration", "style_id",
        "words_scale",   # scale 的 token 槽位（数字落点是 target_words）
        "agents",   # v4.13.0：向导选择的专属 Agent（写入 config.agents）
        "batch_size",   # v4.15.0：批量写作（写N章/场后停在当前进度）
        "penname",   # v4.15.1：作者笔名（写入 plan.config.penname）
    },
    "news": {
        # v4.11.0：news_writer 读 ntype/org/title/idea/cite_style/with_versions
        "ntype", "org", "title", "idea", "cite_style", "with_versions", "style_id",
        "agents",   # v4.13.0：向导选择的专属 Agent（写入 config.agents）
        "batch_size",   # v4.15.0：批量写作（写N章/场后停在当前进度）
        "penname",   # v4.15.1：作者笔名（写入 plan.config.penname）
    },
    "copy": {
        # v4.12.0：copy_writer 读 ktype/platform/title/idea/duration_secs/tone/with_hooks
        "ktype", "platform", "title", "idea", "duration_secs", "tone", "with_hooks", "style_id",
        "duration",   # scale 的 token 槽位（数字落点是 duration_secs）
        "agents",   # v4.13.0：向导选择的专属 Agent（写入 config.agents）
        "batch_size",   # v4.15.0：批量写作（写N章/场后停在当前进度）
        "penname",   # v4.15.1：作者笔名（写入 plan.config.penname）
    },
    "other": {
        # v4.13.0：other_writer 读 template/audience/title/idea/target_words/tone
        "template", "audience", "title", "idea", "tone", "style_id",
        "words_scale",   # scale 的 token 槽位（数字落点是 target_words）
        "agents",   # v4.13.0：向导选择的专属 Agent（写入 config.agents）
        "batch_size",   # v4.15.0：批量写作（写N章/场后停在当前进度）
        "penname",   # v4.15.1：作者笔名（写入 plan.config.penname）
    },
}

# static/v2/js/views/wizard.js 的 controlFor() 支持的类型；schema 用了别的就渲染不出来
RENDERED_TYPES = {"select", "radio", "cards", "scale", "number", "text", "textarea",
                  "chips", "toggle", "note", "preview"}


def _fields(payload=None):
    return medium_mod.wizard_payload(payload or {})


def _all_fields(med_sections):
    for group in ("brief", "basic", "style"):
        for f in med_sections[group]:
            yield group, f


# ---------- A1：schema 事实源 ----------

def test_wizard_schema_covers_four_mediums():
    sections = _fields()
    assert {"novel", "script", "poetry", "prose", "thesis"} <= set(sections)
    for med in medium_mod.MEDIUMS:
        assert sections[med]["style_title"], med
        for group in ("brief", "basic", "style"):
            assert isinstance(sections[med][group], list), (med, group)


def test_schema_field_types_are_renderable():
    for med, sections in _fields().items():
        for group, f in _all_fields(sections):
            assert f["type"] in RENDERED_TYPES, (med, group, f.get("key"), f["type"])
            assert f["key"] and f["label"], (med, group, f)
            assert not f.get("options_from"), f"options_from 应在 wizard_payload 里展开：{f}"


def test_schema_keys_landed_on_engine_cfg():
    """新增字段必须先在 ALLOWED_KEYS 里登记落点，否则视为装饰性字段。"""
    for med, sections in _fields().items():
        for _, f in _all_fields(sections):
            if f["type"] in ("note", "preview"):
                continue
            assert f["key"] in ALLOWED_KEYS[med], f"{med}.{f['key']} 没有引擎落点"


def test_conditional_fields_point_at_real_keys():
    for med, sections in _fields().items():
        keys = {f["key"] for _, f in _all_fields(sections)}
        for _, f in _all_fields(sections):
            when = f.get("when")
            if not when:
                continue
            assert when["field"] in keys, f"{med}.{f['key']} 的 when 指向不存在的 {when['field']}"
            assert when.get("in"), f"{med}.{f['key']} 的 when 缺 in"


def test_scale_fields_never_collide_with_their_number_key():
    """scale 的 token 槽位与数字落点同名时，前端值袋会互相覆盖（选中态丢失、校验误报越界）。"""
    for med, sections in _fields().items():
        for _, f in _all_fields(sections):
            if f["type"] != "scale":
                continue
            assert f.get("custom_key"), f"{med}.{f['key']} 的 scale 缺 custom_key"
            assert f["custom_key"] != f["key"], f"{med}.{f['key']} token 与数字同名"
            assert any("fill" in o for o in (f.get("options") or [])), f"{med}.{f['key']} 快捷档没有 fill"


def test_novel_schema_engine_keys():
    basic = {f["key"]: f for f in _fields()["novel"]["basic"]}
    assert basic["scale"]["custom_key"] == "total_chapters"
    assert basic["min_words_per_chapter"]["default"] == 3000
    assert basic["min_words_per_chapter"]["min"] <= 3000 <= basic["min_words_per_chapter"]["max"]


def test_novel_genre_options_carry_craft_key():
    vocab = {"genres": ["悬疑/刑侦", "都市", "仙侠", "系统流"]}
    opts = medium_mod.novel_genre_options(vocab)
    by_value = {o["value"]: o["key"] for o in opts}
    assert by_value["悬疑/刑侦"] == "mystery"
    assert by_value["都市"] == "urban"
    assert by_value["系统流"] == "_index"      # 无 craft 文件：显式落到通用，不假装覆盖
    assert "仙侠" in by_value                  # craft 文件里有但词表里没有 → 补齐
    assert by_value["仙侠"] == "xianxia"


# ---------- B：/api/create 按文体分发 ----------

@pytest.fixture
def captured_cfg(monkeypatch):
    """拦住三条后台流水线，只记录 cfg（不建目录、不烧 LLM）。"""
    box = {}

    def _stub(engine, name):
        def _fn(cfg, base_key=""):
            box[name] = {"cfg": cfg, "key": base_key}
            raise RuntimeError("测试替身：不真跑流水线")   # 后台线程会吞掉，用于确认已进引擎
        return _fn

    monkeypatch.setattr(script_writer, "create_project", _stub(script_writer, "script"))
    monkeypatch.setattr(poetry_writer, "create_project", _stub(poetry_writer, "poetry"))
    monkeypatch.setattr(prose_writer, "create_project", _stub(prose_writer, "prose"))
    return box


def test_dispatch_creators_cover_three_non_novel_mediums():
    assert {"script", "poetry", "prose", "thesis"} <= set(appmod._MEDIUM_CREATORS)


def test_dispatch_script(captured_cfg):
    r = client.post("/api/create", json={
        "medium": "script", "form": "series", "entryPath": "idea", "genre": "悬疑/犯罪",
        "title": "长夜", "idea": "一桩旧案",
        "episodes_scale": "e16", "episodes": 16,
        "scenes_scale": "s45", "scenes": 45,
        "pages_scale": "p120", "pages": 120,
        "notes": "克制一点", "format_spec": ["标准场景头", "台词居中"],
        "provider_id": "p1", "model": "m1"})
    assert r.status_code == 200
    body = r.json()
    assert body["medium"] == "script" and body["key"].startswith("s_") and body["async"]
    cfg = None
    for _ in range(50):
        if "script" in captured_cfg:
            cfg = captured_cfg["script"]["cfg"]
            break
    assert cfg is not None, "后台线程未进入剧本引擎"
    assert cfg["form"] == "series" and cfg["episodes"] == 16
    assert cfg["scenes"] == 45 and cfg["pages"] == 120
    assert not any(k.endswith("_scale") for k in cfg), "token 槽位不该进引擎"
    assert "悬疑/犯罪" in cfg["notes"] and "标准场景头" in cfg["notes"]
    assert cfg["defaults"]["provider_id"] == "p1"


def test_dispatch_poetry_maps_rounds_and_folds_cipai(captured_cfg):
    r = client.post("/api/create", json={
        "medium": "poetry", "form": "ci", "cipai": "水调歌头", "emotion": "离别后的自省",
        "title": "无题", "idea": "秋月与旧城", "maxRounds": 1,
        "metrical_check": False, "with_notes": True, "with_translation": False})
    assert r.status_code == 200 and r.json()["key"].startswith("p_")
    for _ in range(50):
        if "poetry" in captured_cfg:
            break
    cfg = captured_cfg["poetry"]["cfg"]
    assert cfg["maxRounds"] == 1                    # 旧 payload 里的 rounds 已改名 maxRounds
    assert "水调歌头" in cfg["idea"] and "离别后的自省" in cfg["idea"]
    assert cfg["metrical_check"] is False and cfg["with_notes"] is True


def test_dispatch_poetry_clamps_rounds(captured_cfg):
    client.post("/api/create", json={"medium": "poetry", "idea": "x", "maxRounds": 99})
    for _ in range(50):
        if "poetry" in captured_cfg:
            break
    assert captured_cfg["poetry"]["cfg"]["maxRounds"] == 5


def test_dispatch_poetry_accepts_legacy_rounds_key(captured_cfg):
    client.post("/api/create", json={"medium": "poetry", "idea": "x", "rounds": 2})
    for _ in range(50):
        if "poetry" in captured_cfg:
            break
    assert captured_cfg["poetry"]["cfg"]["maxRounds"] == 2


def test_dispatch_prose_passes_dimension_keys(captured_cfg):
    r = client.post("/api/create", json={
        "medium": "prose", "genre": "shuqing", "operation": "write", "title": "灯下",
        "idea": "写外婆的煤油灯", "audience": "平台订阅读者", "purpose": "记录与纪念",
        "tone": "克制", "words": "w2000", "target_words": 2000,
        "slop_depth": "deep", "fact_check": False})
    assert r.status_code == 200 and r.json()["key"].startswith("e_")
    for _ in range(50):
        if "prose" in captured_cfg:
            break
    cfg = captured_cfg["prose"]["cfg"]
    for k, v in (("genre", "shuqing"), ("operation", "write"), ("audience", "平台订阅读者"),
                 ("purpose", "记录与纪念"), ("tone", "克制"), ("target_words", 2000),
                 ("slop_depth", "deep"), ("fact_check", False)):
        assert cfg[k] == v, k
    assert "words" not in cfg                        # 只留数字落点，不留规模 token


def test_legacy_medium_endpoints_share_the_path(captured_cfg):
    """旧入口仍在，且与 /api/create 走同一套 cfg 组装。"""
    r = client.post("/api/poetry/create", json={"form": "qijue", "idea": "雪夜", "rounds": 3})
    assert r.json()["medium"] == "poetry"
    for _ in range(50):
        if "poetry" in captured_cfg:
            break
    assert captured_cfg["poetry"]["cfg"]["maxRounds"] == 3


def test_unknown_medium_falls_back_to_novel(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    monkeypatch.setattr(appmod.writer, "create_project",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("停")))
    monkeypatch.setattr(appmod.workflow, "fill_defaults", lambda cfg: cfg)
    r = client.post("/api/create", json={"medium": "not-a-medium", "title": "占位", "idea": "x"})
    assert r.status_code == 200
    assert not r.json()["key"].startswith(("s_", "p_", "e_"))   # 回落小说：时间戳前缀，不占文体前缀


def test_novel_payload_writes_min_words_per_chapter(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    monkeypatch.setattr(appmod.workflow, "fill_defaults", lambda cfg: cfg)
    monkeypatch.setattr(writer, "create_project", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("停")))
    r = client.post("/api/create", json={"medium": "novel", "title": "测试书", "idea": "x",
                                         "min_words_per_chapter": 2200, "total_chapters": 150,
                                         "genre": "悬疑/刑侦", "genre_key": "mystery"})
    assert r.status_code == 200
    key = r.json()["key"]
    plan = json.loads((tmp_path / key / "02-写作计划.json").read_text(encoding="utf-8"))
    assert plan["minWordsPerChapter"] == 2200
    assert plan["totalChapters"] == 150
    assert plan["config"]["genre_key"] == "mystery"


def test_onboarding_ships_by_medium(monkeypatch):
    """wizard_payload 必须随 /api/onboarding 下发，前端才有 schema。"""
    monkeypatch.setattr(security, "AUTH_ENABLED", False)
    d = client.get("/api/onboarding").json()
    assert set(d["by_medium"]) == set(medium_mod.MEDIUMS)
    assert d["by_medium"]["script"]["brief"][0]["options"], "剧本体式选项应带词表"


# ---------- B：引擎侧真正消费这些 key ----------

@pytest.fixture
def prompts(monkeypatch):
    """抓提示词：llm.chat 收 msgs，返回空串（引擎走通但不产文）。"""
    box = []

    def _chat(msgs, *a, **k):
        box.append(" ".join(m.get("content", "") for m in msgs))
        return ""

    monkeypatch.setattr(appmod.llm, "chat", _chat, raising=False)
    monkeypatch.setattr(poetry_writer.llm, "chat", _chat)
    monkeypatch.setattr(prose_writer.llm, "chat", _chat)
    return box


def test_poetry_draft_names_the_cipai(prompts):
    poetry_writer.gen_draft({"form": "ci", "cipai": "水调歌头"}, "规划")
    assert "水调歌头" in prompts[-1], "词牌没进起草提示词 = 装饰字段"


def test_poetry_draft_without_cipai_stays_generic(prompts):
    poetry_writer.gen_draft({"form": "ci"}, "规划")
    assert "按所选词牌" in prompts[-1]


def test_prose_brief_uses_given_dimensions(prompts):
    prose_writer.gen_brief({"genre": "suibi", "idea": "写外婆的煤油灯",
                            "audience": "平台订阅读者", "purpose": "记录与纪念",
                            "tone": "克制", "target_words": 2000})
    p = prompts[-1]
    for frag in ("受众：平台订阅读者", "目的：记录与纪念", "语气：克制", "目标字数：约 2000 字"):
        assert frag in p, frag
    assert "不要再标「待确认」" in p


def test_prose_material_drops_fact_section(prompts):
    prose_writer.gen_material({"genre": "suibi", "fact_check": False}, "brief")
    assert "事实与来源" not in prompts[-1]
    prose_writer.gen_material({"genre": "suibi"}, "brief")
    assert "事实与来源" in prompts[-1]


def test_prose_slop_depth_controls_rounds(monkeypatch):
    calls = {"n": 0}

    def _scan(text, *a, **k):
        return {"verdict": "review", "findings": [{"severity": "blocking"}],
                "stats": {"blocking": 1}, "summary": "blocking 1 处"}

    def _deslop(text, rep, cfg):
        calls["n"] += 1
        return text + "改"

    monkeypatch.setattr(prose_writer.slop_checker, "scan", _scan)
    monkeypatch.setattr(prose_writer.slop_checker, "report_yaml", lambda rep: "yaml")
    monkeypatch.setattr(prose_writer, "gen_deslop", _deslop)

    prose_writer.slop_pass("原稿", {"slop_depth": "light"})
    assert calls["n"] == 0, "light 只出报告，不改稿"
    prose_writer.slop_pass("原稿", {"slop_depth": "standard"})
    assert calls["n"] == 1
    prose_writer.slop_pass("原稿", {"slop_depth": "deep"})
    assert calls["n"] == 3, "deep 再跑一轮（累计 3 次）"


def test_poetry_project_honours_check_flag_and_extras(monkeypatch, tmp_path):
    monkeypatch.setattr(poetry_writer, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(poetry_writer.llm, "chat",
                        lambda *a, **k: "一首诗\n第二行", raising=False)
    monkeypatch.setattr(poetry_writer.llm, "chat_json", lambda *a, **k: {}, raising=False)
    poetry_writer.create_project(
        {"title": "测试诗", "form": "modern", "idea": "雪", "maxRounds": 2,
         "with_notes": True, "with_translation": True}, base_key="p_flag")
    plan = json.loads((tmp_path / "p_flag" / poetry_writer.F_PLAN).read_text(encoding="utf-8"))
    final = (tmp_path / "p_flag" / poetry_writer.F_FINAL).read_text(encoding="utf-8")
    assert plan["meta"]["rounds"] == 1, "现代诗一轮校验后退出（保持旧行为）"
    assert "创作手记" in final and "白话译文" in final

    poetry_writer.create_project(
        {"title": "测试诗2", "form": "qijue", "idea": "雪", "maxRounds": 1,
         "metrical_check": False}, base_key="p_off")
    plan2 = json.loads((tmp_path / "p_off" / poetry_writer.F_PLAN).read_text(encoding="utf-8"))
    assert plan2["meta"]["verdict"] == "skipped"
    assert plan2["meta"]["rounds"] == 0, "关闭格律校验后不应进 K/R 轮"
    assert "创作手记" not in (tmp_path / "p_off" / poetry_writer.F_FINAL).read_text(encoding="utf-8")

