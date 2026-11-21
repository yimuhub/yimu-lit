"""榜单风向 HTTP 接口（v5.4.x，docs/系统化升级方案-v5.1 第 8.2 节）。

用 FastAPI APIRouter 收口，**不改 app.py 的既有内容**——主会话收尾只需在 app.py 里加两行挂载：

    from bot import webrank
    app.include_router(webrank.router)

接口契约（与 static/v2/js/views/rank.js 对齐）：
  GET  /api/rank/sources                      全部榜单源（源 + 快照日期清单，按最新倒序）
  POST /api/rank/fetch   {url, source?, title?}    手动抓取榜单 URL → SSR 文本解析 → 存当日快照
  POST /api/rank/text    {text, source?, title?}   手动粘贴榜单文本 → 行式解析 → 存当日快照
  GET  /api/rank/snapshot?source&date&against     读快照（date 缺省最新；against 缺省上一份，算升降幅）
  POST /api/rank/review  {source, date?, refresh?} AI 解读（走 bot/llm.py，scene=「榜单解读」，缓存进快照）
  # 灵感库补充端点（v5.4 灵感速记之外的瀑布流能力，挂本路由避免改 webstats.py）：
  PUT  /api/inspirations/{id}                行内编辑（正文重过 #标签 提取，300 字上限同口径）
  POST /api/inspirations/{id}/pin            置顶 / 取消置顶

抓取纪律：手动触发 only（无定时、无自动爬虫）；UA + 10s 超时 + 2MB 上限；
SSRF 防护与 bot/distill.py 同口径（拒绝回环/内网/链路本地/保留地址，逐跳复查重定向）；
解析失败如实报中文错误，**不做任何反爬对抗**。HTTP 客户端用仓库既有 httpx（未引入 requests）。
"""
from __future__ import annotations

import datetime
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

from fastapi import APIRouter, HTTPException

from . import inspirations, llm, rankbook

router = APIRouter()

# ---------------------------------------------------------------- 抓取参数

FETCH_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FETCH_TIMEOUT = 10                    # 秒（httpx 总超时）
MAX_BYTES = 2 * 1024 * 1024           # 2MB：超出即停止读取（截断后仍尝试解析开头部分）
MAX_REDIRECTS = 4                     # 手动逐跳跟随，每跳复查 SSRF


def _payload(payload: dict) -> dict:
    return payload if isinstance(payload, dict) else {}


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=str(message))


# ---------------------------------------------------------------- SSRF 防护（与 bot/distill.py 同口径）

def _host_forbidden(host: str) -> bool:
    """拒绝回环/内网/链路本地/保留地址（含 DNS 解析后的结果），防 SSRF。"""
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


# ---------------------------------------------------------------- 抓取与解码

def _decode_bytes(raw: bytes, ctype: str = "") -> str:
    """按 content-type 声明的编码解码，失败依次退 utf-8 / gb18030，最后 replace 容错。"""
    encs: list[str] = []
    m = re.search(r"charset=([\w-]+)", ctype or "", re.IGNORECASE)
    if m:
        encs.append(m.group(1))
    encs += ["utf-8", "gb18030"]
    for enc in encs:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_url_text(url: str, max_hops: int = MAX_REDIRECTS) -> str:
    """手动抓取一次榜单页面 → 解码后的 HTML 文本。

    纪律：UA + 10s 超时 + 2MB 上限；手动逐跳跟随重定向（每跳复查 SSRF）；
    政策类失败抛 ValueError（中文如实说明），网络类异常原样上抛由路由层翻译。"""
    import httpx
    if not _url_allowed(url):
        raise ValueError("该地址不允许抓取：内网/回环/保留地址已拦截（防 SSRF）")
    headers = {"User-Agent": FETCH_UA, "Accept-Language": "zh-CN,zh;q=0.9",
               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    cur = url
    with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=False, headers=headers) as client:
        for _ in range(max_hops):
            if not _url_allowed(cur):
                raise ValueError("重定向后的地址不允许抓取：内网/回环/保留地址已拦截（防 SSRF）")
            with client.stream("GET", cur) as r:
                if 300 <= r.status_code < 400:
                    loc = r.headers.get("location") or ""
                    if not loc:
                        raise ValueError(f"页面重定向缺少目标地址（HTTP {r.status_code}）")
                    cur = urljoin(cur, loc)
                    continue
                if r.status_code >= 400:
                    raise ValueError(f"页面返回 HTTP {r.status_code}，抓取失败"
                                     f"（本工具不做反爬对抗；若站点拦截请改用「粘贴文本」方式）")
                ctype = (r.headers.get("content-type") or "").lower()
                if ctype and ("html" not in ctype and "text" not in ctype and "xml" not in ctype):
                    raise ValueError(f"该地址不是网页（content-type: {ctype}），无法解析榜单")
                raw = bytearray()
                for chunk in r.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        break   # 2MB 上限：截断，仍用开头部分尝试解析
                return _decode_bytes(bytes(raw), ctype)
        raise ValueError("重定向次数过多（超过 4 跳），已停止抓取")


