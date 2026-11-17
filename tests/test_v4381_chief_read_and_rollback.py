"""v4.38.1：跨作品读取聊天 / 终审顺序 / 改写必回滚。"""
import json
import os

from bot import chief, final_review, writer


def _mkproj(root, key, name, msgs):
    pdir = os.path.join(root, key)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "02-写作计划.json"), "w", encoding="utf-8") as f:
        json.dump({"novelName": name, "chapters": []}, f, ensure_ascii=False)
    with open(os.path.join(pdir, "chat_log.jsonl"), "w", encoding="utf-8") as f:
        for role, content in msgs:
            f.write(json.dumps({"role": role, "content": content, "ts": 1794888787},
                               ensure_ascii=False) + "\n")
    return pdir


def test_read_last_chat_finds_other_project(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    _mkproj(str(root), "20261113-204655-民国神棍：我在桥头炸出工业革命",
            "民国神棍：我在桥头炸出工业革命",
            [("user", "只改写字数少于 3000 字的章节"), ("assistant", "已把这条要求并入创作。")])
    _mkproj(str(root), "20261117-100000-另一本书", "另一本书", [("user", "你好")])
    monkeypatch.setattr(chief, "DATA_ROOT", str(tmp_path))
    out = chief.read_last_chat("民国神棍", limit=4, current_key="20261117-100000-另一本书",
                               current_pdir=str(root / "20261117-100000-另一本书"))
    assert "民国神棍" in out
    assert "只改写字数少于 3000 字的章节" in out
    assert "已把这条要求并入创作" in out


def test_read_last_chat_missing_is_honest(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    _mkproj(str(root), "20261117-100000-一本书", "一本书", [("user", "你好")])
    monkeypatch.setattr(chief, "DATA_ROOT", str(tmp_path))
    out = chief.read_last_chat("根本不存在的书", current_key="20261117-100000-一本书",
                               current_pdir=str(root / "20261117-100000-一本书"))
    assert "没找到" in out
    assert "一本书" in out


class _Proj:
    def __init__(self, path, plan):
        self.path = path
        self._plan = plan
        self.logs = []

    def plan(self):
        return self._plan

    def save_plan(self, plan):
        self._plan = plan

    def log(self, msg, *_a, **_k):
        self.logs.append(msg)


def test_score_pass_scores_all_before_rewriting(tmp_path, monkeypatch):
    pdir = str(tmp_path)
    with open(os.path.join(pdir, "01.md"), "w", encoding="utf-8") as f:
        f.write("# 第1章\n\n正文一。\n")
    with open(os.path.join(pdir, "02.md"), "w", encoding="utf-8") as f:
        f.write("# 第2章\n\n正文二。\n")
    plan = {"chapters": [{"chapterNumber": 1, "filePath": "01.md"},
                         {"chapterNumber": 2, "filePath": "02.md"}]}
    proj = _Proj(pdir, plan)
    calls = []

    def fake_score(_proj, no):
        calls.append(("score", no))
        return 50 if no == 1 else 70

    def fake_rewrite(_proj, no, **_kw):
        calls.append(("rewrite", no))
        return 65

    monkeypatch.setattr(writer, "score_chapter", fake_score)
    monkeypatch.setattr(writer, "_auto_rewrite_chapter", fake_rewrite)
    monkeypatch.setattr(final_review, "_emit", lambda *a, **k: None)
    res = final_review._score_pass(proj, plan, dict(final_review.POLICY), [], "k")
    assert res["immediate"] == [1]
    assert res["deferred"] == [2]
    # 顺序：两章都评完，才允许对第 1 章动手
    assert calls.index(("rewrite", 1)) > calls.index(("score", 2))


def test_pending_rewrite_recovered(tmp_path):
    pdir = str(tmp_path)
    chap = os.path.join(pdir, "01.md")
    orig = "# 第1章\n\n这是原稿正文。\n"
    cand = "# 第1章\n\n这是被改坏的稿子。\n"
    with open(chap, "w", encoding="utf-8") as f:
        f.write(cand)
    plan = {"chapters": [{"chapterNumber": 1, "filePath": "01.md"}]}
    proj = _Proj(pdir, plan)
    with open(os.path.join(pdir, writer._RW_GUARD_FILE), "w", encoding="utf-8") as f:
        json.dump({"1": {"orig": orig, "sha": writer._sha_text(cand), "state": "scoring"}}, f,
                  ensure_ascii=False)
    n = writer.recover_pending_rewrites(proj, 1)
    assert n == 1
    assert open(chap, encoding="utf-8").read() == orig
    assert not os.path.exists(os.path.join(pdir, writer._RW_GUARD_FILE))
