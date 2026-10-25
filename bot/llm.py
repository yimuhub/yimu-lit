"""OpenAI 兼容 LLM 客户端：按任务所选供应商/模型调用（多供应商支持）

未带 cfg 时安全回落到默认 active 供应商，不破坏旧行为。
"""
import json, os, sys, time, threading
import httpx
from . import providers
from .safeio import atomic_write_json, safe_join

class LLMError(RuntimeError):
    pass

class BudgetExceeded(LLMError):
    """已达 token 预算上限：写线程应停在当前进度，而不是继续烧钱。"""
    pass

# ---------- v0.3.x：Token 账本（按书归集 LLM 用量；线程本地防并发串账） ----------
_TL = threading.local()
_PROJECTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects")
_USAGE_GLOBAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "usage_global.json")
_USAGE_LOCK = threading.Lock()   # usage.json / usage_global.json 读改写串行化
_TRACE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "llm_calls.jsonl")
_TRACE_LOCK = threading.Lock()

def book(key: str, stage: str = ""):
    """标记当前线程正在为谁记账（书 key + 阶段），供 chat() 回读 usage 归集。"""
    _TL.book = key
    _TL.stage = stage or "writing"

def _load_json_loose(path: str, default: dict) -> dict:
    """读账本/配置：文件损坏（历史并发写、截断）时尽量抢救第一个 JSON 对象，救不出用默认值。"""
    try:
        txt = open(path, encoding="utf-8").read()
    except OSError:
        return dict(default)
    try:
        obj = json.loads(txt)
    except Exception:
        try:
            obj, _ = json.JSONDecoder().raw_decode(txt.lstrip())
        except Exception:
            return dict(default)
    return obj if isinstance(obj, dict) else dict(default)


def read_usage(book_key: str) -> dict:
    default = {"total_prompt": 0, "total_completion": 0, "total_tokens": 0, "calls": 0, "by_stage": {}}
    if not book_key:
        return default
    try:
        bdir = safe_join(_PROJECTS_DIR, book_key, label="书 key")
    except ValueError:
        return default
    return _load_json_loose(os.path.join(bdir, "usage.json"), default)


def _record_usage(book_key: str, stage: str, usage: dict):
    """把一次调用的 usage 累加到 <书>/usage.json + 全局 usage_global.json（不致命）。
    多书/多线程并行写同一账本时会互相覆盖，必须加锁 + 原子写。"""
    try:
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        tt = int(usage.get("total_tokens") or (pt + ct))
        with _USAGE_LOCK:
            # 单本
            try:
                bdir = safe_join(_PROJECTS_DIR, book_key, label="书 key")
            except ValueError:
                bdir = ""
            if bdir and os.path.isdir(bdir):
                uf = os.path.join(bdir, "usage.json")
                u = read_usage(book_key)
                u["total_prompt"] += pt; u["total_completion"] += ct; u["total_tokens"] += tt; u["calls"] += 1
                bs = u.setdefault("by_stage", {})
                sb = bs.setdefault(stage, {"prompt": 0, "completion": 0, "total": 0, "calls": 0})
                sb["prompt"] += pt; sb["completion"] += ct; sb["total"] += tt; sb["calls"] += 1
                u["last"] = {"stage": stage, "prompt": pt, "completion": ct, "total": tt}
                atomic_write_json(uf, u)
            # 全局
            g = _load_json_loose(_USAGE_GLOBAL, {"total_tokens": 0, "calls": 0})
            g["total_tokens"] += tt; g["calls"] += 1
            atomic_write_json(_USAGE_GLOBAL, g)
    except Exception:
        pass

def _global_defaults() -> dict:
    try:
        return json.load(open(os.path.join(providers.ROOT, "config.json"), encoding="utf-8")).get("defaults", {}) or {}
    except Exception:
        return {}

def budget_tokens() -> int:
    """单本 token 预算上限（0=不限）。读到全局 config.defaults.token_budget。"""
    try:
        return max(0, int(_global_defaults().get("token_budget") or 0))
    except (TypeError, ValueError):
        return 0

def book_tokens(book_key: str) -> int:
    """该书已用 token（读 usage.json；损坏/不存在按 0）。"""
    try:
        return int(read_usage(book_key).get("total_tokens") or 0)
    except Exception:
        return 0

