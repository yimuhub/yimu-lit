"""v5.3.0 提示词全开放体系回归（bot/prompts_store.py + bot/webprompts.py + 六个接线模块）。

覆盖：md 解析（槽/温度/变量/围栏）、两层目录回退（用户层 → 内置层 → 代码默认）、
温度覆盖、指纹升级判定与「采用新版」、恢复默认、freeze/thaw 批量快照、
内置 md 与代码默认逐字一致、契约标记完整性（改坏 md 后 split_marked/闸门仍兜底）、
selection_ai 与 ghost 走 prompts_store 后输出不变、API 契约。

红线：提示词可编辑，但出口闸门一行不动——用户把系统提示词改坏时，
产出仍会被 purity/闸门拦住（本文件给了确定性证明）。
所有用例不连网（conftest 已 mock llm）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot import final_review, ghost, longform, prompts_store, purity, selection_ai, webprompts, workflow

# ---------------------------------------------------------------- 工具


@pytest.fixture()
def clean_layers(tmp_path, monkeypatch):
    """把两层目录都指到临时目录（内置层先复制真实 prompts/ 便于覆盖测试）。"""
    import shutil

    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    builtin.mkdir()
    user.mkdir()
    real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
    for fn in os.listdir(real):
        if fn.endswith(".md"):
            shutil.copy(os.path.join(real, fn), builtin / fn)
    monkeypatch.setattr(prompts_store, "_builtin_dir", lambda: str(builtin))
    monkeypatch.setattr(prompts_store, "USER_DIR", str(user))
    return {"builtin": str(builtin), "user": str(user)}


def write_user(group: str, raw: str) -> str:
    os.makedirs(prompts_store.USER_DIR, exist_ok=True)
    p = prompts_store.user_path(group)
    with open(p, "w", encoding="utf-8") as f:
        f.write(raw)
    return p


def write_builtin(group: str, raw: str) -> str:
    p = prompts_store.builtin_path(group)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(raw)
    return p


def mini_md(group: str, body: str) -> str:
    """测试用最小合法 md（frontmatter 是本格式的硬性要求）。"""
    return f"---\nid: {group}\nlabel: {group}\nbase: \n---\n\n{body}"


# ---------------------------------------------------------------- 1. md 解析


def test_parse_md_slots_temp_frontmatter():
    raw = (
        "---\nid: demo\nlabel: 演示\nbase: abc123def456\n---\n\n"
        "<!-- 注释不进入提示词 -->\n\n"
        "## base\n公共铁则文本\n\n"
        "## polish\n温度：0.70\n任务段文本 {{变量A}}\n"
    )
    p = prompts_store.parse_prompt_md(raw)
    assert (p["id"], p["label"], p["base"]) == ("demo", "演示", "abc123def456")
    assert p["slots"]["base"]["text"] == "公共铁则文本"
    assert p["slots"]["base"]["temp"] is None
    assert p["slots"]["polish"]["text"] == "任务段文本 {{变量A}}"
    assert p["slots"]["polish"]["temp"] == pytest.approx(0.70)
    assert p["errors"] == []


def test_parse_md_temp_clamped_and_bad_temp_ignored():
    p = prompts_store.parse_prompt_md("## s\n温度：3.9\n正文\n")
    assert p["slots"]["s"]["temp"] == 2.0
    p2 = prompts_store.parse_prompt_md("## s\n温度：热点\n正文\n")
    assert p2["slots"]["s"]["temp"] is None
    assert p2["slots"]["s"]["text"] == "温度：热点\n正文" or "温度" in p2["errors"][0]


def test_parse_md_fence_keeps_hash_lines():
    """槽正文里 `## ` 开头的行（输出格式段）用 ``` 围栏包裹，解析剥壳、正文逐字保留。"""
    raw = "## output_format\n```\n输出格式：\n## 本章概要\n（概要）\n## 正文\n（正文）\n```\n"
    p = prompts_store.parse_prompt_md(raw)
    assert p["slots"]["output_format"]["text"] == "输出格式：\n## 本章概要\n（概要）\n## 正文\n（正文）"
    assert set(p["slots"]) == {"output_format"}


def test_slot_vars_dedup_and_order():
    assert prompts_store.slot_vars("x {{B}} y {{A}} {{B}} 无 {{}} 尾") == ["B", "A"]
    assert prompts_store.slot_vars("没有变量") == []


