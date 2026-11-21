"""v5.4.x 轻量榜单风向回归（docs/系统化升级方案-v5.1 第 8.2 节）。

覆盖：
1. 行式解析容错：多种名次写法（1. / 1、 / TOP3 / 第4名 / 05 / [6]）、竖线与空格分隔、
   作者内联「作者：X」、数据列识别、表头跳过、无名录降级逐行编号、去重；
2. SSR 文本解析：表格单元格 → 行 → 条目；
3. 快照轮转：每源每日一份、同日重抓覆盖、120 天保留清理；
4. 升降幅计算：↑n / ↓n / 新入榜 / 持平，及「跨快照基准」的 HTTP 读路径；
5. URL 抓取：超时降级为中文可操作提示、2MB 上限截断后仍可解析、
   非榜单页面解析失败如实报错、SSRF 拦截、URL→源名推导；
6. AI 解读：scene=「榜单解读」、结果随快照缓存（二次调用不烧模型）、refresh 强制重算；
7. 灵感库补充端点：置顶 / 行内编辑（#标签 重提取、300 字上限、404）。
"""
import datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot import inspirations, llm, rankbook, webrank


@pytest.fixture()
def snap_dir(tmp_path, monkeypatch):
    """快照根目录指到临时目录，测试互不污染（对齐 inspirations.DATA_FILE 的隔离惯例）。"""
    d = str(tmp_path / "rank-snapshots")
    monkeypatch.setattr(rankbook, "SNAP_DIR", d)
    return d


@pytest.fixture()
def client():
    """直接挂 webrank.router（+ webstats.router，灵感列表端点在彼处）测接口契约，
    与生产挂载（app.include_router）完全同一条路。app.py 的挂载由主会话完成（本任务不许改 app.py）。"""
    from bot import webstats

    app = FastAPI()
    app.include_router(webrank.router)
    app.include_router(webstats.router)
    return TestClient(app)


def _e(rank, title, author="", metric=""):
    return {"rank": rank, "title": title, "author": author, "metric": metric}


TODAY = datetime.date.today()
D0 = TODAY.isoformat()
D1 = (TODAY - datetime.timedelta(days=1)).isoformat()
D_OLD = (TODAY - datetime.timedelta(days=121)).isoformat()
D_KEEP = (TODAY - datetime.timedelta(days=120)).isoformat()


# ---------------------------------------------------------------- 1. 行式解析

def test_parse_rank_lines_separators_and_rank_styles():
    text = """
1. 天行健 作者：燕垒生 234万推荐票
2、诡秘之主 | 爱潜水的乌贼 | 12.3万月票
TOP3 大奉打更人 卖报小郎君 8.9万
第4名 星门 无罪 7万收藏
05 五胚 猫腻
[6] 道诡异仙 狐尾的笔
No.7 夜的命名术 会说话的肘子

名次 书名 作者 数据
"""
    entries = rankbook.parse_rank_lines(text)
    assert [e["rank"] for e in entries] == [1, 2, 3, 4, 5, 6, 7]
    assert entries[0]["title"] == "天行健"
    assert entries[0]["author"] == "燕垒生"
    assert entries[0]["metric"] == "234万推荐票"
    assert entries[1]["title"] == "诡秘之主" and entries[1]["author"] == "爱潜水的乌贼"
    assert entries[2]["title"] == "大奉打更人" and entries[2]["metric"] == "8.9万"
    assert entries[3]["rank"] == 4 and entries[3]["title"] == "星门"
    assert entries[4]["title"] == "五胚" and entries[4]["author"] == "猫腻" and entries[4]["metric"] == ""
    assert entries[6]["rank"] == 7    # No.7 写法
    assert len(entries) == 7          # 表头行（名次/书名/作者）被跳过


