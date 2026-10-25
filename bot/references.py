"""全局参考资料库（上传 → 解析 → 切片 → 分析 → 检索 → 引用 → 查重）。

存储布局
- ``references/<r-xxxxxxxxxxxx>/``
  - ``meta.json``      元信息 + 最近一次分析结果（不含原文）
  - ``source.<ext>``   上传原件（不进入 git；由主 Agent 的 .gitignore 负责）
  - ``text.txt``       抽取后的规范纯文本（切片/检索/分析都基于它）
  - ``chunks.jsonl``   每行 {no,text,start,end,headings?,hard_split?}
  - ``analysis.json``  与 meta.analysis 同步的独立副本
  - ``overlap.json``   预留：最近一次查重结果（当前只返回报告，不写盘）
- ``projects/<key>/references.json`` ``{items:[{ref_id,usage_mode,added_at}]}`` 是项目关联的
  唯一真相；``02-写作计划.json`` 里的投影仅在 references.json 缺失时兼容读取。

安全边界
- ref_id 必须匹配 ``^r-[0-9a-f]{12}$``，所有路径都过 safe_join；项目 key 必须对应真实项目目录。
- 写操作失败会删掉本次新建的参考目录，不留半成品；返回错误一律中文、不含绝对路径/堆栈。
- 版权未确认（copyright_confirmed=False）仍可入库，但 usage_mode 强制 disabled，检索/注入永不返回。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import tempfile
import threading
from datetime import datetime
from urllib.parse import urlparse

from . import distill, ingest, llm
from .safeio import atomic_write_json, atomic_write_text, safe_join

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFERENCES_DIR = os.path.join(ROOT, "references")
PROJECTS_DIR = os.path.join(ROOT, "projects")

MAX_REFS = 200
MAX_REF_CHARS = 2_000_000
MAX_CHUNKS = 5000
USAGE_MODES = ("style", "facts", "structure", "adapt", "disabled")
KINDS = ("novel", "script", "poetry", "prose", "reference")
REF_ID_RE = re.compile(r"^r-[0-9a-f]{12}$")

_CREATE_LOCK = threading.Lock()
_META_LOCK = threading.RLock()
_LINK_LOCK = threading.RLock()
_INDEX_LOCK = threading.RLock()
_INDEX: dict[str, dict] = {}

_NOW_FMT = "%Y-%m-%dT%H:%M:%S"


# ---------------------------------------------------------------- 通用小工具

def _now() -> str:
    return datetime.now().strftime(_NOW_FMT)


def _fail(msg: str, **extra) -> dict:
    out = {"ok": False, "error": msg}
    out.update(extra)
    return out


def _ok(payload: dict | None = None, **extra) -> dict:
    out = {"ok": True}
    if payload:
        out.update(payload)
    out.update(extra)
    return out


def _atomic_write_bytes(path: str, data: bytes) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _sig(path: str):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _validate_ref_id(ref_id: str) -> str:
    if not isinstance(ref_id, str) or not REF_ID_RE.match(ref_id):
        raise ValueError("参考 id 非法")
    return ref_id


def _ref_dir(ref_id: str) -> str | None:
    try:
        _validate_ref_id(ref_id)
        return safe_join(REFERENCES_DIR, ref_id, label="参考 id")
    except ValueError:
        return None


def _projects_root() -> str:
    """延迟取 writer.PROJECTS_DIR：测试会 monkeypatch 它，不能模块导入时冻住。"""
    try:
        from . import writer
        return os.path.realpath(getattr(writer, "PROJECTS_DIR", PROJECTS_DIR))
    except Exception:
        return os.path.realpath(PROJECTS_DIR)


def _project_dir(key: str) -> str | None:
    """项目 key 必须是真实存在的项目目录名；非法/不存在返回 None。"""
    try:
        from . import writer
        p = writer._project_dir(key)
    except Exception:
        return None
    try:
        real = os.path.realpath(p)
        root = _projects_root()
        if os.path.commonpath([root, real]) != root or real == root:
            return None
        return real if os.path.isdir(real) else None
    except ValueError:
        return None


def _project_dirs() -> list[str]:
    root = _projects_root()
    try:
        return [os.path.join(root, d) for d in sorted(os.listdir(root))
                if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return []


def _links_path(pdir: str) -> str:
    return os.path.join(pdir, "references.json")


def _read_links(pdir: str) -> dict:
    obj = _read_json(_links_path(pdir))
    if not isinstance(obj, dict):
        return {"items": []}
    items = obj.get("items")
    if not isinstance(items, list):
        items = []
    clean = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        rid = it.get("ref_id")
        if not isinstance(rid, str) or not REF_ID_RE.match(rid) or rid in seen:
            continue
        mode = it.get("usage_mode") if it.get("usage_mode") in USAGE_MODES else "facts"
        seen.add(rid)
        clean.append({"ref_id": rid, "usage_mode": mode,
                      "added_at": str(it.get("added_at") or "")})
    obj["items"] = clean
    return obj


def _write_links(pdir: str, obj: dict) -> None:
    atomic_write_json(_links_path(pdir), obj)


def _all_links() -> dict[str, set[str]]:
    """ref_id → 关联项目 key 集合；坏文件/坏条目直接跳过，不影响其他项目。"""
    out: dict[str, set[str]] = {}
    for pdir in _project_dirs():
        key = os.path.basename(pdir)
        for it in _read_links(pdir)["items"]:
            out.setdefault(it["ref_id"], set()).add(key)
    return out


def _all_ref_ids() -> list[str]:
    try:
        names = os.listdir(REFERENCES_DIR)
    except OSError:
        return []
    out = []
    for n in names:
        if REF_ID_RE.match(n) and os.path.isdir(os.path.join(REFERENCES_DIR, n)):
            out.append(n)
    return sorted(out)


def _count_refs() -> int:
    return len(_all_ref_ids())


def _meta_summary(meta: dict) -> dict:
    """把 meta 规范化成前端可用的摘要；缺字段补默认，旧数据也能读。"""
    mode = meta.get("usage_mode") if meta.get("usage_mode") in USAGE_MODES else "facts"
    if not meta.get("copyright_confirmed", True):
        mode = "disabled"
    analysis = meta.get("analysis") if isinstance(meta.get("analysis"), dict) else {}
    return {
        "id": meta.get("id") or "",
        "ref_id": meta.get("id") or "",
        "title": meta.get("title") or "未命名参考",
        "kind": meta.get("kind") or "reference",
        "media": meta.get("media") or "",
        "source_type": meta.get("source_type") or "text",
        "source": meta.get("source") or "",
        "original_name": meta.get("original_name") or "",
        "ext": meta.get("ext") or "",
        "chars": int(meta.get("chars") or 0),
        "chunks": int(meta.get("chunks") or 0),
        "created_at": meta.get("created_at") or "",
        "updated_at": meta.get("updated_at") or "",
        "usage_mode": mode,
        "copyright_confirmed": bool(meta.get("copyright_confirmed", True)),
        "tags": list(meta.get("tags") or []),
        "summary": meta.get("summary") or "",
        "analysis": analysis,
        "analysis_at": meta.get("analysis_at") or "",
        "hash": meta.get("hash") or "",
        "linked_projects": list(meta.get("linked_projects") or []),
        "warnings": list(meta.get("warnings") or []),
    }


def _read_meta(ref_id: str) -> dict | None:
    pdir = _ref_dir(ref_id)
    if not pdir:
        return None
    obj = _read_json(os.path.join(pdir, "meta.json"))
    if not isinstance(obj, dict):
        return None
    obj["id"] = ref_id
    return _meta_summary(obj)


def _write_meta(ref_id: str, meta: dict) -> None:
    pdir = _ref_dir(ref_id)
    if not pdir:
        raise ValueError("参考 id 非法")
    # 只写 schema 内的字段：_read_meta 会补 ref_id 等展示别名，别污染磁盘格式。
    clean = {k: v for k, v in meta.items() if k not in ("ref_id", "projects", "has_source")}
    clean["id"] = ref_id
    clean["updated_at"] = _now()
    atomic_write_json(os.path.join(pdir, "meta.json"), clean)
    _invalidate(ref_id)


def _invalidate(ref_id: str) -> None:
    with _INDEX_LOCK:
        _INDEX.pop(ref_id, None)


def _clear_cache() -> None:
    with _INDEX_LOCK:
        _INDEX.clear()


def _read_text(ref_id: str) -> str:
    pdir = _ref_dir(ref_id)
    if not pdir:
        return ""
    try:
        with open(os.path.join(pdir, "text.txt"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _new_ref_dir() -> tuple[str, str]:
    """在锁内完成「数量检查 + 唯一 id + mkdir」，并发新增不会撞号。"""
    os.makedirs(REFERENCES_DIR, exist_ok=True)
    with _CREATE_LOCK:
        if _count_refs() >= MAX_REFS:
            raise RuntimeError("参考库已达 200 个上限，请先删除不用的参考")
        for _ in range(8):
            rid = "r-" + secrets.token_hex(6)
            path = safe_join(REFERENCES_DIR, rid, label="参考 id")
            try:
                os.makedirs(path, exist_ok=False)
                return rid, path
            except FileExistsError:
                continue
    raise RuntimeError("无法分配参考编号，请重试")


# ---------------------------------------------------------------- 入库

def _coerce_bytes(file_bytes) -> tuple[bytes, str]:
    if isinstance(file_bytes, (bytes, bytearray, memoryview)):
        return bytes(file_bytes), ""
    if isinstance(file_bytes, (str, os.PathLike)):
        p = os.path.realpath(os.fspath(file_bytes))
        if not os.path.isfile(p):
            return b"", "文件不存在"
        if os.path.getsize(p) > ingest.MAX_BYTES:
            return b"", "文件超过 30MB 上限"
        try:
            with open(p, "rb") as f:
                return f.read(ingest.MAX_BYTES + 1), ""
        except OSError:
            return b"", "文件读取失败"
    return b"", "文件内容非法（需要 bytes 或文件路径）"


def _store_extracted(text: str, *, title: str, kind: str, source_type: str,
                     source: str, original_name: str, ext: str,
                     usage_mode: str, copyright_confirmed: bool,
                     raw_bytes: bytes | None = None,
                     warnings: list[str] | None = None) -> dict:
    """所有来源共用的落盘流程；任何一步失败都删除本次 ref 目录。"""
    clean = ingest.clean_text(text or "")
    if not clean.strip():
        return _fail("内容为空，无法入库")
    if len(clean) > MAX_REF_CHARS:
        return _fail("单个参考超过 200 万字符上限，请拆分后再上传")
    chunks = ingest.chunk_text(clean, max_chunks=MAX_CHUNKS)
    if not chunks:
        return _fail("内容无法切片（可能全是空白或过短）")
    if len(chunks) >= MAX_CHUNKS and chunks[-1]["end"] < len(clean):
        return _fail("内容过长，切片超过 5000 片上限，请拆分后再上传")

    if not kind or kind not in KINDS:
        kind = ingest.detect_kind(clean)
    mode = usage_mode if usage_mode in USAGE_MODES else "facts"
    if not copyright_confirmed:
        mode = "disabled"

    summary_meta = ingest.summarize_meta(clean, original_name)
    title = (str(title or "").strip() or summary_meta["title"])[:200]
    media = kind if kind in ("novel", "script", "poetry", "prose") else ""
    analysis = {
        "genre": media or kind,
        "structure": "",
        "characters": [],
        "style_traits": [],
        "key_facts": [],
        "caution": [],
        "fallback": True,
    }
    now = _now()

    try:
        rid, pdir = _new_ref_dir()
    except RuntimeError as e:
        return _fail(str(e))
    except Exception:
        return _fail("无法创建参考目录，请检查参考库是否可写")

    try:
        if raw_bytes:
            _atomic_write_bytes(os.path.join(pdir, f"source{ext}"), raw_bytes)
        atomic_write_text(os.path.join(pdir, "text.txt"), clean)
        lines = []
        for c in chunks:
            obj = {"no": c["no"], "text": c["text"], "start": c["start"], "end": c["end"]}
            if c.get("headings"):
                obj["headings"] = c["headings"]
            if c.get("hard_split"):
                obj["hard_split"] = True
            lines.append(json.dumps(obj, ensure_ascii=False))
        atomic_write_text(os.path.join(pdir, "chunks.jsonl"), "\n".join(lines) + "\n")
        atomic_write_json(os.path.join(pdir, "analysis.json"),
                          {"ref_id": rid, "analyzed_at": "", "analysis": analysis})
        meta = {
            "id": rid,
            "title": title,
            "kind": kind,
            "media": media,
            "source_type": source_type,
            "source": source,
            "original_name": original_name,
            "ext": ext or ".txt",
            "chars": len(clean),
            "chunks": len(chunks),
            "created_at": now,
            "updated_at": now,
            "usage_mode": mode,
            "copyright_confirmed": bool(copyright_confirmed),
            "tags": [],
            "summary": summary_meta["excerpt"],
            "analysis": analysis,
            "analysis_at": "",
            "hash": hashlib.sha256(clean.encode("utf-8")).hexdigest(),
            "linked_projects": [],
            "warnings": list(warnings or []),
        }
        atomic_write_json(os.path.join(pdir, "meta.json"), meta)
    except Exception:
        shutil.rmtree(pdir, ignore_errors=True)
        return _fail("写入参考库失败，请重试")

    _invalidate(rid)
    return {"ok": True, "ref_id": rid, "ref": _meta_summary(meta),
            "warnings": list(warnings or [])}


def add_upload(file_bytes, filename: str, title: str = "", usage_mode: str = "facts",
               copyright_confirmed: bool = True, cfg: dict | None = None) -> dict:
    """上传文件入库。file_bytes 可为 bytes/bytearray/memoryview 或本地文件路径。"""
    try:
        name = ingest.safe_filename(filename)
        ext = os.path.splitext(name)[1].lower()
        if ext not in ingest.ALLOWED_EXTS:
            return _fail(f"不支持的格式：{ext or '（无扩展名）'}；支持 "
                         + " ".join(ingest.ALLOWED_EXTS))
        data, err = _coerce_bytes(file_bytes)
        if err:
            return _fail(err)
        if len(data) > ingest.MAX_BYTES:
            return _fail("文件超过 30MB 上限")
        bad = ingest.magic_error(ext, data[:8])
        if bad:
            return _fail(bad)

        fd, tmp = tempfile.mkstemp(prefix="ref-upload-", suffix=ext)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            info = ingest.extract_text(tmp, name)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if not info.get("ok"):
            return _fail(info.get("error") or "文件解析失败",
                         warnings=list(info.get("warnings") or []))
        return _store_extracted(
            info["text"], title=title, kind=info.get("kind") or "",
            source_type="upload", source=name, original_name=name, ext=ext,
            usage_mode=usage_mode, copyright_confirmed=copyright_confirmed,
            raw_bytes=data, warnings=list(info.get("warnings") or []))
    except Exception:
        return _fail("上传处理失败，请重试")


def add_text(text: str, title: str, kind: str = "", usage_mode: str = "facts",
             copyright_confirmed: bool = True, cfg: dict | None = None) -> dict:
    """粘贴文本入库（采访稿、大纲、设定等）。"""
    try:
        if not isinstance(text, str) or not text.strip():
            return _fail("内容为空，无法入库")
        name = ingest.safe_filename((title or "手动录入") + ".txt")
        return _store_extracted(
            text, title=title, kind=kind, source_type="text", source="手动录入",
            original_name=name, ext=".txt", usage_mode=usage_mode,
            copyright_confirmed=copyright_confirmed)
    except Exception:
        return _fail("文本入库失败，请重试")


def add_url(url: str, title: str = "", usage_mode: str = "facts",
            copyright_confirmed: bool = True, max_chars: int = 120000,
            cfg: dict | None = None) -> dict:
    """抓取网页正文节选入库。复用 distill.fetch_url_text（含逐跳 SSRF 复查）。"""
    try:
        u = str(url or "").strip()
        try:
            parsed = urlparse(u)
        except Exception:
            parsed = None
        if not parsed or parsed.scheme not in ("http", "https"):
            return _fail("只支持 http/https 网址")
        if not parsed.hostname:
            return _fail("网址缺少主机名")
        if parsed.username or parsed.password:
            return _fail("网址不能包含用户名或密码")
        if not distill._url_allowed(u):
            return _fail("该网址指向内网/回环/保留地址，已拒绝抓取")
        limit = max(1000, min(int(max_chars or 120000), 200000))
        try:
            fetched = distill.fetch_url_text(u, max_chars=limit)
        except Exception:
            fetched = ""
        if not fetched or not fetched.strip():
            return _fail("抓取失败：网址无法访问、被拒绝或没有可读正文")
        name = ingest.safe_filename((title or parsed.hostname or "网页参考") + ".txt")
        return _store_extracted(
            fetched, title=title or parsed.hostname or "网页参考", kind="",
            source_type="url", source=u, original_name=name, ext=".txt",
            usage_mode=usage_mode, copyright_confirmed=copyright_confirmed,
            warnings=["仅保存已抓取的正文节选，未保存整站"])
    except Exception:
        return _fail("网址抓取失败，请重试")


# ---------------------------------------------------------------- 读取

def list_refs(project_key: str = "") -> dict:
    """全库列表；给定项目时只返回该项目关联的参考，并标记 linked。"""
    empty_stats = {"count": 0, "chars": 0, "by_kind": {}, "by_usage": {}}
    try:
        links = _all_links()
        only_ids: set[str] | None = None
        if project_key:
            pdir = _project_dir(project_key)
            if not pdir:
                return _fail("项目不存在或名称非法", items=[], stats=empty_stats)
            only_ids = set(project_ref_ids(project_key))
        items = []
        for rid in _all_ref_ids():
            if only_ids is not None and rid not in only_ids:
                continue
            meta = _read_meta(rid)
            if not meta:
                continue
            item = _meta_summary(meta)
            item["linked"] = bool(links.get(rid))
            item["projects"] = sorted(links.get(rid) or [])
            items.append(item)
        items.sort(key=lambda x: (x["created_at"], x["id"]), reverse=True)
        stats = {
            "count": len(items),
            "chars": sum(i["chars"] for i in items),
            "by_kind": {},
            "by_usage": {},
        }
        for i in items:
            stats["by_kind"][i["kind"]] = stats["by_kind"].get(i["kind"], 0) + 1
            stats["by_usage"][i["usage_mode"]] = stats["by_usage"].get(i["usage_mode"], 0) + 1
        return {"ok": True, "items": items, "stats": stats}
    except Exception:
        return _fail("读取参考库失败", items=[], stats=empty_stats)


def get_ref(ref_id: str) -> dict | None:
    """单个参考的元信息 + 分析；不返回全文，避免接口负载过大。"""
    meta = _read_meta(ref_id)
    if not meta:
        return None
    pdir = _ref_dir(ref_id)
    if not pdir:
        return None
    analysis = _read_json(os.path.join(pdir, "analysis.json"))
    if isinstance(analysis, dict) and isinstance(analysis.get("analysis"), dict):
        meta["analysis"] = analysis["analysis"]
    meta["projects"] = sorted(_all_links().get(ref_id) or [])
    try:
        meta["has_source"] = any(
            os.path.isfile(os.path.join(pdir, n)) for n in os.listdir(pdir)
            if n.startswith("source."))
    except OSError:
        meta["has_source"] = False
    return meta


def read_chunks(ref_id: str, offset: int = 0, limit: int = 50) -> dict:
    """预览切片；单次最多 50 片 / 2 万字，超出部分截断并标记。"""
    pdir = _ref_dir(ref_id)
    if not pdir:
        return _fail("参考 id 非法")
    try:
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 50), 50))
    except (TypeError, ValueError):
        return _fail("offset/limit 参数非法")
    path = os.path.join(pdir, "chunks.jsonl")
    if not os.path.isfile(path):
        return _fail("参考不存在或切片文件缺失")
    items: list[dict] = []
    total = 0
    chars = 0
    truncated = False
    try:
        with open(path, encoding="utf-8") as f:
            for idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                total = idx + 1
                if total <= offset:
                    continue
                if len(items) >= limit:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                text = str(obj.get("text") or "")
                remaining = 20000 - chars
                if remaining <= 0:
                    truncated = True
                    break
                if len(text) > remaining:
                    text = text[:remaining]
                    obj["truncated"] = True
                    truncated = True
                item = {"no": obj.get("no") or (idx + 1), "text": text,
                        "start": obj.get("start", 0), "end": obj.get("end", 0)}
                if obj.get("headings"):
                    item["headings"] = obj["headings"]
                if obj.get("hard_split"):
                    item["hard_split"] = True
                items.append(item)
                chars += len(text)
                if truncated:
                    break
    except OSError:
        return _fail("读取切片失败")
    return {"ok": True, "ref_id": ref_id, "total": total, "offset": offset,
            "limit": limit, "items": items, "chars": chars, "truncated": truncated}


def stats() -> dict:
    """参考库总览：数量/字数/切片数/体裁与用法分布。"""
    items = []
    for rid in _all_ref_ids():
        meta = _read_meta(rid)
        if meta:
            items.append(meta)
    by_kind: dict[str, int] = {}
    by_usage: dict[str, int] = {}
    for m in items:
        by_kind[m["kind"]] = by_kind.get(m["kind"], 0) + 1
        by_usage[m["usage_mode"]] = by_usage.get(m["usage_mode"], 0) + 1
    return {
        "ok": True,
        "count": len(items),
        "max_refs": MAX_REFS,
        "chars": sum(m["chars"] for m in items),
        "chunks": sum(m["chunks"] for m in items),
        "by_kind": by_kind,
        "by_usage": by_usage,
        "linked": len(_all_links()),
    }


# ---------------------------------------------------------------- 修改 / 删除

def delete_ref(ref_id: str) -> dict:
    """删除参考目录，并从所有项目的 references.json 里清理关联；重复删除安全。"""
    pdir = _ref_dir(ref_id)
    if not pdir:
        return _fail("参考 id 非法")
    if not os.path.isdir(pdir):
        return {"ok": True, "deleted": False, "ref_id": ref_id, "note": "参考不存在或已删除"}
    try:
        shutil.rmtree(pdir)
    except OSError:
        return _fail("删除参考失败，请检查文件占用")
    _invalidate(ref_id)
    try:
        with _LINK_LOCK:
            for proj in _project_dirs():
                obj = _read_links(proj)
                kept = [it for it in obj["items"] if it["ref_id"] != ref_id]
                if len(kept) != len(obj["items"]):
                    obj["items"] = kept
                    _write_links(proj, obj)
    except Exception:
        # 关联清理失败不回滚删除：参考已不存在，残留条目在读取时会被忽略。
        pass
    return {"ok": True, "deleted": True, "ref_id": ref_id}


def _sync_usage(ref_id: str, mode: str) -> None:
    try:
        with _LINK_LOCK:
            for proj in _project_dirs():
                obj = _read_links(proj)
                changed = False
                for it in obj["items"]:
                    if it["ref_id"] == ref_id and it["usage_mode"] != mode:
                        it["usage_mode"] = mode
                        changed = True
                if changed:
                    _write_links(proj, obj)
    except Exception:
        pass


def set_usage(ref_id: str, usage_mode: str) -> dict:
    """切换用法并同步到所有已关联项目。版权未确认时只允许 disabled。"""
    if usage_mode not in USAGE_MODES:
        return _fail("用法模式非法，只允许 style/facts/structure/adapt/disabled")
    meta = _read_meta(ref_id)
    if not meta:
        return _fail("参考不存在")
    if not meta["copyright_confirmed"] and usage_mode != "disabled":
        return _fail("该参考尚未确认版权，只能设为 disabled；请先确认版权")
    try:
        with _META_LOCK:
            meta["usage_mode"] = usage_mode
            _write_meta(ref_id, meta)
    except Exception:
        return _fail("保存用法失败，请重试")
    _sync_usage(ref_id, usage_mode)
    return {"ok": True, "ref_id": ref_id, "usage_mode": usage_mode}


def update_meta(ref_id: str, title: str | None = None, tags: list[str] | None = None,
                copyright_confirmed: bool | None = None) -> dict:
    """改标题/标签/版权确认。取消确认会立刻强制 disabled；首次确认会解禁为 facts。"""
    meta = _read_meta(ref_id)
    if not meta:
        return _fail("参考不存在")
    note = ""
    try:
        with _META_LOCK:
            if title is not None:
                t = str(title).strip()[:200]
                if t:
                    meta["title"] = t
            if tags is not None:
                if not isinstance(tags, list):
                    return _fail("tags 必须是字符串数组")
                clean: list[str] = []
                for t in tags:
                    s = str(t).strip()[:30]
                    if s and s not in clean:
                        clean.append(s)
                    if len(clean) >= 20:
                        break
                meta["tags"] = clean
            if copyright_confirmed is not None:
                confirmed = bool(copyright_confirmed)
                was = bool(meta.get("copyright_confirmed"))
                meta["copyright_confirmed"] = confirmed
                if not confirmed:
                    meta["usage_mode"] = "disabled"
                elif not was and meta.get("usage_mode") == "disabled":
                    # 取消确认时被强制禁用的参考，确认后恢复为最保守的 facts；
                    # 用户仍可在界面上改成 style/structure/adapt。
                    meta["usage_mode"] = "facts"
                    note = "版权已确认，用法恢复为 facts"
            _write_meta(ref_id, meta)
    except Exception:
        return _fail("保存参考信息失败，请重试")
    _sync_usage(ref_id, meta.get("usage_mode") or "facts")
    out = {"ok": True, "ref": _meta_summary(meta)}
    if note:
        out["note"] = note
    return out


# ---------------------------------------------------------------- LLM 分析

def _as_str(v, limit: int = 500) -> str:
    if isinstance(v, str):
        return v.strip()[:limit]
    if isinstance(v, (list, tuple)):
        return "；".join(str(x).strip() for x in v if str(x).strip())[:limit]
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)[:limit]
    return ""


def _as_list(v, limit: int = 20, item_limit: int = 200) -> list[str]:
    if isinstance(v, str):
        parts = [x.strip() for x in re.split(r"[\n；;]+", v) if x.strip()]
        return [x[:item_limit] for x in parts[:limit]]
    if isinstance(v, dict):
        v = [f"{k}：{val}" for k, val in v.items()]
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for x in v:
        s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
        s = s.strip()[:item_limit]
        if s:
            out.append(s)
        if len(out) >= limit:
            break
    return out


_FALLBACK_INPUT = 6000


def analyze_ref(ref_id: str, cfg: dict | None = None) -> dict:
    """一次 LLM 调用做结构化分析；LLM 空/失败时用确定性摘要降级，绝不抛异常。"""
    meta = _read_meta(ref_id)
    if not meta:
        return _fail("参考不存在")
    text = _read_text(ref_id)
    if not text.strip():
        return _fail("参考正文缺失，无法分析")
    sample = text[:_FALLBACK_INPUT]
    prompt = (
        "你是参考资料分析助手。请对下面的资料做结构化分析，只输出严格 JSON，不要复述大段原文。字段：\n"
        '{"summary":"150 字以内概括",'
        '"genre":"体裁/题材",'
        '"structure":"结构骨架（章节/幕场/段落组织）",'
        '"characters":["人物或叙述对象及其关系"],'
        '"style_traits":["语言与风格特征（可执行的写法提示）"],'
        '"key_facts":["写作时可直接引用的关键事实/设定"],'
        '"caution":["版权、史实存疑、虚构冲突等使用注意"]}'
    )
    user = (f"【标题】{meta['title']}\n【类型】{meta['kind']}\n"
            f"【正文节选（可能截断）】\n{sample}")

    raw = None
    try:
        raw = llm.chat_json(
            [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
            cfg=cfg, stage="planning")
    except Exception:
        raw = None

    expected = ("summary", "genre", "structure", "characters", "style_traits",
                "key_facts", "caution")
    has_any = isinstance(raw, dict) and any(k in raw for k in expected)
    fallback_summary = f"{meta['title']}（{meta['kind']}，{meta['chars']} 字）\n{text[:300]}"

    if not has_any:
        analysis = {
            "summary": fallback_summary,
            "genre": meta.get("media") or meta["kind"],
            "structure": "",
            "characters": [],
            "style_traits": [],
            "key_facts": [],
            "caution": ["LLM 分析未返回结果，已使用确定性摘要，建议稍后重试"],
            "fallback": True,
        }
    else:
        analysis = {
            "summary": _as_str(raw.get("summary"), 500) or fallback_summary,
            "genre": _as_str(raw.get("genre"), 80) or meta.get("media") or meta["kind"],
            "structure": _as_str(raw.get("structure"), 1200),
            "characters": _as_list(raw.get("characters"), 30),
            "style_traits": _as_list(raw.get("style_traits"), 20),
            "key_facts": _as_list(raw.get("key_facts"), 30),
            "caution": _as_list(raw.get("caution"), 20),
            "fallback": False,
        }

    now = _now()
    try:
        with _META_LOCK:
            fresh = _read_meta(ref_id) or meta
            fresh["analysis"] = analysis
            fresh["analysis_at"] = now
            if analysis.get("summary"):
                fresh["summary"] = _as_str(analysis["summary"], 500)
            _write_meta(ref_id, fresh)
        pdir = _ref_dir(ref_id)
        if pdir:
            atomic_write_json(os.path.join(pdir, "analysis.json"),
                              {"ref_id": ref_id, "analyzed_at": now, "analysis": analysis})
    except Exception:
        return _fail("分析结果保存失败，请重试")
    return {"ok": True, "ref_id": ref_id, "analysis": analysis,
            "summary": analysis["summary"], "fallback": bool(analysis.get("fallback"))}


# ---------------------------------------------------------------- 项目关联

def link_to_project(project_key: str, ref_id: str, usage_mode: str = "facts") -> dict:
    """把参考挂到项目。项目必须真实存在；版权未确认则强制 disabled。"""
    pdir = _project_dir(project_key)
    if not pdir:
        return _fail("项目不存在或名称非法")
    meta = _read_meta(ref_id)
    if not meta:
        return _fail("参考不存在")
    mode = usage_mode if usage_mode in USAGE_MODES else "facts"
    if not meta["copyright_confirmed"]:
        mode = "disabled"
    try:
        with _LINK_LOCK:
            obj = _read_links(pdir)
            found = False
            for it in obj["items"]:
                if it["ref_id"] == ref_id:
                    it["usage_mode"] = mode
                    found = True
                    break
            if not found:
                obj["items"].append({"ref_id": ref_id, "usage_mode": mode,
                                     "added_at": _now()})
            _write_links(pdir, obj)
        with _META_LOCK:
            fresh = _read_meta(ref_id) or meta
            linked = set(fresh.get("linked_projects") or [])
            if project_key not in linked:
                linked.add(project_key)
                fresh["linked_projects"] = sorted(linked)
                _write_meta(ref_id, fresh)
    except Exception:
        return _fail("关联项目失败，请重试")
    return {"ok": True, "project_key": project_key, "ref_id": ref_id,
            "usage_mode": mode, "items": project_refs(project_key)}


def unlink_from_project(project_key: str, ref_id: str) -> dict:
    """取消关联；项目未关联时也返回成功（幂等）。"""
    pdir = _project_dir(project_key)
    if not pdir:
        return _fail("项目不存在或名称非法")
    if not _read_meta(ref_id):
        return _fail("参考不存在")
    try:
        with _LINK_LOCK:
            obj = _read_links(pdir)
            obj["items"] = [it for it in obj["items"] if it["ref_id"] != ref_id]
            _write_links(pdir, obj)
        with _META_LOCK:
            fresh = _read_meta(ref_id)
            if fresh:
                linked = [k for k in (fresh.get("linked_projects") or []) if k != project_key]
                fresh["linked_projects"] = linked
                _write_meta(ref_id, fresh)
    except Exception:
        return _fail("取消关联失败，请重试")
    return {"ok": True, "project_key": project_key, "ref_id": ref_id}


def _legacy_plan_ids(pdir: str) -> list[str]:
    """兼容旧项目：references.json 不存在时读 02 计划的 config.reference_ids 投影。"""
    plan = _read_json(os.path.join(pdir, "02-写作计划.json"))
    if not isinstance(plan, dict):
        return []
    cfg = plan.get("config")
    ids = cfg.get("reference_ids") if isinstance(cfg, dict) else None
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, str) and REF_ID_RE.match(i)]


def project_ref_ids(project_key: str) -> list[str]:
    pdir = _project_dir(project_key)
    if not pdir:
        return []
    if not os.path.isfile(_links_path(pdir)):
        return _legacy_plan_ids(pdir)
    return [it["ref_id"] for it in _read_links(pdir)["items"]]


def project_refs(project_key: str) -> list[dict]:
    """项目关联列表（带标题/体裁/字数）；参考已被删除的条目会标 exists=False。"""
    pdir = _project_dir(project_key)
    if not pdir:
        return []
    if not os.path.isfile(_links_path(pdir)):
        items = [{"ref_id": rid, "usage_mode": "facts", "added_at": ""}
                 for rid in _legacy_plan_ids(pdir)]
    else:
        items = list(_read_links(pdir)["items"])
    out = []
    for it in items:
        meta = _read_meta(it["ref_id"])
        row = dict(it)
        if meta:
            row.update({"title": meta["title"], "kind": meta["kind"],
                        "chars": meta["chars"], "chunks": meta["chunks"],
                        "exists": True})
        else:
            row.update({"title": "（参考已删除）", "kind": "", "chars": 0,
                        "chunks": 0, "exists": False})
        out.append(row)
    return out


# ---------------------------------------------------------------- 检索

_WORD = re.compile(r"[a-z0-9]+")

_ZH_STOP = {
    "的", "了", "在", "是", "和", "有", "我", "你", "他", "她", "它", "这", "那",
    "就", "也", "都", "而", "与", "及", "着", "把", "被", "不", "很", "又", "还",
    "之", "其", "以", "为", "于", "从", "到", "对", "但", "却", "让", "向", "将",
}
_EN_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was", "were",
    "for", "on", "with", "as", "at", "by", "it", "this", "that", "be", "from",
    "we", "you", "they", "he", "she", "but", "not", "have", "has", "had", "will",
}
_STOPWORDS = _ZH_STOP | _EN_STOP


def _tokens(text: str) -> dict[str, int]:
    """中文：单字 + 字 bigram；英文：小写词。停用词剔除，返回词频。"""
    t = (text or "").lower()
    counts: dict[str, int] = {}
    for w in _WORD.findall(t):
        if w in _EN_STOP:
            continue
        counts[w] = counts.get(w, 0) + 1
    for m in re.finditer(r"[\u4e00-\u9fff]+", t):
        run = m.group(0)
        for ch in run:
            if ch not in _ZH_STOP:
                counts[ch] = counts.get(ch, 0) + 1
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            counts[bg] = counts.get(bg, 0) + 1
    return counts


def _snippet(text: str, q_terms: list[str], width: int = 240) -> str:
    """以命中词为中心截 ~240 字，尽量落在段落边界。"""
    if not text:
        return ""
    low = text.lower()
    best = -1
    for term in sorted(q_terms, key=lambda x: (-len(x), x)):
        pos = low.find(term.lower())
        if pos >= 0 and (best < 0 or pos < best):
            best = pos
    if best < 0:
        return text[:width].strip()
    start = max(0, best - width // 3)
    end = min(len(text), start + width)
    nl = text.rfind("\n", 0, start)
    if nl >= 0 and start - nl <= 80:
        start = nl + 1
    nl2 = text.find("\n", end)
    if nl2 >= 0 and nl2 - end <= 80:
        end = nl2
    return text[start:end].strip()


def _load_index(ref_id: str) -> dict | None:
    """懒加载并缓存单个参考的切片/词频；用 mtime+size 失效，并发只读。"""
    pdir = _ref_dir(ref_id)
    if not pdir:
        return None
    mp = os.path.join(pdir, "meta.json")
    cp = os.path.join(pdir, "chunks.jsonl")
    msig, csig = _sig(mp), _sig(cp)
    if msig is None or csig is None:
        return None
    with _INDEX_LOCK:
        hit = _INDEX.get(ref_id)
        if hit and hit["sig"] == (msig, csig):
            return hit
    meta = _read_json(mp)
    if not isinstance(meta, dict):
        return None
    chunks: list[dict] = []
    try:
        with open(cp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                body = str(obj.get("text") or "")
                tf = _tokens(body)
                if not body or not tf:
                    continue
                chunks.append({
                    "no": int(obj.get("no") or len(chunks) + 1),
                    "text": body,
                    "tf": tf,
                    "dl": sum(tf.values()),
                    "headings": list(obj.get("headings") or []),
                })
    except OSError:
        return None
    terms: set[str] = set()
    for c in chunks:
        terms.update(c["tf"].keys())
    entry = {"sig": (msig, csig), "meta": meta, "chunks": chunks, "terms": terms}
    with _INDEX_LOCK:
        _INDEX[ref_id] = entry
    return entry


def _selected_ids(ref_ids) -> list[str]:
    all_ids = _all_ref_ids()
    if ref_ids is None or (isinstance(ref_ids, (list, tuple, set)) and len(ref_ids) == 0):
        return all_ids
    if isinstance(ref_ids, str):
        ref_ids = [ref_ids]
    seen = set()
    out = []
    for rid in ref_ids:
        if isinstance(rid, str) and REF_ID_RE.match(rid) and rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def _retrieve(query: str, ref_ids=None, k: int = 6, max_chars: int = 6000) -> list[dict]:
    q = _tokens(query)
    if not q:
        return []
    q_terms = list(q.keys())
    qset = set(q_terms)
    try:
        k = max(1, int(k))
    except (TypeError, ValueError):
        k = 6
    try:
        max_chars = max(0, int(max_chars))
    except (TypeError, ValueError):
        max_chars = 6000
    if max_chars <= 0:
        return []

    # 先按「ref 级词表」筛候选，避免每次查询扫描全库切片。
    candidates = []
    for order, rid in enumerate(_selected_ids(ref_ids)):
        entry = _load_index(rid)
        if not entry:
            continue
        meta = entry["meta"]
        if not meta.get("copyright_confirmed", True):
            continue
        if (meta.get("usage_mode") or "facts") == "disabled":
            continue
        if not (entry["terms"] & qset):
            continue
        candidates.append((order, rid, entry))
    if not candidates:
        return []

    docs = []
    total_chunks = 0
    total_len = 0
    for order, rid, entry in candidates:
        for ch in entry["chunks"]:
            total_chunks += 1
            total_len += ch["dl"]
            tfq = {t: ch["tf"][t] for t in q_terms if t in ch["tf"]}
            if tfq:
                docs.append((order, rid, entry, ch, tfq))
    if not docs:
        return []
    avgdl = (total_len / total_chunks) if total_chunks else 1.0
    df: dict[str, int] = {}
    for _o, _r, _e, _c, tfq in docs:
        for t in tfq:
            df[t] = df.get(t, 0) + 1
    n_docs = max(1, total_chunks)
    k1, b = 1.2, 0.75
    scored = []
    for order, rid, entry, ch, tfq in docs:
        score = 0.0
        norm = k1 * (1 - b + b * (ch["dl"] / (avgdl or 1.0)))
        for t, tf in tfq.items():
            d = df.get(t, 0)
            idf = math.log(1 + (n_docs - d + 0.5) / (d + 0.5))
            score += idf * (tf * (k1 + 1)) / (tf + norm)
        if score <= 0:
            continue
        scored.append((score, order, ch["no"], rid, entry, ch))
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))

    out: list[dict] = []
    used = 0
    for score, _order, _no, rid, entry, ch in scored:
        if len(out) >= k:
            break
        remaining = max_chars - used
        if remaining < 40:
            break
        sn = _snippet(ch["text"], q_terms)
        if len(sn) > remaining:
            sn = sn[:remaining]
        mode = entry["meta"].get("usage_mode") or "facts"
        out.append({
            "ref_id": rid,
            "title": entry["meta"].get("title") or "未命名参考",
            "chunk_no": ch["no"],
            "snippet": sn,
            "score": round(score, 4),
            "usage_mode": mode,
        })
        used += len(sn)
    return out


def retrieve(query: str, ref_ids=None, k: int = 6, max_chars: int = 6000) -> list[dict]:
    """纯 Python BM25 检索；disabled/未确认版权的参考永不返回。"""
    try:
        return _retrieve(query, ref_ids=ref_ids, k=k, max_chars=max_chars)
    except Exception:
        return []


_RULES = (
    "【参考资料使用规则】\n"
    "1. 只吸收事实、结构与风格，不得连续复制原文 12 字以上；引用须标注来源。\n"
    "2. 与用户设定冲突时，以用户设定为准。\n"
    "3. usage_mode=style 只提炼风格特征，不提供原文情节；structure 只给结构骨架；"
    "facts 只给事实；adapt 可改编但须标注来源。\n"
)


def _mode_body(mode: str, ref_meta: dict, snippet: str) -> str:
    analysis = ref_meta.get("analysis") or {}
    if mode == "style":
        traits = [str(x).strip() for x in (analysis.get("style_traits") or []) if str(x).strip()]
        if traits:
            return "风格特征（仅借鉴写法，不复用情节）：" + "；".join(traits)[:500]
        return "（仅借鉴其句式、节奏、意象与用词，不复用情节与原文）"
    if mode == "structure":
        struct = str(analysis.get("structure") or "").strip()
        if struct:
            return "结构骨架（仅借鉴组织方式）：" + struct[:700]
        return "（按该片段的组织顺序借鉴结构，不复用情节与原文）"
    if mode == "facts":
        return snippet
    if mode == "adapt":
        return snippet + f"\n（改编自《{ref_meta.get('title') or '参考'}》，须标注来源）"
    return ""


def _truncate_paragraphs(text: str, limit: int) -> str:
    """优先在空行处截断，实在没有段落边界才硬截；结果严格 ≤ limit。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n\n", 0, limit)
    if cut < int(limit * 0.6):
        cut = text.rfind("\n", 0, limit)
    if cut < int(limit * 0.6):
        cut = limit
    return text[:cut]