def test_fill_replaces_vars_keeps_json_braces():
    s = prompts_store.fill("生成 {{候选数}} 个候选 输出 {\"titles\":[{\"title\":..}]}", 候选数=5)
    assert "生成 5 个候选" in s
    assert '{"titles":[{"title":..}]}' in s


# ---------------------------------------------------------------- 2. 两层回退与温度


def test_two_layer_fallback_to_code_default(clean_layers):
    assert prompts_store.text("demo", "s", "代码默认") == "代码默认"      # 只有代码默认
    write_builtin("demo", mini_md("demo", "## s\n内置层\n"))
    assert prompts_store.text("demo", "s", "代码默认") == "内置层"        # 内置层生效
    write_user("demo", mini_md("demo", "## s\n用户层\n"))
    assert prompts_store.text("demo", "s", "代码默认") == "用户层"        # 用户层覆盖
    assert prompts_store.delete_user_md("demo") is True                   # 恢复默认 = 删用户文件
    assert prompts_store.text("demo", "s", "代码默认") == "内置层"
    os.remove(prompts_store.builtin_path("demo"))
    assert prompts_store.text("demo", "s", "代码默认") == "代码默认"      # 代码兜底


def test_broken_user_md_silently_falls_back_with_warn(clean_layers, capsys):
    write_builtin("demo", mini_md("demo", "## s\n内置层\n"))
    write_user("demo", "这不是一段合法的 md（缺 frontmatter 与槽）")
    # 静默回退内置层，绝不让调用方拿到异常
    assert prompts_store.text("demo", "s", "代码默认") == "内置层"
    # 装载失败必须留 stderr 警告（等价 console.warn）
    captured = capsys.readouterr()
    assert "[prompts_store]" in captured.err


def test_temperature_override_user_builtin_default(clean_layers):
    assert prompts_store.temperature("demo", "s", 0.5) == 0.5            # 代码默认
    write_builtin("demo", mini_md("demo", "## s\n温度：0.60\n文本\n"))
    assert prompts_store.temperature("demo", "s", 0.5) == pytest.approx(0.60)
    write_user("demo", mini_md("demo", "## s\n温度：0.33\n文本\n"))
    assert prompts_store.temperature("demo", "s", 0.5) == pytest.approx(0.33)   # 用户层最优先


# ---------------------------------------------------------------- 3. 指纹 / 采用新版 / 恢复默认


def test_fingerprint_stable_and_registered_groups():
    groups = set(prompts_store.groups())
    need = {"selection-ai", "ghost", "workflow-writing", "outline", "longform-plan",
            "final-review", "titles", "idea-intake", "deslop", "inspirations"}
    assert need <= groups
    for gid in ("selection-ai", "ghost", "workflow-writing", "outline", "longform-plan",
                "final-review", "titles", "idea-intake", "deslop"):
        fp = prompts_store.fingerprint(gid)
        assert fp and len(fp) == 12
        assert prompts_store.fingerprint(gid) == fp      # 稳定


def test_stale_base_detected_and_adopt_rewrites(clean_layers):
    gid = "titles"
    fp = prompts_store.fingerprint(gid)
    stale = "0" * 12
    write_user(gid, f"---\nid: {gid}\nlabel: 取名器\nbase: {stale}\n---\n\n## system\n我的取名提示词\n")
    parsed = prompts_store.user_parsed(gid)
    assert parsed["base"] == stale                        # 钉在旧指纹 → 「默认值已更新」
    res = prompts_store.adopt(gid)
    assert res["ok"] and res["base"] == fp
    parsed2 = prompts_store.user_parsed(gid)
    assert parsed2["base"] == fp                          # 采用新版 = 重写 base 指纹
    assert parsed2["slots"]["system"]["text"] == "我的取名提示词"   # 用户文本保留


def test_save_user_md_normalizes_frontmatter_and_rejects_unknown_slot(clean_layers):
    res = prompts_store.save_user_md("ghost", "## 不存在的槽\n乱写\n")
    assert res["ok"] is False and res["errors"]           # 槽名校验
    res2 = prompts_store.save_user_md("ghost", "## beat\n我的续写段\n")
    assert res2["ok"] is True
    parsed = prompts_store.user_parsed("ghost")
    assert parsed["id"] == "ghost" and parsed["base"] == prompts_store.fingerprint("ghost")
    assert parsed["slots"]["beat"]["text"] == "我的续写段"


