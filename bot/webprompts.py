"""提示词开放编辑 HTTP 接口（v5.3.0，方案 docs/系统化升级方案-v5.1 §7.2）。

用 FastAPI APIRouter 收口，**不改 app.py 的既有内容**——主会话只需在 app.py 里加两行挂载：

    from bot import webprompts
    app.include_router(webprompts.router)

接口契约（与 static/v2/js/views/promptedit.js 对齐）：
  GET    /api/prompts                 组/槽清单：当前生效文本+温度+指纹状态+变量列表+徽章判定所需字段
  GET    /api/prompts/file?group=X    原始 md（用户层优先，无用户文件回落内置层；layer 标明来源）
  POST   /api/prompts/file            保存用户层 md（校验槽名存在于内置/默认；frontmatter 归一）
  DELETE /api/prompts/file?group=X    恢复默认 = 删用户覆盖文件
  POST   /api/prompts/adopt           采用新版默认 = 把用户文件 base 指纹重写为当前指纹（保留用户文本）

红线：本路由只读写 prompts 两层文件，不触碰任何出口闸门（purity/写前写后/交付闸门）——
用户把提示词改坏时，产出仍由既有闸门兜底（见 tests/test_v53_prompts.py）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import prompts_store

router = APIRouter()


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=str(message))


def _group_payload(group: str) -> dict:
    """单组的完整编辑态：槽清单 + 生效文本 + 温度 + 指纹 + 徽章判定字段。"""
    meta = prompts_store.group_meta(group) or {"label": "", "desc": "", "slots": {}}
    builtin = prompts_store.builtin_parsed(group) or {}
    user = prompts_store.user_parsed(group)
    # 显示名：代码注册值优先；未注册组（如文档型组）回落内置 md frontmatter
    if not meta.get("label"):
        meta["label"] = builtin.get("label") or group
    fp = prompts_store.fingerprint(group)
    user_base = (user or {}).get("base") or ""
    user_raw = prompts_store.user_md_raw(group)
    slots = []
    names = list((meta.get("slots") or {}).keys())
    for name in sorted(set(names) | set((user or {}).get("slots") or {})):
        reg = (meta.get("slots") or {}).get(name) or {}
        uent = (user or {}).get("slots", {}).get(name)
        bent = (builtin.get("slots") or {}).get(name)
        default_text = reg.get("default")
        if default_text is None:
            default_text = (bent or {}).get("text")
        # 生效文本：用户层 → 内置层 → 注册默认（与 prompts_store.text 三层一致）
        if uent and uent.get("text"):
            effective, source = uent["text"], "user"
        elif bent and bent.get("text"):
            effective, source = bent["text"], "builtin"
        else:
            effective, source = default_text, "code"
        eff_temp = None
        if uent and uent.get("temp") is not None:
            eff_temp = uent["temp"]
        elif bent and bent.get("temp") is not None:
            eff_temp = bent["temp"]
        elif reg.get("temp") is not None:
            eff_temp = reg["temp"]
        vars_ = list(reg.get("vars") or [])
        if not vars_ and effective:
            vars_ = prompts_store.slot_vars(effective)
        slots.append({
            "slot": name,
            "label": reg.get("label") or name,
            "desc": reg.get("desc") or "",
            "vars": vars_,
            "temp": eff_temp,
            "temp_default": reg.get("temp"),
            "text": effective,             # 当前生效文本
            "default_text": default_text,  # 当前代码默认（对比视图「新默认」侧）
            "user_text": (uent or {}).get("text"),  # 用户版原文（对比视图「用户版」侧）
            "source": source,
            "modified": bool(uent and default_text is not None and uent.get("text") != default_text),
        })
    return {
        "id": group,
        "label": meta.get("label") or group,
        "desc": meta.get("desc") or "",
        "slots": slots,
        "fingerprint": fp,
        "has_user_file": user_raw is not None,
        "user_base": user_base,
        # 「默认值已更新」：用户文件还钉在旧指纹上，而代码默认已换代
        "default_updated": bool(user_raw is not None and fp and user_base and user_base != fp),
    }


@router.get("/api/prompts")
def api_prompts_list():
    """全部组/槽清单（接线模块 import 触发注册，保证指纹可见）。"""
    from . import selection_ai, ghost, workflow, longform, final_review  # noqa: F401  注册副作用
    groups = []
    for gid in prompts_store.groups():
        try:
            groups.append(_group_payload(gid))
        except Exception as err:
            raise _bad(f"组「{gid}」读取失败：{err}") from err
    return {"ok": True, "groups": groups}


@router.get("/api/prompts/file")
def api_prompts_file(group: str):
    """原始 md：用户层优先（编辑的是用户层），无用户文件回落内置层。"""
    group = str(group or "").strip()
    if not group or "/" in group or "\\" in group or ".." in group:
        raise _bad("非法组名")
    user_raw = prompts_store.user_md_raw(group)
    if user_raw is not None:
        return {"ok": True, "group": group, "layer": "user", "raw": user_raw}
    builtin_raw = prompts_store.builtin_md_raw(group)
    if builtin_raw is None:
        raise HTTPException(status_code=404, detail=f"组「{group}」不存在")
    return {"ok": True, "group": group, "layer": "builtin", "raw": builtin_raw}


@router.post("/api/prompts/file")
def api_prompts_file_save(payload: dict):
    """保存用户层 md。body: {group, raw}——槽名必须存在于内置/默认；frontmatter 归一后落盘。"""
    p = payload if isinstance(payload, dict) else {}
    group = str(p.get("group") or "").strip()
    if not group or "/" in group or "\\" in group or ".." in group:
        raise _bad("非法组名")
    raw = p.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        raise _bad("md 内容为空")
    res = prompts_store.save_user_md(group, raw)
    if not res.get("ok"):
        raise _bad("；".join(res.get("errors") or ["保存失败"]))
    return {"ok": True, "group": group, "base": res.get("base"), "path": res.get("path")}


@router.delete("/api/prompts/file")
def api_prompts_file_delete(group: str):
    """恢复默认：删除用户覆盖文件。"""
    group = str(group or "").strip()
    if not group or "/" in group or "\\" in group or ".." in group:
        raise _bad("非法组名")
    ok = prompts_store.delete_user_md(group)
    if not ok:
        raise HTTPException(status_code=404, detail="该组没有用户覆盖文件，无需恢复")
    return {"ok": True, "group": group, "restored": True}


@router.post("/api/prompts/adopt")
def api_prompts_adopt(payload: dict):
    """采用新版默认：用户文件 base 指纹重写为当前指纹（保留用户槽文本）。body: {group}"""
    p = payload if isinstance(payload, dict) else {}
    group = str(p.get("group") or "").strip()
    if not group or "/" in group or "\\" in group or ".." in group:
        raise _bad("非法组名")
    res = prompts_store.adopt(group)
    if not res.get("ok"):
        raise _bad("；".join(res.get("errors") or ["采用失败"]))
    return {"ok": True, "group": group, "base": res.get("base")}
