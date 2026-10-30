"""v0.5 M4：多 Agent 创作宇宙 —— 角色注册表 + 子 Agent 独立配模型/技能 + 并发调度。

模型解析在 providers.agent_resolve（agent_models > stage_models > 默认），
限流在 providers.ConcurrencyGuard（全局并发上限 + 每 provider 限流，防 429）。
这里提供「宇宙」语义层：AGENTS 注册表、每岗位绑定的读写（模型 + 技能）、批量分发多书并发创作。

v2.1 P1：岗位×技能绑定 —— config.json defaults.agent_skills = { "<agent>": ["<skill>", ...] }。
技能名懒校验（skillkit 由并行同事维护、签名可能变动，校验失败只记警告、不阻止保存）。
"""
import logging

from . import providers
from .safeio import atomic_write_json

log = logging.getLogger(__name__)

# 岗位注册表：每个子 Agent 可单独配 provider/model；缺省回落到 stage_models / 默认供应商。
AGENTS = {
    "planner":  {"label": "规划师", "stage": "planning", "desc": "出大纲/世界观/人物"},
    "writer":   {"label": "作家",   "stage": "writing",  "desc": "写正文/扩写"},
    "reviewer": {"label": "审稿人", "stage": "review",   "desc": "五维评审/逐章评分/终审"},
    "rewriter": {"label": "改写师", "stage": "review",   "desc": "定向重写/自动重写（可独立挂更便宜快的模型）"},
    "title":    {"label": "拟题师", "stage": None,       "desc": "书名/标题生成"},
    "deslopper": {"label": "去味师", "stage": "review",   "desc": "AI 腔定点改写（slop 报告 → 最小改法逐条修）"},
    "editor":    {"label": "总编",   "stage": "review",   "desc": "全书终审/跨章一致性/红线扫描"},
    # v4.12.4：非小说文体专属岗位（每个文体至少 1 个写作 + 1 个审校）
    "script_writer":    {"label": "编剧",     "stage": "writing", "desc": "写场次/对白/动作行；竖屏短剧与分镜"},
    "script_checker":   {"label": "审剧人",   "stage": "review",  "desc": "剧本时代/人物在场/场号连续性体检"},
    "poetry_writer":    {"label": "诗词作者", "stage": "writing", "desc": "近体诗/词/曲/现代诗创作与格律"},
    "poetry_reviewer":  {"label": "诗词审校", "stage": "review",  "desc": "格律核查/意象密度/炼字建议"},
    "prose_writer":     {"label": "散文作者", "stage": "writing", "desc": "随笔/抒情/议论/叙事散文创作"},
    "prose_reviewer":   {"label": "散文审校", "stage": "review",  "desc": "去AI腔/结构推进/语感打磨"},
    "thesis_writer":    {"label": "学术写手", "stage": "writing", "desc": "论文分章撰写/引注管理"},
    "thesis_reviewer":  {"label": "学术审校", "stage": "review",  "desc": "引用核查/降AIGC/学术规范"},
    "children_writer":  {"label": "童话作家", "stage": "writing", "desc": "分级语言/互动设计/插图提示词"},
    "children_reviewer": {"label": "童书审校", "stage": "review", "desc": "年龄适配/安全红线/正面引导"},
    "news_writer":      {"label": "新闻编辑", "stage": "writing", "desc": "倒金字塔/通稿/社媒短版"},
    "news_reviewer":    {"label": "新闻审核", "stage": "review",  "desc": "5W1H核查/引语归属/客观性"},
    "copy_writer":      {"label": "文案策划", "stage": "writing", "desc": "口播稿/种草笔记/营销框架"},
    "copy_reviewer":    {"label": "文案审核", "stage": "review",  "desc": "钩子效果/转化力/平台适配"},
    "other_planner":    {"label": "综合策划", "stage": "planning", "desc": "模板选择/结构设计/受众定位"},
    "other_writer":     {"label": "综合写手", "stage": "writing", "desc": "剧本杀/播客/课程稿/演讲稿/拆书稿/影评"},
    "other_reviewer":   {"label": "综合审校", "stage": "review",  "desc": "模板规范核查/节奏与语感/定稿对账"},
}

def _config_path():
    import os
    return os.path.join(providers.ROOT, "config.json")

