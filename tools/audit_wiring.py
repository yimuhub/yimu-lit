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

# 后端已实现、当前界面**有意不接**的接口。
#
# 判定过：这两套前端（v2 ESM 与经典版 static/index.html）里都找不到调用，
# 但接口本身有明确的非 UI 用途或已被新接口取代。加白必须写清「为什么留」，
# 并且不允许把「UI 该调没调」的接口放进来——那是 bug，不是白名单。
API_ONLY = {
    ("GET", "/api/assist/versions"): "副驾版本历史；v2 副驾只走流式，不回查历史（test_v221_assist_routes 守住契约）",
    ("GET", "/api/chapter-steps"): "章节创作档案；v2 由 SSE 实时事件流直出，无需回查",
    ("GET", "/api/chapter/version"): "单章版本（单数）；v2 一律用 /api/chapter/versions",
    ("GET", "/api/chat/history"): "聊天历史读取；v2 由 /api/state 与 SSE 承载",
    ("GET", "/api/compare"): "两段文本对比；v2 用 /api/chapter/diff",
    ("GET", "/api/download"): "整本下载；已被 /api/export 取代，保留给旧链接",
    ("GET", "/api/llm-calls"): "LLM 调用日志；排障用（运维面，无 UI 入口）",
    ("GET", "/api/preview"): "整本预览；v2 中桌用 /api/state + /api/chapter 逐章取",
    ("GET", "/api/projects"): "项目列表；已被 /api/library 取代",
    ("GET", "/api/quality"): "读 14-质量报告.md；v2 用 /api/chapter-scores + /api/score 实时算",
    ("GET", "/api/references/*/chunks"): "参考分块；v2 用 /api/references/search 检索",
    ("GET", "/api/release-log"): "发版日志；经典版与运维读",
    ("GET", "/api/review"): "读 13-修订记录.md；v2 中桌展示走 /api/state",
    ("GET", "/api/script/assist/versions"): "剧本副驾版本历史；同 /api/assist/versions",
    ("GET", "/api/skeleton"): "写作骨架；经典版入口，v2 由计划栏 + 右栏覆盖同类信息",
    ("GET", "/api/skillkit/preview"): "技能注入预览；调试用（运维面）",
    ("GET", "/api/titles"): "书名候选；v2 向导直接用 /api/plan 返回的候选",
    ("POST", "/api/chat/clear"): "清磁盘聊天记录；v2 切换作品只清界面，接口留作 API 集成",
    ("POST", "/api/plan/ensure"): "补建计划；v2 走 /api/plan/apply",
    ("POST", "/api/plan/replan"): "重新规划；v2 走 /api/plan/multi",
    ("POST", "/api/plan/window"): "规划窗口；v2 尚未接",
    ("POST", "/api/queue/action"): "队列操作；任务队列尚无 UI 入口",
    ("POST", "/api/restart"): "重启服务；一键升级走 /api/self-update（内部自己重启）",
    ("POST", "/api/script/assist/apply"): "剧本副驾应用；v2 用通用副驾",
    ("POST", "/api/script/assist/restore"): "剧本副驾回滚；同上",
    ("POST", "/api/script/assist/stream"): "剧本副驾流式（兼容入口）；v2 用 /api/assist/stream",
    ("POST", "/api/thesis/refs-check"): "论文引用核查；v2 论文工作台走 /api/state",
}


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


# 间接派发：有些模块不直接写 `api.xxx()`，而是统一交给一层薄包装
# （views/settings.js 的 `collabCall`、components/collab.js 的 `callApi`），
# 包装内部用 `api[name]` 动态取用。只认 `api.xxx` 会把这类真实调用全判成
# 「定义了但没人调」——v4.23.2 一度把 10 个在用方法报成未使用。
DYN_PICK_RE = re.compile(r"api\s*\[\s*['\"]([A-Za-z_$][\w$]*)['\"]\s*\]")
DYN_LOOKUP_RE = re.compile(r"api\s*\[\s*(\w+)\s*\]")
FN_HEAD_RE = re.compile(
    r"(?m)(?:function\s+([A-Za-z_$][\w$]*)"
    r"|\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
    r"(?:function|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>))"
)

# 包装函数体与它内部 `api[name]` 的最大距离（字符）。超过就认为不是同一个
# 函数，避免把上一个函数的 `api[name]` 算到下一个函数头上。
_DYN_WINDOW = 1500


def _dispatcher_names(src):
    """找出「用 api[name] 动态派发」的包装函数名。"""
    names = set()
    for m in DYN_LOOKUP_RE.finditer(src):
        heads = [x for x in FN_HEAD_RE.finditer(src, 0, m.start())]
        if not heads or m.start() - heads[-1].end() > _DYN_WINDOW:
            continue
        name = heads[-1].group(1) or heads[-1].group(2)
        if name:
            names.add(name)
    return names


def api_indirect_usages():
    """返回经包装函数/动态下标实际用到的 api key（不含直接 `api.xxx` 调用）。"""
    used = set()
    hits = []
    for path in js_files():
        rel = os.path.relpath(path, ROOT)
        src = open(path, encoding="utf-8").read()
        for k in DYN_PICK_RE.findall(src):          # api['xxx']
            used.add(k)
            hits.append((rel, k))
        for fn in _dispatcher_names(src):
            pat = re.compile(r"\b%s\s*\(\s*['\"]([A-Za-z_$][\w$]*)['\"]" % re.escape(fn))
            for k in pat.findall(src):
                used.add(k)
                hits.append((rel, k))
    return used, hits


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


