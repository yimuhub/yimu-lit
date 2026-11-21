"""v5.8.0 无限画布（创作工作流白板）回归。

覆盖：
1. 整板保存/读取：POST/GET 往返、updatedAt、空板降级、节点/边清洗（kind/color 归一、
   悬空边/自环/重复边过滤、字段截断、数量上限）、_global 全局白板、
   key 别名解析（改名后旧 key 读写同一块板）、路径安全（分隔符/.. → 400）；
2. 锁 + 原子写：并发整板保存不损坏、读侧始终拿到合法 JSON；
3. AI 扩展：mock 注入 llm.chat（scene=画布扩展/json_mode 透传、markdown 围栏解析、
   条目截断）、失败中文报错（502 + 可读 detail）、空文本 400、count 钳制 3–5；
4. 章卡导入：02d-章卡.jsonl 本地懒读（排序/坏行跳过/标题与摘要缺省/状态缺省）、
   无作品 404、空 key 400、无章卡文件返回空列表。
"""
import json
import os
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi import HTTPException

from bot import llm, paths, webflow, writer


# ---------------------------------------------------------------- fixtures

def make_project(name, cards=None, plan=True):
    pdir = os.path.join(writer.PROJECTS_DIR, name)
    os.makedirs(pdir, exist_ok=True)
    if plan:
        with open(os.path.join(pdir, "02-写作计划.json"), "w", encoding="utf-8") as f:
            json.dump({"novelName": "测试书·白板", "totalChapters": 6}, f, ensure_ascii=False)
    if cards is not None:
        lines = []
        for c in cards:
            lines.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
        with open(os.path.join(pdir, "02d-章卡.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
    return pdir


@pytest.fixture()
def api():
    """直挂 webflow.router 测契约（与生产 include_router 同路径；app.py 由主会话挂）。"""
    app = FastAPI()
    app.include_router(webflow.router)
    return TestClient(app)


@pytest.fixture()
def boards_clean(tmp_path, monkeypatch):
    """flow-boards 目录指到临时子目录，测试互不污染。"""
    monkeypatch.setattr(paths, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(paths, "data", lambda *parts: os.path.join(str(tmp_path), *parts))
    return os.path.join(str(tmp_path), "flow-boards")


BOARD = {
    "nodes": [
        {"id": "a1", "kind": "note", "x": 10, "y": -20, "w": 230, "h": 156,
         "title": "开场钩子", "text": "主角在雨夜捡到一枚不该存在的钥匙", "color": "accent"},
        {"id": "c1", "kind": "chapter", "x": 300, "y": 0, "n": 3,
         "title": "第 3 章 · 裂痕", "text": "监控黑屏与旧纽扣", "color": "plain"},
    ],
    "edges": [{"from": "a1", "to": "c1"}],
}


# ---------------------------------------------------------------- 1. 整板读写

def test_board_roundtrip(api, boards_clean):
    r = api.post("/api/flow/board", json={"key": "书A", **BOARD})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["boardKey"] == "书A"
    assert d["nodeCount"] == 2 and d["edgeCount"] == 1
    assert d["updatedAt"]

    g = api.get("/api/flow/board", params={"key": "书A"}).json()
    assert g["ok"] and g["boardKey"] == "书A"
    assert [n["id"] for n in g["nodes"]] == ["a1", "c1"]
    assert g["nodes"][0]["title"] == "开场钩子"
    assert g["nodes"][1]["n"] == 3
    assert g["edges"] == [{"from": "a1", "to": "c1"}]
    assert g["updatedAt"]


def test_board_missing_file_degrades(api, boards_clean):
    g = api.get("/api/flow/board", params={"key": "从未保存的书"}).json()
    assert g["ok"] and g["nodes"] == [] and g["edges"] == [] and g["updatedAt"] == ""


def test_board_sanitize_on_save(api, boards_clean):
    dirty = {
        "nodes": [
            {"id": "ok", "kind": "ai", "x": 1.25, "y": "2", "title": "t" * 200,
             "text": "x" * 5000, "color": "不存在的色"},
            {"kind": "note"},                                    # 无 id → 补 n2
            {"id": "bad", "kind": "alien", "w": 99999, "h": -5},  # kind 归一 + 尺寸钳制
            {"id": "ok", "kind": "note"},                         # 重复 id → 丢弃
        ],
        "edges": [
            {"from": "ok", "to": "n2"},
            {"from": "ok", "to": "ghost"},   # 悬空 → 丢
            {"from": "ok", "to": "ok"},      # 自环 → 丢
            {"from": "ok", "to": "n2"},      # 重复 → 丢
        ],
    }
    r = api.post("/api/flow/board", json={"key": "书清洗", **dirty})
    assert r.status_code == 200
    g = api.get("/api/flow/board", params={"key": "书清洗"}).json()
    nodes = {n["id"]: n for n in g["nodes"]}
    assert set(nodes) == {"ok", "n2", "bad"}
    assert nodes["ok"]["kind"] == "ai" and nodes["ok"]["color"] == "plain"
    assert len(nodes["ok"]["title"]) <= webflow.MAX_TITLE
    assert len(nodes["ok"]["text"]) <= webflow.MAX_TEXT
    assert nodes["ok"]["w"] <= 900 and nodes["bad"]["h"] >= 72
    assert nodes["bad"]["kind"] == "note"
    assert g["edges"] == [{"from": "ok", "to": "n2"}]


def test_board_sanitize_node_cap(api, boards_clean):
    nodes = [{"id": f"n{i}", "kind": "note"} for i in range(webflow.MAX_NODES + 50)]
    r = api.post("/api/flow/board", json={"key": "书上限", "nodes": nodes, "edges": []})
    assert r.status_code == 200
    assert r.json()["nodeCount"] == webflow.MAX_NODES


def test_global_board_uses_underscore_file(api, boards_clean):
    r = api.post("/api/flow/board", json={"key": "", **BOARD})
    assert r.status_code == 200 and r.json()["boardKey"] == "_global"
    assert os.path.exists(os.path.join(boards_clean, "_global.json"))
    g = api.get("/api/flow/board").json()
    assert g["ok"] and g["boardKey"] == "_global" and len(g["nodes"]) == 2
    # 全局板与书板互不串
    g2 = api.get("/api/flow/board", params={"key": "书A"}).json()
    assert g2["nodes"] == []


def test_board_key_alias_after_rename(api, boards_clean):
    new_key = "p_20990101-000000-白板新名"
    old_key = "p_20990101-000000-白板旧名"
    make_project(new_key)
    api.post("/api/flow/board", json={"key": new_key, **BOARD})
    writer.record_rename(old_key, new_key)
    writer._RENAME_MAP_CACHE["mtime"] = None  # 同秒内 mtime 缓存可能不失效，手动捅一下
    g = api.get("/api/flow/board", params={"key": old_key}).json()
    assert g["ok"] and g["boardKey"] == new_key and len(g["nodes"]) == 2


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "..", ".hidden", "a*b", "x" * 200])
def test_board_rejects_unsafe_keys(api, boards_clean, bad):
    for method, path, kwargs in (
        ("get", "/api/flow/board", {"params": {"key": bad}}),
        ("post", "/api/flow/board", {"json": {"key": bad, "nodes": [], "edges": []}}),
    ):
        r = getattr(api, method)(path, **kwargs)
        assert r.status_code == 400, f"{method} {bad} → {r.status_code}"
        assert "非法" in r.json()["detail"]


def test_board_post_rejects_non_list_payload(api, boards_clean):
    r = api.post("/api/flow/board", json={"key": "书防御", "nodes": "not-a-list",
                                          "edges": {"from": "a"}})
    assert r.status_code == 200
    g = api.get("/api/flow/board", params={"key": "书防御"}).json()
    assert g["nodes"] == [] and g["edges"] == []


# ---------------------------------------------------------------- 2. 锁 + 原子写

def test_board_concurrent_saves_never_corrupt(api, boards_clean):
    errs = []

    def w(i):
        try:
            nodes = webflow.sanitize_board({"nodes": [{"id": f"n{i}", "kind": "note",
                                                       "x": i, "y": 0}]})["nodes"]
            webflow.write_board("书并发", nodes, [])
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errs == []
    # 文件始终是合法 JSON 的完整板（last-writer-wins，但绝不半个文件）
    with open(os.path.join(boards_clean, "书并发.json"), encoding="utf-8") as f:
        obj = json.load(f)
    assert len(obj["nodes"]) == 1
    assert obj["nodes"][0]["id"].startswith("n")
    g = api.get("/api/flow/board", params={"key": "书并发"}).json()
    assert g["ok"] and len(g["nodes"]) == 1


# ---------------------------------------------------------------- 3. AI 扩展

def test_expand_via_llm_mock(api, monkeypatch):
    calls = {}

    def fake_chat(messages, **kwargs):
        calls["messages"] = messages
        calls.update(kwargs)
        return (
            "```json\n{\"items\":["
            "{\"title\":\"反转视角\",\"text\":\"从对手的眼睛看这件事\"},"
            "{\"title\":\"极端化\",\"text\":\"把冲突放大到极限\"},"
            "{\"title\":\"代价\",\"text\":\"主角要付出什么\"},"
            "{\"title\":\"多余的一条\",\"text\":\"会被截掉\"}"
            "]}\n```"
        )

    monkeypatch.setattr(llm, "chat", fake_chat)
    r = api.post("/api/flow/expand", json={"key": "书A", "text": "雨夜的钥匙",
                                           "kind": "note", "count": 3})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["count"] == 3
    assert [it["title"] for it in d["items"]] == ["反转视角", "极端化", "代价"]
    assert all(set(it) == {"title", "text"} for it in d["items"])
    assert calls.get("json_mode") is True
    assert calls.get("scene") == "画布扩展"
    assert calls.get("max_tokens") == 1500
    user = calls["messages"][1]["content"]
    assert "雨夜的钥匙" in user and "3" in user


def test_expand_clamps_count(api, monkeypatch):
    seen = {}

    def fake_chat(messages, **kwargs):
        seen["user"] = messages[1]["content"]
        return json.dumps({"items": [{"title": f"t{i}", "text": "b"} for i in range(8)]},
                          ensure_ascii=False)

    monkeypatch.setattr(llm, "chat", fake_chat)
    d = api.post("/api/flow/expand", json={"text": "x", "count": 99}).json()
    assert len(d["items"]) == webflow.EXPAND_COUNT_MAX
    # 0/缺省 = 默认 4；越界负数钳到下限
    d_def = api.post("/api/flow/expand", json={"text": "x"}).json()
    assert len(d_def["items"]) == webflow.EXPAND_COUNT_DEFAULT
    d2 = api.post("/api/flow/expand", json={"text": "x", "count": -5}).json()
    assert len(d2["items"]) == webflow.EXPAND_COUNT_MIN


def test_expand_llm_failure_chinese(api, monkeypatch):
    def boom(messages, **kwargs):
        raise llm.LLMError("余额不足，请充值后重试")

    monkeypatch.setattr(llm, "chat", boom)
    r = api.post("/api/flow/expand", json={"text": "想法"})
    assert r.status_code == 502
    assert "AI 扩展失败" in r.json()["detail"] and "余额不足" in r.json()["detail"]


def test_expand_empty_result_chinese(api, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda messages, **kwargs: "模型咕咕了，没有 JSON")
    r = api.post("/api/flow/expand", json={"text": "想法"})
    assert r.status_code == 502
    assert "没有返回可用的扩展内容" in r.json()["detail"]


def test_expand_requires_text(api, boards_clean):
    r = api.post("/api/flow/expand", json={"text": "   "})
    assert r.status_code == 400
    assert "先在便签里写点想法" in r.json()["detail"]


def test_expand_kind_alias_and_text_cap(api, monkeypatch):
    seen = {}

    def fake_chat(messages, **kwargs):
        seen["user"] = messages[1]["content"]
        return json.dumps({"items": [{"title": "t", "text": "b"}]}, ensure_ascii=False)

    monkeypatch.setattr(llm, "chat", fake_chat)
    r = api.post("/api/flow/expand",
                 json={"text": "字" * 5000, "kind": "alien"})
    assert r.status_code == 200
    assert len(seen["user"]) < 5000   # 节点内容被截到 MAX_EXPAND_TEXT 内


def test_parse_items_tolerates_loose_json():
    assert webflow._parse_items({"items": [{"title": "a", "text": "b"}]})[0]["title"] == "a"
    out = webflow._parse_items('前置废话 {"items":[{"title":"标题","body":"正文"}]} 后置废话')
    assert out and out[0]["text"] == "正文"
    assert webflow._parse_items("完全不是 JSON") == []
    assert webflow._parse_items({"items": [{"title": "", "text": ""}, "垃圾"]}) == []


# ---------------------------------------------------------------- 4. 章卡导入

CARDS = [
    {"n": 2, "title": "回溯", "status": "writing",
     "goal": "陈默带着地下室发现的异常返回警局，试图寻找失踪案规律，却发现所有案件时间点都对应童年的特定节拍。"},
    {"n": 1, "title": "裂痕", "status": "done",
     "goal": "雨夜，刑警陈默正在调查蓝水公寓的连环失踪案，现场监控出现可疑黑屏。"},
    {"n": 3, "title": "", "status": "",
     "conflict": "小刘对陈默产生怀疑，周队启动心理评估。"},
    "{ 这一行是坏 JSON",                    # 坏行 → 跳过
    {"no_title": True},                     # 无 n → 跳过
]


def test_chapters_import_reads_cards(api, boards_clean):
    key = "p_20990101-000000-白板章卡"
    make_project(key, cards=CARDS)
    r = api.get("/api/flow/chapters", params={"key": key})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and [c["n"] for c in d["chapters"]] == [1, 2, 3]
    c1, c2, c3 = d["chapters"]
    assert c1["title"] == "裂痕" and c1["status"] == "done"
    assert c1["summary"].startswith("雨夜，刑警陈默")
    assert c2["status"] == "writing"
    assert c3["title"] == "第3章" and c3["status"] == "planned"
    assert c3["summary"].startswith("小刘对陈默产生怀疑")


def test_chapters_missing_project(api, boards_clean):
    assert api.get("/api/flow/chapters", params={"key": "p_不存在"}).status_code == 404
    assert api.get("/api/flow/chapters").status_code == 400


def test_chapters_no_card_file_returns_empty(api, boards_clean):
    key = "p_20990101-000000-白板无章卡"
    make_project(key)
    d = api.get("/api/flow/chapters", params={"key": key}).json()
    assert d["ok"] and d["chapters"] == []


def test_load_chapters_summary_cap_and_blank_lines():
    pdir = make_project("p_20990101-000000-白板摘要",
                        cards=[{"n": 1, "title": "t", "goal": "长" * 300},
                               "", "   "])
    cards = webflow.load_chapters(pdir)
    assert len(cards) == 1
    assert len(cards[0]["summary"]) == webflow.MAX_SUMMARY


# ---------------------------------------------------------------- 5. 纯函数口径

def test_sanitize_board_direct_on_garbage():
    b = webflow.sanitize_board(None)
    assert b == {"nodes": [], "edges": []}
    b2 = webflow.sanitize_board({"nodes": [{"id": "a", "n": "5"}],
                                 "edges": [{"from": "a", "to": "a"}]})
    assert b2["nodes"][0]["n"] == 5
    assert b2["edges"] == []


def test_write_board_writes_atomic_payload(boards_clean):
    board = webflow.sanitize_board(BOARD)
    saved = webflow.write_board("书直接写", board["nodes"], board["edges"])
    assert saved["version"] == 1 and saved["key"] == "书直接写"
    with open(webflow.board_path("书直接写"), encoding="utf-8") as f:
        obj = json.load(f)
    assert obj["updated_at"] == saved["updated_at"]
