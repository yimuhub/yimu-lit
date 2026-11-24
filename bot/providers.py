"""模型供应商管理层：自定义/预设一键绑定、多供应商并存、按任务选模型。

- 存储：应用根目录 providers.json（与 config.json 同层）
- 迁移：首启 providers.json 缺失时，从旧 config.json 的 llm 块迁成默认 active 供应商
- 解析：resolve(cfg) -> (provider, model)。创作配置带 _provider_id/_model 就用它，
        否则回落到 default_provider（必须 active），再否则第一条 active。全程安全回落。
- 并发安全：resolve 每次从 cfg 现读，不维护模块级"当前供应商"，避免多任务并写串台。
- 密钥：只存本地（inline 或 api_key_file），不外发、不进日志；展示打码。
- v5.9.0 文本/图像双区：每条 provider 带 kind（"text" | "image"，缺省视为 text，
  load_data 一次性补全）。文本链路（resolve/usable/active_model_options/global_model/
  fallback_config）只认 kind=text 条目，绝不落到 image 条目；图像链路走 image_resolve()
  （default_image_provider → 第一条 active 的 kind=image 条目）。双默认槽互相独立：
  default_provider（文本）+ default_image_provider（图像）。同一 base_url 可建两条
  不同 kind 的条目（如「硅基流动-文本 / 硅基流动-图像」）。v5.7.0 的顶层 image_provider
  小节由 _migrate_v59 自动迁成一条 kind=image 条目（幂等，旧小节保留只读不删，
  回滚到旧版本仍可用；save_image_provider 兼容写时同步镜像回旧小节）。
"""
import json, os, re, threading, httpx

from .safeio import atomic_write_json


def _sanitize_proxy_env():
    """httpx 0.28.1 解析 NO_PROXY/no_proxy 里的带方括号 IPv6（[::1]）会抛
    Invalid port: ':1]'，进程内所有模型调用随之失败（实测 macOS 默认环境即命中）。
    去掉方括号语义不变（[::1] → ::1），在模块加载时一次性修正。"""
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if not raw or "[" not in raw:
            continue
        parts = []
        for item in raw.split(","):
            s = item.strip()
            if s.startswith("[") and s.endswith("]"):
                s = s[1:-1]
            if s:
                parts.append(s)
        os.environ[name] = ",".join(parts)


_sanitize_proxy_env()

from .paths import DATA_ROOT as ROOT
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
        "kind": "text",
    }
    return {"version": 2, "default_provider": slug, "providers": [prov]}


def _migrate_v59(data):
    """v5.9.0 数据迁移（幂等，有变更即原子落盘）：
      1) kind 补全：providers[] 每条缺 kind / kind 非法 → 视为 "text"；
      2) v5.7.0 顶层 image_provider 小节 → 迁成一条 kind=image 的 provider 条目
         （保留 base_url/api_key/model→default_model/size）。旧小节原样保留只读不删
         （回滚到 v5.7.0 仍可读）；幂等靠 image_provider_migrated 标记——标记缺失时
         尝试一次迁移并把标记置真（旧小节为空也置真，防止兼容镜像被二次迁移）。
    """
    changed = False
    provs = data.get("providers")
    if not isinstance(provs, list):
        provs = []
        data["providers"] = provs
        changed = True
    for p in provs:
        if not isinstance(p, dict):
            continue
        if p.get("kind") not in ("text", "image"):
            p["kind"] = "text"
            changed = True
        if p.get("kind") == "image":
            raw_models = p.get("models")
            if isinstance(raw_models, str):
                raw_models = [raw_models]
            elif not isinstance(raw_models, list):
                raw_models = []
            models = [str(m).strip() for m in raw_models
                      if str(m).strip() and VALID_MODEL.fullmatch(str(m).strip())]
            if p.get("models") != models:
                p["models"] = models
                changed = True
            default = str(p.get("default_model") or "").strip()
            if default and not VALID_MODEL.fullmatch(default):
                p["default_model"] = ""
                changed = True
    if data.get("image_provider_migrated") is not True:
        legacy = data.get("image_provider")
        if isinstance(legacy, dict) and (str(legacy.get("base_url") or "").strip()
                                         or str(legacy.get("model") or "").strip()):
            entry = {
                "id": _next_id(data, "image"),
                "name": "图像供应商（迁移）",
                "kind": "image", "preset": False,
                "active": bool(legacy.get("active", True)),
                "base_url": str(legacy.get("base_url") or "").strip().rstrip("/"),
                "api_key": str(legacy.get("api_key") or ""),
                "api_key_file": str(legacy.get("api_key_file") or ""),
                "models": ([str(legacy.get("model")).strip()]
                           if str(legacy.get("model") or "").strip() else []),
                "default_model": str(legacy.get("model") or "").strip(),
                "size": str(legacy.get("size") or "").strip() or DEFAULT_IMAGE_SIZE,
            }
            if not _valid_base_url(entry["base_url"]):
                entry["base_url"] = ""      # 旧数据坏了就置空，让用户在新 UI 里重填，绝不抛错
            if entry["default_model"] and not VALID_MODEL.fullmatch(entry["default_model"]):
                entry["default_model"] = ""
            entry["models"] = [m for m in entry["models"]
                               if m and VALID_MODEL.fullmatch(m)]
            provs.append(entry)
            if not data.get("default_image_provider"):
                data["default_image_provider"] = entry["id"]
        data["image_provider_migrated"] = True
        changed = True
    if changed:
        _write(data)
    return data


