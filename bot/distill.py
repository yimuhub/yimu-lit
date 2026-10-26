"""v0.3.4 风格蒸馏引擎。

借鉴 GitHub 上成熟技能的维度框架（writing-dna-skill 的 L1/L2、perfectly-replicate 的 7+1 维、
quill 的"风格 prompt + 强度 + 校准原文"思路），**自研精简实现**：不引入外部依赖、不装外部 skill。

核心：给一段目标文本（粘贴）或若干网址（抓取节选）→ LLM 按该文体专属维度分析「作者的写作风格技法」
（不是剧情内容）→ 输出结构化风格卡片 JSON + 一段可直接注入 craft 的风格 prompt → 存 styles/{name}/。

四种文体各有一套维度：小说/散文故事类、剧本、诗词、散文各看各的技法点，但卡片结构保持一致
（summary / dims / fingerprint_words / anti_ai / style_prompt），下游消费方不用改。

版权红线：只提风格/技法，不复述、不搬运原文文字；产物标注来源；网址抓取只取节选做分析、不留全文。
"""
import os, re, json, datetime, html, ipaddress, socket
from urllib.parse import urljoin, urlparse
from . import llm
from .safeio import atomic_write_json, atomic_write_text, safe_join

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STYLES_DIR = os.path.join(ROOT, "styles")

# 四种文体各自的风格维度清单：键名即卡片 dims 的键，值是提示 LLM 该看什么技法。
# 句式/节奏/视角/情绪/反AI腔是共有维度，只是按文体换了说法和颗粒度。
MEDIUM_SPECS = {
    "novel": {
        "label": "小说",
        "corpus": "节选",
        "dims": [
            ("句式句长", "长短句配比、句式错落方式与修饰密度"),
            ("节奏与爽点", "信息投放的疏密、悬念设置与情绪起伏的节奏安排"),
            ("对话口吻", "人物说话的分寸、口头禅、身份标记与潜台词密度"),
            ("视角人称", "叙述人称、视角距离与叙述者介入叙事的程度"),
            ("情绪基调", "整体情绪色彩、克制程度与爆发点的位置"),
            ("描写密度", "场景/心理/环境描写的比例与详略取舍"),
            ("高频指纹词", "作者反复使用的词、句式与意象标记"),
            ("反AI禁用清单", "这位作者从不出现的表达方式与套话"),
        ],
    },
    "script": {
        "label": "剧本",
        "corpus": "片段",
        "dims": [
            ("场景调度", "场面调度、空间关系与镜头感的处理方式"),
            ("场次节奏", "场次长度、进出场时机与信息投放的节奏"),
            ("对白口吻", "台词口语化程度、身份标记与语体差异"),
            ("潜台词与信息差", "角色真实意图与说出口的话之间的落差"),
            ("时代质感与道具", "时代细节、道具与生活方式的具体程度"),
            ("人物一致性", "各角色的语言习惯、身份与行为一致性"),
            ("行动指令化", "把心理与意图改写成可表演的动作与行为"),
            ("禁用清单", "剧本里不该出现的写法（说明性叙述、内心独白堆砌等）"),
        ],
    },
    "poetry": {
        "label": "诗词",
        "corpus": "作品",
        "dims": [
            ("意象系统", "核心意象的选取、层次与彼此呼应方式"),
            ("炼字与用典", "炼字习惯、用典方式与化用出新"),
            ("句法与节奏", "句式的长短错落、顿挫与音步安排"),
            ("声情与格律", "平仄押韵、声律与情感起伏的配合"),
            ("结句收束", "收束方式、余味与主题落点"),
            ("留白与多义", "不说破的留白与多义空间"),
            ("禁用清单", "诗词里要避免的写法（直白说理、堆砌典故等）"),
        ],
    },
    "prose": {
        "label": "散文",
        "corpus": "段落",
        "dims": [
            ("句子语感", "句子的长短、语气与口语/书面距离"),
            ("散点结构", "材料的组织方式与结构推进逻辑"),
            ("抒情节制", "情绪的流露程度与克制方式"),
            ("物象经营", "器物、景物的选择与写法"),
            ("虚实调度", "实写与虚写、回忆与现实的配比"),
            ("句群节奏", "段落起伏、停顿与段落长度"),
            ("禁用清单", "散文里要避免的写法（空洞抒情、堆砌典故等）"),
        ],
    },
}

# 别名归一：旧调用只传小说或干脆不传，都按 novel 处理。
_MEDIUM_ALIASES = {
    "": "novel",
    "novel": "novel",
    "小说": "novel",
    "script": "script",
    "剧本": "script",
    "poetry": "poetry",
    "诗词": "poetry",
    "prose": "prose",
    "散文": "prose",
}


def normalize_medium(medium: str) -> str:
    m = str(medium or "").strip().lower()
    return _MEDIUM_ALIASES.get(m, "novel")


def _strip_html(t: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?is)<br\s*/?>", "\n", t)
    t = re.sub(r"(?is)</(p|div|h[1-6]|li|tr|blockquote)>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", "", t)
    t = html.unescape(t)
    lines = [ln.strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if len(ln) >= 8]   # 去短噪声（广告/导航残留）
    return "\n".join(lines)


