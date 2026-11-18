"""全书健康流水线：体检 → 还原完整旧稿 → 单章写盘门禁。

这是「书稿质量」的唯一系统出口，任何一本书、任何一条改稿路径都走这里：

1. `audit()`      只读体检：模型说明文字 / 半句截断 / 字数不足 / 文件缺失；
2. `repair()`     把坏稿、半句稿从 `.versions` 还原成完整旧稿（不调模型）；
3. `preflight()`  批量任务开工前跑一次，返回体检 + 还原报告；
4. `save_chapter()` 所有改稿候选写盘前过门禁：空稿、说明文字、篇幅塌陷、
   结尾半句一律拒绝；通过后先留快照、再落盘、同步计划字数。

规则放在这里而不是散在各业务流程里，避免「修好一条路径、另一条又写坏」。
本模块不依赖 writer，可被 writer / app / final_review 任意引用。
"""
from __future__ import annotations

import os
import re
import time

from . import versions, wordguard
from .purity import chapter_text
from .safeio import atomic_write_text

# 模型把「请补发原文」这类说明文字当正文写回磁盘时，开头的典型措辞。
_META_HEADS = (
    "当前诊断", "目前这份", "当前稿件", "当前贴出", "当前收到", "目前收到", "目前提供",
    "当前待修", "当前正文", "当前章节", "当前粘贴", "当前提供的", "目前提供的", "目前收到的",
    "已确认本次修订", "已按", "可以按", "我可以按", "我可以依据", "可以执行",
    "收到。", "本次修订按", "本次按", "请补充",
    "请直接粘贴", "请补发", "请继续粘贴", "无法可靠", "无法百分之百", "不能可靠地",
)
_META_MARKS = (
    "请补充", "请直接粘贴", "请补发", "请继续粘贴", "无法可靠", "无法百分之百",
    "不能可靠地", "我会按", "我将按", "我可以按", "可以按这份", "当前诊断", "目前这份",
    "当前稿件", "当前贴出", "当前收到", "目前收到", "目前提供", "尚未收到", "当前待修",
    "当前正文", "当前章节", "当前粘贴", "本次修订按", "已确认本次修订", "诊断单",
    "修改意见", "定位问题", "信息缺", "无法执行整章", "后续正文缺失", "原文并不完整",
)

_CLEAN_ENDS = "。！？…”』」）"
# 还原来的稿子至少要到这个字数才算「正文」，低于它多半是残留片段。
MIN_RESTORE_WORDS = 200


def clean(text, fallback_title=""):
    return chapter_text(text or "", fallback_title=fallback_title)


def cn_len(text) -> int:
    return wordguard.cn_len(clean(text))


def looks_like_model_note(text, min_body: int = 200) -> bool:
    """是不是模型写给用户的说明/追问，而不是小说正文。

    min_body：有章题时至少要有多少汉字才算正文。新章初稿在测试/极短章场景可能
    只有几十字，调用方传 1 即可放行；体检/还原一律用默认 200。
    """
    s = (text or "").strip()
    if not s:
        return True
    if re.search(r"^#{1,3}\s*第\s*\d+\s*章", s) or s.startswith("## 本章概要"):
        return cn_len(s) < int(min_body)
    if any(s[:120].startswith(h) for h in _META_HEADS):
        return True
    return sum(1 for m in _META_MARKS if m in s) >= 2


def ends_clean(text) -> bool:
    """正文是否收在一句完整的话上（历史稿常被截断在半句）。"""
    c = clean(text).rstrip()
    if not c or "</text>" in c:
        return False
    if c.endswith("补写缺失内容。"):
        return False
    return c.endswith("——") or c[-1] in _CLEAN_ENDS


def reject_reason(orig, cand) -> str:
    """候选稿能否覆盖原章：'' 表示可以。"""
    if not cand or not cand.strip():
        return "候选稿为空"
    if looks_like_model_note(cand):
        return "候选稿是模型说明文字，不是正文"
    n_new = len(re.sub(r"\s+", "", cand))
    n_old = len(re.sub(r"\s+", "", orig or ""))
    if n_old and n_new < max(400, int(n_old * 0.6)):
        return f"篇幅塌陷（{n_new}/{n_old}）"
    return ""


def target_words(proj) -> int:
    try:
        return int((proj.plan() or {}).get("minWordsPerChapter") or 3000)
    except Exception:
        return 3000


def _chapter_file(proj, ch) -> str:
    fp = ch.get("filePath") or ""
    return os.path.join(proj.path, fp) if fp else ""