def test_parse_rank_lines_fallback_sequential_without_rank():
    """整段没有任何名次前缀：降级为每行一条、顺序编号（容错优先，≥3 行才认作榜单）。"""
    entries = rankbook.parse_rank_lines("斗破苍穹 天蚕土豆\n凡人修仙传 忘语\n雪中悍刀行 烽火戏诸侯\n")
    assert [e["rank"] for e in entries] == [1, 2, 3]
    assert entries[0]["title"] == "斗破苍穹" and entries[0]["author"] == "天蚕土豆"
    assert entries[1]["title"] == "凡人修仙传" and entries[1]["author"] == "忘语"
    # 不足 3 行：不硬凑条目（单行提示页/登录墙如实判失败）
    assert rankbook.parse_rank_lines("斗破苍穹 天蚕土豆\n凡人修仙传 忘语\n") == []


def test_parse_rank_lines_dedupes_and_empty_is_empty():
    entries = rankbook.parse_rank_lines("1. 诡秘之主 爱潜水的乌贼\n2. 诡秘之主 爱潜水的乌贼\n")
    assert [e["rank"] for e in entries] == [1]   # 同名书只留首个
    assert rankbook.parse_rank_lines("") == []
    assert rankbook.parse_rank_lines("   \n  \n") == []
    # 只有表头行 → 解析不出条目（调用方如实报错，不做猜测性补救）
    assert rankbook.parse_rank_lines("名次 书名 作者 数据") == []


def test_parse_rank_html_table():
    page = (
        "<html><head><style>.ad{display:none}</style></head><body>"
        "<table><tr><th>名次</th><th>书名</th><th>作者</th><th>数据</th></tr>"
        "<tr><td>1</td><td>诡秘之主</td><td>爱潜水的乌贼</td><td>12.3万</td></tr>"
        "<tr><td>2</td><td>全职高手</td><td>蝴蝶蓝</td><td>9.1万</td></tr></table>"
        "<script>var x=1;</script></body></html>"
    )
    entries = rankbook.parse_rank_html(page)
    assert [e["rank"] for e in entries] == [1, 2]
    assert entries[0]["title"] == "诡秘之主" and entries[0]["author"] == "爱潜水的乌贼"
    assert entries[0]["metric"] == "12.3万"


# ---------------------------------------------------------------- 2. 快照轮转

def test_snapshot_same_day_overwrite(snap_dir):
    rankbook.save_snapshot("起点风云榜", D0, [_e(1, "旧书名")], url="http://x")
    snap = rankbook.save_snapshot("起点风云榜", D0, [_e(1, "新书名")])   # 同日重抓覆盖
    assert snap["entries"][0]["title"] == "新书名"
    snaps = rankbook.list_snapshots("起点风云榜")
    assert [s["date"] for s in snaps] == [D0]     # 每源每日一份
    assert snaps[0]["count"] == 1


def test_snapshot_retention_120_days(snap_dir):
    rankbook.save_snapshot("s", D_OLD, [_e(1, "太久之前")])
    rankbook.save_snapshot("s", D_KEEP, [_e(1, "恰好保期内")])
    rankbook.save_snapshot("s", D0, [_e(1, "今天")])
    dates = {s["date"] for s in rankbook.list_snapshots("s")}
    assert D_OLD not in dates        # 超过 120 天：已清理
    assert D_KEEP in dates and D0 in dates


def test_normalize_source_rejects_traversal():
    assert rankbook.normalize_source("../evil") == "evil"
    assert rankbook.normalize_source("..") == "manual"
    assert rankbook.normalize_source("") == "manual"
    assert rankbook.normalize_source("起点风云榜") == "起点风云榜"
    assert rankbook.normalize_source("a/b\\c:d") == "a-b-c-d"


# ---------------------------------------------------------------- 3. 升降幅

def test_compare_entries_up_down_new_same():
    prev = [_e(1, "甲"), _e(2, "乙"), _e(3, "丙"), _e(4, "丁")]
    cur = [_e(1, "乙"), _e(2, "丙"), _e(3, "戊"), _e(4, "丁"), _e(5, "己")]
    changes = rankbook.compare_entries(prev, cur)
    assert changes[0]["change"]["tag"] == "up" and changes[0]["change"]["delta"] == 1     # 乙 2→1 ↑1
    assert changes[0]["change"]["prev_rank"] == 2
    assert changes[1]["change"]["tag"] == "up" and changes[1]["change"]["delta"] == 1
    assert changes[2]["change"]["tag"] == "new"                                           # 戊 新入榜
    assert changes[3]["change"]["tag"] == "same" and changes[3]["change"]["delta"] == 0
    assert changes[4]["change"]["tag"] == "new"
    # 反向：1→3 应得 ↓2
    down = rankbook.compare_entries([_e(1, "甲")], [_e(3, "甲")])
    assert down[0]["change"]["tag"] == "down" and down[0]["change"]["delta"] == -2


