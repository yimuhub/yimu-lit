#!/usr/bin/env python3
"""样式体检：前端 JS 里用到的 class，CSS 里是否真的定义了。

## 为什么需要它
v2 前端几乎不写 `class="..."`，而是 `h('div', { class: 'xxx' })` 这种对象属性写法
（`core/dom.js` 的 `h()` 把 `class` 键写进 `el.className`）。按 HTML 字符串扫描会
一个都找不到，于是「类名有没有样式」这件事长期没人核对：v4.23.2 排查时发现
21 个类名在样式表里根本不存在。逐个核实后只有 3 个是真缺口（右栏小节标题
`.rp-section .section-head`、设置页小标题 `.set-group-subhead`、向导 AI 按钮
`.wiz-ai-inline`，已补纯排版语义），其余 18 个要么是 JS 查询钩子，要么布局与
间距已由父级 / 兄弟规则提供——写第二条只会和既有的 gap、分隔线、内联 padding 叠加。

## 判定口径
- 只统计「字面量类名」：`class: 'a b'` / `className = 'a'` / `classList.add('a')`
  / `querySelector('.a')`。
- 模板插值里的片段（`${x}`、`${cond ? 'a' : ''}`）会被识别并跳过——这类拼接
  出来的类名另外由 NO_STYLE（刻意无样式）或运行时决定，不参与静态判定。
- NO_STYLE 是本文件维护的白名单：纯 JS 查询钩子、或已有同级类/内联样式覆盖的类名。
  加白必须写清理由，且只能是没有视觉职责的类。

## 用法
    python tools/audit_css.py           # 只报告
    python tools/audit_css.py --strict  # 有缺失就退出码 1（可进门禁）
"""
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS_DIR = os.path.join(ROOT, "static", "v2", "css")
JS_DIR = os.path.join(ROOT, "static", "v2", "js")

# 刻意不定义样式的类：要么是 JS 查询钩子，要么样式由同级类 / 后代选择器 / 内联 style 提供。
# 加白必须写原因；不允许把「该有样式但忘了写」的类塞进来。
NO_STYLE = {
    "collab-live": "与 .live-log 并列，JS 用 querySelector 定位日志容器",
    "msg-phase": "与 .msg-tag 并列，chat.js 用 querySelector 更新阶段文案",
    "msg-status": "与 .hint-text 并列，排版由 .hint-text 提供",
    "mb-view": "与 .bench-view 并列，布局由 .bench-view 提供",
    "cf-tab-text": "位于 .cf-tab 内，继承其排版",
    "bench-input": "textarea，样式由 .bench-editor textarea 提供",
    "palette-backdrop": "与 .modal-backdrop 并列，遮罩样式由后者提供",
    "progress-bar": "创建时带完整内联 style，类名仅作标识",
    "set-check": "label，创建时带完整内联 style",
    "suggest-list": "创建时带完整内联 style（grid 布局）",
    "suggestion": "创建时带完整内联 style（按钮外观）",
    # 以下 6 个是容器/行类：父级或兄弟规则已经给出布局与间距，补样式会叠加。
    # 理由与 static/v2/css/shell.css 末尾的清单保持同步。
    "chat-extras": "在 .chat-column（flex column）内，子项自带 margin",
    "cf-rec-list": "在 .cf-records（已 padding + overflow-y:auto）内，无需再滚",
    "cf-rec-sec-body": "子项 .cf-rec-item 已有 `+ .cf-rec-item{margin-top:6px}`",
    "set-list": "行靠 `.set-row + .set-row` 的 0.5px 分隔线成列，加 gap 会顶开",
    "collab-agent-actions": "父级 .collab-card 已 gap:6px，且整行只有一个按钮",
    "mb-prev": "modal body 是 .modal-body（已 padding + 滚动），非 flex，flex:1 无效",
}

_JS_CLASS_ATTR = re.compile(r"""\bclass\s*[:=]\s*(["'`])(.*?)\1""", re.S)
_JS_CLASSNAME = re.compile(r"""\bclassName\s*=\s*(["'`])(.*?)\1""", re.S)
_JS_CLASSLIST = re.compile(r"""classList\.(?:add|remove|toggle|contains)\(\s*['"`]([\w-]+)['"`]""")
_JS_QUERY = re.compile(r"""querySelector(?:All)?\(\s*['"`]\.([\w-]+)""")
# 合法类名：字母/数字/下划线/连字符，且不以数字开头
_CLASS_TOKEN = re.compile(r"^-?[A-Za-z_][\w-]*$")


def css_classes() -> set[str]:
    body = ""
    for p in sorted(glob.glob(os.path.join(CSS_DIR, "*.css"))):
        with open(p, encoding="utf-8") as f:
            body += f.read()
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)   # 注释里的 .类名 不算定义
    return set(re.findall(r"\.(-?[A-Za-z_][\w-]*)", body))


def _tokens(raw: str) -> list[str]:
    """把 `class: `a ${cond ? 'b' : ''} c`` 拆成字面量 token。

    模板插值整段先删掉再分词，避免把 `=== 'up' ? ' is-on'` 这类表达式碎片当类名。
    删完可能留下 `dot-`、`step-ava-` 这种「前半截 + 插值」的残片，它们不是一个
    完整的类名（真实类名由运行时拼接决定），以连字符结尾的一律丢弃。
    """
    cleaned = re.sub(r"\$\{[^{}]*(\{[^{}]*\}[^{}]*)*\}", " ", raw)
    out = []
    for tok in cleaned.split():
        if tok.endswith("-") or not _CLASS_TOKEN.match(tok):
            continue
        out.append(tok)
    return out


def js_classes() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}

    def add(name: str, where: str) -> None:
        if name and _CLASS_TOKEN.match(name):
            used.setdefault(name, set()).add(where)

    for p in sorted(glob.glob(os.path.join(JS_DIR, "**", "*.js"), recursive=True)):
        rel = os.path.relpath(p, ROOT)
        with open(p, encoding="utf-8") as f:
            text = f.read()
        for rx in (_JS_CLASS_ATTR, _JS_CLASSNAME):
            for m in rx.finditer(text):
                for tok in _tokens(m.group(2)):
                    add(tok, rel)
        for m in _JS_CLASSLIST.finditer(text):
            add(m.group(1), rel)
        for m in _JS_QUERY.finditer(text):
            add(m.group(1), rel)
    return used


def main() -> int:
    defined = css_classes()
    used = js_classes()
    missing = {c: w for c, w in used.items() if c not in defined and c not in NO_STYLE}
    stale = sorted(c for c in NO_STYLE if c not in used)   # 白名单里已经没人用了，该删

    print(f"CSS 定义 {len(defined)} 个类；JS 引用 {len(used)} 个；缺失 {len(missing)} 个")
    if missing:
        print("\n❌ 以下类名在前端用到了，但样式表里没有定义（也没有加白）：")
        for c, where in sorted(missing.items()):
            print(f"   {c:28} {', '.join(sorted(where))}")
        print("\n   要么补样式，要么确认是纯钩子后加进 NO_STYLE 并写明理由。")
    if stale:
        print(f"\n⚠️  NO_STYLE 里有 {len(stale)} 项已经不再被使用，可以删掉：{', '.join(stale)}")
    if not missing and not stale:
        print("✅ JS 用到的类名全部有样式或已明确加白")
    return 1 if (missing and "--strict" in sys.argv) else 0


if __name__ == "__main__":
    sys.exit(main())
