"""资料画布 HTTP 接口（v5.5.2，docs/易创对齐清单 第二节 A2）。

用 FastAPI APIRouter 收口，**不改 app.py**——主会话收尾只需在 app.py 里加两行挂载：

    from bot import webcanvas
    app.include_router(webcanvas.router)

接口契约（与 static/v2/js/views/canvas.js 对齐）：
  GET  /api/canvas/graph?key=<bookKey>   聚合「角色关系图 + 故事线思维导图」一次返回
  POST /api/canvas/edge                  {key, a, b, label} 写回关系标签覆盖层
                                         （label 传空串 = 删除该覆盖，恢复自动标签）

聚合策略（全部轻量规则提取，**绝不调 LLM**）：
1. 人物节点：以 00-人物档案.md 的 `## 人物：X` 标题为准（去粗体标记与括号别名）；
   15-故事图谱.json 里 type=person 的节点只做补充——只收「确实出现在档案正文里」
   的名字（bot/graph.py 的 关系/立场 行正则会把立场短语误抓成"人名"，如「立场为」，
   必须按正文过滤掉）。weight = 名字在 档案/账本/章节摘要 里的出现次数
   （图谱节点自带 weight 字段则直接采用）。
2. 关系边：逐人物取「关系/立场」行——
   · 行内提到其他人物名 → 直连边；
   · 行内含 主角/反派/配角 角色词 → 连到主角（关系行含「主角」的人物，缺省第一位）；
   · 标签按关键词优先级（先命中先用）：
     亲人（父母兄弟姐妹儿女夫妻家人）→ 暧昧（暧昧/恋/爱/情感/女主/男主）→
     师徒（师父/师傅/导师/徒弟/前辈）→ 上下级（上级/下属/上司/部下/领导/老板/总裁/总监/经理/主仆）→
     对手（反派/阻/敌/对手/仇/竞争/对抗/背叛/眼中钉/抹杀/霸凌/清除）→
     利用 → 盟友（盟友/同盟/伙伴/帮/协助/合作/战友/朋友/挚友/同伴）→ 缺省「关联」。
   15-故事图谱.json 目前没有边数据（bot/graph.py 只出节点），所以边完全由规则提取 +
   覆盖层合并。
3. 故事线思维导图（主线/支线/人物线/伏笔四组）：
   · root = 书名（02-写作计划.json novelName → 目录名）；
   · 主线 = 线索账本「登记范围」引号里的主线短语（服务于"X"主线），缺省 plan 主题；
   · 伏笔 = 线索账本登记表行（|ID|线索|…），解析失败时用 15-故事图谱.json 的
     type=thread 节点兜底；
   · 支线 = 账本「第N章后新增/强化线索」的 `- 线索：X` 条目；
   · 人物线 = 人物档案里的角色；
   · seq（前后置）= 伏笔组按（首章, ID）排序后相邻串联。
4. 关系标签覆盖层：写 data/canvas-edges/<resolvedKey>.json（**不写回 15-故事图谱.json**——
   bot/writer.py 每章都会重跑 graph.build_graph 整体重建该文件，写进去会被下一次
   写作直接冲掉）。聚合时覆盖层标签按「无向对 (a,b) 排序键」合并到自动边上；
   对端点都存在但自动提取没发现的覆盖对，也会作为新边补进图（用户手拉的关系不丢）。

全部 best-effort：文件缺失/格式异常都降级，绝不 crash；key 用 writer._project_dir
的别名解析（改名后的旧 key 也能读）。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime

from fastapi import APIRouter, HTTPException

from . import graph as graph_mod
from . import paths
from . import writer
from .safeio import atomic_write_json

router = APIRouter()

_EDGE_LOCK = threading.Lock()

DEFAULT_LABEL = "关联"
MAX_LABEL = 12
MAX_NODES = 40
MAX_EDGE_LABEL = 30

# ---------------------------------------------------------------- 通用小工具

_BOLD_RE = re.compile(r"\*+")
_CH = r"一-龥"


def _read_file(pdir: str, name: str) -> str:
    try:
        p = os.path.join(pdir, name)
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    except OSError:
        return ""


def _clean(s: str) -> str:
    """去 markdown 粗体/空白。"""
    return _BOLD_RE.sub("", str(s or "")).strip()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _project(key: str) -> tuple[str, str]:
    """key → (真实目录, resolvedKey)。目录不存在直接 404。"""
    key = str(key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="缺少作品 key")
    try:
        pdir = writer._project_dir(key)
    except Exception:
        pdir = None
    if not pdir or not os.path.isdir(pdir):
        raise HTTPException(status_code=404, detail="作品不存在或已被删除")
    return pdir, os.path.basename(os.path.realpath(pdir))


# ---------------------------------------------------------------- 人物提取

_PERSON_HEAD_RE = re.compile(r"^##\s*人物[:：]\s*(.{1,20})$", re.M)


def parse_persons(text: str) -> list[dict]:
    """00-人物档案.md → [{name, section}]。只认 `## 人物：X` 标题，去粗体与括号别名。"""
    out: list[dict] = []
    seen: set[str] = set()
    for m in _PERSON_HEAD_RE.finditer(text or ""):
        raw = _clean(m.group(1))
        # 「老张（张启铭）」→ 主名 老张；括号里的别名不单开节点
        name = re.split(r"[（(]", raw)[0].strip(" `\t")
        name = name[:12]
        if not name or name in seen or name in ("主角", "配角", "姓名"):
            continue
        seen.add(name)
        out.append({"name": name, "section": m.group(0)})
    return out[:MAX_NODES]


def person_weight(name: str, texts: list[str], graph_weight=None) -> int:
    """出场权重：图谱自带 weight 优先；否则数名字在各账本文件里的出现次数（下限 1）。"""
    if isinstance(graph_weight, (int, float)) and graph_weight > 0:
        return int(graph_weight)
    n = 0
    for t in texts:
        if not t:
            continue
        n += t.count(name)
    return max(1, n)


# ---------------------------------------------------------------- 关系边提取

# 标签关键词优先级：先命中先用。顺序即文档注释里的规则。
# 注意：亲人词全用双字组合（兄弟/姐妹/儿子…），避免单字「姐/妹/女」这类子串误伤
# （「雅姐」里的「姐」≠ 亲人关系）。
_LABEL_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("亲人", ("父亲", "母亲", "爸爸", "妈妈", "老爸", "老妈", "爷爷", "奶奶", "外公",
              "外婆", "兄弟", "姐妹", "兄妹", "姐弟", "哥哥", "姐姐", "妹妹", "弟弟",
              "兄长", "儿子", "女儿", "父子", "母女", "丈夫", "妻子", "夫妻",
              "家人", "家眷", "亲人", "血缘", "同胞")),
    ("暧昧", ("暧昧", "恋人", "爱慕", "暗恋", "爱情", "情感", "女主", "男主", "心动",
              "心上人", "情侣")),
    ("师徒", ("师父", "师傅", "导师", "徒弟", "师徒", "前辈", "传人", "授业")),
    ("上下级", ("上级", "下属", "上司", "部下", "领导", "老板", "总裁", "总监", "经理",
               "主仆", "效忠", "麾下", "指挥")),
    ("对手", ("反派", "对手", "敌人", "敌对", "仇", "竞争", "对抗", "背叛", "眼中钉",
             "抹杀", "霸凌", "阻挠", "阻", "围剿", "碾压", "征服", "清算")),
    ("利用", ("利用",)),
    ("盟友", ("盟友", "同盟", "伙伴", "协助", "合作", "战友", "朋友", "挚友", "同伴",
             "帮手", "并肩", "相助")),
]


def relation_label(line: str) -> str:
    """关系行 → 标签。全部子串命中（「主要反派」含「反派」也算）。"""
    low = str(line or "")
    for label, words in _LABEL_RULES:
        for w in words:
            if w in low:
                return label
    return DEFAULT_LABEL


_ROLE_WORDS = ("主角", "反派", "配角", "次要角色", "路人")


def relation_edges(persons: list[dict], archive_text: str) -> list[dict]:
    """从每个人物的「关系/立场」行抽边。

    返回 [{from, to, label, kind}]，kind 固定 'relation'（自动提取）；
    direction 规则：被提及者/主角 as 'from' 的反面——统一存 (小, 大) 无向对在
    聚合层做，这里保持「提及者→被提及者」的自然方向即可。
    """
    if not persons:
        return []
    names = [p["name"] for p in persons]

    # 主角 = 关系行含「主角」的人物；缺省第一位。
    protagonist = None
    sections: dict[str, str] = {}
    for p in persons:
        seg = _person_section_text(archive_text, p["name"])
        sections[p["name"]] = seg
        rel = _relation_line(seg)
        if rel and "主角" in rel and not protagonist:
            protagonist = p["name"]
    if not protagonist:
        protagonist = names[0]

    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(a: str, b: str, label: str):
        if a == b or not a or not b:
            return
        key = tuple(sorted((a, b)))
        if key in seen:
            return
        seen.add(key)
        edges.append({"from": a, "to": b, "label": label or DEFAULT_LABEL,
                      "kind": "relation"})

    for p in persons:
        rel = _relation_line(sections.get(p["name"]) or "")
        if not rel:
            continue
        label = relation_label(rel)
        # 1) 行内直接点名其他人物 → 直连边
        mentioned = False
        for other in names:
            if other != p["name"] and other in rel:
                add(p["name"], other, label)
                mentioned = True
        # 2) 角色词（主角/反派/配角）→ 连主角（自己就是主角则跳过）
        if not mentioned and any(w in rel for w in _ROLE_WORDS):
            if p["name"] != protagonist:
                add(p["name"], protagonist, label)
    return edges


def _person_section_text(text: str, name: str) -> str:
    """取人物 X 的档案段落（从 `## 人物：X` 到下一个 `## 人物`）。"""
    if not text:
        return ""
    marks = list(_PERSON_HEAD_RE.finditer(text))
    for i, m in enumerate(marks):
        raw = _clean(m.group(1))
        head = re.split(r"[（(]", raw)[0].strip(" `\t")
        if head == name:
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            return text[m.start():end]
    return ""


# graph.py 误抓碎片兜底用的黑名单：立场/角色/标签词都不是人名。
_NAME_STOP = ({w for _, words in _LABEL_RULES for w in words}
              | {"主角", "配角", "反派", "次要角色", "路人", "姓名", "立场", "立场为",
                 "情感", "情感线", "女主", "男主", "导师", "摇摆", "阻挠", "关系"})


def _looks_like_name(nid: str) -> bool:
    """兜底补充的"人名"要像个名字：2–6 个汉字、不含标点、不在立场/标签词黑名单里。"""
    nid = _clean(nid)
    if not 2 <= len(nid) <= 6 or nid in _NAME_STOP:
        return False
    return bool(re.fullmatch(rf"[{_CH}0-9]+", nid))


def _relation_line(section: str) -> str:
    """段落里的 关系/立场 行（兼容 `- **关系/立场**：` 与 `- 关系/立场：`）。"""
    m = re.search(r"关系/立场[^:：\n]*[:：]\s*([^\n]+)", section or "")
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------- 线索账本 → 故事线

_LEDGER_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_CH_NUM = re.compile(r"[第\s]*(\d{1,3})\s*章")
_BULLET_CLUE = re.compile(r"^\s*[-*]\s*线索[:：]\s*(.+)$")
_SCOPE_RE = re.compile(r"[「\"『]([^「」\"『』]{2,24})[」\"』]([^。\n]{0,12}主线)")


def parse_ledger(text: str) -> dict:
    """10-线索账本.md → {main, table:[{id,text,first_ch,status}], bullets:[{id,text,ch}]}。

    · 表格行：| ID | 线索 | … | 状态 | —— 首章取全行第一个「第N章」，兑现章取最后一个；
    · 条目行：`- 线索：X（已知：…）` → 支线组；
    · 主线短语：登记范围引号 "X" 后接「主线」。
    """
    main = ""
    table: list[dict] = []
    bullets: list[dict] = []
    bullet_no = 0
    for line in (text or "").splitlines():
        if not main:
            sm = _SCOPE_RE.search(line)
            if sm:
                main = sm.group(1).strip()[:24]
        if line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2 or cells[0].startswith(("ID", ":", "---")):
                continue
            if set(cells[0]) <= {"-", " ", ":"}:
                continue
            nums = [int(n) for n in _CH_NUM.findall(" ".join(cells[1:]))]
            table.append({
                "id": _clean(cells[0])[:20] or f"L{len(table) + 1}",
                "text": _clean(cells[1])[:40] or _clean(cells[0])[:40],
                "first_ch": nums[0] if nums else None,
                "due_ch": nums[-1] if len(nums) > 1 else None,
                "status": _clean(cells[-1])[:12] or "planned",
            })
            continue
        bm = _BULLET_CLUE.match(line)
        if bm:
            raw = bm.group(1)
            # 「X（已知：…，真相：…）」→ 取括号前主名
            name = re.split(r"[（(]", raw)[0].strip()
            name = _clean(name)[:24]
            if not name:
                continue
            bullet_no += 1
            chs = _CH_NUM.findall(raw)
            bullets.append({
                "id": f"X{bullet_no}",
                "text": name,
                "ch": int(chs[-1]) if chs else None,
            })
    return {"main": main, "table": table[:24], "bullets": bullets[:24]}


def _plan_title(pdir: str) -> str:
    plan = None
    try:
        plan = json.load(open(os.path.join(pdir, "02-写作计划.json"), encoding="utf-8"))
    except Exception:
        plan = None
    if isinstance(plan, dict):
        t = _clean(str(plan.get("novelName") or ""))
        if t:
            return t
    return ""


def _graph_thread_fallback(pdir: str) -> list[dict]:
    """15-故事图谱.json 的 type=thread 节点兜底（账本解析为空时用）。"""
    g = graph_mod.load(pdir)
    out = []
    for n in g.get("nodes") or []:
        if isinstance(n, dict) and n.get("type") == "thread":
            nid = _clean(n.get("id"))
            if not nid:
                continue
            out.append({"id": nid[:20], "text": nid[:40],
                        "first_ch": n.get("first_ch"), "due_ch": n.get("due_ch"),
                        "status": _clean(n.get("status"))[:12] or "planned"})
    return out[:24]


def build_story(pdir: str, persons: list[dict]) -> dict:
    """故事线思维导图树：root → 主线/人物线/支线/伏笔 四组 + seq 前后置。"""
    title = _plan_title(pdir)
    if not title:
        # 目录名形如 20261018-161605-全链路P5：…；剥掉日期时间前缀当可读书名
        base = os.path.basename(pdir)
        m = re.match(r"^[a-z]?\d{8}-?\d{4,6}-(.+)$", base)
        title = m.group(1) if m else base
    ledger = parse_ledger(_read_file(pdir, "10-线索账本.md"))
    main = ledger["main"]
    if not main:
        main = (title or "主线")[:24]

    foreshadow = ledger["table"]
    if not foreshadow:
        foreshadow = _graph_thread_fallback(pdir)

    root_children: list[dict] = []

    root_children.append({
        "id": "grp-main", "label": "主线", "kind": "group",
        "children": [{"id": "main-0", "label": main or "主线", "kind": "main"}],
    })

    person_items = [{"id": f"p-{p['name']}", "label": p["name"], "kind": "person"}
                    for p in persons]
    root_children.append({
        "id": "grp-person", "label": "人物线", "kind": "group",
        "children": person_items,
    })

    branch_items = [{"id": f"x-{b['id']}", "label": b["text"], "kind": "branch",
                     "ch": b["ch"]} for b in ledger["bullets"]]
    root_children.append({
        "id": "grp-branch", "label": "支线", "kind": "group",
        "children": branch_items,
    })

    fore_items = [{"id": f"f-{t['id']}", "label": t["text"], "kind": "foreshadow",
                   "ch": t["first_ch"], "status": t["status"]} for t in foreshadow]
    root_children.append({
        "id": "grp-foreshadow", "label": "伏笔", "kind": "group",
        "children": fore_items,
    })

    # 前后置：伏笔按（首章, ID）排序后相邻串联（无首章的排最后，不参与串联）
    ordered = sorted([t for t in foreshadow if t.get("first_ch")],
                     key=lambda t: (t["first_ch"], t["id"]))
    seq = [[f"f-{ordered[i]['id']}", f"f-{ordered[i + 1]['id']}"]
           for i in range(len(ordered) - 1)]

    return {"root": {"id": "root", "label": (title or "未命名")[:30], "kind": "root",
                     "children": root_children},
            "seq": seq}


# ---------------------------------------------------------------- 聚合 + 覆盖层

def overlay_path(resolved_key: str) -> str:
    """关系标签覆盖层路径：data/canvas-edges/<resolvedKey>.json。

    走 paths.data（调用时解析而非 import 时绑定）：测试可按用例重定向数据根。"""
    safe = resolved_key if re.match(r"^[\w][\w\-.：]{0,120}$", resolved_key) else ""
    if not safe:
        raise HTTPException(status_code=400, detail="作品 key 非法")
    return os.path.join(paths.data("canvas-edges"), f"{safe}.json")


def _pair_key(a: str, b: str) -> str:
    return "\u001f".join(sorted((str(a or ""), str(b or ""))))


def read_overlay(resolved_key: str) -> dict:
    try:
        obj = json.load(open(overlay_path(resolved_key), encoding="utf-8"))
    except Exception:
        return {"edges": {}}
    if not isinstance(obj, dict) or not isinstance(obj.get("edges"), dict):
        return {"edges": {}}
    return obj


def write_edge_label(resolved_key: str, a: str, b: str, label: str) -> dict:
    """写覆盖层（锁 + 原子写）。label 空 = 删除覆盖，恢复自动标签。"""
    a, b = str(a or "").strip(), str(b or "").strip()
    label = str(label or "").strip()[:MAX_LABEL]
    if not a or not b or a == b:
        raise HTTPException(status_code=400, detail="边的两端节点缺失或相同")
    with _EDGE_LOCK:
        obj = read_overlay(resolved_key)
        edges = obj.get("edges") or {}
        pk = _pair_key(a, b)
        if label:
            edges[pk] = {"label": label[:MAX_EDGE_LABEL], "updated_at": _now()}
        else:
            edges.pop(pk, None)
        obj["edges"] = edges
        obj["version"] = 1
        obj["updated_at"] = _now()
        try:
            atomic_write_json(overlay_path(resolved_key), obj)
        except Exception as err:
            raise HTTPException(status_code=500, detail=f"保存关系标签失败：{err}") from err
    return {"ok": True, "pair": pk, "label": label}


def aggregate(pdir: str) -> dict:
    """目录 → 画布聚合 payload（人物 + 关系边 + 故事线）。"""
    archive = _read_file(pdir, "00-人物档案.md")
    g = graph_mod.load(pdir)

    persons = parse_persons(archive)
    # 图谱 person 节点只在「档案没有 ## 人物 标题可解析」时兜底补充——bot/graph.py
    # 的 关系/立场 正则会把立场短语误抓成"人名"（「立场为」「治逻辑」…），
    # 主解析可用时并入只会污染画布，所以兜底也要过黑名单 + 频次两道滤网。
    if not persons:
        seen: set[str] = set()
        for n in (g.get("nodes") or []):
            if not isinstance(n, dict) or n.get("type") != "person":
                continue
            nid = _clean(n.get("id"))
            if not nid or nid in seen or not _looks_like_name(nid):
                continue
            if archive.count(nid) < 2:
                continue
            persons.append({"name": nid[:12], "section": ""})
            seen.add(nid)
            if len(persons) >= MAX_NODES:
                break

    weight_texts = [archive,
                    _read_file(pdir, "10-线索账本.md"),
                    _read_file(pdir, "11-章节摘要.md")]
    gnode_weight = {}
    for n in (g.get("nodes") or []):
        if isinstance(n, dict) and n.get("id"):
            gnode_weight[_clean(str(n["id"]))] = n.get("weight")

    nodes = []
    for p in persons:
        nodes.append({
            "id": p["name"],
            "label": p["name"],
            "kind": "person",
            "weight": person_weight(p["name"], weight_texts,
                                    gnode_weight.get(p["name"])),
        })

    edges = relation_edges(persons, archive)

    story = build_story(pdir, persons)

    return {
        "characters": {"nodes": nodes, "edges": edges},
        "story": story,
        "source": {
            "graphFile": bool(g.get("nodes")),
            "personCount": len(nodes),
            "edgeCount": len(edges),
        },
    }


def apply_overlay(payload: dict, resolved_key: str) -> dict:
    """把覆盖层标签合并进聚合结果（覆盖命中边 + 补自动提取没发现的手拉边）。"""
    ov = read_overlay(resolved_key).get("edges") or {}
    if not ov:
        return payload
    chars = payload.get("characters") or {}
    nodes = chars.get("nodes") or []
    names = {n["id"] for n in nodes}
    by_pair = {}
    for e in chars.get("edges") or []:
        by_pair[_pair_key(e["from"], e["to"])] = e
    for pk, rec in ov.items():
        if not isinstance(rec, dict):
            continue
        label = str(rec.get("label") or "").strip()
        a, b = (pk.split("\u001f") + ["", ""])[:2]
        if not a or not b:
            continue
        e = by_pair.get(pk)
        if e is not None:
            e["label"] = label or DEFAULT_LABEL
            e["edited"] = True
        elif a in names and b in names and label:
            ne = {"from": a, "to": b, "label": label, "kind": "manual", "edited": True}
            chars["edges"].append(ne)
            by_pair[pk] = ne
    return payload


# ---------------------------------------------------------------- 路由

@router.get("/api/canvas/graph")
def api_canvas_graph(key: str = ""):
    pdir, resolved = _project(key)
    try:
        payload = aggregate(pdir)
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"聚合画布数据失败：{err}") from err
    payload = apply_overlay(payload, resolved)
    plan_title = _plan_title(pdir)
    return {"ok": True, "key": key, "resolvedKey": resolved,
            "title": plan_title or os.path.basename(pdir), **payload}


@router.post("/api/canvas/edge")
def api_canvas_edge(payload: dict):
    p = payload if isinstance(payload, dict) else {}
    key = str(p.get("key") or "").strip()
    _pdir, resolved = _project(key)
    out = write_edge_label(resolved, str(p.get("a") or ""), str(p.get("b") or ""),
                           str(p.get("label") or ""))
    out["key"] = key
    out["resolvedKey"] = resolved
    return out
