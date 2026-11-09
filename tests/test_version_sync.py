"""版本口径守卫：根目录 VERSION 是唯一事实来源。

历史上 `config.example.json` / `package.json` 的 version 一直停在 1.4.0，
而 VERSION 已到 4.32.x；这两个字段代码不读、界面不显示，纯元数据，
但「现在到底是哪一版」变成要翻三个文件才能回答的问题。
`tools/bump_version.py` 现在负责同步它们，本文件防止有人绕过工具手改。

CI 里没有 `config.json`（被 .gitignore 忽略、每台机器自管），
所以对它的检查是「存在才查」。
"""
import importlib.util
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _version():
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
        return f.read().strip().lstrip("v")


def _json_version(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.load(f)["version"]


def test_meta_versions_track_VERSION():
    ver = _version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", ver), f"VERSION 格式不对：{ver}"
    for name in ("config.example.json", "package.json"):
        assert _json_version(name) == ver, (
            f"{name} 的 version 与 VERSION({ver}) 不一致——"
            "跑 python tools/bump_version.py --meta-only 同步")


def test_local_config_version_if_present():
    """本地 config.json 是运行态文件，不入库。

    CI 里新 clone 时没有它，但**跑过前面的用例后会被重建**，而且重建出来的
    配置往往不带 version 键（第一版守卫只判断文件是否存在，结果在 CI 里
    KeyError 把 3.13/3.14 双矩阵都点红了）。所以只有它确实是完整配置时
    才要求同步，其余情况一律跳过——仓库里的两个被跟踪文件才是硬门禁。
    """
    p = os.path.join(ROOT, "config.json")
    if not os.path.exists(p):
        pytest.skip("新 clone 没有本地 config.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        pytest.skip("本地 config.json 不是完整 JSON（运行态文件）")
    if "version" not in data:
        pytest.skip("本地 config.json 是运行态重建的，没有 version 字段")
    assert data["version"] == _version(), (
        "本地 config.json 的 version 落后——跑 python tools/bump_version.py --meta-only")


def test_frontend_cache_stamps_match():
    """index.html 与 app.js 的缓存戳必须一致且各自唯一。"""
    with open(os.path.join(ROOT, "static", "v2", "index.html"), encoding="utf-8") as f:
        html_stamps = set(re.findall(r"\?v=(\d+)", f.read()))
    with open(os.path.join(ROOT, "static", "v2", "js", "app.js"), encoding="utf-8") as f:
        js_stamps = set(re.findall(r"const CSS_V = '(\d+)'", f.read()))
    assert len(html_stamps) == 1 and len(js_stamps) == 1, "缓存戳出现多个不同值"
    assert html_stamps == js_stamps, "两处缓存戳不一致，用户会拿到旧 JS"


def test_sync_meta_versions_repairs_stale_file(tmp_path, monkeypatch):
    """拿一份落后的元数据文件，验证工具能补齐、不误伤嵌套键、可重复执行。"""
    spec = importlib.util.spec_from_file_location(
        "bump_version_under_test", os.path.join(ROOT, "tools", "bump_version.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    f = tmp_path / "package.json"
    f.write_text('{\n  "name": "x",\n  "version": "1.4.0",\n'
                 '  "dependencies": {"a": {"version": "9.9.9"}}\n}\n', encoding="utf-8")
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    monkeypatch.setattr(mod, "META_FILES", ("package.json",))

    assert mod.sync_meta_versions("4.32.1")["package.json"] is True
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["version"] == "4.32.1"
    assert data["dependencies"]["a"]["version"] == "9.9.9", "不能误改嵌套 version 键"
    assert mod.sync_meta_versions("4.32.1")["package.json"] is False, "重复执行应幂等"
