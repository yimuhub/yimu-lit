"""成书终审（v4.38.0）——按作者定的顺序收尾，不按机器方便的顺手顺序。

作者原话（2026-11）：一本书写完以后，
  ① 字数是否达标——最核心，排第一；
  ② 根据评分重写——<60 立刻改；60–80 等全书评价完再决定；
  ③ 全篇校验——整本通读，找问题与逻辑冲突。
且「整体结构和内容衔接不错、总评 ≥75 就不必改 60–80 的章；总评 <75 就改」。
目标不是刷分，是让文本真的像人写的（AI 味检查 + 工艺硬伤 + 连续性）。

本模块只做这件事，并且所有结论都写进 `15-成书终审.md`：
- 字数统计按磁盘正文重数（不信任计划里的旧 wordCount）；
- 评分能复用就复用（内容 sha 未变），复用了多少、重评了多少，报告里写明；
- 重写只保留「真的提分」的版本（沿用章后自动重写的保守策略）；
- 任何一步失败都不阻断成书，报告里如实写「这一步没做成」。

入口：
- run_novel(proj)         小说完整终审（由 writer._validate_and_finish 调用）
- run_for_key(key)        任意文体（小说 / 剧本 / 诗词 / 散文 / 论文 / 小文体）
- auto(key)               非小说流水线收尾钩子（幂等，异常静默）
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re

from . import craft_check, llm, medium, purity, steps, versions, wordguard, workflow

#: 作者定的评分策略（单节 / 全书都是百分制）
POLICY = {
    "immediate": 60,   # 低于 60：立刻重写
    "defer": 80,       # 60–80：等全书评价完再决定
    "book_pass": 75,   # 全书总评 ≥75 且结构不差 → 60–80 的章不改
}

#: 一轮终审里自动重写的上限（防止一次跑掉几小时；超出的如实写进报告，可再点继续）
MAX_REWRITE = 24
MAX_WORD_FIX = 40

_STAGE_LABEL = {
    "verifying": "字数达标", "scoring": "逐章评分", "rewriting": "定向重写",
    "reviewing": "全书校验", "done": "已收尾",
}


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _key(proj) -> str:
    return os.path.basename(str(getattr(proj, "path", "") or ""))


def _emit(key, agent, title, *, phase="done", thinking="", output="", chapter=0, level="info"):
    try:
        steps.emit(key, agent, phase=phase, title=title, thinking=thinking,
                   output=output, chapter=chapter, level=level)
    except Exception:
        pass


def _sha(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()


def _cn(text: str) -> int:
    try:
        return int(wordguard.cn_len(text or ""))
    except Exception:
        return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _write(path: str, text: str) -> None:
    try:
        from .safeio import atomic_write_text
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write_text(path, text)
    except Exception:
        pass


# ================================================================ 小说

def run_novel(proj, policy: dict | None = None, trigger: str = "auto") -> dict:
    """小说成书终审：字数 → 评分/重写 → 全书校验（严格按作者定的顺序）。"""
    from . import writer
    pol = {**POLICY, **(policy or {})}
    key = _key(proj)
    plan = proj.plan() or {}
    chapters = plan.get("chapters") or []
    if not chapters:
        return {"ok": False, "error": "没有章节可终审"}
    # 幂等：正文一字未变且上一轮已收尾 → 直接复用，不再烧 token
    _old = (plan.get("meta") or {}).get("final_review") or {}
    if _old.get("ok") and _old.get("book_sha") == _book_sha(proj, plan):
        return _old

    try:
        from . import journal as _j
        cur = _j.current_task(proj.path)
        if not (isinstance(cur, dict) and cur.get("kind") == "review"):
            _j.begin(proj.path, "review", f"终审《{plan.get('novelName') or proj.name}》",
                     total=len(chapters))
        _j.progress(proj.path, stage="verifying", total=len(chapters), done=0)
        _j.remember(proj.path, "system", "开始成书终审（字数 → 评分重写 → 全书校验）")
    except Exception:
        pass

    _emit(key, "editor", "成书终审开始", phase="start",
          thinking=f"顺序：①字数 → ②评分（<{pol['immediate']} 立即改，"
                    f"{pol['immediate']}–{pol['defer']} 待全书评分定） → ③全书校验",
          output=f"{len(chapters)} 章")

    # ---------- ① 字数是否达标（第一位） ----------
    word = _word_pass(proj, plan, key)
    _emit(key, "editor", "① 字数核验完成",
          output=(f"不足 {word['below']} 章；本轮补足 {word['fixed']} 章"
                  + (f"；仍有 {word['still']} 章未达标" if word["still"] else "")),
          thinking=f"验收线 {word['floor']} 字（标准 {word['target']} 字的 90%）")

    # ---------- ② 逐章评分 + 立即重写 <60 ----------
    plan = proj.plan() or plan
    score = _score_pass(proj, plan, pol, word["changed"], key)
    _emit(key, "reviewer", "② 逐章评分完成",
          output=(f"立即重写 {len(score['immediate'])} 章（<{pol['immediate']}）；"
                  f"待全书裁决 {len(score['deferred'])} 章（{pol['immediate']}–{pol['defer']}）；"
                  f"复用 {score['reused']} 章旧评分"),
          thinking="60–80 分先不动，等第三步的全书总评与结构判定")

    # ---------- ③ 全书校验（含结构 / 衔接 / AI 味 / 逻辑） ----------
    plan = proj.plan() or plan
    book = _book_review(proj, plan, key)
    craft = _craft_book(proj, plan, key)
    struct_high = [f for f in (book.get("l1") or [])
                   if str(f.get("severity") or "").lower() in ("critical", "high")]
    structure_ok = (int(book.get("total") or 0) >= int(pol["book_pass"])
                    and int((craft.get("counts") or {}).get("block") or 0) == 0
                    and not struct_high)

    _emit(key, "editor", "③ 全书校验完成",
          output=(f"总评 {book.get('total')}（达标线 {pol['book_pass']}，"
                  f"{'结构与衔接可接受' if structure_ok else '结构/衔接需要处理'}）；"
                  f"工艺硬伤 {(craft.get('counts') or {}).get('block', 0)} 项"),
          thinking=f"L1 规则命中 {len(book.get('l1') or [])} 条；"
                   f"AI 味/节奏告警 {(craft.get('counts') or {}).get('warn', 0)} 项")

    deferred_decision = "none"
    rewritten_deferred: list[int] = []
    skipped_deferred: list[int] = []
    if score["deferred"]:
        if structure_ok:
            deferred_decision = "keep"
            _emit(key, "editor", "60–80 分章节：不改",
                  output=f"{len(score['deferred'])} 章保留原稿",
                  thinking=f"全书总评 {book.get('total')} ≥ {pol['book_pass']}，"
                           f"结构/衔接与工艺硬伤均通过——低分章节不值得再动")
        else:
            deferred_decision = "rewrite"
            _emit(key, "rewriter", "60–80 分章节：进入定向重写", phase="start",
                  output=f"{len(score['deferred'])} 章",
                  thinking=f"全书总评 {book.get('total')} < {pol['book_pass']} 或结构/工艺未通过")
            targets = score["deferred"][:MAX_REWRITE]
            skipped_deferred = score["deferred"][MAX_REWRITE:]
            for no in targets:
                before = score["deferred_scores"].get(no)
                try:
                    fin = writer._auto_rewrite_chapter(proj, no, pass_line=pol["defer"],
                                                       max_rounds=1, skip_deslop=True)
                    # 只把「确实提分」的算作已改写；分数没变视为改写未生效，如实留在原稿
                    if fin is not None and before is not None and fin > before:
                        rewritten_deferred.append(no)
                    elif fin is not None:
                        proj.log(f"↩️ 终审第{no}章定向重写未提分（{before} → {fin}），保留原稿")
                except Exception as e:
                    proj.log(f"⚠️ 终审第{no}章定向重写失败：{e}")
            if rewritten_deferred:
                plan = proj.plan() or plan
                book = _book_review(proj, plan, key)
                craft = _craft_book(proj, plan, key)
                struct_high = [f for f in (book.get("l1") or [])
                               if str(f.get("severity") or "").lower() in ("critical", "high")]
                structure_ok = (int(book.get("total") or 0) >= int(pol["book_pass"])
                                and int((craft.get("counts") or {}).get("block") or 0) == 0
                                and not struct_high)

    # 终审后的字数事实（只统计，不再改，避免与刚完成的重写互相打架）
    final_words = _final_word_facts(proj, plan)

    result = {
        "ok": True, "at": _now(), "trigger": trigger, "policy": pol,
        "words": word, "final_words": final_words,
        "scores": {"immediate": score["immediate"], "deferred": score["deferred"],
                   "reused": score["reused"], "fresh": score["fresh"],
                   "immediate_after": score["immediate_after"]},
        "book": {"total": book.get("total"), "pass_line": pol["book_pass"],
                 "structure_ok": structure_ok,
                 "l1_findings": len(book.get("l1") or []),
                 "high_findings": len(struct_high)},
        "craft": {"counts": (craft.get("counts") or {}), "gates": (craft.get("gates") or [])[:12]},
        "deferred_decision": deferred_decision,
        "rewritten_deferred": rewritten_deferred,
        "skipped_deferred": skipped_deferred,
        "finished_at": _now(),
        "book_sha": _book_sha(proj, plan),
    }
    _write_report_novel(proj, plan, result)
    try:
        p = proj.plan() or {}
        p.setdefault("meta", {})["final_review"] = result
        proj.save_plan(p)
    except Exception:
        pass
    _emit(key, "editor", "成书终审完成",
          output=f"总评 {book.get('total')} · 字数达标 {final_words['ok']}/{final_words['total']} 章"
                 + (f" · 追加改写 {len(rewritten_deferred)} 章" if rewritten_deferred else ""),
          thinking="结论已写入 15-成书终审.md")
    return result


def _word_pass(proj, plan, key) -> dict:
    """① 字数：按磁盘正文重数，不足的续写/扩写补足（只补，不删）。"""
    from . import writer
    target = int(plan.get("minWordsPerChapter") or 3000)
    floor = int(target * 0.9)
    chapters = plan.get("chapters") or []
    below, fixed, still, changed = [], [], [], []
    for ch in chapters:
        no = ch.get("chapterNumber") or 0
        fp = writer._chapter_file_path(proj, ch)
        if not fp or not os.path.exists(fp):
            below.append(no)
            still.append(no)
            continue
        body = _read(fp)
        wc = _cn(writer._clean_chapter_text(body))
        ch["wordCount"] = wc
        if wc >= floor:
            continue
        below.append(no)
        if len(fixed) >= MAX_WORD_FIX:
            still.append(no)
            continue
        _emit(key, "writer", f"第{no}章字数补足", phase="start",
              thinking=f"当前 {wc} 字，验收线 {floor} 字", chapter=no)
        need = max(300, floor - wc + 150)
        try:
            ctx = writer._build_ctx(proj, ch)
        except Exception:
            ctx = {}
        new_body = body
        try:
            cont = purity.strip_fragment(workflow.continue_chapter(body, ctx, need_words=need))
            if _cn(cont) >= 80:
                new_body = body.rstrip() + "\n\n" + cont
        except Exception:
            pass
        wc2 = _cn(writer._clean_chapter_text(new_body))
        if wc2 < floor:
            try:
                exp = workflow.expand_chapter(new_body, ctx, min_words=target, current_words=wc2)
                exp = purity.sanitize_chapter(exp) if exp else ""
                if exp and _cn(exp) > wc2:
                    new_body = exp
            except Exception:
                pass
        wc3 = _cn(writer._clean_chapter_text(new_body))
        if wc3 > wc:
            try:
                versions.snapshot(proj.path, no, body, label="终审字数补足前")
            except Exception:
                pass
            _write(fp, new_body.strip() + "\n")
            ch["wordCount"] = wc3
            changed.append(no)
            fixed.append(no)
            _emit(key, "writer", f"第{no}章字数补足完成",
                  output=f"{wc} → {wc3} 字（验收线 {floor}）", chapter=no)
        else:
            still.append(no)
            _emit(key, "writer", f"第{no}章字数补足未成功",
                  output=f"仍 {wc3} 字", chapter=no, level="warn")
    try:
        proj.save_plan(plan)
    except Exception:
        pass
    return {"target": target, "floor": floor, "total": len(chapters),
            "below": len(below), "fixed": len(fixed), "still": len(still),
            "below_chapters": below[:40], "still_chapters": still[:40],
            "changed": changed}


def _score_pass(proj, plan, pol, changed, key) -> dict:
    """② 逐章评分：内容 sha 未变且有分就复用；<60 立即重写，60–80 留待全书裁决。"""
    from . import writer
    entries = ((plan.get("meta") or {}).get("chapter_scores") or {})
    reused = fresh = 0
    immediate, deferred, immediate_after = [], [], []
    deferred_scores: dict[int, float] = {}
    chapters = plan.get("chapters") or []
    for ch in chapters:
        no = ch.get("chapterNumber") or 0
        fp = writer._chapter_file_path(proj, ch)
        if not fp or not os.path.exists(fp):
            continue
        body = writer._clean_chapter_text(_read(fp))
        sha = _sha(body)
        ent = entries.get(str(no)) or {}
        score = ent.get("score")
        # 复用只认「同一份正文」：旧数据没有 sha 时保守重评一次，不拿别的正文的分冒充
        need = (score is None or no in changed or ent.get("sha") != sha)
        if need:
            try:
                score = writer.score_chapter(proj, no)
                fresh += 1
            except Exception as e:
                proj.log(f"⚠️ 终审第{no}章评分失败：{e}")
                score = None
        else:
            reused += 1
        # 记内容 sha：以后同一份正文不再重复花 token 评分
        try:
            plan2 = proj.plan() or {}
            ce = (((plan2.get("meta") or {}).get("chapter_scores") or {}).get(str(no)) or {})
            if ce:
                ce["sha"] = sha
                proj.save_plan(plan2)
        except Exception:
            pass
        try:
            sc = float(score) if score is not None else None
        except (TypeError, ValueError):
            sc = None
        if sc is None:
            continue
        if sc < pol["immediate"]:
            immediate.append(no)
            _emit(key, "rewriter", f"第{no}章低于 {pol['immediate']} 分，立即重写", phase="start",
                  output=f"当前 {sc} 分", chapter=no)
            try:
                fin = writer._auto_rewrite_chapter(proj, no, pass_line=pol["immediate"],
                                                   max_rounds=2, skip_deslop=True)
                if fin is not None:
                    immediate_after.append({"ch": no, "from": sc, "to": fin})
                    _emit(key, "rewriter", f"第{no}章立即重写完成",
                          output=f"{sc} → {fin} 分", chapter=no)
            except Exception as e:
                proj.log(f"⚠️ 终审第{no}章立即重写失败：{e}")
        elif sc < pol["defer"]:
            deferred.append(no)
            deferred_scores[no] = sc
    deferred.sort()
    return {"immediate": immediate, "immediate_after": immediate_after,
            "deferred": deferred, "deferred_scores": deferred_scores,
            "reused": reused, "fresh": fresh}


def _book_review(proj, plan, key) -> dict:
    """③ 全书分层审校（问题 / 逻辑 / 结构衔接 / 百分制）。失败不阻断。"""
    from . import writer
    _emit(key, "editor", "全书校验", phase="start",
          thinking="L1 确定性规则 + 五角色分工评审（超长篇分层采样）")
    try:
        return writer._run_layered(proj, "report", 0) or {}
    except Exception as e:
        proj.log(f"⚠️ 终审全书校验失败（不阻断成书）：{e}")
        _emit(key, "editor", "全书校验失败（不阻断成书）", phase="warn", output=str(e)[:120])
        return {}


def _craft_book(proj, plan, key) -> dict:
    """确定性工艺体检（零 token）：AI 味 / 节奏 / 格式 / 断章等硬伤。"""
    from . import writer
    chs = []
    for ch in plan.get("chapters") or []:
        fp = writer._chapter_file_path(proj, ch)
        if fp and os.path.exists(fp):
            chs.append({"n": ch.get("chapterNumber") or 0,
                        "text": writer._clean_chapter_text(_read(fp))})
    try:
        rep = craft_check.check_book(chs, plan.get("config") or {})
        _write(os.path.join(proj.path, "18-工艺体检.md"), craft_check.render_report(rep))
        return rep
    except Exception as e:
        proj.log(f"⚠️ 终审工艺体检失败（不阻断）：{e}")
        return {"counts": {}, "gates": []}


def _final_word_facts(proj, plan) -> dict:
    from . import writer
    target = int(plan.get("minWordsPerChapter") or 3000)
    floor = int(target * 0.9)
    total = ok = 0
    below = []
    for ch in plan.get("chapters") or []:
        fp = writer._chapter_file_path(proj, ch)
        if not fp or not os.path.exists(fp):
            continue
        total += 1
        wc = _cn(writer._clean_chapter_text(_read(fp)))
        if wc >= floor:
            ok += 1
        else:
            below.append({"ch": ch.get("chapterNumber"), "words": wc})
    return {"total": total, "ok": ok, "below": below[:40], "floor": floor}


def _book_sha(proj, plan) -> str:
    from . import writer
    h = hashlib.sha1()
    for ch in plan.get("chapters") or []:
        fp = writer._chapter_file_path(proj, ch)
        if fp and os.path.exists(fp):
            h.update(_read(fp).encode("utf-8", "replace"))
    return h.hexdigest()


def _write_report_novel(proj, plan, result: dict) -> None:
    w = result["words"]
    s = result["scores"]
    b = result["book"]
    c = result["craft"].get("counts") or {}
    lines = [
        "# 成书终审",
        f"> {result['at']} · 策略：<{result['policy']['immediate']} 立即重写；"
        f"{result['policy']['immediate']}–{result['policy']['defer']} 待全书总评定；"
        f"总评 ≥{result['policy']['book_pass']} 且结构/工艺通过则不改",
        "",
        "## ① 字数是否达标（第一位）",
        f"- 标准每章 {w['target']} 字，验收线 {w['floor']} 字（90%）",
        f"- 不足 {w['below']} 章；本轮补足 {w['fixed']} 章；仍有 {w['still']} 章未达标",
    ]
    if w.get("still_chapters"):
        lines.append(f"- 仍未达标章号：{', '.join(str(x) for x in w['still_chapters'])}"
                     + (f"（本轮补足上限 {MAX_WORD_FIX} 章，其余可再点一次继续）"
                        if w["still"] > len(w.get("still_chapters") or []) else ""))
    lines += [
        "",
        "## ② 评分与重写",
        f"- 复用已有评分 {s['reused']} 章（正文 sha 未变）；本轮新评 {s['fresh']} 章",
        f"- 低于 {result['policy']['immediate']} 分、立即重写：{len(s['immediate'])} 章"
        + (f"（{', '.join(str(x) for x in s['immediate'])}）" if s["immediate"] else ""),
    ]
    for row in s.get("immediate_after") or []:
        lines.append(f"  - 第{row['ch']}章：{row['from']} → {row['to']} 分")
    lines.append(f"- {result['policy']['immediate']}–{result['policy']['defer']} 分、留待全书裁决："
                 f"{len(s['deferred'])} 章"
                 + (f"（{', '.join(str(x) for x in s['deferred'])}）" if s["deferred"] else ""))
    if result["deferred_decision"] == "keep":
        lines.append(f"- 裁决：不改。全书总评 {b['total']} ≥ {b['pass_line']}，结构与工艺检查通过")
    elif result["deferred_decision"] == "rewrite":
        lines.append(f"- 裁决：定向重写 {len(result['rewritten_deferred'])} 章"
                     + (f"（{', '.join(str(x) for x in result['rewritten_deferred'])}）"
                        if result["rewritten_deferred"] else ""))
        if result["skipped_deferred"]:
            lines.append(f"- 本轮未处理（超出单轮上限 {MAX_REWRITE} 章）："
                         f"{', '.join(str(x) for x in result['skipped_deferred'])}")
    lines += [
        "",
        "## ③ 全书校验（问题 / 逻辑 / 结构衔接）",
        f"- 全书总评：**{b['total']}** / 100（作者定的全书线 {b['pass_line']}，"
        f"{'通过' if b['total'] >= b['pass_line'] else '未通过'}）",
        f"- 结构/衔接判定：{'可接受' if b['structure_ok'] else '需要处理'}",
        f"- L1 确定性规则命中 {b['l1_findings']} 条（critical/high {b['high_findings']} 条）",
        f"- 工艺体检：硬伤 {c.get('block', 0)} 项 · 告警 {c.get('warn', 0)} 项 · "
        f"提示 {c.get('info', 0)} 项（详见 18-工艺体检.md）",
        "",
        "## 最终字数事实",
        f"- {result['final_words']['ok']}/{result['final_words']['total']} 章达到验收线"
        f"（{result['final_words']['floor']} 字）",
    ]
    if result["final_words"].get("below"):
        lines.append("- 仍低于验收线："
                     + "、".join(f"第{x['ch']}章 {x['words']}字"
                                 for x in result["final_words"]["below"][:12]))
    lines += [
        "",
        "## 说明（不粉饰）",
        "- 字数按磁盘正文重新统计，不采用计划里的旧 wordCount。",
        "- 评分复用只发生在正文 sha 未变时；正文改过就重评。",
        "- 重写只保留提分版本；未提分自动还原原稿（防 LLM 波动改坏）。",
        "- 全书校验是 LLM 审校 + 确定性规则，不等于人工通读；critical/high 项请看 14-质量报告.md。",
        "",
        "_模板：final-review（一幕·一幕·YIMU 神笔）。_",
    ]
    _write(os.path.join(proj.path, "15-成书终审.md"), "\n".join(lines) + "\n")


# ================================================================ 其他文体

_NONNOVEL = {
    "script": "script_writer", "poetry": "poetry_writer", "prose": "prose_writer",
    "thesis": "thesis_writer", "children": "children_writer", "news": "news_writer",
    "copy": "copy_writer", "other": "other_writer",
}


def run_for_key(key: str, policy: dict | None = None, trigger: str = "auto") -> dict:
    """任意文体终审。小说走完整实现；其余文体走「字数 → 分件评分/重写 → 全篇校验」。"""
    from . import writer
    med = ""
    try:
        med = writer.medium_of_key(key) or ""
    except Exception:
        med = ""
    if not med or med == "novel":
        proj = writer.Project(writer._project_dir(key), key)
        return run_novel(proj, policy=policy, trigger=trigger)
    return _run_other(key, med, POLICY if policy is None else {**POLICY, **policy})


def auto(key: str) -> dict:
    """流水线收尾钩子：非小说完成后自动终审；后台线程执行，幂等，异常静默。

    放在后台是为了不拖慢「作品已完成」的返回；终审自身在 journal 里登记，
    进程重启后「继续」可以走 tasks 的 review 续跑器增量接着做。
    """
    try:
        from . import writer
        med = writer.medium_of_key(key) or ""
        if not med or med == "novel":
            return {}

        def _job():
            try:
                from . import journal as _j
                from . import llm as _llm
                _llm.book(key, "review")
                proj = _module_for(med).get_project(key)
                if proj is None:
                    return
                cur = _j.current_task(proj.path)
                if not (isinstance(cur, dict) and cur.get("kind") == "review"):
                    _j.begin(proj.path, "review", f"终审《{proj.name}》", total=1)
                _j.update(proj.path, status="running", stage="reviewing")
                _run_other(key, med, POLICY)
                _j.finish(proj.path, "done", note="成书终审完成")
                _j.remember(proj.path, "system", "成书终审完成")
            except Exception as e:
                try:
                    from . import journal as _j
                    proj = _module_for(med).get_project(key)
                    if proj is not None:
                        _j.update(proj.path, status="paused", stage="interrupted",
                                  note=f"终审中断：{str(e)[:80]}")
                        _j.remember(proj.path, "system", f"成书终审中断：{str(e)[:100]}")
                except Exception:
                    pass

        import threading
        threading.Thread(target=_job, daemon=True).start()
        return {}
    except Exception:
        return {}


def _module_for(med: str):
    name = _NONNOVEL.get(med)
    if not name:
        return None
    import importlib
    try:
        return importlib.import_module(f"bot.{name}")
    except Exception:
        return None


def _pieces_other(key: str, med: str, mod):
    """把任意文体拆成可评分的「件」：[{no,title,path,text}] + 项目计划 + 终稿全文。"""
    proj = mod.get_project(key) if hasattr(mod, "get_project") else None
    if proj is None:
        return None, {}, ""
    plan = proj.plan() or {}
    pdir = proj.path
    pieces = []
    if med == "script":
        body_dir = getattr(mod, "F_BODY", "04-正文")
        for sc in plan.get("scenes") or []:
            no = int(sc.get("no") or 0)
            fn = sc.get("fileName") or f"第{no:03d}场.md"
            fp = os.path.join(pdir, body_dir, os.path.basename(str(fn)))
            pieces.append({"no": no, "title": f"第{no}场", "path": fp, "text": _read(fp)})
        full = "\n\n".join(p["text"].strip() for p in pieces if p["text"].strip())
        return proj, plan, full
    if med == "thesis":
        body_dir = os.path.join(pdir, "02-正文")
        if os.path.isdir(body_dir):
            for fn in sorted(os.listdir(body_dir)):
                if not fn.endswith(".md"):
                    continue
                ch_no = 0
                m = re.match(r"(\d+)", fn)
                if m:
                    ch_no = int(m.group(1))
                pieces.append({"no": ch_no, "title": fn[:-3],
                               "path": os.path.join(body_dir, fn), "text": _read(os.path.join(body_dir, fn))})
        if not pieces:
            fp = os.path.join(pdir, "04-论文定稿.md")
            pieces = [{"no": 1, "title": "论文定稿", "path": fp, "text": _read(fp)}]
        return proj, plan, "\n\n".join(p["text"] for p in pieces if p["text"].strip())
    if med == "children":
        body_dir = os.path.join(pdir, "02-故事")
        if os.path.isdir(body_dir):
            for fn in sorted(os.listdir(body_dir)):
                if fn.endswith(".md"):
                    pieces.append({"no": len(pieces) + 1, "title": fn[:-3],
                                   "path": os.path.join(body_dir, fn),
                                   "text": _read(os.path.join(body_dir, fn))})
    # 单篇 / 以上未命中：取定稿或初稿
    for cand in ("04-定稿.md", "04-新闻稿定稿.md", "04-文案定稿.md",
                 "04-论文定稿.md", "02-初稿.md", "02-正文.md"):
        fp = os.path.join(pdir, cand)
        if os.path.exists(fp):
            pieces = [{"no": 1, "title": "定稿", "path": fp, "text": _read(fp)}]
            break
    full = "\n\n".join(p["text"] for p in pieces if p["text"].strip())
    return proj, plan, full


def _run_other(key: str, med: str, pol: dict) -> dict:
    mod = _module_for(med)
    if not mod:
        return {"ok": False, "error": f"未知文体 {med}"}
    proj, plan, full = _pieces_other(key, med, mod)
    if proj is None or not full.strip():
        return {"ok": False, "error": "还没有可终审的正文"}
    cfg = plan.get("config") if isinstance(plan.get("config"), dict) else {}
    try:
        from . import journal as _j
        cur = _j.current_task(proj.path)
        if not (isinstance(cur, dict) and cur.get("kind") == "review"):
            _j.begin(proj.path, "review", f"终审《{proj.name}》", total=1)
    except Exception:
        pass

    sha = _sha(full)
    old = ((plan.get("meta") or {}).get("final_review") or {})
    if old.get("ok") and old.get("text_sha") == sha:
        return old   # 幂等：同一份正文不重复烧 token

    _emit(key, "editor", "成书终审开始", phase="start",
          thinking=f"{medium.label(med)}：①字数 → ②分件评分（<{pol['immediate']} 立即改）→ ③全篇校验")

    # ① 字数
    target = int(cfg.get("target_words") or cfg.get("words") or 0)
    if med == "script":
        pages = int(plan.get("targetPages") or cfg.get("pages") or 0)
        # 中文剧本按「每页约 350 字」粗估——报告里明确标注是估算，不当成硬指标
        target = int(pages * 350) if pages else 0
    elif med == "thesis":
        target = int(cfg.get("target_words") or 0)
    words = _other_words(proj, pieces=None, plan=plan, med=med, full=full, target=target, cfg=cfg)

    # ② 分件评分 + <60 立即重写
    score = _other_score(proj, plan, mod, med, cfg, pol, key)
    if score.get("rewritten_immediate"):
        full = _pieces_other(key, med, mod)[2]   # 立即重写改过盘：③ 必须校验改后的正文

    # ③ 全篇校验 + 全书总评
    whole = _rubric_score(full[:20000], med, cfg, scope="全篇")
    piece_scores = [p["score"] for p in score["pieces"] if isinstance(p.get("score"), (int, float))]
    piece_avg = (sum(piece_scores) / len(piece_scores)) if piece_scores else None
    book_total = whole.get("total") or 0
    if piece_avg is not None and len(score["pieces"]) > 1:
        book_total = int(round(piece_avg * 0.6 + (whole.get("total") or 0) * 0.4))
    checks = _other_checks(proj, plan, mod, med, full, cfg)
    structure_ok = book_total >= pol["book_pass"] and not checks.get("blocked")

    deferred_decision = "none"
    rewritten = []
    if score["deferred"]:
        if structure_ok:
            deferred_decision = "keep"
        else:
            deferred_decision = "rewrite"
            for p in score["pieces"]:
                if p["no"] not in score["deferred"] or len(rewritten) >= 12:
                    continue
                ok = _rewrite_piece(proj, mod, med, p, cfg, pol["defer"])
                if ok:
                    rewritten.append(p["no"])
            if rewritten:
                full = _pieces_other(key, med, mod)[2]
                whole = _rubric_score(full[:20000], med, cfg, scope="全篇")
                checks = _other_checks(proj, plan, mod, med, full, cfg)
                book_total = whole.get("total") or book_total
                structure_ok = book_total >= pol["book_pass"] and not checks.get("blocked")

    # 分件文件改过 → 重新编译交付用的整稿，避免「终审改了正文、定稿文件还是旧的」
    if score.get("rewritten_immediate") or rewritten:
        try:
            _refresh_compiled(proj, mod, med, plan)
        except Exception as e:
            proj.log(f"⚠️ 终审定稿重编译失败（分件已更新）：{e}")

    result = {
        "ok": True, "at": _now(), "medium": med, "label": medium.label(med),
        "policy": pol, "words": words, "text_sha": _sha(full),
        "scores": {"pieces": score["pieces"], "piece_avg": piece_avg, "whole": whole,
                   "reused": 0, "fresh": len(score["pieces"])},
        "book": {"total": book_total, "pass_line": pol["book_pass"],
                 "structure_ok": structure_ok},
        "checks": checks, "deferred_decision": deferred_decision,
        "rewritten_deferred": rewritten, "finished_at": _now(),
    }
    _write_report_other(proj, med, result)
    try:
        p = proj.plan() or {}
        p.setdefault("meta", {})["final_review"] = result
        proj.save_plan(p)
    except Exception:
        pass
    try:
        from . import journal as _j
        _j.finish(proj.path, "done", note="成书终审完成")
    except Exception:
        pass
    _emit(key, "editor", "成书终审完成",
          output=f"{medium.label(med)} · 全篇 {book_total} 分 · "
                 f"字数{'达标' if words.get('ok') else ('未达标' if words.get('target') else '已统计')}")
    return result


def _refresh_compiled(proj, mod, med: str, plan: dict) -> None:
    """分件改写后重建交付整稿（目前儿童故事/论文有独立定稿文件）。"""
    if med == "children":
        fn = getattr(mod, "_stage_final", None)
        if callable(fn):
            fn(proj, proj.path, plan.get("config") or {}, proj.plan() or plan)
        return
    if med == "thesis":
        body_dir = os.path.join(proj.path, "02-正文")
        parts = [f"# {plan.get('thesisName') or '论文'}\n\n## 摘要\n\n"
                 f"{(plan.get('outline') or {}).get('abstract') or ''}\n"]
        if os.path.isdir(body_dir):
            for fn in sorted(os.listdir(body_dir)):
                if fn.endswith(".md"):
                    parts.append(f"\n\n---\n\n# {fn[:-3]}\n\n" + _read(os.path.join(body_dir, fn)))
        parts.append("\n\n---\n\n" + _read(os.path.join(proj.path, "01-参考文献.md")))
        _write(os.path.join(proj.path, "04-论文定稿.md"), "".join(parts))


def _other_words(proj, pieces, plan, med, full, target, cfg) -> dict:
    """全篇字数达标核验。target=0 表示未设目标，只统计不判达标。"""
    floor = int(target * 0.9) if target else 0
    actual = _cn(full)
    return {
        "target": target, "floor": floor,
        "total": 1,
        "ok": (1 if (target and actual >= floor) else 0),
        "actual": actual,
        "note": ("剧本按每页约 350 字估算总目标" if med == "script" and target else
                 ("未设置目标字数，仅统计实际字数" if not target else "")),
    }


def _rubric_score(text: str, med: str, cfg: dict, scope: str = "") -> dict:
    dims = medium.score_dims(med)
    if not dims:
        return {"scores": {}, "total": None, "weak": [], "fixes": [], "verdict": "无法评分"}
    prompt = (
        f"给下面这份{medium.label(med)}{scope}打分（百分制）。维度与权重：\n"
        + "\n".join(f"- {n}：{w} 分" for n, w in dims) + "\n\n"
        "严格评分：85 以上意味着可以直接交付。只输出 JSON：\n"
        '{"scores":{' + ",".join(f'"{n}":0' for n, _ in dims) + '},'
        '"weak":["最弱维度"],"verdict":"一句话总评","fixes":["最重要的三条修改建议"],'
        '"logic_issues":["具体的逻辑/衔接问题，最多 5 条"]}\n\n'
        f"正文：\n{text}"
    )
    try:
        out = llm.chat_json([{"role": "system", "content": "你是严格且具体的审稿人。"},
                             {"role": "user", "content": prompt}], cfg=cfg, stage="review")
    except Exception:
        out = {}
    if not isinstance(out, dict):
        return {"scores": {}, "total": None, "weak": [], "fixes": [],
                "verdict": "评分失败", "logic_issues": []}
    total = 0.0
    got = out.get("scores") or {}
    for n, w in dims:
        try:
            total += (float(got.get(n)) / 10.0) * w
        except (TypeError, ValueError):
            pass
    out["total"] = round(total, 1)
    out["pass_line"] = medium.pass_line(med)
    return out


def _other_score(proj, plan, mod, med, cfg, pol, key) -> dict:
    """分件评分：剧本按场、论文按章、儿童按单元、单篇按整篇。"""
    pieces = []
    if med == "script":
        body_dir = os.path.join(proj.path, getattr(mod, "F_BODY", "04-正文"))
        for sc in plan.get("scenes") or []:
            no = int(sc.get("no") or 0)
            fp = os.path.join(body_dir, sc.get("fileName") or f"第{no:03d}场.md")
            pieces.append({"no": no, "title": f"第{no}场", "path": fp, "text": _read(fp)})
    elif med == "thesis":
        body_dir = os.path.join(proj.path, "02-正文")
        if os.path.isdir(body_dir):
            for fn in sorted(os.listdir(body_dir)):
                if fn.endswith(".md"):
                    no = int(re.match(r"(\d+)", fn).group(1)) if re.match(r"(\d+)", fn) else len(pieces) + 1
                    pieces.append({"no": no, "title": fn[:-3],
                                   "path": os.path.join(body_dir, fn), "text": _read(os.path.join(body_dir, fn))})
    elif med == "children":
        body_dir = os.path.join(proj.path, "02-故事")
        if os.path.isdir(body_dir):
            for fn in sorted(os.listdir(body_dir)):
                if fn.endswith(".md"):
                    pieces.append({"no": len(pieces) + 1, "title": fn[:-3],
                                   "path": os.path.join(body_dir, fn), "text": _read(os.path.join(body_dir, fn))})
    if not pieces:
        _, _, full = _pieces_other(key, med, mod)
        pieces = [{"no": 1, "title": "全篇", "path": "", "text": full}]

    immediate, deferred, rewritten_immediate = [], [], []
    for p in pieces:
        if not p["text"].strip():
            p["score"] = None
            continue
        sc = _rubric_score(p["text"][:12000], med, cfg, scope=p["title"])
        p["score"] = sc.get("total")
        p["weak"] = sc.get("weak") or []
        p["fixes"] = sc.get("fixes") or []
        try:
            val = float(sc.get("total"))
        except (TypeError, ValueError):
            continue
        if val < pol["immediate"]:
            immediate.append(p["no"])
            if _rewrite_piece(proj, mod, med, p, cfg, pol["immediate"]):
                rewritten_immediate.append(p["no"])
        elif val < pol["defer"]:
            deferred.append(p["no"])
    return {"pieces": pieces, "immediate": immediate, "deferred": deferred,
            "rewritten_immediate": rewritten_immediate}


def _rewrite_piece(proj, mod, med: str, p: dict, cfg: dict, pass_line: int) -> bool:
    """单件定向重写：改完重新评分，只有真的提分才落盘，否则保留原稿。"""
    old = p.get("text") or ""
    if not old.strip() or not p.get("path"):
        return False
    old_score = p.get("score")
    problems = "、".join(p.get("weak") or []) or "整体质量"
    fixes = "\n".join(f"- {x}" for x in (p.get("fixes") or [])[:5])
    try:
        if med == "script":
            scenes = (proj.plan() or {}).get("scenes") or []
            sc = next((x for x in scenes if int(x.get("no") or 0) == int(p.get("no") or 0)), None)
            if not sc:
                return False
            extra = (f"修订要求：短板在【{problems}】。逐条落实：\n{fixes}\n"
                     "保持场次功能、人物与情节不变，只做针对性改写。")
            new = mod.gen_scene_body(sc, extra, cfg)
            new = purity.sanitize_chapter(new) if new else ""
        else:
            prompt = (
                f"下面这份{medium.label(med)}的短板是【{problems}】。逐条落实修改：\n{fixes}\n\n"
                "要求：保持原有信息、事实与结构，只解决上述问题；不要解释、不要加点评；"
                "直接输出改写后的完整正文。\n\n" + old[:16000]
            )
            new = llm.chat([{"role": "system", "content": "你是严格按问题清单改稿的编辑。"},
                            {"role": "user", "content": prompt}], cfg=cfg, stage="review")
            new = (new or "").strip()
    except Exception:
        return False
    if not new or len(new) < len(old) * 0.5:
        return False
    # 提分才保留；LLM 评分有波动，没提分就当这次改写没发生
    try:
        cand = _rubric_score(new[:16000], med, cfg, scope=p.get("title") or "").get("total")
        if old_score is not None and (cand is None or float(cand) <= float(old_score)):
            return False
    except Exception:
        return False
    try:
        from .safeio import atomic_write_text
        with open(p["path"], encoding="utf-8") as f:
            before = f.read()
        try:
            versions.snapshot(proj.path, int(p.get("no") or 0), before, label="终审改写前")
        except Exception:
            pass
        atomic_write_text(p["path"], new.strip() + "\n")
    except Exception:
        return False
    p["text"] = new
    p["score"] = cand
    return True


def _other_checks(proj, plan, mod, med, full, cfg) -> dict:
    """全篇校验：剧本时代/格式、诗词格律、论文引用；其余用全篇逻辑问题。失败不阻断。"""
    issues = []
    blocked = False
    try:
        if med == "script":
            setting = (plan.get("meta") or {}).get("setting")
            era = setting.get("era") if isinstance(setting, dict) else None
            chk = mod.script_checker.check_text(full, era)
            issues = chk.get("issues") or []
            blocked = not chk.get("ok", True)
        elif med == "poetry":
            repo = mod.check_draft(full, plan.get("form") or "modern")
            issues = [f for f in (repo.get("findings") or [])
                      if f.get("severity") in ("fail", "unclear", "advisory")]
            blocked = (repo.get("verdict") == "fail") or int((repo.get("counts") or {}).get("fail") or 0) > 0
        elif med == "thesis":
            issues = mod.check_citations(plan, proj.path) or []
            blocked = bool(issues)
    except Exception as e:
        issues = [{"error": f"确定性校验失败：{type(e).__name__}"}]
    return {"issues": issues[:40], "blocked": blocked}


def _write_report_other(proj, med: str, result: dict) -> None:
    s = result["scores"]
    w = result["words"]
    b = result["book"]
    lines = [
        f"# 成书终审 · {medium.label(med)}",
        f"> {result['at']} · 策略：<{result['policy']['immediate']} 立即重写；"
        f"{result['policy']['immediate']}–{result['policy']['defer']} 待全篇总评定；"
        f"总评 ≥{result['policy']['book_pass']} 且校验通过则不改",
        "",
        "## ① 字数",
        f"- 目标 {w['target'] or '未设置'} 字；实际 {w['actual']} 字"
        + (f"（{w['note']}）" if w.get("note") else ""),
        "",
        "## ② 分件评分与重写",
        f"- 全篇总评 **{b['total']}** / 100；分件均分 {s.get('piece_avg')}",
        f"- 低于 {result['policy']['immediate']} 分立即重写：见下方分件表",
        f"- {result['policy']['immediate']}–{result['policy']['defer']} 分："
        + ("不改（全篇与校验通过）" if result["deferred_decision"] == "keep"
           else ("已定向重写 " + "、".join(map(str, result["rewritten_deferred"]))
                 if result["rewritten_deferred"] else "未改")),
        "",
        "| 件 | 分值 | 薄弱维度 |",
        "|---|---|---|",
    ]
    for p in s.get("pieces") or []:
        lines.append(f"| {p.get('title')} | {p.get('score') if p.get('score') is not None else '—'} "
                     f"| {'、'.join(p.get('weak') or []) or '—'} |")
    lines += [
        "",
        "## ③ 全篇校验",
        f"- 结构/衔接：{'可接受' if b.get('structure_ok') else '需要处理'}",
        f"- 确定性校验命中 {len(result['checks'].get('issues') or [])} 条"
        + ("（存在阻断项）" if result["checks"].get("blocked") else ""),
    ]
    for it in (result["checks"].get("issues") or [])[:12]:
        lines.append(f"  - {json.dumps(it, ensure_ascii=False)[:180]}")
    _logic = (s.get("whole") or {}).get("logic_issues") or []
    if _logic:
        lines.append(f"- 全篇逻辑/衔接问题 {len(_logic)} 条（LLM 审校，供修改参考）：")
        for it in _logic[:8]:
            lines.append(f"  - {str(it)[:200]}")
    lines += [
        "",
        "## 说明（不粉饰）",
        "- 剧本评分按「场」抽取，单场超过模型窗口会截断（12000 字符）——超长场次的结构问题以 07-质量报告 与人工通读为准。",
        "- 诗词格律、剧本时代、论文引用是确定性校验；其余分件评分是 LLM 判断，不等于人工终审。",
        "- 重写只保留更有把握的版本；无法判定提分时不落盘（避免改坏）。",
        "",
        "_模板：final-review（一幕·一幕·YIMU 神笔）。_",
    ]
    _write(os.path.join(proj.path, "15-成书终审.md"), "\n".join(lines) + "\n")
