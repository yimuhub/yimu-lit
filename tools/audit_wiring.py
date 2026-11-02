#!/usr/bin/env python3
"""接线体检：前端调的 API 是否真的存在、前端请求的路径后端是否真的注册。

## 为什么需要它
v4.21.1 清「死 API」时删掉了 `api.onboarding`，但唯一调用方 `views/wizard.js`
是 `await import()` 懒加载的，静态扫描扫不到 → 判成死代码 → 新建作品弹窗
当场 `TypeError`、正文全空，而且**没有任何检查会报错**：eslint 只查未定义变量
（`api.onboarding` 语法完全合法），pytest 只测后端。

这个脚本把两件事对齐：
  1. `api.<key>` 的调用 ←→ api.js 里真实存在的 key（用 node 直接读对象，不靠正则猜）
  2. 前端出现的 `/api/xxx` 字面量 ←→ app.py 真实注册的路由

## 用法
    python tools/audit_wiring.py          # 只报告
    python tools/audit_wiring.py --strict # 有悬空调用/404 路径就退出码 1（可进门禁）
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, "static", "v2", "js")
API_JS = os.path.join(JS_DIR, "core", "api.js")

OK, WARN, BAD = "✅", "⚠️ ", "❌"


# ────────────────────────────────────────── 1. api.js 真实 key（问 node 要）
def api_keys():
    """返回 {key: set(子key)}；node 拿不到时退回正则（会宽松些）。"""
    script = (
        "import(%s).then(m=>{const a=m.api,o={};"
        "for(const k of Object.keys(a)){const v=a[k];"
        "o[k]=(v&&typeof v==='object'&&!Array.isArray(v))?Object.keys(v):[];}"
        "console.log(JSON.stringify(o));})"
        ".catch(e=>{console.error(e.message);process.exit(1);})"
        % json.dumps(API_JS)
    )
    try:
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip().startswith("{"):
            return {k: set(v) for k, v in json.loads(out.stdout.strip()).items()}
    except Exception:
        pass
    # 兜底：正则抓 `  key:` 行
    src = open(API_JS, encoding="utf-8").read()
    i = src.find("export const api = {")
    body = src[i:] if i >= 0 else src
    return {m.group(1): set() for m in re.finditer(r"(?m)^  ([A-Za-z_$][\w$]*)\s*:", body)}


# ────────────────────────────────────────── 2. 前端怎么用 api.*
def js_files():
    for base, _, files in os.walk(JS_DIR):
        for f in files:
            if f.endswith(".js"):
                yield os.path.join(base, f)


# 前面不能是标识符/点/斜杠：排除 import '.../api.js'、https://api.deepseek.com
# 首字母不取 $：模板字符串里的 `api.${name}` 是提示文案，不是调用
USAGE_RE = re.compile(r"(?<![\w$/.])api\s*\.\s*([A-Za-z_][\w$]*)(?:\s*\.\s*([A-Za-z_][\w$]*))?")


def api_usages():
    """**整文件扫描**：`api` 与 `.方法` 经常换行写（`api\\n  .onboarding()`），
    按行 grep 会漏——上一版就是这么把 onboarding 报成「没人调用」的。"""
    out = []
    for path in js_files():
        rel = os.path.relpath(path, ROOT)
        src = open(path, encoding="utf-8").read()
        for m in USAGE_RE.finditer(src):
            head, sub = m.group(1), m.group(2)
            if head == "js":
                continue
            no = src.count("\n", 0, m.start()) + 1
            lines = src.splitlines()
            line = lines[no - 1].strip() if lines else ""
            # 注释里的示例写法（`统一走 api.xxx`）不是调用
            if line.startswith(("//", "*", "/*", "*/")):
                continue
            out.append((rel, no, head, sub, line[:110]))
    return out


# ────────────────────────────────────────── 3. 路由
def normalize(p):
    """把 ${x} / {id} / :id 统一成 *。注意先处理 ${...}，否则会剩下一个 $。"""
    p = re.sub(r"\$\{[^}]*\}", "*", p)
    p = re.sub(r"\{[^}]*\}", "*", p)
    p = re.sub(r":[A-Za-z_]\w*", "*", p)
    p = p.rstrip("/") or "/"
    return p


def backend_routes():
    sys.path.insert(0, ROOT)
    os.environ.setdefault("SHENBI_AUTH", "0")
    import app as appmod  # noqa: E402

    routes = set()
    for r in appmod.app.routes:
        path = getattr(r, "path", None)
        if not path or not path.startswith("/api"):
            continue
        for m in getattr(r, "methods", None) or set():
            if m in ("HEAD", "OPTIONS"):
                continue
            routes.add((m, normalize(path)))
    return routes


PATH_RE = re.compile(r"""['"`](/api/[A-Za-z0-9_\-/$.{}:]*)['"`]""")