def load_data():
    """读 providers.json；缺失则迁移并落盘。返回 dict（保证 kind 已补全）。"""
    with _LOCK:
        data = _load_raw()
        if data is None:
            data = _migrate_from_config()
        data = _migrate_v59(data)
        # 兜底：default_provider 指向的 provider 若不存在/不 active/不是文本类，
        # 指到第一条 active 的文本供应商（图像条目绝不占文本默认槽）。
        dps = data.get("default_provider")
        actives = [p for p in data["providers"] if p.get("active") and p.get("kind") != "image"]
        if dps not in {p["id"] for p in actives} and actives:
            data["default_provider"] = actives[0]["id"]
        return data


def _write(data):
    atomic_write_json(PROVIDERS_FILE, data)


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
            "default_image_provider": data.get("default_image_provider") or "",
            "providers": [_public(p) for p in data["providers"]],
            "presets": PRESETS}


def get_models(pid):
    data = load_data()
    p = _require_id(data, pid)
    return p.get("models", [])


def usable():
    """是否存在「能真正发一次请求」的文本供应商——只读判断，不落盘、不发网络请求。

    口径与 resolve() 对齐（有没有 active 的 kind=text 供应商），但额外要求 base_url
    合法、至少有一个模型名，否则请求必然失败。**不把「没有 API key」当成不可用**：
    本地 Ollama / 自建免鉴权网关合法地不需要 key，误判会让这类用户永远走兜底。
    真的发请求后失败（401/超时等）仍按真实错误上报，不在这里吞掉。
    """
    try:
        data = _load_raw()
        if data is None:
            # 与 load_data 的迁移口径一致，但只在内存里算，不写 providers.json
            data = _migrate_from_config()
        provs = (data or {}).get("providers") or []
    except Exception:
        return False
    for p in provs:
        if not isinstance(p, dict) or not p.get("active") or p.get("kind") == "image":
            continue
        if not _valid_base_url(p.get("base_url")):
            continue
        if p.get("default_model") or p.get("models"):
            return True
    return False


# ---------- 写操作 ----------

