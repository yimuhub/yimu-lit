"""接线回归：前端调的 API 必须存在，前端请求的路径后端必须注册。

## 为什么要有这组测试
v4.21.1 清「死 API」删掉了 `api.onboarding`，唯一调用方 `views/wizard.js`
是 `await import()` 懒加载的，扫描扫不到 → 新建作品弹窗当场 TypeError、正文全空。
`api.scriptPreview` 更早就是这样：mediumbench 一直调，api.js 从来没定义，
剧本「整本预览」点了就炸。两个 bug 都活了好几个版本，因为：
  · eslint no-undef 不报——`api.onboarding` 是合法属性访问；
  · pytest 只测后端，没人测前端模块和 api 对象的对应关系；
  · 手工点不到那么全。

这里把这条关系固化成断言：只要有人再删 api 方法、或改了后端路由名而忘了前端，
测试直接红。
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _audit():
    path = os.path.join(ROOT, "tools", "audit_wiring.py")
    spec = importlib.util.spec_from_file_location("audit_wiring", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit():
    return _audit()


def test_no_dangling_api_calls(audit):
    """前端 `api.xxx()` 的每个 xxx 都必须在 api.js 里真实存在。"""
    keys = audit.api_keys()
    assert keys, "没能读到 api.js 的方法表（node 不可用时会退回正则，也不该为空）"

    bad = []
    for rel, no, head, sub, snippet in audit.api_usages():
        if head not in keys:
            bad.append(f"{rel}:{no} api.{head}  ← {snippet}")
        elif sub and keys[head] and sub not in keys[head]:
            bad.append(f"{rel}:{no} api.{head}.{sub}  ← {snippet}")
    assert not bad, "调用了 api.js 里不存在的方法（运行时会 TypeError）：\n" + "\n".join(bad)


def test_every_frontend_api_path_has_route(audit):
    """前端出现的每个 /api 字面量，后端都要有对应路由（否则 404）。"""
    routes = audit.backend_routes()
    route_paths = sorted({p for _, p in routes})
    fe = audit.frontend_paths()

    missing = []
    for p, where in sorted(fe.items()):
        if any(audit.path_matches(p, rp) for rp in route_paths):
            continue
        missing.append(f"{p}  ← {where[0]}")
    assert not missing, "前端请求了后端不存在的路径：\n" + "\n".join(missing)


def test_wizard_style_sheet_is_around(audit):
    """向导的样式在 settings.css 里，而它是按路由加载的。

    这条守的是「把 CSS 当成死代码删掉 / 忘了在打开弹窗前加载」这一类回退：
    只要 `.wiz-body` 与 `.genre-card` 还在 settings.css，就算合格。
    """
    css = open(os.path.join(ROOT, "static", "v2", "css", "settings.css"), encoding="utf-8").read()
    for cls in (".wiz-body", ".wiz-steps", ".genre-card"):
        assert cls in css, f"settings.css 里没有 {cls}，向导会裸奔"


def test_wizard_loads_css_before_render():
    """从作品库点「新建作品」时也要先补 settings.css，否则弹窗没样式。"""
    app_js = open(os.path.join(ROOT, "static", "v2", "js", "app.js"), encoding="utf-8").read()
    i = app_js.find("async openWizard")
    assert i > 0, "app.js 里找不到 ctx.openWizard"
    seg = app_js[i:i + 900]
    assert "settings.css" in seg and "loadCss" in seg, (
        "openWizard 没有加载 settings.css：从非设置页打开向导会没有样式"
    )


def test_onboarding_api_wired():
    """专项：/api/onboarding 是向导词表的唯一来源，客户端方法不能再次被删。"""
    api_js = open(os.path.join(ROOT, "static", "v2", "js", "core", "api.js"), encoding="utf-8").read()
    assert "onboarding:" in api_js, "api.js 里没有 onboarding，新建作品弹窗会直接 TypeError"
    assert "/api/onboarding" in api_js
    sys.path.insert(0, ROOT)
    os.environ.setdefault("SHENBI_AUTH", "0")
    import app as appmod

    assert any(getattr(r, "path", "") == "/api/onboarding" for r in appmod.app.routes)
