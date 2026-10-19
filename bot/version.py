"""novel-bot 版本号（读 VERSION 文件，当前 v0.5.0 多Agent创作宇宙）。"""
import os
def _read():
    try:
        v = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")).read().strip()
        return v[1:] if v.startswith("v") else v
    except Exception:
        return "0.0.0"
VERSION = _read()
VE = VERSION  # 兼容别名
