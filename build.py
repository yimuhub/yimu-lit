"""构建「神笔文学创作台」自包含打包 zip。

产物：dist/yimu-lit-v<VERSION>.zip
内容：代码 + skillkit + samples + tests + run.py + 启动脚本 + config.example.json + requirements + LICENSE/NOTICE/README
不包：config.json / providers.json / secrets/ / projects/ / data/ / styles/ / skillkit/_external/ / __pycache__ / .git

用法：python build.py   （可选：python build.py --out /tmp）
"""
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(ROOT, "VERSION")


def _version() -> str:
    try:
        return open(VERSION_FILE, encoding="utf-8").read().strip() or "v1.0.0"
    except OSError:
        return "v1.0.0"


# 整目录打包（相对 ROOT）
DIRS = ["bot", "skillkit", "tests", "static", "tools", "samples"]
# 单文件
FILES = ["app.py", "run.py", "requirements.txt", "pyproject.toml",
         "package.json", "LICENSE", "NOTICE.md", "README.md", "VERSION",
         "start.sh", "start.bat", "start.ps1", "config.example.json"]
# 打包内排除
EXCLUDE_SUBDIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", "skillkit/_external"}
EXCLUDE_FILE_PATTERNS = re.compile(
    r"(\.pyc$|~$|\.swp$|\.DS_Store$|backup_\d+|\.bak$)")


def _iter_files():
    out = []
    for d in DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if x not in EXCLUDE_SUBDIRS]
            for fn in filenames:
                if EXCLUDE_FILE_PATTERNS.search(fn):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT)
                out.append(rel)
    for f in FILES:
        if os.path.exists(os.path.join(ROOT, f)):
            out.append(f)
    return sorted(set(out))


def build(out_dir: str | None = None) -> str:
    out_dir = out_dir or os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    ver = _version().lstrip("v")
    zip_path = os.path.join(out_dir, f"yimu-lit-v{ver}.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    files = _iter_files()
    n_bytes = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            full = os.path.join(ROOT, rel)
            zf.write(full, arcname=f"yimu-lit/{rel}")
            n_bytes += os.path.getsize(full)
    print(f"✅ 打包完成：{zip_path}")
    print(f"   文件数 {len(files)}，原始 {n_bytes/1024:.0f} KB")
    print(f"   解压后目录：yimu-lit/")
    print("\n验证清单（在干净机器上）：")
    print("  1. unzip yimu-lit-v*.zip && cd yimu-lit")
    print("  2. python3 -m venv .venv && . .venv/bin/activate  # 或直接用系统 python3")
    print("  3. pip install -r requirements.txt")
    print("  4. cp config.example.json config.json  # 填入 LLM 供应商与密钥")
    print("  5. python run.py                        # 启动，浏览器打开 http://127.0.0.1:46821")
    print("  6. python3 -m pytest tests/ -q -p no:warnings   # 跑测试（可选）")
    return zip_path


def main():
    out = None
    if "--out" in sys.argv:
        i = sys.argv.index("--out")
        out = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    build(out)


if __name__ == "__main__":
    main()
