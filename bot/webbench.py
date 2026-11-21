"""全书查找替换 HTTP 接口（v5.5.x，易创对齐清单 A1，机制自研）。

用 FastAPI APIRouter 收口，**不改 app.py 的既有内容**——主会话只需在 app.py 里加两行挂载
（与 webai/webstats 同位置，认证中间件之后）：

    from bot import webbench
    app.include_router(webbench.router)

接口契约（与 static/v2/js/core/findreplace.js 对齐）：
  GET  /api/book/find?key=&q=[&case_sensitive=1]   全书扫描：按章分组
       {groups: [{n, title, count, snippets: [{before, match, after}]}], total, chapters}
       每章 snippet ≤5 条；查不到正文返回空 groups（只报告，不报错）。
  POST /api/book/replace  body: {key, q, replacement, scope: 'chapter'|'book',
                                 chapters?: [n], skip?: [n], case_sensitive?: bool}
       逐章落盘走既有 saveChapter 链路（bot/book_health.save_chapter：校验→
       「替换前」快照→原子落盘→同步计划字数），再补一次 versions.throttled_snapshot
       （与 PUT /api/chapter 完全同口径）。scope='chapter' 必须给出 chapters；
       scope='book' 可用 chapters 收窄范围；skip 里的章跳过（前端 raw 视图锁定章）。

纯净域红线（铁律 0，本模块的第一优先级约束）：
  · 搜索与替换都只作用于「正文域」= purity.chapter_text 口径（章题+章首引子+正文）。
    「本章概要 / 章节备注」等元信息小节里的匹配**不计入**结果、也绝不写入；
  · 替换前对每章先按 purity.sanitize_chapter 的结构口径核对：把「元信息小节文本」
    与「正文域文本」分开计数，替换后元信息区里 q 的命中数必须不变、replacement
    的命中数不得新增，违反即跳过该章（守门核对，正常路径由构造保证不触发）；
  · 概要小节名识别复用 purity._split_sections 的白名单分类，不自写黑名单。
"""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, HTTPException

from . import purity, versions, writer

router = APIRouter()

# ---------------------------------------------------------------- 参数边界

Q_MAX = 200               # 查找词长度上限
REPL_MAX = 5000           # 替换文本长度上限
SNIPPETS_PER_CHAPTER = 5  # 每章返回的上下文片段上限
CTX_BEFORE = 36           # 片段前文字符数
CTX_AFTER = 56            # 片段后文字符数
MAX_CHAPTERS = 100000     # 章节列表防膨胀上限


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=str(message))


def _not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail=str(message))


def _project_of(key: str):
    """作品 key → writer.Project（别名优先，与 app._get_project 同解析口径）。

    这里不复用 app.PROJECT_INDEX（webbench 不 import app）：每次请求新建实例，
    读路径幂等；替换走 book_health.save_chapter 的统一门禁，计划字数同步由它负责。
    """
    if not isinstance(key, str) or not key.strip():
        return None
    k = key.strip()
    try:
        rk = writer.resolve_key(k)
        if rk and rk != k and os.path.isdir(os.path.join(writer.PROJECTS_DIR, rk)):
            k = rk
    except Exception:
        pass
    try:
        pdir = writer._project_dir(k)
    except Exception:
        return None
    if not pdir or not os.path.isdir(pdir):
        return None
    return writer.Project(pdir, k)


def _chapter_file(proj, ch) -> str:
    """章节文件路径：只允许纯文件名（与 app._chapter_path 同口径，挡计划被篡改后的越界）。"""
    name = os.path.basename((ch or {}).get("filePath") or "")
    if not name:
        return ""
    return os.path.join(proj.path, name)