def add_provider(payload):
    """新增/绑定一个供应商。payload 可含 preset slug 或完整自定义字段。

    v5.9.0：payload.kind 决定条目类型（"text" | "image"，缺省/非法一律按 "text"）。
    图像条目（kind=image）额外存 size（出图尺寸）；同一 base_url 可建两条不同 kind
    的条目（如「硅基流动-文本 / 硅基流动-图像」），模型名放 models[0]/default_model。
    """
    payload = payload or {}
    kind = str(payload.get("kind") or "text").strip()
    if kind not in ("text", "image"):
        kind = "text"
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
                "kind": "text",           # 预设库都是文本对话模型
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
                "kind": kind,
            }
            if kind == "image":
                # 图像条目：出图尺寸（宽x高）；不参与文本对话链路
                size = str(payload.get("size") or "").strip()
                if size and not SIZE_RE.fullmatch(size):
                    raise ProviderError("尺寸非法（形如 1024x1536）")
                p["size"] = size or DEFAULT_IMAGE_SIZE
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
        # 第一个供应商直接设为默认（双默认槽按 kind 各归各位，互不干扰）
        if kind == "image":
            if not data.get("default_image_provider"):
                data["default_image_provider"] = p["id"]
        elif data.get("default_provider") in (None, ""):
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
            if k not in payload:
                continue
            # v4.19.1 兜底：密钥不回显，前端「留空=不修改」；空串/空白一律视为不修改，
            # 绝不能把已保存的密钥覆盖成空（一旦清掉，所有模型调用立即 401）。
            if k in ("api_key", "api_key_file") and not str(payload.get(k) or "").strip():
                continue
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
        # v5.9.0：图像条目的出图尺寸（宽x高）；kind 建好后不可改（换类型=另建一条）
        if "size" in payload:
            size = str(payload.get("size") or "").strip()
            if size and not SIZE_RE.fullmatch(size):
                raise ProviderError("尺寸非法（形如 1024x1536）")
            if size:
                p["size"] = size
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
        # v5.9.0 双默认槽：文本默认只回落到剩余 active 文本条目（绝不指到图像条目）；
        # 图像默认回落到剩余 active 图像条目。没有同类条目就置空（对应链路给出引导）。
        text_actives = [p for p in data["providers"]
                        if p.get("active") and p.get("kind") != "image"]
        img_actives = [p for p in data["providers"]
                       if p.get("active") and p.get("kind") == "image"]
        if data.get("default_provider") == pid:
            data["default_provider"] = text_actives[0]["id"] if text_actives else ""
        if data.get("default_image_provider") == pid:
            data["default_image_provider"] = img_actives[0]["id"] if img_actives else ""
        _write(data)
    return {"removed": pid, "default_provider": data.get("default_provider"),
            "default_image_provider": data.get("default_image_provider") or ""}


def set_default(pid):
    """设为默认（v5.9.0 起按条目 kind 归槽）：文本条目 → default_provider，
    图像条目 → default_image_provider。设为默认即激活。两个默认槽互不干扰。"""
    with _LOCK:
        data = load_data()
        p = _require_id(data, pid)
        if not p.get("active"):
            p["active"] = True  # 设为默认即激活
        if p.get("kind") == "image":
            data["default_image_provider"] = pid
        else:
            data["default_provider"] = pid
        _write(data)
    return data.get("default_provider") if p.get("kind") != "image" else data.get("default_image_provider")


def set_active(pid, active):
    with _LOCK:
        data = load_data()
        p = _require_id(data, pid)
        p["active"] = bool(active)
        if not active and data.get("default_provider") == pid:
            alt = [x for x in data["providers"]
                   if x.get("active") and x.get("kind") != "image"]
            data["default_provider"] = (alt[0]["id"] if alt else "")
        if not active and data.get("default_image_provider") == pid:
            alt_img = [x for x in data["providers"]
                       if x.get("active") and x.get("kind") == "image"]
            data["default_image_provider"] = (alt_img[0]["id"] if alt_img else "")
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
    """-> (provider, model)。文本链路只在 kind=text 条目里解析。
    严格作品绑定（_model_bound）缺失、停用、改为图像或模型下架时明确报错，绝不静默换模型；
    旧配置没有严格标记时继续沿用原有的安全回落策略。"""
    data = load_data()
    if cfg and cfg.get("_model_bound"):
        pid = str(cfg.get("_provider_id") or "")
        bound = next((p for p in data["providers"] if p.get("id") == pid), None)
        if not bound:
            raise ProviderError(f"作品绑定的供应商不存在或已被删除：{pid or '未指定'}")
        if bound.get("kind") == "image":
            raise ProviderError("作品绑定的是图像供应商，文本创作必须改用文本供应商")
        if not bound.get("active"):
            raise ProviderError(f"作品绑定的供应商已停用：{bound.get('name') or pid}")
        model = str(cfg.get("_model") or "")
        if not model or model not in (bound.get("models") or []):
            raise ProviderError(f"作品绑定的模型不在供应商模型列表中：{model or '未指定'}")
        return bound, model

    provs = [p for p in data["providers"] if p.get("kind") != "image"]
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
            # provider 已被删 / 是图像条目 → 落到默认
        elif cfg.get("_model"):
            # 只有模型没供应商：尝试匹配到能出该模型的 active 文本供应商
            for p in provs:
                if p.get("active") and cfg["_model"] in p.get("models", []):
                    return p, cfg["_model"]

    # 2) default_provider
    dp = data.get("default_provider")
    r = _pick(dp) if dp else None
    if r and r[0].get("active"):
        return r
    # 3) 第一条 active 文本供应商
    for p in provs:
        if p.get("active"):
            m = p.get("default_model") or (p.get("models") or [""])[0]
            return p, m
    raise ProviderError("尚未绑定任何可用的模型供应商，请先绑定一个供应商")