def frontend_paths():
    out = {}
    for path in js_files():
        rel = os.path.relpath(path, ROOT)
        for no, line in enumerate(open(path, encoding="utf-8"), 1):
            for m in PATH_RE.finditer(line):
                p = normalize(m.group(1).split("?")[0])
                out.setdefault(p, []).append(f"{rel}:{no}")
    return out


def path_matches(pattern, concrete):
    """带 * 的路径按「一段通配」匹配。前端 `/api/script/${tab}` 归一成
    `/api/script/*`，后端是一堆 `/api/script/bible` 之类的字面路由，
    字面比字面永远对不上，必须按通配比。"""
    if pattern == concrete:
        return True
    if "*" not in pattern and "*" not in concrete:
        return False
    rx = "^" + re.escape(pattern).replace(r"\*", "[^/]+") + "$"
    if re.match(rx, concrete):
        return True
    rx2 = "^" + re.escape(concrete).replace(r"\*", "[^/]+") + "$"
    return bool(re.match(rx2, pattern))


def main():
    strict = "--strict" in sys.argv
    keys = api_keys()
    usages = api_usages()
    hard = 0

    print("=" * 70)
    print(f"1) 前端 api.<key>  ←→  api.js（真实 {len(keys)} 个方法）")
    print("=" * 70)
    bad = []
    used = set()
    for rel, no, head, sub, snip in usages:
        if head not in keys:
            bad.append((rel, no, f"api.{head}", snip))
            continue
        if sub and keys[head] and sub not in keys[head]:
            bad.append((rel, no, f"api.{head}.{sub}", snip))
            continue
        used.add(head)
    if bad:
        for rel, no, what, snip in bad:
            print(f"{BAD} {what:26} {rel}:{no}   {snip}")
        print(f"\n   共 {len(bad)} 处调用了不存在的方法（运行时会 TypeError）")
        hard += len(bad)
    else:
        print(f"{OK} 0 处悬空（实际用到 {len(used)} 个方法）")
    unused = sorted(set(keys) - used)
    if unused:
        print(f"{WARN} 定义了但前端没调（可能给 mock/外部用）：{', '.join(unused)}")

    print()
    print("=" * 70)
    print("2) 前端 /api 字面量  ←→  app.py 路由")
    print("=" * 70)
    try:
        routes = backend_routes()
    except Exception as e:
        # 导不进 app 就等于「没法核对」，不能说成通过——这正是本仓库反复踩的假绿。
        print(f"{BAD} 无法导入 app.py（{e.__class__.__name__}: {e}）")
        print("   路由比对无法进行；请用装了依赖的解释器跑（见 requirements.txt）")
        return 1
    fe = frontend_paths()
    route_paths = sorted({p for _, p in routes})
    missing = []
    for p, where in sorted(fe.items()):
        if any(path_matches(p, rp) for rp in route_paths):
            continue
        missing.append((p, where[0]))
    if missing:
        for p, where in missing:
            print(f"{BAD} 后端没有 {p:32} ← {where}")
        print(f"\n   共 {len(missing)} 个前端请求会 404")
        hard += len(missing)
    else:
        print(f"{OK} 前端 {len(fe)} 个 /api 路径全部有对应路由")

    print()
    print("=" * 70)
    print("3) 后端路由无人引用（仅提示，可能是 CLI/启动脚本在用）")
    print("=" * 70)
    dead = []
    for m, p in sorted(routes):
        # 前端路径多是用模板拼的（`/api/providers/${id}`），必须按通配比，
        # 直接字符串相等会把一大批正常接口误判成「没人用」。
        if any(path_matches(p, fp) for fp in fe):
            continue
        dead.append(f"{m} {p}")
    if dead:
        print(f"{WARN} {len(dead)} 个：")
        for d in dead[:40]:
            print("     " + d)
        if len(dead) > 40:
            print(f"     …… 另有 {len(dead) - 40} 个")
    else:
        print(f"{OK} 全部有前端引用")

    return 1 if (strict and hard) else 0


if __name__ == "__main__":
    sys.exit(main())