def test_builtin_md_matches_code_defaults_byte_for_byte():
    """内置 md 与接线模块注册的代码默认值逐字一致；frontmatter base == 当前指纹。

    代码默认换代时此测试必红——提示重新生成 prompts/*.md（指纹会打进断言信息）。
    """
    for gid in ("selection-ai", "ghost", "workflow-writing", "outline", "longform-plan",
                "final-review", "titles", "idea-intake", "deslop"):
        meta = prompts_store.group_meta(gid)
        assert meta and meta["slots"], f"{gid} 未注册"
        parsed = prompts_store.parse_prompt_md(open(prompts_store.builtin_path(gid), encoding="utf-8").read())
        assert parsed["errors"] == [], f"{gid} md 解析告警：{parsed['errors']}"
        for name, s in meta["slots"].items():
            ent = parsed["slots"].get(name)
            assert ent is not None, f"{gid}.{name} 缺槽（内置 md 少写了）"
            assert ent["text"] == s["default"], (
                f"{gid}.{name} 内置 md 与代码默认不一致——改代码默认后须同步重新生成 prompts/{gid}.md"
            )
            if s["temp"] is not None:
                assert ent["temp"] == pytest.approx(s["temp"]), f"{gid}.{name} 温度不一致"
        assert parsed["base"] == prompts_store.fingerprint(gid), (
            f"{gid} frontmatter base 过期：应为 {prompts_store.fingerprint(gid)}"
        )


def test_builtin_contract_md_keeps_marker_lines():
    raw = open(prompts_store.builtin_path("workflow-writing"), encoding="utf-8").read()
    assert f"=== {purity.MARK_OPEN} ===" in raw
    assert f"=== {purity.MARK_CLOSE} ===" in raw
    assert "红线" in raw      # md 头部注释带警告


# ---------------------------------------------------------------- 4. 冻结快照（批量任务运行中不受中途编辑影响）


def test_freeze_holds_version_until_thaw(clean_layers):
    write_user("ghost", mini_md("ghost", "## beat\nv1\n"))
    assert prompts_store.text("ghost", "beat", "代码默认") == "v1"
    prompts_store.freeze()
    try:
        assert prompts_store.frozen() is True
        assert prompts_store.text("ghost", "beat", "代码默认") == "v1"
        write_user("ghost", mini_md("ghost", "## beat\nv2\n"))           # 批量运行中改提示词
        assert prompts_store.text("ghost", "beat", "代码默认") == "v1"   # 本批不受影响
        assert prompts_store.temperature("ghost", "beat", 0.3) == 0.3   # 冻结时无温度行 → 代码默认
        write_user("ghost", mini_md("ghost", "## beat\n温度：0.9\nv2\n"))
        assert prompts_store.temperature("ghost", "beat", 0.3) == 0.3
    finally:
        prompts_store.thaw()
    assert prompts_store.text("ghost", "beat", "代码默认") == "v2"       # 下次任务用新版
    assert prompts_store.temperature("ghost", "beat", 0.3) == pytest.approx(0.9)


def test_freeze_thaw_refcount_and_snapshot(clean_layers):
    snap = prompts_store.snapshot()
    assert isinstance(snap, dict) and snap
    prompts_store.freeze()
    prompts_store.freeze()          # 引用计数（嵌套/并行批次）
    write_user("ghost", mini_md("ghost", "## beat\n中途版\n"))
    prompts_store.thaw()
    assert prompts_store.frozen() is True          # 还有一层引用
    assert prompts_store.text("ghost", "beat", "代码默认") != "中途版"
    prompts_store.thaw()
    assert prompts_store.frozen() is False
    assert prompts_store.text("ghost", "beat", "代码默认") == "中途版"


def test_writer_batch_loop_freezes_prompts():
    """接线检查：批量写作线程（proceed/继续）启动即冻结、结束解冻。"""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bot", "writer.py"), encoding="utf-8").read()
    assert "prompts_store.freeze()" in src
    assert "prompts_store.thaw()" in src


# ---------------------------------------------------------------- 5. selection_ai 走 prompts_store 后输出不变


