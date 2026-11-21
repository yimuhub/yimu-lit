"""章节版本快照：编辑/重写前留底，可按版本对比与回滚。

存储：<书>/.versions/chNNN/<时间戳>-<hash8>[-<tag>].md；全部快照最多保留 _KEEP 份，
其中自动保存快照（tag=auto）单独再限 _AUTO_KEEP 份，避免边写边存把历史全部冲掉。
tag 进文件名不进内容：list_versions 按 tag 还原中文 label（自动保存/恢复前…），
旧文件没有 tag，label 回落空串，前端显示 id，行为与 v5.1.0 之前一致。
只做本地文件，不依赖数据库；所有路径都经过 safe_join。
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import os
import re
import time

from .safeio import atomic_write_text, safe_join

_KEEP = 30
_AUTO_KEEP = 20          # v5.1.0：自动保存类快照每章上限（超出删最旧的 auto）
_VID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_TAG_RE = re.compile(r"^(?P<base>.+?)-(auto|restore)$")
# 文件名 tag → 对外中文 label（bench 版本列表直接显示）
_TAG_LABELS = {"auto": "自动保存", "restore": "恢复前"}

# v5.1.0 节流参数：距上版 ≥5 分钟 或 字数变化 ≥200 才落新快照；首版 ≥50 字。
_AUTO_MIN_GAP = 300.0    # 秒
_AUTO_MIN_DELTA = 200    # 字符
_AUTO_MIN_FIRST = 50     # 字符


def _chapter_dir(proj_path: str, num: int) -> str:
    num = int(num)
    if num < 1 or num > 100000:
        raise ValueError("章节号不合法")
    d = os.path.join(proj_path, ".versions", f"ch{num:03d}")
    os.makedirs(d, exist_ok=True)
    return d


def _parse_label(vid: str) -> str:
    """从版本 id 尾部 tag 还原中文 label（旧快照无 tag → 空串）。"""
    m = _TAG_RE.match(vid or "")
    return _TAG_LABELS.get(m.group(2), "") if m else ""


def _list_raw(proj_path: str, num: int) -> list[dict]:
    d = _chapter_dir(proj_path, num)
    out = []
    for name in os.listdir(d):
        if not name.endswith(".md"):
            continue
        fp = os.path.join(d, name)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        vid = name[:-3]
        ts = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        out.append({"id": vid, "ts": ts, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def list_versions(proj_path: str, num: int) -> list[dict]:
    return [{"id": v["id"], "ts": v["ts"], "size": v["size"], "label": _parse_label(v["id"])}
            for v in _list_raw(proj_path, num)]


def _latest_text(proj_path: str, num: int) -> str:
    raw = _list_raw(proj_path, num)
    if not raw:
        return ""
    try:
        return open(os.path.join(_chapter_dir(proj_path, num), raw[0]["id"] + ".md"),
                    encoding="utf-8").read()
    except OSError:
        return ""


def snapshot(proj_path: str, num: int, text: str, label: str = "", tag: str = "",
             force: bool = False) -> dict | None:
    """内容有变化才落一份快照（相同内容不重复占空间；force=True 跳过该检查，
    供「恢复前」快照使用——当前稿可能与最新快照恰好相同，但恢复动作本身要留痕）。

    tag：可选的文件名后缀（auto=自动保存 / restore=恢复前），用于按类别限份数
    与在前端版本列表还原中文 label；不给 tag 的旧式快照（编辑前/改稿前…）行为不变。
    """
    if not text or not text.strip():
        return None
    if not force and _latest_text(proj_path, num) == text:
        return None
    d = _chapter_dir(proj_path, num)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    vid = f"{stamp}-{digest}-{tag}" if tag else f"{stamp}-{digest}"
    atomic_write_text(os.path.join(d, vid + ".md"), text)
    raw = _list_raw(proj_path, num)
    for old in raw[_KEEP:]:
        try:
            os.remove(os.path.join(d, old["id"] + ".md"))
        except OSError:
            pass
    _prune_tag(d, raw, tag, _AUTO_KEEP)
    return {"id": vid, "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "size": len(text.encode("utf-8")), "label": label or _TAG_LABELS.get(tag, "")}


def _prune_tag(d: str, raw: list[dict], tag: str, keep: int) -> None:
    """按 tag 限份数：同名 tag 的快照最多 keep 份（超出删最旧），不动其他类别。"""
    if not tag or keep <= 0:
        return
    same = [v for v in raw if v["id"].endswith("-" + tag)]
    for old in same[keep:]:
        try:
            os.remove(os.path.join(d, old["id"] + ".md"))
        except OSError:
            pass


def throttled_snapshot(proj_path: str, num: int, text: str, label: str = "自动保存") -> dict | None:
    """v5.1.0 边写边存的节流快照：不是每次保存都留档，按三条规则收敛——

    1) 首版 ≥50 字：太短的起手稿不值得占一个版本位；
    2) 距上一版 ≥5 分钟，或 与上一版字数差 ≥200：密集手打/小幅删改不落盘；
    3) auto 类快照每章上限 20 版（超出删最旧）。

    与 snapshot() 相同的幂等约定：内容与最新一版完全一致时不落。
    返回落盘的版本 dict；未达条件或内容未变返回 None（调用方无须区分）。
    """
    if not text or not text.strip():
        return None
    raw = _list_raw(proj_path, num)
    # 节流基准是「上一版自动快照」：编辑前/改稿前等人工快照每次保存都会刷新整表 mtime，
    # 若拿全部快照算间隔，≥5 分钟这条路径在写作会话里永远走不到。
    autos = [v for v in raw if v["id"].endswith("-auto")]
    if autos:
        try:
            newest_fp = os.path.join(_chapter_dir(proj_path, num), autos[0]["id"] + ".md")
            gap = time.time() - os.path.getmtime(newest_fp)
        except OSError:
            gap = _AUTO_MIN_GAP          # 读不到 mtime → 放行，宁多勿丢
        try:
            prev_len = len(read_version(proj_path, num, autos[0]["id"]))
        except Exception:
            prev_len = 0
        if gap < _AUTO_MIN_GAP and abs(len(text) - prev_len) < _AUTO_MIN_DELTA:
            return None
    elif len(text) < _AUTO_MIN_FIRST:
        return None
    return snapshot(proj_path, num, text, label=label, tag="auto")


def restore_version(proj_path: str, num: int, vid: str, cur_text: str) -> str:
    """v5.1.0 恢复准备：先把当前稿存为「恢复前」快照（可再滚回来），返回目标版本文本。

    · 目标版本不存在/vid 非法直接抛错——调用方拿到的文本必定可写盘；
    · 当前稿与恢复目标一致（重复恢复同一版）→ 无覆盖风险，不落重复快照；
    · 当前稿为空 → 没有恢复价值，不落；
    · 其余情况 force 落「恢复前」快照：哪怕当前稿恰好等于最新一版
      （自动保存刚存过），恢复动作本身也要留痕，前端才能看到「恢复前」入口。
    """
    text = read_version(proj_path, num, vid)   # 先校验目标，再动当前稿
    if cur_text == text:
        return text
    if (cur_text or "").strip():
        snapshot(proj_path, num, cur_text, label="恢复前", tag="restore", force=True)
    return text


def read_version(proj_path: str, num: int, vid: str) -> str:
    if not _VID.match(vid or ""):
        raise ValueError("版本号不合法")
    fp = safe_join(_chapter_dir(proj_path, num), vid + ".md", label="版本号")
    try:
        return open(fp, encoding="utf-8").read()
    except FileNotFoundError as e:
        raise ValueError("版本不存在") from e


def diff_text(old: str, new: str, old_label: str = "历史版本", new_label: str = "当前") -> str:
    lines = list(difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(),
        fromfile=old_label, tofile=new_label, lineterm="", n=3))
    if not lines:
        return "（内容一致）"
    return "\n".join(lines)[:200000]
