"""一幕·YIMU 神笔 桌面版启动器（PyInstaller 入口）。

职责：准备用户数据目录 → 起本地服务（uvicorn）→ 自动打开浏览器 → 常驻运行。
端口被占（比如已开着一个实例）时自动右移，直到找到空闲端口。

数据目录：macOS ~/Library/Application Support/YiMuLit，Windows %APPDATA%/YiMuLit，
可用环境变量 YIMU_DATA_DIR 覆盖。应用包内（skillkit/static）为只读资源，不写任何数据。
"""
import os
import sys
import threading
import time
import webbrowser


def _prepare() -> None:
    """冻结模式下的早期环境准备（必须在导入 app 之前执行）。"""
    if getattr(sys, "frozen", False):
        # onedir: 资源在 _MEIPASS；桌面数据目录由 bot.paths 计算（Application Support / APPDATA）
        os.environ.setdefault("_YIMU_RESOURCE_ROOT", getattr(sys, "_MEIPASS", ""))
    # 禁用桌面版的应用内自更新（zipball 覆盖源码在冻结包里无意义且有风险）
    os.environ["YIMU_DESKTOP"] = "1"


_prepare()


def _free_port(start: int = 46821, tries: int = 20) -> int:
    import socket
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


PORT = _free_port(int(os.environ.get("YIMU_PORT") or 46821))


def _serve() -> None:
    import uvicorn
    # v4.28.0：冻结模式必须传 app 对象——"app:app" 字符串形式依赖 sys.path 上的
    # app.py 文件，PyInstaller 的 PYZ 包里找不到（实测 ERROR: Could not import module "app"）
    import app as _app
    uvicorn.run(_app.app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)


def main() -> None:
    from bot.paths import DATA_ROOT
    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    # 等服务就绪再开浏览器（最多 15 秒）
    import urllib.request
    url = f"http://127.0.0.1:{PORT}"
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    webbrowser.open(url)

    print(f"一幕·YIMU 神笔已启动：{url}（数据目录 {DATA_ROOT}）", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
