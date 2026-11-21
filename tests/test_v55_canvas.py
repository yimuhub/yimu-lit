"""v5.5.2 资料画布回归（docs/易创对齐清单 第二节 A2）。

覆盖：
1. 人物提取：`## 人物：X` 标题（粗体/括号别名/去重）；graph.json 碎片人名的兜底滤网；
2. 关系边提取：关系/立场 行 → 直连边 + 角色词连主角；标签关键词优先级（亲人→暧昧→师徒→
   上下级→对手→利用→盟友→关联）；
3. 线索账本解析：登记表行（首章/兑现章/状态）、`- 线索：` 支线条目、「登记范围」主线短语；
4. 故事线树：主线/人物线/支线/伏笔四组 + 伏笔前后置 seq；无账本时 15-故事图谱.json
   thread 节点兜底；空目录降级不 crash；
5. 边覆盖层：data/canvas-edges/<key>.json 读写（锁 + 原子写）、label 截断、空 label 删除、
   自环 400、并发写不丢条目；聚合时覆盖标签合并 + 手拉边补进；
6. HTTP 契约：GET /api/canvas/graph（key 别名解析 → resolvedKey）、POST /api/canvas/edge、
   404/400 语义。
"""
import json
import os
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi import HTTPException

from bot import paths, webcanvas, writer


# ---------------------------------------------------------------- fixtures

ARCHIVE = """# 测试书 人物档案

## 人物：陈默
- **姓名/称谓**：全名陈默。
- **关系/立场**：主角。立场：利用规则反噬，表面顺从，实际操控。

## 人物：林哲
- **姓名/称谓**：全名林哲；被称为"天枢之神"。
- **关系/立场**：主要反派。立场：阻。视主角为眼中钉，试图抹杀。

## 人物：苏晚
- **姓名/称谓**：全名苏晚；陈默叫她"晚晚"。
- **关系/立场**：配角（女主/情感线）。立场：帮。提供陈默缺失的人性锚点。

## 人物：老张（张启铭）
- **姓名/称谓**：全名张启铭；主角叫他"张叔"。
- **关系/立场**：关键配角。立场：帮。关键时刻协助陈默。
"""

LEDGER = """# 线索账本

> **登记范围**：仅登记服务于"底层逆袭-垄断崩塌"主线的核心悬念/伏笔。

## 登记表

| ID | 线索 | 首次出现(章) | 初始表象 | 真实含义 | 可推理证据 | 误导方式 | 计划兑现(章) | 状态 |
|---|---|---|---|---|---|---|---|---|
| L1 | 旧型神经贴片的金色光泽 | 第1章 | 表象一 | 真相一 | 证据一 | 误导一 | 第3章 | planned |
| L2 | 清道夫通讯链路的瞬时杂音 | 第2章 | 表象二 | 真相二 | 证据二 | 误导二 | 第4章 | planned |

_第02章后新增/强化线索（登记，控制数量）：_
- 线索：神笔（已知：休眠接口，真相：能改写核心逻辑）
- 线索：剪枝协议（已知：行动代号，真相：清除指令）→ 需计划兑现
"""