def get_bindings():
    """返回 {agent: {provider_id, model}} —— 从 config.json defaults.agent_models。"""
    import json
    try:
        return json.load(open(_config_path(), encoding="utf-8")).get("defaults", {}).get("agent_models", {}) or {}
    except Exception:
        return {}

def set_binding(agent, provider_id=None, model=None):
    """把某岗位的模型绑定写进 config.json（留空 = 取消绑定、回落 stage/默认）。"""
    import json
    p = _config_path()
    try:
        c = json.load(open(p, encoding="utf-8"))
    except Exception:
        c = {}
    d = c.setdefault("defaults", {})
    am = d.setdefault("agent_models", {})
    cur = am.get(agent, {}) or {}
    cur["provider_id"] = provider_id or cur.get("provider_id") or None
    cur["model"] = model or cur.get("model") or None
    if not (cur.get("provider_id") or cur.get("model")):
        am[agent] = {}      # 全空 → 清空绑定
    else:
        am[agent] = cur
    atomic_write_json(p, c)
    return am


# ---------- 岗位 × 技能绑定（v2.1 P1；defaults.agent_skills） ----------

# v3.0.0：内置默认技能包。岗位从未配置过技能时首次读取自动填入并落盘
# （用户显式绑定过——包括清空为空列表——则完全尊重用户配置）。
DEFAULT_AGENT_SKILLS = {
    "planner":   ["novel-volume-arc-planning", "novel-multithread-pov", "core-longform-continuity"],
    "writer":    ["novel-references-guides-golden3", "novel-references-guides-dialogue-writing",
                  "novel-hangup-curve", "core-anti-slop"],
    "reviewer":  ["core-anti-slop", "core-longform-continuity"],
    "rewriter":  ["core-feedback-revision", "novel-hangup-curve"],
    "title":     ["novel-hangup-curve"],
    "deslopper": ["core-anti-slop"],
    "editor":    ["core-ten-genre", "core-longform-continuity"],
    # v4.12.4：非小说岗位默认技能
    "script_writer":    ["script-scene-craft", "script-dialogue", "core-anti-slop"],
    "script_checker":   ["core-longform-continuity"],
    "poetry_writer":    ["poetry-jinti", "poetry-imagery-density", "core-anti-slop"],
    "poetry_reviewer":  ["poetry-meter-selfcheck"],
    "prose_writer":     ["prose-drafting-detail", "prose-structure-progression", "core-anti-slop"],
    "prose_reviewer":   ["prose-revision-final"],
    "thesis_writer":    ["thesis-methodology"],
    "thesis_reviewer":  ["thesis-citation-standards"],
    "children_writer":  ["children-methodology"],
    "children_reviewer": ["children-methodology"],
    "news_writer":      ["news-methodology"],
    "news_reviewer":    ["news-methodology"],
    "copy_writer":      ["copy-methodology"],
    "copy_reviewer":    ["copy-methodology"],
    "other_planner":    ["other-methodology", "other-templates"],
    "other_writer":     ["other-methodology", "other-templates"],
    "other_reviewer":   ["other-methodology", "other-templates"],
}

def get_skill_bindings():
    """返回 {agent: [skill name, ...]} —— 从 config.json defaults.agent_skills。
    v3.0.0：内置岗位从未配置过时自动填入默认技能包（一次性落盘，之后用户可随意改）。"""
    import json
    p = _config_path()
    try:
        c = json.load(open(p, encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_AGENT_SKILLS)   # 无 config（如测试环境）：只返回默认包，不落盘
    d = c.setdefault("defaults", {})
    asm = d.get("agent_skills")
    if not isinstance(asm, dict):
        asm = {}
    changed = False
    for agent, defaults in DEFAULT_AGENT_SKILLS.items():
        if agent not in asm:
            asm[agent] = list(defaults)
            changed = True
    if changed:
        d["agent_skills"] = asm
        atomic_write_json(p, c)
    return asm

# ---------- 按作品绑定（v3.0.0；<书>/02-写作计划.json meta.agent_skills） ----------

def book_skill_bindings(pdir):
    """某本书的按岗位技能绑定 {agent: [skills]}；非小说/缺失一律返回 {}。"""
    import json, os
    try:
        plan = json.load(open(os.path.join(pdir, "02-写作计划.json"), encoding="utf-8"))
        v = (plan.get("meta") or {}).get("agent_skills")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}

