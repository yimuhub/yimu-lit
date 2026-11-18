"""v4.22.1 正文纯净域回归：复制 / 预览 / 成书 / 导出 / 聊天留档一律不得带元信息。

P0 铁律（见 功能表.md「铁律 0」）：可复制可导出的正文 = 章题 + 章首引子 + 正文。
本章概要、章节备注、修改说明、审核评分、字数统计都是元信息，一个字符都不许出去。
"""
import json
import os
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import app as appmod
from bot import export as export_mod
from bot import purity, writer

KEY = "book"

POLLUTED = """# 第03章 副驾驶

## 本章概要
周望载到红雨衣，留下旧车票。评分 86 分。

## 章首引子
雨是从傍晚开始下的。

## 正文
他关掉收音机。

## 章节备注
下章埋线：岚字。

## 修改说明
1. 加强了开头的雨声细节。
2. 把对白改得更冷。

## 审核评分
| 维度 | 得分 |
|---|---|
| 结构 | 8 |
| 语言 | 7 |

综合 86 分。
"""


# ---------------------------------------------------------------- 纯净域本体

def test_chapter_text_drops_rewrite_and_review_sections():
    out = purity.chapter_text(POLLUTED)
    assert out.startswith("# 第03章 副驾驶")
    assert "雨是从傍晚开始下的。" in out and "他关掉收音机。" in out
    for junk in ("本章概要", "章节备注", "修改说明", "审核评分", "维度", "综合 86 分"):
        assert junk not in out


def test_chapter_text_no_notes_but_trailing_sections():
    """没有「章节备注」时，尾部的修改说明/评分小节也必须被剥掉（旧实现必漏）。"""
    t = ("# 第03章 副驾驶\n\n## 章首引子\n雨。\n\n## 正文\n他关掉收音机。\n\n"
         "## 修改说明\n1. 加强了开头的雨声细节。\n")
    out = purity.chapter_text(t)
    assert "修改说明" not in out and "加强了开头" not in out
    assert "他关掉收音机。" in out


def test_chapter_text_tail_noise_without_header():
    """模型不写小节标题、直接甩一段评分/说明 —— 也要按噪声剥掉。"""
    t = ("# 第03章 副驾驶\n\n他关掉收音机。\n\n---\n\n"
         "本章评分 86 分，主要问题：节奏偏慢。\n修改说明：1) 删去重复描写；")
    out = purity.chapter_text(t)
    assert out == "# 第03章 副驾驶\n\n他关掉收音机。"


def test_chapter_text_idempotent():
    once = purity.chapter_text(POLLUTED)
    assert purity.chapter_text(once) == once
    assert purity.chapter_body(once) == purity.chapter_body(POLLUTED)


def test_chapter_body_has_no_title_no_markers():
    body = purity.chapter_body(POLLUTED)
    assert not body.startswith("#")
    assert "##" not in body
    assert body.startswith("雨是从傍晚开始下的。")


def test_sanitize_keeps_four_sections_but_drops_noise():
    """写盘前净化：内部四段（概要/引子/正文/备注）保留，外来小节不落盘。"""
    out = purity.sanitize_chapter(POLLUTED)
    assert "## 本章概要" in out and "## 章首引子" in out
    assert "## 正文" in out and "## 章节备注" in out
    assert "修改说明" not in out and "审核评分" not in out


def test_strip_fragment_removes_ack_and_section_headers():
    assert purity.strip_fragment("好的，以下是续写：\n\n## 正文\n\n雨更大了。") == "雨更大了。"
    assert "评分" not in purity.strip_fragment("雨更大了。\n\n## 评分\n86 分")


def test_ack_regex_does_not_eat_normal_prose():
    """「这是雨夜。」是正常正文，不能被当成「这是……」开场白吃掉。"""
    t = "# 第01章 起\n\n这是雨夜。\n\n他关上车门。"
    assert purity.chapter_body(t).startswith("这是雨夜。")


def test_summary_that_swallowed_body_is_not_wiped():
    """模型漏写 `## 正文`、正文并进超长「本章概要」时，不许把整章洗没。"""
    t = "# 第01章 某章\n\n## 本章概要\n" + ("正文句子。" * 200)
    assert purity.chapter_body(t).strip()


# ---------------------------------------------------------------- 导出 / 预览 / 下载

