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


def _prepare() -> None:
    """冻结模式下的早期环境准备（必须在导入 app 之前执行）。"""
    if getattr(sys, "frozen", False):
        # onedir: 资源在 _MEIPASS；桌面数据目录由 bot.paths 计算（Application Support / APPDATA）
        os.environ.setdefault("_YIMU_RESOURCE_ROOT", getattr(sys, "_MEIPASS", ""))
    # 禁用桌面版的应用内自更新（zipball 覆盖源码在冻结包里无意义且有风险）
    os.environ["YIMU_DESKTOP"] = "1"


_prepare()

if getattr(sys, "frozen", False):
    # .app 无控制台：stdout/stderr 重定向到数据目录日志（便于排障）
    try:
        from bot.paths import ensure_data_dir
        _log = os.path.join(ensure_data_dir("logs"), "launcher.log")
        sys.stdout = sys.stderr = open(_log, "a", encoding="utf-8", buffering=1)
    except Exception:
        pass


def _existing_instance():
    """单实例：数据目录 lock 文件里有存活实例（端口可访问）→ 返回其端口，直接复用。"""
    import urllib.request
    try:
        from bot.paths import data
        lock = data("instance.lock")
        if os.path.exists(lock):
            try:
                port = int(open(lock, encoding="utf-8").read().strip())
            except Exception:
                return None
            try:
                urllib.request.urlopen("http://127.0.0.1:" + str(port), timeout=1)
                return port
            except Exception:
                return None
    except Exception:
        return None
    return None


def _write_lock(port):
    try:
        from bot.paths import data
        with open(data("instance.lock"), "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:
        pass


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


def _wait_ready() -> bool:
    import urllib.request
    url = f"http://127.0.0.1:{PORT}"
    for _ in range(80):
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def main() -> None:
    from bot.paths import DATA_ROOT
    # v4.28.2 单实例：已有存活实例 → 直接开浏览器复用，不再起第二个服务
    existing = _existing_instance()
    if existing:
        print("一幕·YIMU 神笔已在运行（端口 " + str(existing) + "），打开现有窗口", flush=True)
        import webbrowser
        webbrowser.open("http://127.0.0.1:" + str(existing))
        return

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    _write_lock(PORT)

    ready = _wait_ready()
    print(f"一幕·YIMU 神笔已启动：http://127.0.0.1:{PORT}（数据目录 {DATA_ROOT}）", flush=True)

    # v4.28.1：原生窗口优先（pywebview —— Mac 是 WKWebView、Windows 是 WebView2，
    # 关窗即退出）；缺 pywebview 或显式 YIMU_NO_WINDOW=1 时回退系统浏览器。
    headless = os.environ.get("YIMU_NO_WINDOW") == "1"
    if ready and not headless:
        try:
            import webview
            webview.create_window(
                "一幕·YIMU 神笔",
                f"http://127.0.0.1:{PORT}",
                width=1440, height=900, min_size=(1024, 680),
            )
            webview.start()   # 阻塞到窗口关闭 → 进程退出（服务是 daemon 线程）
            return
        except Exception as e:
            print(f"[launcher] 原生窗口不可用（{type(e).__name__}: {e}），回退浏览器", flush=True)
    if ready and not headless:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
