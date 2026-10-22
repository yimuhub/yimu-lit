"""v1.0.0：诗词流水线 + 格律校验器 回归测试。

覆盖：平水韵数据完整性、切句、体式识别、粘对检查、押韵检查、
     孤平、三态判定、项目创建、API 端点。

校验器用真实古诗做基准：真实格律诗不应被判 fail。
"""
import json
import os
import shutil
import time
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from bot import llm, poetry_checker as pc, poetry_writer as pw

import app as appmod

client = TestClient(appmod.app)

# 真实古诗（基准）：这些不应被判 fail
CLASSICS = [
    ("登鹳雀楼 五绝 王之涣", "白日依山尽，黄河入海流。\n欲穷千里目，更上一层楼。", "五绝"),
    ("春晓 五绝 孟浩然", "春眠不觉晓，处处闻啼鸟。\n夜来风雨声，花落知多少。", "五绝"),
    ("枫桥夜泊 七绝 张继", "月落乌啼霜满天，江枫渔火对愁眠。\n姑苏城外寒山寺，夜半钟声到客船。", "七绝"),
    ("黄鹤楼 七律 崔颢", "昔人已乘黄鹤去，此地空余黄鹤楼。\n黄鹤一去不复返，白云千载空悠悠。\n"
                       "晴川历历汉阳树，芳草萋萋鹦鹉洲。\n日暮乡关何处是？烟波江上使人愁。", "七律"),
    ("锦瑟 七律 李商隐", "锦瑟无端五十弦，一弦一柱思华年。\n庄生晓梦迷蝴蝶，望帝春心托杜鹃。\n"
                       "沧海月明珠有泪，蓝田日暖玉生烟。\n此情可待成追忆，只是当时已惘然。", "七律"),
    ("静夜思 乐府", "床前明月光，疑是地上霜。\n举头望明月，低头思故乡。", ""),
]


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "白日依山尽\n黄河入海流\n欲穷千里目\n更上一层楼",
                        raising=False)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {
        "dims": {"意象": 8, "章法": 8, "声律": 9, "情感": 7},
        "total": 80, "verdict": "尚可"}, raising=False)
    yield


# ---------- 数据 ----------

def test_pingshui_data_loaded():
    idx = pc._rhyme_index()
    assert len(idx) > 8000, f"平水韵表字数过少：{len(idx)}"
    tones = pc._tone_map()
    assert len(tones) > 8000
    # 抽查几个常识字
    assert pc._rhyme_of("东") == "上平一东"
    assert pc._tone_of("东")[0] == "平"
    assert pc._tone_of("雪")[0] == "仄"     # 入声
    assert pc._tone_of("一")[0] == "仄"     # 入声


def test_pingshui_file_shipped():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "skillkit", "poetry", "data", "pingshui.json")
    assert os.path.exists(p), "平水韵数据文件缺失"
    d = json.load(open(p, encoding="utf-8"))
    assert len(d) > 8000


def test_cipai_table_shipped():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "skillkit", "poetry", "词", "词牌格律表.md")
    assert os.path.exists(p)
    t = open(p, encoding="utf-8").read()
    assert "水调歌头" in t and "如梦令" in t
    # 不得含词作原文范例
    assert t.count("范例") <= 1


# ---------- 切句与体式 ----------

def test_extract_lines_two_forms():
    """一句一行 与 两句一行 都要能切对。"""
    a = pc.extract_lines("白日依山尽\n黄河入海流\n欲穷千里目\n更上一层楼")
    assert a == ["白日依山尽", "黄河入海流", "欲穷千里目", "更上一层楼"]
    b = pc.extract_lines("白日依山尽，黄河入海流。\n欲穷千里目，更上一层楼。")
    assert b == ["白日依山尽", "黄河入海流", "欲穷千里目", "更上一层楼"]


def test_extract_lines_strips_numbering():
    out = pc.extract_lines("1. 白日依山尽\n2、黄河入海流\n（3）欲穷千里目")
    assert out == ["白日依山尽", "黄河入海流", "欲穷千里目"]


def test_detect_form():
    assert pc.detect_form(["白日依山尽"] * 4) == "wujue"
    assert pc.detect_form(["月落乌啼霜满天"] * 4) == "qijue"
    assert pc.detect_form(["白日依山尽"] * 4 + ["黄河入海流"] * 4) == "wulv"
    # 字数不一致 → 判不出
    assert pc.detect_form(["白日依山尽", "黄河入海流长"] * 4) == ""
    assert pc.detect_form([]) == ""


def test_normalize_form():
    assert pc.normalize_form("七律") == "qilv"
    assert pc.normalize_form("qijue") == "qijue"
    assert pc.normalize_form("五言律诗") == "wulv"


# ---------- 校验：真实古诗不应误报 ----------

@pytest.mark.parametrize("name,poem,form", CLASSICS)
def test_classics_not_failed(name, poem, form):
    r = pc.validate(poem, form)
    fails = [x for x in r["findings"] if x["severity"] == "fail"]
    assert not fails, f"{name} 被误判：{[x['message'] for x in fails]}"


def test_classics_pass_verdict():
    """格律完整的诗应判 pass 或 unclear（多音字/生僻字），不应 fail。"""
    for name, poem, form in CLASSICS:
        r = pc.validate(poem, form)
        assert r["verdict"] in ("pass", "unclear"), f"{name} 判定为 {r['verdict']}"


# ---------- 校验：违规能抓到 ----------

def test_wrong_line_count_fails():
    r = pc.validate("白日依山尽，黄河入海流。", "五律")
    assert r["verdict"] == "fail"
    assert any("句数不符" in x["message"] for x in r["findings"])


