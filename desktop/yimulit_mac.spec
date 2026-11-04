# -*- mode: python ; coding: utf-8 -*-
# Mac .app 打包（windowed，无控制台；数据走 ~/Library/Application Support/YiMuLit）
import os

ROOT = os.path.dirname(os.path.abspath(SPECPATH))   # spec 在 desktop/ 下

a = Analysis(
    [os.path.join(ROOT, "desktop", "launcher.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "static"), "static"),
        (os.path.join(ROOT, "skillkit"), "skillkit"),
        (os.path.join(ROOT, "samples"), "samples"),
        (os.path.join(ROOT, "VERSION"), "."),
        (os.path.join(ROOT, "config.example.json"), "."),
    ],
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
    ],
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
    name="一幕神笔",
    debug=False,
    strip=False,
    upx=False,
    console=False,      # .app 无控制台；日志写 stdout 缓冲，排障用 CLI 版
    app=True,           # 生成 .app bundle
)
app = BUNDLE(exe, name="一幕神笔.app")
