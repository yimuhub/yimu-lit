"""剧本实体抽取（v4.33.5）：从已写正文里抽人物 / 地点 / 场次结构，零 LLM 成本。

**为什么要有这个模块**

小说改编成的短剧，在 `剧本计划.json` 里「一集」就是 1 个 scene，`place` 只能
填占位的「第 N 集」，`cast` 干脆没有。于是：
  · 右栏「状态」的地点列表成了 50 条「第1集…第50集」；
  · 人物表 0 条，人物 / 结构两个页签永远空白；
  · 一致性体检没有可比对的底稿。

但正文里每一场其实都写着 `场1：外 部落广场 白天` 和 `林野：台词`。把这些抽出来
就是真实的一致性底稿——不需要再调模型，也不会和正文不符。

**设计约束**（与 bot/continuity.py、bot/script_state.py 一致）
  · 每一项解析失败都降级为跳过，绝不 crash；
  · 只读正文，不改正文；`sync_plan` 是唯一的写入口，且默认不调用；
  · 结果按「正文目录签名」缓存，右栏每 5 秒轮询不会反复读 1MB 文件。
"""
import os
import re

# 场标题：`场1：外 部落广场 白天` / `第3场：内-木屋-夜` / `**场 12: 内外 广场**`
# 两种写法都要认：编号在前（第3场）和场字在前（场3）。
RE_SCENE = re.compile(
    r"^\s*[*#>\-\s]*\s*(?:第\s*(\d+)\s*场|场\s*(\d+))\s*[*]*\s*[：:]\s*[*]*\s*"
    r"(内\s*/\s*外|内外|内景|外景|内|外)?\s*[*]*\s*(.*?)\s*[*]*\s*$"
)
# 对白：`林野：台词` / `苏清柔: 台词`（冒号后必须有内容）
RE_LINE = re.compile(r"^\s*[*>\-\s]*([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9·]{0,5})\s*[*]*\s*[：:]\s*\S")
# 集标题：`# 第1集：弃子觉醒`
RE_EP = re.compile(r"^\s*#+\s*第\s*(\d+)\s*集\s*[：:·\-—\s]*(.*)$", re.M)

TIME_WORDS = {
    "白天", "夜晚", "夜", "日", "晨", "清晨", "清晨时分", "黄昏", "傍晚", "深夜",
    "正午", "午后", "凌晨", "黎明", "上午", "下午", "晚上", "时刻", "次日", "当夜",
    "同日", "傍晚时分", "深夜时分", "夜半", "晌午", "未知时间", "不详", "稍后",
    "同时", "入夜", "天亮", "天亮前", "日落", "日出",
}
# 只用一个字表示时间/光线的写法（`场3：内 通道 暗`）
TIME_CHARS = {"夜", "日", "晨", "昏", "暗", "昼"}
# 不是角色的「说话人」：旁白/画外音以外都是噪音
NON_CHARACTER = {
    "旁白", "画外音", "字幕", "标题", "本集", "上集", "下集", "片尾", "片头",
    "内心", "独白", "声音", "众人", "全场", "所有人",
}
# 每集只保留多少个地点写进计划摘要
_PLAN_PLACES = 3
_PLAN_CAST = 6

_CACHE: dict[str, tuple] = {}


def _read(path: str) -> str:
    try:
        return open(path, encoding="utf-8").read()
    except Exception:
        return ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").replace("*", " ")).strip(" ·、，,：:")


def _split_place_time(rest: str) -> tuple[str, str]:
    """`部落广场 白天` → ('部落广场', '白天')；认不出时间就把整段当地点。"""
    rest = _clean(rest)
    if not rest:
        return "", ""
    toks = [t for t in re.split(r"[\s·｜|，,]+", rest) if t]
    if not toks:
        return "", ""
    if len(toks) >= 2 and (toks[-1] in TIME_WORDS or (len(toks[-1]) == 1 and toks[-1] in TIME_CHARS)):
        return _clean(" ".join(toks[:-1])), toks[-1]
    # `部落广场-夜` 这种用连字符分隔的写法
    parts = [p for p in re.split(r"[-—–]+", rest) if p.strip()]
    if len(parts) >= 2 and _clean(parts[-1]) in TIME_WORDS:
        return _clean(" ".join(parts[:-1])), _clean(parts[-1])
    return rest, ""


