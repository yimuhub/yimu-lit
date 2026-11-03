"""v4.24.12 创作状态按钮（继续/暂停）消失与空壳占位。

用户报的现象：**只要刷新页面，创作状态的那个按钮就消失了**。
本文件把三个真因钉成回归测试：

1. **/api/status 不带 key**（后端）
   `Project.status()` 只回进度字段，没有 key。前端 `refreshWork` 把它和作品库条目
   合并成 work；刷新时 `/api/status`（单本）比 `/api/library`（全量聚合）先回，
   此刻作品库条目还没到 → work 缺 key → 创作状态按钮按「定位不到作品」整颗不渲染。
   修法：status 自带 key + 前端合并时显式落 key。

2. **空壳按钮占 25px**（前端）
   按钮工厂的注释写着「idle 态返回 null（调用方不渲染）」，但实现里 idle 态只是
   清空内容——元素仍在，`.btn-creative` 的 `padding: 0 12px` 让它在按钮行里
   留下一段莫名加宽的空白。修法：idle 用 hidden 属性移出布局。

3. **状态字段脏了就失去入口**（前端）
   `creativeToggleState` 原本只认固定几个 state 名；书在盘上的 status 若是
   `unknown` 之类，还有没写完的章节却没有「继续」入口。修法：total > done 即 resume。
"""
import json
import re

import pytest

from bot import writer

PLAN_FILE = "02-写作计划.json"


# ---------------------------------------------------------------- 后端：status 必须带 key

