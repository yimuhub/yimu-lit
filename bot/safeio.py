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


def _read_umask() -> int:
    """读一次当前 umask；只在这里短暂地清零再还原，避免运行期反复改进程状态。"""
    current = os.umask(0o022)
    os.umask(current)
    return current


# 新建文件时的默认权限：0666 去掉 umask。mkstemp 建的是 0600，
# 直接换过去会把用户本来能读的文件悄悄改成只有自己能读。
_DEFAULT_FILE_MODE = 0o666 & ~_read_umask()


def _target_mode(path: str) -> int:
    """覆盖已有文件沿用原权限；新建文件用默认权限。"""
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return _DEFAULT_FILE_MODE


def atomic_write_text(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, _target_mode(path))
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
