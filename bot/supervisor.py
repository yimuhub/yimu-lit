"""总编辑 Agent（v4.26.0）——每本书一个常驻监制看护线程。

定位：不是「写得更快的作者」，是监制——对整本书的创作过程负责到交成品：
  看（确定性体检，0 token）：停滞/孤儿线程、每章评分与字数、空章、超期坑、大纲耗尽；
  判（决策规则写死，不摇摆）：下表全部确定性规则，LLM 只在被派活时参与；
  派（复用既有修复机制，自己不造引擎）：断点恢复 / 逐章评分 / 并行改写 / 扩写 / 巡检 / 重规划；
  报（全程活动流可见）：以「总编辑」岗位在对话流发言，用户随时看得见它在干什么、为什么。

自动化程度（config.defaults.supervisor_autonomy）：
  guarded（默认）：执行类修复全自动（恢复/评分/改写/扩写/巡检）；结构级重规划只生成提案等确认。
  auto（全自动）：低风险重规划提案自动应用；结构级（requires_past_rewrite）仍生成提案等确认。

断路器（防失控空烧 token）：
  · 停滞自动恢复：每本书 24h 内最多 3 次，超预算转「需要你决策」卡片；
  · 单章自动重写：每章每次监制周期最多 1 轮派发（沿用改写自身每章 ≤2 轮上限）；
  · 用户暂停（meta.paused）绝对不自动恢复——尊重人第一；
  · 极速模式（config.express）只管「别停」，不派质量改写（极速=初稿契约）。

接入点：writer.mark_pipeline 的 0→1 / →0 收口自动上下车（九文体通用，引擎零改动）。
"""

from __future__ import annotations

import json
import os
import threading
import time

from .safeio import atomic_write_json

_TICK_INTERVAL = 75        # 常态体检间隔（秒）
_FIRST_TICK_DELAY = 8      # 首检延迟：给引擎线程留出落盘时间
_STOP_DELAY = 6            # 流水线归零后延迟判定（让 finally 收尾落盘）
_RESUME_BUDGET = 3         # 24h 停滞自动恢复上限
_RESUME_WINDOW = 24 * 3600
_RESUME_MIN_GAP = 30 * 60  # 两次自动恢复最小间隔（防引擎实际在跑时双开）
_NN_STALL_SECS = 900       # 非小说引擎：活动流静默超 15 分钟视为停滞（每个阶段/场次都会发事件）

# 测试模式：conftest 置 True → ensure() 不再孵化看护线程（tick_once 仍可直接调用）
TEST_MODE = False

_WATCHDOGS: dict[str, "Supervisor"] = {}
_REG_LOCK = threading.Lock()


def _evt(key, phase, title, output="", chapter=0, thinking=""):
    try:
        from . import steps
        steps.emit(key, "supervisor", phase, title=title, output=output,
                   chapter=chapter, thinking=thinking)
    except Exception:
        pass


def autonomy_mode() -> str:
    """guarded（默认，结构级找确认）| auto（低风险结构级也自动）。"""
    try:
        from . import writer as _wr
        return str((_wr._cfg_defaults() or {}).get("supervisor_autonomy") or "guarded").strip().lower()
    except Exception:
        return "guarded"


def express_on(cfg: dict) -> bool:
    try:
        from . import express
        return bool(express.on(cfg or {}))
    except Exception:
        return False


# ---------------------------------------------------------------- 计划读写（全文体）

def _load(key: str):
    """(med, pdir, fp, plan) —— med/计划文件路径/项目目录/计划内容，全文体通用。
    v4.26.0 修复：此前把 _plan_of 的第二个返回值（计划文件路径）误当项目目录用，
    导致体检读章节文件、写回计划全部落在错误路径上（空章检测因此失效）。"""
    from . import writer as _wr
    try:
        pdir = _wr._project_dir(key)
    except Exception:
        return "", "", "", {}
    med, fp, plan = _wr._plan_of(pdir)
    return med, pdir, fp, plan


def _save_budget(key: str, plan: dict, med: str, pdir: str) -> None:
    """把断路器账目写回计划 meta.supervisor（按书串行，防与引擎线程互相覆盖）。"""
    try:
        from . import writer as _wr
        meta = plan.setdefault("meta", {})
        meta["supervisor"] = plan.get("meta", {}).get("supervisor") or meta.get("supervisor") or {}
        with _wr._acq_book(key):
            # 重读合并：只更新 supervisor 字段，避免覆盖引擎线程刚写的其他字段
            _med2, _pdir2, _fp2, plan2 = _load(key)
            if not plan2:
                plan2 = plan
            plan2.setdefault("meta", {})["supervisor"] = meta.get("supervisor") or {}
            atomic_write_json(_fp2, plan2)
    except Exception:
        pass