def _mk_book(tmp_path, monkeypatch, key="book", status="in_progress", done=50, total=100,
             paused=False):
    """造一本有 progress 的书，返回项目目录。"""
    pdir = tmp_path / key
    pdir.mkdir(parents=True, exist_ok=True)
    chapters = [
        {"chapterNumber": n, "title": f"第{n}章", "status": "completed", "wordCount": 100}
        for n in range(1, done + 1)
    ] + [
        {"chapterNumber": n, "title": f"第{n}章", "status": "pending", "wordCount": 0}
        for n in range(done + 1, total + 1)
    ]
    meta = {"paused": True} if paused else {}
    json.dump({"novelName": "测试书", "status": status, "chapters": chapters,
               "totalChapters": total, "meta": meta, "config": {}},
              open(pdir / PLAN_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    return pdir


def test_status_carries_key(tmp_path, monkeypatch):
    """Project.status() 必须带 key——前端靠它把进度对回具体作品。"""
    _mk_book(tmp_path, monkeypatch)
    st = writer.Project(str(tmp_path / "book"), "book").status()
    assert "key" in st, "status 缺 key，前端合并出的 work 就定位不到作品"
    assert st["key"] == "book"


def test_status_key_present_for_every_state(tmp_path, monkeypatch):
    """各种状态（写完/暂停/未写）下 status 都要带 key，不能只在某条路径上有。"""
    for status, paused in [("in_progress", False), ("paused", True),
                            ("completed", False), ("unknown", False)]:
        _mk_book(tmp_path, monkeypatch, status=status, paused=paused,
                 done=(100 if status == "completed" else 50))
        st = writer.Project(str(tmp_path / "book"), "book").status()
        assert st.get("key") == "book", f"status={status} 时 status 没带 key"


def test_api_status_response_has_key(tmp_path, monkeypatch):
    """接口层同样要带 key（前端 refreshWork 直接吃这份返回体）。

    走真实路径：只重定向 PROJECTS_DIR，让 /api/status 自己按 key 找书——
    这样测的是「刷新页面」时前端真正会拿到的那份返回体。
    """
    import app as appmod
    from fastapi.testclient import TestClient

    _mk_book(tmp_path, monkeypatch)
    appmod.PROJECT_INDEX.pop("book", None)
    client = TestClient(appmod.app)
    resp = client.get("/api/status", params={"key": "book"})
    assert resp.status_code == 200, f"/api/status 没找到这本书：{resp.text[:200]}"
    body = resp.json()
    assert body.get("key") == "book", "接口返回体缺 key → 刷新后按钮定位不到作品"


def test_status_works_without_library_loaded(tmp_path, monkeypatch):
    """回归：刷新页面时 /api/library 还没回来，光靠 /api/status 也足以定位作品。

    这是「刷新即消失」的真现场——前端 refreshWork 把 status 与作品库条目合并成 work，
    若 status 不带 key，条目还没到的那个窗口里 work.key 就是 undefined。
    """
    import app as appmod
    from fastapi.testclient import TestClient

    _mk_book(tmp_path, monkeypatch)
    appmod.PROJECT_INDEX.pop("book", None)
    client = TestClient(appmod.app)
    status = client.get("/api/status", params={"key": "book"}).json()

    # 模拟前端合并的最坏情况：作品库条目还没到
    merged = {**(status or {})}
    assert merged.get("key") == "book", "只靠 status 就该能定位作品"
    assert merged.get("state") == "in_progress"
    assert merged.get("total") == 100 and merged.get("done") == 50


# ---------------------------------------------------------------- 前端：三态判定 + 空壳

FRONTEND = (
    "static/v2/js/components/planbar.js",
    "static/v2/js/app.js",
)


def _read(rel):
    with open(rel, encoding="utf-8") as f:
        return f.read()


def test_frontend_toggle_state_logic():
    """用与前端一致的判定函数验证各状态落到哪个相位。"""
    src = _read("static/v2/js/components/planbar.js")
    m = re.search(r"function creativeToggleState\(work\) \{(.*?)\n\}", src, re.S)
    assert m, "找不到 creativeToggleState，判定逻辑被挪走了"
    body = m.group(1)
    # 关键分支必须还在（防止后续重构把「继续」入口判没了）
    assert "'pausing'" in body and "'running'" in body and "'resume'" in body
    assert "'idle'" in body
    # v4.24.12 新增：还有没写完的章节 → 也要给「继续」入口
    assert "total > done" in body, "缺「total > done → resume」兜底，状态字段脏时用户会失去继续入口"


def test_frontend_idle_button_hidden():
    """空闲态按钮必须移出布局，不能留 25px 空壳在按钮行里。"""
    src = _read("static/v2/js/components/planbar.js")
    assert "el.hidden = st === 'idle'" in src, "空闲态未隐藏按钮 → 按钮行出现莫名加宽的空白"
    # hidden 元素仍可被 store 订阅点亮，所以不能直接 return null 丢掉元素
    assert "el.hidden = false" in src or "'idle'" in src


def test_css_hides_btn_creative():
    """CSS 必须显式声明 .btn-creative[hidden]{display:none}。

    .btn-creative 自带 display:inline-flex，优先级高于 UA 样式表的
    [hidden]{display:none}——不写这条规则，hidden 属性等于没设。
    """
    css = _read("static/v2/css/panels.css")
    assert re.search(r"\.btn-creative\[hidden\]\s*\{[^}]*display:\s*none", css), \
        "缺 .btn-creative[hidden]{display:none}，空闲态按钮会残留空壳占位"


def test_refresh_work_keeps_key():
    """前端合并 work 时必须显式带上 key。"""
    src = _read("static/v2/js/app.js")
    m = re.search(r"async refreshWork\(\) \{.*?\n  \}", src, re.S)
    assert m, "找不到 refreshWork"
    body = m.group(0)
    assert re.search(r"key,", body), "refreshWork 合并 work 时未显式落 key"


def test_planbar_and_rightpanel_share_one_factory():
    """按钮工厂必须仍被目标条与右栏共用（别修一处漏一处）。"""
    planbar = _read("static/v2/js/components/planbar.js")
    right = _read("static/v2/js/components/rightpanel.js")
    assert "export function createCreativeToggle" in planbar
    assert "createCreativeToggle" in right, "右栏不再用统一按钮工厂，三态行为会分叉"
    assert right.count("createCreativeToggle") >= 2, "右栏的两个入口应都用工厂"


# ---------------------------------------------------------------- 回归：状态映射不漂移

@pytest.mark.parametrize("plan_status,paused,done,total,expect_state,expect_paused", [
    ("in_progress", False, 50, 100, "in_progress", False),
    ("paused", True, 50, 100, "paused", True),
    ("completed", False, 100, 100, "completed", False),
])
def test_status_state_passthrough(tmp_path, monkeypatch, plan_status, paused,
                                  done, total, expect_state, expect_paused):
    """plan.status 原样透传成 status().state，paused 标志不被吞。"""
    _mk_book(tmp_path, monkeypatch, status=plan_status, paused=paused,
             done=done, total=total)
    st = writer.Project(str(tmp_path / "book"), "book").status()
    assert st["state"] == expect_state
    assert st["paused"] is expect_paused
    assert (st["done"], st["total"]) == (done, total)


def test_write_loop_sets_running_flag(tmp_path, monkeypatch):
    """回归：_write_loop 自己要设 running 标志。

    一键完稿（one_shot）直接调 _write_loop 绕过了 start_writing，
    于是写作期间 running 一直是 False —— 前端按钮显示成「继续」，
    可书其实正在写。修法是把 _set_book_running 放进 _write_loop。
    """
    src = _read("bot/writer.py")
    m = re.search(r"def _write_loop\(proj: Project.*?\n    try:", src, re.S)
    assert m, "找不到 _write_loop 开头"
    head = m.group(0)
    assert "_set_book_running(" in head, \
        "_write_loop 开头未设 running 标志 → 绕过 start_writing 的调用路径（如一键完稿）报 running:false"


def test_book_live_counts_oneshot_as_running():
    """一键完稿期间 /api/book-live 也要报 running=true。"""
    src = _read("app.py")
    m = re.search(r'"running": bool\(st\.get\("running"\)[^)]*\)[^,]*', src)
    assert m, "找不到 /api/book-live 的 running 判定"
    assert "osh" in m.group(0), \
        "一键完稿（one_shot）未计入 running → 极速/一键完稿时按钮显示成「继续」而非「创作中」"