def make_project(name, *, archive=ARCHIVE, ledger=LEDGER, plan=True, graph=None):
    root = writer.PROJECTS_DIR
    pdir = os.path.join(root, name)
    os.makedirs(pdir, exist_ok=True)
    if archive is not None:
        with open(os.path.join(pdir, "00-人物档案.md"), "w", encoding="utf-8") as f:
            f.write(archive)
    if ledger is not None:
        with open(os.path.join(pdir, "10-线索账本.md"), "w", encoding="utf-8") as f:
            f.write(ledger)
    if plan:
        with open(os.path.join(pdir, "02-写作计划.json"), "w", encoding="utf-8") as f:
            json.dump({"novelName": "测试书·画布", "totalChapters": 5}, f, ensure_ascii=False)
    if graph is not None:
        with open(os.path.join(pdir, "15-故事图谱.json"), "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False)
    return pdir


@pytest.fixture()
def api():
    """直挂 webcanvas.router 测契约（与生产 include_router 同路径；app.py 由主会话挂）。"""
    app = FastAPI()
    app.include_router(webcanvas.router)
    return TestClient(app)


@pytest.fixture()
def overlay_clean(tmp_path, monkeypatch):
    """覆盖层目录指到临时子目录，测试互不污染。"""
    d = str(tmp_path / "canvas-edges")
    monkeypatch.setattr(paths, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(paths, "data", lambda *parts: os.path.join(str(tmp_path), *parts))
    return d


# ---------------------------------------------------------------- 1. 人物提取

def test_parse_persons_headings_bold_and_alias():
    text = (
        "# 档案\n\n"
        "## 人物：**陈默**\n正文\n\n"
        "## 人物：老张（张启铭）\n正文\n\n"
        "## 人物：陈默\n重复\n\n"
        "## 人物：配角\n占位\n"
    )
    names = [p["name"] for p in webcanvas.parse_persons(text)]
    assert names == ["陈默", "老张"]


def test_person_weight_counts_mentions_and_floor():
    texts = ["陈默出场 陈默再次出场", "别人提陈默"]
    assert webcanvas.person_weight("陈默", texts) == 3
    assert webcanvas.person_weight("查无此人", ["完全无关"]) == 1
    assert webcanvas.person_weight("图谱人物", [], graph_weight=7) == 7


# ---------------------------------------------------------------- 2. 关系边

def test_relation_label_priority():
    assert webcanvas.relation_label("主要反派。立场：阻。视主角为眼中钉") == "对手"
    assert webcanvas.relation_label("配角（女主/情感线）立场：帮") == "暧昧"
    assert webcanvas.relation_label("导师/阻碍者") == "师徒"
    assert webcanvas.relation_label("下属叫 ta 雅姐") == "上下级"
    assert webcanvas.relation_label("同盟伙伴，关键时刻帮忙") == "盟友"
    assert webcanvas.relation_label("立场：利用（利用主角破局）") == "利用"
    assert webcanvas.relation_label("立场：摇摆。") == "关联"
    # 对手关键词优先于「利用」
    assert webcanvas.relation_label("反派；立场：阻，兼利用") == "对手"


def test_relation_edges_role_words_link_to_protagonist():
    persons = [{"name": n, "section": ""} for n in ("陈默", "林哲", "苏晚", "老张")]
    edges = webcanvas.relation_edges(persons, ARCHIVE)
    pair = {tuple(sorted((e["from"], e["to"]))): e["label"] for e in edges}
    assert pair[tuple(sorted(("陈默", "林哲")))] == "对手"
    assert pair[tuple(sorted(("陈默", "苏晚")))] == "暧昧"
    assert pair[tuple(sorted(("陈默", "老张")))] == "盟友"
    # 无向：不出现自环与重复
    assert all(e["from"] != e["to"] for e in edges)


def test_relation_edges_direct_mention():
    persons = [{"name": n, "section": ""} for n in ("陈默", "老鬼")]
    archive = ARCHIVE + "\n\n## 人物：老鬼\n- **关系/立场**：想利用陈默的破坏力作为掩护，兼帮忙转移资产。\n"
    edges = webcanvas.relation_edges(persons, archive)
    assert any({e["from"], e["to"]} == {"陈默", "老鬼"} for e in edges)


# ---------------------------------------------------------------- 3. 线索账本

def test_parse_ledger_table_bullets_and_scope():
    d = webcanvas.parse_ledger(LEDGER)
    assert d["main"] == "底层逆袭-垄断崩塌"
    assert [t["id"] for t in d["table"]] == ["L1", "L2"]
    assert d["table"][0]["first_ch"] == 1 and d["table"][0]["due_ch"] == 3
    assert d["table"][0]["status"] == "planned"
    assert [b["text"] for b in d["bullets"]] == ["神笔", "剪枝协议"]


def test_parse_ledger_empty_and_malformed():
    assert webcanvas.parse_ledger("")["table"] == []
    d = webcanvas.parse_ledger("| a |\n| --- |\n| --- |\n| X | 线索名 | 第2章 | st |")
    assert d["table"][0]["id"] == "X"


# ---------------------------------------------------------------- 4. 故事线树

def test_build_story_groups_and_seq(tmp_path):
    pdir = make_project("p_20990101-000000-画布故事线")
    persons = webcanvas.parse_persons(ARCHIVE)
    story = webcanvas.build_story(pdir, persons)
    assert story["root"]["label"] == "测试书·画布"
    groups = {c["id"]: c for c in story["root"]["children"]}
    assert set(groups) == {"grp-main", "grp-person", "grp-branch", "grp-foreshadow"}
    assert groups["grp-main"]["children"][0]["label"] == "底层逆袭-垄断崩塌"
    assert [i["id"] for i in groups["grp-person"]["children"]] == [
        "p-陈默", "p-林哲", "p-苏晚", "p-老张"]
    assert [i["id"] for i in groups["grp-branch"]["children"]] == ["x-X1", "x-X2"]
    assert [i["id"] for i in groups["grp-foreshadow"]["children"]] == ["f-L1", "f-L2"]
    assert story["seq"] == [["f-L1", "f-L2"]]


def test_build_story_graph_file_fallback_when_no_ledger(tmp_path):
    pdir = make_project("p_20990101-000000-画布兜底", ledger=None, graph={
        "nodes": [{"id": "**L1**", "type": "thread", "first_ch": 1, "due_ch": 2, "status": "planned"},
                  {"id": "陈默", "type": "person"}],
        "threads": [], "persons": ["陈默"], "total_chapters": 3,
    })
    story = webcanvas.build_story(pdir, [])
    fore = next(c for c in story["root"]["children"] if c["id"] == "grp-foreshadow")
    assert [i["id"] for i in fore["children"]] == ["f-L1"]


def test_build_story_empty_dir_degrades(tmp_path):
    story = webcanvas.build_story(str(tmp_path / "空目录"), [])
    assert story["root"]["label"]
    fore = next(c for c in story["root"]["children"] if c["id"] == "grp-foreshadow")
    assert fore["children"] == []


# ---------------------------------------------------------------- 5. 聚合

def test_aggregate_persons_edges_story(tmp_path):
    pdir = make_project("p_20990101-000000-画布聚合")
    out = webcanvas.aggregate(pdir)
    nodes = out["characters"]["nodes"]
    assert [n["id"] for n in nodes] == ["陈默", "林哲", "苏晚", "老张"]
    assert nodes[0]["weight"] >= 1
    edges = out["characters"]["edges"]
    pairs = {tuple(sorted((e["from"], e["to"]))): e["label"] for e in edges}
    assert pairs.get(tuple(sorted(("陈默", "林哲")))) == "对手"
    assert pairs.get(tuple(sorted(("陈默", "苏晚")))) == "暧昧"
    assert out["story"]["root"]["label"] == "测试书·画布"
    assert out["source"]["personCount"] == 4


def test_aggregate_noisy_graph_persons_filtered_when_headings_exist(tmp_path):
    """graph.py 的 关系/立场 碎片人名（立场为/治逻辑…）不得污染有标题档案的画布。"""
    pdir = make_project("p_20990101-000000-画布去噪", graph={
        "nodes": [{"id": "立场为", "type": "person"}, {"id": "治逻辑", "type": "person"},
                  {"id": "陈默", "type": "person"}],
        "threads": [], "persons": ["立场为", "治逻辑"], "total_chapters": 1,
    })
    out = webcanvas.aggregate(pdir)
    assert [n["id"] for n in out["characters"]["nodes"]] == ["陈默", "林哲", "苏晚", "老张"]


def test_aggregate_fallback_persons_when_no_headings(tmp_path):
    """没有 ## 人物 标题时，graph.json person 兜底（黑名单 + ≥2 次滤网）。"""
    archive = "没有标题的档案。林默在此，林默又在此。立场为只是词。"
    pdir = make_project("p_20990101-000000-画布兜底人物", archive=archive, ledger=None, graph={
        "nodes": [{"id": "林默", "type": "person"}, {"id": "立场为", "type": "person"},
                  {"id": "只出现一次的路人", "type": "person"}],
        "threads": [], "persons": [], "total_chapters": 1,
    })
    out = webcanvas.aggregate(pdir)
    assert [n["id"] for n in out["characters"]["nodes"]] == ["林默"]


def test_aggregate_empty_dir(tmp_path):
    out = webcanvas.aggregate(str(tmp_path / "什么都没有"))
    assert out["characters"]["nodes"] == []
    assert out["characters"]["edges"] == []
    assert out["story"]["root"]["children"]


# ---------------------------------------------------------------- 6. 边覆盖层

def test_write_and_read_overlay(overlay_clean):
    webcanvas.write_edge_label("书A", "陈默", "林哲", "宿敌")
    obj = webcanvas.read_overlay("书A")
    pk = webcanvas._pair_key("陈默", "林哲")
    assert obj["edges"][pk]["label"] == "宿敌"
    # 无向：反向读取同一键
    assert webcanvas._pair_key("林哲", "陈默") == pk


def test_write_overlay_truncates_and_clears(overlay_clean):
    webcanvas.write_edge_label("书B", "甲", "乙", "这是一个超过十二个字的超长关系标签")
    pk = webcanvas._pair_key("甲", "乙")
    assert len(webcanvas.read_overlay("书B")["edges"][pk]["label"]) <= webcanvas.MAX_LABEL
    webcanvas.write_edge_label("书B", "甲", "乙", "")
    assert pk not in webcanvas.read_overlay("书B")["edges"]


def test_write_overlay_rejects_self_and_missing(tmp_path, overlay_clean):
    with pytest.raises(HTTPException):
        webcanvas.write_edge_label("书C", "甲", "甲", "自环")
    with pytest.raises(HTTPException):
        webcanvas.write_edge_label("书C", "", "乙", "缺端点")


def test_overlay_concurrent_writes_all_persist(tmp_path, overlay_clean):
    """并发写（锁 + 原子写）：10 线程各写一条，全部落盘不丢。"""
    errs = []

    def w(i):
        try:
            webcanvas.write_edge_label("书并发", f"甲{i}", f"乙{i}", f"关系{i}")
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errs == []
    assert len(webcanvas.read_overlay("书并发")["edges"]) == 10


def test_apply_overlay_merges_labels_and_manual_edges(tmp_path):
    pdir = make_project("p_20990101-000000-画布合并")
    webcanvas.write_edge_label("p_20990101-000000-画布合并", "陈默", "林哲", "宿敌")
    # 苏晚—老张 是自动提取没发现的组合 → 作为「手拉边」补进
    webcanvas.write_edge_label("p_20990101-000000-画布合并", "苏晚", "老张", "忘年之交")
    payload = webcanvas.aggregate(pdir)
    merged = webcanvas.apply_overlay(payload, "p_20990101-000000-画布合并")
    edges = {(e["from"], e["to"]): e for e in merged["characters"]["edges"]}
    # 自动边被覆盖
    assert edges[("林哲", "陈默")]["label"] == "宿敌" and edges[("林哲", "陈默")]["edited"]
    # 覆盖层里的手拉边（自动提取没发现）被补进（覆盖层键是有序对）
    manual = [e for e in merged["characters"]["edges"] if e["kind"] == "manual"]
    assert len(manual) == 1
    assert {manual[0]["from"], manual[0]["to"]} == {"苏晚", "老张"}
    assert manual[0]["label"] == "忘年之交" and manual[0]["edited"]


# ---------------------------------------------------------------- 7. HTTP 契约

def test_api_graph_and_edge_roundtrip(api, overlay_clean):
    key = "p_20990101-000000-画布HTTP"
    make_project(key)
    r = api.get("/api/canvas/graph", params={"key": key})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["resolvedKey"] == key and d["title"] == "测试书·画布"
    assert len(d["characters"]["nodes"]) == 4
    assert d["characters"]["edges"]
    assert d["story"]["root"]["children"]

    r2 = api.post("/api/canvas/edge", json={"key": key, "a": "陈默", "b": "林哲", "label": "宿敌"})
    assert r2.status_code == 200 and r2.json()["ok"]
    d2 = api.get("/api/canvas/graph", params={"key": key}).json()
    e = [x for x in d2["characters"]["edges"] if {x["from"], x["to"]} == {"陈默", "林哲"}]
    assert e and e[0]["label"] == "宿敌" and e[0]["edited"]

    # 清空标签 = 删除覆盖
    r3 = api.post("/api/canvas/edge", json={"key": key, "a": "陈默", "b": "林哲", "label": ""})
    assert r3.status_code == 200


def test_api_graph_key_alias(api, overlay_clean):
    """改名后的旧 key 通过 rename_map 别名解析落到新家。"""
    old_key = "p_20990101-000000-画布旧名"
    new_key = "p_20990101-000000-画布新名"
    make_project(new_key)
    writer.record_rename(old_key, new_key)
    writer._RENAME_MAP_CACHE["mtime"] = None  # 同秒内两次写盘 mtime 缓存可能不失效，手动捅一下
    r = api.get("/api/canvas/graph", params={"key": old_key})
    assert r.status_code == 200
    assert r.json()["resolvedKey"] == new_key


def test_api_errors(api, overlay_clean):
    assert api.get("/api/canvas/graph", params={"key": ""}).status_code == 400
    assert api.get("/api/canvas/graph", params={"key": "p_不存在"}).status_code == 404
    key = "p_20990101-000000-画布校验"
    make_project(key)
    assert api.post("/api/canvas/edge", json={"key": key, "a": "甲", "b": "甲", "label": "自环"}).status_code == 400
    assert api.post("/api/canvas/edge", json={"key": "p_不存在", "a": "甲", "b": "乙", "label": "x"}).status_code == 404