def _engine_running(key: str, med: str) -> bool:
    """引擎是否真的在跑（停滞检测的唯一判据）：
    · 小说：pipeline_running ∪ _book_running（两条注册表都要查——v4.26.0 之前小说主链路
      只登记后者，只查前者会把「正在写」误判成停滞，造成双开流水线的事故）；
    · 非小说：无线程注册表，用活动流新鲜度——引擎每个阶段/场次都发事件，静默超阈值即停滞。"""
    from . import writer as _wr
    from . import steps as _steps
    if med in ("", "novel"):
        return bool(_wr.pipeline_running(key)
                    or _wr._book_running(os.path.basename(str(key or ""))))
    last = _steps.last_event_ts(key)
    return bool(last) and (time.time() - last) < _NN_STALL_SECS


def _budget(key: str, plan: dict) -> dict:
    meta = plan.get("meta") or {}
    b = meta.get("supervisor")
    return b if isinstance(b, dict) else {}


def _budget_add(key: str, plan: dict, med: str, pdir: str, field: str, value) -> dict:
    b = _budget(key, plan)
    if field == "resumes":
        b["resumes"] = [t for t in (b.get("resumes") or []) if time.time() - float(t) < _RESUME_WINDOW]
        b["resumes"] = (b.get("resumes") or []) + [time.time()]
    else:
        d = b.get(field) if isinstance(b.get(field), dict) else {}
        d[str(value)] = int(d.get(str(value)) or 0) + 1
        b[field] = d
    plan.setdefault("meta", {})["supervisor"] = b
    _save_budget(key, plan, med, pdir)
    return b


# ---------------------------------------------------------------- 派活（全部复用既有机制）

def _dispatch_resume(key: str, med: str, pdir: str) -> bool:
    """停滞自愈：断点恢复（复用 tasks.resume / 各引擎 resume_project）。"""
    import threading as _th
    if med in ("", "novel"):
        from . import writer as _wr
        proj = _wr.Project(pdir, key)
        ok = _wr.resume_book(key)
        return bool(ok)
    mod = _nonnovel_module(med)
    if mod is None or not hasattr(mod, "resume_project"):
        return False
    _th.Thread(target=lambda: _safe_call(lambda: mod.resume_project(key)),
               name=f"sv-resume-{key}", daemon=True).start()
    return True


def _nonnovel_module(med: str):
    mapping = {"script": "script_writer", "poetry": "poetry_writer", "prose": "prose_writer",
               "thesis": "thesis_writer", "children": "children_writer", "news": "news_writer",
               "copy": "copy_writer", "other": "other_writer"}
    name = mapping.get(med)
    if not name:
        return None
    import importlib
    try:
        return importlib.import_module(f".{name}", __package__)
    except Exception:
        return None


def _safe_call(fn):
    try:
        return fn()
    except Exception as e:
        print(f"[supervisor] 派活异常: {type(e).__name__}: {e}", flush=True)


def _dispatch_score(key: str, med: str, pdir: str, no: int) -> None:
    """补评分：异步线程直接调 writer.score_chapter（含 llm.book 记账）。"""
    import threading as _th
    from . import writer as _wr
    if med not in ("", "novel"):
        return   # 非小说评分由各自引擎管，总编辑不越权
    def _job():
        proj = _wr.Project(pdir, key)
        _wr.llm.book(os.path.basename(pdir), "review")
        try:
            _wr.score_chapter(proj, no)
        finally:
            _wr.llm.set_chapter(None)
    _th.Thread(target=_job, name=f"sv-score-{key}-{no}", daemon=True).start()


def _dispatch_rewrite(key: str, med: str, pdir: str, chapters: list[int]) -> bool:
    """低分章定向重写：复用 run_parallel_rewrite（保留更好守护自带回滚）。"""
    import threading as _th
    from . import writer as _wr
    if med not in ("", "novel"):
        return False
    prog = getattr(_wr, "_PARA_RW_PROGRESS", {}).get(key)
    if isinstance(prog, dict) and prog.get("running"):
        return False   # 已有改写在跑，不叠加
    _th.Thread(target=lambda: _safe_call(
        lambda: _wr.run_parallel_rewrite(key, chapters=chapters, workers=3, max_rounds=2)),
        name=f"sv-rewrite-{key}", daemon=True).start()
    return True