_SEL_BASE = ("你是网文写作台的划词助手。用户在正文里选中了一小段文字，你只处理这一段。"
             "输出要求：直接输出处理后的文字成品，不要任何解释、前言、引号包裹或小节标题；"
             "不要输出「以下是」「修改说明」等元信息；保持中文小说的叙事文体。")


def test_selection_ai_output_identical_to_original(clean_layers):
    """无用户覆盖时，六动作系统提示词与接线前的原字符串逐字一致。"""
    p = selection_ai.system_prompt("polish")
    assert p == (_SEL_BASE + "\n\n本次任务【润色】：提升这句话的表达——把平庸直白的表述改得更有画面感与"
                 "节奏感；【不替用户扩剧情】：不得新增情节、人物、设定与对话轮次，只打磨既有内容的"
                 "措辞；长度与原文相当（±20%）。")
    p = selection_ai.system_prompt("fix")
    assert p == (_SEL_BASE + "\n\n本次任务【纠错】：只改正错别字、用词错误与标点问题（重复标点、"
                 "引号方向、顿号逗号混用）；【不动剧情】：不增删句子、不调整语序、不改情节与人物，"
                 "原文没有错的字词一律原样保留。")
    p = selection_ai.system_prompt("deslop")
    assert p.startswith(_SEL_BASE + "\n\n本次任务【去 AI 味】：")
    assert selection_ai._SLOP_HINTS in p           # 套话清单由运行时注入 {{套话清单}}
    assert "{{套话清单}}" not in p
    p = selection_ai.system_prompt("tone", persona="老刑警，说话短促")
    assert "指定人物：老刑警，说话短促" in p and "{{人物说明}}" not in p
    p = selection_ai.system_prompt("tone", persona="")
    assert "判断不出来就保持原语气" in p and "指定人物：未指定" in p
    p = selection_ai.system_prompt("custom", instruction="把句子改短")
    assert "用户指令：把句子改短" in p
    p = selection_ai.system_prompt("custom", instruction="")
    assert "用户指令：（用户未填写，按通顺润色处理）" in p
    p = selection_ai.system_prompt("expand")
    assert "1.5-2.5 倍" in p
    with pytest.raises(selection_ai.SelectionAIError):
        selection_ai.system_prompt("nope")


def test_selection_ai_temperature_uses_store_but_defaults_unchanged(clean_layers):
    assert selection_ai._temperature_of("fix") == pytest.approx(0.10)
    assert selection_ai._temperature_of("expand") == pytest.approx(0.85)
    # 用户覆盖温度后 run() 生效（llm.chat 被 conftest mock，这里直接抓参数验证）
    captured = {}

    def _fake_chat(msgs, **kw):
        captured["temperature"] = kw.get("temperature")
        return "成品文本"

    from bot import llm
    orig = llm.chat
    llm.chat = _fake_chat
    try:
        write_user("selection-ai", mini_md("selection-ai", "## polish\n温度：0.42\n任务段\n"))
        selection_ai.run("polish", "选中文本")
        assert captured["temperature"] == pytest.approx(0.42)
    finally:
        llm.chat = orig


def test_selection_ai_user_override_changes_prompt(clean_layers):
    write_user("selection-ai",
               "---\nid: selection-ai\nlabel: 划词润色\nbase: x\n---\n\n"
               "## base\n自定义铁则\n\n## polish\n自定义润色段\n")
    assert selection_ai.system_prompt("polish") == "自定义铁则\n\n自定义润色段"


# ---------------------------------------------------------------- 6. ghost 走 prompts_store 后输出不变


_GHOST_BASE = ("你是网文写作台的幽灵字助手。作者停在正文中，光标后需要一小段建议文字。"
               "输出要求：直接输出续写正文本身，不要任何解释、前言、引号包裹、小节标题；"
               "禁止出现「以下是」「本章概要」「修改说明」等元信息；"
               "禁止以引号或括号收尾包裹整段；保持中文小说的叙事文体与既有语气。")