# v4.23.2：字符类里必须包含 `(` `)`，否则 `${enc(key)}` 这种「插值里带函数调用」的
# 路径会在 `(` 处截断，`/api/library/${enc(key)}/trash` 被当成 `/api/library/`，
# 于是 api.js 里真实在用的 25 个 REST 路由全被误判成「前端零引用」。
PATH_RE = re.compile(r"""['"`](/api/[A-Za-z0-9_\-/$.{}:()]*)['"`]""")


def frontend_paths():
    out = {}
    # v2 前端（ESM 模块）+ 旧版单文件前端 static/index.html。
    # 旧版仍挂在 /classic 上（app.py:index 的兜底与 /classic 路由都在服务它），
    # 只扫 static/v2 会把「只有经典版在用」的接口误判成无人引用。
    targets = list(js_files())
    legacy = os.path.join(ROOT, "static", "index.html")
    if os.path.exists(legacy):
        targets.append(legacy)
    for path in targets:
        rel = os.path.relpath(path, ROOT)
        for no, line in enumerate(open(path, encoding="utf-8"), 1):
            for m in PATH_RE.finditer(line):
                raw = m.group(1).split("?")[0]
                p = normalize(raw)
                # 以 `/` 结尾说明是拼接前缀：`"/api/live/"+encodeURIComponent(key)`
                # 抓到的字面量是 `/api/live/`，归一后变 `/api/live`，会被误判成 404。
                # 补一段通配，按「后面还接一段」比对（后端对应 /api/live/{key}）。
                if raw.endswith("/") and not p.endswith("*"):
                    p = p + "/**"
                out.setdefault(p, []).append(f"{rel}:{no}")
    return out


def _rx(pattern: str) -> str:
    """把路径 pattern 编译成正则：`*` 匹配一段，`**` 匹配任意多段。

    `**` 专用于「前端拼接前缀」：`"/api/bkchat/"+key+"/feedback"` 抓到的字面量是
    `/api/bkchat/`，补成 `/api/bkchat/**` 才能对上后端的 `/api/bkchat/{key}/feedback`。
    """
    body = re.escape(pattern).replace(r"\*\*", "\x00").replace(r"\*", "[^/]+").replace("\x00", ".*")
    return "^" + body + "$"


def path_matches(pattern, concrete):
    """带 * 的路径按「一段通配」匹配。前端 `/api/script/${tab}` 归一成
    `/api/script/*`，后端是一堆 `/api/script/bible` 之类的字面路由，
    字面比字面永远对不上，必须按通配比。

    `**` 表示「任意多段」，只出现在前端拼接前缀（见 frontend_paths）。
    """
    if pattern == concrete:
        return True
    if "*" not in pattern and "*" not in concrete:
        return False
    if re.match(_rx(pattern), concrete):
        return True
    return bool(re.match(_rx(concrete), pattern))


def main():
    strict = "--strict" in sys.argv
    keys = api_keys()
    usages = api_usages()
    indirect, indirect_hits = api_indirect_usages()
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
    # 经 collabCall('x') / callApi('x') / api['x'] 的动态派发：同样要核对存在性，
    # 名字打错一样是运行时才炸，而且这种写法更难肉眼发现。
    for rel, k in indirect_hits:
        if k not in keys:
            bad.append((rel, 0, f"api['{k}']（动态派发）", "方法不存在"))
            continue
        used.add(k)
    if bad:
        for rel, no, what, snip in bad:
            where = f"{rel}:{no}" if no else rel
            print(f"{BAD} {what:26} {where}   {snip}")
        print(f"\n   共 {len(bad)} 处调用了不存在的方法（运行时会 TypeError）")
        hard += len(bad)
    else:
        print(f"{OK} 0 处悬空（实际用到 {len(used)} 个方法，其中 {len(indirect)} 个经动态派发）")
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
        dead.append((m, p))
    unknown = [(m, p) for m, p in dead if (m, p) not in API_ONLY]
    known = [(m, p) for m, p in dead if (m, p) in API_ONLY]
    stale = sorted(k for k in API_ONLY if k not in dead)   # 已被 UI 接上或已删，白名单该收缩

    if known:
        print(f"{OK} {len(known)} 个接口属「后端能力、界面有意不接」，已归类（见 API_ONLY 注释）")
    for m, p in stale:
        print(f"{WARN} 白名单里的 {m} {p} 已不再无人引用，可以从 API_ONLY 移除")
    if unknown:
        print(f"{BAD} {len(unknown)} 个既没被前端调用、也没归类——要么补入口，要么写进 API_ONLY 说明理由：")
        for m, p in unknown:
            print(f"     {m} {p}")
    elif not stale:
        print(f"{OK} 全部后端路由都有归属（前端可达 / 有意不接）")
    else:
        print(f"{OK} 全部有前端引用")

    return 1 if (strict and hard) else 0


if __name__ == "__main__":
    sys.exit(main())
