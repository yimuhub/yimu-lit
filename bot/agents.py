"""v0.5 M4：多 Agent 创作宇宙 —— 角色注册表 + 子 Agent 独立配模型 + 并发调度。

模型解析在 providers.agent_resolve（agent_models > stage_models > 默认），
限流在 providers.ConcurrencyGuard（全局并发上限 + 每 provider 限流，防 429）。
这里提供「宇宙」语义层：AGENTS 注册表、每岗位绑定的读写、批量分发多书并发创作。
"""
from . import providers

# 岗位注册表：每个子 Agent 可单独配 provider/model；缺省回落到 stage_models / 默认供应商。
AGENTS = {
    "planner":  {"label": "规划师", "stage": "planning", "desc": "出大纲/世界观/人物"},
    "writer":   {"label": "作家",   "stage": "writing",  "desc": "写正文/扩写"},
    "reviewer": {"label": "审稿人", "stage": "review",   "desc": "五维评审/逐章评分/终审"},
    "rewriter": {"label": "改写师", "stage": "review",   "desc": "定向重写/自动重写（可独立挂更便宜快的模型）"},
    "title":    {"label": "拟题师", "stage": None,       "desc": "书名/标题生成"},
    "deslopper": {"label": "去味师", "stage": "review",   "desc": "AI 腔定点改写（slop 报告 → 最小改法逐条修）"},
    "editor":    {"label": "总编",   "stage": "review",   "desc": "全书终审/跨章一致性/红线扫描"},
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
    json.dump(c, open(p, "w"), ensure_ascii=False, indent=2)
    return am

def view():
    """给前端：每个岗位的绑定 + 可选项（供应商→模型）。"""
    opts = providers.active_model_options()
    binds = get_bindings()
    out = []
    for agent, meta in AGENTS.items():
        b = binds.get(agent, {}) or {}
        out.append({"agent": agent, "label": meta["label"], "stage": meta["stage"], "desc": meta["desc"],
                    "provider_id": b.get("provider_id"), "model": b.get("model"),
                    "effective": ("已绑定 " + (b.get("model") or "?")) if (b.get("provider_id") or b.get("model")) else "回落 stage/默认"})
    return {"agents": out, "options": opts, "max_global_agents": _max_global()}

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
    json.dump(c, open(p, "w"), ensure_ascii=False, indent=2)
    return c["defaults"]["max_global_agents"]


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