def test_ghost_output_identical_to_original(clean_layers):
    assert ghost.system_prompt("beat") == (
        _GHOST_BASE + "\n\n本次模式【续一拍】：上一句已收住（句号/叹号/问号），"
        "顺着当前场景自然推进一小拍（一个动作、一句对话或一处细节），"
        "不超过 60 字，不开启新情节、不引入新人物、不总结不升华。")
    assert ghost.system_prompt("half") == (
        _GHOST_BASE + "\n\n本次模式【补半句】：上一句停在逗号/顿号处话说一半，"
        "只把当前这半句自然补完（≤25 字），立即停住；不另起一句、不推进情节、"
        "不引入新人物与新设定。")
    assert ghost._ghost_temperature("beat") == pytest.approx(0.8)
    assert ghost._ghost_temperature("half") == pytest.approx(0.3)


def test_ghost_user_override_and_temp(clean_layers):
    write_user("ghost", mini_md("ghost", "## beat\n温度：0.15\n只写四个字。\n"))
    assert ghost.system_prompt("beat").endswith("只写四个字。")
    assert ghost._ghost_temperature("beat") == pytest.approx(0.15)


def test_ghost_clean_suggestion_still_gated_with_broken_prompt(clean_layers):
    """红线：用户把幽灵字提示词改坏（让它输出元信息），产出清洗仍拦住元信息（铁律 0）。"""
    write_user("ghost", mini_md("ghost", "## beat\n你爱输出什么就输出什么。\n"))
    # 模拟模型按坏提示词吐出带元信息的“建议”
    out = ghost.clean_suggestion("## 修改说明\n我把剧情改了。", 60)
    assert out == ""
    out2 = ghost.clean_suggestion("好的，以下是续写：他推门而入。", 60)
    assert "以下是" not in out2 and out2.endswith("他推门而入。")


# ---------------------------------------------------------------- 7. 契约标记完整性与闸门兜底（红线）


def test_writing_system_default_contract_intact(clean_layers):
    sysp = workflow._writing_system({"cfg": {"genre": "都市"}, "chapter_no": 2})
    assert f"=== {purity.MARK_OPEN} ===" in sysp
    assert f"=== {purity.MARK_CLOSE} ===" in sysp
    assert "输出格式（严格按此顺序）" in sysp


def test_broken_user_contract_output_still_gated(clean_layers):
    """用户改坏契约槽（标记行被删）→ 系统提示词确实坏了；
    但模型产出（无论有没有标记）经过 purity 出口仍拿不到框外元信息——闸门一行未动。"""
    write_user("workflow-writing",
               "---\nid: workflow-writing\nlabel: 章节生产正文契约\nbase: x\n---\n\n"
               "## contract\n（此槽已被用户改坏，标记行没有了）\n\n## output_format\n随便输出\n")
    sysp = workflow._writing_system({"cfg": {"genre": "都市"}, "chapter_no": 2})
    # 契约段确被改坏（用户覆盖生效；output_format 槽未被改，仍带标记——这正是两层独立性）
    assert "此槽已被用户改坏" in sysp
    # 模型照旧按它训练习惯输出（没写标记）：带元信息小节
    model_out = ("## 本章概要\n主角发现密室。\n\n## 章首引子\n夜色沉沉。\n\n"
                 "## 正文\n他推开了门。\n\n## 章节备注\n下章接密室。\n\n"
                 "## 修改说明\n我改了三处。\n\n## 审核评分\n92 分。")
    # 没有标记 → split_marked 回退白名单（ok=False，inner=原文）
    _b, inner, _a, ok = purity.split_marked(model_out)
    assert ok is False
    cleaned = purity.sanitize_chapter(model_out)
    assert cleaned is not None
    reader_view = purity.chapter_text(cleaned)
    assert "他推开了门。" in reader_view                    # 正文在
    for junk in ("修改说明", "审核评分", "92 分", "我改了三处"):
        assert junk not in reader_view                     # 框外元信息全被拦
    # 模型有标记时：只取框内，框外一律剥掉
    marked = ("## 本章概要\n概要\n\n=== 一幕·YIMU 神笔工作台·开篇 ===\n## 正文\n真正文\n"
              "=== 一幕·YIMU 神笔工作台·结尾 ===\n\n## 章节备注\n备注\n")
    _b2, inner2, _a2, ok2 = purity.split_marked(marked)
    assert ok2 is True and "真正文" in inner2 and "概要" not in inner2 and "备注" not in inner2


# ---------------------------------------------------------------- 8. 其余接线点（workflow / longform / final_review）