def _book(tmp_path, monkeypatch, extra_files=()):
    pdir = tmp_path / KEY
    pdir.mkdir()
    (pdir / "第003章.md").write_text(POLLUTED, encoding="utf-8")
    (pdir / "01-大纲.md").write_text("# 大纲\n\n只应有规划，不应被下载", encoding="utf-8")
    for name, body in extra_files:
        (pdir / name).write_text(body, encoding="utf-8")
    json.dump({"novelName": "测试书", "status": "completed",
               "chapters": [{"chapterNumber": 1, "title": "开端", "status": "completed",
                             "wordCount": 12, "filePath": "第001章.md"},
                            {"chapterNumber": 2, "title": "承接", "status": "pending",
                             "wordCount": 0, "filePath": "第002章.md"},
                            {"chapterNumber": 3, "title": "副驾驶", "status": "completed",
                             "wordCount": 12, "filePath": "第003章.md"}],
               "meta": {}, "config": {}},
              open(pdir / "02-写作计划.json", "w", encoding="utf-8"), ensure_ascii=False)
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))
    appmod.PROJECT_INDEX.clear()
    return pdir


def test_export_markdown_is_clean_and_single_title(tmp_path, monkeypatch):
    _book(tmp_path, monkeypatch)
    md = export_mod.markdown(appmod._get_project(KEY))
    assert "他关掉收音机。" in md
    for junk in ("本章概要", "章节备注", "修改说明", "审核评分", "综合 86 分"):
        assert junk not in md
    # 章题只出现一次（此前 plan 标题 + 文件里的 `# 第003章` 会双题头）
    assert md.count("第3章") + md.count("第003章") <= 2
    assert "副驾驶" in md


def test_export_http_formats_clean(tmp_path, monkeypatch):
    _book(tmp_path, monkeypatch)
    c = TestClient(appmod.app)
    for fmt in ("md", "print"):
        r = c.get(f"/api/export?key={KEY}&fmt={fmt}")
        assert r.status_code == 200, fmt
        text = r.text
        assert "他关掉收音机。" in text and "修改说明" not in text and "本章概要" not in text


def test_preview_returns_clean_text(tmp_path, monkeypatch):
    _book(tmp_path, monkeypatch)
    c = TestClient(appmod.app)
    data = c.get(f"/api/preview?key={KEY}").json()
    assert "他关掉收音机。" in data["text"] and "修改说明" not in data["text"]
    assert data["chapters_full"], "逐章原文不能为空"
    for ch in data["chapters_full"]:
        assert "修改说明" not in ch["text"] and "本章概要" not in ch["text"]


def test_preview_ignores_stale_book_file(tmp_path, monkeypatch):
    """成书文件即使被旧版逻辑写脏，预览也不许把它端出去。"""
    _book(tmp_path, monkeypatch, extra_files=[("成书-测试书.md", "# 测试书\n\n## 本章概要\n脏内容\n\n## 修改说明\n改了什么")])
    c = TestClient(appmod.app)
    data = c.get(f"/api/preview?key={KEY}").json()
    assert "脏内容" not in data["text"] and "修改说明" not in data["text"]


def test_download_builds_from_chapters_not_outline(tmp_path, monkeypatch):
    _book(tmp_path, monkeypatch)
    c = TestClient(appmod.app)
    r = c.get(f"/api/download?key={KEY}")
    assert r.status_code == 200
    assert "他关掉收音机。" in r.text
    assert "只应有规划" not in r.text          # 旧实现会回落成 01-大纲.md
    assert "修改说明" not in r.text


def test_chapter_api_clean_text(tmp_path, monkeypatch):
    _book(tmp_path, monkeypatch)
    c = TestClient(appmod.app)
    data = c.get(f"/api/chapter?key={KEY}&n=3").json()
    assert "修改说明" not in data["cleanText"] and "本章概要" not in data["cleanText"]
    assert "他关掉收音机。" in data["cleanText"]


def test_chat_history_cleans_legacy_chapter_card(tmp_path, monkeypatch):
    """旧留档里的污染章节卡：读历史时净化（前端「复制本章」才拿不到脏内容）。"""
    pdir = _book(tmp_path, monkeypatch)
    rec = {"role": "assistant", "content": POLLUTED, "ts": 1700000000,
           "type": "chapter", "no": 3, "title": "副驾驶", "words": 12, "quality": 86}
    (pdir / "chat_log.jsonl").write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    c = TestClient(appmod.app)
    items = c.get(f"/api/chat/history?key={KEY}").json()["items"]
    chapters = [x for x in items if x.get("type") == "chapter"]
    assert chapters
    for x in chapters:
        assert "修改说明" not in x["content"] and "本章概要" not in x["content"]
        assert "他关掉收音机。" in x["content"]


