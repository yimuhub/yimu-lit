"""v4.24.4：一致性巡检器（Consistency Audit）——定期「坑盘点 + 矛盾扫描 + LLM 仲裁」。

为什么单独一个模块：伏笔账本（19-伏笔账本.json）一直在记录（每条带埋设章号/回收窗口/
状态），但记了没人管——写完 50 章时 227 条 open 伏笔里多少超期了没人知道。本模块补上
「反馈环」：每 N 章（或手动）跑一次巡检，产出修复计划（必修矛盾 / 该填的坑 / 可留观察），
修复计划写进 plan.meta.fill_plan，由写作提示置顶注入，让作家在未来的章节里把坑填上。

触发：
  - 自动：writer._write_loop 每完成 audit_every 章（默认 10，可配 consistency.audit_every）
  - 手动：POST /api/consistency-audit/run；对话「检查一致性」（规划器动作 consistency_audit）

产物：
  - 22-巡检报告.json（覆盖写，供 UI 查看）
  - plan.meta.fill_plan（修复计划，写前注入 + 完成验证）
  - 创作活动流事件（audit 岗位）
"""
import json, os, threading, time

_RUNNING = {}   # key -> True（防重入）

#: 伏笔悬置多少章算「长开未收」（无 due_to 时用）
_LONG_OPEN_AGE = 15


def _ledger(pdir) -> dict:
    try:
        d = json.load(open(os.path.join(pdir, "19-伏笔账本.json"), encoding="utf-8"))
        return d.get("items") or {}
    except Exception:
        return {}


def pit_inventory(pdir, current_chapter) -> dict:
    """伏笔盘点：open 坑按悬置状态分类（纯确定性，零 LLM）。"""
    overdue, due_soon, long_open, fresh = [], [], [], []
    for pit, v in _ledger(pdir).items():
        if not isinstance(v, dict) or str(v.get("status") or "open").lower() != "open":
            continue
        setup = int(v.get("setup_chapter") or 0)
        due_to = int(v.get("due_to") or 0)
        age = max(0, int(current_chapter or 0) - setup)
        rec = {"pit": str(pit), "description": str(v.get("description") or "")[:80],
               "setup_chapter": setup, "due_from": int(v.get("due_from") or 0),
               "due_to": due_to, "age": age, "importance": str(v.get("importance") or "")}
        if due_to and current_chapter > due_to:
            rec["over_by"] = current_chapter - due_to
            overdue.append(rec)
        elif due_to and current_chapter >= int(v.get("due_from") or 0):
            due_soon.append(rec)
        elif age >= _LONG_OPEN_AGE:
            long_open.append(rec)
        else:
            fresh.append(rec)
    _srt = lambda lst: sorted(lst, key=lambda x: (-x["age"], x["pit"]))
    return {"overdue": _srt(overdue), "due_soon": _srt(due_soon),
            "long_open": _srt(long_open), "fresh": fresh,
            "counts": {"open": len(overdue) + len(due_soon) + len(long_open) + len(fresh),
                       "overdue": len(overdue), "due_soon": len(due_soon),
                       "long_open": len(long_open), "fresh": len(fresh)}}


def read_report(key) -> dict | None:
    try:
        from bot.writer import _project_dir
        p = os.path.join(_project_dir(key), "22-巡检报告.json")
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))
    except Exception:
        pass
    return None


def _save_report(key, report):
    try:
        from bot.writer import _project_dir
        from bot.safeio import atomic_write_json
        atomic_write_json(os.path.join(_project_dir(key), "22-巡检报告.json"), report)
    except Exception:
        pass


def _recent_summaries(pdir, upto, n=3) -> str:
    try:
        rows = {}
        p = os.path.join(pdir, "21-章摘要.jsonl")
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    rows[int(d.get("n") or 0)] = str(d.get("summary") or "")[:150]
                except Exception:
                    continue
        return "\n".join(f"第{k}章：{rows[k]}" for k in sorted(rows, reverse=True)[:n] if rows.get(k))
    except Exception:
        return ""