def test_compare_across_snapshots_via_store(snap_dir):
    rankbook.save_snapshot("榜", D1, [_e(1, "甲"), _e(2, "乙")])
    rankbook.save_snapshot("榜", D0, [_e(1, "乙"), _e(2, "甲")])
    base = rankbook.previous_snapshot("榜", D0)
    assert base["date"] == D1
    changes = rankbook.compare_entries(base["entries"], rankbook.load_snapshot("榜", D0)["entries"])
    assert changes[0]["change"]["tag"] == "up"       # 乙 2→1
    assert changes[1]["change"]["tag"] == "down"     # 甲 1→2


# ---------------------------------------------------------------- 4. URL 抓取

class _FakeResp:
    """httpx 流式响应替身：iter_bytes 分块吐 body。"""
    status_code = 200

    def __init__(self, body: str, chunk: int = 65536):
        self._body = body.encode("utf-8")
        self._chunk = chunk
        self.headers = {"content-type": "text/html; charset=utf-8"}

    def iter_bytes(self):
        for i in range(0, len(self._body), self._chunk):
            yield self._body[i:i + self._chunk]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, resp, *a, **k):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url, **k):
        return self._resp


TABLE_HTML = (
    "<table><tr><td>1</td><td>诡秘之主</td><td>爱潜水的乌贼</td><td>12.3万</td></tr>"
    "<tr><td>2</td><td>全职高手</td><td>蝴蝶蓝</td><td>9.1万</td></tr></table>"
)


def test_fetch_url_success_and_source_from_domain(snap_dir, client, monkeypatch):
    monkeypatch.setattr(webrank, "_url_allowed", lambda u: True)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(_FakeResp(TABLE_HTML)))
    r = client.post("/api/rank/fetch", json={"url": "https://www.qidian.com/rank/yuepiao/"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["source"] == "qidian.com"          # 未指定源名 → 域名（去 www.）
    assert d["date"] == D0 and d["count"] == 2
    assert d["changes"][0]["change"]["tag"] == "new"
    assert len(d["snapshot"]["entries"]) == 2
    # 快照确实落盘
    assert rankbook.load_snapshot("qidian.com", D0)["entries"][1]["title"] == "全职高手"


def test_fetch_url_timeout_degrades_to_readable_message(snap_dir, client, monkeypatch):
    """URL 抓取超时：降级为中文可操作提示（如实报告），不留半份快照。"""
    monkeypatch.setattr(webrank, "_url_allowed", lambda u: True)

    class _TimeoutClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **k):
            raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _TimeoutClient())
    r = client.post("/api/rank/fetch", json={"url": "https://slow.example.com/rank", "source": "慢站"})
    assert r.status_code == 400
    assert "超时" in r.json()["detail"]
    assert rankbook.load_snapshot("慢站", D0) is None


def test_fetch_url_2mb_cap_truncates_but_parses(snap_dir, monkeypatch):
    """2MB 上限：超过即停止读取（截断），开头部分的榜单仍可解析。"""
    monkeypatch.setattr(webrank, "_url_allowed", lambda u: True)
    big = TABLE_HTML + "<p>" + "填充" * (1024 * 1024 // 2) + "</p>"   # ≈3MB
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _FakeClient(_FakeResp(big)))
    text = webrank.fetch_url_text("https://example.com/rank")
    assert len(text.encode("utf-8")) <= 2 * 1024 * 1024 + 65536 + 1024   # 截断在 2MB 附近
    entries = rankbook.parse_rank_html(text)
    assert entries[0]["title"] == "诡秘之主"


