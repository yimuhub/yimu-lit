"""v4.32.0 桌面桥：pywebview 窗口句柄由 desktop/launcher.py 注入。

app.py 据此判断「桌面模式」并弹原生文件对话框——桌面 webview（macOS WKWebView /
Windows WebView2）不支持 <a download>，blob 下载会变成导航到文件内容页；
网页（源码运行）模式 WINDOW 恒为 None，一切走浏览器下载。
"""
import os

WINDOW = None   # webview.Window，由 launcher 在 create_window 后注入


def is_desktop() -> bool:
    return WINDOW is not None


def save_dialog(default_name: str):
    """弹原生「存储」对话框；用户取消或不可用返回 None，否则返回完整路径。
    pywebview 的 create_file_dialog 可在任意线程调用（后端请求线程即发即用）。"""
    if WINDOW is None:
        return None
    import webview
    result = WINDOW.create_file_dialog(
        webview.SAVE_DIALOG,
        save_filename=default_name or "export",
    )
    if not result:
        return None
    if isinstance(result, str):
        return result
    return result[0] if len(result) else None


def pick_folder():
    """弹原生「选择文件夹」对话框（预留）；取消返回 None。"""
    if WINDOW is None:
        return None
    import webview
    result = WINDOW.create_file_dialog(webview.FOLDER_DIALOG)
    if not result:
        return None
    return result[0] if len(result) else None


def reveal_in_os(path: str):
    """在访达/资源管理器里显示导出的文件（best effort）。"""
    try:
        if os.name == "nt":
            os.startfile("/select," + os.path.normpath(path))   # noqa: S606
        else:
            import subprocess
            subprocess.Popen(["open", "-R", path])   # noqa: S603
    except Exception:
        pass