def set_book_skills(pdir, agent, names):
    """把某本书的岗位技能绑定写进 02-写作计划.json 的 meta.agent_skills。
    空列表 = 清空该岗位的本书绑定（回落全局绑定）。"""
    import json, os, datetime
    from .safeio import atomic_write_json as _aw
    cleaned = _clean_skills(names)
    _check_skills_exist(cleaned)
    fp = os.path.join(pdir, "02-写作计划.json")
    try:
        plan = json.load(open(fp, encoding="utf-8"))
    except Exception:
        raise ValueError("作品计划缺失或不可读（仅支持小说作品的按作品绑定）")
    meta = plan.setdefault("meta", {})
    binds = meta.get("agent_skills")
    if not isinstance(binds, dict):
        binds = {}
    binds[agent] = cleaned
    meta["agent_skills"] = binds
    plan["updatedAt"] = datetime.datetime.now().isoformat()
    _aw(fp, plan)
    return binds

def _clean_skills(names):
    """names 必须是字符串列表：去空白、去重、排序；非法输入抛 ValueError。"""
    if isinstance(names, str) or not isinstance(names, (list, tuple, set)):
        raise ValueError("技能必须是字符串列表")
    out = set()
    for n in names:
        if not isinstance(n, str):
            raise ValueError("技能必须是字符串列表")
        n = n.strip()
        if n:
            out.add(n)
    return sorted(out)

def _check_skills_exist(names):
    """懒校验：bot/skillkit 由并行同事维护、签名可能变动——容错导入，
    查不到的技能只记警告，不阻止保存，绝不 import 期崩溃。返回未知名列表。"""
    unknown = []
    try:
        from . import skillkit
        by_name = getattr(skillkit, "by_name", None)
        if not callable(by_name):
            return []
        for n in names:
            try:
                if by_name(n) is None:
                    unknown.append(n)
            except Exception:
                return []      # skillkit 行为变化：放弃校验，不影响保存
    except Exception:
        return []
    if unknown:
        log.warning("agent 技能懒校验：技能库中未找到 %s（仍已保存）", unknown)
    return unknown

def skills_for(agent):
    """某岗位绑定的技能名列表；未绑定/异常一律返回 []，永不抛异常。"""
    try:
        v = get_skill_bindings().get(agent)
        if not isinstance(v, list):
            return []
        return [s for s in v if isinstance(s, str) and s.strip()]
    except Exception:
        return []

def set_skills(agent, names):
    """把某岗位的技能绑定写进 config.json（空列表 = 取消绑定）。
    agent 必须在 AGENTS 内；names 去重排序后落盘，技能名懒校验只警告。"""
    if agent not in AGENTS:
        raise ValueError(f"未知岗位：{agent!r}")
    cleaned = _clean_skills(names)
    _check_skills_exist(cleaned)
    import json
    p = _config_path()
    try:
        c = json.load(open(p, encoding="utf-8"))
    except Exception:
        c = {}
    d = c.setdefault("defaults", {})
    asm = d.setdefault("agent_skills", {})
    asm[agent] = cleaned          # 空列表 = 清空绑定（与 set_binding 全空语义一致）
    atomic_write_json(p, c)
    return asm

def _skills_of(sbinds, agent):
    v = sbinds.get(agent)
    if not isinstance(v, list):
        return []
    return sorted({s.strip() for s in v if isinstance(s, str) and s.strip()})

def bindings_view():
    """给前端协作面板：岗位 + 模型绑定 + 技能绑定。
    返回 { agents: [{agent,label,stage,desc,provider_id,model,effective,skills}], max_global_agents }。"""
    binds = get_bindings()
    sbinds = get_skill_bindings()
    out = []
    all_agents = _all_agents()
    # v4.13.0：岗位 → 所属体裁映射，前端协作面板按当前作品体裁过滤岗位
    try:
        from . import medium as _medium_mod
        medium_map = dict(_medium_mod.MEDIUM_AGENTS)   # {agent: medium}
    except Exception:
        medium_map = {}
    for agent, meta in all_agents.items():
        b = binds.get(agent, {}) or {}
        out.append({"agent": agent, "label": meta["label"], "stage": meta["stage"], "desc": meta["desc"],
                    "provider_id": b.get("provider_id"), "model": b.get("model"),
                    "medium": medium_map.get(agent, ""),
                    "effective": ("已绑定 " + (b.get("model") or "?")) if (b.get("provider_id") or b.get("model")) else "回落 stage/默认",
                    "skills": _skills_of(sbinds, agent)})
    return {"agents": out, "max_global_agents": _max_global()}