def fallback_config(cfg=None, *, exclude_provider: str = "", exclude_model: str = ""):
    """429 降级用的下一组供应商/模型。只返回 active 且不同于当前的一组；无则 None。

    这是“单供应商被限流时不把整本书写崩”的最低保障：调用方拿到 cfg 后递归 chat，
    由正常路径重新走超时/重试；不会改动用户默认设置。
    """
    if cfg and cfg.get("_model_bound"):
        return None
    data = load_data()
    provs = [p for p in (data.get("providers") or []) if p.get("kind") != "image"]
    for p in provs:
        if not p.get("active"):
            continue
        if exclude_provider and p.get("id") == exclude_provider:
            continue
        model = p.get("default_model") or (p.get("models") or [""])[0]
        if model and model != exclude_model:
            return {"_provider_id": p.get("id"), "_model": model}
    return None


def stage_resolve(cfg, stage):
    """P4 按环节选模型：优先读运行期 cfg 的 defaults.stage_models[stage]（含 cfg_overrides），
    缺失再回落磁盘 config.json；命中则用该环节指定的供应商/模型，否则回落默认 resolve(cfg)。
    stage ∈ planning/writing/review。严格作品绑定优先于所有岗位和环节规则。"""
    if cfg and cfg.get("_model_bound"):
        return resolve(cfg)
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

# ---------- v0.5 M4：子 Agent 独立配模型 + 并发/限流守卫 ----------

def agent_resolve(cfg, agent=None, stage=None):
    """v0.5 M4：子 Agent 模型绑定优先级 —— agent_models[agent] > stage_models[stage] > 默认 resolve。
    让不同岗位（创作/审稿/重写/规划）挂不同供应商/模型，把负载摊开。
    严格作品绑定优先于所有岗位和环节规则。"""
    if cfg and cfg.get("_model_bound"):
        return resolve(cfg)
    if agent:
        pick = (((cfg or {}).get("meta") or {}).get("agent_models") or {}).get(agent) or {}
        if not pick:
            import json as _json, os as _os
            try:
                pick = (_json.load(open(_os.path.join(ROOT, "config.json"), encoding="utf-8"))
                        .get("defaults", {}).get("agent_models", {})).get(agent) or {}
            except Exception:
                pick = {}
        if pick.get("model") or pick.get("provider_id"):
            c2 = dict(cfg or {})
            c2["_provider_id"] = pick.get("provider_id") or c2.get("_provider_id")
            c2["_model"] = pick.get("model") or c2.get("_model")
            try:
                return resolve(c2)
            except ProviderError:
                pass
    if stage:
        return stage_resolve(cfg, stage)
    return resolve(cfg)