def test_workflow_slots_wired(clean_layers):
    # 简介生成（idea-intake）
    sysp = workflow.extract_config("写一本都市异能")
    # llm.chat_json 被 conftest mock 返回 {}，这里只验证提示词组装不抛错、变量已注入
    assert sysp == {}
    # 取名器
    titles = workflow.gen_titles({"medium": "novel", "genre": "悬疑"}, n=4)
    assert titles == []
    # 去味
    ctx = {"cfg": {"genre": "悬疑"}}
    ds = workflow.deslop_chapter("## 正文\n" + "这句话不仅仅是一种表达，更是一种态度。" * 5, ctx)
    assert ds["cleaned"].startswith("## 正文")            # mock LLM 返回空 → 保留原文
    # 章纲（连载型）
    out = workflow.gen_outline({"medium": "novel", "genre": "悬疑", "chapters": 3})
    assert isinstance(out, str)


def test_longform_replan_slots_wired(clean_layers):
    assert longform._replan_system("planner") == longform.MULTI_ROLES["planner"]["system"]
    assert longform._replan_system("editor") == longform.MULTI_ROLES["editor"]["system"]
    assert longform._replan_system("") == longform.MULTI_ROLES["editor"]["system"]
    write_user("longform-plan", mini_md("longform-plan", "## planner\n自定义规划师\n"))
    assert longform._replan_system("planner") == "自定义规划师"
    assert longform._replan_system("editor") == longform.MULTI_ROLES["editor"]["system"]


def test_final_review_slots_wired(clean_layers):
    # 评分与改稿 system 走开放槽；mock llm 返回 ""，验证组装链路不抛错即可
    out = final_review._rubric_score("正文样本", "novel", {}, scope="")
    assert isinstance(out, dict)


# ---------------------------------------------------------------- 9. API 契约


@pytest.fixture()
def api(clean_layers):
    app = FastAPI()
    app.include_router(webprompts.router)
    return TestClient(app)


def test_api_prompts_list(api):
    d = api.get("/api/prompts").json()
    assert d["ok"] is True
    ids = {g["id"] for g in d["groups"]}
    assert {"selection-ai", "ghost", "workflow-writing", "outline", "longform-plan",
            "final-review", "titles", "idea-intake", "deslop", "inspirations"} <= ids
    sel = next(g for g in d["groups"] if g["id"] == "selection-ai")
    assert sel["label"] == "划词润色"
    slots = {s["slot"]: s for s in sel["slots"]}
    assert set(slots) == {"base", "fix", "polish", "expand", "deslop", "tone", "custom"}
    assert slots["deslop"]["vars"] == ["套话清单"]
    assert slots["tone"]["vars"] == ["人物说明"]
    assert slots["custom"]["vars"] == ["用户指令"]
    assert slots["fix"]["temp"] == pytest.approx(0.10)
    assert slots["polish"]["text"].startswith("本次任务【润色】")      # 槽=任务段（公共铁则在 base 槽）
    assert slots["base"]["text"].startswith("你是网文写作台的划词助手")
    assert slots["polish"]["source"] == "builtin"
    insp = next(g for g in d["groups"] if g["id"] == "inspirations")
    assert insp["slots"] == [] and insp["has_user_file"] is False
    assert insp["label"] == "灵感速记"      # 未注册组显示名回落内置 md frontmatter


def test_api_file_save_delete_and_validation(api):
    # 内置层原始 md
    d = api.get("/api/prompts/file", params={"group": "titles"}).json()
    assert d["ok"] and d["layer"] == "builtin" and "## system" in d["raw"]
    # 坏槽名被拒
    bad = api.post("/api/prompts/file", json={"group": "titles", "raw": "## 不存在\nx\n"})
    assert bad.status_code == 400
    # 合法保存（用户层）
    ok = api.post("/api/prompts/file", json={"group": "titles", "raw": "## system\n我的取名提示\n"})
    assert ok.status_code == 200
    d2 = api.get("/api/prompts/file", params={"group": "titles"}).json()
    assert d2["layer"] == "user"
    lst = api.get("/api/prompts").json()
    g = next(x for x in lst["groups"] if x["id"] == "titles")
    assert g["has_user_file"] is True and g["default_updated"] is False
    slot = next(s for s in g["slots"] if s["slot"] == "system")
    assert slot["modified"] is True and slot["source"] == "user" and slot["text"] == "我的取名提示"
    # 404
    assert api.get("/api/prompts/file", params={"group": "nope"}).status_code == 404
    # 恢复默认
    dele = api.delete("/api/prompts/file", params={"group": "titles"})
    assert dele.status_code == 200
    lst2 = api.get("/api/prompts").json()
    g2 = next(x for x in lst2["groups"] if x["id"] == "titles")
    assert g2["has_user_file"] is False
    slot2 = next(s for s in g2["slots"] if s["slot"] == "system")
    assert slot2["modified"] is False and slot2["source"] == "builtin"