def parse_body(text: str) -> list[dict]:
    """把一集正文切成场：返回 [{no, io, place, time, cast[], lines}]。

    `no` 是**集内**场号（每集都从 1 开始），跨集不唯一，调用方需要补集号。
    """
    scenes: list[dict] = []
    cur: dict | None = None
    last_speaker = ""
    for raw in str(text or "").splitlines():
        m = RE_SCENE.match(raw)
        if m:
            io = _clean(m.group(3) or "")
            place, tm = _split_place_time(m.group(4) or "")
            cur = {"no": int(m.group(1) or m.group(2)), "io": io, "place": place,
                   "time": tm, "cast": [], "lines": 0}
            scenes.append(cur)
            last_speaker = ""
            continue
        if cur is None:
            continue
        lm = RE_LINE.match(raw)
        if lm:
            nm = lm.group(1).strip()
            if nm and nm not in NON_CHARACTER and not nm.startswith(("他", "她", "它")):
                cur["lines"] += 1
                last_speaker = nm
                if nm not in cur["cast"]:
                    cur["cast"].append(nm)
            continue
        # 动作行里出现已出场的说话人，也算本场在场（不打字幕的独角戏很常见）
        if last_speaker and last_speaker in raw and last_speaker not in cur["cast"]:
            cur["cast"].append(last_speaker)
    return scenes


def _body_signature(body_dir: str) -> tuple:
    try:
        names = [n for n in os.listdir(body_dir) if n.endswith(".md")]
    except Exception:
        return (0, 0, 0)
    total = 0
    latest = 0.0
    for n in names:
        try:
            st = os.stat(os.path.join(body_dir, n))
        except Exception:
            continue
        total += st.st_size
        latest = max(latest, st.st_mtime)
    return (len(names), int(total), int(latest))


def _plan(pdir: str) -> dict:
    try:
        import json
        return json.load(open(os.path.join(pdir, "剧本计划.json"), encoding="utf-8")) or {}
    except Exception:
        return {}


def extract(pdir: str, plan: dict | None = None) -> dict:
    """抽取整部剧的实体。返回 {episodes, scenes, characters, locations, summary}。

    集号以计划为准（scenes[].no ↔ fileName），计划缺失时退回文件名里的数字。
    """
    body_dir = os.path.join(pdir, "04-正文")
    sig = _body_signature(body_dir)
    cached = _CACHE.get(pdir)
    if cached and cached[0] == sig:
        return cached[1]

    plan = plan if plan is not None else _plan(pdir)
    by_file: dict[str, dict] = {}
    for sc in plan.get("scenes") or []:
        fn = os.path.basename(str(sc.get("fileName") or "").strip())
        if fn:
            by_file[fn] = sc

    episodes: dict[str, dict] = {}
    flat: list[dict] = []
    chars: dict[str, dict] = {}
    locs: dict[str, dict] = {}

    try:
        names = sorted(n for n in os.listdir(body_dir) if n.endswith(".md"))
    except Exception:
        names = []

    for fn in names:
        text = _read(os.path.join(body_dir, fn))
        if not text.strip():
            continue
        sc_plan = by_file.get(fn) or {}
        ep = sc_plan.get("no")
        if not isinstance(ep, int):
            m = re.search(r"(\d+)", fn)
            ep = int(m.group(1)) if m else 0
        title = ""
        m = RE_EP.search(text)
        if m:
            title = _clean(m.group(2))
        scenes = parse_body(text)
        cue = {"no": ep, "file": fn,
               "title": title or sc_plan.get("title") or "",
               "scenes": scenes,
               "words": sc_plan.get("wordCount") or len(text),
               "status": sc_plan.get("status") or ""}
        episodes[str(ep)] = cue
        for i, s in enumerate(scenes, 1):
            row = dict(s)
            row["episode"] = ep
            row["local"] = i
            row["file"] = fn
            flat.append(row)
            for nm in s["cast"]:
                c = chars.setdefault(nm, {"episodes": [], "lines": 0, "first": ep,
                                          "scenes": 0})
                c["lines"] += 1
                c["scenes"] += 1
                if ep not in c["episodes"]:
                    c["episodes"].append(ep)
                if ep < (c["first"] or ep):
                    c["first"] = ep
            if s["place"]:
                loc = locs.setdefault(s["place"], {"episodes": [], "count": 0,
                                                   "io": s["io"], "times": []})
                loc["count"] += 1
                if ep not in loc["episodes"]:
                    loc["episodes"].append(ep)
                if s["time"] and s["time"] not in loc["times"]:
                    loc["times"].append(s["time"])

    for c in chars.values():
        c["episodes"].sort()
    for loc in locs.values():
        loc["episodes"].sort()

    data = {
        "episodes": episodes, "scenes": flat,
        "characters": chars, "locations": locs,
        "summary": {
            "episodes": len(episodes), "scenes": len(flat),
            "characters": len(chars), "locations": len(locs),
            "no_scene_heading": [ep for ep, e in episodes.items() if not e["scenes"]],
        },
    }
    _CACHE[pdir] = (sig, data)
    return data


