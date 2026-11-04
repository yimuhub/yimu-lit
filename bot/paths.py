"""运行路径解析（v4.28.0 桌面打包的地基）。

双根模型：
  · RESOURCE_ROOT —— 只读资源，随应用分发：skillkit/、static/、samples/、VERSION、
    config.example.json。开发模式下就是仓库根目录。
  · DATA_ROOT    —— 用户可写数据：projects/、data/、config.json、providers.json、
    secrets/、styles/、schedules.json 等。开发模式下与 RESOURCE_ROOT 相同（仓库根），
    桌面（PyInstaller 冻结）模式下为系统用户数据目录，保证应用包只读、数据可迁移。

约定：
  · 代码里禁止再手写 `dirname(dirname(abspath(__file__)))` 这类自推导根路径，
    一律从本模块导入（DATA_ROOT / RESOURCE_ROOT / CONFIG_PATH）。
  · CONFIG_PATH 保证存在：首次访问时自动从资源里的 config.example.json 拷贝生成，
    桌面版开箱即用；开发模式不受影响（仓库里已有 config.json）。
"""
from __future__ import annotations

import os
import shutil
import sys

APP_NAME = "YiMuLit"

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    # PyInstaller onedir：资源解包在 _MEIPASS（Mac .app 内 / Windows _internal）
    RESOURCE_ROOT = os.environ.get("_YIMU_RESOURCE_ROOT") or getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    if sys.platform == "darwin":
        _base = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
    elif os.name == "nt":
        _base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
    else:
        _base = os.path.expanduser(f"~/.{APP_NAME.lower()}")
    DATA_ROOT = os.environ.get("YIMU_DATA_DIR") or _base
else:
    RESOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_ROOT = os.environ.get("YIMU_DATA_DIR") or RESOURCE_ROOT

os.makedirs(DATA_ROOT, exist_ok=True)


def resource(*parts: str) -> str:
    """只读资源路径（随应用分发）。"""
    return os.path.join(RESOURCE_ROOT, *parts)


def data(*parts: str) -> str:
    """用户可写数据路径。"""
    return os.path.join(DATA_ROOT, *parts)


def ensure_data_dir(*parts: str) -> str:
    p = data(*parts)
    os.makedirs(p, exist_ok=True)
    return p


_CONFIG_READY = False


def config_file() -> str:
    """config.json 的规范路径；不存在时从资源模板自动生成（桌面首启开箱即用）。"""
    global _CONFIG_READY
    p = data("config.json")
    if _CONFIG_READY and not os.path.exists(p):
        # 运行中被删（罕见）：重新生成一次
        _seed_config(p)
        return p
    if not _CONFIG_READY:
        if not os.path.exists(p):
            _seed_config(p)
        _CONFIG_READY = True
    return p


def _seed_config(p: str) -> None:
    template = resource("config.example.json")
    try:
        if os.path.exists(template):
            shutil.copyfile(template, p)
            return
    except Exception:
        pass
    with open(p, "w", encoding="utf-8") as f:
        f.write("{}")


CONFIG_PATH = data("config.json")
if not os.path.exists(CONFIG_PATH) and FROZEN:
    # 桌面首启：从资源模板生成（开发模式仓库已有 config.json，不动）
    _tpl = resource("config.example.json")
    try:
        if os.path.exists(_tpl):
            shutil.copyfile(_tpl, CONFIG_PATH)
    except Exception:
        pass
