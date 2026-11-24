"""无限画布（创作工作流白板）HTTP 接口（v5.8.0）。

与 v5.6.0 资料画布（bot/webcanvas.py，数据驱动的图谱视图）定位不同：这里是
**自由白板**——想法→大纲→章节卡的工作流工具，节点自由摆放、自由连线、AI 扩展。

端点（FastAPI APIRouter，**不改 app.py**，主会话两行挂载）：

  GET  /api/flow/board?key=      读整板。key 可空 = 全局白板（存 _global.json）；
                                 非空按书隔离，改名后的旧 key 走 writer.resolve_key
                                 别名解析落到新家。返回 {ok, key, nodes, edges, updatedAt}。
  POST /api/flow/board           整板保存（前端防抖后全量覆盖）。**简单覆盖、无乐观锁**：
                                 单机单用户的白板场景下 last-writer-wins 足够，把
                                 updatedAt 乐观检查的复杂度省给前端防抖（600ms）。
  POST /api/flow/expand          AI 扩展节点：{key?, text, kind:'note'|'idea', count?}
                                 → bot/llm.py（scene="画布扩展"，轻量 JSON 输出）→
                                 [{title, text}]；失败翻成中文 HTTPException
                                 （v5.5.1 韧性层的中文报错由 llm.py 内部完成）。
  GET  /api/flow/chapters?key=   章卡导入：直接本地读 02d-章卡.jsonl（轻量懒读，
                                 **不走 /api/plan**——那条链路会触发 ensure_plan 的
                                 LLM 规划与全量树整形，导入只需要章号/标题/状态/一句话摘要）。

存储：data/flow-boards/<safe(key)>.json，锁 + safeio.atomic_write_json 原子写
（webcanvas 覆盖层同款）。画布内容是作者工作区草稿（纯净域），不进正文/导出链路，
后端不做正文清洗；导入的章卡文本只读透传。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime

from fastapi import APIRouter, HTTPException

from . import llm, paths, writer
from .safeio import atomic_write_json, safe_join

router = APIRouter()

_BOARD_LOCK = threading.Lock()

GLOBAL_BOARD_KEY = "_global"
BOARD_DIR = "flow-boards"
CARD_FILE = "02d-章卡.jsonl"

MAX_NODES = 500
MAX_EDGES = 1200
MAX_TITLE = 80
MAX_TEXT = 4000
MAX_ID = 64
MAX_CARDS = 300
MAX_SUMMARY = 90
MAX_EXPAND_TEXT = 3000
MAX_KEY = 120

NODE_KINDS = ("note", "chapter", "ai")
NODE_COLORS = ("plain", "accent", "success", "warn", "danger")
EXPAND_KINDS = ("note", "idea")
EXPAND_COUNT_DEFAULT = 4
EXPAND_COUNT_MIN = 3
EXPAND_COUNT_MAX = 5

_EXPAND_SCENE = "画布扩展"
_EXPAND_SYSTEM = "你是创作工作流白板里的想法扩展助手，只输出严格 JSON。"

# Windows 保留字符 + 控制字符：这些出现在 key 里一律拒绝（目录名不会含它们，
# 手工构造的请求也不该绕过），其余 Unicode（中文/空格/全角括号）都放行。
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- key 与路径

def board_id(key: str) -> str:
    """HTTP key → 白板存储名。空 = 全局 _global；含分隔符/保留字符/点开头 400。

    注意：这里不做目录存在性检查——白板 key 只决定文件名，作品被删后草稿
    白板仍应可读（资料画布 404 是因为要聚合项目文件，白板不需要）。"""
    key = str(key or "").strip()
    if not key:
        return GLOBAL_BOARD_KEY
    try:
        nk = writer.resolve_key(key)
        if nk and nk != key:
            key = nk
    except Exception:
        pass  # 别名解析失败按原 key 走
    if len(key) > MAX_KEY or key.startswith(".") or _UNSAFE_NAME_RE.search(key):
        raise HTTPException(status_code=400, detail="作品 key 非法")
    return key


def board_path(safe_id: str) -> str:
    """白板文件路径：safe_join 二道防线（realpath + 越界检查）。"""
    return safe_join(paths.data(BOARD_DIR), f"{safe_id}.json", label="白板 key")


def _project_dir(key: str) -> str:
    """章卡导入用：key → 真实目录，必须存在（章卡是项目数据）。"""
    key = str(key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="缺少作品 key")
    try:
        pdir = writer._project_dir(key)
    except Exception:
        pdir = None
    if not pdir or not os.path.isdir(pdir):
        raise HTTPException(status_code=404, detail="作品不存在或已被删除")
    return pdir


# ---------------------------------------------------------------- 整板清洗

def _num(v, default=0.0) -> float:
    try:
        n = float(v)
        return n if n == n and n not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


def _clean_node(raw, fallback_id: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    nid = str(raw.get("id") or fallback_id).strip()[:MAX_ID]
    if not nid:
        return None
    kind = str(raw.get("kind") or "note")
    if kind not in NODE_KINDS:
        kind = "note"
    color = str(raw.get("color") or "plain")
    if color not in NODE_COLORS:
        color = "plain"
    try:
        n = int(raw.get("n"))
        if not 0 <= n <= 99999:
            n = None
    except (TypeError, ValueError):
        n = None
    return {
        "id": nid,
        "kind": kind,
        "x": round(_num(raw.get("x")), 1),
        "y": round(_num(raw.get("y")), 1),
        "w": max(120.0, min(900.0, _num(raw.get("w"), 220))),
        "h": max(72.0, min(1600.0, _num(raw.get("h"), 150))),
        "title": str(raw.get("title") or "")[:MAX_TITLE],
        "text": str(raw.get("text") or "")[:MAX_TEXT],
        "color": color,
        "n": n,
    }


def sanitize_board(obj) -> dict:
    """任意来源（磁盘/HTTP）的板数据 → 规范 {nodes, edges}。

    · 节点：字段清洗 + 数量上限；id 缺失按序补 n1/n2…，重复 id 丢弃后至；
    · 边：from/to 必须指向存在的节点，去自环、去重（保留先出现的方向），
      方向语义 a→b（有向），a→b 与 b→a 是两条不同的线。"""
    if not isinstance(obj, dict):
        obj = {}
    raw_nodes = obj.get("nodes") if isinstance(obj.get("nodes"), list) else []
    raw_edges = obj.get("edges") if isinstance(obj.get("edges"), list) else []

    nodes: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_nodes[:MAX_NODES]):
        n = _clean_node(raw, f"n{i + 1}")
        if not n or n["id"] in seen:
            continue
        seen.add(n["id"])
        nodes.append(n)

    edges: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()
    for raw in raw_edges[:MAX_EDGES]:
        if not isinstance(raw, dict):
            continue
        a = str(raw.get("from") or "").strip()[:MAX_ID]
        b = str(raw.get("to") or "").strip()[:MAX_ID]
        if not a or not b or a == b or a not in seen or b not in seen:
            continue
        pk = (a, b)
        if pk in seen_edges:
            continue
        seen_edges.add(pk)
        edges.append({"from": a, "to": b})

    return {"nodes": nodes, "edges": edges}


def read_board(safe_id: str) -> dict:
    try:
        with open(board_path(safe_id), encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError, TypeError):
        return {"nodes": [], "edges": [], "updatedAt": ""}
    board = sanitize_board(obj)
    board["updatedAt"] = str(obj.get("updated_at") or "") if isinstance(obj, dict) else ""
    return board


def write_board(safe_id: str, nodes: list[dict], edges: list[dict]) -> dict:
    payload = {
        "version": 1,
        "key": safe_id,
        "nodes": nodes,
        "edges": edges,
        "updated_at": _now(),
    }
    with _BOARD_LOCK:
        try:
            atomic_write_json(board_path(safe_id), payload)
        except Exception as err:
            raise HTTPException(status_code=500, detail=f"保存白板失败：{err}") from err
    return payload


# ---------------------------------------------------------------- 章卡导入

def _card_summary(card: dict) -> str:
    """章卡 → 一句话摘要：goal 优先，缺了退 conflict/turn，再退空。"""
    for field in ("goal", "conflict", "turn", "payoff", "cliffhanger"):
        v = str(card.get(field) or "").strip()
        if v:
            return re.sub(r"\s+", " ", v)[:MAX_SUMMARY]
    return ""


def load_chapters(pdir: str) -> list[dict]:
    """02d-章卡.jsonl → [{n, title, status, summary}]（按章号升序，坏行跳过）。

    只读不改；状态缺省 planned（与 longform._norm_status_card 的词表对齐）。"""
    path = os.path.join(pdir, CARD_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            card = json.loads(line)
        except ValueError:
            continue
        if not isinstance(card, dict):
            continue
        try:
            n = int(card.get("n"))
        except (TypeError, ValueError):
            continue
        title = re.sub(r"\s+", " ", str(card.get("title") or "")).strip()[:40] or f"第{n}章"
        status = str(card.get("status") or "planned").strip().lower()[:12] or "planned"
        out.append({"n": n, "title": title, "status": status,
                    "summary": _card_summary(card)})
        if len(out) >= MAX_CARDS:
            break
    out.sort(key=lambda c: c["n"])
    return out


# ---------------------------------------------------------------- AI 扩展

def _expand_user_prompt(text: str, kind: str, count: int) -> str:
    task = ("围绕这段内容发散 3-5 个新的子想法（头脑风暴角度：新冲突/新人物/意外转折/"
            "世界观补充/情绪落点）。" if kind == "note"
            else "把这段内容拆成 3-5 个可执行的写作大纲点（按推进顺序，每个点一句话能落地成一段情节）。")
    return (
        f"{task}\n\n节点内容：\n{text[:MAX_EXPAND_TEXT]}\n\n"
        f"只输出 JSON（恰好 {count} 个条目）："
        '{"items":[{"title":"不超过16字的小标题","text":"40-80字的展开说明"}]}'
    )


def _parse_items(raw) -> list[dict]:
    if isinstance(raw, dict):
        obj = raw
    else:
        s = str(raw or "").strip()
        if s.startswith("```"):
            s = s.strip("`")
            s = s[s.find("{"):] if "{" in s else s
            s = s[:s.rfind("}") + 1] if "}" in s else s
        try:
            obj = json.loads(s)
        except ValueError:
            i, j = s.find("{"), s.rfind("}")
            if not (0 <= i < j):
                return []
            try:
                obj = json.loads(s[i:j + 1])
            except ValueError:
                return []
    items = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = re.sub(r"\s+", " ", str(it.get("title") or "")).strip()[:40]
        text = re.sub(r"\s+", " ", str(it.get("text") or it.get("body") or "")).strip()[:400]
        if not title and not text:
            continue
        out.append({"title": title or "子想法", "text": text})
    return out


def expand_items(text: str, kind: str, count: int, cfg: dict | None = None) -> list[dict]:
    """调 LLM 生成子想法；失败向上抛（api_expand 翻成中文 HTTP 错误）。"""
    raw = llm.chat(
        [{"role": "system", "content": _EXPAND_SYSTEM},
         {"role": "user", "content": _expand_user_prompt(text, kind, count)}],
        cfg=cfg, json_mode=True, temperature=0.6, max_tokens=1500,
        scene=_EXPAND_SCENE,
    )
    return _parse_items(raw)[:count]


# ---------------------------------------------------------------- 路由

@router.get("/api/flow/board")
def api_flow_board(key: str = ""):
    safe = board_id(key)
    board = read_board(safe)
    return {"ok": True, "key": key, "boardKey": safe,
            "nodes": board["nodes"], "edges": board["edges"],
            "updatedAt": board["updatedAt"]}


@router.post("/api/flow/board")
def api_flow_board_save(payload: dict):
    p = payload if isinstance(payload, dict) else {}
    safe = board_id(str(p.get("key") or ""))
    board = sanitize_board({"nodes": p.get("nodes"), "edges": p.get("edges")})
    saved = write_board(safe, board["nodes"], board["edges"])
    return {"ok": True, "boardKey": safe,
            "nodeCount": len(saved["nodes"]), "edgeCount": len(saved["edges"]),
            "updatedAt": saved["updated_at"]}


@router.post("/api/flow/expand")
def api_flow_expand(payload: dict):
    p = payload if isinstance(payload, dict) else {}
    text = str(p.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="扩展需要节点内容：先在便签里写点想法")
    kind = str(p.get("kind") or "note")
    if kind not in EXPAND_KINDS:
        kind = "note"
    try:
        count = int(p.get("count") or EXPAND_COUNT_DEFAULT)
    except (TypeError, ValueError):
        count = EXPAND_COUNT_DEFAULT
    count = max(EXPAND_COUNT_MIN, min(EXPAND_COUNT_MAX, count))
    cfg = {}
    if p.get("key"):
        try:
            _plan = writer._plan_of(_project_dir(str(p.get("key"))))[2]
            cfg = (_plan or {}).get("config") or {}
        except Exception:
            cfg = {}
    try:
        items = expand_items(text, kind, count, cfg=cfg)
    except HTTPException:
        raise
    except Exception as err:
        detail = str(err) or err.__class__.__name__
        raise HTTPException(status_code=502,
                            detail=f"AI 扩展失败：{detail}。请检查模型配置后重试") from err
    if not items:
        raise HTTPException(status_code=502,
                            detail="AI 没有返回可用的扩展内容，请稍后重试或换个写法")
    return {"ok": True, "kind": kind, "count": len(items), "items": items}


@router.get("/api/flow/chapters")
def api_flow_chapters(key: str = ""):
    pdir = _project_dir(key)
    chapters = load_chapters(pdir)
    return {"ok": True, "key": key, "chapters": chapters}