# ---------------------------------------------------------------- 旧数据自愈

def test_purify_data_tool_dry_run_never_writes(tmp_path):
    """体检模式必须只读：跑一遍后文件字节不变（防止误删用户原稿）。"""
    ch = tmp_path / "测试项目" / "章节"
    ch.mkdir(parents=True)
    f = ch / "第01章-旧稿.md"
    f.write_text(POLLUTED, encoding="utf-8")
    before = f.read_bytes()

    tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tools", "purify_data.py")
    res = subprocess.run([sys.executable, tool, "--root", str(tmp_path)],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert "其中需净化：1" in res.stdout
    assert f.read_bytes() == before, "体检模式改动了文件"


def test_purify_data_tool_apply_cleans_and_backs_up(tmp_path):
    """写回后才去掉元信息小节，且原稿留在 .purify-backup/ 里。"""
    ch = tmp_path / "测试项目" / "章节"
    ch.mkdir(parents=True)
    f = ch / "第01章-旧稿.md"
    f.write_text(POLLUTED, encoding="utf-8")
    before = f.read_text(encoding="utf-8")

    tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tools", "purify_data.py")
    res = subprocess.run([sys.executable, tool, "--root", str(tmp_path), "--apply"],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr

    after = f.read_text(encoding="utf-8")
    assert "修改说明" not in after and "审核评分" not in after
    assert "## 章节备注" in after and "他关掉收音机。" in after  # 四段结构保留
    assert purity.chapter_text(after) == purity.chapter_text(before)  # 取文口径完全一致

    baks = list((tmp_path / "测试项目" / ".purify-backup").rglob("第01章-旧稿.md"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf-8") == before


# ---------------------------------------------------------------- 前后端口径一致

@pytest.mark.skipif(not shutil.which("node"), reason="需要 node 校验前后端清洗口径一致")
def test_frontend_purity_matches_backend(tmp_path):
    cases = {
        "polluted": POLLUTED,
        "no_notes": "# 第03章 X\n\n## 章首引子\n雨。\n\n## 正文\n他关掉收音机。\n\n## 修改说明\n加强细节。",
        "tail": "# 第03章 X\n\n他关掉收音机。\n\n本章评分 86 分。",
        "plain": "他关掉收音机。\n\n雨停了。",
        # v4.22.2：章题之前甩改写说明 + 整篇包在 ```markdown 里（第90章真实形态）
        "lead_preamble": (
            "这是一份针对《第09章 黑纹共振》的最小改写。\n**主要修改逻辑：**\n"
            "1.  **去重高频词**：将“没有”替换为“缺失”。\n2.  去除电报体。\n\n"
            "```markdown\n# 第09章 黑纹共振\n\n## 章首引子\n雨是从傍晚开始下的。\n\n"
            "## 正文\n他关掉收音机。\n```"
        ),
        "lead_only": "这是一份针对《第09章 X》的最小改写。\n\n## 正文\n他关掉收音机。",
        # v4.22.3：正文契约标记——框内是正文，框外一律不算
        "marked": (
            "## 本章概要\n周望载到红雨衣。\n\n"
            "=== 一幕·YIMU 神笔工作台·开篇 ===\n## 章首引子\n\n雨是从傍晚开始下的。\n\n"
            "## 正文\n\n他关掉收音机。\n\n=== 一幕·YIMU 神笔工作台·结尾 ===\n\n"
            "## 章节备注\n下章回收车票。\n\n## 修改说明\n加强环境描写。综合评分 86 分。"
        ),
        "marked_bare": ("一幕·YIMU·开篇\n## 正文\n他关掉收音机。\n"
                        "一幕·YIMU·结尾\n框外多写的正文。"),
    }
    js = """
import { cleanChapterText } from %s;
const cases = JSON.parse(process.argv[1]);
const out = {};
for (const [k, v] of Object.entries(cases)) out[k] = cleanChapterText(v);
console.log(JSON.stringify(out));
""" % json.dumps(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "static", "v2", "js", "core", "purity.js"))
    res = subprocess.run(["node", "--input-type=module", "-e", js, json.dumps(cases, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    js_out = json.loads(res.stdout.strip().splitlines()[-1])
    for k, v in cases.items():
        assert js_out[k] == purity.chapter_text(v), f"前后端口径不一致：{k}"