def _check_budget(book_key: str) -> None:
    cap = budget_tokens()
    if cap and book_key and book_tokens(book_key) >= cap:
        raise BudgetExceeded(f"《{book_key}》已达 token 预算上限（{cap}），已停止继续调用模型")

def _trace_enabled() -> bool:
    return bool(_global_defaults().get("llm_trace", True))

_TRACE_FIELDS = ("book", "stage", "agent", "provider", "model", "cached", "ok",
                 "prompt_tokens", "completion_tokens", "total_tokens", "latency_ms", "error")

def _trace_call(**rec) -> None:
    """结构化调用日志（JSONL）：字段白名单，绝不写入 prompt/密钥。"""
    if not _trace_enabled():
        return
    rec = {k: rec[k] for k in _TRACE_FIELDS if k in rec}
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        with _TRACE_LOCK:
            with open(_TRACE_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if os.path.getsize(_TRACE_FILE) > 2_000_000:
                lines = open(_TRACE_FILE, encoding="utf-8").read().splitlines()[-3000:]
                with open(_TRACE_FILE, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
    except Exception:
        pass

def recent_calls(limit: int = 50) -> list[dict]:
    try:
        lines = open(_TRACE_FILE, encoding="utf-8").read().splitlines()[-max(1, min(500, limit)):]
    except Exception:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out

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

# ---------- v0.7 R3：LLM 内容缓存（大厂标准：确定性 prompt 复用，省 token + 提速；cfg llm_cache 可关） ----------
import hashlib
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 256

def _read_global_cache_default():
    """模块级读一次全局 <ROOT>/config.json 的 defaults.llm_cache（缺省 False）。"""
    try:
        gc = json.load(open(os.path.join(providers.ROOT, "config.json"), encoding="utf-8"))
        return bool((gc.get("defaults") or {}).get("llm_cache", False))
    except Exception:
        return False

_GLOBAL_CACHE_DEFAULT = _read_global_cache_default()

def _cache_key(model, temperature, json_mode, msgs, provider_id="", base_url="", max_tokens=None):
    try:
        blob = json.dumps([provider_id, base_url, model, temperature, json_mode, max_tokens, msgs],
                          ensure_ascii=False, sort_keys=True)[:6000]
    except Exception:
        return None
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()

def _cache_get(k):
    if not k:
        return None
    with _CACHE_LOCK:
        return _CACHE.get(k)

def _cache_set(k, v):
    if not k:
        return
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)), None)   # 近似 LRU（插入序）
        _CACHE[k] = v

def _cache_enabled(cfg, use_cache):
    """use_cache 显式参数优先；否则读 cfg.defaults.llm_cache；再否则回落全局 providers/config.json
    defaults.llm_cache（每书 cfg 通常不含该键，需读全局默认，否则生产路径缓存不生效）；默认关。"""
    if use_cache is not None:
        return bool(use_cache)
    if ((cfg or {}).get("defaults") or {}).get("llm_cache") is not None:
        return bool((cfg or {}).get("defaults")["llm_cache"])
    return bool(_GLOBAL_CACHE_DEFAULT)


def _trace_prov(prov, model):
    """可观测性：记录每次调用命中的供应商/模型（stderr，不影响返回）。"""
    print(f"[llm] provider={prov.get('id')} model={model} base={prov.get('base_url')}",
          file=sys.stderr, flush=True)

def _trace_cache(hit: bool, model):
    """v0.7 R3：可观测性：标记 LLM 缓存命中/写入（stderr）。"""
    print(f"[llm] cache={'HIT' if hit else 'MISS/SET'} model={model}", file=sys.stderr, flush=True)

def _max_global():
    """v0.5 M4：全局并发上限（config.json defaults.max_global_agents，缺省 4）。"""
    try:
        return int(json.load(open(os.path.join(providers.ROOT, "config.json"), encoding="utf-8"))
                      .get("defaults", {}).get("max_global_agents", 4))
    except Exception:
        return 4

