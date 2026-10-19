"""模型供应商管理层：自定义/预设一键绑定、多供应商并存、按任务选模型。

- 存储：应用根目录 providers.json（与 config.json 同层）
- 迁移：首启 providers.json 缺失时，从旧 config.json 的 llm 块迁成默认 active 供应商
- 解析：resolve(cfg) -> (provider, model)。创作配置带 _provider_id/_model 就用它，
        否则回落到 default_provider（必须 active），再否则第一条 active。全程安全回落。
- 并发安全：resolve 每次从 cfg 现读，不维护模块级"当前供应商"，避免多任务并写串台。
- 密钥：只存本地（inline 或 api_key_file），不外发、不进日志；展示打码。
"""
import json, os, re, time, threading, httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROVIDERS_FILE = os.path.join(ROOT, "providers.json")
CONFIG_FILE = os.path.join(ROOT, "config.json")
SECRETS_DIR = os.path.join(ROOT, "secrets")

# 锁：providers.json 读写（CRUD + 迁移）串行化。
# 用 RLock（可重入）：CRUD 函数持锁后会调 load_data()（也取锁），普通 Lock 会死锁。
_LOCK = threading.RLock()

# ---------- 内置预设（OpenAI 兼容）——一键绑定 ----------
PRESETS = [
    {"slug": "agnes", "name": "Agnes AI", "base_url": "https://api.agnes-ai.cn/v1",
     "models": ["agnes-2.5-flash", "agnes-3.0-flash"], "auth": "Bearer key", "local": False},
    {"slug": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1",
     "models": ["gpt-4o", "gpt-4o-mini", "o3"], "auth": "Bearer key", "local": False},
    {"slug": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "models": ["deepseek-chat", "deepseek-reasoner"], "auth": "Bearer key", "local": False},
    {"slug": "qwen", "name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "models": ["qwen-plus", "qwen-max", "qwen-long"], "auth": "Bearer key", "local": False},
    {"slug": "glm", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "models": ["glm-4-plus", "glm-4-flash", "glm-4v"], "auth": "Bearer key", "local": False},
    {"slug": "kimi", "name": "Moonshot Kimi", "base_url": "https://api.moonshot.cn/v1",
     "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"], "auth": "Bearer key", "local": False},
    {"slug": "ollama", "name": "Ollama 本地", "base_url": "http://127.0.0.1:11434/v1",
     "models": [], "auth": "无需 key（本地拉 /v1/models）", "local": True},
]

VALID_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
VALID_MODEL = re.compile(r"^[A-Za-z0-9._:/-]{1,120}$")


class ProviderError(RuntimeError):
    pass


# ---------- 校验 / 工具 ----------

def _valid_base_url(url):
    u = (url or "").strip()
    if not u:
        return False
    m = re.match(r"^https?://", u)
    if not m:
        return False
    host = u.split("://", 1)[1].split("/", 1)[0]
    return True


def _env_key(p):
    """环境变量取 key：按供应商 slug（<ID>_API_KEY / <name>_API_KEY），最后通用 NOVEL_BOT_API_KEY 兜底。"""
    cands = []
    pid = (p.get("id") or "").strip()
    if pid:
        cands.append(f"{pid.upper()}_API_KEY")
    slugname = re.sub(r"[^a-z0-9]+", "_", (p.get("name") or "").lower()).strip("_")
    if slugname:
        cands.append(f"{slugname.upper()}_API_KEY")
    cands.append("NOVEL_BOT_API_KEY")
    for c in cands:
        v = (os.environ.get(c) or "").strip()
        if v:
            return v
    return ""


def _local_key_file(p):
    pid = (p.get("id") or "").strip()
    return os.path.join(SECRETS_DIR, f"{pid}.key") if pid else ""


def _resolve_key(p):
    """返回该供应商可用 API key。优先级：inline → 环境变量 → 本地 secrets/<id>.key → 旧 api_key_file。
    任何来源取到即用；全没有则空串（前端据此提示绑定，绝不崩溃）。"""
    key = (p.get("api_key") or "").strip()
    if key:
        return key
    key = _env_key(p)
    if key:
        return key
    lf = _local_key_file(p)
    if lf and os.path.exists(lf):
        try:
            key = open(lf, encoding="utf-8").read().strip()
            if key:
                return key
        except Exception:
            pass
    kf = (p.get("api_key_file") or "").strip()
    if kf and os.path.exists(kf):
        try:
            key = open(kf, encoding="utf-8").read().strip()
            if key:
                return key
        except Exception:
            pass
    return ""