def _dispatch_audit(key: str) -> None:
    try:
        from . import audit
        if not audit._RUNNING.get(key):
            threading.Thread(target=lambda: _safe_call(lambda: audit.run_audit(key, "supervisor")),
                             name=f"sv-audit-{key}", daemon=True).start()
    except Exception:
        pass


def _dispatch_replan(key: str, med: str, pdir: str, plan: dict) -> None:
    """大纲耗尽/规划不足 → 重规划提案。guarded 只出提案等确认；auto 自动应用低风险提案。"""
    try:
        from . import longform, writer as _wr
        proj = _wr.Project(pdir, key)
        mode = autonomy_mode()
        _evt(key, "start", "规划即将耗尽，生成续写方案",
             output=f"已完成章数接近大纲规划上限，按「{('全自动' if mode == 'auto' else '低自动（结构级找你确认）')}」模式重规划",
             thinking="续写质量取决于规划先行的纪律：先出提案，再按自动化程度决定是否直接应用")
        proposal = longform.replan(proj, scope="window", reason="supervisor:outline_exhausted")
        pid = (proposal or {}).get("id")
        if not pid:
            return
        if mode == "auto" and not (proposal or {}).get("requires_past_rewrite"):
            try:
                longform.apply_replan(proj, pid, allow_past=False)
                _evt(key, "done", "重规划提案已自动应用（全自动模式）",
                     output=f"提案 {pid} 已应用，创作继续")
                return
            except Exception as e:
                _evt(key, "warn", "重规划提案自动应用失败，转人工确认", output=str(e)[:120])
        _evt(key, "warn", "重规划提案已生成，等你确认",
             output=f"提案 {pid}：在右栏「规划」或对话里说「应用重规划提案」即可；结构级改动不自动应用")
    except Exception as e:
        _evt(key, "warn", "重规划失败", output=str(e)[:120])


# ---------------------------------------------------------------- 体检项（确定性，0 token）

def _check_chapter_gate(key, med, pdir, plan, cfg) -> None:
    """每章闸门：最近完成章的 评分/字数/空章。小说链路；极速模式跳质量派活。"""
    from . import writer as _wr
    if med not in ("", "novel"):
        return
    if express_on(cfg):
        return
    chs = plan.get("chapters") or []
    done = [c for c in chs if c.get("status") == "completed"]
    if not done:
        return
    scores = {}
    try:
        scores = {p.get("ch"): p for p in (_wr.per_chapter_scores(_wr.Project(pdir, key)) or [])}
    except Exception:
        pass
    pass_line = _wr._pass_line()
    floor = max(1, int(plan.get("minWordsPerChapter") or 3000))
    budget = _budget(key, plan).get("rewrite") or {}
    for ch in sorted(done, key=lambda c: int(c.get("chapterNumber") or 0), reverse=True)[:3]:
        no = int(ch.get("chapterNumber") or 0)
        if not no:
            continue
        # ① 空章/字数验收：文件缺失或正文 <70% 线 → 打回 pending 自动重写
        body = ""
        try:
            fp = ch.get("filePath") and os.path.join(pdir, os.path.basename(ch["filePath"]))
            body = open(fp, encoding="utf-8").read() if fp and os.path.exists(fp) else ""
        except Exception:
            body = ""
        wc = len(body) if body else int(ch.get("wordCount") or 0)
        # 以实际正文为准——wordCount 字段可能与文件脱节（写盘中断/引擎误标），
        # 空章检测不许被陈旧的 wordCount 骗过
        if wc < floor * 0.7:
            _reset_pending_chapter(key, med, pdir, no)
            _evt(key, "warn", f"第 {no} 章是空章/字数严重不足（{wc} 字 < 验收线 {int(floor * 0.7)}）",
                 output="已打回待写，正在重新创作本章", chapter=no)
            return   # 一轮只处理一章，避免连环打回
        # ② 未评分 → 派评分
        if no not in scores:
            _dispatch_score(key, med, pdir, no)
            _evt(key, "start", f"第 {no} 章已完成但还没有评分，已派审稿人补评", chapter=no)
            return
        # ③ 低分 → 派定向重写（每章一次，已有改写在跑则不叠加）
        sc = (scores.get(no) or {}).get("score")
        if isinstance(sc, (int, float)) and sc < pass_line and not int(budget.get(str(no)) or 0):
            if _dispatch_rewrite(key, med, pdir, [no]):
                _budget_add(key, plan, med, pdir, "rewrite", no)
                _evt(key, "start",
                     f"第 {no} 章 {sc} 分（< 达标线 {pass_line}），已派 3 岗位定向重写",
                     output="改写分必须高于原分，否则自动回滚原稿——质量只进不退", chapter=no)
                return


