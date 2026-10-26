"""v2.2.2：对话追章与章节真实状态合并。

- _plan_view：02-写作计划.json 的 status/wordCount/title 只升不降地合进 02d 章卡。
- /api/chat/stream：payload.key 指向写作中的作品时，回复推完后进入追章循环——
  新完成的章节经 chapter_start/chapter_delta/chapter_done 推给前端，最后 done 带
  完成小结；开局快照里已完成的章节不重推。
"""
import json
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot import providers, v2api
from bot.v2api import _plan_view, register_v2_routes


def _frames(body: str):
    """把 SSE 正文拆成 [(event, data)]；帧以空行分隔，event/data 各占一行。"""
    out = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        event, data = "message", None
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[len("data:"):].strip())
                except Exception:
                    data = line[len("data:"):].strip()
        out.append((event, data))
    return out


@pytest.fixture(autouse=True)
def _fast_env(monkeypatch):
    """供应商探测放行 + 把 2s 轮询间隔压到 50ms，测试秒级完成。"""
    monkeypatch.setattr(providers, "usable", lambda: True)
    monkeypatch.setattr(v2api, "_HEARTBEAT_SECS", 0.05)


# ---------------------------------------------------------------- 任务一：_plan_view 合并


def _mk_project(tmp_path, chapters, cards, arc_nos=None):
    """伪造 02-写作计划.json + 02d-章卡.jsonl，返回重新读出的 plan（_plan_view 会原地改）。"""
    if arc_nos is None:
        arc_nos = [c["chapterNumber"] for c in chapters]
    plan = {"novelName": "测试书", "status": "in_progress", "chapters": chapters,
            "volumes": [{"no": 1, "arcs": [{"no": 1, "chapters": arc_nos}]}]}
    (tmp_path / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "02d-章卡.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cards), encoding="utf-8")
    return json.loads((tmp_path / "02-写作计划.json").read_text(encoding="utf-8"))


def test_plan_view_merges_real_status(tmp_path):
    chapters = [
        {"chapterNumber": 1, "title": "风起", "status": "completed", "wordCount": 3200,
         "filePath": "第01章-风起.md"},
        {"chapterNumber": 2, "title": "云涌", "status": "in_progress", "wordCount": None,
         "filePath": "第02章-云涌.md"},
        {"chapterNumber": 4, "title": "虹起", "status": "completed", "wordCount": 1111,
         "filePath": "第04章-虹起.md"},
        {"chapterNumber": 5, "title": "风住", "status": "pending", "wordCount": None,
         "filePath": "第05章-风住.md"},
    ]
    cards = [
        {"n": 1, "title": "", "status": "planned"},
        {"n": 2, "title": "云涌", "status": "changed"},
        {"n": 3, "title": "雷落", "status": "done"},
        {"n": 4, "title": "虹起", "status": "done"},
        {"n": 5, "title": "风住", "status": "planned"},
    ]
    plan = _mk_project(tmp_path, chapters, cards, arc_nos=[1, 2, 3, 4, 5])
    # 真实 Project.plan() 每次读盘（02-写作计划.json），假件同样从磁盘读
    proj = SimpleNamespace(path=str(tmp_path),
                           plan=lambda: json.loads((tmp_path / "02-写作计划.json").read_text(encoding="utf-8")))
    out = _plan_view(proj, plan)
    c1, c2, c3, c4, c5 = out["volumes"][0]["arcs"][0]["chapter_cards"]

    # planned + 计划 completed → done，且带 word_count、补 title
    assert c1["status"] == "done" and c1["word_count"] == 3200 and c1["title"] == "风起"
    # changed + 计划 in_progress → writing，word_count 为 None
    assert c2["status"] == "writing" and c2["word_count"] is None
    # done 不被降级；计划里没有该章 → 保持原状，不附加 word_count
    assert c3["status"] == "done" and "word_count" not in c3
    # done + 计划 completed → 仍 done，word_count 照带
    assert c4["status"] == "done" and c4["word_count"] == 1111
    # pending 计划不动 planned 卡
    assert c5["status"] == "planned" and c5["word_count"] is None


def test_plan_view_without_chapters_keeps_cards(tmp_path):
    """写作计划没有 chapters 字段（防御）：章卡原样保留，不报错。"""
    cards = [{"n": 1, "volume": 1, "arc": 1, "title": "风起", "status": "planned"}]
    plan = _mk_project(tmp_path, [], cards)
    plan.pop("chapters", None)
    (tmp_path / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    proj = SimpleNamespace(path=str(tmp_path),
                           plan=lambda: json.loads((tmp_path / "02-写作计划.json").read_text(encoding="utf-8")))
    out = _plan_view(proj, plan)
    card = out["volumes"][0]["arcs"][0]["chapter_cards"][0]
    assert card["status"] == "planned" and "word_count" not in card


# ---------------------------------------------------------------- 任务二：追章流


class FakeProject:
    """最小项目假件：plan() 每次返回可变副本（真 Project 是每次读盘，无缓存问题）。"""

    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        self.running = True
        self.state = "in_progress"
        self.plan_data = {
            "novelName": "测试书", "status": "in_progress",
            "chapters": [
                {"chapterNumber": 1, "title": "风起", "status": "completed",
                 "wordCount": 3000, "filePath": "第01章-风起.md"},
                {"chapterNumber": 2, "title": "云涌", "status": "in_progress",
                 "wordCount": None, "filePath": "第02章-云涌.md"},
            ],
        }

    def plan(self):
        with self.lock:
            return json.loads(json.dumps(self.plan_data))

    def status(self):
        with self.lock:
            chs = self.plan_data["chapters"]
            return {"state": self.state, "running": self.running, "paused": False,
                    "done": sum(1 for c in chs if c["status"] == "completed"),
                    "total": len(chs),
                    "words": sum(c.get("wordCount") or 0 for c in chs),
                    "name": self.plan_data.get("novelName", "测试书")}

    def finish_second(self):
        """模拟后台写作线程：片刻后第 2 章完成、全书 completed。"""
        with self.lock:
            ch2 = self.plan_data["chapters"][1]
            ch2["status"] = "completed"
            ch2["wordCount"] = 3210
            self.state = "completed"
            self.running = False


def test_follow_stream_pushes_new_chapters_only(tmp_path):
    proj = FakeProject(tmp_path)
    (tmp_path / "第01章-风起.md").write_text("第一章正文，开局快照里不应被重推。" * 5, encoding="utf-8")
    body2 = "夜色像一层湿布盖住港口，探照灯扫过缆绳与锈迹。" * 4  # 88 字 → 两片
    (tmp_path / "第02章-云涌.md").write_text(body2, encoding="utf-8")

    def fake_chat(sid, message, key=None):
        assert key == "bk", "key 必须随消息透传给会话状态机"
        threading.Timer(0.15, proj.finish_second).start()
        return "已开始创作"

    app = FastAPI()
    register_v2_routes(app, lambda key: proj if key == "bk" else None, fake_chat)
    client = TestClient(app)

    r = client.post("/api/chat/stream",
                    json={"message": "继续写", "key": "bk", "session_id": "s-follow"})
    assert r.status_code == 200, r.text
    frames = _frames(r.text)
    events = [e for e, _ in frames]

    # 回复正文照旧走 start/status/delta
    assert events[0] == "start"
    assert "".join(d["text"] for e, d in frames if e == "delta") == "已开始创作"

    # 追章：只有第 2 章被推（第 1 章在开局快照里，不重推）
    starts = [d for e, d in frames if e == "chapter_start"]
    deltas = [d for e, d in frames if e == "chapter_delta"]
    dones = [d for e, d in frames if e == "chapter_done"]
    assert [d["no"] for d in starts] == [2]
    assert starts[0]["title"] == "云涌"
    assert all(d["no"] == 2 for d in deltas)
    assert "".join(d["text"] for d in deltas) == body2
    assert [d["no"] for d in dones] == [2] and dones[0]["words"] == 3210

    # 收尾 done：正文保留 + 完成小结（书名/章数/总字数）+ chapters 清单
    assert events[-1] == "done"
    done = frames[-1][1]
    assert done["ok"] is True
    assert done["text"].startswith("已开始创作")
    assert "《测试书》" in done["text"] and "2/2" in done["text"]
    assert done["chapters"] == [{"no": 2, "words": 3210}]
    assert done["session_id"] == "s-follow" and "elapsed" in done


def test_follow_stream_paused_exit(tmp_path):
    """写作中途暂停 → 循环退出，done 带暂停提示，不再等待。"""
    proj = FakeProject(tmp_path)

    def pause_soon():
        with proj.lock:
            proj.state = "paused"
            proj.running = False
            proj.plan_data["status"] = "paused"

    app = FastAPI()
    register_v2_routes(app, lambda key: proj if key == "bk" else None,
                       lambda s, m, k=None: threading.Timer(0.15, pause_soon).start() or "好的")
    client = TestClient(app)

    r = client.post("/api/chat/stream",
                    json={"message": "继续写", "key": "bk", "session_id": "s-pause"})
    frames = _frames(r.text)
    done = frames[-1][1]
    assert done["ok"] is True
    assert "暂停" in done["text"]
    assert done["chapters"] == []
    assert not [e for e, _ in frames if e.startswith("chapter_")]


def test_no_key_keeps_plain_done(tmp_path):
    """payload 没有 key（或作品不在写作中）→ 行为与旧版完全一致。"""
    proj = FakeProject(tmp_path)
    proj.running = False
    proj.state = "completed"

    app = FastAPI()
    register_v2_routes(app, lambda key: proj if key == "bk" else None, lambda s, m, k=None: "普通回复")
    client = TestClient(app)

    r = client.post("/api/chat/stream", json={"message": "你好", "session_id": "s-plain"})
    frames = _frames(r.text)
    assert [e for e, _ in frames] == ["start", "status", "delta", "done"]
    done = frames[-1][1]
    assert done["text"] == "普通回复" and "chapters" not in done
    assert "".join(d["text"] for e, d in frames if e == "delta") == "普通回复"