def _read_chapter(fp: str) -> str:
    if not fp or not os.path.exists(fp):
        return ""
    try:
        with open(fp, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _compile(q: str, case_sensitive: bool):
    """查找词 → 正则（转义处理，位置与原文一一对应）；空词返回 None。"""
    try:
        return re.compile(re.escape(q), 0 if case_sensitive else re.IGNORECASE)
    except Exception:
        return None


def _count(rx, text: str) -> int:
    if not rx or not text:
        return 0
    return sum(1 for _ in rx.finditer(text))


def _meta_zone_text(raw: str) -> str:
    """元信息小节（概要/备注等白名单外小节）的原文——替换只作用于正文域，
    这一区一个字都不动。小节识别复用 purity 的结构分类（_split_sections），
    与 sanitize_chapter 同一判定源，不自写黑名单（铁律 0 维护纪律）。"""
    try:
        _head, secs = purity._split_sections(purity.strip_model_noise(raw or ""))
    except Exception:
        return ""
    return "\n".join("\n".join(lines) for kind, _t, _lv, lines in secs
                     if kind in ("summary", "notes", "noise"))


def _prose_of(raw: str, fallback_title: str) -> str:
    """正文域口径：章题 + 章首引子 + 正文（purity.chapter_text，与编辑器 cleanText 一致）。"""
    try:
        return purity.chapter_text(raw or "", fallback_title=fallback_title or "")
    except Exception:
        return ""


def _snippet(prose: str, start: int, end: int) -> dict:
    before = prose[max(0, start - CTX_BEFORE):start]
    after = prose[end:end + CTX_AFTER]
    return {
        "before": ("…" if start > CTX_BEFORE else "") + before,
        "match": prose[start:end],
        "after": after + ("…" if end + CTX_AFTER < len(prose) else ""),
    }


def _plan_chapters(proj) -> list[dict]:
    try:
        plan = proj.plan() or {}
    except Exception:
        return []
    chs = plan.get("chapters") or []
    return chs if isinstance(chs, list) else []


def _norm_no_list(value) -> list[int]:
    out: list[int] = []
    for v in (value or [])[:MAX_CHAPTERS]:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= MAX_CHAPTERS and n not in out:
            out.append(n)
    return out


def _display_key(proj) -> str:
    return os.path.basename(proj.path or "") or ""


# ---------------------------------------------------------------- 全书查找

@router.get("/api/book/find")
def api_book_find(key: str, q: str = "", case_sensitive: bool = False):
    """全书扫描：逐章取正文域（purity.chapter_text 口径），大小写不敏感（可开精确）。

    返回按章分组 results；概要/备注等元信息小节里的匹配不计入；查不到只报告
    （groups=[] / total=0），不报错。"""
    q = str(q or "")
    if not q.strip():
        raise _bad("缺少查找词")
    q = q[:Q_MAX]
    proj = _project_of(key)
    if proj is None:
        raise _not_found("项目不存在或未加载，请刷新作品库")
    rx = _compile(q, bool(case_sensitive))
    if rx is None:
        raise _bad("查找词无法解析")

    groups = []
    total = 0
    for ch in _plan_chapters(proj):
        try:
            n = int(ch.get("chapterNumber") or 0)
        except (TypeError, ValueError):
            continue
        if n < 1:
            continue
        raw = _read_chapter(_chapter_file(proj, ch))
        if not raw.strip():
            continue
        prose = _prose_of(raw, ch.get("title") or "")
        if not prose:
            continue
        spans = list(rx.finditer(prose))
        if not spans:
            continue
        total += len(spans)
        groups.append({
            "n": n,
            "title": ch.get("title") or "",
            "count": len(spans),
            "snippets": [_snippet(prose, m.start(), m.end()) for m in spans[:SNIPPETS_PER_CHAPTER]],
        })
    return {"ok": True, "key": _display_key(proj), "q": q,
            "case_sensitive": bool(case_sensitive),
            "groups": groups, "total": total, "chapters": len(groups)}


# ---------------------------------------------------------------- 全书替换

@router.post("/api/book/replace")
def api_book_replace(payload: dict):
    """全书/单章替换：逐章走既有 saveChapter 链路落盘（可从章节历史版本恢复）。

    · 每章先取正文域并重排 replacement；元信息小节（概要/备注）由 merge_meta
      原样回填——替换内容构造上就进不了元信息区，另有 sanitize_chapter 口径的
      守门核对（元信息区 q 命中数不变、replacement 命中数不新增，违反即跳过）；
    · 落盘 = book_health.save_chapter（校验 → 「替换前」快照 → 原子写 → 计划字数）
      + versions.throttled_snapshot（与 PUT /api/chapter 同口径）；
      替换前的快照**每章必留**（不受节流限制），原文可在章节历史版本中恢复；
    · skip 里的章跳过（前端 raw 视图锁定章）；查不到正文的章只报告不报错。
    """
    p = payload if isinstance(payload, dict) else {}
    key = str(p.get("key") or "").strip()
    q = str(p.get("q") or "")
    if not q.strip():
        raise _bad("缺少查找词")
    q = q[:Q_MAX]
    repl = p.get("replacement")
    repl = "" if repl is None else str(repl)[:REPL_MAX]
    scope = str(p.get("scope") or "").strip()
    if scope not in ("chapter", "book"):
        raise _bad("scope 必须是 chapter 或 book")
    case_sensitive = bool(p.get("case_sensitive"))
    chapters = _norm_no_list(p.get("chapters"))
    skip = set(_norm_no_list(p.get("skip")))

    if scope == "chapter" and not chapters:
        raise _bad("scope=chapter 需要提供 chapters（章号列表）")

    proj = _project_of(key)
    if proj is None:
        raise _not_found("项目不存在或未加载，请刷新作品库")
    rx = _compile(q, case_sensitive)
    if rx is None:
        raise _bad("查找词无法解析")

    by_no: dict[int, dict] = {}
    for ch in _plan_chapters(proj):
        try:
            n = int(ch.get("chapterNumber") or 0)
        except (TypeError, ValueError):
            continue
        if n >= 1:
            by_no[n] = ch

    if scope == "chapter":
        targets = [n for n in chapters if n in by_no]
    elif chapters:
        targets = [n for n in chapters if n in by_no]
    else:
        targets = sorted(by_no)
    changed: list[dict] = []
    skipped: list[dict] = []
    # raw 视图锁定章：先报告再从目标里剔除（跳过也要让前端看得见原因）
    locked_out = [n for n in targets if n in skip]
    for n in locked_out:
        skipped.append({"n": n, "title": by_no[n].get("title") or "", "reason": "raw_locked"})
    targets = [n for n in targets if n not in skip]

    for n in targets:
        ch = by_no[n]
        title = ch.get("title") or ""
        raw = _read_chapter(_chapter_file(proj, ch))
        if not raw.strip():
            skipped.append({"n": n, "title": title, "reason": "empty"})
            continue
        prose = _prose_of(raw, title)
        if not prose:
            skipped.append({"n": n, "title": title, "reason": "no_prose"})
            continue
        spans = list(rx.finditer(prose))
        if not spans:
            # 查不到正文（包括「只在概要里出现」）→ 只报告，不报错、不落盘
            skipped.append({"n": n, "title": title, "reason": "no_match"})
            continue
        last = 0
        parts: list[str] = []
        for m in spans:
            parts.append(prose[last:m.start()])
            parts.append(repl)
            last = m.end()
        parts.append(prose[last:])
        new_prose = "".join(parts)

        # ---- 纯净域守门核对（替换前口径核对，元信息小节一个字都不许动）----
        new_raw = purity.merge_meta(new_prose, raw)
        meta_before = _meta_zone_text(raw)
        meta_after = _meta_zone_text(new_raw)
        repl_rx = _compile(repl, True) if repl else None
        if (_count(rx, meta_after) != _count(rx, meta_before)
                or (repl_rx is not None
                    and _count(repl_rx, meta_after) > _count(repl_rx, meta_before))):
            skipped.append({"n": n, "title": title, "reason": "purity_guard"})
            continue

        # ---- 既有 saveChapter 链路：校验 → 「替换前」快照 → 落盘 → 计划字数 ----
        res = writer.book_health.save_chapter(proj, n, new_raw, label="替换前", kind="manual")
        if not res.get("ok"):
            skipped.append({"n": n, "title": title,
                            "reason": str(res.get("reason") or "save_rejected")})
            continue
        if not res.get("unchanged"):
            # 与 PUT /api/chapter 同口径：保存成功后补一次节流快照（auto 标签）
            try:
                versions.throttled_snapshot(proj.path, n, new_raw, label="自动保存")
            except Exception:
                pass
        changed.append({"n": n, "title": title, "count": len(spans),
                        "words": int(res.get("words") or 0)})

    # ---- 与 PUT /api/chapter 同语义的收尾：完成态 + 全书字数 + 成书重建 ----
    if changed:
        done = {c["n"] for c in changed}
        try:
            with proj.lock:
                plan = proj.plan() or {}
                chs = plan.get("chapters") or []
                for c in chs:
                    try:
                        no = int(c.get("chapterNumber") or 0)
                    except (TypeError, ValueError):
                        continue
                    if no in done and str(c.get("status") or "") != "completed":
                        c["status"] = "completed"
                plan["totalWords"] = sum(int(c.get("wordCount") or 0) for c in chs)
                proj.save_plan(plan)
        except Exception:
            pass
        try:
            writer.rebuild_book(proj)
        except Exception:
            pass

    return {"ok": True, "key": _display_key(proj), "q": q, "replacement": repl,
            "scope": scope, "case_sensitive": case_sensitive,
            "changed": changed, "skipped": skipped,
            "total": sum(c["count"] for c in changed), "chapters": len(changed)}
