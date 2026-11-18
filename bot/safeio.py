"""路径安全与原子写。

任何来自 HTTP/前端的名称（项目 key、风格名、分支 id）都必须经过 safe_join，
禁止 ``..``、分隔符、绝对路径与符号链接逃逸。写 JSON/文本统一走 atomic_*，
避免断电或并发写留下半个文件。
"""
from __future__ import annotations

import json
import os
import tempfile
import time


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


def _ensure_dir(d: str) -> None:
    """确保目录真的可用再返回。

    v4.23.2：某些运行环境（沙箱把 mkdir 代理给宿主进程执行）里 `os.makedirs`
    返回时目录尚未真正落盘，紧接着 `mkstemp(dir=d)` 会 ENOENT，报出
    「No such file or directory: .../.tmp-xxxx.part」，从堆栈完全看不出是目录问题。
    这里建完确认一次，未就绪则短暂等待；正常本机环境一次就过，无额外开销。
    """
    os.makedirs(d, exist_ok=True)
    if os.path.isdir(d):
        return
    for _ in range(25):          # 最多等约 1s
        time.sleep(0.04)
        if os.path.isdir(d):
            return
    raise FileNotFoundError(f"目标目录不可用（mkdir 未生效）：{d}")


def atomic_write_text(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    _ensure_dir(d)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, _target_mode(path))
        # v4.16.1：Windows 上目标文件被并发句柄/杀毒实时扫描短暂锁定时
        # os.replace 抛 WinError 5 → 重试数次（间隔递增），仍失败才抛
        _last = None
        for _i in range(8):   # v4.16.8：Windows 并行多线程高频读写计划文件，1.9s 重试不够 → 指数退避总 ~15s
            try:
                os.replace(tmp, path)
                _last = None
                break
            except PermissionError as _e:
                _last = _e
                time.sleep(min(0.3 * (2 ** _i), 5.0))
        if _last is not None:
            raise _last
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
