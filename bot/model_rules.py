"""v0.3 P4：模型规则引擎

按接入的供应商/模型自动套用各家的限制（上下文、max_tokens、RPM/TPM、429 退避、
extra_body、免费额度提示）。未知/通用供应商 → 用户可自由配置（generic=True）。

用法：
    from . import model_rules
    rules = model_rules.effective_rules(prov, user_over)   # prov = providers.resolve 的 dict
    # rules: {vendor, context, max_tokens, rpm, tpm, backoff_base, call_gap, extra_body, generic, note}
"""

# 已知供应商规则（保守值；用户可经 generic override 覆盖）
from typing import Optional

_PROFILES = {
    "agnes":    {"context": 131072, "max_tokens": 8192, "rpm": 60, "tpm": 100000, "backoff_base": 2, "call_gap": 2.0, "extra_body": {}, "note": "Agnes 免费额度有限：429 时按 2s 退避，注意 call_gap", "generic": False},
    "deepseek": {"context": 1000000, "max_tokens": 8192, "rpm": 250, "tpm": 500000, "backoff_base": 3, "call_gap": 0.0, "extra_body": {"thinking": False}, "note": "DeepSeek：1M 上下文；thinking 默认关省成本", "generic": False},
    "openai":   {"context": 128000, "max_tokens": 16000, "rpm": 500, "tpm": 200000, "backoff_base": 2, "call_gap": 0.0, "extra_body": {}, "note": "OpenAI：按 Tier 的 RPM/TPM", "generic": False},
    "gemini":   {"context": 1000000, "max_tokens": 8192, "rpm": 1500, "tpm": 1000000, "backoff_base": 2, "call_gap": 0.0, "extra_body": {}, "note": "Gemini：1M 上下文", "generic": False},
    "qwen":     {"context": 131072, "max_tokens": 8192, "rpm": 60, "tpm": 100000, "backoff_base": 2, "call_gap": 1.0, "extra_body": {}, "note": "通义千问 Qwen", "generic": False},
    "glm":      {"context": 128000, "max_tokens": 8192, "rpm": 50, "tpm": 100000, "backoff_base": 2, "call_gap": 1.0, "extra_body": {}, "note": "智谱 GLM", "generic": False},
    "generic":  {"context": 128000, "max_tokens": 8192, "rpm": 60, "tpm": 100000, "backoff_base": 3, "call_gap": 1.0, "extra_body": {}, "note": "通用：以下参数可自由配置", "generic": True},
}

# base_url 关键词 → 供应商
_DETECT = [
    ("agnes", "agnes"), ("agnes-ai", "agnes"),
    ("deepseek", "deepseek"),
    ("openai.com", "openai"), ("api.openai", "openai"),
    ("gemini", "gemini"), ("generativelanguage", "gemini"),
    ("dashscope", "qwen"), ("qwen", "qwen"), ("alibaba", "qwen"),
    ("zhipu", "glm"), ("bigmodel", "glm"), ("glm", "glm"),
]


def detect_vendor(base_url: str = "") -> str:
    """按 base_url 识别供应商；识别不到 → generic（用户自由配置）。"""
    u = (base_url or "").lower()
    for kw, v in _DETECT:
        if kw in u:
            return v
    return "generic"


def effective_rules(prov: dict, user_over: Optional[dict] = None) -> dict:
    """供应商 dict（base_url/...）→ 有效规则。user_over 可覆盖任意字段（通用供应商的自由配置入口）。"""
    vendor = detect_vendor(prov.get("base_url", "") if prov else "")
    rules = dict(_PROFILES.get(vendor, _PROFILES["generic"]))
    rules["vendor"] = vendor
    # 供应商自身可配 max_tokens/context 优先于 profile（providers.json 里用户填的）
    for k_src, k_dst in (("max_tokens", "max_tokens"), ("context", "context")):
        if prov and prov.get(k_src):
            try:
                rules[k_dst] = int(prov.get(k_src))
            except (ValueError, TypeError):
                pass
    # 用户 override（通用/自由配置）
    if user_over:
        for k in ("context", "max_tokens", "rpm", "tpm", "backoff_base", "call_gap"):
            if user_over.get(k) not in (None, ""):
                try:
                    rules[k] = int(user_over[k]) if k not in ("backoff_base", "call_gap") else float(user_over[k])
                except (ValueError, TypeError):
                    pass
        if user_over.get("extra_body"):
            rules["extra_body"] = {**rules["extra_body"], **user_over["extra_body"]}
    return rules


# ---------- 限流（RPM）+ 上下文钳制 ----------
import time as _t, threading as _th
_rl = {"ts": 0.0, "n": 0, "lock": _th.Lock()}

# v0.3 P4：通用/自由配置 的 override 存 top-level model_rules.json
import os as _os
_OVR_FILE = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "model_rules.json")


def load_overrides() -> dict:
    try:
        import json as _js
        return _js.load(open(_OVR_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_overrides(d: dict):
    import json as _js
    open(_OVR_FILE, "w", encoding="utf-8").write(_js.dumps(d, ensure_ascii=False, indent=2))


def pace_rpm(rules: dict, key: str = "") -> None:
    """按 rpm 平滑：每分钟不超过 rpm 次调用。"""
    rpm = int(rules.get("rpm") or 60)
    if rpm <= 0:
        return
    with _rl["lock"]:
        now = _t.time()
        if now - _rl["ts"] > 60:
            _rl["ts"], _rl["n"] = now, 0
        _rl["n"] += 1
        if _rl["n"] > rpm:
            wait = 60 - (now - _rl["ts"])
            if wait > 0:
                _t.sleep(min(wait, 60))


def clamp_context(messages: list, context: int) -> list:
    """粗估上下文（按 4 字≈1 token 的中文粗估），超 budget 则从中间裁剪保留首尾（系统+首章+尾）。
    只做保护性裁剪，不做精确分词。"""
    if not context or context <= 0:
        return messages
    rough = context  # token budget
    def _est(msgs):
        return sum(len(str(m.get("content", ""))) for m in msgs) // 4
    if _est(messages) <= rough:
        return messages
    # 保留 system(0) + 最后 N 条（近期上下文）
    keep_tail = max(1, int(rough / _est(messages) * len(messages)))
    if len(messages) <= keep_tail + 1:
        return messages
    head = messages[:1]
    tail = messages[-keep_tail:]
    return head + [{"role": "user", "content": "（前文过长已省略，以下为近段上下文，请据此续写）"}] + tail
