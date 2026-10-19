"""OpenAI 兼容 LLM 客户端：按任务所选供应商/模型调用（多供应商支持）

未带 cfg 时安全回落到默认 active 供应商，不破坏旧行为。
"""
import json, sys, time
import httpx
from . import providers

class LLMError(RuntimeError):
    pass


def _trace_prov(prov, model):
    """可观测性：记录每次调用命中的供应商/模型（stderr，不影响返回）。"""
    print(f"[llm] provider={prov.get('id')} model={model} base={prov.get('base_url')}",
          file=sys.stderr, flush=True)

def chat(messages, json_mode=False, temperature=None, max_tokens=None, retries=3, cfg=None):
    """一次性 chat 调用，返回文本。带简单重试。cfg 决定用哪个供应商/模型。"""
    prov, model = providers.resolve(cfg)
    _trace_prov(prov, model)
    key = providers.key_of(prov)
    base = prov["base_url"].rstrip("/")
    body = {
        "model": model,
        "messages": messages,
        "temperature": (temperature if temperature is not None else prov.get("temperature", 0.85)),
        "max_tokens": max_tokens or prov.get("max_tokens", 12000),
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer %s" % key
    last_err = None
    for i in range(retries):
        try:
            with httpx.Client(timeout=prov.get("timeout", 900)) as client:
                r = client.post(base + "/chat/completions",
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

def chat_json(messages, temperature=None, cfg=None):
    """调用并解析 JSON（容忍模型夹带 markdown 代码块）。cfg 决定供应商/模型。"""
    raw = chat(messages, json_mode=True, temperature=temperature, cfg=cfg)
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