def _read(fp: str) -> str:
    try:
        return open(fp, encoding="utf-8", errors="replace").read() if fp and os.path.exists(fp) else ""
    except OSError:
        return ""


def restore_best(proj, ch_no, min_words=3000):
    """取最近一份「完整句收尾」的旧稿；优先达到 min_words，否则取最长完整稿。"""
    cands = []
    for v in versions.list_versions(proj.path, int(ch_no)):   # 新→旧
        try:
            t = versions.read_version(proj.path, int(ch_no), v["id"])
        except Exception:
            continue
        if looks_like_model_note(t) or not ends_clean(t):
            continue
        cw = cn_len(t)
        if cw >= MIN_RESTORE_WORDS:
            cands.append((t, cw, v["ts"]))
    for c in cands:
        if c[1] >= int(min_words):
            return c
    return max(cands, key=lambda c: c[1]) if cands else (None, 0, None)


def restore_longer(proj, ch_no, cur_words, target):
    """短章优先找回明显更长且完整收尾的旧稿。"""
    need = max(int(target), int(int(cur_words) * 1.15))
    for v in versions.list_versions(proj.path, int(ch_no)):
        try:
            t = versions.read_version(proj.path, int(ch_no), v["id"])
        except Exception:
            continue
        if looks_like_model_note(t) or not ends_clean(t):
            continue
        cw = cn_len(t)
        if cw >= need:
            return t, cw, v["ts"]
    return None, 0, None


def audit(proj, target: int | None = None) -> dict:
    """只读体检。只检查计划里「已写」的章；未开写的章不算缺失。"""
    target = int(target or target_words(proj))
    plan = proj.plan() or {}
    out = {"target": target, "total": 0, "meta": [], "truncated": [],
           "short": [], "missing": []}
    for ch in plan.get("chapters") or []:
        no = int(ch.get("chapterNumber") or 0)
        if not no:
            continue
        fp = _chapter_file(proj, ch)
        if not fp:
            continue
        if not os.path.exists(fp):
            if ch.get("status") in ("completed", "in_progress"):
                out["missing"].append({"ch": no})
            continue
        out["total"] += 1
        raw = _read(fp)
        w = cn_len(raw)
        if looks_like_model_note(raw):
            out["meta"].append({"ch": no, "words": w})
            continue
        if w >= MIN_RESTORE_WORDS and not ends_clean(raw):
            out["truncated"].append({"ch": no, "words": w})
        if w < target:
            out["short"].append({"ch": no, "words": w})
    out["needs_work"] = sorted({x["ch"] for k in ("meta", "truncated", "short", "missing")
                                for x in out[k]})
    return out


