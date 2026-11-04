"""v4.21.0：创作事件总线——把「哪个岗位在做什么/想了什么/产出了什么」结构化广播。

供对话内活动流（Agent 逐条动作 + 思考 + 产出卡）、本章创作档案、并行写作可视化共用。
- emit():   离散步骤（start/thinking/done），写入内存环形缓冲 + 追加 projects/<key>/创作事件.jsonl
- emit_delta()/stream_state(): 流式步骤（作家正文、规划师大纲的逐字输出），前端按 offset 拉增量
- since():  前端按 cursor 增量拉取（配合 /api/book-live 的 2.5s 轮询）
- chapter_events(): 按章过滤全部事件（含历史，从 JSONL 读）——三期「本章档案」数据源

设计约束：绝不抛异常、绝不阻塞写作线程（一切失败静默）；JSONL 超 2MB 裁剪留尾。
"""
import json, os, threading, time
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_DIR = os.path.join(ROOT, "projects")
MAX_PER_BOOK = 1200         # 内存环形缓冲上限
JSONL_MAX_BYTES = 8 * 1024 * 1024
JSONL_KEEP = 3000           # 裁剪后保留行数（创作过程要求永久保留，随书删除才清）

_LOCK = threading.RLock()
_HYDRATED = {}             # key -> True（每进程每书只灌一次）


def _hydrate(key):
    """环形缓冲为空且落盘 JSONL 有历史时（典型：服务重启），把最近 300 条
    灌回内存——保证「刷新/重启后创作过程仍在」。幂等：只灌一次。
    v4.24.16 修复：此前只看 _HYDRATED 标志不看 ring——本 boot 已 emit 过事件
    （如「继续」后前端才首次轮询 book-live）时，JSONL 尾部 300 条（含刚 emit 的
    那几条）被重新编号后二次灌入，同一事件出现两份、cursor 虚高。现在 ring
    非空即跳过：内存里的就是最新的，历史由 chapter_events/读 JSONL 兜底。"""
    key = str(key or "")
    if not key or _HYDRATED.get(key):
        return
    _HYDRATED[key] = True
    with _LOCK:
        if _RING.get(key):
            return   # 本 boot 已有事件：不灌，防重复
    path = _jsonl_path(key)
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-300:]
        with _LOCK:
            ring = _RING.setdefault(key, deque(maxlen=MAX_PER_BOOK))
            max_no = _SEQ.get(key, 0)
            for line in lines:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if not isinstance(e, dict):
                    continue
                # 顺序重编号：历史文件里 no 可能跨启动碰撞（旧版无自愈），
                # 回放只关心先后顺序，统一重新编号最稳。
                max_no += 1
                e["no"] = max_no
                ring.append(e)
            _SEQ[key] = max(_SEQ.get(key, 0), max_no)
    except Exception:
        pass

_RING: dict = {}            # key -> deque[dict]
_SEQ: dict = {}             # key -> 自增序号
_STREAM: dict = {}          # key -> {"agent","chapter","text","ts"}（当前流式步骤；done 后保留至下一次 emit 流）


def _jsonl_path(key) -> str:
    return os.path.join(PROJECTS_DIR, str(key), "创作事件.jsonl")


def _agent_label(agent) -> str:
    a = str(agent or "")
    if a == "system":
        return "系统"   # v4.26.1：系统事件中文名（历史事件由前端兜底）
    try:
        from bot import agents as _ag
        info = _ag.AGENTS.get(a)
        return (info or {}).get("label") or a or "系统"
    except Exception:
        return a or "系统"


def _trim_jsonl(path):
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= JSONL_MAX_BYTES:
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-JSONL_KEEP:])
    except Exception:
        pass


def emit(key, agent, phase="done", title="", thinking="", output="",
         chapter=0, level="info", ms=0, stream_text="", think=False):
    """广播一条离散创作步骤。phase: start（进行中）/ done（完成）/ warn。
    title: 动作名（如「生成人物档案」「写第3章」）；thinking: 思考摘要；output: 产出摘要；
    chapter: 关联章节号（0=全书级）；ms: 该步耗时毫秒。"""
    try:
        key = str(key or "")
        if not key:
            return
        now = time.time()
        entry = {
            "agent": str(agent or "system"),
            "agent_label": _agent_label(agent),
            "phase": str(phase or "done"),
            "title": str(title or "")[:120],
            "thinking": str(thinking or "")[:600],
            "output": str(output or "")[:600],
            "chapter": int(chapter or 0),
            "level": str(level or "info"),
            "ms": int(ms or 0),
            "ts": int(now),
            "time": time.strftime("%H:%M:%S"),
        }
        if stream_text:
            entry["stream_text"] = str(stream_text)[:30000]
        if think:
            entry["is_thinking"] = True
        path = _jsonl_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _LOCK:
            seq = _SEQ.get(key, 0) + 1
            _SEQ[key] = seq
            entry["no"] = seq
            ring = _RING.setdefault(key, deque(maxlen=MAX_PER_BOOK))
            ring.append(entry)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            _trim_jsonl(path)
    except Exception:
        pass


def last_event_ts(key) -> float:
    """该书活动流最近一条事件的时间戳（秒级 epoch）；无事件返回 0。
    v4.26.0：供总编辑做「引擎是否还活着」的新鲜度判据（非小说引擎无线程注册表）。"""
    try:
        key = str(key or "")
        _hydrate(key)
        with _LOCK:
            ring = _RING.get(key)
            if not ring:
                return 0.0
            return float(max(int(e.get("ts") or 0) for e in ring))
    except Exception:
        return 0.0


