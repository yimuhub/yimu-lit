"""OpenAI 兼容 LLM 客户端（走本地 new-api 或 Agnes 直连）"""
import json, os, time
import httpx

_cfg_cache = None

def load_config():
    global _cfg_cache
    if _cfg_cache is None:
        with open(os.path.join(os.path.dirname(__file__), "..", "config.json")) as f:
            _cfg_cache = json.load(f)
    return _cfg_cache

def _api_key(cfg):
    llm = cfg["llm"]
    key = llm.get("api_key") or ""
    if not key and llm.get("api_key_file"):
        p = llm["api_key_file"]
        if os.path.exists(p):
            with open(p) as f:
                key = f.read().strip()
    return key

class LLMError(RuntimeError):
    pass

def chat(messages, json_mode=False, temperature=None, max_tokens=None, retries=3):
    """一次性 chat 调用，返回文本。带简单重试。"""
    cfg = load_config()
    llm = cfg["llm"]
    key = _api_key(cfg)
    body = {
        "model": llm["model"],
        "messages": messages,
        "temperature": (llm.get("temperature", 0.85) if temperature is None else temperature),
        "max_tokens": max_tokens or llm.get("max_tokens", 12000),
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    last_err = None
    for i in range(retries):
        try:
            with httpx.Client(timeout=llm.get("timeout", 900)) as client:
                r = client.post(llm["base_url"].rstrip("/") + "/chat/completions",
                                json=body, headers=headers)
                if r.status_code == 429:
                    time.sleep(5 * (i + 1))
                    last_err = LLMError(f"429 限流: {r.text[:200]}")
                    continue
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]["content"]
        except LLMError as e:
            last_err = e
        except Exception as e:
            last_err = LLMError(str(e))
            time.sleep(3)
    raise last_err

def chat_json(messages, temperature=None):
    """调用并解析 JSON（容忍模型夹带 markdown 代码块）"""
    raw = chat(messages, json_mode=True, temperature=temperature)
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
