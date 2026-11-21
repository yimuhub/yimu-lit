"""OpenAI 兼容 LLM 客户端：按任务所选供应商/模型调用（多供应商支持）

未带 cfg 时安全回落到默认 active 供应商，不破坏旧行为。
"""
import json, os, random, sys, time, threading
from urllib.parse import urlparse

from .paths import DATA_ROOT
import httpx
from . import providers
from .safeio import atomic_write_json, safe_join


def _local_base(base_url: str) -> bool:
    """本机网关一律不走环境代理：系统 NO_PROXY 含 IPv6 方括号时 httpx 会解析失败。"""
    try:
        host = (urlparse(base_url or "").hostname or "").lower()
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0") or host.startswith("127.")

class LLMError(RuntimeError):
    pass

class BudgetExceeded(LLMError):
    """已达 token 预算上限：写线程应停在当前进度，而不是继续烧钱。"""
    pass

# ---------- v0.3.x：Token 账本（按书归集 LLM 用量；线程本地防并发串账） ----------
_TL = threading.local()
_PROJECTS_DIR = os.path.join(DATA_ROOT, "projects")
_USAGE_GLOBAL = os.path.join(DATA_ROOT, "usage_global.json")
_USAGE_LOCK = threading.Lock()   # usage.json / usage_global.json 读改写串行化
_TRACE_FILE = os.path.join(DATA_ROOT, "llm_calls.jsonl")
_TRACE_LOCK = threading.Lock()

def book(key: str, stage: str = ""):
    """标记当前线程正在为谁记账（书 key + 阶段），供 chat() 回读 usage 归集。"""
    _TL.book = key
    _TL.stage = stage or "writing"


def current_book() -> str:
    """当前线程正在服务的书 key（llm.book() 标记；未标记返回空串）。
    供 workflow 流式接线复用——book 上下文与 token 记账同源，调用方无需重复传参。"""
    return getattr(_TL, "book", None) or ""


def set_chapter(no):
    """v2.9.0：标记当前线程正在写哪一章（章号），token 精确归到章。
    传 None 清除（审校/成书阶段不计入任何章）。"""
    _TL.chapter = no

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
                # v2.9.0：线程标记了章号时，token 同步归到章（并行写作下每章互不串账）
                chno = getattr(_TL, "chapter", None)
                if chno:
                    cb = u.setdefault("by_chapter", {}).setdefault(str(chno), {"total": 0, "calls": 0})
                    cb["total"] += tt; cb["calls"] += 1
                u["last"] = {"stage": stage, "prompt": pt, "completion": ct, "total": tt}
                atomic_write_json(uf, u)
            # 全局
            g = _load_json_loose(_USAGE_GLOBAL, {"total_tokens": 0, "calls": 0})
            g["total_tokens"] += tt; g["calls"] += 1
            atomic_write_json(_USAGE_GLOBAL, g)
    except Exception:
        pass

_CFG_CACHE = {"mtime": 0, "data": {}}
_CFG_LOCK = threading.Lock()   # _CFG_CACHE 读改写串行化（同 _USAGE_LOCK/_CACHE_LOCK 模式）


def _global_defaults() -> dict:
    """v4.12.16：按 mtime 缓存 config.json——此前每次 LLM 调用重复 open+json.load 3-5 次。"""
    fp = os.path.join(providers.ROOT, "config.json")
    try:
        mtime = os.path.getmtime(fp)
        with _CFG_LOCK:
            if _CFG_CACHE["mtime"] != mtime:
                _CFG_CACHE["data"] = json.load(open(fp, encoding="utf-8")).get("defaults", {}) or {}
                _CFG_CACHE["mtime"] = mtime
            return _CFG_CACHE["data"]
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


def book_chapter_tokens(book_key: str, no) -> int:
    """v2.9.0：该书某章已用 token（usage.json by_chapter；无记录按 0）。"""
    try:
        u = read_usage(book_key)
        return int(((u.get("by_chapter") or {}).get(str(no)) or {}).get("total") or 0)
    except Exception:
        return 0

