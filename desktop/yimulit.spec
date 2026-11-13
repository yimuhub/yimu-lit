# -*- mode: python ; coding: utf-8 -*-
# 一幕·YIMU 神笔 桌面打包（PyInstaller onedir，控制台版——排障用）。
# 构建：pyinstaller desktop/yimulit.spec --noconfirm
import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.dirname(os.path.abspath(SPECPATH))   # spec 在 desktop/ 下

# pywebview 原生窗口壳（Mac=WKWebView / Win=WebView2）及其平台后端
wv_datas, wv_bins, wv_hidden = collect_all("webview")

a = Analysis(
    [os.path.join(ROOT, "desktop", "launcher.py")],
    pathex=[ROOT],
    binaries=wv_bins,
    datas=[
        (os.path.join(ROOT, "static"), "static"),
        (os.path.join(ROOT, "skillkit"), "skillkit"),
        (os.path.join(ROOT, "samples"), "samples"),
        (os.path.join(ROOT, "VERSION"), "."),
        (os.path.join(ROOT, "config.example.json"), "."),
    ] + wv_datas,
    hiddenimports=[
        "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
        "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan", "uvicorn.lifespan.on",
        "bot.script_writer", "bot.poetry_writer", "bot.prose_writer", "bot.thesis_writer",
        "bot.children_writer", "bot.news_writer", "bot.copy_writer", "bot.other_writer",
        "bot.mini_writer_base", "bot.skill_evo", "bot.feedback", "bot.eval",
        "bot.audit", "bot.supervisor", "bot.planner", "bot.nlu", "bot.assist",
        "bot.longform", "bot.export", "bot.platforms", "bot.model_rules",
        "bot.wordguard", "bot.slop_checker", "bot.purity", "bot.context",
        "bot.journal", "bot.chatsum", "bot.ingest", "bot.state",
        # v4.36.0：形态标尺 / 工艺体检 / 写前事实卡（均为函数内 import，显式登记防漏打）
        "bot.taste", "bot.craft_check", "bot.factcard",
        "bot.delivery_gate", "bot.delivery_spec",
    ] + wv_hidden,
    excludes=["pytest", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YiMuLit",
    icon=os.path.join(ROOT, "desktop", "icon", "icon.ico"),
    debug=False,
    strip=False,
    upx=False,
    console=True,   # 控制台版保留排障输出；用户安装包走 mac/windowed 或 Inno 静默
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="YiMuLit")