def test_fetch_url_unparseable_page_reports_honestly(snap_dir, client, monkeypatch):
    """抓到了页面但解析不出榜单：如实报错（脚本渲染/需登录场景），不做猜测性补救。"""
    monkeypatch.setattr(webrank, "_url_allowed", lambda u: True)
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **k: _FakeClient(_FakeResp("<html><body>请登录后查看</body></html>")))
    r = client.post("/api/rank/fetch", json={"url": "https://example.com/rank", "source": "登录墙"})
    assert r.status_code == 400
    assert "未能解析出榜单条目" in r.json()["detail"]
    assert "粘贴文本" in r.json()["detail"]   # 给出可操作建议


def test_fetch_url_ssrf_blocked(snap_dir, client):
    """内网/回环地址直接拦截（防 SSRF），不发起请求。"""
    r = client.post("/api/rank/fetch", json={"url": "http://127.0.0.1:8848/rank"})
    assert r.status_code == 400
    assert "不允许抓取" in r.json()["detail"]


def test_fetch_requires_url(snap_dir, client):
    assert client.post("/api/rank/fetch", json={}).status_code == 400


# ---------------------------------------------------------------- 5. 文本采集与快照读路径

def test_text_endpoint_and_snapshot_read(snap_dir, client):
    # 采集端点永远写「今天」的快照（每源每日一份）：昨日基准用账本层预置来模拟
    rankbook.save_snapshot("测试榜", D1, [_e(1, "旧甲", "作者A", "10万"), _e(2, "旧乙", "作者B", "8万")])
    today_text = "1. 旧乙 作者B 9万\n2. 旧甲 作者A 7万\n3. 新丙 作者C 6万\n"
    r1 = client.post("/api/rank/text", json={"text": today_text, "source": "测试榜"})
    assert r1.status_code == 200
    d = r1.json()
    assert d["date"] == D0 and d["count"] == 3
    assert d["compared_with"] == D1
    assert [c["change"]["tag"] for c in d["changes"]] == ["up", "down", "new"]
    # GET 快照：缺省最新 + 自动对比上一份
    r2 = client.get("/api/rank/snapshot", params={"source": "测试榜"})
    assert r2.status_code == 200
    got = r2.json()
    assert got["snapshot"]["date"] == D0
    assert got["compared_with"] == D1
    # 指定日期 + 指定对比基准
    r3 = client.get("/api/rank/snapshot", params={"source": "测试榜", "date": D0, "against": D1})
    assert r3.status_code == 200
    assert r3.json()["changes"][0]["change"]["delta"] == 1
    # 不存在的快照 → 404
    assert client.get("/api/rank/snapshot", params={"source": "不存在"}).status_code == 404


def test_text_endpoint_parse_failure_reports_honestly(snap_dir, client):
    r = client.post("/api/rank/text", json={"text": "名次 书名 作者 数据", "source": "空榜"})
    assert r.status_code == 400
    assert "未能解析出榜单条目" in r.json()["detail"]
    assert client.post("/api/rank/text", json={"text": "  ", "source": "空榜"}).status_code == 400


def test_sources_endpoint_lists_sources_and_dates(snap_dir, client):
    rankbook.save_snapshot("榜A", D1, [_e(1, "x")])
    rankbook.save_snapshot("榜A", D0, [_e(1, "y")])
    rankbook.save_snapshot("榜B", D0, [_e(1, "z")])
    r = client.get("/api/rank/sources")
    assert r.status_code == 200
    srcs = {s["source"]: s for s in r.json()["sources"]}
    assert set(srcs) == {"榜A", "榜B"}
    assert srcs["榜A"]["dates"][0] == D0 and srcs["榜A"]["latest"] == D0


# ---------------------------------------------------------------- 6. AI 解读

