"""桌面版自动更新（v4.29.0）——overlay 运行时模型。

原理：PyInstaller 冻结包里的代码（PYZ）无法被覆盖更新，所以桌面版采用
「overlay 运行时层」：launcher 启动时若发现 DATA_ROOT/runtime/ 里有 bot 代码，
就把这一层插到 sys.path 最前（冻结 importer 对 app/bot* 让路），应用代码
即从可写的 overlay 层加载——更新 = 覆盖 overlay + 重启进程，无需管理员、无需重装。

流程（/api/self-update 桌面分支）：
  1. GitHub releases/latest 取最新 tag，与当前 VERSION（overlay 优先）比较；
  2. 下载 zipball → 白名单解压到 runtime_staging（bot/static/skillkit/VERSION/
     config.example.json；projects/config.json 等用户数据绝不动）；
  3. 校验 staging 完整性（VERSION + bot/paths.py 存在）→ 原子换名到 runtime；
  4. 响应 need_restart → 前端确认 → /api/restart 桌面分支重启整个进程。

依赖边界：更新只覆盖本项目代码；新版本若引入新的第三方依赖，冻结包里没有的
会在重启时报 ImportError——launcher 会把错误写日志并回退浏览器提示（v1 边界）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import zipfile

from .paths import DATA_ROOT

REPO = "yimuhub/yimu-lit"
RUNTIME_DIR = os.path.join(DATA_ROOT, "runtime")
STAGING_DIR = os.path.join(DATA_ROOT, "runtime_staging")
#: 允许从更新包覆盖的路径（白名单——用户数据绝不在此列）
WHITELIST = ("bot", "static", "skillkit", "VERSION", "config.example.json", "requirements.txt")


def overlay_active() -> bool:
    """当前进程是否正运行在 overlay 层上。"""
    return bool(os.environ.get("YIMU_OVERLAY"))


def current_version() -> str:
    """当前生效版本：overlay 的 VERSION 优先，回落冻结包的。"""
    for base in (RUNTIME_DIR, ):
        try:
            with open(os.path.join(base, "VERSION"), encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            continue
    try:
        from .paths import resource
        with open(resource("VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "0.0.0"


def _ver_tuple(v: str):
    parts = []
    for x in v.strip().lstrip("v").split("."):
        try:
            parts.append(int(x))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def fetch_latest() -> dict:
    """GitHub latest release：{ok, has_update, remote, local, tag, zipball, notes}。

    v4.29.0：首选 releases/latest 的 302 重定向解析（github.com 页面跳转不走
    API 限流池——未认证 API 只有 60 次/h，共享出口 IP 必撞 403）；API 作为后备。
    可用环境变量 YIMU_GITHUB_TOKEN 提升配额。"""
    import urllib.request
    local = current_version()

    def _auth_header():
        tok = os.environ.get("YIMU_GITHUB_TOKEN") or ""
        return {"Authorization": f"token {tok}"} if tok else {}

    tag = ""
    try:
        # 首选：releases/latest 页面 302 → Location 带 tag（不受 API 限流影响）
        req = urllib.request.Request(
            f"https://github.com/{REPO}/releases/latest", method="HEAD",
            headers={**_auth_header(), "User-Agent": "yimu-lit"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            final = resp.geturl()
        m = re.search(r"/tag/([^/]+)$", final.rstrip("/"))
        if m:
            tag = m.group(1).lstrip("v")
    except Exception:
        tag = ""
    if not tag:
        try:
            url = f"https://api.github.com/repos/{REPO}/releases/latest"
            req = urllib.request.Request(url, headers=_auth_header())
            with urllib.request.urlopen(req, timeout=15) as resp:
                rel = json.load(resp)
            tag = str(rel.get("tag_name") or "").lstrip("v")
        except Exception as e:
            return {"ok": False, "error": f"检查更新失败（网络/GitHub 不可达）：{type(e).__name__}"}
    if not tag:
        return {"ok": False, "error": "GitHub 上没有可用的 Release"}
    has_update = _ver_tuple(tag) > _ver_tuple(local)
    return {"ok": True, "local": local, "remote": tag, "tag": "v" + tag,
            "has_update": has_update,
            "zipball": f"https://github.com/{REPO}/archive/refs/tags/v{tag}.zip",
            "notes": ""}


def _download(zipball_url: str, dest: str) -> None:
    import urllib.request
    with urllib.request.urlopen(zipball_url, timeout=120) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def _extract_whitelist(zip_path: str, staging: str) -> int:
    """解压白名单路径到 staging（剥掉 zipball 顶层目录前缀）。返回解压文件数。"""
    n = 0
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        members = os.listdir(tmp)
        if len(members) != 1:
            raise RuntimeError(f"更新包结构异常：顶层 {len(members)} 项")
        top = os.path.join(tmp, members[0])
        for name in WHITELIST:
            src = os.path.join(top, name)
            if not os.path.exists(src):
                continue
            dst = os.path.join(staging, name)
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
            n += 1
    return n


def _validate_staging(staging: str) -> None:
    for must in ("VERSION", os.path.join("bot", "paths.py"), os.path.join("bot", "__init__.py")):
        if not os.path.exists(os.path.join(staging, must)):
            raise RuntimeError(f"更新包不完整：缺 {must}")
    _check_new_dependencies(staging)


def _check_new_dependencies(staging: str) -> None:
    """依赖预检：新版 requirements 引入了冻结包没有的第三方包 → 拒绝本次更新
    （提示重新下载完整安装包），避免重启后 ImportError 把应用搞挂。"""
    import importlib.util
    req = os.path.join(staging, "requirements.txt")
    if not os.path.exists(req):
        return
    missing = []
    try:
        for line in open(req, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            top = re.split(r"[\[<>=!~;\s]", line)[0].strip().lower()
            if not top:
                continue
            # 依赖名 → 导入名的已知差异（先查映射再做 -→_ 归一，python-docx→docx 才能命中）
            top = {"pyyaml": "yaml", "python-docx": "docx", "pypdf": "pypdf",
                   "python-multipart": "multipart", "pillow": "pil",
                   "beautifulsoup4": "bs4", "pywebview": "webview"}.get(top, top.replace("-", "_"))
            if importlib.util.find_spec(top) is None:
                missing.append(top)
    except Exception as _e:
        # v4.29.0：静默吞异常会让预检失效——至少留痕
        print(f"[updater] 依赖预检异常：{type(_e).__name__}: {_e}", file=sys.stderr, flush=True)
        return
    if missing:
        raise RuntimeError("此更新需要新依赖：" + "、".join(missing) +
                           "——请到 Release 重新下载完整安装包升级")


def desktop_update(cfg=None) -> dict:
    """桌面版一键更新主体。返回 {ok, updated, need_restart, from, to, msg}。"""
    latest = fetch_latest()
    if not latest.get("ok"):
        return {"ok": False, "msg": latest.get("error")}
    if not latest.get("has_update"):
        return {"ok": True, "updated": False,
                "msg": f"已是最新版本 v{latest.get('local')}"}
    # 幂等保护：runtime 层已装到目标版本 → 无需更新
    if _ver_tuple(current_version()) >= _ver_tuple(latest["remote"]):
        return {"ok": True, "updated": False, "msg": f"已是最新版本 v{current_version()}"}
    zipball = latest["zipball"]
    tmp_zip = os.path.join(tempfile.gettempdir(), f"yimulit_update_{latest['remote']}.zip")
    try:
        _download(zipball, tmp_zip)
        if os.path.exists(STAGING_DIR):
            shutil.rmtree(STAGING_DIR, ignore_errors=True)
        os.makedirs(STAGING_DIR, exist_ok=True)
        _extract_whitelist(tmp_zip, STAGING_DIR)
        _validate_staging(STAGING_DIR)
    except Exception as e:
        return {"ok": False, "msg": f"更新包下载/解压失败：{e}"}
    finally:
        try:
            os.remove(tmp_zip)
        except OSError:
            pass
    # 原子换名：旧 runtime 让位（保留一代便于回滚排查）
    backup = os.path.join(DATA_ROOT, "runtime_backup")
    if os.path.exists(backup):
        shutil.rmtree(backup, ignore_errors=True)
    if os.path.exists(RUNTIME_DIR):
        shutil.move(RUNTIME_DIR, backup)
    shutil.move(STAGING_DIR, RUNTIME_DIR)
    return {"ok": True, "updated": True, "need_restart": True,
            "from": latest["local"], "to": latest["remote"],
            "msg": f"已更新 v{latest['local']} → v{latest['remote']}（overlay 层），重启进程生效"}


def desktop_restart() -> None:
    """重启桌面进程：新进程带 YIMU_OVERLAY 环境变量加载 runtime 层。"""
    import subprocess
    import sys
    exe = sys.executable
    argv = [exe] + sys.argv
    env = dict(os.environ)
    env["YIMU_OVERLAY"] = RUNTIME_DIR
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        subprocess.Popen(argv, env=env, creationflags=flags, close_fds=True)
        os._exit(0)
    try:
        os.execve(exe, argv, env)   # Mac/Linux：原地换镜像，无端口竞争
    except Exception:
        subprocess.Popen(argv, env=env, start_new_session=True)
        os._exit(0)