def _set_plan_words(proj, no: int, words: int, target: int) -> None:
    try:
        with proj.lock:
            plan = proj.plan() or {}
            chs = plan.get("chapters") or []
            ch = next((c for c in chs if int(c.get("chapterNumber") or 0) == int(no)), None)
            if ch is None:
                return
            ch["wordCount"] = int(words)
            ch["wordCountPass"] = int(words) >= int(target)
            ch["editedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            plan["totalWords"] = sum(int(c.get("wordCount") or 0) for c in chs)
            proj.save_plan(plan)
    except Exception:
        pass


def repair(proj, target: int | None = None) -> dict:
    """把说明文字稿、半句截断稿还原成版本库里的完整旧稿（零 LLM）。

    - 说明文字：优先还原 ≥ 目标字数的完整稿，没有再取最长完整稿；
    - 半句截断：先按「明显更长」找，再退到「≥ 目标」或最长完整稿；
    - 找不到完整旧稿的截断章记进 `needs_finish`，交给补字/续写流程收尾。
    """
    target = int(target or target_words(proj))
    plan = proj.plan() or {}
    repaired, needs_finish = [], []
    for ch in plan.get("chapters") or []:
        no = int(ch.get("chapterNumber") or 0)
        fp = _chapter_file(proj, ch)
        if not no or not fp or not os.path.exists(fp):
            continue
        raw = _read(fp)
        w = cn_len(raw)
        is_note = looks_like_model_note(raw)
        is_cut = (not is_note) and w >= MIN_RESTORE_WORDS and not ends_clean(raw)
        if not is_note and not is_cut:
            continue
        if is_note:
            txt, tw, ts = restore_best(proj, no, target)
            why = "说明文字"
        else:
            txt, tw, ts = restore_longer(proj, no, w, target)
            if not txt:
                txt, tw, ts = restore_best(proj, no, target)
            why = "半句截断"
        if not txt or tw < MIN_RESTORE_WORDS or clean(txt).strip() == clean(raw).strip():
            if is_cut:
                needs_finish.append({"ch": no, "words": w})
            continue
        versions.snapshot(proj.path, no, raw, label=f"{why}备份")
        atomic_write_text(fp, txt)
        _set_plan_words(proj, no, tw, target)
        repaired.append({"ch": no, "reason": why, "before_words": w,
                         "after_words": tw, "version": ts})
    if repaired:
        proj.log("♻️ 版本库还原 %d 章（%s）：%s" % (
            len(repaired), "、".join(sorted({r["reason"] for r in repaired})),
            "、".join(f"第{r['ch']}章" for r in repaired[:12])
            + ("…" if len(repaired) > 12 else "")))
    if needs_finish:
        proj.log("⚠️ %d 章截断且版本库没有完整旧稿，留给补字/续写收尾：%s" % (
            len(needs_finish),
            "、".join(f"第{x['ch']}章" for x in needs_finish[:12])
            + ("…" if len(needs_finish) > 12 else "")))
    return {"target": target, "repaired": repaired,
            "needs_finish": sorted(x["ch"] for x in needs_finish)}


def preflight(proj, purpose: str = "", target: int | None = None) -> dict:
    """批量任务开工前的统一体检：先还原能还原的，再给出完整报告。"""
    target = int(target or target_words(proj))
    rep = repair(proj, target)
    aud = audit(proj, target)
    proj.log("🩺 开工体检%s：还原 %d 章；仍缺字 %d 章、截断 %d 章、说明文字 %d 章、缺文件 %d 章" % (
        f"（{purpose}）" if purpose else "", len(rep["repaired"]),
        len(aud["short"]), len(aud["truncated"]), len(aud["meta"]), len(aud["missing"])))
    canon_data = {}
    try:
        from . import canon
        canon_data = canon.run(proj, quiet=True)
        seeded = canon.seed_state(proj)
        if seeded.get("promises") or seeded.get("opponents") or seeded.get("humanity"):
            proj.log("📌 正典入账：从故事圣经补登记 "
                     f"{seeded.get('promises', 0)} 条承诺、"
                     f"{seeded.get('opponents', 0)} 名对手、"
                     f"{seeded.get('humanity', 0)} 条人性命题")
    except Exception as e:
        try:
            proj.log(f"⚠️ 正典体检跳过（不影响开工）：{e}")
        except Exception:
            pass
    return {"repair": rep, "audit": aud, "canon": canon_data}


def save_chapter(proj, ch_no, text, *, label="改稿前", kind="rewrite",
                 orig: str | None = None, target: int | None = None,
                 snapshot: bool = True) -> dict:
    """单章写盘唯一门禁：校验 → 留快照 → 原子落盘 → 同步计划字数。

    kind:
      rewrite / expand / fix / deslop / guided → 必须是完整正文且完整收尾；
      restore / revert                          → 只拦空稿和说明文字；
      draft                                     → 新章初稿，允许结尾待续写；
      manual                                    → 作者手改，只拦空稿和说明文字。
    """
    target = int(target or target_words(proj))
    plan = proj.plan() or {}
    ch = next((c for c in (plan.get("chapters") or [])
               if int(c.get("chapterNumber") or 0) == int(ch_no)), None)
    if ch is None or not ch.get("filePath"):
        return {"ok": False, "reason": "章节不存在"}
    fp = _chapter_file(proj, ch)
    old = _read(fp)
    if not (text or "").strip():
        return {"ok": False, "reason": "候选稿为空"}
    if kind not in ("manual",) and looks_like_model_note(
            text, min_body=1 if kind == "draft" else 200):
        return {"ok": False, "reason": "候选稿是模型说明文字，不是正文"}
    if kind in ("rewrite", "expand", "fix", "deslop", "guided"):
        if orig is not None:
            why = reject_reason(orig, text)
            if why:
                return {"ok": False, "reason": why}
        if not ends_clean(text):
            return {"ok": False, "reason": "候选稿结尾不完整（疑似截断）"}
    if old.strip() == (text or "").strip():
        return {"ok": True, "unchanged": True, "words": cn_len(text),
                "before_words": cn_len(old)}
    if snapshot and old.strip():
        versions.snapshot(proj.path, ch_no, old, label=label)
    atomic_write_text(fp, text)
    words = cn_len(text)
    _set_plan_words(proj, ch_no, words, target)
    return {"ok": True, "unchanged": False, "words": words,
            "before_words": cn_len(old)}
