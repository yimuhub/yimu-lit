# -*- mode: python ; coding: utf-8 -*-
# 一幕·YIMU 神笔 桌面打包（PyInstaller onedir）。
# 构建：pyinstaller desktop/yimulit.spec --noconfirm
import os

block_cipher = None
ROOT = os.path.dirname(os.path.abspath(SPECPATH))   # spec 在 desktop/ 下，仓库根是上级

a = Analysis(
    [os.path.join(ROOT, "desktop", "launcher.py")],
    pathex=[ROOT],
    binaries=[],
    # 只读资源随包分发；projects/data/config 等用户数据在首启时生成于用户目录
    datas=[
        (os.path.join(ROOT, "static"), "static"),
        (os.path.join(ROOT, "skillkit"), "skillkit"),
        (os.path.join(ROOT, "samples"), "samples"),
        (os.path.join(ROOT, "VERSION"), "."),
        (os.path.join(ROOT, "config.example.json"), "."),
    ],
    hiddenimports=[
        # uvicorn 的动态导入面
        "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
        "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan", "uvicorn.lifespan.on",
        # bot 内 importlib 动态导入的引擎/子模块
        "bot.script_writer", "bot.poetry_writer", "bot.prose_writer", "bot.thesis_writer",
        "bot.children_writer", "bot.news_writer", "bot.copy_writer", "bot.other_writer",
        "bot.mini_writer_base", "bot.skill_evo", "bot.feedback", "bot.eval",
        "bot.audit", "bot.supervisor", "bot.planner", "bot.nlu", "bot.assist",
        "bot.longform", "bot.export", "bot.platforms", "bot.model_rules",
        "bot.wordguard", "bot.slop_checker", "bot.purity", "bot.context",
        "bot.journal", "bot.chatsum", "bot.ingest", "bot.state",
    ],
    excludes=["pytest", "tkinter"],
    hookspath=[],
    runtime_hooks=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YiMuLit",
    debug=False,
    strip=False,
    upx=False,
    console=True,   # 保留控制台便于首启排障；发布稳定后可改 False
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="YiMuLit")
