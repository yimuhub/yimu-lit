"""v4.41.4 交付口径回归：
1. 整书导出只拦硬伤（N03 半句截断），格式残留只记录；
2. 截断章补全必须「只补结尾、不整章重写」，且完整性优先于评分；
3. 模型连接失败立刻中止，不再无意义地一轮轮重试。
"""
import json
import os

from bot import delivery_gate as gate, writer


def _make_project(tmp_path):
    pdir = tmp_path / "book"
    pdir.mkdir()
    (pdir / "第001章.md").write_text("林野推开木门，风从石阶上掠过，他", encoding="utf-8")
    plan = {"novelName": "测试书", "status": "completed",
            "chapters": [{"chapterNumber": 1, "title": "开端", "status": "completed",
                          "filePath": "第001章.md", "wordCount": 200}],
            "meta": {}, "config": {"validation": {"pass_line": 80}}}
    (pdir / "02-写作计划.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return writer.Project(str(pdir), "book")


def test_hard_only_gate_blocks_truncation_not_format_noise():
    rep = gate.check_delivery(
        [{"n": 1, "text": "林野听见 Host 的声音，握紧了拳头，他"}], kind="novel")
    ok, msg = gate.export_ready(rep, enforcement="enforcing", block_codes=("N03",))
    assert ok is False and "N03" in msg
    # 完整句 + 英文残留：只记录，不阻断
    rep2 = gate.check_delivery(
        [{"n": 1, "text": "林野听见 Host 的声音，握紧了拳头。"}], kind="novel")
    ok2, _ = gate.export_ready(rep2, enforcement="enforcing", block_codes=("N03",))
    assert ok2 is True


def test_finish_truncated_chapter_appends_only_tail(monkeypatch, tmp_path):
    proj = _make_project(tmp_path)
    before = open(os.path.join(proj.path, "第001章.md"), encoding="utf-8").read()
    cont = ("回头看了一眼门里的灯，轻轻把门带上。" +
            "石阶上的风还没停，他知道今晚必须把那份协议送到。" * 8)
    monkeypatch.setattr(writer, "_build_ctx", lambda proj, ch: {})
    monkeypatch.setattr(writer.workflow, "continue_chapter",
                        lambda body, ctx, need_words=1200: cont)
    r = writer.finish_truncated_chapter(proj, 1, max_rounds=1)
    after = open(os.path.join(proj.path, "第001章.md"), encoding="utf-8").read()
    assert r["kept"] is True
    assert after.startswith(before.rstrip()), "必须保留原章开头，只允许在结尾续写"
    assert writer._ends_clean(after)
    assert len(after) > len(before) + 80
    assert "回头看了一眼门里的灯" in after


def test_finish_truncated_chapter_aborts_on_provider_error(monkeypatch, tmp_path):
    proj = _make_project(tmp_path)
    before = open(os.path.join(proj.path, "第001章.md"), encoding="utf-8").read()

    def _boom(*a, **k):
        raise RuntimeError("API 密钥无效或未配置（HTTP 401）")

    monkeypatch.setattr(writer, "_build_ctx", lambda proj, ch: {})
    monkeypatch.setattr(writer.workflow, "continue_chapter", _boom)
    r = writer.finish_truncated_chapter(proj, 1, max_rounds=2)
    assert r["kept"] is False and r.get("abort") is True
    assert "401" in r["note"]
    assert open(os.path.join(proj.path, "第001章.md"), encoding="utf-8").read() == before
