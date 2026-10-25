"""路径安全与原子写。

任何来自 HTTP/前端的名称（项目 key、风格名、分支 id）都必须经过 safe_join，
禁止 ``..``、分隔符、绝对路径与符号链接逃逸。写 JSON/文本统一走 atomic_*，
避免断电或并发写留下半个文件。
"""
from __future__ import annotations

import json
import os
import tempfile


class UnsafePathError(ValueError):
    pass


def is_safe_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    n = name.strip()
    if not n or n in (".", ".."):
        return False
    if "\x00" in n:
        return False
    if "/" in n or "\\" in n:
        return False
    if os.path.isabs(n) or os.path.basename(n) != n:
        return False
    return True


def safe_join(root: str, name: str, *, label: str = "名称") -> str:
    """把 name 安全地拼到 root 下；越界或非法直接抛 UnsafePathError。"""
    if not is_safe_name(name):
        raise UnsafePathError(f"{label}非法：{name!r}")
    root = os.path.realpath(root)
    p = os.path.realpath(os.path.join(root, name))
    try:
        inside = os.path.commonpath([root, p]) == root
    except ValueError:
        inside = False
    if p == root or not inside:
        raise UnsafePathError(f"{label}越界：{name!r}")
    return p


def safe_existing(root: str, name: str, *, label: str = "名称") -> str | None:
    try:
        p = safe_join(root, name, label=label)
    except UnsafePathError:
        return None
    return p if os.path.exists(p) else None


def atomic_write_text(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, data, **kwargs) -> None:
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("indent", 2)
    atomic_write_text(path, json.dumps(data, **kwargs))