def chat(messages, json_mode=False, temperature=None, max_tokens=None, retries=3, cfg=None, stage=None, agent=None, use_cache=None):
    """一次性 chat 调用，返回文本。带简单重试。cfg 决定用哪个供应商/模型；
    stage（planning/writing/review）若配置了 stage_models 则按环节选模型（P4）。
    agent（v0.5 M4）：子 Agent 岗位（planner/writer/reviewer/rewriter/title）——优先 agent_models 绑定。
    v0.3 P4：按模型规则引擎套 上下文/max_tokens/RPM/429退避/extra_body。v0.5：跨线程 ConcurrencyGuard 限并发。
    v0.7 R3：use_cache（None=按 cfg.defaults.llm_cache）——确定性 prompt 命中缓存直接返回（省 token+提速）。"""
    from . import model_rules
    if agent is None and stage:          # v0.5 M4：未显式指定 agent 时按 stage 推导岗位，agent_models 绑定即生效
        agent = {"planning": "planner", "writing": "writer", "review": "reviewer"}.get(stage, stage)
    prov, model = providers.agent_resolve(cfg, agent, stage)
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
    # v0.7 R3：确定性 prompt 缓存命中 → 直接返回（不调 LLM、不记账、不占并发）
    _ce = _cache_enabled(cfg, use_cache)
    _ck = _cache_key(body["model"], body["temperature"], json_mode, _msgs,
                             prov.get("id") or "", prov.get("base_url") or "", body.get("max_tokens")) if _ce else None
    _bk = getattr(_TL, "book", None)
    _stage = getattr(_TL, "stage", stage) or (stage or "writing")
    _hit = _cache_get(_ck)
    if _hit is not None:
        _trace_cache(True, model)
        _trace_call(book=_bk or "", stage=_stage, agent=agent or "", provider=prov.get("id") or "",
                    model=model, cached=True, ok=True, total_tokens=0)
        return _hit
    _check_budget(_bk)      # 缓存命中不计费，预算检查放在缓存之后
    _t0 = time.time()
    last_err = None
    _throttle(cfg)                      # P4/P5：调用前平滑节奏（防 429）
    model_rules.pace_rpm(rules)         # v0.3 P4：按模型规则的 RPM 限流
    timeout = _llm_timeout(cfg, stage, prov)
    _backoff = int(rules.get("backoff_base", 3))
    _guard = providers.ConcurrencyGuard(_max_global())   # v0.5 M4：跨线程全局并发 + provider 限流
    for i in range(retries):
        try:
            with _guard.acquire(prov):
                with httpx.Client(timeout=timeout) as client:
                    r = client.post(base + "/chat/completions",
                                    json=body, headers=headers)
            if r.status_code == 429:
                time.sleep(_backoff * (i + 1))   # v0.3 P4：按供应商退避基值（在 guard 外 sleep，不占名额）
                last_err = LLMError(f"429 限流: {r.text[:200]}")
                continue
            r.raise_for_status()
            data = r.json()
            _last_call["ts"] = time.time()
            _u = data.get("usage") or {}
            _bk = getattr(_TL, "book", None)
            if _bk and _u:                     # v0.3.x：回读 usage，归集到该书 token 账本
                _record_usage(_bk, getattr(_TL, "stage", "writing") or "writing", _u)
            _content = data["choices"][0]["message"]["content"]
            _cache_set(_ck, _content)          # v0.7 R3：存缓存（成功后）
            _trace_call(book=_bk or "", stage=_stage, agent=agent or "", provider=prov.get("id") or "",
                        model=model, cached=False, ok=True,
                        prompt_tokens=int(_u.get("prompt_tokens") or 0),
                        completion_tokens=int(_u.get("completion_tokens") or 0),
                        total_tokens=int(_u.get("total_tokens") or 0),
                        latency_ms=int((time.time() - _t0) * 1000))
            return _content
        except LLMError as e:
            last_err = e
        except Exception as e:
            last_err = LLMError(str(e))
            time.sleep(3)
    _trace_call(book=_bk or "", stage=_stage, agent=agent or "", provider=prov.get("id") or "",
                model=model, cached=False, ok=False, error=str(last_err)[:300],
                latency_ms=int((time.time() - _t0) * 1000))
    raise last_err

def chat_json(messages, temperature=None, cfg=None, stage=None, agent=None, use_cache=None):
    """调用并解析 JSON（容忍模型夹带 markdown 代码块）。cfg 决定供应商/模型；stage 按环节选模型；agent（v0.5）子 Agent 岗位。
    v0.7 R3：use_cache 透传 chat()（确定性 prompt 命中缓存直接返回）。"""
    raw = chat(messages, json_mode=True, temperature=temperature, cfg=cfg, stage=stage, agent=agent, use_cache=use_cache)
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