class ConcurrencyGuard:
    """v0.5 M4：全局并发上限 + 每 provider 限流。多 Agent/多书并发创作改写时先排队，避免打爆单一 API（429）。"""
    import threading as _t
    _lock = _t.Lock()
    _prov = {}          # pid -> Semaphore
    _global = None
    _gmax = None
    def __init__(self, global_max=4):
        self._global_max = max(1, int(global_max))
    @classmethod
    def _g(cls, n):
        with cls._lock:
            if cls._global is None or cls._gmax != n:
                cls._global = cls._t.Semaphore(n); cls._gmax = n
            return cls._global
    @classmethod
    def _p(cls, provider):
        pid = (provider or {}).get("id") or "_default"
        with cls._lock:
            s = cls._prov.get(pid)
            if s is None:
                rpm = int((provider or {}).get("rpm") or 20)
                s = cls._prov[pid] = cls._t.Semaphore(max(1, rpm // 4))   # 在飞并发 ≈ rpm/4（保守防 429）
            return s
    def acquire(self, provider):
        g, p = self._g(self._global_max), self._p(provider)
        return _Guard(g, p)

class _Guard:
    def __init__(self, g, p): self._g, self._p = g, p
    def __enter__(self):
        self._g.acquire(timeout=600); self._p.acquire(timeout=600); return self
    def __exit__(self, *a):
        try: self._p.release()
        finally: self._g.release()


def active_model_options():
    """给前端的 供应商->模型 选择树（v5.9.0 起只含文本条目，图像条目不进创作选择器）。"""
    data = load_data()
    out = []
    for p in data["providers"]:
        if not p.get("active") or p.get("kind") == "image":
            continue
        out.append({"id": p["id"], "name": p["name"],
                    "models": p.get("models", []), "default_model": p.get("default_model", "")})
    return out


# ---------- 全局默认模型（v4.20：设置页「模型与供应商」的全局默认选择项） ----------

def global_model():
    """当前全局默认模型 {provider_id, model}（= default_provider + 其 default_model）。
    没有可用默认时返回 {"provider_id": "", "model": ""}，绝不抛异常。"""
    try:
        data = load_data()
        dp = data.get("default_provider") or ""
        p = next((x for x in data["providers"]
                  if x.get("id") == dp and x.get("kind") != "image"), None)
        if not p:
            return {"provider_id": "", "model": ""}
        model = p.get("default_model") or (p.get("models") or [""])[0] or ""
        return {"provider_id": dp, "model": model}
    except Exception:
        return {"provider_id": "", "model": ""}


def set_global_model(provider_id, model):
    """设置全局默认模型：default_provider=provider_id，且该供应商 default_model=model。
    校验：供应商必须存在且 active、必须是文本条目（图像条目请用「图像默认模型」槽）；
    model 必须在其 models 里（model 为空则取其 default_model）。
    返回 {"provider_id", "model"}；非法输入抛 ProviderError。"""
    provider_id = str(provider_id or "").strip()
    model = str(model or "").strip()
    with _LOCK:
        data = load_data()
        p = _require_id(data, provider_id)
        if p.get("kind") == "image":
            raise ProviderError("该供应商是图像类型，请改在「图像默认模型」里设置")
        if not p.get("active"):
            raise ProviderError("供应商已停用，不能设为全局默认：%s" % provider_id)
        if not model:
            model = p.get("default_model") or (p.get("models") or [""])[0]
        if not model:
            raise ProviderError("供应商 %s 没有可选模型，请先在供应商里添加模型" % provider_id)
        if model not in (p.get("models") or []):
            raise ProviderError("模型 %s 不在供应商 %s 的模型列表里" % (model, provider_id))
        set_default(provider_id)          # 设为默认（其内部会激活并落盘 default_provider）
        data2 = load_data()
        _require_id(data2, provider_id)["default_model"] = model
        _write(data2)
    return {"provider_id": provider_id, "model": model}


# ---------- v5.9.0：图像供应商（providers[] 的 kind=image 条目，AI 封面生成等图像能力） ----------
#
# 结构选择（v5.9.0 重构说明）：图像能力并入 providers[] 统一管理——每条 provider 带
# kind（"text" | "image"，**单值**；同一 base_url 可建两条不同 kind 的条目，如
# 「硅基流动-文本 / 硅基流动-图像」），不采用 "text,image" 多值：
#   · 多值 kind 要在 resolve/image_resolve/两个默认槽/UI 徽标里再拆字符串，收益只是
#     「少建一条」，却让「文本默认 / 图像默认」两个槽的候选集出现交集，配置语义变糊；
#   · 单值 kind 后两条链路的候选集天然不相交（resolve 整体剔除 image 条目、
#     image_resolve 只取 image 条目），回归好钉死，UI 一个徽标一个编辑弹窗就讲清楚。
# 图像链路解析：image_resolve()（default_image_provider → 第一条 active 的 kind=image）；
# 双默认槽互相独立：default_provider（文本）+ default_image_provider（图像）。
# v5.7.0 兼容：旧顶层 image_provider 小节由 _migrate_v59 一次性迁成 kind=image 条目
# （幂等；旧小节保留只读不删，回滚安全）；save_image_provider（webcover.py 的
# /api/library/cover/ai/config POST 兼容入口）写盘时把生效条目镜像回旧小节。
# 图像 HTTP 调用在 webcover.py；本模块只管存储/校验/解析/探活。

DEFAULT_IMAGE_SIZE = "1024x1536"      # 封面竖版默认尺寸
SIZE_RE = re.compile(r"^\d{2,4}x\d{2,4}$")
IMAGE_ENV_KEYS = ("IMAGE_API_KEY", "NOVEL_BOT_IMAGE_API_KEY")


def _image_entries(data):
    """providers[] 里的图像条目（保持文件顺序）。"""
    return [p for p in (data.get("providers") or [])
            if isinstance(p, dict) and p.get("kind") == "image"]


def _image_model_of(p) -> str:
    return str(p.get("default_model") or "").strip() or \
        ((p.get("models") or [""])[0] if p.get("models") else "")


def image_resolve():
    """图像链路解析 -> (provider, model) 或 None。
    顺序：default_image_provider（必须 active）→ 第一条 active 的 kind=image 条目。
    与文本链路互不串台：resolve() 在候选集里整体剔除 image 条目。"""
    data = load_data()
    imgs = [p for p in _image_entries(data) if p.get("active")]
    dp = data.get("default_image_provider")
    for p in imgs:
        if p.get("id") == dp:
            return p, _image_model_of(p)
    if imgs:
        p = imgs[0]
        return p, _image_model_of(p)
    return None


def _image_key_of(p) -> str:
    """图像条目可用 key：inline → 环境变量（<id>/<name> 口径）→ 本地 key 文件 →
    IMAGE_API_KEY / NOVEL_BOT_IMAGE_API_KEY 兜底（v5.7.0 环境变量用户无缝续用）。"""
    key = _resolve_key(p)
    if not key:
        for env in IMAGE_ENV_KEYS:
            key = (os.environ.get(env) or "").strip()
            if key:
                break
    return key


def image_provider_runtime() -> dict:
    """服务端内部视图（含明文 key，只进请求 header，绝不外发/进日志/下发前端）。"""
    pair = image_resolve()
    if not pair:
        return {"base_url": "", "model": "", "size": DEFAULT_IMAGE_SIZE,
                "api_key": "", "configured": False}
    p, model = pair
    base = str(p.get("base_url") or "").strip().rstrip("/")
    if base and not _valid_base_url(base):
        base = ""
    return {"base_url": base, "model": model, "size": p.get("size") or DEFAULT_IMAGE_SIZE,
            "api_key": _image_key_of(p),
            "configured": bool(base and model)}


def _mask_or_hint(key: str) -> str:
    if key:
        return "已设置（%s…%s）" % (key[:3], key[-2:]) if len(key) > 8 else "已设置"
    return "未设置（可留空：本地免鉴权接口）"


def _image_entry_public(p) -> dict:
    """图像条目给前端的安全视图（key 打码；model 取生效图像模型名）。"""
    out = _public(p)
    out["model"] = _image_model_of(p)
    return out


def image_provider() -> dict:
    """v5.7.0 兼容视图：当前生效（默认）图像供应商的打码字段（封面弹窗无配置判断用）。"""
    rt = image_provider_runtime()
    return {"configured": rt["configured"], "base_url": rt["base_url"],
            "model": rt["model"], "size": rt["size"],
            "has_key": bool(rt["api_key"]), "key_display": _mask_or_hint(rt["api_key"])}


def image_config_view() -> dict:
    """GET /api/library/cover/ai/config 的完整视图：
    v5.7.0 兼容字段（configured/base_url/model/size/has_key/key_display，前端弹窗
    只看 configured）+ v5.9.0 新增（default_image_provider + kind=image 条目列表）。"""
    data = load_data()
    base = image_provider()
    return {"ok": True, **base,
            "default_image_provider": data.get("default_image_provider") or "",
            "providers": [_image_entry_public(p) for p in _image_entries(data)]}


def save_image_provider(payload) -> dict:
    """保存图像供应商配置（v5.9.0：写到 providers[] 的 kind=image 条目）。

    目标条目选择：payload.provider_id（必须存在且是图像条目）→ default_image_provider
    → 唯一图像条目 → 新建一条（无图像条目时）。显式传 provider_id 同时把它设为
    default_image_provider（「图像默认模型」槽的保存走这里）。

    字段级语义（v5.7.0 兼容口径）：
      · base_url/model 显式传空串 = 清除该项（两者任一为空即视为未配置）；
      · api_key 留空 = 保留已保存值（与文本供应商 v4.19.1 口径一致，绝不清空密钥）；
      · size 缺省不动；显式传值必须是 宽x高 数字形状。
    写盘同时把生效条目镜像回 v5.7.0 顶层 image_provider 小节（旧小节保留只读不删，
    回滚到旧版本仍有配置可用；并置迁移标记防止镜像被二次迁移）。
    返回打码视图；非法输入抛 ProviderError。"""
    payload = payload or {}
    # 字段校验先行（与 v5.7.0 同序：base_url → model → size，只校验 payload 里出现的键）
    if "base_url" in payload:
        _base_in = str(payload.get("base_url") or "").strip().rstrip("/")
        if _base_in and not _valid_base_url(_base_in):
            raise ProviderError("base_url 非法（必须是 http/https 开头的接口地址）")
    if "model" in payload:
        _model_in = str(payload.get("model") or "").strip()
        if _model_in and not VALID_MODEL.fullmatch(_model_in):
            raise ProviderError("模型名非法（字母/数字/点/中划线）")
    if "size" in payload:
        _size_in = str(payload.get("size") or "").strip()
        if _size_in and not SIZE_RE.fullmatch(_size_in):
            raise ProviderError("尺寸非法（形如 1024x1536）")
    with _LOCK:
        data = load_data()
        imgs = _image_entries(data)
        pid = str(payload.get("provider_id") or "").strip()
        if pid:
            target = _require_id(data, pid)
            if target.get("kind") != "image":
                raise ProviderError("该供应商不是图像类型：%s" % pid)
        else:
            dp = data.get("default_image_provider")
            target = next((p for p in imgs if p.get("id") == dp), None)
            if target is None and len(imgs) == 1:
                target = imgs[0]
        if target is None:
            # 新建一条图像条目（形状与 add_provider 对齐；base_url/model 可后补，
            # 未配齐时 configured=False，走「无配置」引导）
            base = str(payload.get("base_url") or "").strip().rstrip("/")
            model = str(payload.get("model") or "").strip()
            size = str(payload.get("size") or "").strip()
            name = str(payload.get("name") or "").strip() or "图像供应商"
            raw_id = str(payload.get("id") or "").strip()
            new_id = raw_id if VALID_SLUG.fullmatch(raw_id) else \
                _next_id(data, name) if slugify(name) else _next_id(data, "image")
            target = {
                "id": new_id, "name": name, "kind": "image", "preset": False,
                "active": True, "local": "127.0.0.1" in base or "localhost" in base,
                "base_url": base,
                "api_key": str(payload.get("api_key") or ""),
                "api_key_file": "",
                "models": [model] if model else [],
                "default_model": model,
                "size": size or DEFAULT_IMAGE_SIZE,
            }
            data["providers"].append(target)
        else:
            if "base_url" in payload:
                target["base_url"] = str(payload.get("base_url") or "").strip().rstrip("/")
            if "model" in payload:
                model = str(payload.get("model") or "").strip()
                if model:
                    target["default_model"] = model
                    if model not in (target.get("models") or []):
                        target["models"] = [model] + [m for m in (target.get("models") or [])
                                                      if m != model]
                else:
                    target["default_model"] = ""
                    target["models"] = []
            if "size" in payload:
                size = str(payload.get("size") or "").strip()
                if size:
                    target["size"] = size
            key = str(payload.get("api_key") or "").strip()
            if key:
                target["api_key"] = key
        # 显式指定条目 = 同时设为图像默认（「图像默认模型」槽保存语义）；首次建条目同理
        if pid or not data.get("default_image_provider"):
            data["default_image_provider"] = target["id"]
        # v5.7.0 兼容镜像：生效（默认）图像条目回写顶层 image_provider 小节。
        eff = next((p for p in _image_entries(data)
                    if p.get("id") == data.get("default_image_provider")), None)
        if eff is None:
            imgs2 = _image_entries(data)
            eff = imgs2[0] if imgs2 else target
        data["image_provider"] = {
            "base_url": str(eff.get("base_url") or "") if eff else "",
            "api_key": str(eff.get("api_key") or "") if eff else "",
            "model": str(eff.get("default_model") or "") if eff else "",
            "size": str(eff.get("size") or DEFAULT_IMAGE_SIZE) if eff else "",
        }
        data["image_provider_migrated"] = True
        _write(data)
    return image_provider()


def test_image_provider(provider_id: str = "", size: str = "512x512", timeout: int = 30) -> dict:
    """图像接口探活：真实请求一张小尺寸测试图（b64_json）。detail 全中文。
    v5.9.0：可按条目探活（provider_id）；缺省探当前生效（默认）图像供应商。"""
    if provider_id:
        data = load_data()
        p = _require_id(data, provider_id)
        if p.get("kind") != "image":
            return {"ok": False, "detail": "该供应商不是图像类型，无法做图像探活"}
        base = str(p.get("base_url") or "").strip().rstrip("/")
        model = _image_model_of(p)
        key = _image_key_of(p)
        if not (base and model):
            return {"ok": False, "detail": "该条目未配置完整：请填写接口地址与图像模型名"}
    else:
        rt = image_provider_runtime()
        if not rt["configured"]:
            return {"ok": False, "detail": "尚未配置：请填写接口地址与图像模型名"}
        base, model, key = rt["base_url"], rt["model"], rt["api_key"]
    url = base.rstrip("/") + "/images/generations"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer %s" % key
    body = {"model": model, "prompt": "一张纯灰色方块测试图", "n": 1,
            "size": size or "512x512", "response_format": "b64_json"}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(url, headers=headers, json=body)
    except Exception as e:
        return {"ok": False, "detail": "无法连接图像接口：%s" % str(e)[:160]}
    if r.status_code == 200:
        try:
            item = (r.json().get("data") or [{}])[0]
            if item.get("b64_json") or item.get("url"):
                return {"ok": True, "detail": "已连通，返回 1 张测试图（模型 %s）" % model}
            return {"ok": False, "detail": "已连通（HTTP 200）但响应里没有图片数据，请确认该模型是图像模型"}
        except Exception:
            return {"ok": True, "detail": "已连通（HTTP 200）"}
    if r.status_code in (401, 403):
        return {"ok": False, "detail": "鉴权失败（%d），请检查 API key 是否正确" % r.status_code}
    if r.status_code == 429:
        return {"ok": False, "detail": "请求太频繁（429），请稍后再试"}
    if r.status_code == 404:
        return {"ok": False, "detail": "端点不存在（404）：请确认 base_url 以 /v1 结尾、且该模型支持 images/generations"}
    return {"ok": False, "detail": "接口返回 %d：%s" % (r.status_code, (r.text or "")[:140])}