def context_block(query: str, ref_ids, max_chars: int = 4000, k: int = 6) -> str:
    """构造写作 prompt 的参考块：带引用标记、用法规则，长度严格 ≤ max_chars。"""
    try:
        max_chars = max(0, int(max_chars))
    except (TypeError, ValueError):
        max_chars = 4000
    if max_chars <= 0:
        return ""
    # 显式空列表表示「本项目没有关联参考」：不能退化成检索全库。
    if ref_ids is not None and isinstance(ref_ids, (list, tuple, set)) and len(ref_ids) == 0:
        return ""
    # 先扣掉规则头，避免检索把预算吃光后连一条引用都放不下。
    hits = retrieve(query, ref_ids=ref_ids, k=k, max_chars=max(0, max_chars - len(_RULES)))
    if not hits:
        return ""
    metas: dict[str, dict] = {}
    parts = [_RULES]
    used = len(_RULES)
    for h in hits:
        rid = h["ref_id"]
        if rid not in metas:
            metas[rid] = _read_meta(rid) or {}
        meta = metas[rid]
        if not meta.get("copyright_confirmed", True) or h["usage_mode"] == "disabled":
            continue
        body = _mode_body(h["usage_mode"], meta, h["snippet"])
        if not body:
            continue
        block = f"[参考：{h['title']}#{h['chunk_no']}]\n（用法：{h['usage_mode']}）\n{body}\n"
        remaining = max_chars - used
        if remaining <= len(f"[参考：{h['title']}#{h['chunk_no']}]\n"):
            break
        if len(block) > remaining:
            block = block[:remaining]
        parts.append(block)
        used += len(block)
        if used >= max_chars:
            break
    out = "\n".join(parts)
    return _truncate_paragraphs(out, max_chars)


