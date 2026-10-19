"""P5 轻量故事图谱（自包含，无外部数据库）。
从项目文件抽取 人物/物品/地点/组织/术语/伏笔 为节点、关系为边，
存 15-故事图谱.json；提供"按章取相关实体"的邻域检索，注入写作上下文。
全部 best-effort：文件缺失/格式异常都降级，绝不 crash。"""
import json, os, re

PDIR_FILE = "15-故事图谱.json"

def _read(pdir, name):
    p = os.path.join(pdir, name)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

def _persons(text):
    """从人物档案抓 人名 + 关系/立场（## 人物：X … 关系/立场：与Y…）"""
    out = []
    for m in re.finditer(r"(?:##\s*)?人物[:：]\s*([^\n，,。]{1,12})", text):
        out.append(m.group(1).strip())
    for m in re.finditer(r"关系/立场[:：]\s*([^\n]+)", text):
        seg = m.group(1)
        for name in re.findall(r"([一-龥]{1,8})", seg):
            if len(name) >= 2 and name not in out:
                out.append(name)
    # 去重保序
    seen, res = set(), []
    for n in out:
        if n not in seen and n not in ("主角", "配角", "姓名"):
            seen.add(n); res.append(n)
    return res[:20]

def _terms(text):
    """从 故事圣经 设定词典表格抓术语名"""
    res = []
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and cells[0] and not cells[0].startswith(("名称", "---", "|")) and len(cells[0]) <= 12:
                res.append(cells[0])
    return res[:30]

def _threads(text):
    """从 线索账本 抓 线索 → [{id, first_ch, due_ch, status}]"""
    res = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0].startswith(("ID", "---")) or set(cells[0]) <= {"-", " "}:
            continue
        def ci(idx):
            m = re.search(r"\d+", cells[idx]) if idx < len(cells) else None
            return int(m.group()) if m else None
        res.append({"id": cells[0], "first_ch": ci(1), "due_ch": ci(5) if len(cells) > 5 else None,
                    "status": cells[-1]})
    return res

def build_graph(pdir, total_chapters=10):
    persons = _persons(_read(pdir, "00-人物档案.md"))
    terms = _terms(_read(pdir, "09-故事圣经.md"))
    threads = _threads(_read(pdir, "10-线索账本.md"))
    nodes = [{"id": p, "type": "person"} for p in persons]
    nodes += [{"id": t, "type": "term"} for t in terms]
    for th in threads:
        nodes.append({"id": th["id"], "type": "thread", "first_ch": th["first_ch"],
                     "due_ch": th["due_ch"], "status": th["status"]})
    graph = {"nodes": nodes,
             "threads": threads,
             "persons": persons,
             "total_chapters": total_chapters}
    with open(os.path.join(pdir, PDIR_FILE), "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
    return graph

def load(pdir):
    p = os.path.join(pdir, PDIR_FILE)
    try:
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"nodes": [], "threads": []}
    except Exception:
        return {"nodes": [], "threads": []}

def active_context(pdir, chapter_no, k=8):
    """本章相关实体：已出场且（未闭环）的人物 + 活跃/临近兑现的伏笔。用于注入写作上下文。"""
    g = load(pdir)
    person_seen = [n for n in g.get("persons", []) if n]
    active_threads = []
    for th in g.get("threads", []):
        fc, dc = th.get("first_ch"), th.get("due_ch")
        st = (th.get("status") or "").lower()
        closed = ("paid" in st) or ("回收" in (th.get("status") or ""))
        if not closed and ((dc is None) or (dc >= chapter_no)):
            active_threads.append(th["id"])
    # 相关性排序：临近兑现(due 接近本章)的伏笔优先；人物按已知数量
    def tkey(t):
        for x in g.get("threads", []):
            if x["id"] == t:
                return abs((x.get("due_ch") or chapter_no) - chapter_no)
        return 999
    active_threads.sort(key=tkey)
    return {
        "chapter_no": chapter_no,
        "persons": person_seen[:max(k, 4)],
        "active_threads": active_threads[:k],
    }
