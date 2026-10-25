"""前端语法门禁：旧版内联脚本 + v2 ES modules。

只查语法（不执行 DOM 逻辑）；本机/CI 没装 node 时跳过并返回 0。
v2 使用原生 ES modules，必须用 --input-type=module 检查，否则 import 会被误判。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGACY_HTML = os.path.join(ROOT, "static", "index.html")
V2_HTML = os.path.join(ROOT, "static", "v2", "index.html")
V2_JS = os.path.join(ROOT, "static", "v2", "js")


def _check_commonjs(node: str, code: str, label: str) -> int:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(code)
        path = f.name
    try:
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[frontend] {label} 语法错误：\n{r.stderr[:2000]}")
            return 1
        return 0
    finally:
        os.unlink(path)


def _check_module(node: str, code: str, label: str) -> int:
    r = subprocess.run([node, "--input-type=module", "--check"], input=code,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[frontend] {label} 语法错误：\n{r.stderr[:2000]}")
        return 1
    return 0


def _check_inline(node: str) -> tuple[int, int]:
    if not os.path.exists(LEGACY_HTML):
        return 0, 0
    html = open(LEGACY_HTML, encoding="utf-8").read()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    bad = 0
    for i, code in enumerate(blocks, 1):
        bad += _check_commonjs(node, code, f"index.html 第 {i} 个内联 script")
    return len(blocks), bad


def _walk_js(base: str):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "node_modules", ".git")]
        for fn in sorted(filenames):
            if fn.endswith((".js", ".mjs")):
                yield os.path.join(dirpath, fn)


def _check_v2_html(node: str) -> int:
    """index.html 只允许一个入口 module + 可选的内联引导，按 module 检查。"""
    if not os.path.exists(V2_HTML):
        return 0
    html = open(V2_HTML, encoding="utf-8").read()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    bad = 0
    for i, code in enumerate(blocks, 1):
        if not code.strip():
            continue
        bad += _check_module(node, code, f"v2/index.html 第 {i} 个内联 script")
    return bad


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("[frontend] node 未安装，跳过 JS 语法检查")
        return 0

    blocks, bad = _check_inline(node)
    checked = 0
    if os.path.isdir(V2_JS):
        for path in _walk_js(V2_JS):
            rel = os.path.relpath(path, ROOT)
            code = open(path, encoding="utf-8").read()
            bad += _check_module(node, code, rel)
            checked += 1
    bad += _check_v2_html(node)

    if bad:
        print(f"[frontend] 共 {bad} 处语法错误")
        return 1
    if blocks or checked:
        print(f"[frontend] 旧版 {blocks} 个内联 script + v2 {checked} 个 module 语法通过")
    else:
        print("[frontend] 未找到需要检查的前端脚本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