def _reset_pending_chapter(key: str, med: str, pdir: str, no: int) -> None:
    from . import writer as _wr
    try:
        with _wr._acq_book(key):
            _med2, _pdir2, fp2, plan2 = _load(key)
            if not plan2:
                return
            for c in plan2.get("chapters") or []:
                if int(c.get("chapterNumber") or 0) == no:
                    c["status"] = "pending"
                    c["wordCount"] = None
                    break
            plan2["status"] = "in_progress"
            atomic_write_json(fp2, plan2)
    except Exception:
        pass


def _check_pits(key, med, pdir, plan) -> None:
    """超期坑盯梢：有超期未收的坑且最近没跑过巡检 → 派巡检（自带防重入）。"""
    try:
        from . import audit
        rep = audit.read_report(key) or {}
        pits = rep.get("pits") or rep.get("pit_inventory") or {}
        overdue = 0
        if isinstance(pits, dict):
            for v in pits.values():
                if isinstance(v, dict) and v.get("status") == "open" and v.get("overdue"):
                    overdue += 1
        elif isinstance(pits, list):
            overdue = sum(1 for v in pits if isinstance(v, dict) and v.get("overdue"))
        if overdue:
            last = (_budget(key, plan).get("audits") or [])
            if not last or time.time() - float(last[-1]) > 6 * 3600:
                _dispatch_audit(key)
                _budget_add(key, plan, med, pdir, "audits", "last")
                _evt(key, "start", f"发现 {overdue} 个超期未回收的坑，已派一致性巡检",
                     output="巡检会给超期坑排修复计划：必修的派定向改写，该填的注入未来章节")
    except Exception:
        pass


def _check_outline_exhausted(key, med, pdir, plan, cfg) -> None:
    """规划不足：已完成章数达到大纲章节数但全书未完 → 重规划。"""
    if med not in ("", "novel"):
        return
    total = int(plan.get("totalChapters") or 0)
    chs = plan.get("chapters") or []
    if not total or not chs:
        return
    done = sum(1 for c in chs if c.get("status") == "completed")
    if done >= len(chs) and len(chs) < total:
        b = _budget(key, plan)
        if not (b.get("replanned_at") and time.time() - float(b["replanned_at"]) < 12 * 3600):
            _budget_add(key, plan, med, pdir, "replanned_at", "once")
            plan.setdefault("meta", {})["supervisor"] = _budget(key, plan)
            _dispatch_replan(key, med, pdir, plan)


def _final_acceptance(key, med, pdir, plan) -> None:
    """完稿终审：全章完成 → 汇总质量账，宣布成品就绪（不重复跑门禁，只验收汇报）。"""
    try:
        from . import writer as _wr
        chs = plan.get("chapters") or []
        done = sum(1 for c in chs if c.get("status") == "completed")
        scores = []
        try:
            scores = [p.get("score") for p in (_wr.per_chapter_scores(_wr.Project(pdir, key)) or [])
                      if isinstance(p.get("score"), (int, float))]
        except Exception:
            pass
        avg = round(sum(scores) / len(scores), 1) if scores else None
        words = sum(int(c.get("wordCount") or 0) for c in chs)
        overdue_note = ""
        try:
            from . import audit
            rep = audit.read_report(key) or {}
            pits = rep.get("pits") or rep.get("pit_inventory") or {}
            overdue = 0
            if isinstance(pits, dict):
                overdue = sum(1 for v in pits.values() if isinstance(v, dict) and v.get("status") == "open" and v.get("overdue"))
            elif isinstance(pits, list):
                overdue = sum(1 for v in pits if isinstance(v, dict) and v.get("overdue"))
            if overdue:
                overdue_note = f"；⚠️ 仍有 {overdue} 个超期坑未回收（可在对话里说「检查一致性」补巡检）"
        except Exception:
            pass
        # v4.27.0：完稿即沉淀——本书全部评分数据已就位，自动生成一次进化候选
        try:
            from bot import skill_evo as _se
            if ((_wr._cfg_defaults() or {}).get("skill_evolution") or {}).get("auto_propose", True):
                def _evo():
                    try:
                        _r = _se.propose_from_writing_data("novel", cfg=plan.get("config") or {})
                        if isinstance(_r, dict) and _r.get("ok"):
                            _evt(key, "done", "本书写作数据已沉淀为进化候选技能",
                                 output="到「技能进化」页审阅：评测达标后转正，下一本更好")
                    except Exception:
                        pass
                threading.Thread(target=_evo, name=f"sv-evo-{key}", daemon=True).start()
        except Exception:
            pass
        _evt(key, "done", "完稿终审：成品已就绪",
             output=(f"全书 {done}/{len(chs)} 章完成，约 {words} 字"
                     + (f"，均分 {avg}（达标线 {_wr._pass_line()}）" if avg is not None else "")
                     + overdue_note + "。可在「作品」里导出连载。"),
             thinking="终审只验收汇报，不重复跑放行门禁——门禁由完稿流程自带")
    except Exception as e:
        _evt(key, "warn", "终审汇总异常", output=str(e)[:120])


