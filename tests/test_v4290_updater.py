"""v4.29.0：桌面自动更新（overlay 模型）回归。

覆盖：版本比较、zipball 白名单解压+完整性校验+依赖预检、
fetch_latest 的 has_update 判定、desktop_update 的「已最新/更新成功」分支
（网络 mock），以及 overlay 引导后 paths 的资源根切换。
"""
import os
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from bot import updater


def _real_requirements() -> str:
    """构造更新包时写仓库真实的 requirements.txt。

    v4.32.1：这里以前写死「fastapi + pyyaml 两行」，但生产 requirements.txt
    里根本没有 PyYAML，全仓也没有任何 import yaml。CI 只安装
    requirements.txt + requirements-dev.txt，于是 _check_new_dependencies
    的 find_spec("yaml") 必然为 None，解压校验与完整更新流程两个用例
    从 v4.29.0 起一直误报「需要新依赖 yaml」，而本地 .venv 恰好旁带装了
    PyYAML 所以全绿——典型的「测试环境与 CI 环境不一致」。
    改用真实清单：预检要检查的包与 CI 实际安装的包天然一致；
    with_bad_dep 分支照旧验证「新依赖被拦截」的能力。
    """
    with open(os.path.join(REPO_ROOT, "requirements.txt"), encoding="utf-8") as f:
        return f.read().rstrip("\n") + "\n"


def _make_zip(tmp_path, version="9.9.9", with_bad_dep=False):
    root = tmp_path / f"yimu-lit-{version}"
    (root / "bot").mkdir(parents=True)
    (root / "static").mkdir()
    (root / "skillkit").mkdir()
    (root / "bot" / "__init__.py").write_text("", encoding="utf-8")
    (root / "bot" / "paths.py").write_text("DATA_ROOT = RESOURCE_ROOT = '.'", encoding="utf-8")
    (root / "VERSION").write_text(version, encoding="utf-8")
    (root / "config.example.json").write_text("{}", encoding="utf-8")
    (root / "static" / "index.html").write_text("x", encoding="utf-8")
    (root / "requirements.txt").write_text(
        _real_requirements() + ("some-new-package\n" if with_bad_dep else ""), encoding="utf-8")
    zp = tmp_path / "update.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for f in root.rglob("*"):
            zf.write(f, f.relative_to(tmp_path))
    return zp


def test_ver_tuple():
    assert updater._ver_tuple("4.28.10") > updater._ver_tuple("4.28.9")
    assert updater._ver_tuple("v4.29.0") == (4, 29, 0)


def test_extract_whitelist_and_validate(tmp_path):
    zp = _make_zip(tmp_path)
    st = tmp_path / "staging"
    st.mkdir()
    n = updater._extract_whitelist(str(zp), str(st))
    assert n == 6   # bot/static/skillkit/VERSION/config.example.json/requirements.txt
    updater._validate_staging(str(st))
    # 用户数据字段绝不进 overlay 白名单
    assert not (st / "projects").exists()


def test_zip_requirements_tracks_repo(tmp_path):
    """构造的更新包依赖清单必须跟着仓库走，别再手捏一份假清单。"""
    zp = _make_zip(tmp_path)
    st = tmp_path / "staging"
    st.mkdir()
    updater._extract_whitelist(str(zp), str(st))
    assert (st / "requirements.txt").read_text(encoding="utf-8") == _real_requirements()


def test_dependency_preflight_blocks_new_dep(tmp_path):
    zp = _make_zip(tmp_path, with_bad_dep=True)
    st = tmp_path / "staging"
    st.mkdir()
    updater._extract_whitelist(str(zp), str(st))
    try:
        updater._validate_staging(str(st))
        assert False, "应拦截新依赖"
    except RuntimeError as e:
        assert "新依赖" in str(e)


def test_desktop_update_already_latest(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "fetch_latest", lambda: {
        "ok": True, "local": "4.28.2", "remote": "4.28.2", "has_update": False})
    r = updater.desktop_update()
    assert r["ok"] and not r["updated"]


def test_desktop_update_full_flow(monkeypatch, tmp_path):
    """mock 下载：真实解压/校验/换名逻辑，overlay 就位。"""
    zp = _make_zip(tmp_path, version="9.9.9")
    monkeypatch.setattr(updater, "fetch_latest", lambda: {
        "ok": True, "local": "4.28.2", "remote": "9.9.9", "has_update": True,
        "zipball": "file://" + str(zp)})
    monkeypatch.setattr(updater, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(updater, "STAGING_DIR", str(tmp_path / "staging"))
    monkeypatch.setattr(updater, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(updater, "_download", lambda url, dest: open(dest, "wb").write(open(zp, "rb").read()))
    r = updater.desktop_update()
    assert r["updated"] is True and r["need_restart"] is True
    assert os.path.exists(os.path.join(str(tmp_path / "runtime"), "VERSION"))
    with open(os.path.join(str(tmp_path / "runtime"), "VERSION")) as f:
        assert f.read().strip() == "9.9.9"
    # 再跑一次：runtime VERSION 已是 9.9.9 → 已最新
    monkeypatch.setattr(updater, "current_version", lambda: "9.9.9")
    r2 = updater.desktop_update()
    assert r2["ok"] and not r2["updated"]


def test_debug_preflight(tmp_path):
    import re, importlib.util
    zp = _make_zip(tmp_path, with_bad_dep=True)
    st = tmp_path / "staging"
    st.mkdir()
    updater._extract_whitelist(str(zp), str(st))
    req = st / "requirements.txt"
    print("req exists:", req.exists())
    for line in open(req, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        top = re.split(r"[\[<>=!~;\s]", line)[0].strip().lower().replace("-", "_")
        spec = importlib.util.find_spec(top)
        print("PKG", top, "->", "MISSING" if spec is None else "FOUND")
    assert req.exists()