def _check_budget(book_key: str) -> None:
    cap = budget_tokens()
    if cap and book_key and book_tokens(book_key) >= cap:
        raise BudgetExceeded(f"《{book_key}》已达 token 预算上限（{cap}），已停止继续调用模型")

def _trace_enabled() -> bool:
    return bool(_global_defaults().get("llm_trace", True))

_TRACE_FIELDS = ("book", "stage", "agent", "provider", "model", "cached", "ok",
                 "prompt_tokens", "completion_tokens", "total_tokens", "latency_ms", "error",
                 "scene")   # v5.2.0：中文场景标签（写作/评审/幽灵字/续写/划词…），可选字段

def _trace_call(**rec) -> None:
    """结构化调用日志（JSONL）：字段白名单，绝不写入 prompt/密钥。
    v5.2.0：可选 scene（中文场景标签）——调用方未传时该字段不出现在账本行里。"""
    if not _trace_enabled():
        return
    rec = {k: rec[k] for k in _TRACE_FIELDS if k in rec}
    if not rec.get("scene"):
        rec.pop("scene", None)
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


LLM_ERROR_LOG = os.path.join(DATA_ROOT, "data", "llm_errors.jsonl")

def _log_llm_error(provider_id: str, model: str, base_url: str, status, detail: str,
                   book: str = "", stage: str = ""):
    """v4.16.8：模型调用失败全局留痕（data/llm_errors.jsonl）——
    AI 生成/创作失败后「后台没日志」的直接解。诊断包自动收集。"""
    try:
        import datetime as _dt
        os.makedirs(os.path.dirname(LLM_ERROR_LOG), exist_ok=True)
        entry = {"ts": _dt.datetime.now().isoformat(timespec="seconds"),
                 "provider": provider_id, "model": model, "base_url": base_url,
                 "status": status, "book": book, "stage": stage,
                 "detail": (detail or "").replace("\n", " ")[:300]}
        with open(LLM_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _llm_timeout(cfg, stage, prov):
    """每调用 httpx 超时：defaults.llm_timeout 覆盖供应商默认；planning 环节可再放宽 planning_timeout。
    v5.5.1：超时秒数可配置（读 config defaults.llm_timeout / planning_timeout 与供应商 timeout 字段，
    默认不动）；供应商代理本身可能另有更短的读超时窗口（实测 120s），这只能靠「重试降载」兜底。"""
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


# ---------- v5.5.1：524/超时韧性——重试降载 + 中文可行动报错 ----------
# 实测（桌面端 2026-11-21）：供应商代理 http://127.0.0.1:3425/v1 有 120 秒读超时窗口，
# 长规划调用超窗即回 HTTP 524；此前 chat() 只「原样重试」同一份大输入，3 连败后把
# 英文 httpx 报错向上抛，longform._llm_json 吞成 {} → 章卡永远占位 → 写前闸门拒收 →
# 整本书停摆。现在：重试仍失败且属 planning 类调用时，自动缩小输入（截短上下文）
# 再试一次；最终失败给出中文可行动建议。

_TIMEOUT_MARKS = ("524", "522", "timed out", "timeout", "总时长超过", "排队超过",
                  "proxy read timeout", "read timeout", "wall-clock")
_PLANNING_STAGE_MARKS = ("planning", "plan", "longform")
_SHRINK_MIN_CHARS = 1200   # 输入小于该值时缩小意义不大，原样重试即可


def _is_load_timeout(err) -> bool:
    """识别「供应商/代理过载或读超时」类错误（524/网关超时/wall-clock 守卫/排队超时）。"""
    s = str(err or "")
    low = s.lower()
    return any(m.lower() in low for m in _TIMEOUT_MARKS)


def _planning_like(stage) -> bool:
    """planning / 长规划类调用（longform_plan / longform_window / longform_chapter …）。"""
    s = str(stage or "").lower()
    return any(m in s for m in _PLANNING_STAGE_MARKS)


def _msg_len(messages) -> int:
    try:
        return sum(len(str(m.get("content") or "")) for m in messages or [])
    except Exception:
        return 0


def _shrink_messages(messages, keep_ratio: float = 0.45, floor: int = 500) -> list:
    """确定性截短上下文（头尾保留，绝不碰最后一条消息的完整性）：

    · system 头（指令在头部）+ 尾（输出格式约定常在尾部）都保留；
    · 历史消息只保留最近 2 条，更早的折叠成一行省略说明；
    · 每条超长消息按 keep_ratio 截短：头 70% + 「…[上下文已截短以降低负载]…」 + 尾 30%，
      这样 JSON 输出规范（通常在尾部）与任务描述（通常在头部）都不会丢。
    """
    out = []
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    n = len(msgs)
    for idx, m in enumerate(msgs):
        role = str(m.get("role") or "user")
        content = str(m.get("content") or "")
        # 只保留最近 2 条非 system 消息 + 全部 system；更早的折叠
        if role != "system" and idx < n - 2:
            continue
        cap = max(floor, int(len(content) * keep_ratio))
        if len(content) > cap:
            head = int(cap * 0.7)
            tail = max(0, cap - head)
            content = (content[:head] + "\n…[上下文已截短以降低负载，关键任务要求与输出格式见下]…\n"
                       + (content[-tail:] if tail else ""))
        out.append({"role": role, "content": content})
    dropped = n - len(out)
    if dropped > 0:
        if out and out[0].get("role") == "system":
            out[0]["content"] = f"（另有 {dropped} 条较早的历史消息已省略以降低单次请求负载）\n" + out[0]["content"]
        else:
            out.insert(0, {"role": "system",
                           "content": f"（另有 {dropped} 条较早的历史消息已省略以降低单次请求负载）"})
    return out


def _human_timeout_error(err) -> LLMError:
    """把 524/超时类英文报错翻译成中文 + 可行动建议（原错误尾部保留供诊断）。"""
    return LLMError(
        "供应商代理读超时（HTTP 524/网关超时，常见于代理约 120 秒的读超时窗口）：已自动重试并缩小输入，"
        "仍然失败。建议："
        "①在「设置 → 模型与供应商」换响应更快的模型；"
        "②调低单次规划批量（总规划改分批）或缩短上下文；"
        "③如代理固定 120 秒读超时，可在设置里把 llm_timeout 调小以便更快进入下一轮重试。"
        f"原始错误：{str(err)[:160]}")

# ---------- v0.7 R3：LLM 内容缓存（大厂标准：确定性 prompt 复用，省 token + 提速；cfg llm_cache 可关） ----------
import hashlib
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

_HTTP_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm-http")
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
    """v4.8.0 重大修复：此前 blob 截前 6000 字符再哈希——章节号在 prompt 末尾根本没进键，
    并行写作下前 6000 字符（蓝图/文风/人物）几乎相同 → 不同章节命中同一条缓存，
    返回逐字相同的正文（「标题不同、内容重复」的根因）。现在对完整 blob 取哈希。"""
    try:
        blob = json.dumps([provider_id, base_url, model, temperature, json_mode, max_tokens, msgs],
                          ensure_ascii=False, sort_keys=True)
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

def chat(messages, json_mode=False, temperature=None, max_tokens=None, retries=3, cfg=None, stage=None, agent=None, use_cache=None, scene=None, _shrunk=False):
    """一次性 chat 调用，返回文本。带简单重试。cfg 决定用哪个供应商/模型；
    stage（planning/writing/review）若配置了 stage_models 则按环节选模型（P4）。
    agent（v0.5 M4）：子 Agent 岗位（planner/writer/reviewer/rewriter/title）——优先 agent_models 绑定。
    v0.3 P4：按模型规则引擎套 上下文/max_tokens/RPM/429退避/extra_body。v0.5：跨线程 ConcurrencyGuard 限并发。
    v0.7 R3：use_cache（None=按 cfg.defaults.llm_cache）——确定性 prompt 命中缓存直接返回（省 token+提速）。
    v5.2.0：scene（可选，中文场景标签如「幽灵字」「续写」「划词」）——只进调用账本行，不影响路由与返回。
    v5.5.1：524/超时韧性——同一输入重试仍超时/524 且属 planning 类调用时，自动缩小输入
    （截短上下文）再试一次（_shrunk 内部标志防递归）；最终失败把报错翻成中文 + 可行动建议。
    流式接口（chat_stream）语义不变：其兜底路径本来就委托 chat()，自动获得同等韧性。"""
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
                    model=model, cached=True, ok=True, total_tokens=0, scene=scene or "")
        return _hit
    _check_budget(_bk)      # 缓存命中不计费，预算检查放在缓存之后
    _t0 = time.time()
    last_err = None
    _throttle(cfg)                      # P4/P5：调用前平滑节奏（防 429）
    model_rules.pace_rpm(rules)         # v0.3 P4：按模型规则的 RPM 限流
    timeout = _llm_timeout(cfg, stage, prov)
    _backoff = int(rules.get("backoff_base", 3))
    _guard = providers.ConcurrencyGuard(_max_global())   # v0.5 M4：跨线程全局并发 + provider 限流
    _orig_len = _msg_len(_msgs)
    _shrunk_done = False          # 本轮调用链里是否已做过「缩小输入」降载
    for i in range(retries):
        # v5.5.1：524/超时降载——重试 1 次原样（i=1）；再失败且属 planning 类长输入调用，
        # 从第 3 次尝试（i=2）起自动截短上下文再试（总规划超窗 524 的根治：缩小输入比无限原样重试有效）。
        if (i >= 2 and last_err is not None and _is_load_timeout(last_err)
                and not _shrunk_done and not _shrunk
                and _planning_like(stage) and _orig_len >= _SHRINK_MIN_CHARS):
            _shrunk_done = True
            _msgs = _shrink_messages(_msgs)
            body["messages"] = _msgs
            _ck = _cache_key(body["model"], body["temperature"], json_mode, _msgs,
                             prov.get("id") or "", prov.get("base_url") or "",
                             body.get("max_tokens")) if _ce else None
            print(f"[llm] 524/超时降载重试：输入 {_orig_len} → {_msg_len(_msgs)} 字符（stage={stage}）",
                  file=sys.stderr, flush=True)
            _trace_call(book=_bk or "", stage=_stage, agent=agent or "", provider=prov.get("id") or "",
                        model=model, cached=False, ok=False,
                        error=f"timeout→shrink-retry {_orig_len}->{_msg_len(_msgs)}",
                        scene=scene or "")
        try:
            with _guard.acquire(prov):
                # v4.15.7：wall-clock 总时长守卫——httpx read-timeout 是「两次读之间」的超时，
                # 代理挂起时若持续滴字节永不触发（实测并行书/百万字规划 25 分钟无动静）。
                # 请求放独立 daemon 线程，主线程 fut.result(timeout) 强制中断。
                def _post_once(started):
                    # v4.24.16 修复：wall-clock 守卫此前把「在 8 线程池里排队的时间」也计入
                    # 超时——多书并行 + 评分/规划各岗并发时，后续请求在池里排队，守卫会
                    # 误杀还没真正发出的请求（fut.cancel 对已运行 future 无效，挂起请求
                    # 继续占坑，池被慢慢耗尽）。改为 future 实际开始执行后才起算。
                    started.set()
                    with httpx.Client(timeout=timeout,
                                      trust_env=not _local_base(base)) as client:
                        return client.post(base + "/chat/completions",
                                           json=body, headers=headers)
                _started = threading.Event()   # v4.28.0：经参数绑定（ruff B023，重试循环内闭包）
                fut = _HTTP_POOL.submit(_post_once, _started)
                try:
                    # 排队宽限 60s（纯等线程）；开始执行后再给完整 timeout+60
                    if not _started.wait(timeout=60):
                        fut.cancel()
                        raise LLMError("LLM 线程池排队超过 60s（并发过载保护，稍后重试）") from None
                    r = fut.result(timeout=timeout + 60)
                except FuturesTimeoutError:
                    fut.cancel()
                    # 原始超时异常无附加信息，消息本身已说明原因，不链式挂到外层
                    raise LLMError(f"LLM 调用总时长超过 {timeout + 60}s（wall-clock 守卫强制中断，防代理挂起）") from None
            if r.status_code == 429:
                # v5.1：429 不再只做线性 sleep——读 Retry-After、指数退避+抖动，
                # 末次仍 429 时切到另一组 active 供应商/模型（不覆盖用户默认配置）。
                _ra = 0
                try:
                    _ra = int(float(r.headers.get("retry-after") or 0))
                except (TypeError, ValueError):
                    _ra = 0
                _wait = max(_ra, _backoff * (2 ** i)) + random.uniform(0.2, 1.0)
                _wait = min(float(_wait), 90.0)
                last_err = LLMError(f"429 限流: {r.text[:200]}")
                if i >= retries - 1 and not (cfg or {}).get("_fallback_used"):
                    _fb = providers.fallback_config(cfg, exclude_provider=prov.get("id") or "",
                                                     exclude_model=model)
                    if _fb:
                        _c2 = dict(cfg or {})
                        _c2.update(_fb)
                        _c2["_fallback_used"] = True
                        try:
                            _log_llm_error(prov.get("id") or "", model, base, 429,
                                           f"fallback→{_fb.get('_provider_id')}/{_fb.get('_model')}",
                                           book=_bk or "", stage=_stage or "")
                        except Exception:
                            pass
                        return chat(messages, json_mode=json_mode, temperature=temperature,
                                    max_tokens=max_tokens, retries=2, cfg=_c2, stage=stage,
                                    agent=agent, use_cache=use_cache, scene=scene)
                time.sleep(_wait)
                continue
            if r.status_code in (401, 403):
                # v4.16.8：鉴权失败给出可行动的中文提示，并记入全局错误日志
                _log_llm_error(prov.get("id") or "", model, base, r.status_code, r.text[:200],
                               book=_bk or "", stage=_stage or "")
                raise LLMError(f"API 密钥无效或未配置（HTTP {r.status_code}）——请到「设置 → 模型与供应商」"
                               f"检查 {prov.get('id')}/{model} 的接口地址与密钥，点「测试连接」验证。供应商后台无调用记录 = 请求未通过鉴权。")
            if r.status_code >= 500:
                _log_llm_error(prov.get("id") or "", model, base, r.status_code, r.text[:200],
                               book=_bk or "", stage=_stage or "")
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
                        latency_ms=int((time.time() - _t0) * 1000), scene=scene or "")
            return _content
        except LLMError as e:
            last_err = e
        except Exception as e:
            last_err = LLMError(str(e))
            time.sleep(3)
    _trace_call(book=_bk or "", stage=_stage, agent=agent or "", provider=prov.get("id") or "",
                model=model, cached=False, ok=False, error=str(last_err)[:300],
                latency_ms=int((time.time() - _t0) * 1000), scene=scene or "")
    # v5.5.1：retries<=2 的调用链（如 429 fallback 用 retries=2）在循环里没机会降载——
    # 结束时若是超时类错误且属 planning 长输入，补一次「缩小输入」重试（只补一次，防递归）。
    if (last_err is not None and not _shrunk and not _shrunk_done
            and _is_load_timeout(last_err) and _planning_like(stage)
            and _msg_len(messages) >= _SHRINK_MIN_CHARS):
        try:
            _small = _shrink_messages(messages)
            print(f"[llm] 524/超时降载补试：输入 {_msg_len(messages)} → {_msg_len(_small)} 字符（stage={stage}）",
                  file=sys.stderr, flush=True)
            return chat(_small, json_mode=json_mode, temperature=temperature,
                        max_tokens=max_tokens, retries=1, cfg=cfg, stage=stage, agent=agent,
                        use_cache=use_cache, scene=scene, _shrunk=True)
        except LLMError as e2:
            last_err = e2 if _is_load_timeout(e2) else last_err
        except Exception:
            pass
    if last_err is not None and _is_load_timeout(last_err):
        raise _human_timeout_error(last_err)
    raise last_err or LLMError("LLM 调用失败（retries=0）")

