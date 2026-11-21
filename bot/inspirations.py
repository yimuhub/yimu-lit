"""灵感速记（v5.4.0，首页「灵感速记卡」的存储层）。

设计（docs/系统化升级方案-v5.1 第 6.3 节，全部自研）：
  · 瀑布流素材库 ``data/inspirations.json``：
      {"version": 1, "items": [{id, text, tags, created, updated, pinned}]}
  · 常驻单条输入，失焦即存；``#标签`` 语法在前端提取展示、在后端提取入账（两侧同口径）；
  · 单条上限 300 字（网文一句话灵感足够，超长请开作品）；
  · 零 LLM、确定性；写入走 safeio.atomic_write_json + 进程内锁 + fcntl 文件锁。
"""
from __future__ import annotations

import contextlib
import datetime
import json
import re
import threading
import uuid

from .paths import data
from .safeio import atomic_write_json

# 灵感库文件（tests 里 monkeypatch 本常量即可整体隔离）
DATA_FILE = data("inspirations.json")

MAX_LEN = 300          # 单条正文字数上限
MAX_TAGS = 8           # 单条标签数上限
MAX_ITEMS = 2000       # 防膨胀：最多保留条数（裁最旧的非置顶）

# ``#标签`` 语法：# 后跟非空白/非常见标点的 1-20 个字符（允许中英数字与 - _ ·），
# 并要求标签后面紧跟边界（空白/#/标点/串尾）——超长标签整体不收，绝不静默截断。
# 排除集：空白 + 常见中英文标点（弯引号用 \u 转义写死，避免编辑器把弯引号换成 ASCII）。
_TAG_CHARS = "\\s#，。；！？、：\u201c\u201d\u2018\u2019（）【】《》…!?.,:;()"
TAG_RE = re.compile("#([^" + _TAG_CHARS + "]{1,20})(?=$|[" + _TAG_CHARS + "])")

_LOCK_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: str) -> threading.RLock:
    with _LOCK_GUARD:
        lk = _LOCKS.get(path)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[path] = lk
        return lk


@contextlib.contextmanager
def _locked():
    """进程内 RLock + 跨进程 fcntl 文件锁（fcntl 不可用退化为纯线程锁）。"""
    with _lock_for(DATA_FILE):
        fh = None
        try:
            try:
                import fcntl  # POSIX only
            except ImportError:
                fcntl = None
            if fcntl is not None:
                fh = open(DATA_FILE + ".lock", "a+", encoding="utf-8")
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fh is not None:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    fh.close()
                except Exception:
                    pass


def extract_tags(text: str) -> list[str]:
    """``#标签`` 语法提取：去重、限 8 个、限 20 字。与前端 core 侧同口径。"""
    out: list[str] = []
    for m in TAG_RE.finditer(str(text or "")):
        t = m.group(1).strip()
        if t and t not in out:
            out.append(t)
        if len(out) >= MAX_TAGS:
            break
    return out


def _empty() -> dict:
    return {"version": 1, "items": []}


def load() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return _empty()
    items = []
    for it in raw["items"]:
        if not isinstance(it, dict):
            continue
        items.append({
            "id": str(it.get("id") or "")[:40],
            "text": str(it.get("text") or "")[:MAX_LEN],
            "tags": [str(t)[:20] for t in (it.get("tags") or []) if isinstance(t, str)][:MAX_TAGS],
            "created": str(it.get("created") or ""),
            "updated": str(it.get("updated") or ""),
            "pinned": bool(it.get("pinned")),
        })
    return {"version": 1, "items": [x for x in items if x["id"] and x["text"]]}


def save(d: dict) -> None:
    items = d.get("items") or []
    if len(items) > MAX_ITEMS:
        # 裁最旧的非置顶；置顶条目永不裁
        pinned = [x for x in items if x.get("pinned")]
        rest = sorted((x for x in items if not x.get("pinned")), key=lambda x: x.get("created") or "")
        d["items"] = pinned + rest[-(MAX_ITEMS - len(pinned)):]
    atomic_write_json(DATA_FILE, d)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def add(text: str) -> dict:
    """新增一条灵感。``#标签`` 在后端再提取一次（前端提取结果仅作展示参考）。
    超长/为空抛 ValueError，由路由层转 400。"""
    text = str(text or "").strip()
    if not text:
        raise ValueError("灵感内容不能为空")
    if len(text) > MAX_LEN:
        raise ValueError(f"灵感最多 {MAX_LEN} 字，当前 {len(text)} 字")
    with _locked():
        d = load()
        item = {
            "id": uuid.uuid4().hex[:12],
            "text": text,
            "tags": extract_tags(text),
            "created": _now(),
            "updated": _now(),
            "pinned": False,
        }
        d["items"].append(item)
        save(d)
        return item


def delete(item_id: str) -> bool:
    with _locked():
        d = load()
        before = len(d["items"])
        d["items"] = [x for x in d["items"] if x.get("id") != str(item_id or "")]
        changed = len(d["items"]) != before
        if changed:
            save(d)
        return changed


def set_pinned(item_id: str, pinned: bool) -> dict | None:
    """置顶/取消置顶（瀑布流排序用；当前首页只展示最近条目，能力预留）。"""
    with _locked():
        d = load()
        for it in d["items"]:
            if it.get("id") == str(item_id or ""):
                it["pinned"] = bool(pinned)
                it["updated"] = _now()
                save(d)
                return it
        return None


def list_items(limit: int = 50) -> list[dict]:
    """置顶优先，其余按创建时间倒序。"""
    limit = max(1, min(int(limit or 50), 200))
    items = load()["items"]
    pinned = [x for x in items if x.get("pinned")]
    rest = sorted((x for x in items if not x.get("pinned")), key=lambda x: x.get("created") or "", reverse=True)
    return (pinned + rest)[:limit]
