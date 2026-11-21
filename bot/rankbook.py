"""榜单风向账本（v5.4.x，docs/系统化升级方案-v5.1 第 8.2 节，全部自研）。

定位「手动采风 + AI 解读」：用户手动粘贴榜单 URL（服务端抓一次）或直接粘贴榜单文本，
解析成名次/书名/作者/数据列后存为快照；不做自动爬虫、不做定时抓取、不做反爬对抗。

存储（data/rank-snapshots/<源标识>/<YYYY-MM-DD>.json）：
    {"version": 1, "source": <源标识>, "date": "YYYY-MM-DD", "url": <抓取来源或空>,
     "title": <榜单标题或空>, "entries": [{rank, title, author, metric}],
     "review": <AI 解读文本或空>, "review_at": <解读生成时间或空>, "created": <落盘时间>}
  · 每源每日一份：同日重抓直接覆盖当日文件；
  · 保留 120 天：每次落盘顺手清理更早的过期快照；
  · 两次快照自动算升降幅（compare_entries：↑n / ↓n / 新入榜）。

行式解析容错（parse_rank_lines）：
  · 名次前缀容忍 「1.」「1、」「01」「TOP3」「No.4」「[7]」「第5名」等写法；
  · 字段分隔容忍 | ／ ／ tab / 连续空格；作者列容忍 「作者：X」「文：X」内联写法；
  · 数据列取最后一个含数字的字段（推荐票/月票/收藏等公开数据）；
  · 整段文本没有任何名次前缀时降级为「每行一条、顺序编号」；
  · 表头行（含 名次+书名+作者 字样）与空行跳过，同名书去重保首个。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import html as _html

from .paths import data
from .safeio import atomic_write_json

# 快照根目录（tests 里 monkeypatch 本常量即可整体隔离）
SNAP_DIR = data("rank-snapshots")

RETAIN_DAYS = 120        # 快照保留天数
MAX_ENTRIES = 200        # 单份快照最多收录条目数（防膨胀）
MAX_TITLE = 80           # 书名截断上限
MAX_AUTHOR = 40          # 作者截断上限
MAX_METRIC = 40          # 数据列截断上限
MAX_SOURCE_LEN = 40      # 源标识长度上限

# ---------------------------------------------------------------- 源标识

# 源标识只保留中英数字/下划线/连字符/点（\w 含 CJK；点为域名自然存在），
# 其余一律替换为连字符，再剥掉首尾的连字符与点——从根上杜绝路径穿越
# （".."、隐藏目录名全被剥净，纯点串退回 manual）。
_SOURCE_BAD_RE = re.compile(r"[^\w.-]+", re.UNICODE)


def normalize_source(source) -> str:
    """源标识安全化：非法字符替换为连字符、剥首尾连字符与点、限长；空则退回 manual。"""
    s = _SOURCE_BAD_RE.sub("-", str(source or "").strip())
    s = s.strip("-.")
    s = s[:MAX_SOURCE_LEN]
    s = s.strip("-.")
    return s or "manual"


def _source_dir(source: str) -> str:
    return os.path.join(SNAP_DIR, normalize_source(source))


# ---------------------------------------------------------------- 行式解析

# 名次前缀：行首 1-3 位数字，容忍 「1.」「1、」「01」「TOP3」「No.4」「[7]」「第5名」「(8)」
RANK_RE = re.compile(
    r"^\s*[（(\[]?\s*(?:第\s*)?(?:top|no\.?|#)?\s*(\d{1,3})\s*(?:名|位)?\s*[)）\]】]?\s*(?:[.、:：\-—|]|\s|$)",
    re.IGNORECASE,
)

# 字段分隔：竖线/斜线优先，其次 tab 与连续空格；单空格在无竖线时兜底切分（榜单场景够用）
PIPE_SPLIT_RE = re.compile(r"\s*[|｜/／]\s*|\t+")
SPACE_SPLIT_RE = re.compile(r"\s+")

# 作者内联写法：「作者：燕垒生」「文：猫腻」（冒号必带——书名含「文」字时不误判）
AUTHOR_INLINE_RE = re.compile(r"(?:作者|主笔|文)\s*[:：]\s*([^\s|｜/／]{1,20})", re.IGNORECASE)

DIGIT_RE = re.compile(r"\d")
AUTHOR_PREFIX_RE = re.compile(r"^(?:作者|文|主笔|up主)\s*[:：]\s*", re.IGNORECASE)

# 表头守卫：一行同时含这些字样时按表头跳过
_HEADER_WORDS = ("名次", "书名", "作者")


def _norm_title(title: str) -> str:
    """书名归一化（升降幅按书名配对）：去全部空白 + 小写。"""
    return re.sub(r"\s+", "", str(title or "")).lower()


def _split_fields(rest: str) -> tuple[str, str, str]:
    """把名次后的剩余文本切成（书名, 作者, 数据列）。见模块 docstring 的容错规则。"""
    rest = str(rest or "").strip()
    if not rest:
        return "", "", ""
    parts = [p.strip() for p in PIPE_SPLIT_RE.split(rest) if p.strip()]
    if len(parts) == 1:
        parts = [p.strip() for p in SPACE_SPLIT_RE.split(rest) if p.strip()]
    if not parts:
        return "", "", ""
    title = parts[0][:MAX_TITLE]
    author = ""
    metric = ""
    rest_parts = parts[1:]
    m = AUTHOR_INLINE_RE.search(rest)
    if m:
        author = m.group(1).strip()
        rest_parts = [p for p in rest_parts if author not in p]
    # 数据列：剩余部分里倒序找第一个含数字的字段
    mi = None
    for i in range(len(rest_parts) - 1, -1, -1):
        if DIGIT_RE.search(rest_parts[i]):
            mi = i
            break
    if mi is not None:
        metric = rest_parts[mi][:MAX_METRIC]
        rest_parts = rest_parts[:mi] + rest_parts[mi + 1:]
    if not author and rest_parts:
        author = rest_parts[0]
    author = AUTHOR_PREFIX_RE.sub("", author or "").strip()[:MAX_AUTHOR]
    return title, author, metric


def _mk_entry(rank: int, title: str, author: str, metric: str) -> dict:
    return {"rank": int(rank), "title": title, "author": author, "metric": metric}


def parse_rank_lines(text: str) -> list[dict]:
    """榜单文本 → 条目列表。优先按名次前缀严格解析；全无名次时降级逐行顺序编号。

    解析不出任何条目返回空列表（调用方如实报错，不做猜测性补救）。"""
    lines = [ln.strip() for ln in str(text or "").splitlines()]
    lines = [ln for ln in lines if ln]

    def _is_header(ln: str) -> bool:
        return all(w in ln for w in _HEADER_WORDS)

    entries: list[dict] = []
    seen: set[str] = set()

    def _push(rank: int, rest_or_line: str) -> None:
        title, author, metric = _split_fields(rest_or_line)
        if not title:
            return
        key = _norm_title(title)
        if not key or key in seen:
            return
        seen.add(key)
        if len(entries) >= MAX_ENTRIES:
            return
        entries.append(_mk_entry(rank, title, author, metric))

    # 第一遍：严格名次行
    for ln in lines:
        if _is_header(ln):
            continue
        m = RANK_RE.match(ln)
        if not m:
            continue
        rank = int(m.group(1))
        if any(e["rank"] == rank for e in entries):
            rank = len(entries) + 1   # 名次重复（噪声行）→ 按出现顺序顺延
        _push(rank, ln[m.end():])

    if entries:
        return entries

    # 第二遍（降级）：整段没有名次前缀 → 每行一条、顺序编号。
    # 门槛：至少 3 行才认作榜单——登录墙/提示页这类单行文本如实判失败，不硬凑条目。
    rest_lines = [ln for ln in lines if not _is_header(ln)]
    if len(rest_lines) >= 3:
        for ln in rest_lines:
            _push(len(entries) + 1, ln)
    return entries


# ---------------------------------------------------------------- SSR 文本解析

_SCRIPT_RE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_TR_RE = re.compile(r"(?i)</tr\s*>")
_CELL_RE = re.compile(r"(?i)</t[dh]\s*>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_BLOCK_RE = re.compile(r"(?i)</?(?:p|div|li|ul|ol|table|thead|tbody|h[1-6]|section|article|dl|dd|dt|blockquote)[^>]*>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def html_to_text(page: str) -> str:
    """SSR 网页 → 逐行纯文本（表格单元格拼成 | 分隔的行，其余块级标签换行）。"""
    s = str(page or "")
    s = _SCRIPT_RE.sub(" ", s)
    s = _CELL_RE.sub("|", s)
    s = _TR_RE.sub("\n", s)
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_RE.sub("\n", s)
    s = _TAG_RE.sub(" ", s)
    s = _html.unescape(s).replace("\u00a0", " ")
    lines = [_WS_RE.sub(" ", ln).strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def parse_rank_html(page: str) -> list[dict]:
    """抓取到的网页 → 条目列表（先转文本再走行式解析）。"""
    return parse_rank_lines(html_to_text(page))


# ---------------------------------------------------------------- 快照存储

def _today() -> str:
    return datetime.date.today().isoformat()


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def snapshot_path(source: str, date: str) -> str:
    return os.path.join(_source_dir(source), f"{date}.json")


def _valid_date(date: str) -> bool:
    try:
        datetime.date.fromisoformat(str(date or ""))
        return True
    except ValueError:
        return False


def save_snapshot(source: str, date: str, entries: list[dict],
                  url: str = "", title: str = "") -> dict:
    """写当日快照（同日重抓覆盖），并顺手清理超过保留期的旧快照。"""
    date = str(date or _today())
    if not _valid_date(date):
        raise ValueError(f"快照日期不合法：{date}（应为 YYYY-MM-DD）")
    src = normalize_source(source)
    clean: list[dict] = []
    seen: set[str] = set()
    for e in (entries or [])[:MAX_ENTRIES]:
        if not isinstance(e, dict):
            continue
        t = str(e.get("title") or "").strip()[:MAX_TITLE]
        if not t or _norm_title(t) in seen:
            continue
        seen.add(_norm_title(t))
        clean.append({
            "rank": int(e.get("rank") or len(clean) + 1),
            "title": t,
            "author": str(e.get("author") or "").strip()[:MAX_AUTHOR],
            "metric": str(e.get("metric") or "").strip()[:MAX_METRIC],
        })
    payload = {
        "version": 1,
        "source": src,
        "date": date,
        "url": str(url or "")[:500],
        "title": str(title or "")[:100],
        "entries": clean,
        "review": "",
        "review_at": "",
        "created": _now_iso(),
    }
    atomic_write_json(snapshot_path(src, date), payload)
    prune_snapshots(src)
    return payload


def load_snapshot(source: str, date: str) -> dict | None:
    """读指定日快照；不存在或损坏返回 None。"""
    src = normalize_source(source)
    try:
        with open(snapshot_path(src, date), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        return None
    return raw


def _snap_dates(source: str) -> list[str]:
    """该源下所有快照日期（从文件名取），升序。"""
    d = _source_dir(source)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in names:
        if n.endswith(".json") and _valid_date(n[:-5]):
            out.append(n[:-5])
    return sorted(out)


def latest_snapshot(source: str) -> dict | None:
    dates = _snap_dates(source)
    return load_snapshot(source, dates[-1]) if dates else None


def previous_snapshot(source: str, date: str) -> dict | None:
    """严格早于 date 的最近一份快照（升降幅的对比基准）。"""
    dates = [d for d in _snap_dates(source) if d < str(date or "")]
    return load_snapshot(source, dates[-1]) if dates else None


def list_snapshots(source: str) -> list[dict]:
    """快照清单（日期倒序）：[{date, count, has_review}]。"""
    out = []
    for d in reversed(_snap_dates(source)):
        snap = load_snapshot(source, d) or {}
        out.append({
            "date": d,
            "count": len(snap.get("entries") or []),
            "has_review": bool(snap.get("review")),
        })
    return out


def prune_snapshots(source: str, keep_days: int = RETAIN_DAYS) -> int:
    """清理超过保留期的旧快照，返回删除数（按文件名日期判断，不碰今天之后的异常文件）。"""
    src = normalize_source(source)
    cutoff = (datetime.date.today() - datetime.timedelta(days=int(keep_days))).isoformat()
    removed = 0
    for d in _snap_dates(src):
        if d < cutoff:
            try:
                os.remove(snapshot_path(src, d))
                removed += 1
            except OSError:
                pass
    return removed


def list_sources() -> list[dict]:
    """全部榜单源：[{source, dates(倒序), latest}]，按最新快照日期倒序。"""
    try:
        names = os.listdir(SNAP_DIR)
    except OSError:
        return []
    out = []
    for n in names:
        if not os.path.isdir(os.path.join(SNAP_DIR, n)):
            continue
        dates = _snap_dates(n)
        if dates:
            out.append({"source": n, "dates": list(reversed(dates)), "latest": dates[-1]})
    out.sort(key=lambda x: x["latest"], reverse=True)
    return out


def set_review(source: str, date: str, review: str) -> dict | None:
    """把 AI 解读写回当日快照缓存（review + review_at）。"""
    snap = load_snapshot(source, date)
    if snap is None:
        return None
    snap["review"] = str(review or "").strip()
    snap["review_at"] = _now_iso() if snap["review"] else ""
    atomic_write_json(snapshot_path(normalize_source(source), date), snap)
    return snap


# ---------------------------------------------------------------- 升降幅

def compare_entries(prev_entries: list[dict], cur_entries: list[dict]) -> list[dict]:
    """给当前榜单算相对上一份快照的升降幅。

    配对键：书名（去空白归一）。delta = prev_rank - cur_rank（正数=上升）；
    上一份没有的书标 tag="new"（新入榜）。返回条目附 change 字段的列表。"""
    prev_map: dict[str, int] = {}
    for e in (prev_entries or []):
        if isinstance(e, dict) and e.get("title"):
            prev_map.setdefault(_norm_title(e["title"]), int(e.get("rank") or 0))
    out = []
    for e in (cur_entries or []):
        item = dict(e)
        pr = prev_map.get(_norm_title(e.get("title") or ""))
        if pr is None:
            item["change"] = {"delta": 0, "tag": "new", "prev_rank": None}
        else:
            delta = pr - int(e.get("rank") or 0)
            item["change"] = {
                "delta": delta,
                "tag": "up" if delta > 0 else ("down" if delta < 0 else "same"),
                "prev_rank": pr,
            }
        out.append(item)
    return out


def summarize_for_review(snap: dict, changes: list[dict]) -> str:
    """把当前榜单 + 名次变化压成喂给模型的纯文本（只有榜单数据，绝无创作内容）。"""
    src = str(snap.get("source") or "")
    date = str(snap.get("date") or "")
    lines = [f"榜单源：{src}", f"快照日期：{date}", f"条目数：{len(changes)}", "榜单（名次 | 书名 | 作者 | 公开数据 | 较上一份快照）："]
    for c in changes[:MAX_ENTRIES]:
        ch = c.get("change") or {}
        tag = {("up"): f"↑{ch['delta']}", ("down"): f"↓{abs(ch['delta'])}",
               ("same"): "持平", ("new"): "新入榜"}.get(ch.get("tag"), "—")
        lines.append(f"{c.get('rank')} | {c.get('title')} | {c.get('author') or '佚名'} | {c.get('metric') or '—'} | {tag}")
    up = sum(1 for c in changes if (c.get("change") or {}).get("tag") == "up")
    down = sum(1 for c in changes if (c.get("change") or {}).get("tag") == "down")
    new = sum(1 for c in changes if (c.get("change") or {}).get("tag") == "new")
    lines.append(f"变化统计：上升 {up} 本 / 下降 {down} 本 / 新入榜 {new} 本")
    return "\n".join(lines)
