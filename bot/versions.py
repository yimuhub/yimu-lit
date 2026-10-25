"""章节版本快照：编辑/重写前留底，可按版本对比与回滚。

存储：<书>/.versions/chNNN/<时间戳>-<hash8>.md；每章最多保留 _KEEP 份。
只做本地文件，不依赖数据库；所有路径都经过 safe_join。
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import os
import re

from .safeio import atomic_write_text, safe_join

_KEEP = 30
_VID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def _chapter_dir(proj_path: str, num: int) -> str:
    num = int(num)
    if num < 1 or num > 100000:
        raise ValueError("章节号不合法")
    d = os.path.join(proj_path, ".versions", f"ch{num:03d}")
    os.makedirs(d, exist_ok=True)
    return d


def _list_raw(proj_path: str, num: int) -> list[dict]:
    d = _chapter_dir(proj_path, num)
    out = []
    for name in os.listdir(d):
        if not name.endswith(".md"):
            continue
        fp = os.path.join(d, name)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        vid = name[:-3]
        ts = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        out.append({"id": vid, "ts": ts, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def list_versions(proj_path: str, num: int) -> list[dict]:
    return [{"id": v["id"], "ts": v["ts"], "size": v["size"]}
            for v in _list_raw(proj_path, num)]


def _latest_text(proj_path: str, num: int) -> str:
    raw = _list_raw(proj_path, num)
    if not raw:
        return ""
    try:
        return open(os.path.join(_chapter_dir(proj_path, num), raw[0]["id"] + ".md"),
                    encoding="utf-8").read()
    except OSError:
        return ""


def snapshot(proj_path: str, num: int, text: str, label: str = "") -> dict | None:
    """内容有变化才落一份快照（相同内容不重复占空间）。"""
    if not text or not text.strip():
        return None
    if _latest_text(proj_path, num) == text:
        return None
    d = _chapter_dir(proj_path, num)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    vid = f"{stamp}-{digest}"
    atomic_write_text(os.path.join(d, vid + ".md"), text)
    raw = _list_raw(proj_path, num)
    for old in raw[_KEEP:]:
        try:
            os.remove(os.path.join(d, old["id"] + ".md"))
        except OSError:
            pass
    return {"id": vid, "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "size": len(text.encode("utf-8")), "label": label}


def read_version(proj_path: str, num: int, vid: str) -> str:
    if not _VID.match(vid or ""):
        raise ValueError("版本号不合法")
    fp = safe_join(_chapter_dir(proj_path, num), vid + ".md", label="版本号")
    try:
        return open(fp, encoding="utf-8").read()
    except FileNotFoundError as e:
        raise ValueError("版本不存在") from e


def diff_text(old: str, new: str, old_label: str = "历史版本", new_label: str = "当前") -> str:
    lines = list(difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(),
        fromfile=old_label, tofile=new_label, lineterm="", n=3))
    if not lines:
        return "（内容一致）"
    return "\n".join(lines)[:200000]
