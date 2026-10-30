"""一幕·YIMU 一幕·YIMU 神笔文学创作台 版本号（单一来源：读 VERSION 文件；升级只需改 VERSION 一处，全站版本随之一致）。"""
import os
def _read():
    try:
        v = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")).read().strip()
        return v[1:] if v.startswith("v") else v
    except Exception:
        return "0.0.0"
VERSION = _read()
VE = VERSION  # 兼容别名


def current() -> str:
    """v4.1.0：每次重读 VERSION 文件——热更新后版本号立即生效，不再吃 import 快照。"""
    return _read()