def since(key, cursor=0, limit=60):
    """增量拉取：返回 {items:[no>cursor 的事件], cursor:最新序号}。
    v4.21.3：重启后自动从落盘 JSONL 自愈，历史创作过程永久可回放。"""
    try:
        key = str(key or "")
        _hydrate(key)
        with _LOCK:
            ring = _RING.get(key) or deque()
            cur = _SEQ.get(key, 0)
            items = [dict(e) for e in ring if e.get("no", 0) > int(cursor or 0)]
        # v4.21.4：全量回放（cursor=0）时返回最新的 limit 条——此前返回最旧的 60 条
        # 而游标直接跳到最新，导致含正文全文的最新事件被整段跳过。
        items = items[-max(1, min(int(limit or 60), 200)):]
        return {"items": items, "cursor": cur}
    except Exception:
        return {"items": [], "cursor": 0}


def emit_delta(key, agent, chapter, chunk, done=False, title="", tchunk="", final_text=None):
    """流式步骤：chunk 为正文增量；tchunk 为模型思维链增量（reasoning），分开累积。
    done=True 结束本次流：正文全文 + 模型思考全文各自落盘（永久记录，随书删除才清）。
    v4.24.16：final_text 传入时以它为准重置正文缓冲——流式失败静默回退 chat() 的场景，
    缓冲里只有 partial 前缀，落盘全文必须与实际返回一致。"""
    try:
        key = str(key or "")
        if not key:
            return
        with _LOCK:
            st = _STREAM.get(key)
            if st is None or st.get("agent") != str(agent or "") or st.get("chapter") != int(chapter or 0):
                st = _STREAM.setdefault(key, {"agent": str(agent or ""), "text": "", "thinking": ""})
            st.update({"agent": str(agent or ""), "chapter": int(chapter or 0),
                       "title": str(title or st.get("title") or ""),
                       "ts": int(time.time())})
            if chunk:
                st["text"] = (st.get("text") or "") + str(chunk)[-20000:]
            if tchunk:
                st["thinking"] = (st.get("thinking") or "") + str(tchunk)[-30000:]
            if final_text is not None:
                st["text"] = str(final_text)   # v4.24.16：以调用方实际返回为准（回退场景缓冲只有前缀）
            if done:
                st["done"] = True
                # v4.21.3/4：流式全文永久落盘——正文与模型思考各自作为 done 事件
                # 写入 ring + JSONL，刷新/重启后前端原样回放（豆包式对话留存）。
                _text = st.get("text") or ""
                _think = st.get("thinking") or ""
                if _think.strip():
                    emit(key, st.get("agent"), "done",
                         title=(st.get("title") or "实时输出") + "（模型思考）",
                         chapter=st.get("chapter", 0),
                         stream_text=_think[:30000], think=True)
                if _text.strip():
                    emit(key, st.get("agent"), "done",
                         title=(st.get("title") or "实时输出") + "（正文全文）",
                         chapter=st.get("chapter", 0),
                         stream_text=_text[:30000])
                st["text"] = ""
                st["thinking"] = ""
    except Exception:
        pass


def stream_state(key, offset=0, think_offset=0):
    """前端拉当前流式步骤：正文与模型思考两条独立增量流；无流返回 {active: False}。"""
    try:
        st = _STREAM.get(str(key or ""))
        if not st or (not st.get("text") and not st.get("thinking")):
            return {"active": False}
        text = st.get("text") or ""
        think = st.get("thinking") or ""
        off = max(0, int(offset or 0))
        toff = max(0, int(think_offset or 0))
        return {"active": True, "agent": st.get("agent"), "agent_label": _agent_label(st.get("agent")),
                "chapter": st.get("chapter", 0), "title": st.get("title") or "",
                "offset": len(text), "text": text[off:], "done": bool(st.get("done")),
                "think_offset": len(think), "thinking": think[toff:]}
    except Exception:
        return {"active": False}


def chapter_events(key, chapter, limit=400):
    """三期「本章档案」：按章过滤事件（内存 ring + 历史 JSONL 去重合并，新→旧）。"""
    try:
        key, chapter = str(key or ""), int(chapter or 0)
        path = _jsonl_path(key)
        merged, seen = [], set()
        with _LOCK:
            ring = list(_RING.get(key) or deque())
        for e in reversed(ring):
            if chapter and e.get("chapter") not in (0, chapter):
                continue
            no = e.get("no")
            if no in seen:
                continue
            seen.add(no)
            merged.append(e)
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in reversed(f.readlines()):
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if chapter and e.get("chapter") not in (0, chapter):
                        continue
                    no = e.get("no")
                    if no in seen:
                        continue
                    seen.add(no)
                    merged.append(e)
        return merged[:max(1, min(int(limit or 400), 1000))]
    except Exception:
        return []


def reset(key):
    """删除作品/重开一本时清内存态（JSONL 随目录删除，不必单独清）。"""
    try:
        with _LOCK:
            k = str(key or "")
            _RING.pop(k, None)
            _SEQ.pop(k, None)
            _STREAM.pop(k, None)
    except Exception:
        pass