def test_review_scene_cached_and_refresh(snap_dir, client, monkeypatch):
    """AI 解读：scene=「榜单解读」、只发榜单数据；缓存命中不烧模型；refresh 强制重算。"""
    rankbook.save_snapshot("解读榜", D0, [_e(1, "诡秘之主", "爱潜水的乌贼", "12.3万")])
    calls = []

    def fake_chat(messages, **kw):
        calls.append({"messages": messages, "kw": kw})
        return "分类分布：玄幻占六成。\n\n标签风向：无缺上位。\n\n竞品提示：可借鉴克苏鲁+蒸汽朋克。"

    monkeypatch.setattr(llm, "chat", fake_chat)

    r1 = client.post("/api/rank/review", json={"source": "解读榜"})
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["ok"] is True and d1["cached"] is False
    assert len(calls) == 1
    assert calls[0]["kw"].get("scene") == "榜单解读"
    user_msg = calls[0]["messages"][-1]["content"]
    assert "诡秘之主" in user_msg and "12.3万" in user_msg      # 只发榜单数据
    assert calls[0]["messages"][0]["content"].find("不含任何创作内容") >= 0   # 隐私口径在提示词里
    # 解读随快照缓存
    snap = rankbook.load_snapshot("解读榜", D0)
    assert snap["review"].startswith("分类分布") and snap["review_at"]

    r2 = client.post("/api/rank/review", json={"source": "解读榜"})
    assert r2.status_code == 200 and r2.json()["cached"] is True
    assert r2.json()["review"] == d1["review"]
    assert len(calls) == 1                        # 缓存命中：不再调用模型

    r3 = client.post("/api/rank/review", json={"source": "解读榜", "refresh": True})
    assert r3.status_code == 200 and r3.json()["cached"] is False
    assert len(calls) == 2                        # refresh 强制重算


def test_review_no_snapshot_404_and_empty_model_result(snap_dir, client, monkeypatch):
    assert client.post("/api/rank/review", json={"source": "无快照"}).status_code == 404
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "")   # conftest 缺省 mock 即此形态
    rankbook.save_snapshot("空解读", D0, [_e(1, "x")])
    r = client.post("/api/rank/review", json={"source": "空解读"})
    assert r.status_code == 400
    assert "没有返回解读内容" in r.json()["detail"]
    snap = rankbook.load_snapshot("空解读", D0)
    assert snap["review"] == ""                   # 失败不写缓存


# ---------------------------------------------------------------- 7. 灵感库补充端点

@pytest.fixture()
def insp_file(tmp_path, monkeypatch):
    f = str(tmp_path / "inspirations.json")
    monkeypatch.setattr(inspirations, "DATA_FILE", f)
    return f


def test_inspiration_pin_endpoint(snap_dir, client, insp_file):
    item = inspirations.add("一条灵感 #修仙")
    r = client.post(f"/api/inspirations/{item['id']}/pin", json={"pinned": True})
    assert r.status_code == 200
    assert r.json()["item"]["pinned"] is True
    # 置顶后排在最前
    lst = client.get("/api/inspirations?limit=10").json()["items"]
    assert lst[0]["id"] == item["id"]
    # 取消置顶
    r2 = client.post(f"/api/inspirations/{item['id']}/pin", json={"pinned": False})
    assert r2.json()["item"]["pinned"] is False
    # 不存在的条目 → 404
    assert client.post("/api/inspirations/no-such-id/pin", json={"pinned": True}).status_code == 404


def test_inspiration_edit_endpoint_reextracts_tags(snap_dir, client, insp_file):
    item = inspirations.add("原始正文 #修仙")
    r = client.put(f"/api/inspirations/{item['id']}", json={"text": "改后的正文 #都市 #重生"})
    assert r.status_code == 200
    d = r.json()["item"]
    assert d["text"] == "改后的正文 #都市 #重生"
    assert d["tags"] == ["都市", "重生"]          # #标签 重提取（与新增同口径）
    assert d["updated"] >= item["created"]
    # 空文本 / 超长 → 400 中文提示；不存在 → 404
    assert client.put(f"/api/inspirations/{item['id']}", json={"text": "  "}).status_code == 400
    long_text = "x" * 301
    assert client.put(f"/api/inspirations/{item['id']}", json={"text": long_text}).status_code == 400
    assert client.put("/api/inspirations/no-such-id", json={"text": "hi"}).status_code == 404
