"""技能库加载器 + 最小上下文注入（v1.0.0 · 神笔文学创作台）

职责：
1. 扫描 skillkit/，解析每个 .md 的 YAML frontmatter，建立索引；
2. 按「文体 + 阶段」精确挑出该阶段真正要用的技能文件，拼成 system 上下文。

**核心约束：绝不整库注入。** 库有 ~600 KB，一次全塞会挤爆上下文且稀释注意力。
stage_inject() 单次只取 3–6 个文件（8–25 KB），按 weight 排序后截断。

frontmatter 缺省时按目录推断 medium/kind，保证无 frontmatter 的文件也能用。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import medium as medium_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLKIT_DIR = os.path.join(ROOT, "skillkit")

# 外部语料挂载目录（默认 gitignore、不随产品分发；放进去的东西不在授权范围内）
EXTERNAL_DIR = os.path.join(SKILLKIT_DIR, "_external")

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


@dataclass
class SkillFile:
    relpath: str
    name: str = ""
    title: str = ""
    media: list[str] = field(default_factory=list)
    stages: list[str] = field(default_factory=list)
    kind: str = "method"
    weight: int = 3
    license: str = ""
    source: str = ""
    size: int = 0
    text: str = ""
    external: bool = False

    def header(self) -> str:
        """文件头注释：来源与许可要能随内容一起被 LLM 看到，避免误引。"""
        bits = []
        if self.source:
            bits.append(f"来源：{self.source}")
        if self.license:
            bits.append(f"许可：{self.license}")
        if bits:
            return f"[{' | '.join(bits)}]\n"
        return ""


# ---------------------------------------------------------------- 索引

_INDEX: list[SkillFile] | None = None
_TEXT_CACHE: dict[str, str] = {}


def _parse_scalar(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def _parse_frontmatter(text: str) -> dict:
    m = _FM_RE.match(text)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = _parse_scalar(v)
    return out


def _infer_from_path(relpath: str) -> tuple[list[str], str]:
    """按路径推断 medium 与 kind（frontmatter 缺失时的兜底）。"""
    parts = relpath.split(os.sep)
    top = parts[0] if parts else ""
    med_map = {
        "novel": ["novel"],
        "script": ["script"],
        "poetry": ["poetry"],
        "prose": ["prose"],
        "_core": ["all"],
    }
    media = med_map.get(top, ["all"])
    kind = "method"
    if "templates" in parts:
        kind = "template"
    elif "data" in parts:
        kind = "data"
    elif "cases" in parts or "案例" in top or "大师" in parts:
        kind = "case"
    return media, kind


def _iter_md(base: str, prefix: str = "") -> list[str]:
    out = []
    if not os.path.isdir(base):
        return out
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for fn in filenames:
            if fn.endswith(".md"):
                rel = os.path.relpath(os.path.join(dirpath, fn), base)
                out.append(os.path.join(prefix, rel) if prefix else rel)
    return out


def _load_one(path: str, relpath: str, external: bool = False) -> SkillFile | None:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None
    fm = _parse_frontmatter(text)
    media = fm.get("medium") or fm.get("media") or []
    if isinstance(media, str):
        media = [media]
    stages = fm.get("stages") or fm.get("stage") or []
    if isinstance(stages, str):
        stages = [stages]
    if not media:
        media, kind0 = _infer_from_path(relpath)
    else:
        _, kind0 = _infer_from_path(relpath)
    kind = fm.get("kind") or kind0
    weight = fm.get("weight")
    try:
        weight = int(weight)
    except Exception:
        weight = 3
    return SkillFile(
        relpath=relpath,
        name=str(fm.get("name") or os.path.splitext(os.path.basename(relpath))[0]),
        title=str(fm.get("title") or ""),
        media=[str(m) for m in media] or ["all"],
        stages=[str(s) for s in stages],
        kind=str(kind),
        weight=weight,
        license=str(fm.get("license") or ""),
        source=str(fm.get("source") or ""),
        size=len(text),
        text=text,
        external=external,
    )


def index(rebuild: bool = False) -> list[SkillFile]:
    """全库索引（带缓存）。"""
    global _INDEX
    if _INDEX is not None and not rebuild:
        return _INDEX
    items: list[SkillFile] = []
    for rel in _iter_md(SKILLKIT_DIR):
        sf = _load_one(os.path.join(SKILLKIT_DIR, rel), rel)
        if sf:
            items.append(sf)
    if os.path.isdir(EXTERNAL_DIR):
        for rel in _iter_md(EXTERNAL_DIR):
            sf = _load_one(os.path.join(EXTERNAL_DIR, rel), os.path.join("_external", rel), external=True)
            if sf:
                items.append(sf)
    _INDEX = items
    return items


def reload():
    """改了技能文件后重载索引。"""
    global _INDEX
    _INDEX = None
    _TEXT_CACHE.clear()
    return index(rebuild=True)


def read(relpath: str) -> str:
    """读一个技能文件正文（带缓存）。"""
    if relpath in _TEXT_CACHE:
        return _TEXT_CACHE[relpath]
    base = EXTERNAL_DIR if relpath.startswith("_external" + os.sep) else SKILLKIT_DIR
    p = os.path.join(base, relpath)
    try:
        with open(p, encoding="utf-8") as f:
            t = f.read()
    except Exception:
        t = ""
    _TEXT_CACHE[relpath] = t
    return t


def by_name(name: str) -> SkillFile | None:
    for sf in index():
        if sf.name == name:
            return sf
    return None


# ---------------------------------------------------------------- 注入

def _match(sf: SkillFile, med: str, stage: str) -> bool:
    if not any(m in (med, "all") for m in sf.media):
        return False
    if not sf.stages:
        return True  # 未声明阶段 = 通用
    return stage in sf.stages


def pick(med: str, stage: str, include_case: bool = False,
         limit: int = 8, kinds: tuple[str, ...] = ("method", "template", "data"),
         genre: str = "") -> list[SkillFile]:
    """挑出该文体 + 该阶段要用的技能文件，按 weight 降序。

    genre: 小说题材（mystery/romance/horror/urban/xuanhuan/_index）。
           传了就把该题材文件提到最前，其余题材文件不取。
    """
    cand = []
    gkey = _genre_key(genre) if genre else ""
    gen_dir = os.path.join("references", "genre")
    for sf in index():
        if sf.kind == "case" and not include_case:
            continue
        if kinds and sf.kind not in kinds:
            continue
        if not _match(sf, med, stage):
            continue
        cand.append(sf)

    def sort_key(sf: SkillFile):
        # 命中的题材排最前；其它题材排最后（数量不够时才补位）
        if gkey and gen_dir in sf.relpath:
            if sf.relpath.endswith(os.path.join(gen_dir, gkey + ".md")):
                return (0, -sf.weight, sf.relpath)      # 命中题材：最高优先
            return (2, -sf.weight, sf.relpath)          # 其它题材：垫底
        return (1, -sf.weight, sf.relpath)              # 非题材文件：中间

    cand.sort(key=sort_key)
    if gkey:
        # 命中题材 + 非题材文件优先；其余题材仅在数量不足时补位
        primary = [sf for sf in cand if sort_key(sf)[0] < 2]
        rest = [sf for sf in cand if sort_key(sf)[0] == 2]
        cand = primary + rest
    return cand[:limit]


def _genre_key(genre: str) -> str:
    """题材名 → 题材 craft 文件 key；未命中回落 _index。"""
    g = (genre or "").lower()
    table = [
        (("推理", "悬疑", "犯罪", "mystery", "惊悚"), "mystery"),
        (("言情", "甜宠", "都市情感", "romance"), "romance"),
        (("恐怖", "惊悚", "horror", "灵异"), "horror"),
        (("玄幻", "仙侠", "修仙", "修真", "xuanhuan", "玄幻仙侠"), "xuanhuan"),
        (("都市", "现实", "职场", "商战", "重生", "赘婿", "urban", "爽文"), "urban"),
    ]
    for keys, val in table:
        for k in keys:
            if k in g:
                return val
    return "_index"


def stage_inject(stage: str, med: str, extra: list[str] | None = None,
                 include_case: bool = False, max_bytes: int = 26000,
                 limit: int = 8, genre: str = "") -> str:
    """按「文体 + 阶段」拼出最小上下文。

    - 只取该阶段必需文件（默认 3–6 个）
    - 超 max_bytes 时按 weight 从低到高丢弃，保证高价值文件不被截断
    - extra: 额外强制注入的 skill name 列表
    - include_case: 是否允许案例库（默认否——体积大且部分受版权约束）
    - genre: 小说题材定向（只注入该题材 craft，不注入其它题材）
    """
    if not medium_mod.is_medium(med):
        med = "novel"
    files = pick(med, stage, include_case=include_case, limit=limit, genre=genre)
    if extra:
        chosen = {sf.relpath for sf in files}
        for nm in extra:
            sf = by_name(nm)
            if sf and sf.relpath not in chosen:
                files.append(sf)
                chosen.add(sf.relpath)

    parts: list[str] = []
    total = 0
    dropped: list[str] = []
    for sf in sorted(files, key=lambda s: -s.weight):
        head = sf.header()
        body = sf.text
        piece = f"\n\n===== 技能：{sf.title or sf.name}（{sf.relpath}）=====\n{head}{body}"
        if total + len(piece) > max_bytes and parts:
            dropped.append(sf.relpath)
            continue
        parts.append(piece)
        total += len(piece)

    if not parts:
        return ""
    out = f"【{medium_mod.label(med)} · {stage} 阶段参考】" + "".join(parts)
    if dropped:
        out += f"\n\n（本次因篇幅限制未注入：{', '.join(dropped)}）"
    return out


def list_skills(med: str = "", stage: str = "") -> list[dict]:
    """给前端/调试用的技能清单。"""
    out = []
    for sf in index():
        if med and not any(m in (med, "all") for m in sf.media):
            continue
        if stage and stage not in sf.stages:
            continue
        out.append({
            "relpath": sf.relpath,
            "name": sf.name,
            "title": sf.title,
            "media": sf.media,
            "stages": sf.stages,
            "kind": sf.kind,
            "weight": sf.weight,
            "license": sf.license,
            "source": sf.source,
            "size": sf.size,
            "external": sf.external,
        })
    return out


def stats() -> dict:
    items = index()
    by_media: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    total = 0
    for sf in items:
        total += sf.size
        for m in sf.media:
            by_media[m] = by_media.get(m, 0) + 1
        by_kind[sf.kind] = by_kind.get(sf.kind, 0) + 1
    return {
        "files": len(items),
        "bytes": total,
        "by_media": by_media,
        "by_kind": by_kind,
    }