# ---------------------------------------------------------------- 查重

_OV_TOKEN = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")
_OV_BASE = 1000003
_OV_MASK = (1 << 64) - 1


def _ov_tokens(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    toks: list[str] = []
    spans: list[tuple[int, int]] = []
    for m in _OV_TOKEN.finditer(text or ""):
        s = m.group(0)
        if s.isascii():
            s = s.lower()
            if len(s) == 1 and s.isalpha():
                continue  # 单个字母（变量名/序号）没有查重意义
        toks.append(s)
        spans.append((m.start(), m.end()))
    return toks, spans


def _win_hashes(ids: list[int], n: int):
    if len(ids) < n:
        return
    base = _OV_BASE
    powb = pow(base, n - 1, 1 << 64)
    h = 0
    for k in range(n):
        h = (h * base + ids[k]) & _OV_MASK
    yield h
    for i in range(n, len(ids)):
        h = ((h - ids[i - n] * powb) * base + ids[i]) & _OV_MASK
        yield h


def _hash_at(ids: list[int], i: int, n: int) -> int:
    h = 0
    for k in range(i, i + n):
        h = (h * _OV_BASE + ids[k]) & _OV_MASK
    return h


def _union_len(spans: list[tuple[int, int]]) -> int:
    if not spans:
        return 0
    spans = sorted(spans)
    total = 0
    cur_s, cur_e = spans[0]
    for s, e in spans[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    return total + cur_e - cur_s


def overlap_check(text: str, ref_ids=None, n: int = 12, min_hits: int = 1) -> dict:
    """检测生成文本与参考的连续 n 字/词重合；只报告，不改写。

    中文按字、英文按词；n 至少 10，并过滤停用词占比过高的窗口（常见套话/成语），
    避免把「他不知道该说什么」这类通用句误报成抄袭。
    """
    base = {"ok": True, "ratio": 0.0, "match_count": 0, "matches": [],
            "note": "", "n": 12}
    try:
        text = str(text or "")
        try:
            n = max(10, min(int(n or 12), 80))
        except (TypeError, ValueError):
            n = 12
        base["n"] = n
        toks, spans = _ov_tokens(text)
        if len(toks) < n:
            base["note"] = f"文本不足 {n} 个有效字/词，未发现连续重合"
            return base

        ids_map: dict[str, int] = {}
        text_ids = [ids_map.setdefault(t, len(ids_map)) for t in toks]
        text_map: dict[int, list[int]] = {}
        for i, h in enumerate(_win_hashes(text_ids, n)):
            bucket = text_map.setdefault(h, [])
            if len(bucket) < 8:
                bucket.append(i)

        selected = _selected_ids(ref_ids)
        hits: dict[tuple[str, int], dict] = {}
        marked: list[tuple[int, int]] = []
        order_map = {rid: i for i, rid in enumerate(selected)}
        for rid in selected:
            entry = _load_index(rid)
            if not entry:
                continue
            meta = entry["meta"]
            if not meta.get("copyright_confirmed", True):
                continue
            if (meta.get("usage_mode") or "facts") == "disabled":
                continue
            title = meta.get("title") or "未命名参考"
            for ch in entry["chunks"]:
                rt, rs = _ov_tokens(ch["text"])
                if len(rt) < n:
                    continue
                rids = [ids_map.setdefault(t, len(ids_map)) for t in rt]
                i = 0
                powb = pow(_OV_BASE, n - 1, 1 << 64)
                h = _hash_at(rids, 0, n) if len(rids) >= n else 0
                while i + n <= len(rids):
                    positions = text_map.get(h)
                    matched = False
                    if positions:
                        window = rids[i:i + n]
                        info = [t for t in rt[i:i + n]
                                if t not in _STOPWORDS and (not t.isascii() or len(t) > 1)]
                        # 通用套话过滤：非停用词过半，且至少 3 个不同实词。
                        if len(info) / n >= 0.5 and len(set(info)) >= 3:
                            for p in positions:
                                if text_ids[p:p + n] == window:
                                    marked.append((spans[p][0], spans[p + n - 1][1]))
                                    key = (rid, ch["no"])
                                    rec = hits.get(key)
                                    if rec is None:
                                        rec = {"ref_id": rid, "title": title,
                                               "chunk_no": ch["no"], "excerpt": "",
                                               "length": 0}
                                        hits[key] = rec
                                    core_s, core_e = rs[i][0], rs[i + n - 1][1]
                                    if core_e - core_s > rec["length"]:
                                        rec["length"] = core_e - core_s
                                        ex_s = max(0, core_s - 20)
                                        ex_e = min(len(ch["text"]), core_e + 20)
                                        ex = ch["text"][ex_s:ex_e].strip()
                                        rec["excerpt"] = ex[:200] + ("…" if len(ex) > 200 else "")
                                    matched = True
                    if matched:
                        i += n
                        if i + n <= len(rids):
                            h = _hash_at(rids, i, n)
                    else:
                        if i + n < len(rids):
                            h = ((h - rids[i] * powb) * _OV_BASE + rids[i + n]) & _OV_MASK
                        i += 1

        matches = sorted(hits.values(),
                         key=lambda x: (order_map.get(x["ref_id"], 99999), x["chunk_no"]))
        matched_chars = _union_len(marked)
        ratio = min(1.0, matched_chars / max(1, len(text)))
        try:
            min_hits = max(1, int(min_hits or 1))
        except (TypeError, ValueError):
            min_hits = 1
        ok = len(matches) < min_hits
        if matches:
            note = (f"发现 {len(matches)} 处连续 {n} 字/词重合（覆盖 {matched_chars} 字，"
                    f"占比 {ratio:.1%}），建议改写；本检查只报告，未改动文本。")
        else:
            note = f"未发现连续 {n} 字/词重合。"
        return {"ok": ok, "ratio": round(ratio, 4), "match_count": len(matches),
                "matches": matches, "note": note, "n": n}
    except Exception:
        base["note"] = "查重执行失败，请重试"
        return base
