"""OpenAI 兼容 LLM 客户端：按任务所选供应商/模型调用（多供应商支持）

未带 cfg 时安全回落到默认 active 供应商，不破坏旧行为。
"""
import json, os, sys, time, threading
import httpx
from . import providers

class LLMError(RuntimeError):
    pass

# ---------- v0.3.x：Token 账本（按书归集 LLM 用量；线程本地防并发串账） ----------
_TL = threading.local()
_PROJECTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects")
_USAGE_GLOBAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "usage_global.json")

def book(key: str, stage: str = ""):
    """标记当前线程正在为谁记账（书 key + 阶段），供 chat() 回读 usage 归集。"""
    _TL.book = key
    _TL.stage = stage or "writing"

def _record_usage(book_key: str, stage: str, usage: dict):
    """把一次调用的 usage 累加到 <书>/usage.json + 全局 usage_global.json（不致命）。"""
    try:
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        tt = int(usage.get("total_tokens") or (pt + ct))
        # 单本
        bdir = os.path.join(_PROJECTS_DIR, book_key)
        if os.path.isdir(bdir):
            uf = os.path.join(bdir, "usage.json")
            u = json.load(open(uf, encoding="utf-8")) if os.path.exists(uf) else {
                "total_prompt": 0, "total_completion": 0, "total_tokens": 0, "calls": 0, "by_stage": {}}
            u["total_prompt"] += pt; u["total_completion"] += ct; u["total_tokens"] += tt; u["calls"] += 1
            bs = u.setdefault("by_stage", {})
            sb = bs.setdefault(stage, {"prompt": 0, "completion": 0, "total": 0, "calls": 0})
            sb["prompt"] += pt; sb["completion"] += ct; sb["total"] += tt; sb["calls"] += 1
            u["last"] = {"stage": stage, "prompt": pt, "completion": ct, "total": tt}
            json.dump(u, open(uf, "w", encoding="utf-8"), ensure_ascii=False)
        # 全局
        g = json.load(open(_USAGE_GLOBAL, encoding="utf-8")) if os.path.exists(_USAGE_GLOBAL) else {"total_tokens": 0, "calls": 0}
        g["total_tokens"] += tt; g["calls"] += 1
        json.dump(g, open(_USAGE_GLOBAL, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass

# P5/P4 节奏：两次 LLM 调用之间的最小间隔（防 429 限流）+ 每调用超时可配
_last_call = {"ts": 0.0}

def _call_gap(cfg):
    try:
        return float((((cfg or {}).get("defaults")) or {}).get("call_gap", 2) or 0)
    except Exception:
        return 0

def _throttle(cfg):
    """调用前按 call_gap 平滑节奏：距上次完成不足 gap 则补齐（0=关）。"""
    gap = _call_gap(cfg)
    if gap > 0:
        wait = gap - (time.time() - _last_call["ts"])
        if wait > 0:
            time.sleep(wait)

def _llm_timeout(cfg, stage, prov):
    """每调用 httpx 超时：defaults.llm_timeout 覆盖供应商默认；planning 环节可再放宽 planning_timeout。"""
    d = ((cfg or {}).get("defaults")) or {}
    try:
        base = int(d.get("llm_timeout") or prov.get("timeout", 900))
    except (ValueError, TypeError):
        base = prov.get("timeout", 900)
    if stage == "planning":
        try:
            base = int(d.get("planning_timeout") or base)
        except (ValueError, TypeError):
            pass
    return base


def _trace_prov(prov, model):
    """可观测性：记录每次调用命中的供应商/模型（stderr，不影响返回）。"""
    print(f"[llm] provider={prov.get('id')} model={model} base={prov.get('base_url')}",
          file=sys.stderr, flush=True)

def chat(messages, json_mode=False, temperature=None, max_tokens=None, retries=3, cfg=None, stage=None):
    """一次性 chat 调用，返回文本。带简单重试。cfg 决定用哪个供应商/模型；
    stage（planning/writing/review）若配置了 stage_models 则按环节选模型（P4）。
    v0.3 P4：按模型规则引擎套 上下文/max_tokens/RPM/429退避/extra_body。"""
    from . import model_rules
    if stage:
        prov, model = providers.stage_resolve(cfg, stage)
    else:
        prov, model = providers.resolve(cfg)
    _trace_prov(prov, model)
    key = providers.key_of(prov)
    base = prov["base_url"].rstrip("/")
    rules = model_rules.effective_rules(prov, model_rules.load_overrides())
    _mt = max_tokens or prov.get("max_tokens", 12000)
    if rules.get("max_tokens"):
        _mt = min(int(_mt), int(rules["max_tokens"]))
    _msgs = model_rules.clamp_context(messages, rules.get("context", 0))
    body = {
        "model": model,
        "messages": _msgs,
        "temperature": (temperature if temperature is not None else prov.get("temperature", 0.85)),
        "max_tokens": _mt,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if rules.get("extra_body"):          # v0.3 P4：供应商专属参数（如 DeepSeek thinking）
        for _k, _v in rules["extra_body"].items():
            if _k not in body:
                body[_k] = _v
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer %s" % key
    last_err = None
    _throttle(cfg)                      # P4/P5：调用前平滑节奏（防 429）
    model_rules.pace_rpm(rules)         # v0.3 P4：按模型规则的 RPM 限流
    timeout = _llm_timeout(cfg, stage, prov)
    _backoff = int(rules.get("backoff_base", 3))
    for i in range(retries):
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(base + "/chat/completions",
                                json=body, headers=headers)
                if r.status_code == 429:
                    time.sleep(_backoff * (i + 1))   # v0.3 P4：按供应商退避基值
                    last_err = LLMError(f"429 限流: {r.text[:200]}")
                    continue
                r.raise_for_status()
                data = r.json()
                _last_call["ts"] = time.time()
                _u = data.get("usage") or {}
                _bk = getattr(_TL, "book", None)
                if _bk and _u:                     # v0.3.x：回读 usage，归集到该书 token 账本
                    _record_usage(_bk, getattr(_TL, "stage", "writing") or "writing", _u)
                return data["choices"][0]["message"]["content"]
        except LLMError as e:
            last_err = e
        except Exception as e:
            last_err = LLMError(str(e))
            time.sleep(3)
    raise last_err

def chat_json(messages, temperature=None, cfg=None, stage=None):
    """调用并解析 JSON（容忍模型夹带 markdown 代码块）。cfg 决定供应商/模型；stage 按环节选模型。"""
    raw = chat(messages, json_mode=True, temperature=temperature, cfg=cfg, stage=stage)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):] if "{" in raw else raw
        raw = raw[:raw.rfind("}") + 1]
    try:
        return json.loads(raw)
    except Exception:
        # 兜底：从文本里抠第一个 JSON 对象
        i, j = raw.find("{"), raw.rfind("}")
        if 0 <= i < j:
            return json.loads(raw[i:j + 1])
        raise