def view():
    """给前端：每个岗位的绑定 + 可选项（供应商→模型）。
    v2.1：字段保持原样，仅每个 agent 增加 skills。"""
    bv = bindings_view()
    bv["options"] = providers.active_model_options()
    return bv

def _max_global():
    import json
    try:
        return int(json.load(open(_config_path(), encoding="utf-8")).get("defaults", {}).get("max_global_agents", 4))
    except Exception:
        return 4

def set_max_global(n):
    import json
    p = _config_path()
    try:
        c = json.load(open(p, encoding="utf-8"))
    except Exception:
        c = {}
    c.setdefault("defaults", {})["max_global_agents"] = max(1, int(n))
    atomic_write_json(p, c)
    return c["defaults"]["max_global_agents"]


def _custom_agents_path():
    import os
    return os.path.join(providers.ROOT, "config.json")

def _load_custom_agents():
    """从 config.json defaults.custom_agents 读取自定义 Agent 列表。"""
    import json
    try:
        return json.load(open(_config_path(), encoding="utf-8")).get("defaults", {}).get("custom_agents", {}) or {}
    except Exception:
        return {}

def _save_custom_agents(custom):
    """持久化自定义 Agent 到 config.json。"""
    import json
    p = _config_path()
    try:
        c = json.load(open(p, encoding="utf-8"))
    except Exception:
        c = {}
    c.setdefault("defaults", {})["custom_agents"] = custom
    atomic_write_json(p, c)
    return custom

def add_agent(name, desc, stage="writing", skills=None, label=None):
    """添加一个自定义 Agent。
    name: 英文 key（如 'xianxia_writer'）
    label: 中文名（如 '玄幻作家'）
    desc: 描述（如 '专门写玄幻小说'）
    stage: 阶段码（planning/writing/review）
    skills: 绑定的技能名列表
    """
    if not name or not isinstance(name, str):
        raise ValueError("Agent 名称不能为空")
    key = name.strip().lower().replace(" ", "_")
    custom = _load_custom_agents()
    custom[key] = {
        "label": label or name,
        "stage": stage or "writing",
        "desc": desc or "",
        "custom": True,
    }
    _save_custom_agents(custom)
    # 如果有技能绑定，也写入
    if skills and isinstance(skills, list):
        asm = get_skill_bindings()
        asm[key] = [s for s in skills if isinstance(s, str) and s.strip()]
        import json
        p = _config_path()
        try:
            c = json.load(open(p, encoding="utf-8"))
        except Exception:
            c = {}
        c.setdefault("defaults", {})["agent_skills"] = asm
        atomic_write_json(p, c)
    return custom[key]

def _all_agents():
    """合并内置 Agent + 自定义 Agent。"""
    custom = _load_custom_agents()
    merged = dict(AGENTS)
    for k, v in custom.items():
        if k not in merged:
            merged[k] = v
    return merged


# ---------- 批量分发：多本书同时创作（受 ConcurrencyGuard 限并发） ----------
def dispatch_write(book_keys, proj_getter, writer_write):
    """并发对多本书跑 write_book（每本书一个线程；真正的 LLM 并发由 ConcurrencyGuard 在全局/provider 上限内排队）。
    返回 {key: {ok, message}}。"""
    import threading
    results = {}
    lock = threading.Lock()
    def _one(key):
        try:
            proj = proj_getter(key)
            writer_write(proj)
            with lock:
                results[key] = {"ok": True}
        except Exception as e:
            with lock:
                results[key] = {"ok": False, "message": str(e)[:200]}
    threads = [threading.Thread(target=_one, args=(k,), daemon=True) for k in book_keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results