def test_wrong_char_count_fails():
    r = pc.validate("白日依山尽，黄河入海流。\n欲穷千里目，更上江雪。", "五绝")
    assert r["verdict"] == "fail"
    assert any("字数不符" in x["message"] for x in r["findings"])


def test_out_of_rhyme_fails():
    """第 4 句韵脚用入声韵，与主韵出韵。"""
    r = pc.validate("白日依山尽，黄河入海流。\n欲穷千里目，更上江雪。", "五绝")
    assert r["verdict"] == "fail"
    assert any("出韵" in x["message"] for x in r["findings"])


def test_unknown_form_is_unclear():
    r = pc.validate("一些不成句的文字", "")
    assert r["verdict"] == "unclear"
    assert r["form"] == ""


def test_empty_poem():
    r = pc.validate("", "")
    assert r["verdict"] == "unclear"


def test_to_yaml_contains_verdict():
    r = pc.validate("白日依山尽，黄河入海流。\n欲穷千里目，更上一层楼。", "五绝")
    y = pc.to_yaml(r)
    assert "体式" in y and "判定" in y and "检查项" in y


def test_rhyme_lookup():
    d = pc.rhyme_lookup("楼")
    assert d["rhyme"] == "下平十一尤"
    assert d["tone"] == "平"
    assert d["known"]


# ---------- 项目 ----------

def test_create_poetry_project():
    cfg = {"title": "咏梅", "idea": "墙角数枝梅", "form": "七绝", "rounds": 2}
    proj = pw.create_project(cfg)
    try:
        assert os.path.basename(proj.path).startswith("p_")
        for fn in (pw.F_NOTE, pw.F_IMAGERY, pw.F_DRAFT, pw.F_CHECK, pw.F_FINAL, pw.F_PLAN):
            assert os.path.exists(os.path.join(proj.path, fn)), f"缺 {fn}"
        plan = proj.plan()
        assert plan["medium"] == "poetry"
        assert plan["form"] == "qijue"
        assert plan["status"] == "completed"
        st = proj.status()
        assert st["medium"] == "poetry"
    finally:
        shutil.rmtree(proj.path, ignore_errors=True)


def test_modern_poetry_skips_prosody():
    cfg = {"title": "现代诗", "idea": "一个场景", "form": "现代诗"}
    proj = pw.create_project(cfg)
    try:
        report = pw.check_draft("白日依山尽\n黄河入海流", "modern")
        assert report["verdict"] == "pass"
        assert any("不做格律" in x["message"] for x in report["findings"])
    finally:
        shutil.rmtree(proj.path, ignore_errors=True)


def test_recheck_writes_report():
    cfg = {"title": "复校", "idea": "x", "form": "五绝"}
    proj = pw.create_project(cfg)
    try:
        rep = pw.recheck(os.path.basename(proj.path))
        assert rep
        assert "verdict" in rep
        assert os.path.exists(os.path.join(proj.path, pw.F_CHECK))
    finally:
        shutil.rmtree(proj.path, ignore_errors=True)


# ---------- API ----------

def test_poetry_routes_registered():
    paths = {getattr(r, "path", "") for r in appmod.app.routes}
    for p in ["/api/poetry/projects", "/api/poetry/create", "/api/poetry/status",
              "/api/poetry/part", "/api/poetry/recheck", "/api/poetry/rhyme",
              "/api/poetry/check", "/api/poetry/forms"]:
        assert p in paths, f"缺路由 {p}"


def test_poetry_forms_api():
    d = client.get("/api/poetry/forms").json()
    labels = [f["label"] for f in d["forms"]]
    assert "七律" in labels and "词" in labels and "现代诗" in labels


def test_poetry_check_api():
    r = client.get("/api/poetry/check", params={
        "poem": "白日依山尽，黄河入海流。\n欲穷千里目，更上一层楼。",
        "form": "五绝"}).json()
    assert r["verdict"] in ("pass", "unclear")
    assert r["form_label"] == "五绝"


def test_poetry_rhyme_api():
    d = client.get("/api/poetry/rhyme", params={"ch": "楼"}).json()
    assert d["rhyme"] == "下平十一尤"


def test_poetry_create_api():
    """v1.2.1：create 变异步（立即返回 key，后台跑 LLM）。轮询等后台完成再断言产物。"""
    r = client.post("/api/poetry/create", json={
        "title": "接口测试诗", "idea": "一个想法", "form": "七绝"}).json()
    assert "error" not in r, r
    key = r["key"]
    try:
        assert r["medium"] == "poetry"
        assert r.get("async") is True
        # 等后台线程完成（mock LLM 秒回，最多 10s）
        txt = ""
        for _ in range(40):
            time.sleep(0.25)
            p = client.get("/api/poetry/part", params={"key": key, "part": "final"})
            if p.status_code == 200:
                txt = p.json().get("text", "")
                if txt:
                    break
        assert txt, "后台 10s 内未产出定稿"
        assert client.get("/api/poetry/status", params={"key": key}).status_code == 200
        items = client.get("/api/poetry/projects").json()["items"]
        assert any(i["key"] == key for i in items)
    finally:
        shutil.rmtree(os.path.join(pw.PROJECTS_DIR, key), ignore_errors=True)


def test_poetry_missing_key_404():
    assert client.get("/api/poetry/status", params={"key": "p_not_exist"}).status_code == 404
    assert client.get("/api/poetry/part", params={"key": "p_not_exist"}).status_code == 404