# ---------------------------------------------------------------- tick（一轮体检）

def tick_once(key: str) -> dict:
    """执行一轮体检（同步、可测试）。返回本轮动作摘要。"""
    med, pdir, _fp, plan = _load(key)
    if not plan:
        stop(key)
        return {"action": "stop", "why": "no_plan"}
    status = str(plan.get("status") or "")
    meta = plan.get("meta") or {}
    cfg = plan.get("config") or {}
    out = {"action": "none"}

    if meta.get("paused"):
        _evt(key, "warn", "创作已暂停（你点的暂停），总编辑待命",
             output="点「继续」我会重新上岗；暂停期间绝不自动恢复", )
        stop(key)
        out.update({"action": "standby", "why": "paused"})
        return out

    if status in ("completed", "failed"):
        if status == "completed":
            _final_acceptance(key, med, pdir, plan)
            out.update({"action": "final_acceptance"})
        stop(key)
        out.update({"why": status})
        return out

    from . import writer as _wr
    running = _engine_running(key, med)

    # ① 停滞自愈：计划说在写、实际没有引擎在跑 → 断点恢复（预算内 + 最小间隔）
    if not running:
        b = _budget(key, plan)
        resumes = [t for t in (b.get("resumes") or []) if time.time() - float(t) < _RESUME_WINDOW]
        if len(resumes) >= _RESUME_BUDGET:
            _evt(key, "warn", "创作停滞，但自动恢复预算已用完（24h 内 3 次）",
                 output="请手动点「继续」；若反复停滞请到「系统日志」看原因")
            out.update({"action": "budget_exhausted"})
            return out
        # v4.26.1b：撤销「30 分钟恢复冷却」——实测有害：服务重启后注册表必然为空
        # （引擎真的没在跑），冷却却把必要的恢复也拦住，书会卡死到冷却过期。
        # 防双开已由「3 次/24h 预算 + 引擎真实状态判据」覆盖，冷却纯冗余。
        ok = _dispatch_resume(key, med, pdir)
        if ok:
            _budget_add(key, plan, med, pdir, "resumes", "x")
            _evt(key, "start", "检测到创作中断，已自动断点恢复",
                 output="（服务重启/线程意外退出都会被我接住——不用你盯着）")
            out.update({"action": "auto_resume"})
        else:
            _evt(key, "warn", "检测到创作停滞，自动恢复失败",
                 output="请手动点「继续」，或查看系统日志")
            out.update({"action": "resume_failed"})
        return out

    # ② 常规体检（线程活着才做；极速模式闸门内部会跳质量派活）
    _check_chapter_gate(key, med, pdir, plan, cfg)
    _check_pits(key, med, pdir, plan)
    _check_outline_exhausted(key, med, pdir, plan, cfg)
    out.update({"action": "checked", "running": True})
    return out


# ---------------------------------------------------------------- 看护线程

class Supervisor:
    def __init__(self, key: str):
        self.key = str(key)
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self._loop, name=f"supervisor-{key}", daemon=True)

    def _loop(self):
        if not self.stop_evt.wait(_FIRST_TICK_DELAY):
            while not self.stop_evt.is_set():
                try:
                    r = tick_once(self.key)
                    if r.get("action") in ("stop", "standby", "final_acceptance",
                                           "budget_exhausted", "resume_failed"):
                        return   # 停机类决定：看护线程退出（继续/恢复会重新 ensure）
                except Exception as e:
                    print(f"[supervisor] tick 异常: {type(e).__name__}: {e}", flush=True)
                if self.stop_evt.wait(_TICK_INTERVAL):
                    return

    def start(self):
        self.thread.start()


