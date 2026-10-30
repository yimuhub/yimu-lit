"""v2 API 聚合层（M6 整合）。

把长篇内核 / 参考资料 / 反馈进化 / 固定评测 暴露为稳定 HTTP 接口，同时保持旧版
`app.py` 不改也能启动：所有新模块都在函数内延迟导入，缺失时返回 501 而不是让
应用 import 失败。这样并行开发期间旧功能与测试始终可用。

约定：
- 成功返回 JSON 可序列化 dict；失败返回 `{"error": "中文原因"}` 与非 2xx。
- 不在这里做业务逻辑；只做参数校验、项目定位、结果整形与错误映射。
- 不打印用户正文、密钥或绝对路径。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import threading
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mod(name: str):
    """延迟导入 bot.<name>；返回 (模块, 错误响应)。"""
    try:
        return importlib.import_module(f"bot.{name}"), None
    except Exception as e:
        return None, JSONResponse(
            {"error": f"{name} 模块尚未就绪：{e}", "code": "module_unavailable"},
            status_code=501,
        )


def _bad(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)


# 各文体的「写正文」阶段码（bot/medium.py 的 stages）。技能文件的 stages 用的是这些码，
# 前端传来的泛化值（WRITE / 空）需要先落地成具体码，否则 pick 只会返回通用技能。
_WRITE_STAGE = {"novel": "P2", "script": "S5", "poetry": "C", "prose": "D", "thesis": "T2", "children": "C2", "news": "N2", "copy": "K3"}


def _resolve_stage(stage: str, med: str) -> str:
    """把前端传来的阶段值落地成该文体真实存在的阶段码。

    空值或泛化的 WRITE/WRITING → 该文体「写正文」的阶段；已是合法码则原样保留；
    无法识别时也回落到写正文阶段（技能文件的 stages 里查不到非法码会静默漏掉题材技能）。
    """
    s = str(stage or "").strip().upper()
    write = _WRITE_STAGE.get(med, "P2")
    if not s or s in ("WRITE", "WRITING", "WRITER"):
        return write
    try:
        from bot import medium as medium_mod
        if s in medium_mod.all_stage_codes(med):
            return s
    except Exception:
        return s
    return write


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _read_jsonl(path: str, limit: int = 5000) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
                if len(out) >= limit:
                    break
    except Exception:
        return []
    return out


def _chat_log_path(proj) -> str:
    """小说聊天历史 JSONL 文件路径。"""
    return os.path.join(proj.path, "chat_log.jsonl")


def _save_chat_msg(proj, role: str, content: str, extra: dict | None = None):
    """追加一条聊天消息到项目的 chat_log.jsonl。"""
    try:
        import time as _t
        rec = {"role": role, "content": content, "ts": int(_t.time())}
        if extra and isinstance(extra, dict):
            rec.update(extra)
        with open(_chat_log_path(proj), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_chat_log(proj, limit: int = 500) -> list:
    """读取项目的聊天历史。"""
    return _read_jsonl(_chat_log_path(proj), limit=limit)


def _project(get_project, key):
    """定位项目；返回 (proj, None) 或 (None, 响应)。"""
    if not isinstance(key, str) or not key:
        return None, _bad("缺少作品 key", 400)
    proj = get_project(key)
    if not proj:
        return None, _bad("作品不存在", 404)
    return proj, None


def _cfg():
    try:
        from bot import llm
        return llm._global_defaults()
    except Exception:
        return {}


def _persist_learning(summary) -> None:
    """把反馈聚合快照写进学习档案，供下次选题/风格建议使用；失败不影响接口。"""
    try:
        from bot import learn
        learn.record_feedback_summary(summary)
    except Exception:
        pass


def _norm_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# 会话流：等待期心跳间隔（秒）与「没有任何可用供应商」时的友好兜底文案。
_HEARTBEAT_SECS = 2.0
_NO_PROVIDER_TEXT = "您的消息已收到。由于未配置大模型，请配置模型。"
# 追章模式：轮询间隔沿用心跳节奏；正文切片大小/片间隔；最长陪跑 30 分钟。
_CHAPTER_CHUNK = 40
_CHAPTER_DELTA_GAP = 0.012
_FOLLOW_TIMEOUT_SECS = 30 * 60


def _waiting_text(elapsed: float) -> str:
    """等待心跳文案：<8s 普通思考，>=8s 深入思考，>=60s 追加网关慢提示（单行）。"""
    n = int(elapsed)
    if elapsed >= 60:
        return f"模型正在深入思考（已等待 {n}s），网关较慢，可点停止后稍后重试"
    if elapsed >= 8:
        return f"模型正在深入思考（已等待 {n}s）"
    return f"模型正在思考中（已等待 {n}s）"


def _provider_ready() -> bool:
    """开跑前探测「有没有可用供应商」；探测自身出错时按有处理，避免掩盖真实错误。"""
    try:
        from bot import providers
        return bool(providers.usable())
    except Exception:
        return True


# ------------------------------------------------------------ 追章（v2.2.2）


def _follow_project(payload: dict, get_project):
    """追章目标项目：payload.key 能解析且作品存在才返回，否则 None（走普通收尾）。"""
    key = payload.get("key")
    if not isinstance(key, str) or not key:
        return None
    try:
        return get_project(key) or None
    except Exception:
        return None


def _proj_status(proj) -> dict:
    """proj.status() 的防御包装；任何异常/非 dict 都按「未知状态」处理。"""
    try:
        st = proj.status()
    except Exception:
        return {}
    return st if isinstance(st, dict) else {}


def _proj_plan(proj) -> dict:
    """proj.plan() 的防御包装；读不到计划按空处理（Project.plan 每次都读盘，无缓存问题）。"""
    try:
        p = proj.plan()
    except Exception:
        return {}
    return p if isinstance(p, dict) else {}


def _next_pending(plan: dict):
    """plan 里下一个待写章节 (no, title)；全部写完时返回 None。"""
    best = None
    for c in plan.get("chapters") or []:
        if not isinstance(c, dict) or str(c.get("status") or "") not in ("pending", "in_progress"):
            continue
        no = _as_int(c.get("chapterNumber"))
        if no is None:
            continue
        if best is None or no < best[0]:
            best = (no, str(c.get("title") or f"第{no}章"))
    return best


def _read_chapter_file(proj, file_path) -> str:
    """按计划里的 filePath 读章节正文；越界/非法/读不到一律空串（只发 start/done）。"""
    name = str(file_path or "").strip()
    if not name:
        return ""
    try:
        from bot.safeio import safe_join
        path = safe_join(proj.path, name, label="章节文件")
    except Exception:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _as_int(v):
    """宽松转 int；失败返回 None（章节号/字数来自磁盘 JSON，可能缺失或为脏数据）。"""
    try:
        return int(v)
    except Exception:
        return None


def _merge_card_status(card: dict, flat) -> dict:
    """章卡叠加 02-写作计划.json 里的真实写作状态：只升级不降级。

    planned/changed + 计划 completed → done；planned/changed + in_progress → writing；
    done 保持 done；计划里没有该章时原样返回（另补 word_count/title）。
    """
    out = dict(card) if isinstance(card, dict) else {"n": 0, "status": "planned"}
    if not isinstance(flat, dict):
        return out
    st = out.get("status")
    fs = str(flat.get("status") or "")
    if st in ("planned", "changed"):
        if fs == "completed":
            out["status"] = "done"
        elif fs == "in_progress":
            out["status"] = "writing"
    try:
        out["word_count"] = int(flat.get("wordCount"))
    except Exception:
        out["word_count"] = None
    if not out.get("title") and flat.get("title"):
        out["title"] = str(flat["title"])
    return out


def _plan_view(proj, plan: dict) -> dict:
    """把 02c 总规划与 02d 章卡合成前端树；结构不匹配时原样返回。"""
    if not isinstance(plan, dict):
        return {"volumes": []}
    cards = _read_jsonl(os.path.join(proj.path, "02d-章卡.jsonl"), limit=20000)
    by_no = {}
    for c in cards:
        try:
            by_no[int(c.get("n"))] = c
        except Exception:
            continue
    # 写作计划里的真实状态：chapterNumber → 章节条目（02d 章卡的状态可能滞后）。
    # 事实源是 02-写作计划.json（writer 落盘）；02c 只有老项目迁移时才带 chapters，
    # 所以必须从 proj.plan() 取，传入的 plan（02c）只作兜底。
    flat = {}
    disk_plan = proj.plan() if proj is not None else None
    if isinstance(disk_plan, dict) and isinstance(disk_plan.get("chapters"), list):
        chs = disk_plan["chapters"]
    else:
        chs = plan.get("chapters")
    if isinstance(chs, list):
        for c in chs:
            if isinstance(c, dict):
                no = _as_int(c.get("chapterNumber"))
                if no is not None:
                    flat[no] = c
    sc = plan.get("scale")
    if isinstance(sc, dict):
        words = int(sc.get("target_words") or 0)
        chs = int(sc.get("chapters") or 0)
        # 不足一万字直接报字数，避免「0.6666 万字」这类别扭小数
        plan["scale_label"] = (f"{words / 10000:g} 万字 · {chs} 章" if words >= 10000 and chs
                               else (f"{words} 字 · {chs} 章" if words and chs
                                     else (sc.get("label") or "")))
    volumes = plan.get("volumes")
    if not isinstance(volumes, list):
        return plan
    for v in volumes:
        for a in (v.get("arcs") or []):
            nums = [n for n in (a.get("chapters") or []) if isinstance(n, int)]
            if not nums and a.get("start_chapter") and a.get("end_chapter"):
                try:
                    nums = list(range(int(a["start_chapter"]), int(a["end_chapter"]) + 1))
                except Exception:
                    nums = []
            # 用章卡里的 volume/arc 归属补全（总规划只存了卷弧标题时）
            if not nums:
                vno, ano = v.get("no"), a.get("no")
                nums = [n for n, c in by_no.items()
                        if c.get("volume") == vno and c.get("arc") == ano]
            a["chapters"] = sorted(set(nums))
            a["chapter_cards"] = [_merge_card_status(by_no.get(n, {"n": n, "status": "planned"}),
                                                     flat.get(n)) for n in a["chapters"]]
    return plan


def _attach_digest(proj, lf, plan: dict) -> dict:
    try:
        d = lf.plan_digest(proj)
        return d if isinstance(d, dict) else {}
    except Exception:
        pass
    chapters = plan.get("chapters") or []
    written = sum(1 for c in chapters if c.get("status") == "completed")
    return {"chapters_written": written, "chapters_planned": len(chapters)}


def _chapter_ledger(pdir: str, k: int = 5) -> dict:
    """v4.7.0（借鉴 reveric 长篇状态管理）：最近 k 章的断章钩子/情绪/紧张度轨迹，
    用于防「连续多章同款钩子/同款情绪」的模式收敛。读 21-章摘要.jsonl。"""
    fp = os.path.join(pdir, "21-章摘要.jsonl")
    rows = []
    try:
        with open(fp, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                q = d.get("quality_signals") or {}
                rows.append({"n": d.get("n"),
                             "hook": (q.get("hook") or "")[:60],
                             "tension": q.get("tension"),
                             "emotion": q.get("emotion") or "",
                             "hook_type": q.get("hook_type") or ""})
    except Exception:
        return {}
    rows = [r for r in rows if r["n"] is not None][-k:]
    tensions = [r["tension"] for r in rows if isinstance(r["tension"], int)]
    last_peak, gap = None, None
    for i, t in enumerate(tensions):
        if isinstance(t, int) and t >= 8:
            last_peak = i
    if last_peak is not None:
        gap = len(tensions) - 1 - last_peak
    return {"recent": rows, "tension_track": tensions, "since_peak": gap}


def _medium_state_payload(proj, key: str, q: str = "") -> dict:
    """v4.6.2：非小说的状态 pane 数据（剧本用剧本状态库；诗词/散文/论文给阶段/产物映射）。"""
    if str(key).startswith("s_"):
        return _script_state_payload(proj, key, q)
    from bot import writer as _wr, medium as _medium_mod
    med = {"p_": "poetry", "e_": "prose", "t_": "thesis",
           "c_": "children", "n_": "news", "k_": "copy", "o_": "other"}.get(str(key)[:2], "prose")
    it = _wr.library_item(key) or {}
    timeline: list[dict] = []
    issues: list[str] = []
    chars: list[dict] = []
    if med == "thesis":
        from bot import thesis_writer
        tp = thesis_writer.get_project(key)
        plan = (tp.plan() if tp else None) or {}
        status_map = plan.get("chapterStatus") or {}
        for ch in (plan.get("outline") or {}).get("chapters") or []:
            no = int(ch.get("no") or 0)
            timeline.append({"chapter": no, "time": "", "event": ch.get("title") or f"第{no}章",
                             "location": "、".join(ch.get("sections") or []),
                             "characters": []})
        try:
            issues = thesis_writer.check_citations(plan, proj.path)
        except Exception:
            issues = []
        chars = [{"name": "引用池", "status": f"{len(plan.get('refs') or [])} 条"}]
    elif med in ("children", "news", "copy", "other"):
        from bot import children_writer, news_writer, copy_writer, other_writer
        mods = {"children": children_writer, "news": news_writer, "copy": copy_writer, "other": other_writer}
        pp = mods[med].get_project(key)
        plan = (pp.plan() if pp else None) or {}
        cmap = plan.get("chapterStatus") or {}
        stages_list = _medium_stages(med)
        for i, (code, cname) in enumerate(stages_list):
            st = cmap.get(code, "pending")
            timeline.append({"chapter": i + 1, "time": "", "event": f"{cname}（{code}）",
                             "location": {"completed": "完成", "in_progress": "进行中", "pending": "待处理"}.get(st, st),
                             "characters": []})
        if plan.get("status") == "completed":
            issues = []
        else:
            issues = [f"当前阶段：{plan.get('meta', {}).get('stage', '未知')}"]
    else:
        from bot import poetry_writer as _pw, prose_writer as _ew
        cls = _pw if med == "poetry" else _ew
        pp = cls.get_project(key)
        plan = (pp.plan() if pp else None) or {}
        meta = plan.get("meta") or {}
        stage_idx = int(meta.get("stageIdx") or 0)
        stages_list = _medium_stages(med)
        for i, (code, cname) in enumerate(stages_list):
            timeline.append({"chapter": i + 1, "time": "", "event": f"{cname}（{code}）",
                             "location": "完成" if stage_idx > i or plan.get("status") == "completed"
                             else ("进行中" if stage_idx == i else "待处理"), "characters": []})
        if med == "poetry" and plan.get("meta", {}).get("verdict"):
            issues = [f"格律判定：{plan['meta']['verdict']}"] if plan["meta"]["verdict"] not in ("pass", "skipped") else []
        if med == "prose" and (meta.get("slop") or {}).get("summary"):
            issues = [f"去味扫描：{meta['slop']['summary']}"]
    return {"ok": True, "key": key,
            "state": {"metrics": {"words": it.get("total_words") or 0},
                      "last_audit": None},
            "current_chapter": it.get("done") or 1,
            "chapters": {"done": it.get("done") or 0, "total": it.get("total") or 0},
            "characters": chars, "locations": [], "items": [], "factions": [],
            "threads": [], "foreshadowing": [],
            "timeline": timeline[-2000:],
            "issues": issues, "foreshadow_due": {}, "excerpt": ""}


def _script_state_payload(proj, key: str, q: str = "") -> dict:
    """v4.3.0：剧本的状态数据（与小说 /api/state 同构），供右栏状态 pane 复用。"""
    from bot import script_state, script_writer
    st = script_state.build_state(proj.path)
    audit = script_state.load_audit(proj.path)
    sp = script_writer.get_project(key)
    plan = (sp.plan() if sp else None) or {}
    scenes = plan.get("scenes") or []
    done = sum(1 for sc in scenes if sc.get("status") == "completed")
    words = sum(sc.get("wordCount") or 0 for sc in scenes)
    cur = next((sc.get("no") for sc in scenes if sc.get("status") != "completed"), None) or (scenes[-1].get("no") if scenes else 1)
    chars = [{"name": k, "status": f"{len(v.get('scenes') or [])} 场"} for k, v in (st.get("characters") or {}).items()]
    locs = [{"name": k, "status": f"{len(v.get('scenes') or [])} 场"} for k, v in (st.get("locations") or {}).items()]
    timeline = [{"chapter": sc.get("no"), "time": sc.get("time") or "",
                 "event": (sc.get("heading") or f"{sc.get('kind') or ''} {sc.get('place') or ''}").strip(),
                 "location": sc.get("place") or "",
                 "characters": [x.strip() for x in re.split(r"[、，,/\s]+", str(sc.get("cast") or "")) if x.strip()]}
                for sc in scenes]
    handled = set(audit.get("handled") or []) if isinstance(audit, dict) else set()
    issues = [i for i in ((audit or {}).get("issues") or []) if i not in handled]
    return {"ok": True, "key": key,
            "state": {"metrics": {"words": words, "last_chapter": cur},
                      "last_audit": ({"at": audit.get("at"), "scope": "script",
                                      "issues": issues[:8]} if audit.get("at") else None)},
            "current_chapter": cur,
            "chapters": {"done": done, "total": len(scenes)},
            "characters": chars, "locations": locs, "items": [], "factions": [],
            "threads": [], "foreshadowing": [],
            "timeline": timeline[-2000:],
            "issues": issues, "foreshadow_due": {},
            "excerpt": ""}


def _medium_stage_pairs(med: str) -> list[tuple[str, str]]:
    """阶段表 → [(code, name), ...]（2 元组，不含 desc）。"""
    from bot.medium import stages as _stages
    return [(c, n) for c, n, _ in _stages(med)]


def _medium_plan_view(proj, key: str, med: str) -> dict:
    """v4.12.5：非小说计划 → 规划树。每文体有自己的阶段名与完成状态。"""
    from bot import (script_writer, poetry_writer, prose_writer, thesis_writer,
                     children_writer, news_writer, copy_writer)
    def _chapter(no, title, status, words, extra=""):
        return {"n": no, "title": title or f"第{no}步", "status": status,
                "word_count": words, "goal": extra}

    chapters, label, mode = [], "", ""
    if med == "script":
        sp = script_writer.get_project(key)
        plan = (sp.plan() if sp else None) or {}
        label = plan.get("scriptName") or key
        mode = plan.get("formLabel") or "剧本"
        for sc in plan.get("scenes") or []:
            chapters.append(_chapter(sc.get("no"), (sc.get("heading") or f"第 {sc.get('no')} 场"),
                                     sc.get("status") or "pending", sc.get("wordCount") or 0,
                                     f"{sc.get('kind') or ''} {sc.get('place') or ''}".strip()))
    elif med == "thesis":
        tp = thesis_writer.get_project(key)
        plan = (tp.plan() if tp else None) or {}
        label = plan.get("thesisName") or key
        mode = plan.get("degreeLabel") or "论文"
        status_map = plan.get("chapterStatus") or {}
        for ch in (plan.get("outline") or {}).get("chapters") or []:
            no = int(ch.get("no") or 0)
            chapters.append(_chapter(no, ch.get("title"), status_map.get(str(no), "pending"),
                                     ch.get("target_words") or 0, "、".join(ch.get("sections") or [])))
    elif med in ("children", "news", "copy", "other"):
        from bot import other_writer as _ow
        mods = {"children": children_writer, "news": news_writer, "copy": copy_writer, "other": _ow}
        pp = mods[med].get_project(key)
        plan = (pp.plan() if pp else None) or {}
        label = plan.get(_NAME_KEYS.get(med, "name")) or key
        mode = plan.get(_FORM_KEYS.get(med, "form")) or _medium_label(med)
        status_map = plan.get("chapterStatus") or {}
        stage_names = _medium_stage_pairs(med)
        for i, (code, cname) in enumerate(stage_names):
            st = status_map.get(code, "pending")
            if plan.get("status") == "completed":
                st = "completed"
            chapters.append(_chapter(i + 1, f"{cname}（{code}）", st, 0, ""))
    else:
        # poetry / prose：按 stageIdx 映射
        cls = poetry_writer if med == "poetry" else prose_writer
        pp = cls.get_project(key)
        plan = (pp.plan() if pp else None) or {}
        label = (plan.get("poemName") or plan.get("proseName")) or key
        meta = plan.get("meta") or {}
        stages_list = _medium_stage_pairs(med)
        for i, (code, cname) in enumerate(stages_list):
            chapters.append(_chapter(i + 1, f"{cname}（{code}）",
                                     "completed" if (meta.get("stageIdx", 0) or 0) > i or plan.get("status") == "completed"
                                     else ("in_progress" if (meta.get("stageIdx", 0) or 0) == i else "pending"),
                                     0, plan.get("statusLabel") or ""))
        mode = plan.get("genreLabel") or plan.get("operationLabel") or label

    # 阶段名替换：plan-vol-head 显示文体专有标题
    vol_title = {"news": "新闻稿流程", "children": "故事流程", "copy": "文案流程",
                 "script": "剧本流程", "thesis": "论文流程",
                 "poetry": "诗词流程", "prose": "散文流程", "other": "创作流程"}.get(med, "规划")
    arc = {"no": 1, "title": label or "规划", "purpose": "",
           "chapters": [c["n"] for c in chapters],
           "chapter_cards": chapters}
    return {"version": 1, "mode": mode, "scale_label": f"{len(chapters)} 步",
            "premise": "", "volumes": [{"no": 1, "title": vol_title, "arcs": [arc]}]}


_NAME_KEYS = {"children": "bookName", "news": "newsName", "copy": "copyName",
              "script": "scriptName", "thesis": "thesisName",
              "poetry": "poemName", "prose": "proseName", "other": "otherName"}
_FORM_KEYS = {"children": "typeLabel", "news": "typeLabel", "copy": "platformLabel",
              "script": "formLabel", "thesis": "degreeLabel", "other": "templateLabel"}


def _medium_label(med):
    from bot.medium import label as _label
    return _label(med)


def _medium_stages(med: str) -> list[tuple[str, str]]:
    from bot import medium as _medium_mod
    return [(c, n) for c, n, _ in _medium_mod.stages(med)]


def register_v2_routes(app, get_project, chat_func=None):
    from bot import chatsum
    from bot.writer import _clean_chapter_text
    """在 app 上注册 v2 接口。由 app.py 在 `_get_project` 定义后调用。"""

    # ------------------------------------------------------------ 能力探测
    @app.get("/api/capabilities")
    def api_capabilities():
        feats = {"classic_ui": os.path.exists(os.path.join(ROOT, "static", "index.html")),
                 "v2_ui": os.path.exists(os.path.join(ROOT, "static", "v2", "index.html")),
                 "chat_stream": bool(chat_func)}
        for name, modname in (("longform", "longform"), ("references", "references"),
                              ("feedback", "feedback"), ("skill_evolution", "skill_evo"),
                              ("eval", "eval")):
            try:
                importlib.import_module(f"bot.{modname}")
                feats[name] = True
            except Exception:
                feats[name] = False
        try:
            from bot import medium as medium_mod
            media = [{"key": k, "label": v.get("label"), "icon": v.get("icon"),
                      "desc": v.get("desc"), "stages": [s[0] for s in v.get("stages", [])]}
                     for k, v in medium_mod.MEDIUMS.items()]
        except Exception:
            media = []
        return {"ok": True, "features": feats, "mediums": media,
                "time": int(time.time())}

    # ------------------------------------------------------------ 长篇规划
    @app.get("/api/plan")
    def api_plan(key: str = ""):
        proj, err = _project(get_project, key)
        if err:
            return err
        # v4.6.2：剧本/诗词/散文/论文的「规划」页签——把各自计划映射成与小说同构的
        # volumes/arcs/chapters 树，planPane 直接复用
        _med = {"s_": "script", "p_": "poetry", "e_": "prose", "t_": "thesis",
                "c_": "children", "n_": "news", "k_": "copy", "o_": "other"}.get(str(key)[:2])
        if _med:
            return {"ok": True, "key": key, "plan": _medium_plan_view(proj, key, _med),
                    "digest": None, "proposals": []}
        lf, err = _mod("longform")
        if err:
            return err
        try:
            result = lf.ensure_plan(proj)
            plan = result if isinstance(result, dict) else None
            if not plan:
                plan = _read_json(os.path.join(proj.path, "02c-总规划.json"), {})
            plan = _plan_view(proj, plan)
            proposals = lf.list_proposals(proj)
            if isinstance(proposals, dict):
                proposals = proposals.get("items") or proposals.get("proposals") or []
            return {"ok": True, "key": key, "plan": plan,
                    "digest": _attach_digest(proj, lf, plan),
                    "proposals": _norm_list(proposals)}
        except Exception as e:
            return _bad(f"读取规划失败：{e}", 500)

    @app.post("/api/plan/ensure")
    def api_plan_ensure(payload: dict):
        return api_plan(str(payload.get("key") or ""))

    @app.post("/api/plan/replan")
    def api_plan_replan(payload: dict):
        proj, err = _project(get_project, payload.get("key"))
        if err:
            return err
        lf, err = _mod("longform")
        if err:
            return err
        scope = str(payload.get("scope") or "window")
        if scope not in ("book", "volume", "arc", "window", "chapter"):
            return _bad("scope 只能是 book/volume/arc/window/chapter")
        try:
            proposal = lf.replan(proj, scope=scope,
                                 instruction=str(payload.get("instruction") or "")[:4000],
                                 reason=str(payload.get("reason") or "manual"))
            return {"ok": True, "proposal": proposal}
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"生成重规划提案失败：{e}", 500)

    @app.post("/api/plan/multi")
    def api_plan_multi(payload: dict):
        """v2.1 P2：多岗位同时规划 —— 规划师与总编各出一份提案，前端并排对比后选一份应用。"""
        proj, err = _project(get_project, payload.get("key"))
        if err:
            return err
        lf, err = _mod("longform")
        if err:
            return err
        scope = str(payload.get("scope") or "window")
        if scope not in ("book", "volume", "arc", "window", "chapter"):
            return _bad("scope 只能是 book/volume/arc/window/chapter")
        roles = payload.get("roles")
        if not isinstance(roles, list):
            roles = None
        try:
            fn = getattr(lf, "multi_replan", None)
            if not callable(fn):
                return _bad("多角色规划尚未就绪", 501)
            cards = fn(proj, scope=scope,
                       instruction=str(payload.get("instruction") or "")[:4000],
                       roles=roles)
            return {"ok": True, "key": payload.get("key"), "scope": scope, "cards": cards or []}
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"多角色规划失败：{e}", 500)

    @app.post("/api/plan/apply")
    def api_plan_apply(payload: dict):
        proj, err = _project(get_project, payload.get("key"))
        if err:
            return err
        lf, err = _mod("longform")
        if err:
            return err
        pid = str(payload.get("proposal_id") or "")
        if not pid:
            return _bad("缺少 proposal_id")
        try:
            res = lf.apply_replan(proj, pid, allow_past=bool(payload.get("allow_past")))
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "应用失败"))
            return {"ok": True, "result": res}
        except ValueError as e:
            return _bad(str(e))
        except Exception as e:
            return _bad(f"应用提案失败：{e}", 500)

    @app.post("/api/plan/reject")
    def api_plan_reject(payload: dict):
        proj, err = _project(get_project, payload.get("key"))
        if err:
            return err
        lf, err = _mod("longform")
        if err:
            return err
        try:
            return {"ok": True, "result": lf.reject_replan(
                proj, str(payload.get("proposal_id") or ""),
                str(payload.get("note") or ""))}
        except Exception as e:
            return _bad(f"驳回失败：{e}", 500)

    @app.post("/api/plan/window")
    def api_plan_window(payload: dict):
        proj, err = _project(get_project, payload.get("key"))
        if err:
            return err
        lf, err = _mod("longform")
        if err:
            return err
        try:
            start = int(payload.get("start") or 0)
            end = int(payload.get("end") or 0)
            return {"ok": True, "result": lf.plan_window(proj, start, end)}
        except Exception as e:
            return _bad(f"滚动规划失败：{e}", 500)

    # ------------------------------------------------------------ 状态库
    @app.get("/api/state")
    def api_state(key: str = "", q: str = ""):
        proj, err = _project(get_project, key)
        if err:
            return err
        # v4.3.0：剧本的状态 pane —— 数据源换 18-剧本状态库 / 22-剧本体检 / 剧本计划
        if str(key)[:2] in ("s_", "p_", "e_", "t_", "c_", "n_", "k_", "o_"):
            return _medium_state_payload(proj, key, q)
        stmod, err = _mod("state")
        if err:
            return err
        try:
            st = stmod.load(proj.path) or {}
            # v2.3.0：注入最近一次一致性体检（22-规划体检.json，由 /api/audit 或写作收束写入）
            try:
                afp = os.path.join(proj.path, "22-规划体检.json")
                if os.path.exists(afp):
                    with open(afp, encoding="utf-8") as _f:
                        _audit = json.load(_f) or {}
                    if isinstance(_audit, dict) and _audit.get("at"):
                        _handled = set(_audit.get("handled") or [])
                        _issues = [i for i in (_audit.get("issues") or []) if i not in _handled]
                        st = dict(st)
                        st["last_audit"] = {"at": _audit.get("at"), "scope": _audit.get("scope"),
                                            "issues": _issues[:8]}
            except Exception:
                pass
            plan = proj.plan() or {}
            total = len(plan.get("chapters") or [])
            done = sum(1 for c in (plan.get("chapters") or []) if c.get("status") == "completed")
            cur = min(done + 1, total or done + 1)
            try:
                issues = stmod.validate(st)
            except Exception:
                issues = []
            try:
                due = stmod.foreshadow_due(st, cur)
            except Exception:
                due = {}
            chars = [{"name": k, **v} for k, v in (st.get("characters") or {}).items()]
            return {"ok": True, "key": key, "state": st,
                    "current_chapter": cur,
                    "chapters": {"done": done, "total": total},
                    "characters": chars,
                    "locations": [{"name": k, **v} for k, v in (st.get("locations") or {}).items()],
                    "items": [{"name": k, **v} for k, v in (st.get("items") or {}).items()],
                    "factions": [{"name": k, **v} for k, v in (st.get("factions") or {}).items()],
                    "threads": _norm_list(st.get("threads")),
                    "foreshadowing": [{"id": k, **v} for k, v in (st.get("foreshadowing") or {}).items()],
                    # v2.8.0：时间线全量返回（上限 2000 条），前端按章节分组渲染，
                    # 不再截断到 30 条导致「时间线只剩几个点」
                    "timeline": _norm_list(st.get("timeline"))[-2000:],
                    # v4.7.0 借鉴 reveric：手法账本（最近 5 章钩子/情绪/紧张度）防模式收敛
                    "ledger": _chapter_ledger(proj.path, 5),
                    "issues": issues, "foreshadow_due": due,
                    "excerpt": stmod.excerpt(st, q, 6000)}
        except Exception as e:
            return _bad(f"读取状态库失败：{e}", 500)

    # ------------------------------------------------------------ 参考资料
    @app.get("/api/references")
    def api_refs(key: str = ""):
        refs, err = _mod("references")
        if err:
            return err
        try:
            res = refs.list_refs(key)
            if isinstance(res, list):
                res = {"items": res}
            return {"ok": True, **(res or {})}
        except Exception as e:
            return _bad(f"读取参考资料失败：{e}", 500)

    @app.post("/api/references/upload")
    async def api_refs_upload(request: Request):
        refs, err = _mod("references")
        if err:
            return err
        try:
            form = await request.form()
        except Exception:
            return _bad("上传组件未就绪（需要 python-multipart）", 503)
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            return _bad("缺少文件")
        try:
            data = await up.read(30 * 1024 * 1024 + 1)
        except Exception as e:
            return _bad(f"读取上传文件失败：{e}")
        if len(data) > 30 * 1024 * 1024:
            return _bad("文件超过 30MB 上限", 413)
        try:
            res = refs.add_upload(
                data,
                filename=str(form.get("filename") or getattr(up, "filename", "") or "upload.txt"),
                title=str(form.get("title") or ""),
                usage_mode=str(form.get("usage_mode") or "facts"),
                copyright_confirmed=str(form.get("copyright_confirmed") or "true").lower()
                in ("1", "true", "yes", "on"),
                cfg=_cfg(),
            )
        except Exception as e:
            return _bad(f"解析参考资料失败：{e}", 500)
        if isinstance(res, dict) and res.get("ok") is False:
            return _bad(str(res.get("error") or "入库失败"))
        rid = (res or {}).get("ref_id") or ((res or {}).get("ref") or {}).get("id")
        pkey = str(form.get("project_key") or "")
        if pkey and rid:
            try:
                refs.link_to_project(pkey, rid, str(form.get("usage_mode") or "facts"))
            except Exception:
                pass
        return {"ok": True, "item": (res or {}).get("ref") or (res or {}).get("item") or res,
                "ref_id": rid}

    @app.post("/api/references/text")
    def api_refs_text(payload: dict):
        refs, err = _mod("references")
        if err:
            return err
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return _bad("缺少参考资料正文")
        try:
            res = refs.add_text(text, str(payload.get("title") or "未命名资料"),
                                kind=str(payload.get("kind") or ""),
                                usage_mode=str(payload.get("usage_mode") or "facts"),
                                copyright_confirmed=bool(payload.get("copyright_confirmed", True)),
                                cfg=_cfg())
        except Exception as e:
            return _bad(f"保存参考资料失败：{e}", 500)
        if isinstance(res, dict) and res.get("ok") is False:
            return _bad(str(res.get("error") or "入库失败"))
        rid = (res or {}).get("ref_id") or ((res or {}).get("ref") or {}).get("id")
        pkey = str(payload.get("project_key") or "")
        if pkey and rid:
            try:
                refs.link_to_project(pkey, rid, str(payload.get("usage_mode") or "facts"))
            except Exception:
                pass
        return {"ok": True, "item": (res or {}).get("ref") or (res or {}).get("item") or res,
                "ref_id": rid}

    @app.post("/api/references/url")
    def api_refs_url(payload: dict):
        refs, err = _mod("references")
        if err:
            return err
        url = str(payload.get("url") or "")
        if not url:
            return _bad("缺少网址")
        try:
            res = refs.add_url(url, title=str(payload.get("title") or ""),
                               usage_mode=str(payload.get("usage_mode") or "facts"),
                               copyright_confirmed=bool(payload.get("copyright_confirmed", True)),
                               cfg=_cfg())
        except Exception as e:
            return _bad(f"抓取参考资料失败：{e}", 500)
        if isinstance(res, dict) and res.get("ok") is False:
            return _bad(str(res.get("error") or "入库失败"))
        rid = (res or {}).get("ref_id") or ((res or {}).get("ref") or {}).get("id")
        pkey = str(payload.get("project_key") or "")
        if pkey and rid:
            try:
                refs.link_to_project(pkey, rid, str(payload.get("usage_mode") or "facts"))
            except Exception:
                pass
        return {"ok": True, "item": (res or {}).get("ref") or (res or {}).get("item") or res,
                "ref_id": rid}

    @app.patch("/api/references/{ref_id}")
    def api_refs_patch(ref_id: str, payload: dict):
        refs, err = _mod("references")
        if err:
            return err
        try:
            if payload.get("usage_mode") is not None:
                res = refs.set_usage(ref_id, str(payload["usage_mode"]))
                if isinstance(res, dict) and res.get("ok") is False:
                    return _bad(str(res.get("error") or "更新失败"))
            if any(k in payload for k in ("title", "tags", "copyright_confirmed")):
                res = refs.update_meta(ref_id, title=payload.get("title"),
                                       tags=payload.get("tags"),
                                       copyright_confirmed=payload.get("copyright_confirmed"))
                if isinstance(res, dict) and res.get("ok") is False:
                    return _bad(str(res.get("error") or "更新失败"))
            return {"ok": True, "item": refs.get_ref(ref_id)}
        except Exception as e:
            return _bad(f"更新参考资料失败：{e}", 500)

    @app.delete("/api/references/{ref_id}")
    def api_refs_delete(ref_id: str):
        refs, err = _mod("references")
        if err:
            return err
        try:
            res = refs.delete_ref(ref_id)
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "删除失败"))
            return {"ok": True, "result": res}
        except Exception as e:
            return _bad(f"删除参考资料失败：{e}", 500)

    @app.post("/api/references/{ref_id}/analyze")
    def api_refs_analyze(ref_id: str):
        refs, err = _mod("references")
        if err:
            return err
        try:
            res = refs.analyze_ref(ref_id, cfg=_cfg())
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "分析失败"))
            return {"ok": True, "analysis": res}
        except Exception as e:
            return _bad(f"分析参考资料失败：{e}", 500)

    @app.get("/api/references/{ref_id}/chunks")
    def api_refs_chunks(ref_id: str, offset: int = 0, limit: int = 20):
        refs, err = _mod("references")
        if err:
            return err
        try:
            return {"ok": True, **refs.read_chunks(ref_id, offset, min(max(limit, 1), 50))}
        except Exception as e:
            return _bad(f"读取切片失败：{e}", 500)

    @app.get("/api/references/search")
    def api_refs_search(key: str = "", q: str = "", k: int = 6):
        refs, err = _mod("references")
        if err:
            return err
        if not q.strip():
            return {"ok": True, "items": []}
        try:
            ids = None
            if key:
                ids = refs.project_ref_ids(key)
                if not ids:
                    return {"ok": True, "items": []}
            items = refs.retrieve(q, ids, k=min(max(int(k), 1), 20))
            return {"ok": True, "items": items,
                    "block": refs.context_block(q, ids, 4000, k=min(max(int(k), 1), 20))}
        except Exception as e:
            return _bad(f"检索失败：{e}", 500)

    @app.post("/api/references/link")
    def api_refs_link(payload: dict):
        refs, err = _mod("references")
        if err:
            return err
        proj, perr = _project(get_project, payload.get("key"))
        if perr:
            return perr
        try:
            return {"ok": True, "result": refs.link_to_project(
                os.path.basename(proj.path), str(payload.get("ref_id") or ""),
                str(payload.get("usage_mode") or "facts"))}
        except Exception as e:
            return _bad(f"关联失败：{e}", 500)

    @app.post("/api/references/unlink")
    def api_refs_unlink(payload: dict):
        refs, err = _mod("references")
        if err:
            return err
        proj, perr = _project(get_project, payload.get("key"))
        if perr:
            return perr
        try:
            return {"ok": True, "result": refs.unlink_from_project(
                os.path.basename(proj.path), str(payload.get("ref_id") or ""))}
        except Exception as e:
            return _bad(f"取消关联失败：{e}", 500)

    @app.post("/api/references/overlap")
    def api_refs_overlap(payload: dict):
        refs, err = _mod("references")
        if err:
            return err
        text = payload.get("text")
        if not isinstance(text, str):
            return _bad("缺少待检文本")
        try:
            ids = payload.get("ref_ids")
            if ids is None and payload.get("key"):
                ids = refs.project_ref_ids(str(payload["key"]))
            return {"ok": True, **refs.overlap_check(text, ids)}
        except Exception as e:
            return _bad(f"查重失败：{e}", 500)

    # ------------------------------------------------------------ 反馈
    @app.post("/api/feedback")
    def api_feedback_add(payload: dict):
        fb, err = _mod("feedback")
        if err:
            return err
        try:
            if payload.get("before") is not None and payload.get("after") is not None:
                rec = fb.capture_edit(str(payload.get("project_key") or ""),
                                      payload.get("chapter_no"),
                                      payload.get("before"), payload.get("after"),
                                      rating=payload.get("rating"),
                                      tags=payload.get("tags") or [],
                                      note=payload.get("note") or "")
            else:
                rec = fb.record(payload)
            if isinstance(rec, dict) and rec.get("ok") is False:
                return _bad(str(rec.get("error") or "记录失败"))
            return {"ok": True, "record": rec}
        except Exception as e:
            return _bad(f"记录反馈失败：{e}", 500)

    @app.get("/api/feedback")
    def api_feedback_list(key: str = "", medium: str = "", stage: str = "", limit: int = 100):
        fb, err = _mod("feedback")
        if err:
            return err
        try:
            items = fb.list_feedback(key, medium, stage, min(max(limit, 1), 500))
            summary = fb.aggregate()
            _persist_learning(summary)
            return {"ok": True, "items": _norm_list(items),
                    "summary": summary,
                    "focus": fb.suggest_focus(medium, stage)}
        except Exception as e:
            return _bad(f"读取反馈失败：{e}", 500)

    @app.get("/api/feedback/summary")
    def api_feedback_summary():
        fb, err = _mod("feedback")
        if err:
            return err
        try:
            summary = fb.aggregate()
            _persist_learning(summary)
            return {"ok": True, "summary": summary}
        except Exception as e:
            return _bad(f"汇总反馈失败：{e}", 500)

    # ------------------------------------------------------------ 技能与进化
    @app.get("/api/skills")
    def api_skills(medium: str = "", stage: str = ""):
        try:
            from bot import skillkit
            return {"ok": True, "items": skillkit.list_skills(medium, stage),
                    "stats": skillkit.stats()}
        except Exception as e:
            return _bad(f"读取技能库失败：{e}", 500)

    @app.get("/api/skills/tree")
    def api_skills_tree(medium: str = ""):
        """技能库导航树：文体 → 题材分组（前端 #/skills 左栏）。"""
        try:
            from bot import skillkit
            fn = getattr(skillkit, "tree", None)
            if not fn:
                return {"ok": True, "tree": []}
            return {"ok": True, "tree": fn(medium or None)}
        except Exception as e:
            return _bad(f"读取技能树失败：{e}", 500)

    @app.get("/api/skills/file")
    def api_skills_file(path: str = ""):
        """按相对路径读取单个技能文件（frontmatter + 正文）；越界路径返回 400。"""
        try:
            from bot import skillkit
            return {"ok": True, **skillkit.read_skill(path)}
        except ValueError as e:
            return _bad(str(e), 400)
        except Exception as e:
            return _bad(f"读取技能文件失败：{e}", 500)

    @app.get("/api/skills/applicable")
    def api_skills_applicable(key: str = "", stage: str = ""):
        """某本书在当前题材下会被注入的技能清单 + 注入预览。"""
        proj, err = _project(get_project, key)
        if err:
            return err
        try:
            from bot import medium as medium_mod
            from bot import skillkit
            style = _read_json(os.path.join(proj.path, "style.json"), {}) or {}
            plan = proj.plan() or {}
            brief = plan.get("brief") or {}
            meta = plan.get("meta") or {}
            med = str(plan.get("medium") or meta.get("medium") or style.get("medium") or "")
            if not med:
                # v4.12.6：key 前缀兜底——children/news/copy 引擎不写 plan["medium"]，
                # 此前永远回落 novel → 技能面板全部显示小说技能
                med = {"c_": "children", "n_": "news", "k_": "copy", "o_": "other",
                       "s_": "script", "p_": "poetry", "e_": "prose",
                       "t_": "thesis"}.get(str(key)[:2], "novel")
            if not medium_mod.is_medium(med):
                med = "novel"
            st = _resolve_stage(stage, med)
            genre = str(style.get("genre") or brief.get("genre") or meta.get("genre") or plan.get("genre") or "")
            picked = skillkit.pick(med, st, genre=genre)
            from bot import skill_evo
            toggles = skill_evo._load_toggles() if hasattr(skill_evo, "_load_toggles") else {}
            items = [{"relpath": sf.relpath, "name": sf.name, "title": sf.title,
                      "kind": sf.kind, "weight": sf.weight, "genre": sf.genre or "_index",
                      "evolved": sf.evolved,
                      "enabled": toggles.get(sf.name, True)} for sf in picked]
            preview = ""
            try:
                preview = skillkit.stage_inject(st, med, genre=genre, max_bytes=6000)
            except Exception:
                pass
            return {"ok": True, "key": key, "medium": med, "genre": genre,
                    "stage": st, "items": items, "injected_preview": preview}
        except Exception as e:
            return _bad(f"读取本书技能失败：{e}", 500)

    @app.get("/api/skills/validate")
    def api_skills_validate():
        try:
            from bot import skillkit
            fn = getattr(skillkit, "validate_all", None)
            return {"ok": True, "result": fn() if fn else {"note": "当前版本无全库校验"}}
        except Exception as e:
            return _bad(f"校验技能库失败：{e}", 500)

    @app.get("/api/skills/evolution")
    def api_skills_evolution(status: str = ""):
        se, err = _mod("skill_evo")
        if err:
            return err
        out = {"ok": True}
        try:
            out["candidates"] = _norm_list(se.list_candidates(status or "pending"))
        except Exception as e:
            out["candidates"] = []
            out["candidates_error"] = str(e)
        try:
            out["history"] = _norm_list(se.history())
        except Exception:
            out["history"] = []
        try:
            fb, _ = _mod("feedback")
            out["feedback"] = fb.aggregate() if fb else {}
        except Exception:
            out["feedback"] = {}
        try:
            from bot import skillkit
            out["stats"] = skillkit.stats()
        except Exception:
            out["stats"] = {}
        return out

    @app.post("/api/skills/evolution/propose")
    def api_skills_propose(payload: dict):
        se, err = _mod("skill_evo")
        if err:
            return err
        try:
            if payload.get("from_feedback"):
                res = se.propose_from_feedback(str(payload.get("medium") or ""),
                                               str(payload.get("stage") or ""), cfg=_cfg())
            else:
                res = se.propose(str(payload.get("name") or ""),
                                 str(payload.get("medium") or ""),
                                 payload.get("stages") or [],
                                 str(payload.get("title") or ""),
                                 str(payload.get("content") or ""),
                                 reason=str(payload.get("reason") or ""),
                                 cfg=_cfg())
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "生成候选失败"))
            return {"ok": True, "candidate": res}
        except Exception as e:
            return _bad(f"生成候选失败：{e}", 500)

    @app.post("/api/skills/evolution/evaluate")
    def api_skills_evaluate(payload: dict):
        se, err = _mod("skill_evo")
        if err:
            return err
        try:
            res = se.evaluate_candidate(str(payload.get("id") or ""),
                                        str(payload.get("baseline") or ""), cfg=_cfg())
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "评测失败"))
            return {"ok": True, "result": res}
        except Exception as e:
            return _bad(f"评测失败：{e}", 500)

    @app.post("/api/skills/evolution/promote")
    def api_skills_promote(payload: dict):
        se, err = _mod("skill_evo")
        if err:
            return err
        try:
            res = se.promote(str(payload.get("id") or ""), force=bool(payload.get("force")),
                             approver=str(payload.get("approver") or "user"))
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "晋升失败"))
            return {"ok": True, "result": res}
        except Exception as e:
            return _bad(f"晋升失败：{e}", 500)

    @app.post("/api/skills/evolution/reject")
    def api_skills_reject(payload: dict):
        se, err = _mod("skill_evo")
        if err:
            return err
        try:
            return {"ok": True, "result": se.reject(str(payload.get("id") or ""),
                                                    str(payload.get("note") or ""))}
        except Exception as e:
            return _bad(f"驳回失败：{e}", 500)

    @app.post("/api/skills/evolution/rollback")
    def api_skills_rollback(payload: dict):
        se, err = _mod("skill_evo")
        if err:
            return err
        try:
            res = se.rollback(str(payload.get("name") or ""), str(payload.get("version") or ""))
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "回滚失败"))
            return {"ok": True, "result": res}
        except Exception as e:
            return _bad(f"回滚失败：{e}", 500)

    # ------------------------------------------------------------ v2.5.0 技能切换
    @app.post("/api/skills/evolution/toggle")
    def api_skills_toggle(payload: dict):
        se, err = _mod("skill_evo")
        if err:
            return err
        try:
            name = str(payload.get("name") or "")
            enabled = bool(payload.get("enabled"))
            res = se.toggle(name, enabled) if hasattr(se, "toggle") else {"ok": True, "enabled": enabled}
            return {"ok": True, "result": res}
        except Exception as e:
            return _bad(f"切换失败：{e}", 500)

    # ------------------------------------------------------------ v2.5.0 一致性修复
    @app.post("/api/fix-consistency")
    def api_fix_consistency(payload: dict):
        key = str(payload.get("key") or payload.get("id") or "")
        if not key:
            proj = _current_proj()
            if proj:
                key = proj.key
        if not key:
            return _bad("缺少 key")
        proj = get_project(key)
        if not proj:
            return _bad("作品不存在", 404)
        try:
            cont, cerr = _mod("continuity")
            if cerr:
                return cerr
            issue_id = str(payload.get("id") or payload.get("type") or "")
            if not issue_id:
                return _bad("缺少问题标识（id 或 type）")
            res = cont.fix(proj.path, issue_id)
            return {"ok": True, "key": key, "result": res}
        except Exception as e:
            return _bad(f"修复失败：{e}", 500)

    # ------------------------------------------------------------ v2.5.1 手动评分
    @app.post("/api/score")
    def api_score(payload: dict):
        key = str(payload.get("key") or "")
        if not key:
            proj = _current_proj()
            if proj:
                key = proj.key
        if not key:
            return _bad("缺少 key")
        proj = get_project(key)
        if not proj:
            return _bad("作品不存在", 404)
        try:
            import threading as _th
            from bot import writer as _w
            pdir = proj.path
            # 先尝试从现有质量报告读取
            q = _w.quality_score(pdir) if hasattr(_w, "quality_score") else None
            if q is not None:
                return {"ok": True, "score": q, "source": "existing_report"}
            # 没有评分 → 后台触发评分（调用 run_release_gate 或直接生成质量报告）
            def _bg_score():
                try:
                    proj.log("📊 正在生成质量评分…")
                    if hasattr(_w, "run_release_gate"):
                        _w.run_release_gate(os.path.basename(pdir))
                    else:
                        # 降级：直接调用 quality_report 生成
                        from bot import longform as _lf
                        if hasattr(_lf, "quality_report"):
                            _lf.quality_report(proj)
                except Exception as _e:
                    try:
                        proj.log(f"⚠️ 评分失败：{_e}")
                    except Exception:
                        pass
            _th.Thread(target=_bg_score, daemon=True).start()
            # 立即返回，告知用户后台正在评分
            # 同时用字数估算一个临时分数（每万字基准 60 分，上下浮动）
            try:
                plan = proj.plan() if hasattr(proj, "plan") else {}
                chs = (plan or {}).get("chapters") or []
                done = sum(1 for c in chs if isinstance(c, dict) and c.get("status") == "completed")
                est = min(85, 55 + done * 2) if done > 0 else None
            except Exception:
                est = None
            return {"ok": True, "score": est, "source": "estimating", "msg": "正在后台生成评分，请稍后刷新查看"}
        except Exception as e:
            return _bad(f"评分失败：{e}", 500)

    # ------------------------------------------------------------ 固定评测
    @app.get("/api/eval/cases")
    def api_eval_cases(medium: str = ""):
        ev, err = _mod("eval")
        if err:
            return err
        try:
            return {"ok": True, "items": ev.list_cases(medium)}
        except Exception as e:
            return _bad(f"读取评测集失败：{e}", 500)

    @app.get("/api/eval/runs")
    def api_eval_runs(limit: int = 20):
        ev, err = _mod("eval")
        if err:
            return err
        try:
            return {"ok": True, "items": ev.list_runs(min(max(limit, 1), 100))}
        except Exception as e:
            return _bad(f"读取评测记录失败：{e}", 500)

    @app.post("/api/eval/compare")
    def api_eval_compare(payload: dict):
        ev, err = _mod("eval")
        if err:
            return err
        try:
            res = ev.compare(str(payload.get("baseline") or ""),
                             str(payload.get("candidate") or ""),
                             payload.get("case_ids"), cfg=_cfg())
            if isinstance(res, dict) and res.get("ok") is False:
                return _bad(str(res.get("error") or "评测失败"))
            return {"ok": True, "result": res}
        except Exception as e:
            return _bad(f"评测失败：{e}", 500)

    # ------------------------------------------------------------ 会话流式
    @app.post("/api/chat/stream")
    async def api_chat_stream(request: Request):
        if not chat_func:
            return _bad("流式接口未启用", 501)
        try:
            payload = await request.json()
        except Exception:
            return _bad("请求体必须是 JSON")
        message = str(payload.get("message") or "")
        if not message.strip():
            return _bad("消息不能为空")
        sid = str(payload.get("session_id") or uuid.uuid4().hex)
        # v2.3.0：key 随消息透传给会话状态机（idle + 有效 key → 绑定本书，绝不误起新书）
        key = str(payload.get("key") or "") or None

        # v2.6.0：持久化用户消息到聊天历史
        _chat_proj = get_project(key) if key else None
        if _chat_proj:
            _save_chat_msg(_chat_proj, "user", message)

        async def gen():
            t0 = time.time()
            # v2.9.0：对话上下文自动压缩（只影响展示对话的摘要与 @Agent 上下文，
            # 后台创作流水线不经此路径）。压缩结果落 chat_summary.json 并随 start 帧告知前端。
            _digest = None
            if _chat_proj:
                try:
                    _hist = _load_chat_log(_chat_proj, limit=5000)
                    _cres = chatsum.maybe_compress(_chat_proj.path, _hist) if _hist else None
                    if _cres and _cres.get("compressed"):
                        _digest = _cres["summary"]
                        yield _sse("compressed", {"count": _cres["count"],
                                                  "summary": _cres["summary"]})
                except Exception:
                    _digest = None
            yield _sse("start", {"session_id": sid, "ts": int(t0)})

            # 没有任何可用供应商：消息照收，直接给友好兜底；绝不进 chat_func
            # （不碰网络、不推进会话状态机）。
            if not _provider_ready():
                yield _sse("status", {"stage": "thinking", "text": "正在检查模型配置…"})
                yield _sse("delta", {"text": _NO_PROVIDER_TEXT})
                yield _sse("done", {"ok": True, "fallback": True, "text": _NO_PROVIDER_TEXT,
                                    "session_id": sid, "elapsed": round(time.time() - t0, 2)})
                return

            yield _sse("status", {"stage": "thinking", "text": "正在理解你的需求…"})

            # chat_func 是同步阻塞的（一轮可能几分钟），丢到 daemon 线程里跑，
            # 主协程每 ~2s 发一次心跳。用 Event 而不是 wait_for(queue.get())：
            # Event 的状态不会被取消吞掉，边界时刻完成也不会丢结果。
            loop = asyncio.get_running_loop()
            ready = asyncio.Event()
            box: list = []

            def _work():
                try:
                    # v2.9.0：压缩摘要随调用传入（chat() 支持 digest 参数时）；旧签名自动回退
                    try:
                        result = chat_func(sid, message, key, digest=_digest)
                    except TypeError:
                        result = chat_func(sid, message, key)
                    # v2.8.0：AI 回复落盘移到 worker 线程——原来放在生成器里，
                    # 客户端中途断开（等待期常达几分钟）会取消生成器，落盘被跳过，
                    # 造成聊天记录「只有问没有答」。
                    if _chat_proj:
                        _save_chat_msg(_chat_proj, "assistant",
                                       result if isinstance(result, str) else str(result or ""),
                                       {"elapsed": round(time.time() - t0, 2)})
                    box.append((True, result))
                except BaseException as e:  # 原样带回事件循环，转成 error 帧
                    box.append((False, e))
                finally:
                    try:
                        loop.call_soon_threadsafe(ready.set)
                    except RuntimeError:
                        pass  # 事件循环已关闭（客户端早退）：结果丢弃，线程自然结束

            worker = threading.Thread(target=_work, name="v2-chat-worker", daemon=True)
            worker.start()

            # v4.3.0+v4.12.2：等待期间推送引擎过程行。
            # v4.12.2 改读运行日志文件而非内存 events——非小说引擎（children 等）用
            # 自己的 Project 类（无 events 属性共享），writer.Project.events 永远空 → 心跳无进度。
            _log_proj = get_project(key) if key else None
            _log_idx = 0
            _log_file = os.path.join(_log_proj.path, "运行日志.txt") if _log_proj else ""
            while not ready.is_set():
                try:
                    await asyncio.wait_for(ready.wait(), timeout=_HEARTBEAT_SECS)
                except (TimeoutError, asyncio.TimeoutError):  # v4.7.1 Python<3.11 时 asyncio.TimeoutError 不是内置 TimeoutError 子类
                    pushed_line = ""
                    if _log_file and os.path.exists(_log_file):
                        try:
                            lines = open(_log_file, encoding="utf-8").read().splitlines()
                            if len(lines) > _log_idx:
                                _log_idx = len(lines)
                                pushed_line = lines[-1]
                        except Exception:
                            pass
                    if pushed_line:
                        yield _sse("status", {"stage": "writing",
                                              "text": pushed_line.split("] ", 1)[-1]})
                    else:
                        yield _sse("status", {"stage": "thinking",
                                              "text": _waiting_text(time.time() - t0)})

            ok, value = box[0] if box else (False, RuntimeError("会话处理未返回结果"))
            if not ok:
                err = f"处理失败：{value}"
                yield _sse("error", {"error": err})
                yield _sse("done", {"ok": False, "error": err,
                                    "elapsed": round(time.time() - t0, 2)})
                return
            text = value if isinstance(value, str) else str(value or "")
            # v2.6.0：持久化 AI 回复（v2.8.0 起移到 worker 线程，见 _work 注释）
            # 旧状态机返回整段文本；按小段推送，让前端有真实的逐字阅读感。
            for i in range(0, len(text), 28):
                yield _sse("delta", {"text": text[i:i + 28]})
                await asyncio.sleep(0.012)

            done_payload = {"ok": True, "text": text, "session_id": sid,
                            "elapsed": round(time.time() - t0, 2)}

            # ---------- 追章模式（v2.2.2）----------
            # 本轮触发了后台写作（payload.key 指向的作品 running / in_progress）时，
            # 继续轮询写作计划，把新完成的章节逐字推给前端。开局快照里已完成的
            # 章节不重推，避免历史章节在每轮对话里重复刷屏。心跳与追章共用这一
            # 个循环；客户端断开时 CancelledError 从 sleep 处正常终止生成器，
            # 写作线程不受影响。
            proj = _follow_project(payload, get_project)
            st = _proj_status(proj) if proj else {}
            # v2.4.0 修复假完成：追章模式未进入时，检查是否有 pending 章节
            # 如果有 pending 但没在写，发 warning 而非静默 done
            if not (proj is not None and (st.get("running") or st.get("state") == "in_progress")):
                _pending = proj and _next_pending(_proj_plan(proj))
                if _pending:
                    yield _sse("status", {"stage": "warning",
                                          "text": f"《{st.get('name') or '作品'}》尚有未完成章节，但写作未启动。请在聊天中说「继续创作」或「全部章节创作」来启动。"})
                    done_payload = {"ok": True, "text": text,
                                    "session_id": sid, "elapsed": round(time.time() - t0, 2),
                                    "chapters": [], "warning": "pending_not_writing"}

            if proj is not None and (st.get("running") or st.get("state") == "in_progress"):
                seen = set()
                for c in _proj_plan(proj).get("chapters") or []:
                    if isinstance(c, dict) and str(c.get("status")) == "completed":
                        no = _as_int(c.get("chapterNumber"))
                        if no is not None:
                            seen.add(no)
                pushed: list = []
                deadline = time.time() + _FOLLOW_TIMEOUT_SECS
                while True:
                    plan = _proj_plan(proj)
                    for c in plan.get("chapters") or []:
                        if not isinstance(c, dict) or str(c.get("status")) != "completed":
                            continue
                        no = _as_int(c.get("chapterNumber"))
                        if no is None or no in seen:
                            continue
                        seen.add(no)
                        words = _as_int(c.get("wordCount")) or 0
                        yield _sse("chapter_start", {"no": no,
                                                     "title": str(c.get("title") or f"第{no}章")})
                        raw = _read_chapter_file(proj, c.get("filePath"))
                        # v4.12.11：对话追章只发清洗后的正文（引子+正文），剥本章概要/章节备注
                        body = _clean_chapter_text(raw)
                        for i in range(0, len(body), _CHAPTER_CHUNK):
                            yield _sse("chapter_delta", {"no": no, "text": body[i:i + _CHAPTER_CHUNK]})
                            await asyncio.sleep(_CHAPTER_DELTA_GAP)
                        # v2.9.0：章节完成卡片元数据（完成时间/字数/token/质量分）
                        _meta = (plan.get("meta") or {})
                        _score = ((_meta.get("chapter_scores") or {}).get(str(no)) or {}).get("score")
                        _card = {"no": no, "title": str(c.get("title") or f"第{no}章"),
                                 "words": words,
                                 "tokens": _as_int(c.get("tokens")),
                                 "quality": _score,
                                 "ts": c.get("completedAt") or "",
                                 "key": str(payload.get("key") or "")}
                        yield _sse("chapter_done", _card)
                        pushed.append({"no": no, "words": words})
                        # v2.9.0：章节完成记录也进聊天留档（type=chapter），刷新后历史完整重放
                        if _chat_proj:
                            _save_chat_msg(_chat_proj, "assistant", body,
                                           {"type": "chapter", "no": no, "title": _card["title"],
                                            "words": words, "tokens": _card["tokens"],
                                            "quality": _score, "completedAt": _card["ts"]})

                    st = _proj_status(proj)
                    state = str(st.get("state") or "")
                    if state in ("completed", "failed") and not st.get("running"):
                        break
                    if state == "paused" or st.get("paused"):
                        state = "paused"
                        break
                    if time.time() >= deadline:
                        state = "timeout"
                        break
                    # 章与章之间继续发思考心跳，文案带上下一个待写章节的进度
                    nxt = _next_pending(plan)
                    yield _sse("status", {"stage": "writing",
                                          "text": (f"正在创作第{nxt[0]}章：{nxt[1]}…") if nxt
                                          else "正在收尾全部章节…"})
                    await asyncio.sleep(_HEARTBEAT_SECS)

                name = str(st.get("name") or "作品")
                d = _as_int(st.get("done")) or 0
                total = _as_int(st.get("total")) or 0
                words_sum = _as_int(st.get("words")) or 0
                if state == "paused":
                    tail = f"《{name}》写作已暂停（当前 {d}/{total} 章），随时可以让我继续。"
                elif state == "timeout":
                    tail = f"《{name}》仍在后台创作（当前 {d}/{total} 章），稍后回来看新章节就好。"
                else:
                    tail = (f"《{name}》本轮新增 {len(pushed)} 章，全书进度 {d}/{total} 章，"
                            f"累计约 {words_sum} 字。")
                done_payload = {"ok": True, "text": f"{text}\n\n{tail}".strip(),
                                "session_id": sid, "elapsed": round(time.time() - t0, 2),
                                "chapters": pushed}

            yield _sse("done", done_payload)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ 聊天历史
    @app.get("/api/chat/history")
    def api_chat_history(key: str):
        """读取作品的全部聊天历史（JSONL 格式，按时间正序）。
        v2.8.0 修复：_project 返回 (proj, err) 元组，此前直接当 proj 用导致
        AttributeError → /api/chat/history 恒 500，聊天记录永远读不出来。"""
        proj, err = _project(get_project, key)
        if err:
            return err
        items = _load_chat_log(proj, limit=5000)
        return {"ok": True, "items": items}

    @app.post("/api/chat/clear")
    def api_chat_clear(payload: dict):
        """清空作品的聊天历史。"""
        key = str(payload.get("key") or "")
        proj, err = _project(get_project, key)
        if err:
            return err
        try:
            p = _chat_log_path(proj)
            if os.path.exists(p):
                os.remove(p)
            return {"ok": True}
        except Exception as e:
            return _bad(f"清空失败：{e}", 500)

    # ------------------------------------------------------------ 版本与更新检查
    @app.get("/api/version")
    def api_version():
        try:
            from bot.version import current as _version_current
            v = _version_current()
        except Exception:
            v = "0.0.0"
        return {"ok": True, "version": v, "time": int(time.time())}

    @app.get("/api/check-update")
    def api_check_update():
        """检查 GitHub 最新 release 版本号，与本地版本比较。
        本地配置 GITHUB_REPO（如 yim-lit/yimu-lit）或通过环境变量 GITHUB_REPO 设置。
        网络不通时优雅降级。
        """
        import urllib.request
        repo = os.environ.get("GITHUB_REPO", "").strip()
        if not repo:
            # 尝试从 .git/config 读取 remote origin
            gitcfg = os.path.join(ROOT, ".git", "config")
            if os.path.exists(gitcfg):
                try:
                    for line in open(gitcfg, "r", errors="ignore"):
                        line = line.strip()
                        if "github.com:" in line and ".git" in line:
                            # git@github.com:user/repo.git
                            repo = line.split("github.com:")[-1].rstrip(" ").rstrip(".git")
                            break
                        elif "github.com/" in line and ".git" in line:
                            # https://github.com/user/repo.git
                            repo = line.split("github.com/")[-1].rstrip(" ").rstrip(".git")
                            break
                except Exception:
                    pass
        if not repo:
            return {"ok": False, "error": "未配置 GitHub 仓库地址（设置环境变量 GITHUB_REPO=user/repo 或在 .git/config 中配置 remote origin）"}

        try:
            local_v = ""
            try:
                from bot.version import current as _version_current
                local_v = _version_current()
            except Exception:
                local_v = "0.0.0"

            url = f"https://api.github.com/repos/{repo}/releases/latest"
            req = urllib.request.Request(url, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "yimu-lit-check-update"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            remote_tag = (data.get("tag_name") or "").lstrip("v")
            remote_url = data.get("html_url") or f"https://github.com/{repo}/releases/latest"
            body = (data.get("body") or "")[:2000]

            def _cmp(a, b):
                """返回 True 如果 b > a（版本号比较）"""
                try:
                    pa = [int(x) for x in a.split(".")]
                    pb = [int(x) for x in b.split(".")]
                    for i in range(max(len(pa), len(pb))):
                        va = pa[i] if i < len(pa) else 0
                        vb = pb[i] if i < len(pb) else 0
                        if vb > va:
                            return True
                        if vb < va:
                            return False
                    return False
                except Exception:
                    return b != a

            has_update = _cmp(local_v, remote_tag)
            return {
                "ok": True,
                "local_version": local_v,
                "latest_version": remote_tag,
                "has_update": has_update,
                "release_url": remote_url,
                "changelog": body,
                "repo": repo
            }
        except Exception as e:
            es = str(e)
            hint = ""
            if "timed out" in es.lower() or "timeout" in es.lower():
                hint = "（网络超时，请检查网络连接后重试）"
            elif "urlopen" in es.lower() or "connection" in es.lower() or "refused" in es.lower():
                hint = "（无法连接 GitHub，请检查网络或代理设置）"
            elif "403" in es or "rate" in es.lower():
                hint = "（GitHub API 速率限制，请稍后再试）"
            elif "404" in es:
                hint = "（未找到 Release，请确认仓库地址正确且已发布 Release）"
            return {"ok": False, "error": f"检查更新失败：{e} {hint}", "hint": hint}

    return app
