"""提取 static/index.html 里的内联 <script>，用 node --check 做语法门禁。

只查语法（不执行 DOM 逻辑）；本机/CI 没装 node 时跳过并返回 0。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "static", "index.html")


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("[frontend] node 未安装，跳过 JS 语法检查")
        return 0
    html = open(HTML, encoding="utf-8").read()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        print("[frontend] 未找到内联 <script>")
        return 1
    bad = 0
    for i, code in enumerate(blocks, 1):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(code)
            path = f.name
        try:
            r = subprocess.run([node, "--check", path], capture_output=True, text=True)
            if r.returncode != 0:
                bad += 1
                print(f"[frontend] 第 {i} 个 script 语法错误：\n{r.stderr[:2000]}")
        finally:
            os.unlink(path)
    if bad:
        return 1
    print(f"[frontend] {len(blocks)} 个内联 script 语法通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