def ensure(key: str):
    """流水线上车（0→1）时调用：孵化该书的总编辑看护线程（已有则跳过）。"""
    key = os.path.basename(str(key or ""))
    if not key or TEST_MODE:
        return None
    with _REG_LOCK:
        sup = _WATCHDOGS.get(key)
        if sup is not None and sup.thread.is_alive():
            return sup
        sup = Supervisor(key)
        _WATCHDOGS[key] = sup
    sup.start()
    # 接手宣言：让用户知道从现在起有人在盯（含本书的监制方案）
    med, pdir, _fp, plan = _load(key)
    if plan:
        total = int(plan.get("totalChapters") or len(plan.get("scenes") or []) or 0)
        unit = {"script": "场", "poetry": "轮", "prose": "阶段"}.get(med, "章")
        mode = autonomy_mode()
        _evt(key, "start", "总编辑已接手，统领本书创作到交成品",
             output=(f"全书约 {total or '?'} {unit}；达标线内自动重写、停滞自动恢复、坑账自动盯梢、"
                     f"完稿自动终审——重规划{'低风险自动应用' if mode == 'auto' else '会先给你确认'}。"
                     "过程中我做的每个决定都会在这里汇报。"))
    return sup


def pipeline_stopped(key: str):
    """流水线归零（→0）后延迟判定：暂停待命 / 完稿终审 / 意外停滞自愈。"""
    key = os.path.basename(str(key or ""))
    if not key or TEST_MODE:
        return
    def _later():
        time.sleep(_STOP_DELAY)
        with _REG_LOCK:
            sup = _WATCHDOGS.get(key)
        if sup is None or not sup.thread.is_alive():
            tick_once(key)
    threading.Thread(target=_later, name=f"sv-stopped-{key}", daemon=True).start()


def stop(key: str):
    key = os.path.basename(str(key or ""))
    with _REG_LOCK:
        sup = _WATCHDOGS.pop(key, None)
    if sup is not None:
        sup.stop_evt.set()


def _last_engine_event_ts(ev_path: str) -> float:
    """创作事件 JSONL 里最后一条「非 supervisor」事件的时间戳；无文件/无记录返回 0。"""
    try:
        with open(ev_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-80:]
        best = 0.0
        for ln in reversed(lines):
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if not isinstance(e, dict) or str(e.get("agent") or "") == "supervisor":
                continue
            best = float(e.get("ts") or 0)
            break
        return best
    except Exception:
        return 0.0


def sweep_orphans():
    """开机孤儿扫描：把「最近活跃且计划说在写」的书交给总编辑接手。
    服务重启会杀掉看护线程与写作线程——没有这一步，重启后任何书都只能
    等用户人工发现并点「继续」（用户点名的「突然停了」场景的正解）。

    v4.26.1 紧急收紧：只接手**活动流 6h 内有动静**的书——否则历史遗留的
    in_progress 旧测试书会被逐本判定「停滞」并全部自动拉起写作（实测一次
    开机扫出 66 本、连当天的十几本接口测试书也被拉起，token 灾难）。
    6h > 单书最长中断-发现窗口（看护间隔×预算），真在写的书必然命中。"""
    from . import writer as _wr
    n = 0
    try:
        for d in sorted(os.listdir(_wr.PROJECTS_DIR)):
            pdir = os.path.join(_wr.PROJECTS_DIR, d)
            if not os.path.isdir(pdir):
                continue
            ev = os.path.join(pdir, "创作事件.jsonl")
            try:
                # v4.26.1b：用「最后一条非 supervisor 事件」的时间——接手宣言会刷新文件
                # mtime，用 mtime 自指污染（实测：扫描自己把 66 本旧书全变成"最近活跃"）
                age = time.time() - _last_engine_event_ts(ev)
            except OSError:
                continue   # 从没写过正文的书（规划态旧稿）不接手
            if age > 6 * 3600:
                continue
            med, fp, plan = _wr._plan_of(pdir)
            if not plan:
                continue
            status = str(plan.get("status") or "")
            if status not in ("in_progress", "planning", "writing"):
                continue
            if (plan.get("meta") or {}).get("paused"):
                continue
            ensure(d)
            n += 1
        if n:
            print(f"[supervisor] 开机孤儿扫描：已接手 {n} 本最近活跃的在写书", flush=True)
    except Exception as e:
        print(f"[supervisor] 孤儿扫描异常: {e}", flush=True)
    return n
