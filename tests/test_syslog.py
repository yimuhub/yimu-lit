# -*- coding: utf-8 -*-
"""v4.19.0：系统级操作日志（bot/syslog.py）回归。

覆盖：追加写入 / 敏感字段脱敏 / kind 过滤与关键词检索 / 裁剪 / 清空。
日志文件固定写到应用根 data/system_log.jsonl，测试通过临时改写 LOG_FILE 隔离，
不污染真实日志。
"""

import json

from bot import syslog


def _use_tmp_log(tmp_path, monkeypatch):
    fp = tmp_path / "system_log.jsonl"
    monkeypatch.setattr(syslog, "LOG_FILE", str(fp))
    return fp


def test_log_event_appends_jsonl(tmp_path, monkeypatch):
    fp = _use_tmp_log(tmp_path, monkeypatch)
    syslog.log_event("settings", "修改设置", {"quality_mode": {"旧值": "off", "新值": "auto"}}, "修改设置：quality_mode")
    syslog.log_event("provider", "删除供应商", {"id": "p1"}, "删除供应商「p1」")
    lines = fp.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    e = json.loads(lines[0])
    assert e["kind"] == "settings"
    assert e["action"] == "修改设置"
    assert e["detail"]["quality_mode"]["新值"] == "auto"


def test_sensitive_keys_masked(tmp_path, monkeypatch):
    _use_tmp_log(tmp_path, monkeypatch)
    syslog.log_event("provider", "添加供应商", {"id": "p1", "api_key": "sk-should-not-leak", "nested": {"token": "abc"}}, "")
    raw = syslog.LOG_FILE and open(syslog.LOG_FILE, encoding="utf-8").read()
    assert "sk-should-not-leak" not in raw
    assert "abc" not in raw
    assert raw.count("***") == 2


def test_tail_filters_by_kind_and_query(tmp_path, monkeypatch):
    _use_tmp_log(tmp_path, monkeypatch)
    syslog.log_event("system", "服务启动", {"version": "x"}, "服务启动")
    syslog.log_event("settings", "修改设置", {"k": 1}, "修改设置：k")
    syslog.log_event("skill", "添加技能", {"name": "s1"}, "添加自定义技能「s1」")
    kinds = [e["kind"] for e in syslog.tail(limit=10)]
    assert kinds == ["skill", "settings", "system"]   # 新→旧
    only_settings = syslog.tail(limit=10, kind="settings")
    assert len(only_settings) == 1 and only_settings[0]["summary"] == "修改设置：k"
    hit = syslog.tail(limit=10, q="s1")
    assert len(hit) == 1 and hit[0]["kind"] == "skill"
    miss = syslog.tail(limit=10, q="不存在的关键词xyz")
    assert miss == []


def test_clear_and_missing_file(tmp_path, monkeypatch):
    _use_tmp_log(tmp_path, monkeypatch)
    assert syslog.tail() == [] and syslog.count() == 0   # 文件不存在也不抛错
    syslog.log_event("system", "清空日志", None, "清空系统操作日志")
    assert syslog.count() == 1
    syslog.clear()
    assert syslog.count() == 0


def test_trim_keeps_latest(tmp_path, monkeypatch):
    fp = _use_tmp_log(tmp_path, monkeypatch)
    monkeypatch.setattr(syslog, "MAX_BYTES", 10)   # 强制每次写都触发裁剪
    monkeypatch.setattr(syslog, "KEEP", 3)
    for i in range(6):
        syslog.log_event("system", f"事件{i}", None, f"事件{i}")
    entries = syslog.tail(limit=10)
    assert len(entries) == 3
    assert entries[0]["summary"] == "事件5" and entries[-1]["summary"] == "事件3"