def _llm_arbitrate(key, proj, findings, pits, current_chapter) -> dict | None:
    """LLM 仲裁（审稿人岗）：把确定性扫描结果 + 超期坑分类成 修复计划。"""
    try:
        from bot import llm
        findings_txt = "\n".join(f"- [{f.get('severity')}] {f.get('location')}：{f.get('detail')}"
                                 for f in (findings or [])[:20]) or "（确定性扫描未发现硬伤）"
        pits_txt = "\n".join(
            f"- 「{p['pit']}」第{p['setup_chapter']}章埋设，已悬置{p['age']}章"
            f"{('，原计划第' + str(p['due_to']) + '章前兑现，已超期' + str(p['over_by']) + '章') if p.get('over_by') else ''}"
            f"：{p['description']}"
            for p in (pits.get('overdue') or [])[:12]) or "（无超期坑）"
        due_soon_txt = "\n".join(f"- 「{p['pit']}」（窗口 {p['due_from']}–{p['due_to']} 章）"
                                 for p in (pits.get('due_soon') or [])[:10]) or "（无）"
        summaries = _recent_summaries(proj.path, current_chapter)
        sys_p = (
            "你是小说的一致性仲裁 Agent。系统已对全书做了确定性扫描（连续性检查）与伏笔盘点，"
            "你的职责是把机器发现的问题**分类并给出具体修法**，让作家在未来的章节里把坑填上。\\n"
            "输出严格 JSON：{\\n"
            '  "must_fix": [{"issue": "矛盾描述", "chapters": [涉及章号], "fix_hint": "具体修法（重写哪章的哪段/怎么改）"}],\\n'
            '  "fill_plan": [{"pit": "坑名", "fill_by": 目标章号, "hint": "给作家的回收指引（一句话）"}],\\n'
            '  "watch": ["可留观察的事项"]\\n}\\n'
            "分类原则：\\n"
            "- 硬矛盾（人物/设定/时间线冲突，读者能察觉）→ must_fix\\n"
            "- 超期未收且该收的坑 → fill_plan（fill_by = 建议在未来 1-3 章内回收）\\n"
            "- 次要/可自然延后的 → watch\\n"
            "必须结合最近章节摘要判断——已经回收的坑不要再列。"
        )
        user_p = (f"当前写到第 {current_chapter} 章。\\n\\n【确定性扫描发现】\\n{findings_txt}\\n\\n"
                  f"【超期未收的坑】\\n{pits_txt}\\n\\n【即将到期】\\n{due_soon_txt}\\n\\n"
                  f"【最近章节摘要】\\n{summaries}")
        res = llm.chat_json([{"role": "system", "content": sys_p},
                             {"role": "user", "content": user_p}],
                            cfg=None, stage="review", agent="reviewer", temperature=0.2)
        if isinstance(res, dict):
            return {"must_fix": res.get("must_fix") or [],
                    "fill_plan": res.get("fill_plan") or [],
                    "watch": res.get("watch") or []}
    except Exception:
        pass
    return None


def run_audit(key, trigger="manual"):
    """跑一次一致性巡检。返回报告 dict；已在跑返回 None。"""
    if _RUNNING.get(key):
        return None
    _RUNNING[key] = True
    try:
        from bot import continuity, journal, steps as _steps
        from bot.writer import Project, _project_dir, _pass_line
        try:
            from bot import steps as _steps
        except Exception:
            _steps = None
        pdir = _project_dir(key)
        proj = Project(pdir, key)
        plan = proj.plan() or {}
        chapters = plan.get("chapters") or []
        current = max([int(c.get("chapterNumber") or 0) for c in chapters
                       if c.get("status") == "completed"] or [0])
        proj.log(f"🔍 一致性巡检启动（{trigger}）：扫描 {current} 章 + 伏笔盘点…")
        if _steps:
            try:
                _steps.emit(key, "system", "start", title="一致性巡检",
                            thinking="确定性扫描连续性 + 伏笔超期盘点，审稿人仲裁分类",
                            output=f"扫描 {current} 章")
            except Exception:
                pass

        # ① 确定性扫描
        try:
            l1 = continuity.run_project(pdir)
            findings = (l1 or {}).get("findings") or []
        except Exception:
            findings = []

        # ② 伏笔盘点
        pits = pit_inventory(pdir, current)

        # ③ LLM 仲裁
        verdict = _llm_arbitrate(key, proj, findings, pits, current) or {}
        must_fix = verdict.get("must_fix") or []
        fill_plan = verdict.get("fill_plan") or []
        watch = verdict.get("watch") or []

        report = {"ts": int(time.time()),
                  "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "trigger": trigger, "chapter_at": current,
                  "pass_line": _pass_line(),
                  "continuity_findings": findings[:30],
                  "pits": pits, "must_fix": must_fix, "fill_plan": fill_plan,
                  "watch": watch,
                  "stats": {"findings": len(findings),
                            "pits": pits.get("counts", {}),
                            "must_fix": len(must_fix), "fill_plan": len(fill_plan)}}

        # ④ 报告落盘 + 修复计划进 plan.meta（写前注入用）
        _save_report(key, report)
        try:
            with proj.lock:
                pl = proj.plan() or {}
                pl.setdefault("meta", {})
                pl["meta"]["fill_plan"] = [
                    {"pit": f.get("pit"), "fill_by": f.get("fill_by"),
                     "hint": f.get("hint"), "ts": int(time.time())}
                    for f in fill_plan if isinstance(f, dict) and f.get("pit")]
                proj.save_plan(pl)
        except Exception:
            pass

        # ⑤ 活动流 + 日志
        summary = (f"硬矛盾 {len(must_fix)} 项 · 待填坑 {len(fill_plan)} 个 · "
                   f"观察 {len(watch)} 项 · 超期伏笔 {pits.get('counts', {}).get('overdue', 0)} 个")
        proj.log(f"🔍 一致性巡检完成（{trigger}）：{summary}")
        if _steps:
            try:
                _steps.emit(key, "system", "done", title="一致性巡检完成",
                            chapter=0, output=summary)
            except Exception:
                pass
        try:
            journal.remember(proj.path, "system", f"一致性巡检：{summary}")
        except Exception:
            pass
        return report
    except Exception:
        return None
    finally:
        _RUNNING.pop(key, None)


def maybe_auto(key, done_chapters):
    """写作循环调用：每 audit_every 章自动触发一次（异步，绝不阻塞写作）。"""
    try:
        from bot.writer import _cfg_defaults
        d = _cfg_defaults()
        every = int((d.get("consistency") or {}).get("audit_every") or 10)
        if every <= 0 or done_chapters <= 0 or done_chapters % every != 0:
            return
        if _RUNNING.get(key):
            return
        threading.Thread(target=run_audit, args=(key, "auto"), daemon=True).start()
    except Exception:
        pass
