"""v1.0.0：散文流水线 + 反 AI 腔检测器 回归测试。

检测器用两类样本做基准：AI 腔文本必须被抓住，人写文本不应误报。
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from bot import llm, prose_writer as pw, slop_checker as sc

import app as appmod

client = TestClient(appmod.app)

AI_TEXT = """在当今这个日新月异的时代，人工智能技术已经扮演了至关重要的角色。
研究表明，AI 技术在提升效率方面具有不可替代的价值。
本文将探讨 AI 技术在教育领域的应用，让我们一起拭目以待。
综上所述，AI 技术是推动社会发展的重要举措。我们拭目以待。"""

HUMAN_TEXT = """我妈的抽屉里还留着我小学的转学申请表，日期是 2003 年 9 月 1 日。
那天她送我去学校，我背着书包，她站在校门口没走。
我回头看了一眼，她在原地站着，手插在围裙口袋里。
后来那张纸一直放着，谁也没动过。"""


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch):
    def _fake_chat(messages, *a, **k):
        # 审稿模式要出诊断表，其余返正文
        user = ""
        for m in messages:
            if m.get("role") == "user":
                user = m.get("content", "")
        if "只出诊断" in user or "位置" in user and "最小改法" in user:
            return "| 位置 | 问题 | 读起来的后果 | 最小改法 |\n|---|---|---|---|\n| 第1句 | 太短 | 平淡 | 补一个细节 |"
        return HUMAN_TEXT

    monkeypatch.setattr(llm, "chat", _fake_chat, raising=False)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {}, raising=False)
    yield


# ---------- 检测器 ----------

def test_slop_detects_ai_text():
    rep = sc.scan(AI_TEXT)
    assert rep["verdict"] == "review"
    assert rep["stats"]["blocking"] > 0
    cats = set(x.get("category") for x in rep["findings"])
    assert "套话开场" in cats
    assert "总结句" in cats
    assert "元话语" in cats


def test_slop_clean_text_no_false_positive():
    rep = sc.scan(HUMAN_TEXT)
    assert rep["stats"]["blocking"] == 0, \
        f"误报：{[x['message'] for x in rep['findings'] if x['severity'] == 'blocking']}"
    assert rep["verdict"] in ("pass", "review")


def test_slop_empty_text():
    rep = sc.scan("")
    assert rep["stats"]["blocking"] == 0
    assert rep["verdict"] == "pass"


def test_slop_telegraph_detection():
    t = "他走了。\n她哭了。\n灯灭了。"
    rep = sc.scan(t)
    cats = set(x.get("category") for x in rep["findings"])
    assert "电报体" in cats


def test_slop_top_words():
    words = sc.top_words("苹果苹果苹果苹果香蕉橘子梨子", n=5)
    assert words
    assert words[0][0] == "苹果"


def test_slop_repetition():
    rep = sc.check_repetition("重要 重要 重要 重要 重要 其它 别的", threshold=3)
    assert any(x["word"] == "重要" for x in rep)


def test_slop_yaml_report():
    rep = sc.scan(AI_TEXT)
    y = sc.report_yaml(rep)
    assert "判定" in y and "命中项" in y and "阻断" in y


# ---------- 散文流水线 ----------

def test_normalize_operation():
    assert pw.normalize_op("write") == "write"
    assert pw.normalize_op("审") == "review"
    assert pw.normalize_op("帮我改一下") == "refactor"
    assert pw.normalize_op("重写") == "recreate"


def test_normalize_genre():
    assert pw.normalize_genre("suibi") == "suibi"
    assert pw.normalize_genre("随笔") == "suibi"
    assert pw.normalize_genre("议论") == "yilun"
    assert pw.normalize_genre("人物特写") == "profile"


def test_create_prose_project_write():
    cfg = {"title": "写一篇", "idea": "怀念母亲", "genre": "suibi", "operation": "write"}
    proj = pw.create_project(cfg)
    try:
        assert os.path.basename(proj.path).startswith("e_")
        for fn in (pw.F_BRIEF, pw.F_MATERIAL, pw.F_DRAFT, pw.F_SLOP, pw.F_FINAL, pw.F_PLAN):
            assert os.path.exists(os.path.join(proj.path, fn)), f"缺 {fn}"
        plan = proj.plan()
        assert plan["medium"] == "prose"
        assert plan["operation"] == "write"
        assert plan["status"] == "completed"
    finally:
        shutil.rmtree(proj.path, ignore_errors=True)


def test_create_prose_project_review_does_not_modify():
    """review 模式只出诊断，不改原文。"""
    cfg = {"title": "审稿", "idea": "x", "genre": "suibi", "operation": "review",
           "sourceText": HUMAN_TEXT}
    proj = pw.create_project(cfg)
    try:
        draft = pw.read_part(os.path.basename(proj.path), "draft")
        assert "位置" in draft and "最小改法" in draft
        plan = proj.plan()
        assert plan["status"] == "completed"
    finally:
        shutil.rmtree(proj.path, ignore_errors=True)


def test_prose_status():
    cfg = {"title": "状态", "idea": "x", "genre": "suibi"}
    proj = pw.create_project(cfg)
    try:
        st = proj.status()
        assert st["medium"] == "prose"
        assert st["total"] == 6
    finally:
        shutil.rmtree(proj.path, ignore_errors=True)


# ---------- API ----------

def test_prose_routes_registered():
    paths = {getattr(r, "path", "") for r in appmod.app.routes}
    for p in ["/api/prose/projects", "/api/prose/create", "/api/prose/status",
              "/api/prose/part", "/api/prose/operations", "/api/slop/scan"]:
        assert p in paths, f"缺路由 {p}"


def test_prose_operations_api():
    d = client.get("/api/prose/operations").json()
    assert len(d["operations"]) == 4
    assert len(d["genres"]) >= 5


def test_slop_scan_api():
    r = client.post("/api/slop/scan", json={"text": AI_TEXT}).json()
    assert r["verdict"] == "review"
    assert r["stats"]["blocking"] > 0
    assert "yaml" in r


def test_prose_create_api():
    r = client.post("/api/prose/create", json={
        "title": "接口散文2", "idea": "一个想法", "genre": "suibi", "operation": "write"}).json()
    assert "error" not in r, r
    key = r["key"]
    try:
        assert r["medium"] == "prose"
        assert client.get("/api/prose/status", params={"key": key}).status_code == 200
        parts = client.get("/api/prose/part", params={"key": key, "part": "final"}).json()
        assert parts["text"]
    finally:
        shutil.rmtree(os.path.join(pw.PROJECTS_DIR, key), ignore_errors=True)


def test_prose_missing_key_404():
    assert client.get("/api/prose/status", params={"key": "e_not_exist"}).status_code == 404
    assert client.get("/api/prose/part", params={"key": "e_not_exist"}).status_code == 404