def _host_forbidden(host: str) -> bool:
    """拒绝回环/内网/链路本地/保留地址，防 SSRF（含 DNS 解析后的结果）。"""
    h = (host or "").strip().lower().strip("[]")
    if not h or h == "localhost" or h.endswith(".local") or h.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(h, None)
    except Exception:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def _url_allowed(url: str) -> bool:
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    if u.username or u.password:
        return False
    return not _host_forbidden(u.hostname)


def fetch_url_text(url: str, max_chars: int = 12000) -> str:
    """抓取小说网址正文（best-effort）。手动跟随重定向，每一跳都复查地址，防 SSRF。"""
    import httpx
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
        cur = url
        with httpx.Client(timeout=25, follow_redirects=False, headers=headers) as c:
            for _ in range(4):
                if not _url_allowed(cur):
                    return ""
                r = c.get(cur)
                if r.is_redirect:
                    loc = r.headers.get("location")
                    if not loc:
                        return ""
                    cur = urljoin(cur, loc)
                    continue
                r.raise_for_status()
                body = _strip_html(r.text)
                break
            else:
                return ""
        seen, out = set(), []
        for ln in body.splitlines():
            if not ln or ln in seen:
                continue
            seen.add(ln); out.append(ln)
        return "\n".join(out)[:max_chars]
    except Exception:
        return ""


def _build_style_prompt(card: dict, spec: dict) -> str:
    """把该文体的全部维度拼成一段可直接注入 craft 的风格说明。"""
    d = card.get("dims")
    if not isinstance(d, dict):
        d = {}
    keys = [k for k, _ in spec["dims"] if str(d.get(k) or "").strip()]
    keys += [k for k in d if k not in keys and str(d.get(k) or "").strip()]
    lines = "\n".join("- %s：%s" % (k, str(d[k]).strip()) for k in keys)
    fps = [str(w).strip() for w in (card.get("fingerprint_words") or []) if str(w).strip()][:8]
    anti = [str(a).strip() for a in (card.get("anti_ai") or []) if str(a).strip()][:8]
    return (
        "【专属蒸馏风格：%s】以下风格技法要求（只改写法，不改剧情内容）：\n"
        "概括：%s\n%s\n"
        "- 善用高频指纹词：%s\n"
        "- 务必避免（反 AI 腔）：%s"
        % (
            str(card.get("name", "")),
            str(card.get("summary", "")),
            lines,
            "、".join(fps),
            "；".join(anti),
        )
    )


def distill(corpus: str, name: str, genre: str = "", source: str = "粘贴文本",
            cfg: dict = None, medium: str = "") -> dict:
    """corpus≥500字效果佳。medium 决定用哪套维度（空串=小说）。返回风格卡片并落盘。"""
    corpus = (corpus or "").strip()
    if len(corpus) < 120:
        return {"ok": False, "note": "语料太短（<120 字），至少给 500 字以上才能可靠蒸馏风格"}
    corpus = corpus[:16000]
    med = normalize_medium(medium)
    spec = MEDIUM_SPECS[med]
    dim_hint = ",".join('"%s":"%s"' % (k, desc) for k, desc in spec["dims"])
    sys_p = (
        "你是一位写作风格分析专家。请对下面给出的「%s%s」做风格蒸馏："
        "分析这位作者的写作风格技法（只看写法，不看剧情内容），只输出严格 JSON，字段：\n"
        '{"summary":"一句话风格概括","dims":{%s},'
        '"fingerprint_words":["3-8个高频指纹词"],"anti_ai":["5-8条要避免的AI腔"]}\n'
        "dims 里每项给具体、可执行的描述。不要复述或搬运原文剧情，不要照抄原句；"
        "fingerprint_words 只给词，不要给句子。"
        % (spec["label"], spec["corpus"], dim_hint)
    )
    user_p = ("【文体】%s\n【题材参考】%s\n\n【作者节选】\n%s"
              % (spec["label"], genre or "未定", corpus))
    card = llm.chat_json([{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
                         cfg=cfg, stage="planning")
    if not isinstance(card, dict):
        card = {"summary": "（蒸馏失败，请重试）", "dims": {}, "fingerprint_words": [], "anti_ai": []}
    card["name"] = name
    card["genre"] = genre or ""
    card["medium"] = med
    card["source"] = source
    card["created_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    card["style_prompt"] = _build_style_prompt(card, spec)
    try:
        sdir = safe_join(STYLES_DIR, name, label="风格名")
    except ValueError:
        return {"ok": False, "note": "风格名非法"}
    os.makedirs(sdir, exist_ok=True)
    atomic_write_json(os.path.join(sdir, "style.json"), card)
    atomic_write_text(os.path.join(sdir, "samples.txt"), corpus[:6000])
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
                    "medium": c.get("medium", ""), "created_at": c.get("created_at", ""),
                    "fingerprint_words": c.get("fingerprint_words", [])})
    return out


def get_style(name: str):
    try:
        p = os.path.join(safe_join(STYLES_DIR, name, label="风格名"), "style.json")
    except ValueError:
        return None
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def delete_style(name: str):
    import shutil
    try:
        p = safe_join(STYLES_DIR, name, label="风格名")
    except ValueError:
        return {"ok": False, "note": "风格名非法"}
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
        return {"ok": True}
    return {"ok": False, "note": "风格不存在"}
