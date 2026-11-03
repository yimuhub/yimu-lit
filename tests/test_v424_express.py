"""v4.24.0 极速模式（Express）。

覆盖四层，缺一层都会在真实使用里退化成「看起来有这个模式、实际没生效」：
  1. 判定口径 bot/express.py —— 全仓库唯一的解释器
  2. 入口归一 app._express_into —— 两条界面入口（mode 卡片 / express 开关）等价
  3. 向导短表单 bot.medium._copy_sections —— 极速下只留必填项
  4. 接口标识 bot.v2api —— 前端据此显示「极速模式 · 初稿」
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import express, medium


# ---------------------------------------------------------------- 判定口径

def test_on_accepts_both_entry_forms():
    # 小说走「创作模式」卡片，其余文体走「创作节奏」开关，两者必须等价
    assert express.on({"mode": "express"}) is True
    assert express.on({"express": True}) is True
    assert express.on({"express": "express"}) is True     # cards 提交的是字符串
    assert express.on({"express": "1"}) is True
    assert express.on({"express": "true"}) is True


def test_on_rejects_standard_and_absent():
    assert express.on({"mode": "rolling"}) is False
    assert express.on({"mode": "one_shot"}) is False
    assert express.on({"express": "standard"}) is False   # 非小说的「标准」选项
    assert express.on({"express": False}) is False
    assert express.on({}) is False
    assert express.on(None) is False


def test_on_explicit_false_wins_over_mode():
    """express 键一旦存在就以它为准：防止 mode=express 的老项目被误判。"""
    assert express.on({"express": False, "mode": "express"}) is False


def test_skip_stage_only_review_stages():
    # 只跳评审/打磨，创作与交付阶段一个不跳
    assert express.skip_stage("poetry", "R") is True       # 评改
    assert express.skip_stage("poetry", "K") is False      # 格律校验是纯 CPU，必须留
    assert express.skip_stage("poetry", "F") is False      # 定稿
    assert express.skip_stage("prose", "A") is True        # 去味
    assert express.skip_stage("prose", "P") is False       # 精修=拼接定稿
    assert express.skip_stage("thesis", "T4") is True      # 降 AIGC
    assert express.skip_stage("thesis", "T2") is False     # 分章撰写
    assert express.skip_stage("news", "N3") is True        # 事实核查
    assert express.skip_stage("news", "N4") is True        # 多版本
    assert express.skip_stage("other", "O3") is True       # 润色


def test_skip_stage_never_skips_structure():
    """一致性靠结构保证：novel/script/children 一张评审阶段都不跳。"""
    for med in ("novel", "script", "children", "copy"):
        assert express.stages(med) == frozenset(), f"{med} 不该有任何阶段被跳过"


def test_skip_stage_unknown_medium_is_conservative():
    # 未知文体一律不跳——宁可慢，不可残缺
    assert express.skip_stage("不存在的文体", "X") is False
    assert express.skip_stage("", "R") is False
    assert express.skip_stage(None, None) is False


def test_word_floor_is_softer_but_positive():
    # 用户定稿：保留 60% 软线（不足时补写一次），不是完全不设目标
    assert 0 < express.WORD_FLOOR_RATIO < 0.9


# ---------------------------------------------------------------- 入口归一

def _appmod():
    import app as appmod
    return appmod


def test_express_into_normalizes_both_entries():
    appmod = _appmod()
    # 小说的创作模式卡片
    cfg = {}
    appmod._express_into(cfg, {"mode": "express"})
    assert cfg.get("express") is True

    # 其余文体的创作节奏开关
    cfg = {}
    appmod._express_into(cfg, {"express": "express"})
    assert cfg.get("express") is True


def test_express_into_does_not_pollute_plan_with_false():
    """老项目没有 express 键。标准模式不该写 False 进去——
    02-写作计划.json 是用户可见的，写一堆 false 只会让它变脏。"""
    appmod = _appmod()
    for payload in ({}, {"mode": "rolling"}, {"express": "standard"}):
        cfg = {}
        appmod._express_into(cfg, payload)
        assert "express" not in cfg, f"不该写键：{payload}"


# ---------------------------------------------------------------- 向导短表单

def _visible_keys(medium_key, group, values):
    """按与前端 isVisible 同一条规则算出可见字段（含 not_in 反向条件）。"""
    fields = medium.wizard_payload()[medium_key][group]
    out = []
    for f in fields:
        rule = f.get("when")
        if not rule:
            out.append(f["key"])
            continue
        if rule.get("in"):
            if values.get(rule["field"]) in rule["in"]:
                out.append(f["key"])
        elif rule.get("not_in"):
            if values.get(rule["field"]) not in rule["not_in"]:
                out.append(f["key"])
    return out


def test_novel_express_shows_only_essentials():
    """极速模式的「简单设定」：题材 / 模式 / 一句话起点 / 章数 / 每章字数。
    batch_size 保留在表单上（必填项不隐藏），但由 express 效果自动选中「全部写完」。"""
    keys = []
    for g in ("brief", "basic", "style"):
        keys += _visible_keys("novel", g, {"mode": "express"})
    for must in ("genre", "mode", "idea", "scale", "min_words_per_chapter", "batch_size"):
        assert must in keys, f"极速模式必须保留 {must}"
    for noise in ("platform", "tone", "perspective", "sells", "title", "agents"):
        assert noise not in keys, f"极速模式不该出现 {noise}"


def test_novel_standard_shows_full_form():
    keys = []
    for g in ("brief", "basic", "style"):
        keys += _visible_keys("novel", g, {"mode": "rolling"})
    for k in ("platform", "tone", "perspective", "sells", "batch_size", "title"):
        assert k in keys, f"标准模式不该隐藏 {k}"


def test_every_medium_has_an_express_entry():
    """全部文体都要能选极速——这是需求明确的适用范围。"""
    for key in ("novel", "script", "poetry", "prose", "thesis",
                "children", "news", "copy", "other"):
        fields = medium.wizard_payload()[key]["brief"]
        keys = [f["key"] for f in fields]
        if key == "novel":
            opts = [o["value"] for f in fields if f["key"] == "mode" for o in f["options"]]
            assert "express" in opts, f"{key} 的创作模式缺极速项"
        else:
            assert "express" in keys, f"{key} 缺「创作节奏」开关"
            opts = [o["value"] for f in fields if f["key"] == "express" for o in f["options"]]
            assert opts == ["standard", "express"], f"{key} 的创作节奏取值不对：{opts}"


def test_express_hides_polish_settings():
    """风格页全是打磨/排版设置，极速模式整页收起。"""
    for key in ("script", "poetry", "prose"):
        shown = _visible_keys(key, "style", {"express": "express", "form": "modern"})
        for f in medium.wizard_payload()[key]["style"]:
            if f.get("type") in ("note", "preview"):
                continue
            assert f["key"] not in shown, f"{key} 极速模式不该显示风格项 {f['key']}"


def test_poetry_keeps_metrical_check_toggle():
    """格律校验必须保留（K 是确定性检查，砍掉等于输出废品），
    所以它自己的 form 条件优先，不该被极速的收起规则覆盖。"""
    fields = medium.wizard_payload()["poetry"]["style"]
    mc = next(f for f in fields if f["key"] == "metrical_check")
    assert mc.get("when") == {"field": "form", "in": ["wujue", "qijue", "wulv", "qilv", "ci", "qu"]}


def test_express_entry_fields_carry_the_batch_effect():
    """创作模式/创作节奏字段必须挂 effect="express"——前端靠它把 batch_size 自动拉满成
    「全部写完」。删掉这个 effect，极速模式就会退化成「还是逐章写」。"""
    novel_mode = next(f for f in medium.wizard_payload()["novel"]["brief"] if f["key"] == "mode")
    assert novel_mode.get("effect") == "express"
    for key in ("script", "poetry", "prose", "thesis", "children", "news", "copy", "other"):
        fld = next(f for f in medium.wizard_payload()[key]["brief"] if f["key"] == "express")
        assert fld.get("effect") == "express", f"{key} 的创作节奏没挂 express 效果"


def test_express_never_hides_required_fields():
    """「简单设定」不等于「不给输入」：必填项一个都不能收。"""
    for key in ("novel", "script", "poetry", "prose", "thesis",
                "children", "news", "copy", "other"):
        w = medium.wizard_payload()[key]
        for g in ("brief", "basic", "style"):
            for f in w[g]:
                if f.get("required") and f.get("when", {}).get("not_in"):
                    raise AssertionError(f"{key}.{f['key']} 是必填项，却在极速下被隐藏")


# ---------------------------------------------------------------- 接口标识

def test_plan_view_exposes_express_flag(tmp_path, monkeypatch):
    """前端靠 plan.express 判定；没这个字段就会退回显示 mode 原始 token。"""
    from bot import v2api
    assert callable(getattr(v2api, "_plan_view", None))
    # _plan_view 需要 proj.path；用最小桩验证字段会被写入
    class _Proj:
        path = str(tmp_path)
        def plan(self):
            return {"config": {"express": True}, "chapters": []}
    out = v2api._plan_view(_Proj(), {"mode": "express", "volumes": []})
    assert out.get("express") is True

    class _ProjStd:
        path = str(tmp_path)
        def plan(self):
            return {"config": {"mode": "rolling"}, "chapters": []}
    out = v2api._plan_view(_ProjStd(), {"mode": "rolling", "volumes": []})
    assert out.get("express") is False


# v4.24.14：02c 的 mode 是向导留下的只读记录，不能拿来判「现在是不是极速」。
# 回归场景：书用极速开、后来切回标准 → 02c.mode 仍是 "express"，
# 旧实现把开关显示成「开」，用户每次点击都发 on=false，再也切不回极速。

def test_plan_view_ignores_stale_mode_token(tmp_path):
    from bot import v2api

    class _ProjStale:
        path = str(tmp_path)
        def plan(self):
            # 写作计划已切回标准（express=False），但 config.mode 还剩 express
            return {"config": {"express": False, "mode": "express"}, "chapters": []}

    out = v2api._plan_view(_ProjStale(), {"mode": "express", "volumes": []})
    assert out.get("express") is False, "config.express=False 被 02c 的历史 token 顶翻了"
    assert out.get("mode") == "rolling", "关掉极速后不该把 express 原样吐给界面"


def test_plan_view_falls_back_to_mode_for_legacy_plan(tmp_path):
    """老计划 config 里一个开关字段都没有：只能靠 02c 的 mode 判。"""
    from bot import v2api

    class _ProjLegacy:
        path = str(tmp_path)
        def plan(self):
            return {"config": {}, "chapters": []}

    out = v2api._plan_view(_ProjLegacy(), {"mode": "express", "volumes": []})
    assert out.get("express") is True