def key_of(p):
    """public：返回该供应商可用的明文 API key（供 llm 客户端组 header 用）。"""
    return _resolve_key(p)


def _mask_key(p):
    key = _resolve_key(p)
    if not key:
        return "未设置 key（网页绑定 / 环境变量 %s_API_KEY / secrets/%s.key 任一）" % \
               ((p.get("id") or "<供应商>"), (p.get("id") or "<id>"))
    return "已设置（%s…%s）" % (key[:3], key[-2:]) if len(key) > 8 else "已设置"


def _public(p):
    """返回可安全下发给前端的 provider 视图（打码 key，不泄露明文）。"""
    out = dict(p)
    out["key_display"] = _mask_key(p)
    out["has_key"] = bool(_resolve_key(p))
    # 不把明文 key / 文件路径泄露
    out.pop("api_key", None)
    out.pop("api_key_file", None)
    return out


# ---------- 存储 / 迁移 ----------

def _load_raw():
    if not os.path.exists(PROVIDERS_FILE):
        return None
    with open(PROVIDERS_FILE, encoding="utf-8") as f:
        return json.load(f)


def _migrate_from_config():
    """旧 config.json.llm -> 一条默认 active 供应商，保持旧行为。"""
    llm = {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            llm = json.load(f).get("llm", {}) or {}
    except Exception:
        llm = {}
    model = llm.get("model", "default-model")
    base = (llm.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    name = llm.get("name") or "默认供应商"
    slug = VALID_SLUG.fullmatch(llm.get("provider_slug") or "") and llm["provider_slug"] \
        or "default"
    prov = {
        "id": slug, "name": name, "preset": True, "active": True,
        "base_url": base,
        "api_key": llm.get("api_key", ""), "api_key_file": llm.get("api_key_file", ""),
        "models": [model] if model else [], "default_model": model,
        "temperature": llm.get("temperature", 0.85),
        "max_tokens": llm.get("max_tokens", 12000),
        "timeout": llm.get("timeout", 900),
    }
    return {"version": 2, "default_provider": slug, "providers": [prov]}


def load_data():
    """读 providers.json；缺失则迁移并落盘。返回 dict。"""
    with _LOCK:
        data = _load_raw()
        if data is None:
            data = _migrate_from_config()
            _write(data)
        # 兜底：default_provider 指向的 provider 若不存在/不 active，指到第一条 active
        dps = data.get("default_provider")
        actives = [p for p in data["providers"] if p.get("active")]
        if dps not in {p["id"] for p in actives} and actives:
            data["default_provider"] = actives[0]["id"]
        return data


def _write(data):
    with open(PROVIDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _next_id(data, name):
    base = slugify(name) or "provider"
    taken = {p["id"] for p in data["providers"]}
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:40] or None


def _require_id(data, pid):
    for p in data["providers"]:
        if p["id"] == pid:
            return p
    raise ProviderError("供应商不存在：%s" % pid)


# ---------- 查询 ----------

def list_providers():
    data = load_data()
    return {"default_provider": data.get("default_provider"),
            "providers": [_public(p) for p in data["providers"]],
            "presets": PRESETS}


def get_models(pid):
    data = load_data()
    p = _require_id(data, pid)
    return p.get("models", [])


# ---------- 写操作 ----------

def add_provider(payload):
    """新增/绑定一个供应商。payload 可含 preset slug 或完整自定义字段。"""
    payload = payload or {}
    with _LOCK:
        data = load_data()
        preset_slug = payload.get("preset")
        preset = next((x for x in PRESETS if x["slug"] == preset_slug), None) if preset_slug else None

        if preset is not None and not payload.get("base_url"):
            p = {
                "id": payload.get("id") or _next_id(data, preset["slug"]),
                "name": payload.get("name") or preset["name"],
                "preset": True, "local": preset["local"],
                "base_url": preset["base_url"],
                "api_key": payload.get("api_key", ""),
                "api_key_file": payload.get("api_key_file", ""),
                "models": [m for m in (payload.get("models") or preset["models"])],
                "default_model": (payload.get("models") or preset["models"] or [""])[0],
                "temperature": payload.get("temperature", 0.85),
                "max_tokens": payload.get("max_tokens", 12000),
                "timeout": payload.get("timeout", 900),
                "active": payload.get("active", True),
            }
        else:
            base_url = (payload.get("base_url") or "").rstrip("/")
            if not _valid_base_url(base_url):
                raise ProviderError("base_url 非法（必须是 http/https 或本地 http://127.0.0.1）")
            p = {
                "id": payload.get("id") or "",
                "name": payload.get("name") or "自定义供应商",
                "preset": False, "local": "127.0.0.1" in base_url or "localhost" in base_url,
                "base_url": base_url,
                "api_key": payload.get("api_key", ""),
                "api_key_file": payload.get("api_key_file", ""),
                "models": [m for m in (payload.get("models") or []) if VALID_MODEL.fullmatch(str(m))],
                "default_model": "",
                "temperature": payload.get("temperature", 0.85),
                "max_tokens": payload.get("max_tokens", 12000),
                "timeout": payload.get("timeout", 900),
                "active": payload.get("active", True),
            }
        # 去重模型、设 default_model
        p["models"] = _dedupe([m for m in p["models"] if m])
        if not p["models"]:
            p["default_model"] = ""
        else:
            p["default_model"] = payload.get("default_model") or p["models"][0]
            if p["default_model"] not in p["models"]:
                p["default_model"] = p["models"][0]
        # id 唯一
        if not p["id"]:
            p["id"] = _next_id(data, p["name"])
        elif not VALID_SLUG.fullmatch(p["id"]):
            raise ProviderError("id 非法（小写字母/数字/中划线）")
        elif any(x["id"] == p["id"] for x in data["providers"]):
            raise ProviderError("id 已被占用：%s" % p["id"])
        data["providers"].append(p)
        # 第一个供应商直接设为默认
        if data["default_provider"] in (None, ""):
            data["default_provider"] = p["id"]
        _write(data)
    return _public(p)


def update_provider(pid, payload):
    payload = payload or {}
    with _LOCK:
        data = load_data()
        p = _require_id(data, pid)
        allowed = {"name", "api_key", "api_key_file", "temperature", "max_tokens",
                   "timeout", "active"}
        for k in allowed:
            if k in payload:
                p[k] = payload[k]
        if "base_url" in payload and payload["base_url"]:
            if not _valid_base_url(payload["base_url"]):
                raise ProviderError("base_url 非法")
            p["base_url"] = payload["base_url"].rstrip("/")
        if "models" in payload:
            p["models"] = _dedupe([m for m in (payload["models"] or []) if m and VALID_MODEL.fullmatch(str(m))])
            if not p["models"]:
                p["default_model"] = ""
            elif p.get("default_model") not in p["models"]:
                p["default_model"] = p["models"][0]
        if "default_model" in payload and payload["default_model"] in p.get("models", []):
            p["default_model"] = payload["default_model"]
        _write(data)
    return _public(p)


def remove_provider(pid):
    with _LOCK:
        data = load_data()
        _require_id(data, pid)
        actives = [p for p in data["providers"] if p.get("active") and p["id"] != pid]
        if not actives and all(p.get("active") for p in data["providers"]):
            raise ProviderError("不能删除唯一的 active 供应商；请先启用其它供应商")
        if pid in {p["id"] for p in actives} and not actives:
            # 删的是默认但不唯一 active：交给 default 兜底
            pass
        data["providers"] = [p for p in data["providers"] if p["id"] != pid]
        if data.get("default_provider") == pid:
            data["default_provider"] = (actives[0]["id"] if actives else
                                       (data["providers"][0]["id"] if data["providers"] else ""))
        _write(data)
    return {"removed": pid, "default_provider": data.get("default_provider")}


def set_default(pid):
    with _LOCK:
        data = load_data()
        p = _require_id(data, pid)
        if not p.get("active"):
            p["active"] = True  # 设为默认即激活
        data["default_provider"] = pid
        _write(data)
    return data.get("default_provider")


def set_active(pid, active):
    with _LOCK:
        data = load_data()
        p = _require_id(data, pid)
        p["active"] = bool(active)
        if not active and data.get("default_provider") == pid:
            alt = [x for x in data["providers"] if x.get("active")]
            data["default_provider"] = (alt[0]["id"] if alt else "")
        _write(data)
    return _public(p)


def _dedupe(lst):
    seen, out = set(), []
    for x in lst:
        x = str(x).strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------- 探测 / 拉模型 ----------

def _client_timeout(p):
    return p.get("timeout", 900)


def fetch_models(pid):
    """拉 GET /models。成功返回模型 id 列表；失败返回 None（不阻塞）。"""
    data = load_data()
    p = _require_id(data, pid)
    key = _resolve_key(p)
    url = p["base_url"].rstrip("/") + "/models"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer %s" % key
    try:
        with httpx.Client(timeout=20) as c:
            r = c.get(url, headers=headers)
            if r.status_code != 200:
                return None
            j = r.json()
            ids = [m.get("id") for m in j.get("data", []) if m.get("id")]
            return [i for i in ids if i] or None
    except Exception:
        return None


def test_provider(pid, probe_chat=True):
    """探活：先拉 /models；不行就用极短 chat（max_tokens=8）探活。返回 {ok, detail}。"""
    data = load_data()
    p = _require_id(data, pid)
    key = _resolve_key(p)
    base = p["base_url"].rstrip("/")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer %s" % key
    # 1) /models
    try:
        with httpx.Client(timeout=15) as c:
            r = c.get(base + "/models", headers=headers)
            if r.status_code == 200:
                j = r.json()
                return {"ok": True, "detail": "已连通，模型数 %d" % len(j.get("data", [])),
                        "models": [m.get("id") for m in j.get("data", []) if m.get("id")]}
            if r.status_code in (401, 403):
                return {"ok": False, "detail": "鉴权失败（%d），请检查 API key" % r.status_code}
    except Exception as e:
        return {"ok": False, "detail": "无法连接：%s" % str(e)[:160]}
    # 2) 极短 chat
    model = p.get("default_model") or (p.get("models") or ["gpt-4o"])[0]
    if probe_chat:
        try:
            with httpx.Client(timeout=20) as c:
                r = c.post(base + "/chat/completions", headers=headers,
                            json={"model": model, "max_tokens": 8,
                                 "messages": [{"role": "user", "content": "ping"}]})
                if r.status_code == 200:
                    return {"ok": True, "detail": "已连通（chat 探活成功，模型 %s）" % model}
                return {"ok": False, "detail": "chat 探活 %d：%s" % (r.status_code, r.text[:140])}
        except Exception as e:
            return {"ok": False, "detail": "chat 探活失败：%s" % str(e)[:160]}
    return {"ok": False, "detail": "/models 不可用且未开 chat 探活"}


# ---------- 解析（供 llm 用） ----------

def resolve(cfg=None):
    """-> (provider, model)。安全回落，绝不因没配 provider 而裸抛。
    创作 cfg 带 _provider_id/_model 就用它；否则 default_provider；再否则第一条 active。"""
    data = load_data()
    provs = data["providers"]
    by_id = {p["id"]: p for p in provs}

    def _pick(pid, model=None):
        p = by_id.get(pid)
        if not p:
            return None
        m = model or p.get("default_model") or (p.get("models") or [""])[0]
        return p, m

    # 1) cfg 显式指定
    if cfg:
        pid = cfg.get("_provider_id")
        if pid:
            r = _pick(pid, cfg.get("_model"))
            if r:
                return r
            # provider 已被删 → 落到默认
        elif cfg.get("_model"):
            # 只有模型没供应商：尝试匹配到能出该模型的 active 供应商
            for p in provs:
                if p.get("active") and cfg["_model"] in p.get("models", []):
                    return p, cfg["_model"]

    # 2) default_provider
    dp = data.get("default_provider")
    r = _pick(dp) if dp else None
    if r and r[0].get("active"):
        return r
    # 3) 第一条 active
    for p in provs:
        if p.get("active"):
            m = p.get("default_model") or (p.get("models") or [""])[0]
            return p, m
    raise ProviderError("尚未绑定任何可用的模型供应商，请先绑定一个供应商")


def stage_resolve(cfg, stage):
    """P4 按环节选模型：优先读运行期 cfg 的 defaults.stage_models[stage]（含 cfg_overrides），
    缺失再回落磁盘 config.json；命中则用该环节指定的供应商/模型，否则回落默认 resolve(cfg)。
    stage ∈ planning/writing/review。"""
    sm = {}
    try:
        sm = ((cfg or {}).get("defaults") or {}).get("stage_models") or {}
    except Exception:
        sm = {}
    if not sm:
        import json as _json, os as _os
        try:
            sm = _json.load(open(_os.path.join(ROOT, "config.json"), encoding="utf-8")).get("defaults", {}).get("stage_models", {}) or {}
        except Exception:
            sm = {}
    pick = sm.get(stage) or {}
    if pick.get("model") or pick.get("provider_id"):
        c2 = dict(cfg or {})
        c2["_provider_id"] = pick.get("provider_id") or c2.get("_provider_id")
        c2["_model"] = pick.get("model") or c2.get("_model")
        try:
            return resolve(c2)
        except ProviderError:
            pass
    return resolve(cfg)

def active_model_options():
    """给前端的 供应商->模型 选择树。"""
    data = load_data()
    out = []
    for p in data["providers"]:
        if not p.get("active"):
            continue
        out.append({"id": p["id"], "name": p["name"],
                    "models": p.get("models", []), "default_model": p.get("default_model", "")})
    return out
