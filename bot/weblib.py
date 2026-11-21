"""书架升级 HTTP 接口（v5.3.0）：作品封面 + 自定义分组（docs/系统化升级方案 7.1，全部自研）。

用 FastAPI APIRouter 收口，**不改 app.py 的既有内容**——主会话在 app.py 认证中间件之后、
``@app.get("/api/library/{key}")`` 等路由注册**之前**加两行挂载（与 webai/webstats 同位置，
放在 webstats 之后即可。必须在其之前：GET /api/library/groups、GET/DELETE /api/library/cover
是单段路径，若挂在 /api/library/{key} 之后会被先注册的通配路由遮住）：

    from bot import weblib
    app.include_router(weblib.router)

接口契约（与 static/v2/js/views/library.js 对齐）：
  GET    /api/library/groups                 分组列表 + 作品归属映射
  POST   /api/library/groups                 分组 CRUD：{action: create|rename|delete|reorder, ...}
  POST   /api/library/assign-group           移动作品到分组：{key, groupId|null}
  POST   /api/library/cover                  上传封面（multipart: key+file，或 JSON: key+dataUrl/base64）
  DELETE /api/library/cover?key=             移除封面
  GET    /api/library/cover?key=[&v=]        读取封面图片（无封面 404，前端回退字形占位）
  GET    /api/library/intros                 各作品「一句话简介」简介搜索用（前端本地搜索字段）

边界约定：
  · 回收站/归档**不在这里**——沿用既有 trashed_at/archived_at 生命周期与
    /api/library/{key}/trash|restore|archive|purge 端点（不发明第二套软删机制）；
  · 分组/封面是附加能力：任何失败只报错，绝不影响作品列表主链路；
  · data/book-groups.json 与封面文件都是运行时用户数据（/data/ 已 gitignore，封面在
    projects/<key>/ 下也不入库）。
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import tempfile
import threading
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from .paths import data
from .safeio import atomic_write_json
from . import writer

router = APIRouter()

# ---------------------------------------------------------------- 分组存储（data/book-groups.json）

# tests 里 monkeypatch 本常量即可整体隔离
DATA_FILE = data("book-groups.json")

MAX_GROUPS = 50          # 分组数量上限（防脏数据无限膨胀）
NAME_MAX = 30            # 分组名长度上限（字符）
KEY_MAX = 200            # 作品 key 长度上限

ID_RE = re.compile(r"^g-[0-9a-f]{4,32}$")

_LOCK_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: str) -> threading.RLock:
    with _LOCK_GUARD:
        lk = _LOCKS.get(path)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[path] = lk
        return lk


@contextlib.contextmanager
def _locked():
    """进程内 RLock + 跨进程 fcntl 文件锁（fcntl 不可用退化为纯线程锁），对齐 bot/writestats.py。"""
    with _lock_for(DATA_FILE):
        fh = None
        try:
            try:
                import fcntl  # POSIX only
            except ImportError:
                fcntl = None
            if fcntl is not None:
                fh = open(DATA_FILE + ".lock", "a+", encoding="utf-8")
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fh is not None:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    fh.close()
                except Exception:
                    pass


def _empty() -> dict:
    return {"version": 1, "groups": [], "assign": {}, "updated": ""}


def _clean_name(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:NAME_MAX]


def _sanitize(raw) -> dict:
    """读进来的数据做形状修复：坏字段一律丢弃，绝不抛错（分组是附加能力）。"""
    d = _empty()
    if not isinstance(raw, dict):
        return d
    groups = []
    seen_ids = set()
    for g in (raw.get("groups") or [])[:MAX_GROUPS]:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "")
        name = _clean_name(g.get("name"))
        if not gid or gid in seen_ids or not name:
            continue
        try:
            order = int(g.get("order"))
        except (TypeError, ValueError):
            order = len(groups) + 1
        groups.append({"id": gid, "name": name, "order": order})
        seen_ids.add(gid)
    assign = {}
    valid = seen_ids
    for k, v in (raw.get("assign") or {}).items():
        if isinstance(k, str) and 0 < len(k) <= KEY_MAX and str(v) in valid:
            assign[k[:KEY_MAX]] = str(v)
    d["groups"] = sorted(groups, key=lambda x: (x["order"], x["id"]))
    d["assign"] = assign
    return d


def load() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return _empty()
    return _sanitize(raw)


def save(d: dict) -> None:
    d["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    atomic_write_json(DATA_FILE, d)


def _next_id() -> str:
    return f"g-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------- 分组操作（纯逻辑，HTTP 与测试共用）

def list_groups() -> dict:
    """分组列表 + 归属映射。groups 按 order 升序；assign 缺席 = 未分组。"""
    with _locked():
        d = load()
        return {"ok": True,
                "groups": [dict(g) for g in d["groups"]],
                "assign": dict(d["assign"])}


def create_group(name: str) -> dict:
    raw = str(name or "").strip()
    if not raw:
        return {"ok": False, "note": "分组名不能为空"}
    if len(raw) > NAME_MAX:
        return {"ok": False, "note": f"分组名最长 {NAME_MAX} 个字"}
    name = _clean_name(raw)
    with _locked():
        d = load()
        if len(d["groups"]) >= MAX_GROUPS:
            return {"ok": False, "note": f"分组最多 {MAX_GROUPS} 个，请先清理不用的分组"}
        if any(g["name"] == name for g in d["groups"]):
            return {"ok": False, "note": f"已有同名分组「{name}」"}
        nxt = max([g["order"] for g in d["groups"]] or [0]) + 1
        g = {"id": _next_id(), "name": name, "order": nxt}
        d["groups"].append(g)
        save(d)
        return {"ok": True, "group": g}


def rename_group(gid: str, name: str) -> dict:
    gid = str(gid or "").strip()
    raw = str(name or "").strip()
    if not raw:
        return {"ok": False, "note": "分组名不能为空"}
    if len(raw) > NAME_MAX:
        return {"ok": False, "note": f"分组名最长 {NAME_MAX} 个字"}
    name = _clean_name(raw)
    with _locked():
        d = load()
        for g in d["groups"]:
            if g["id"] == gid:
                if any(o["id"] != gid and o["name"] == name for o in d["groups"]):
                    return {"ok": False, "note": f"已有同名分组「{name}」"}
                g["name"] = name
                save(d)
                return {"ok": True, "group": dict(g)}
        return {"ok": False, "note": "分组不存在或已被删除"}


def delete_group(gid: str) -> dict:
    """删除分组：组内作品自动回到「未分组」（assign 里的映射一并清掉）。"""
    gid = str(gid or "").strip()
    with _locked():
        d = load()
        keep = [g for g in d["groups"] if g["id"] != gid]
        if len(keep) == len(d["groups"]):
            return {"ok": False, "note": "分组不存在或已被删除"}
        d["groups"] = keep
        d["assign"] = {k: v for k, v in d["assign"].items() if v != gid}
        save(d)
        return {"ok": True}


def reorder_groups(ids) -> dict:
    """按给定 id 顺序重排分组（未知 id 忽略；没点到的分组排在其后，保持原相对顺序）。"""
    order_ids = [str(x) for x in (ids or []) if isinstance(x, (str, int))]
    with _locked():
        d = load()
        rank = {gid: i for i, gid in enumerate(order_ids)}
        rest = [g for g in d["groups"] if g["id"] not in rank]
        hit = [g for g in d["groups"] if g["id"] in rank]
        hit.sort(key=lambda g: rank[g["id"]])
        new = hit + rest
        for i, g in enumerate(new):
            g["order"] = i + 1
        d["groups"] = new
        save(d)
        return {"ok": True, "groups": [dict(g) for g in new]}


def _project_exists(key: str) -> bool:
    """作品是否真实存在（key 同时过 safe_join 防路径逃逸）。"""
    try:
        pdir = writer._project_dir(key)
    except Exception:
        return False
    if not pdir or not os.path.isdir(pdir):
        return False
    return bool(writer.library_item(key))


def assign_group(key: str, group_id) -> dict:
    """把作品移进分组；group_id=None/空 表示移出分组（回到未分组）。"""
    key = str(key or "").strip()[:KEY_MAX]
    if not key:
        return {"ok": False, "note": "缺少作品 key"}
    if not _project_exists(key):
        return {"ok": False, "note": "作品不存在或已被删除"}
    with _locked():
        d = load()
        if group_id is None or str(group_id).strip() == "":
            removed = d["assign"].pop(key, None)
            save(d)
            return {"ok": True, "key": key, "groupId": None, "removed": removed}
        gid = str(group_id).strip()
        if not any(g["id"] == gid for g in d["groups"]):
            return {"ok": False, "note": "目标分组不存在或已被删除"}
        d["assign"][key] = gid
        save(d)
        return {"ok": True, "key": key, "groupId": gid}


# ---------------------------------------------------------------- 封面（projects/<key>/cover.png|jpg|webp）

COVER_MAX_BYTES = 5 * 1024 * 1024   # 5MB 上限
COVER_STEMS = ("cover.png", "cover.jpg", "cover.webp", "cover.jpeg", "cover.gif", "cover.bmp")
COVER_MEDIA = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def sniff_image(data: bytes) -> str | None:
    """按文件头（magic bytes）判图型：png / jpg / webp；其余不认——扩展名可伪造，内容不会。"""
    if not isinstance(data, bytes) or len(data) < 12:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _cover_dir(key: str) -> str | None:
    """作品目录（key 过 safe_join 防逃逸）；作品不存在返回 None。"""
    try:
        pdir = writer._project_dir(key)
    except Exception:
        return None
    if not pdir or not os.path.isdir(pdir):
        return None
    return pdir


def cover_file(key: str) -> tuple[str, str] | None:
    """已存在封面 → (路径, 扩展名)；没有封面 → None。"""
    pdir = _cover_dir(key)
    if not pdir:
        return None
    for stem in COVER_STEMS:
        p = os.path.join(pdir, stem)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            ext = os.path.splitext(stem)[1].lstrip(".")
            if ext == "jpeg":
                ext = "jpg"
            return p, ext
    return None


def _remove_other_covers(pdir: str, keep: str | None = None) -> int:
    """清掉目录里的旧封面（写入新封面后调用，keep=新封面文件名）。"""
    n = 0
    try:
        for name in os.listdir(pdir):
            if name == keep or name not in COVER_STEMS:
                continue
            try:
                os.unlink(os.path.join(pdir, name))
                n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """二进制原子写：同目录临时文件 + os.replace，断电不留半个文件。"""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cover-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_cover(key: str, data: bytes) -> dict:
    """写入封面（≤5MB、按文件头校验图型），并清理旧封面。返回新封面信息。"""
    key = str(key or "").strip()[:KEY_MAX]
    if not key:
        return {"ok": False, "note": "缺少作品 key"}
    if not isinstance(data, bytes) or not data:
        return {"ok": False, "note": "没有读到封面内容"}
    if len(data) > COVER_MAX_BYTES:
        return {"ok": False, "note": "封面图片超过 5MB，请压缩后再上传"}
    ext = sniff_image(data)
    if not ext:
        return {"ok": False, "note": "封面只支持 PNG / JPG / WebP 图片（按文件内容校验，改后缀没用）"}
    pdir = _cover_dir(key)
    if not pdir:
        return {"ok": False, "note": "作品不存在或已被删除"}
    target = os.path.join(pdir, f"cover.{ext}")
    _atomic_write_bytes(target, data)
    removed = _remove_other_covers(pdir, keep=f"cover.{ext}")
    try:
        mtime = int(os.path.getmtime(target))
    except OSError:
        mtime = 0
    return {"ok": True, "key": key, "cover": f"cover.{ext}", "mtime": mtime,
            "removed_old": removed, "size": len(data)}


def remove_cover(key: str) -> dict:
    key = str(key or "").strip()[:KEY_MAX]
    pdir = _cover_dir(key)
    if not pdir:
        return {"ok": False, "note": "作品不存在或已被删除"}
    removed = _remove_other_covers(pdir)
    return {"ok": True, "key": key, "removed": removed}


def _decode_data_url(raw: str) -> bytes:
    """解析 base64 / dataUrl（data:image/png;base64,xxxx）。"""
    import base64
    s = str(raw or "").strip()
    m = re.match(r"^data:[^;]+;base64,(.*)$", s, re.S)
    if m:
        s = m.group(1)
    s = re.sub(r"\s+", "", s)
    return base64.b64decode(s, validate=False)


def _bad(message: str, status: int = 400):
    return JSONResponse({"ok": False, "error": str(message), "detail": str(message)}, status_code=status)


def _cover_url(info: dict) -> str:
    return f"/api/library/cover?key={_q(info.get('key'))}&v={info.get('mtime') or 0}"


def _q(v) -> str:
    from urllib.parse import quote
    return quote(str(v or ""), safe="")


# ---------------------------------------------------------------- 各作品「一句话简介」（本地搜索字段）

INTRO_MAX = 160


def book_intros() -> dict:
    """{key: 简介}：读各作品计划 config.idea（一句话起点），退而取 config.theme。
    只读不写，读挂了跳过该作品——简介搜索是锦上添花，不能拖累作品库。"""
    out = {}
    try:
        root = writer.PROJECTS_DIR
        names = writer._project_dirs()   # 注意：这里返回的是目录名（= key），不是绝对路径
    except Exception:
        return out
    for name in names:
        try:
            _, _, plan = writer._plan_of(os.path.join(root, name))
            if not plan:
                continue
            cfg = plan.get("config") or {}
            text = str(cfg.get("idea") or cfg.get("theme") or "").strip()
            if text:
                out[name[:KEY_MAX]] = text[:INTRO_MAX]
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- HTTP 端点

@router.get("/api/library/groups")
def api_groups():
    return list_groups()


@router.post("/api/library/groups")
def api_groups_post(payload: dict):
    """分组 CRUD。body: {action: create|rename|delete|reorder, ...}"""
    p = payload if isinstance(payload, dict) else {}
    act = str(p.get("action") or "").strip()
    if act == "create":
        res = create_group(p.get("name"))
    elif act == "rename":
        res = rename_group(p.get("id"), p.get("name"))
    elif act == "delete":
        res = delete_group(p.get("id"))
    elif act == "reorder":
        res = reorder_groups(p.get("ids") or p.get("order"))
    else:
        return _bad("未知操作，action 只支持 create / rename / delete / reorder")
    if not res.get("ok"):
        return _bad(str(res.get("note") or "分组操作失败"))
    return res


@router.post("/api/library/assign-group")
def api_assign_group(payload: dict):
    """移动作品到分组。body: {key, groupId|null}（groupId 为空 = 移出分组）。"""
    p = payload if isinstance(payload, dict) else {}
    res = assign_group(p.get("key"), p.get("groupId"))
    if not res.get("ok"):
        return _bad(str(res.get("note") or "移动分组失败"))
    return res


@router.get("/api/library/intros")
def api_intros():
    return {"ok": True, "intros": book_intros()}


@router.get("/api/library/cover")
def api_cover_get(key: str = "", v: str = ""):
    """读取封面图片。v 是前端加的时间戳（防缓存），服务端忽略。
    无封面返回 404，前端 <img onerror> 回退到字形占位。"""
    hit = cover_file(key)
    if not hit:
        return JSONResponse({"ok": False, "error": "该作品还没有封面", "detail": "该作品还没有封面"}, status_code=404)
    path, ext = hit
    return FileResponse(path, media_type=COVER_MEDIA.get(ext, "application/octet-stream"),
                        headers={"Cache-Control": "no-cache"})


@router.delete("/api/library/cover")
def api_cover_delete(key: str = ""):
    res = remove_cover(key)
    if not res.get("ok"):
        return _bad(str(res.get("note") or "移除封面失败"))
    return res


@router.post("/api/library/cover")
async def api_cover_post(request: Request):
    """上传封面。两种形态：
      · multipart/form-data：字段 key + file（input file 即时预览走这条）；
      · application/json：{key, dataUrl|base64}。
    写 projects/<key>/cover.<png|jpg|webp>，同目录旧封面一律清掉。"""
    ctype = (request.headers.get("content-type") or "").lower()
    key = ""
    data = b""
    if ctype.startswith("application/json"):
        try:
            p = await request.json()
        except Exception:
            return _bad("请求体不是合法 JSON")
        p = p if isinstance(p, dict) else {}
        key = str(p.get("key") or "")
        raw = p.get("dataUrl") or p.get("base64")
        if raw is None:
            return _bad("缺少封面数据（dataUrl/base64）")
        try:
            data = _decode_data_url(raw)
        except Exception:
            return _bad("封面数据解码失败，请重新选择图片")
    else:
        try:
            form = await request.form()
        except Exception:
            return _bad("上传组件未就绪（需要 python-multipart）", 503)
        key = str(form.get("key") or "")
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            return _bad("缺少封面文件")
        try:
            data = await up.read(COVER_MAX_BYTES + 1)
        except Exception as e:
            return _bad(f"读取上传文件失败：{e}")
    res = save_cover(key, data)
    if not res.get("ok"):
        over = "超过 5MB" in str(res.get("note") or "")
        return _bad(str(res.get("note") or "封面上传失败"), 413 if over else 400)
    return {"ok": True, **res, "url": _cover_url(res)}