def chat_stream(messages, cfg=None, agent=None, stage=None, on_delta=None, on_thinking=None,
                json_mode=False, temperature=None, max_tokens=None, retries=3, use_cache=None, scene=None):
    """chat() 的流式版：同一套路由/限流/预算/日志；增量文本经 on_delta(text_chunk) 回调；返回完整文本。

    - agent/stage 路由与 chat() 一致（agent 缺省按 stage 推导 planner/writer/reviewer）。
    - SSE 逐帧解析 choices[0].delta.content，data: [DONE] 结束；末帧 usage 回读进 token 账本。
    - v4.21.4：delta.reasoning_content（深度思考模型的思维链）经 on_thinking(piece) 单独回调，
      不混入正文；供应商不给 reasoning 时该回调不触发（前端回退到模板化思考摘要）。
    - 响应不是 SSE（普通 JSON）→ 取整段文本回调一次并返回（视为不支持流式）。
    - 流式任何异常（供应商不支持/网络中断/解析失败/超时）→ 丢弃已收文本，回退调 chat() 非流式一次性返回。
    - v5.2.0：scene（可选，中文场景标签）——只进调用账本行，不影响流式行为。"""
    from . import model_rules
    if agent is None and stage:          # 与 chat() 一致：未显式指定 agent 时按 stage 推导岗位
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
        "stream": True,
        "stream_options": {"include_usage": True},   # OpenAI 兼容：让末帧带 usage，token 账本才不漏记
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if rules.get("extra_body"):          # 供应商专属参数，与 chat() 同规则合并
        for _k, _v in rules["extra_body"].items():
            if _k not in body:
                body[_k] = _v
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer %s" % key

    def _safe_cb(piece):
        """on_delta 回调绝不阻断生成（消费方异常只丢帧）。"""
        try:
            if on_delta and piece:
                on_delta(piece)
        except Exception:
            pass

    def _safe_think(piece):
        """on_thinking 回调（模型思维链增量），与正文分流、同样绝不阻断生成。"""
        try:
            if on_thinking and piece:
                on_thinking(piece)
        except Exception:
            pass

    # 缓存/预算：与 chat() 同一套（缓存命中 → 整段回调一次直接返回，不计费）
    _ce = _cache_enabled(cfg, use_cache)
    _ck = _cache_key(body["model"], body["temperature"], json_mode, _msgs,
                     prov.get("id") or "", prov.get("base_url") or "", body.get("max_tokens")) if _ce else None
    _bk = getattr(_TL, "book", None)
    _stage = getattr(_TL, "stage", stage) or (stage or "writing")
    _hit = _cache_get(_ck)
    if _hit is not None:
        _trace_cache(True, model)
        _trace_call(book=_bk or "", stage=_stage, agent=agent or "", provider=prov.get("id") or "",
                    model=model, cached=True, ok=True, total_tokens=0, scene=scene or "")
        _safe_cb(_hit)
        return _hit
    _check_budget(_bk)      # 缓存命中不计费，预算检查放在缓存之后（与 chat() 一致）
    _t0 = time.time()
    _throttle(cfg)                      # P4/P5：调用前平滑节奏（防 429）
    model_rules.pace_rpm(rules)         # 按模型规则的 RPM 限流
    timeout = _llm_timeout(cfg, stage, prov)
    _guard = providers.ConcurrencyGuard(_max_global())   # 跨线程全局并发 + provider 限流（与 chat() 同一套）
    got, usage = [], {}
    try:
        with _guard.acquire(prov):
            # wall-clock 守卫：httpx read-timeout 只管「两次读之间」，代理滴字节时靠逐帧 deadline 兜底
            _deadline = time.time() + timeout + 60
            with httpx.Client(timeout=timeout,
                              trust_env=not _local_base(base)) as client:
                with client.stream("POST", base + "/chat/completions",
                                   json=body, headers=headers) as r:
                    if r.status_code != 200:
                        r.read()
                        raise LLMError(f"HTTP {r.status_code}: {(r.text or '')[:200]}")
                    if "text/event-stream" not in (r.headers.get("content-type") or "").lower():
                        # 供应商不支持流式：普通 JSON 整段回调一次并返回
                        data = json.loads(r.read().decode("utf-8", "replace"))
                        _msg0 = data["choices"][0].get("message") or {}
                        _piece = str(_msg0.get("content") or "")
                        _think0 = str(_msg0.get("reasoning_content") or _msg0.get("reasoning") or "")
                        usage = data.get("usage") or {}
                        got.append(_piece)
                        _safe_think(_think0)
                        _safe_cb(_piece)
                    else:
                        _done_seen = False
                        for line in r.iter_lines():
                            if time.time() > _deadline:
                                raise LLMError(f"LLM 流式调用总时长超过 {timeout + 60}s（wall-clock 守卫强制中断）")
                            line = (line or "").strip()
                            if not line.startswith("data:"):
                                continue
                            payload = line[len("data:"):].strip()
                            if not payload:
                                continue
                            if payload == "[DONE]":
                                _done_seen = True
                                break
                            try:
                                ev = json.loads(payload)
                            except Exception:
                                # JSONDecodeError 信息量有限，原始帧内容已在消息里，不链接
                                raise LLMError(f"SSE 帧解析失败: {payload[:120]}") from None
                            usage = ev.get("usage") or usage
                            chs = ev.get("choices") or []
                            if not chs:
                                continue
                            _delta = chs[0].get("delta") or {}
                            _think = _delta.get("reasoning_content") or _delta.get("reasoning") or ""
                            if _think:
                                _safe_think(_think)   # 模型思维链增量（与正文分流）
                            piece = _delta.get("content") or ""
                            if piece:
                                got.append(piece)
                                _safe_cb(piece)   # 增量文本实时回调（在调用线程，emit 侧自带锁）
                        if not _done_seen:
                            # 连接提前断开（没收到 [DONE]）= 生成被截断，按失败回退，绝不把半截正文当成功
                            raise LLMError("流式响应未正常结束（未收到 [DONE]，疑似连接中断）")
        text = "".join(got)
        if not text.strip():
            raise LLMError("流式返回为空")
        _last_call["ts"] = time.time()
        if _bk and usage:                  # 回读 usage，归集到该书 token 账本（与 chat() 同路径）
            _record_usage(_bk, getattr(_TL, "stage", "writing") or "writing", usage)
        _cache_set(_ck, text)
        _trace_call(book=_bk or "", stage=_stage, agent=agent or "", provider=prov.get("id") or "",
                    model=model, cached=False, ok=True,
                    prompt_tokens=int((usage or {}).get("prompt_tokens") or 0),
                    completion_tokens=int((usage or {}).get("completion_tokens") or 0),
                    total_tokens=int((usage or {}).get("total_tokens") or 0),
                    latency_ms=int((time.time() - _t0) * 1000), scene=scene or "")
        return text
    except Exception as _se:
        # 兜底：流式任何异常 → 丢弃已收文本，回退完整 chat()（其自带超时/重试/用户可读报错）
        _trace_call(book=_bk or "", stage=_stage, agent=agent or "", provider=prov.get("id") or "",
                    model=model, cached=False, ok=False, error="stream→fallback: " + str(_se)[:280],
                    latency_ms=int((time.time() - _t0) * 1000), scene=scene or "")
        return chat(messages, json_mode=json_mode, temperature=temperature, max_tokens=max_tokens,
                    retries=retries, cfg=cfg, stage=stage, agent=agent, use_cache=use_cache, scene=scene)


def chat_json(messages, temperature=None, cfg=None, stage=None, agent=None, use_cache=None, scene=None):
    """调用并解析 JSON（容忍模型夹带 markdown 代码块）。cfg 决定供应商/模型；stage 按环节选模型；agent（v0.5）子 Agent 岗位。
    v0.7 R3：use_cache 透传 chat()（确定性 prompt 命中缓存直接返回）。
    v5.2.0：scene（可选，中文场景标签）透传 chat()。"""
    raw = chat(messages, json_mode=True, temperature=temperature, cfg=cfg, stage=stage, agent=agent, use_cache=use_cache, scene=scene)
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
