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


def _bootstrap_overlay() -> None:
    """v4.29.0：若存在 runtime overlay 层（自动更新已装的新代码），把它插到
    sys.path 最前，并让冻结 importer 对 app/bot* 让路——应用代码即从可写
    overlay 层加载。必须在任何 import bot / import app 之前执行。"""
    if not getattr(sys, "frozen", False):
        return   # 开发模式：仓库本身就是代码源，无 overlay 概念
    # DATA_ROOT 推导与 bot/paths.py 保持一致（此时还不能 import bot.paths）
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/YiMuLit")
    elif os.name == "nt":
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "YiMuLit")
    else:
        base = os.path.expanduser("~/.yimulit")
    base = os.environ.get("YIMU_DATA_DIR") or base
    overlay = os.environ.get("YIMU_OVERLAY") or os.path.join(base, "runtime")
    if not os.path.exists(os.path.join(overlay, "bot", "paths.py")):
        return   # 没有 overlay：用冻结包内置代码
    sys.path.insert(0, overlay)
    os.environ["YIMU_OVERLAY"] = overlay
    # 冻结 importer 对 app / bot* 让路 → PathFinder 从 overlay 加载新代码；
    # 其余（httpx/uvicorn/stdlib…）仍走冻结包，行为不变。
    for finder in sys.meta_path:
        if type(finder).__name__ == "FrozenImporter":
            _orig = finder.find_spec
            def _patched(name, *a, _orig=_orig, **k):
                if name == "app" or name == "bot" or name.startswith("bot."):
                    return None
                return _orig(name, *a, **k)
            try:
                finder.find_spec = _patched
            except Exception:
                pass
            break


_bootstrap_overlay()

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


def _remove_lock():
    try:
        from bot.paths import data
        p = data("instance.lock")
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


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
    # v4.30.2 修复：已有存活实例 → 静默退出（macOS 启动同一 .app 会自动把已开的
    # 窗口带到前台）。此前这里 webbrowser.open 复用旧实例——用户双击客户端时
    # 若 46821 被占（开发服务器/残留实例），就会「跳转到网页」而不是打开客户端，
    # 且那个页面常因令牌失效满屏 401。冻结模式绝不弹浏览器。
    existing = _existing_instance()
    if existing:
        print("一幕·YIMU 神笔已在运行（端口 " + str(existing) + "），激活已有窗口", flush=True)
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
            # v4.32.0：应用名统一中文「一幕神笔」；窗口句柄注入 desktop_bridge，
            # 导出走原生「存储」对话框（webview 不支持 <a download>）
            _win = webview.create_window(
                "一幕神笔",
                f"http://127.0.0.1:{PORT}",
                width=1440, height=900, min_size=(1024, 680),
            )
            from bot import desktop_bridge
            desktop_bridge.WINDOW = _win
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
    import atexit
    atexit.register(lambda: _remove_lock())
    main()
