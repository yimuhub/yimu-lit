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
MAX_PER_BOOK = 600          # 内存环形缓冲上限
JSONL_MAX_BYTES = 2 * 1024 * 1024
JSONL_KEEP = 1500           # 裁剪后保留行数

_LOCK = threading.Lock()
_RING: dict = {}            # key -> deque[dict]
_SEQ: dict = {}             # key -> 自增序号
_STREAM: dict = {}          # key -> {"agent","chapter","text","ts"}（当前流式步骤；done 后保留至下一次 emit 流）


def _jsonl_path(key) -> str:
    return os.path.join(PROJECTS_DIR, str(key), "创作事件.jsonl")


def _agent_label(agent) -> str:
    try:
        from bot import agents as _ag
        info = _ag.AGENTS.get(str(agent or ""))
        return (info or {}).get("label") or str(agent or "系统")
    except Exception:
        return str(agent or "系统")


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
         chapter=0, level="info", ms=0):
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


def since(key, cursor=0, limit=60):
    """增量拉取：返回 {items:[no>cursor 的事件], cursor:最新序号}。"""
    try:
        key = str(key or "")
        with _LOCK:
            ring = _RING.get(key) or deque()
            cur = _SEQ.get(key, 0)
            items = [dict(e) for e in ring if e.get("no", 0) > int(cursor or 0)]
        return {"items": items[:max(1, min(int(limit or 60), 200))], "cursor": cur}
    except Exception:
        return {"items": [], "cursor": 0}


def emit_delta(key, agent, chapter, chunk, done=False, title=""):
    """流式步骤：chunk 为增量文本。done=True 结束本次流（保留全文到下一次流开始）。"""
    try:
        key = str(key or "")
        if not key:
            return
        with _LOCK:
            st = _STREAM.get(key)
            if st is None or st.get("agent") != str(agent or "") or st.get("chapter") != int(chapter or 0):
                st = _STREAM.setdefault(key, {"agent": str(agent or ""), "text": ""})
            st.update({"agent": str(agent or ""), "chapter": int(chapter or 0),
                       "title": str(title or st.get("title") or ""),
                       "ts": int(time.time())})
            if chunk:
                st["text"] = (st.get("text") or "") + str(chunk)[-20000:]
            if done:
                st["done"] = True
    except Exception:
        pass


def stream_state(key, offset=0):
    """前端拉当前流式步骤：{agent, chapter, title, offset, text(自 offset 起), done}；无流返回 {active: False}。"""
    try:
        st = _STREAM.get(str(key or ""))
        if not st or not st.get("text"):
            return {"active": False}
        text = st.get("text") or ""
        off = max(0, int(offset or 0))
        return {"active": True, "agent": st.get("agent"), "agent_label": _agent_label(st.get("agent")),
                "chapter": st.get("chapter", 0), "title": st.get("title") or "",
                "offset": len(text), "text": text[off:], "done": bool(st.get("done"))}
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