def _fetch_error_message(err: Exception) -> str:
    """httpx 网络异常 → 中文可操作提示（如实报告，不猜不掩盖）。"""
    name = type(err).__name__
    if "Timeout" in name:
        return "抓取超时（10 秒内未完成）：该站点可能较慢或不可达，可稍后重试，或直接复制榜单文本用「粘贴文本」解析"
    if "Connect" in name:
        return "连接失败：无法访问该地址，请检查 URL 是否正确、本机是否能上网"
    if "Decoding" in name or "InvalidURL" in name:
        return "地址或编码异常，无法抓取：请检查 URL，或改用「粘贴文本」方式"
    return f"抓取失败：{err}"


def _source_from_url(url: str) -> str:
    """未指定源名时用域名做源标识（去掉 www.）。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    host = re.sub(r"^www\.", "", host)
    return host or "manual"


# ---------------------------------------------------------------- 采集 → 快照（fetch/text 共用）

def _collect_and_save(source: str, raw_text: str, url: str = "", title: str = "") -> dict:
    """解析榜单文本 → 存当日快照 → 相对上一份快照算升降幅。解析失败如实报错。"""
    entries = rankbook.parse_rank_lines(raw_text)
    if not entries:
        raise _bad("未能解析出榜单条目：请确认每行是「名次 书名 作者 数据」格式"
                   "（该页面可能是脚本渲染或需登录），建议手动复制榜单文本后用「粘贴文本」解析")
    today = datetime.date.today().isoformat()
    snap = rankbook.save_snapshot(source, today, entries, url=url, title=title)
    prev = rankbook.previous_snapshot(source, snap["date"])
    changes = rankbook.compare_entries((prev or {}).get("entries") or [], snap["entries"])
    return {
        "ok": True,
        "source": snap["source"],
        "date": snap["date"],
        "count": len(snap["entries"]),
        "changes": changes,
        "compared_with": (prev or {}).get("date", ""),
        "snapshot": snap,
    }


@router.post("/api/rank/fetch")
def api_rank_fetch(payload: dict):
    """手动抓取榜单 URL。body: {url, source?, title?}"""
    p = _payload(payload)
    url = str(p.get("url") or "").strip()
    if not url:
        raise _bad("请先粘贴榜单页面链接")
    source = str(p.get("source") or "").strip() or _source_from_url(url)
    try:
        page = fetch_url_text(url)
    except ValueError as err:
        raise _bad(str(err)) from err
    except Exception as err:
        raise _bad(_fetch_error_message(err)) from err
    if not page.strip():
        raise _bad("页面没有返回可用文本（可能是脚本渲染或需登录页），请手动复制榜单文本后用「粘贴文本」解析")
    return _collect_and_save(source, rankbook.html_to_text(page), url=url,
                             title=str(p.get("title") or "").strip())


@router.post("/api/rank/text")
def api_rank_text(payload: dict):
    """手动粘贴榜单文本。body: {text, source?, title?}"""
    p = _payload(payload)
    text = str(p.get("text") or "").strip()
    if not text:
        raise _bad("请先粘贴榜单文本")
    source = str(p.get("source") or "").strip() or "manual"
    return _collect_and_save(source, text, title=str(p.get("title") or "").strip())


# ---------------------------------------------------------------- 快照读取 / AI 解读

@router.get("/api/rank/sources")
def api_rank_sources():
    return {"ok": True, "sources": rankbook.list_sources()}


@router.get("/api/rank/snapshot")
def api_rank_snapshot(source: str = "", date: str = "", against: str = ""):
    """读快照。date 缺省取最新；against 缺省取严格早于 date 的上一份，自动算升降幅。"""
    source = str(source or "").strip()
    if not source:
        raise _bad("缺少榜单源")
    snap = rankbook.load_snapshot(source, date) if date else rankbook.latest_snapshot(source)
    if snap is None:
        raise HTTPException(status_code=404, detail="快照不存在：请先采集一份榜单")
    base = (rankbook.load_snapshot(source, against) if against
            else rankbook.previous_snapshot(source, snap["date"]))
    changes = rankbook.compare_entries((base or {}).get("entries") or [], snap.get("entries") or [])
    return {
        "ok": True,
        "snapshot": snap,
        "changes": changes,
        "compared_with": (base or {}).get("date", ""),
    }


# AI 解读系统提示：明确隐私口径——只发榜单数据，绝不包含用户创作内容。
_REVIEW_SYSTEM = (
    "你是网文榜单分析师。用户会提供一份手动采集的榜单快照（名次 / 书名 / 作者 / 公开数据列 / "
    "较上一份快照的名次变化），**只发送榜单数据，不含任何创作内容**。"
    "请用中文输出 300 字以内的解读，分三段："
    "① 分类与题材分布（从书名与公开数据推断）；"
    "② 标签风向与热点趋势（结合名次升降说明）；"
    "③ 给作者的竞品提示（可借鉴的题材/选题方向，不逐条点评具体作品）。"
    "不得虚构榜单里不存在的数据，不得输出与榜单无关的内容。"
)


@router.post("/api/rank/review")
def api_rank_review(payload: dict):
    """AI 解读（bot/llm.py，scene=「榜单解读」）。结果随快照缓存，refresh=1 强制重算。"""
    p = _payload(payload)
    source = str(p.get("source") or "").strip()
    if not source:
        raise _bad("缺少榜单源")
    date = str(p.get("date") or "").strip()
    snap = rankbook.load_snapshot(source, date) if date else rankbook.latest_snapshot(source)
    if snap is None:
        raise HTTPException(status_code=404, detail="没有找到该源的快照，请先采集一份榜单")
    date = snap["date"]
    if snap.get("review") and not bool(p.get("refresh")):
        return {"ok": True, "cached": True, "review": snap["review"],
                "review_at": snap.get("review_at") or "", "date": date}
    prev = rankbook.previous_snapshot(source, date)
    changes = rankbook.compare_entries((prev or {}).get("entries") or [], snap.get("entries") or [])
    summary = rankbook.summarize_for_review(snap, changes)
    msgs = [{"role": "system", "content": _REVIEW_SYSTEM}, {"role": "user", "content": summary}]
    try:
        out = llm.chat(msgs, stage="rank", scene="榜单解读", temperature=0.4)
    except Exception as err:
        raise _bad(f"AI 解读失败：{err}") from err
    out = str(out or "").strip()
    if not out:
        raise _bad("模型没有返回解读内容，请稍后重试或检查模型配置")
    saved = rankbook.set_review(source, date, out) or {}
    return {"ok": True, "cached": False, "review": out,
            "review_at": saved.get("review_at") or "", "date": date}


# ---------------------------------------------------------------- 灵感库补充端点

def _inspiration_edit(item_id: str, text: str) -> dict | None:
    """行内编辑：复用 inspirations 的存储层与锁（补端点放在本路由，不改 webstats.py）。"""
    with inspirations._locked():
        d = inspirations.load()
        for it in d["items"]:
            if it.get("id") == str(item_id or ""):
                it["text"] = text
                it["tags"] = inspirations.extract_tags(text)   # #标签 重提取（与新增同口径）
                it["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
                inspirations.save(d)
                return it
        return None


@router.put("/api/inspirations/{insp_id}")
def api_inspirations_edit(insp_id: str, payload: dict):
    """行内编辑灵感。body: {text}（#标签 语法重提取；300 字上限与新增同口径）"""
    p = _payload(payload)
    text = str(p.get("text") or "").strip()
    if not text:
        raise _bad("灵感内容不能为空")
    if len(text) > inspirations.MAX_LEN:
        raise _bad(f"灵感最多 {inspirations.MAX_LEN} 字，当前 {len(text)} 字")
    item = _inspiration_edit(insp_id, text)
    if item is None:
        raise HTTPException(status_code=404, detail="灵感不存在或已删除")
    return {"ok": True, "item": item}


@router.post("/api/inspirations/{insp_id}/pin")
def api_inspirations_pin(insp_id: str, payload: dict):
    """置顶 / 取消置顶。body: {pinned}"""
    p = _payload(payload)
    item = inspirations.set_pinned(insp_id, bool(p.get("pinned")))
    if item is None:
        raise HTTPException(status_code=404, detail="灵感不存在或已删除")
    return {"ok": True, "item": item}