def reset_cache() -> None:
    _CACHE.clear()


def places_of(ext: dict, ep) -> list[str]:
    """某一集里出现过（按出场顺序去重）的地点。"""
    row = (ext.get("episodes") or {}).get(str(ep)) or {}
    out: list[str] = []
    for s in row.get("scenes") or []:
        if s.get("place") and s["place"] not in out:
            out.append(s["place"])
    return out


def cast_of(ext: dict, ep) -> list[str]:
    row = (ext.get("episodes") or {}).get(str(ep)) or {}
    out: list[str] = []
    for s in row.get("scenes") or []:
        for nm in s.get("cast") or []:
            if nm not in out:
                out.append(nm)
    return out


def place_summary(ext: dict, ep) -> str:
    ps = places_of(ext, ep)
    if not ps:
        return ""
    if len(ps) <= _PLAN_PLACES:
        return "、".join(ps)
    return "、".join(ps[:_PLAN_PLACES]) + f" 等 {len(ps)} 处"


# --------------------------------------------------------------- 文档渲染

def _md_table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |",
           "| " + " | ".join(":---" for _ in head) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def chars_md(ext: dict, plan: dict | None = None) -> str:
    """人物小传：从对白抽到的角色表 + 出处（哪几集、多少句台词）。"""
    plan = plan or {}
    chars = ext.get("characters") or {}
    if not chars:
        return ("# 人物小传\n\n还没有可提取的人物。已写正文里没有 `角色：台词` 形式的对白，"
                "或者本剧还没开始写正文。\n")
    rows = []
    for nm, c in sorted(chars.items(), key=lambda kv: (-kv[1]["lines"], kv[0])):
        eps = c.get("episodes") or []
        span = f"{eps[0]}–{eps[-1]} 集" if len(eps) > 1 else f"第 {eps[0]} 集" if eps else "—"
        rows.append([nm, c.get("lines") or 0, len(eps), span,
                     "、".join(str(x) for x in eps[:8]) + ("…" if len(eps) > 8 else "")])
    return "\n".join([
        f"# 人物小传：{plan.get('scriptName') or '本剧'}",
        "",
        f"> 由正文对白自动提取（零模型调用）：共 **{len(chars)}** 位角色。"
        "台词数为 `角色：台词` 行数，出现集数为该角色开口过的集数。",
        "",
        _md_table(["角色", "台词", "出现集数", "跨度", "出现集"], rows),
        "",
    ])


def struct_md(ext: dict, plan: dict | None = None) -> str:
    """结构大纲：集 → 场 → 地点/时间 的树，按集号排序。"""
    plan = plan or {}
    eps = ext.get("episodes") or {}
    if not eps:
        return "# 结构大纲\n\n正文还没有落盘，暂时没有结构可列。\n"
    lines = [f"# 结构大纲：{plan.get('scriptName') or '本剧'}", "",
             f"> 由正文场标题自动提取：**{len(eps)}** 集 / "
             f"**{ext.get('summary', {}).get('scenes', 0)}** 场。"
             "（`场N：内外 地点 时间`）", ""]
    for key in sorted(eps, key=lambda x: int(x) if str(x).isdigit() else 0):
        e = eps[key]
        head = f"## 第 {key} 集" + (f" · {e['title']}" if e.get("title") else "")
        lines.append(head)
        if not e.get("scenes"):
            lines.append("")
            lines.append("_（这一集没有可识别的场标题，可能是待重写的残片）_")
            lines.append("")
            continue
        lines.append("")
        lines.append(_md_table(
            ["场", "内外", "地点", "时间", "出场"],
            [[s["no"], s.get("io") or "", s.get("place") or "（未标注）",
              s.get("time") or "", "、".join(s.get("cast") or []) or "—"]
             for s in e["scenes"]]))
        lines.append("")
    return "\n".join(lines)


