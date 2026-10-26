"""v2.3.0 引擎侧写作钩子测试：里程碑标题回流 / 用户指令计入学习样本。

tmp 项目夹具照抄 test_longform / test_writer_integration 的构造风格；LLM 由 conftest
统一 stub，需要合法 JSON 时局部覆盖。所有断言跑独立可测函数，绝不在测试里跑真写作循环。
"""
import json
import os

from bot import learn, llm, writer


def _make_proj(tmp_path, done, milestone=None, total=30, seed_titles=None):
    """构造一个含 done 章已完成的项目目录 + 02-写作计划.json（可直接喂 _maybe_backflow_titles）。"""
    pdir = tmp_path / "book"
    pdir.mkdir(exist_ok=True)
    chapters = []
    for i in range(1, total + 1):
        chapters.append({
            "chapterNumber": i, "title": f"第{i}章", "filePath": f"第{i:02d}章.md",
            "status": "completed" if i <= done else "pending",
            "wordCount": 3200 if i <= done else None,
            "wordCountPass": True if i <= done else None, "retryCount": 0,
        })
    meta = {}
    if milestone is not None:
        meta["title_milestone"] = milestone
    plan = {
        "version": 1, "novelName": "回流测试", "totalChapters": total,
        "minWordsPerChapter": 20, "status": "in_progress", "writingMode": "serial",
        "config": {"genre": "都市", "idea": "小人物逆袭", "chapters": total},
        "titles": [], "chapters": chapters, "meta": meta,
    }
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (pdir / "01-大纲.md").write_text("# 大纲\n\n第1章 开局\n", encoding="utf-8")
    if seed_titles is not None:
        (pdir / "05-标题候选.json").write_text(json.dumps(seed_titles, ensure_ascii=False), encoding="utf-8")
    return writer.Project(str(pdir), "book")


def _titles_payload(*names):
    return {"titles": [{"title": n, "technique": "悬念型", "why": f"{n}的推荐理由"} for n in names]}


def _read_candidates(proj):
    fp = os.path.join(proj.path, "05-标题候选.json")
    return json.loads(open(fp, encoding="utf-8").read())


# ---------- 任务一：里程碑标题回流 ----------

def test_milestone_backflow_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: _titles_payload("逆流", "破晓之城", "长夜将明"))
    proj = _make_proj(tmp_path, done=10)
    added = writer._maybe_backflow_titles(proj)
    assert added == 3
    doc = _read_candidates(proj)
    # 契约格式：顶层 updated_at + items；每条 title/technique/why/at
    assert set(doc.keys()) >= {"updated_at", "items"}
    assert isinstance(doc["updated_at"], str) and doc["updated_at"]
    assert len(doc["items"]) == 3
    for it in doc["items"]:
        assert set(it.keys()) == {"title", "technique", "why", "at"}
        assert it["title"] in ("逆流", "破晓之城", "长夜将明")
    # meta 记录上次回流里程碑
    assert proj.plan()["meta"]["title_milestone"] == 10


def test_same_milestone_not_repeated(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: _titles_payload("逆流", "破晓之城", "长夜将明"))
    proj = _make_proj(tmp_path, done=12)
    assert writer._maybe_backflow_titles(proj) == 3
    # 再次调用：done 仍落在同一个 10 里程碑（12//10*10==10），不重复追加
    assert writer._maybe_backflow_titles(proj) is None
    assert len(_read_candidates(proj)["items"]) == 3


def test_triggers_again_at_20(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: _titles_payload("A书", "B书", "C书"))
    proj = _make_proj(tmp_path, done=20, milestone=10)
    added = writer._maybe_backflow_titles(proj)
    assert added == 3
    assert proj.plan()["meta"]["title_milestone"] == 20
    assert len(_read_candidates(proj)["items"]) == 3


