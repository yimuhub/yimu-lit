"""前端资源版本戳一致性：index.html 的 `?v=` 与 app.js 的 CSS_V 必须相同。

## 为什么要有这组测试
`static/v2/index.html` 用 `?v=291` 给常驻 CSS/JS 做缓存戳，`app.js` 里另有一个
`const CSS_V = '291'` 给「路由级按需加载」的 CSS（settings/skills/chatfloat）用。
两者是**两个独立的字面量**，改 CSS 时必须一起 +1。

漏改的后果不是报错，而是「半新半旧」：浏览器认为常驻 CSS 已经变了（新 URL），
但懒加载的 CSS 还是旧 URL，于是继续用缓存里的旧样式——用户看到设置页间距、
浮窗记录区跟别人不一样，刷新几次又好了，极难复现。

这类问题在 v4.23.2 之前没有任何检查（pytest 只测后端，eslint 不看你写的字符串），
所以固化成断言：谁只改了一边，`pytest` 直接红。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "static", "v2", "index.html")
APP_JS = os.path.join(ROOT, "static", "v2", "js", "app.js")

_V_IN_HTML = re.compile(r'(?:href|src)="(/v2/[^"?]+)\?v=(\d+)"')
_CSS_V = re.compile(r"""CSS_V\s*=\s*['"](\d+)['"]""")
_CSS_LOAD = re.compile(r"""loadCss\(`?['"]?(/v2/css/[\w.-]+\.css)""")
# 引用时可能带 `?v=291`，也可能不带；只取路径部分做存在性检查
_CSS_SRC = re.compile(r'(?:href|src)="(/v2/[^"?]+)')


def _html():
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def _app_js():
    with open(APP_JS, encoding="utf-8") as f:
        return f.read()


def _local(url):
    """把前端 URL 映射到磁盘路径：`/v2/xxx` 由 `static/v2/xxx` 提供。"""
    path = os.path.join(ROOT, "static", url.lstrip("/"))
    return path


def test_index_asset_version_is_uniform():
    """index.html 里所有 `?v=` 必须是同一个数字（漏改一个就是半新半旧）。"""
    pairs = _V_IN_HTML.findall(_html())
    assert pairs, "index.html 里没找到任何 `?v=` 资源版本戳"
    versions = {v for _, v in pairs}
    assert len(versions) == 1, f"index.html 里出现了不一致的 ?v=：{sorted(versions)}"


def test_css_v_matches_index_html():
    """app.js 的 CSS_V 必须与 index.html 的 `?v=` 相同。"""
    html_versions = {v for _, v in _V_IN_HTML.findall(_html())}
    m = _CSS_V.search(_app_js())
    assert m, "app.js 里没找到 CSS_V 常量（改名了？这里也要跟着改）"
    assert m.group(1) in html_versions, (
        f"app.js 的 CSS_V={m.group(1)} 与 index.html 的 ?v={sorted(html_versions)} 不一致；"
        "改 CSS 时这两个数字必须一起 +1"
    )


def test_lazy_css_files_exist():
    """app.js 里 loadCss 引用的文件必须真实存在（写错文件名会静默 404，样式整块丢失）。"""
    missing = []
    for rel in sorted(set(_CSS_LOAD.findall(_app_js()))):
        if not os.path.exists(_local(rel)):
            missing.append(rel)
    assert not missing, f"app.js 引用了不存在的 CSS：{missing}"


def test_index_referenced_assets_exist():
    """index.html 里 href/src 引用的本地资源必须真实存在。"""
    missing = []
    for rel in sorted(set(_CSS_SRC.findall(_html()))):
        if not os.path.exists(_local(rel)):
            missing.append(rel)
    assert not missing, f"index.html 引用了不存在的资源：{missing}"
