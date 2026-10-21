"""确定性连续性引擎（L1，零 LLM 成本、可复现）。
对作品目录跑规则检查，返回结构化 findings，喂给质量报告与 Guardian 角色。
设计原则：每一项失败都降级为 "skipped" 提示，绝不 crash（旧项目缺文件照样跑）。"""
import json, os, re

SEV_RANK = {"info": 0, "warning": 1, "critical": 2}

def _read(pdir, name):
    p = os.path.join(pdir, name)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

def _plan(pdir):
    p = os.path.join(pdir, "02-写作计划.json")
    try:
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
    except Exception:
        return {}

def _cn(text):
    return len(re.findall(r"[\u4e00-\u9fff]", text))

def _chapter_files(pdir, plan):
    """返回 [(no, title, text, path)]；缺 02 计划时按 第NN章*.md 扫描。"""
    chs = []
    for c in plan.get("chapters", []):
        no = c.get("chapterNumber")
        fp = os.path.join(pdir, c.get("filePath", ""))
        txt = _read(pdir, c.get("filePath", "")) if c.get("filePath") else ""
        chs.append((no, c.get("title", ""), txt, fp))
    if not chs:
        for f in sorted(os.listdir(pdir)):
            m = re.match(r"^第(\d+)章.*\.md$", f)
            if m and "续写" not in f:
                chs.append((int(m.group(1)), f, _read(pdir, f), os.path.join(pdir, f)))
    return [c for c in chs if c[0] is not None]

def _parse_clues(text):
    """从 10-线索账本 的表格里抽线索行 → [dict(id, first, due, status)]。解析失败返回 []。"""
    rows = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0].startswith(("ID", "---", "线")):
            continue
        if set(cells[0]) <= {"-", " "}:
            continue
        rows.append({"id": cells[0], "first": cells[1] if len(cells) > 1 else "",
                     "due": cells[5] if len(cells) > 5 else "", "status": cells[-1]})
    return rows

def _to_int(s, default=None):
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else default

def run_project(pdir, min_words=3000, dormant=3, absent=5):
    """跑 L1 全部检查。返回 {'findings': [ {check,severity,location,detail} ], 'stats': {...} }。"""
    findings = []
    def add(check, severity, location, detail):
        findings.append({"check": check, "severity": severity, "location": location, "detail": detail})

    plan = _plan(pdir)
    min_words = plan.get("minWordsPerChapter", min_words)
    chs = _chapter_files(pdir, plan)
    total_done = sum(1 for c in chs if _cn(c[2]) > 0)

    # 1) 字数（权威：按文件重数）
    for no, _title, txt, _ in chs:
        if not _cn(txt):
            continue
        w = _cn(txt)
        if w < min_words:
            add("字数未达标", "warning", f"第{no}章", f"{w} 字 < {min_words}")
        if no >= 4 and w < 600:
            add("疑似空转章", "warning", f"第{no}章", f"仅 {w} 字，可能空转")

    # 2) 黄金三章门（粗规则；细则由 LLM 审校复核）
    g3 = [c for c in chs if c[0] and c[0] <= 3]
    if g3:
        c1 = next((c for c in g3 if c[0] == 1), None)
        if c1 and _cn(c1[2]) < 800:
            add("黄金三章·开篇偏薄", "warning", "第1章", "正文 <800 字，钩子/主角/首爽点可能没立住")
        if len([c for c in g3 if _cn(c[2]) > 0]) < 3:
            add("黄金三章·缺章", "critical", "1-3章", "前 3 章有未写完/缺失")

    # 3) 伏笔生命周期（best-effort 解析线索账本）
    clues = _parse_clues(_read(pdir, "10-线索账本.md"))
    last_no = max((c[0] for c in chs), default=0)
    if not clues:
        add("伏笔账本·未解析", "info", "10-线索账本", "未登记可解析线索（或无）；请人工确认主线谜团有登记")
    for cl in clues:
        st = cl["status"].lower()
        due = _to_int(cl["due"], None)
        if "paid" in st or "回收" in cl["status"]:
            continue
        if due is not None and last_no >= due and "open" in st:
            add("伏笔超期未兑现", "critical", f"L {cl['id']}", f"计划第{due}章兑现，已写到第{last_no}章仍未回收")
        elif due is not None and last_no - _to_int(cl["first"], 0) >= dormant * 2 and "planned" in st:
            add("伏笔闲置", "warning", f"L {cl['id']}", "长期 planned 未推进")

    # 4) 主角缺席（best-effort：人物档案里标注"主角"的姓名）
    names = re.findall(r"主角[:：]?\s*\**([^*\n，,。;；]{1,10})", _read(pdir, "00-人物档案.md"))
    names = [n.strip() for n in names if n.strip()]
    if not names:
        # 兜底：取人物档案第一个 "## 人物：X"
        m0 = re.search(r"##\s*人物[:：]\s*([^\n]+)", _read(pdir, "00-人物档案.md"))
        if m0:
            names = [m0.group(1).strip()]
    for nm in names[:2]:
        seq = []
        for no, _, txt, _ in chs:
            if _cn(txt) and nm in txt:
                seq = []
            else:
                seq.append(no)
        if seq and max(seq) >= absent:
            add("主角/关键人物缺席", "warning", f"连续{max(seq)}章", f"「{nm}」自第{min(seq)}章起未出现")

    # 5) 爽点/钩子 软信号（非判词，供参考）
    for no, _title, txt, _ in chs:
        if no is None or not txt or no == 1:
            continue
        tail = txt[-200:]
        hook = any(k in tail for k in ("？", "竟", "却", "反转", "打脸", "升级", "原来", "背后"))
        if not hook:
            add("章末钩子缺失(信号)", "info", f"第{no}章", "章末未检测到典型钩子词（信号，非判词）")

    return {"findings": findings, "stats": {"chapters_done": total_done,
                                             "clues_registered": len(clues)}}
