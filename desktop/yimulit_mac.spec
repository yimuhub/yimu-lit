# -*- mode: python ; coding: utf-8 -*-
# Mac .app 打包（windowed 原生窗口：pywebview + WKWebView；数据走 Application Support）
import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.dirname(os.path.abspath(SPECPATH))

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
    ] + wv_hidden,
    excludes=["pytest", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="YiMuLit",
    debug=False,
    strip=False,
    upx=False,
    console=False,   # .app 无控制台；launcher 日志重定向到数据目录 launcher.log
    app=True,
)
app = BUNDLE(
    exe,
    name="YiMuLit.app",
    icon=os.path.join(ROOT, "desktop", "icon", "icon.icns"),
    info_plist={
        "CFBundleName": "YiMuLit",
        "CFBundleDisplayName": "一幕·YIMU 神笔",
        "CFBundleDisplayName": "一幕·YIMU 神笔",
        "NSLocalNetworkUsageDescription": "一幕神笔在本机启动创作服务，需要访问本地网络（127.0.0.1）。",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