def quality_md(pdir: str, ext: dict, plan: dict | None = None) -> str:
    """质量报告：体检结论 + 覆盖统计，直接读 22-剧本体检.json。"""
    import json
    plan = plan or {}
    scenes = plan.get("scenes") or []
    done = [s for s in scenes if s.get("status") == "completed"]
    words = sum(int(s.get("wordCount") or 0) for s in scenes)
    try:
        audit = json.load(open(os.path.join(pdir, "22-剧本体检.json"), encoding="utf-8")) or {}
    except Exception:
        audit = {}
    issues = audit.get("issues") or []
    sm = ext.get("summary") or {}
    lines = [f"# 质量报告：{plan.get('scriptName') or '本剧'}", "",
             f"> 自动汇总（零模型调用）· 体检时间 {audit.get('at') or '尚未体检'}", "",
             "## 总体", "",
             f"- 进度：**{len(done)}/{len(scenes)}** 集，约 **{words}** 字",
             f"- 正文可识别场次：**{sm.get('scenes', 0)}** 场",
             f"- 人物 **{sm.get('characters', 0)}** 位 · 地点 **{sm.get('locations', 0)}** 处",
             f"- 体检问题：**{len(issues)}** 条", ""]
    if issues:
        lines.append("## 待处理")
        lines.append("")
        for i in issues:
            lines.append(f"- {i}")
    else:
        lines.append("## 待处理")
        lines.append("")
        lines.append("体检没有发现问题（人物在场、场次连续、时代用词均通过）。")
    missing = [str(s.get("no")) for s in scenes if s.get("status") != "completed"]
    if missing:
        lines += ["", "## 待写集", "",
                  "、".join(f"第 {x} 集" for x in missing), ""]
    return "\n".join(lines)


def revisions_md(pdir: str, ext: dict, plan: dict | None = None) -> str:
    """修订记录：把引擎做过的「修订性动作」按时间列出来（来自创作事件与计划元信息）。"""
    import json
    plan = plan or {}
    rows: list[list] = []
    ev_path = os.path.join(pdir, "创作事件.jsonl")
    try:
        with open(ev_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                kind = str(ev.get("kind") or ev.get("type") or ev.get("action") or "")
                if not any(k in kind for k in ("revise", "rewrite", "fix", "repair", "audit")):
                    continue
                rows.append([str(ev.get("at") or ev.get("time") or "")[:19],
                             str(ev.get("agent") or ev.get("by") or "引擎"),
                             str(ev.get("detail") or ev.get("msg") or kind)[:120]])
    except Exception:
        pass
    repair = (plan.get("meta") or {}).get("v4334_repair") or {}
    if repair.get("at"):
        for it in repair.get("items") or []:
            rows.append([str(repair["at"])[:19], "计划校准",
                         f"第 {it.get('no')} 集 {it.get('change')}（{it.get('chars')} 字符）"])
    rows.sort(key=lambda r: r[0])
    lines = [f"# 修订记录：{plan.get('scriptName') or '本剧'}", ""]
    if not rows:
        lines += ["本剧还没有做过通读修订（通读修订会整本重读并出一份修订记录，是分钟级的模型任务）。", "",
                  "已发生的自动动作可以在「质量」页签看到，那里是按体检结果汇总的。"]
    else:
        lines.append(f"> 共 {len(rows)} 条，按时间排序。")
        lines.append("")
        lines.append(_md_table(["时间", "来源", "内容"], rows[-200:]))
    return "\n".join(lines)


def storyboard_md(pdir: str, ext: dict, plan: dict | None = None) -> str:
    """分镜页签的兜底：没生成时也说清楚「差什么、怎么触发」，不留白屏。"""
    import json
    plan = plan or {}
    scenes = [s for s in (plan.get("scenes") or []) if s.get("status") == "completed"]
    sm = ext.get("summary") or {}
    rows = []
    for key in sorted((ext.get("episodes") or {}), key=lambda x: int(x) if str(x).isdigit() else 0):
        e = ext["episodes"][key]
        if not e.get("scenes"):
            continue
        rows.append([f"第 {key} 集" + (f" · {e['title']}" if e.get("title") else ""),
                     len(e["scenes"]), e.get("words") or 0, "待拆分镜"])
    lines = ["# 分镜表", "",
             f"> 尚未生成。本剧已写完 **{len(scenes)}** 集，正文可识别 **{sm.get('scenes', 0)}** 场。",
             "",
             "拆分镜是模型任务（每场一次调用）。点标题栏的「分镜表」→「生成」即可，"
             "已写场次会**逐场追加**到这个页签，不用等全部跑完。", ""]
    if rows:
        lines.append(_md_table(["集", "场数", "字数", "分镜"], rows))
        lines.append("")
    try:
        data = json.load(open(os.path.join(pdir, "08-分镜表.json"), encoding="utf-8")) or {}
        shots = data.get("shots") or []
        if shots:
            got = {}
            for sh in shots:
                got[sh.get("scene")] = got.get(sh.get("scene"), 0) + 1
            lines += ["", f"## 已拆分的场（共 {len(shots)} 个镜头）", "",
                      "、".join(f"第 {k} 场 {v} 镜" for k, v in sorted(got.items(), key=lambda kv: str(kv[0])))]
    except Exception:
        pass
    return "\n".join(lines)