def test_dedup_by_title(tmp_path, monkeypatch):
    seed = {"updated_at": "2026-01-01T00:00:00",
            "items": [{"title": "逆流", "technique": "旧", "why": "旧", "at": "2026-01-01T00:00:00"}]}
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: _titles_payload("逆流", "新星", "曙光"))
    proj = _make_proj(tmp_path, done=10, seed_titles=seed)
    added = writer._maybe_backflow_titles(proj)
    assert added == 2  # 「逆流」已存在，只追加新星/曙光
    titles = [it["title"] for it in _read_candidates(proj)["items"]]
    assert titles.count("逆流") == 1
    assert set(titles) == {"逆流", "新星", "曙光"}


def test_cap_at_20(tmp_path, monkeypatch):
    seed_items = [{"title": f"旧题{i}", "technique": "t", "why": "w", "at": "2026-01-01T00:00:00"}
                  for i in range(25)]
    seed = {"updated_at": "2026-01-01T00:00:00", "items": seed_items}
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: _titles_payload("新甲", "新乙", "新丙"))
    proj = _make_proj(tmp_path, done=10, seed_titles=seed)
    writer._maybe_backflow_titles(proj)
    doc = _read_candidates(proj)
    assert len(doc["items"]) == 20  # 封顶 20，保留最新
    titles = {it["title"] for it in doc["items"]}
    assert {"新甲", "新乙", "新丙"} <= titles


def test_backflow_never_raises_on_llm_error(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("网关炸了")
    monkeypatch.setattr(llm, "chat_json", _boom)
    proj = _make_proj(tmp_path, done=10)
    # 生成异常被吞掉：返回 None，不抛出，且已把里程碑推进（不阻断写作）
    assert writer._maybe_backflow_titles(proj) is None


# ---------- 任务三：queue_msg 计入学习样本 ----------

def _patch_projects(monkeypatch, tmp_path):
    monkeypatch.setattr(writer, "PROJECTS_DIR", str(tmp_path))


def _make_disk_project(tmp_path, key):
    pdir = tmp_path / key
    pdir.mkdir(exist_ok=True)
    plan = {"version": 1, "novelName": key, "totalChapters": 1, "status": "in_progress",
            "config": {"genre": "都市"}, "chapters": [
                {"chapterNumber": 1, "title": "第1章", "filePath": "第01章.md", "status": "completed"}],
            "meta": {}}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return key


def test_queue_msg_records_learn(tmp_path, monkeypatch):
    _patch_projects(monkeypatch, tmp_path)
    key = _make_disk_project(tmp_path, "kb1")
    calls = []
    monkeypatch.setattr(learn, "note_user_directive", lambda k, t: calls.append((k, t)))
    assert writer.queue_msg(key, "把节奏加快，多打脸") is True
    assert calls == [(key, "把节奏加快，多打脸")]
    # 指令仍正常入队
    assert len(writer.pending_queue(key)) == 1


def test_queue_msg_survives_learn_error(tmp_path, monkeypatch):
    _patch_projects(monkeypatch, tmp_path)
    key = _make_disk_project(tmp_path, "kb2")

    def _raise(k, t):
        raise RuntimeError("学习模块炸了")
    monkeypatch.setattr(learn, "note_user_directive", _raise)
    # 记录失败被吞掉：入队不受影响
    assert writer.queue_msg(key, "换个钩子") is True
    assert len(writer.pending_queue(key)) == 1


def test_note_user_directive_is_exception_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(learn, "_DATA", str(tmp_path))
    monkeypatch.setattr(learn, "_P", str(tmp_path / "learn_profiles.json"))

    def _boom(*a, **k):
        raise RuntimeError("写盘炸了")
    monkeypatch.setattr(learn, "_save", _boom)
    # 内部异常被吞，返回 {} 而非抛出
    assert learn.note_user_directive("kb3", "测试") == {}


def test_note_user_directive_persists_and_surfaces(tmp_path, monkeypatch):
    monkeypatch.setattr(learn, "_DATA", str(tmp_path))
    monkeypatch.setattr(learn, "_P", str(tmp_path / "learn_profiles.json"))
    learn.note_user_directive("kb4", "喜欢短句")
    learn.note_user_directive("kb4", "别注水")
    prof = learn.profile()
    assert prof["directives_total"] == 2
    assert "喜欢短句" in prof["recent_directives"]
    assert "别注水" in prof["recent_directives"]