def test_api_default_updated_badge_and_adopt(api):
    gid = "ghost"
    fp = prompts_store.fingerprint(gid)
    # 伪造旧指纹的用户文件（默认值换代场景）
    # 直接落盘（模拟「旧版本程序时保存的用户文件」——保存接口会把 base 归一到当前指纹，
    # 只有默认值真的换代后，磁盘上的旧 base 才会与新指纹不一致）
    write_user(gid, f"---\nid: {gid}\nlabel: 幽灵字续写\nbase: {'9' * 12}\n---\n\n## base\n{fp}\n\n## beat\n我的续写段\n")
    g = next(x for x in api.get("/api/prompts").json()["groups"] if x["id"] == gid)
    assert g["default_updated"] is True
    assert api.post("/api/prompts/adopt", json={"group": gid}).status_code == 200
    g2 = next(x for x in api.get("/api/prompts").json()["groups"] if x["id"] == gid)
    assert g2["default_updated"] is False and g2["user_base"] == fp
    assert g2["slots"][0]["modified"] is True or any(s["modified"] for s in g2["slots"]) is False


def test_api_adopt_without_user_file_400(api):
    assert api.post("/api/prompts/adopt", json={"group": "deslop"}).status_code == 400


def test_api_prompts_contract_matches_frontend_calls():
    """前端 promptedit.js 调用的路径与后端路由一一对应（文本级校验）。"""
    fe = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "static", "v2", "js", "views", "promptedit.js"), encoding="utf-8").read()
    be = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "bot", "webprompts.py"), encoding="utf-8").read()
    for path in ("/api/prompts", "/api/prompts/file", "/api/prompts/adopt"):
        assert path in fe and path in be
    assert "@router.get" in be and "@router.post" in be and "@router.delete" in be


# ---------------------------------------------------------------- 10. JSON 形状槽可编辑（改坏由出口兜底）


def test_json_shape_slot_editable_and_code_fallback(clean_layers):
    """把 idea-intake 的 JSON 形状改坏 → 解析由调用方兜底（chat_json 失败返回 {}），不炸任务。"""
    res = prompts_store.save_user_md(
        "idea-intake", "## system\n只输出【完全不是JSON】的东西？？\n")
    assert res["ok"] is True
    sysp = prompts_store.text("idea-intake", "system", default=workflow._EXTRACT_SYSTEM)
    assert "完全不是JSON" in sysp                     # 槽已生效
    prefs = workflow.load_prefs()
    filled = prompts_store.fill(sysp, 偏好题材=str(prefs["preferences"].get("favoriteGenres")),
                                常写章数=str(prefs["preferences"].get("typicalChapterCount")))
    assert "完全不是JSON" in filled
    # chat_json 被 conftest mock；真实场景解析失败时 extract_config 的调用方有既有兜底，
    # 删用户文件即恢复：
    prompts_store.delete_user_md("idea-intake")
    assert prompts_store.text("idea-intake", "system", default=workflow._EXTRACT_SYSTEM) == workflow._EXTRACT_SYSTEM


def test_never_raises_on_any_garbage(clean_layers):
    """任何形式的坏输入都不许让 text/temperature 抛错（创作任务失败红线）。"""
    for raw in ("", "---\nbroken", "## a\n温度：x\n", "---\nid: x\n---\n## s\n温度：99\n\n\n",
                "## s\n" + "{{" * 40, "---\nid: demo\nbase: z\n---\n"):
        write_user("demo", raw)
        assert isinstance(prompts_store.text("demo", "s", "兜底"), str)
        assert isinstance(prompts_store.temperature("demo", "s", 0.5), float)
