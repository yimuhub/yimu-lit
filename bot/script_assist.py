"""v2.2.0 P5：剧本「场 / 段级定向改写」引擎（现已并入通用副驾 bot/assist.py）。

工作台对话框里的指令（「把第 3 场第 2 段改得更紧张」「第二场第四段删掉最后一句」）
只会命中目标段：生成阶段产出 patch（old / new / base_hash），前端先出 diff，
用户点「应用」后才落盘；落盘时校验 base_hash 防并发覆盖，并把旧稿快照到
``04-正文/.versions/``，可逐版本回退。

落盘范围自证：写完后重新分块，除目标索引外每一块的 hash 必须与写前一致；
有一处不等就恢复旧全文并报「改写范围越界，已回退」。

v2.2.1 起，剧本/诗词/散文共用同一套引擎与留档（见 bot/assist.py），
本模块保留剧本侧的旧接口，作为兼容层对外提供。
"""
from __future__ import annotations

import re

from . import assist as _assist
from . import llm, script_writer  # noqa: F401  （llm 供测试 monkeypatch）

# 兼容旧调用方：版本目录与文件名规则仍是剧本口径
VERSIONS_DIR = _assist.VERSIONS_DIR
VERSION_RE = re.compile(r"^第(\d{3})场-\d{8}-\d{6}\.md$")

split_blocks = _assist.split_blocks
block_hash = _assist.block_hash
_join_blocks = _assist._join_blocks


def parse_target(text: str) -> dict:
    """从自然语言里抽场号/段号；没有的维度返回 None。"""
    got = _assist.parse_target(text, "script")
    return {"scene": got["doc"], "block": got["block"]}


def _scene_no(value):
    """剧本场号对外一律是 int（旧接口口径）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def assist(key: str, message: str, focus: dict | None = None,
           history: list | None = None, cfg: dict | None = None) -> dict:
    """对话框入口：定位目标 → 讨论 / 追问 / 生成段级 patch（不写盘）。"""
    focus = dict(focus or {}) if isinstance(focus, dict) else {}
    if focus.get("doc") is None and focus.get("scene") is not None:
        focus["doc"] = str(focus.get("scene"))
    res = _assist.assist(key, "script", message, focus, history, cfg)
    intent = res.get("intent") or {}
    patch = res.get("patch")
    out = {
        "ok": res.get("ok"),
        "reply": res.get("reply"),
        "intent": {"kind": intent.get("kind"), "scene": _scene_no(intent.get("doc")),
                   "block": intent.get("block")},
        "patch": None,
    }
    if patch:
        out["patch"] = {"scene": _scene_no(patch.get("doc")), "block": patch.get("block"),
                        "base_hash": patch.get("base_hash"), "old": patch.get("old"),
                        "new": patch.get("new"), "scene_title": patch.get("doc_title")}
    return out


def apply_edit(key: str, scene: int, block: int, base_hash: str, new_text: str) -> dict:
    """把 patch 写回单场：hash 校验 → 快照 → 原子写 → 范围自证 → 更新计划。"""
    res = _assist.apply_edit(key, "script", scene, block, base_hash, new_text)
    if not res.get("ok"):
        return res
    return {"ok": True, "text": res.get("text"), "scene": _scene_no(res.get("doc")),
            "block": res.get("block"), "wordCount": res.get("wordCount"),
            "version": res.get("version")}


def list_versions(key: str, scene: int) -> dict:
    res = _assist.list_versions(key, "script", scene)
    if not res.get("ok"):
        return res
    return {"ok": True, "items": res.get("items") or []}


def restore_version(key: str, scene: int, version: str) -> dict:
    res = _assist.restore_version(key, "script", scene, version)
    if not res.get("ok"):
        return res
    return {"ok": True, "text": res.get("text"), "wordCount": res.get("wordCount"),
            "version": res.get("version")}
