"""v0.3.4 风格蒸馏引擎。

借鉴 GitHub 上成熟技能的维度框架（writing-dna-skill 的 L1/L2、perfectly-replicate 的 7+1 维、
quill 的"风格 prompt + 强度 + 校准原文"思路），**自研精简实现**：不引入外部依赖、不装外部 skill。

核心：给一段小说文本（粘贴）或若干小说网址（抓取节选）→ LLM 按 7 维分析「作者的写作风格技法」
（不是剧情内容）→ 输出结构化风格卡片 JSON + 一段可直接注入 craft 的风格 prompt → 存 styles/{name}/。

版权红线：只提风格/技法，不复述、不搬运原文文字；产物标注来源；网址抓取只取节选做分析、不留全文。
"""
import os, re, json, datetime, html
from . import llm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STYLES_DIR = os.path.join(ROOT, "styles")


def _strip_html(t: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?is)<br\s*/?>", "\n", t)
    t = re.sub(r"(?is)</(p|div|h[1-6]|li|tr|blockquote)>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", "", t)
    t = html.unescape(t)
    lines = [ln.strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if len(ln) >= 8]   # 去短噪声（广告/导航残留）
    return "\n".join(lines)


def fetch_url_text(url: str, max_chars: int = 12000) -> str:
    """抓取小说网址正文（best-effort，纯 httpx；JS 渲染站抓不到会返回空 → 提示改粘贴）。"""
    import httpx
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
        with httpx.Client(timeout=25, follow_redirects=True, headers=headers) as c:
            r = c.get(url)
            r.raise_for_status()
            body = _strip_html(r.text)
        seen, out = set(), []
        for ln in body.splitlines():
            if not ln or ln in seen:
                continue
            seen.add(ln); out.append(ln)
        return "\n".join(out)[:max_chars]
    except Exception:
        return ""


def distill(corpus: str, name: str, genre: str = "", source: str = "粘贴文本", cfg: dict = None) -> dict:
    """corpus≥500字效果佳。返回风格卡片 dict 并写入 styles/{name}/style.json。"""
    corpus = (corpus or "").strip()
    if len(corpus) < 120:
        return {"ok": False, "note": "语料太短（<120 字），至少给 500 字以上才能可靠蒸馏风格"}
    corpus = corpus[:16000]
    sys_p = ("你是一位写作风格分析专家。请对下面的小说节选做「风格蒸馏」：分析这位作者的写作风格技法（不看剧情内容），"
             "只输出严格 JSON，字段：\n"
             '{"summary":"一句话风格概括","dims":{"句式句长":"...","高频指纹词":"...","节奏与爽点":"...",'
             '"对话口吻":"...","视角人称":"...","情绪基调":"...","反AI禁用清单":"..."},'
             '"fingerprint_words":["3-8个高频指纹词"],"anti_ai":["5-8条要避免的AI腔"]}\n'
             "dims 里每项给具体、可执行的描述。不要复述或搬运原文剧情。")
    user_p = ("【题材参考】" + (genre or "未定") + "\n\n【作者节选】\n" + corpus)
    card = llm.chat_json([{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
                         cfg=cfg, stage="planning")
    if not isinstance(card, dict):
        card = {"summary": "（蒸馏失败，请重试）", "dims": {}, "fingerprint_words": [], "anti_ai": []}
    card["name"] = name
    card["genre"] = genre or ""
    card["source"] = source
    card["created_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    d = card.get("dims", {})
    card["style_prompt"] = (
        "【专属蒸馏风格：%s】以下风格技法要求（只改写法，不改剧情内容）：\n"
        "概括：%s\n" % (name, str(card.get("summary", "")))
        + "\n".join("- %s：%s" % (k, str(d.get(k, ""))) for k in
                    ["句式句长", "节奏与爽点", "对话口吻", "视角人称", "情绪基调"] if d.get(k))
        + "\n- 善用高频指纹词：" + "、".join((card.get("fingerprint_words") or [])[:8])
        + "\n- 务必避免（反 AI 腔）：" + "；".join((card.get("anti_ai") or [])[:8])
    )
    sdir = os.path.join(STYLES_DIR, name)
    os.makedirs(sdir, exist_ok=True)
    json.dump(card, open(os.path.join(sdir, "style.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    open(os.path.join(sdir, "samples.txt"), "w", encoding="utf-8").write(corpus[:6000])
    card["path"] = sdir
    return {"ok": True, "name": name, "summary": card.get("summary", ""), "style": card}


def list_styles():
    if not os.path.isdir(STYLES_DIR):
        return []
    out = []
    for n in sorted(os.listdir(STYLES_DIR)):
        p = os.path.join(STYLES_DIR, n, "style.json")
        if not os.path.exists(p):
            continue
        try:
            c = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        out.append({"name": n, "genre": c.get("genre", ""), "summary": c.get("summary", ""),
                    "created_at": c.get("created_at", ""), "fingerprint_words": c.get("fingerprint_words", [])})
    return out


def get_style(name: str):
    p = os.path.join(STYLES_DIR, name, "style.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def delete_style(name: str):
    import shutil
    p = os.path.join(STYLES_DIR, name)
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
        return {"ok": True}
    return {"ok": False, "note": "风格不存在"}
