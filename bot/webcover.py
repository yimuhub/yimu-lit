"""AI 生成封面（v5.7.0）：书架封面走 OpenAI 兼容 images/generations 协议（全部自研）。

FastAPI APIRouter 收口，不改 app.py / weblib.py —— 主会话在 app.py 认证中间件之后、
``@app.get("/api/library/{key}")`` 等路由注册**之前**加两行挂载（与 weblib/webcanvas 同位置；
本路由路径都是 /api/library/cover/ai 两段后缀，不会被单段通配 /api/library/{key} 吞）：

    from bot import webcover
    app.include_router(webcover.router)

接口契约（与 static/v2/js/core/aicover.js、views/settings.js 对齐）：
  POST /api/library/cover/ai          {key, prompt?, style?, mock?} 生成并落盘
                                      → {ok, cover, url, mtime, mock}
  GET  /api/library/cover/ai/config   图像供应商配置（key 打码）。v5.9.0 起图像能力并入
                                      providers[] 的 kind=image 条目，返回 = v5.7.0 兼容字段
                                      {ok, configured, base_url, model, size, has_key,
                                      key_display}（取自默认图像条目，封面弹窗只看 configured）
                                      + {default_image_provider, providers:[图像条目列表]}。
  POST /api/library/cover/ai/config   保存图像配置 {provider_id?, name?, base_url?, api_key?,
                                      model?, size?}：显式 provider_id → 保存到该条目并设为
                                      图像默认；否则默认图像条目 / 唯一图像条目 / 新建。
                                      api_key 留空=保留旧值（密钥不回显）。
  POST /api/library/cover/ai/test     探活：{provider_id?} 按条目（缺省=当前默认）请求一张
                                      512x512 测试图 → {ok, detail}（detail 全中文）

设计要点：
  · 提示词组装走 prompts/ 开放体系（组 cover-art，system+user 两槽，可用户覆盖）：
    美术指导人设——书名/题材/基调 → ≤180 字画面描述；中文书名竖排由前端排版呈现，
    提示词层面禁止把任何文字画进画面（{{书名}} 仅作题材气质参考）。
  · 图像 HTTP（httpx）：b64_json 优先、url 兜底回捞；60s 超时；错误全中文
    （401/403 鉴权、429 限流、404 端点、超时、模型不存在）。
  · 落盘复用 weblib 既有链路（import save_cover/_cover_url，不复制第二套）：
    文件头校验图型、写 projects/<key>/cover.<png|jpg|webp>、清理旧封面同口径。
  · mock 模式：body mock=true（或 ?mock=1）不穿透——用代码内置的 1x1 占位 PNG
    走同一条校验落盘链路（pytest 验证 mock 生成→落盘→旧封面清理）；
    前端 ?mock=1（IS_MOCK）则在本地画渐变占位，根本不发请求（aicover.js）。
  · 无图像供应商 → 400「请先在设置→供应商配置图像模型」（v5.9.0：图像供应商 =
    供应商列表里 kind=image 的条目，在设置→模型与供应商添加，类型选「图像」）。
  · key 别名解析复用 writer._project_dir（与 webcanvas._project 同款，改名后的旧 key 可用）。
"""
from __future__ import annotations

import base64
import os
import struct
import zlib

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import prompts_store, providers, writer
from .weblib import save_cover, _cover_url   # 只读 import：封面校验/落盘/清理旧封面复用 weblib

router = APIRouter()

IMAGE_TIMEOUT = 60            # 图像生成超时（秒）
PROMPT_INPUT_MAX = 180        # 用户画面描述上限（≤180 字画面描述）
STYLE_INPUT_MAX = 60          # 风格关键词上限
PRESET_STYLES = ("水墨", "国风插画", "都市光影", "暗黑悬疑", "清新文艺")


class ImageAPIError(RuntimeError):
    """图像接口调用失败（message 全中文，直接下发前端）。"""


# ---------------------------------------------------------------- 提示词（组 cover-art：system+user 两槽）

# 代码默认值与指纹源；prompts/cover-art.md 与此处保持逐字一致（v53 同款纪律）。
_SYS_DEFAULT = (
    "你是一位资深书籍封面美术指导，为一部中文小说绘制竖版封面插画。"
    "构图服从竖版封面的视觉习惯：主体居中偏下、上部留出排书名的空间、四周留白；"
    "色调与题材气质一致，光影方向统一。"
    "铁则：画面中绝对不得出现任何文字、字母、数字、汉字、水印、签名或对话框——"
    "书名与作者名由后期排版添加；招牌、书页、屏幕等物件上的文字一律虚化或去掉。"
    "画面聚焦一处具体场景：主体是谁（或是什么）、在做什么、环境、光线与色调都要具体可画。"
)

_USER_DEFAULT = (
    "为《{{书名}}》绘制竖版书籍封面插画。"
    "题材：{{题材}}；气质基调：{{基调}}。"
    "画面：{{画面}}。"
    "（书名仅用于把握题材气质，画面中不得出现任何文字。）"
)

# v5.7.0：登记代码默认值（提示词编辑页指纹与「默认值已更新」判定来源）
prompts_store.register(
    "cover-art", "AI 封面",
    desc="书架 AI 生成封面的图像提示词：美术指导人设（system）+ 画面描述模板（user）。",
    slots={
        "system": {"default": _SYS_DEFAULT, "label": "美术指导人设与铁则", "temp": None,
                   "vars": [],
                   "desc": "发给图像接口的公共铁则：竖版构图习惯 + 画面禁止出现文字。"},
        "user": {"default": _USER_DEFAULT, "label": "画面描述模板", "temp": None,
                 "vars": prompts_store.slot_vars(_USER_DEFAULT),
                 "desc": "画面描述模板；{{画面}} 由用户风格描述与题材组装（≤180 字）。"},
    },
)


def _clip(text: str, limit: int) -> str:
    """单行化 + 截断（用户输入防灌长提示词）。"""
    s = " ".join(str(text or "").split())
    return s[:limit]


def build_cover_prompt(*, title: str = "", genre: str = "", idea: str = "",
                       style: str = "", extra: str = "") -> str:
    """组装封面提示词（美术指导人设）。三层开放：用户层 → 内置层 → 代码默认（prompts_store.text）。"""
    sys_text = prompts_store.text("cover-art", "system", default=_SYS_DEFAULT)
    tpl = prompts_store.text("cover-art", "user", default=_USER_DEFAULT)
    # 画面描述：用户输入优先（≤180 字）；没写则按题材给一个具体可画的兜底场景
    scene = _clip(extra, PROMPT_INPUT_MAX) or \
        "与题材气质相符的一处象征性主场景，主体剪影鲜明，氛围浓郁，光线有层次"
    style_kw = _clip(style, STYLE_INPUT_MAX)
    parts = [scene]
    if style_kw:
        parts.append("整体风格倾向：" + style_kw)
    idea_kw = _clip(idea, 80)
    if idea_kw:
        parts.append("参考意象：" + idea_kw)
    user_text = prompts_store.fill(
        tpl,
        书名=_clip(title, 40) or "未命名",
        题材=_clip(genre, 20) or "小说",
        基调=style_kw or "贴合题材的整体氛围",
        画面="；".join(parts),
    )
    return f"{sys_text}\n\n{user_text}"


# ---------------------------------------------------------------- 图像接口（httpx，b64 优先 / url 兜底）

def _auth_headers(api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    return headers


def _raise_for_status(r: httpx.Response, model: str) -> None:
    """把图像接口的错误码翻成全中文 ImageAPIError。"""
    if r.status_code == 200:
        return
    if r.status_code in (401, 403):
        raise ImageAPIError("图像接口鉴权失败（%d），请检查 API key 是否正确" % r.status_code)
    if r.status_code == 429:
        raise ImageAPIError("图像接口请求太频繁（429），请稍后再试")
    if r.status_code == 404:
        raise ImageAPIError(
            "图像端点不存在（404）：请确认 base_url 以 /v1 结尾、且模型支持 images/generations")
    txt = (r.text or "")[:200]
    low = txt.lower()
    if r.status_code == 400 and ("model" in low or "模型" in txt):
        raise ImageAPIError("图像模型不存在或不可用：%s（服务端返回 %d）" % (model, r.status_code))
    raise ImageAPIError("图像接口返回 %d：%s" % (r.status_code, txt))


def _fetch_image_bytes(url: str, timeout: int) -> bytes:
    """url 兜底回捞：下载图像 bytes（断链/超时全中文）。"""
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.TimeoutException:
        raise ImageAPIError("下载生成图片超时，请稍后重试") from None
    except httpx.HTTPError as e:
        raise ImageAPIError("下载生成图片失败：%s" % str(e)[:160]) from e
    if r.status_code != 200 or not r.content:
        raise ImageAPIError("下载生成图片失败（HTTP %d）" % r.status_code)
    return r.content


def generate_image(rt: dict, prompt: str, timeout: int = IMAGE_TIMEOUT) -> bytes:
    """调 images/generations 拿一张图：b64_json 优先，url 兜底回捞。返回图片 bytes。"""
    base = (rt.get("base_url") or "").rstrip("/")
    model = rt.get("model") or ""
    body = {"model": model, "prompt": prompt, "n": 1,
            "size": rt.get("size") or providers.DEFAULT_IMAGE_SIZE,
            "response_format": "b64_json"}
    try:
        r = httpx.post(base + "/images/generations", headers=_auth_headers(rt.get("api_key") or ""),
                       json=body, timeout=timeout)
    except httpx.TimeoutException:
        raise ImageAPIError("图像接口连接超时（%d 秒），请稍后重试或换更快的模型" % timeout) from None
    except httpx.HTTPError as e:
        raise ImageAPIError("无法连接图像接口，请检查 base_url：%s" % str(e)[:160]) from e
    _raise_for_status(r, model)
    try:
        j = r.json()
    except Exception as e:
        raise ImageAPIError("图像接口返回了无法解析的内容，请确认该端点是 OpenAI 兼容 images 接口") from e
    items = j.get("data") or []
    if not items or not isinstance(items[0], dict):
        raise ImageAPIError("图像接口没有返回图片，请稍后重试")
    item = items[0]
    b64 = item.get("b64_json")
    if b64:
        try:
            return base64.b64decode(str(b64))
        except Exception as e:
            raise ImageAPIError("图片数据（b64_json）解码失败，请重试") from e
    img_url = str(item.get("url") or "").strip()
    if img_url:
        return _fetch_image_bytes(img_url, timeout)
    raise ImageAPIError("图像接口既没返回 b64_json 也没返回 url，请换一个兼容 OpenAI images 协议的模型")


# ---------------------------------------------------------------- mock：内置 1x1 占位 PNG（代码生成，无网络）

def mock_cover_png() -> bytes:
    """内置 1x1 占位 PNG（mock 模式不穿透真实图像接口）。用 zlib/struct 现拼，
    带合法 IHDR/IDAT/IEND 与 CRC，能通过 weblib.sniff_image 的文件头校验。"""

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)   # 1x1，8bit RGBA
    raw = b"\x00" + b"\x40\x44\x54\x64"                    # filter 0 + 灰蓝像素
    return (b"\x89PNG\r\n\x1a\n" +
            _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b""))


# ---------------------------------------------------------------- 纯逻辑（HTTP 与 pytest 共用）

def _project_of(key: str):
    """key → (pdir, plan) 或 (None, None)。别名解析走 writer._project_dir。"""
    key = str(key or "").strip()
    if not key:
        return None, None
    try:
        pdir = writer._project_dir(key)
    except Exception:
        pdir = None
    if not pdir or not os.path.isdir(pdir):
        return None, None
    try:
        _med, _pj, plan = writer._plan_of(pdir)
    except Exception:
        plan = {}
    return pdir, (plan or {})


def generate_and_save(key: str, prompt: str = "", style: str = "",
                      mock: bool = False) -> dict:
    """生成 → weblib.save_cover 校验落盘（清理旧封面同口径）。返回给前端的 dict 或错误。"""
    mock = bool(mock)
    if not mock:
        pdir, plan = _project_of(key)
        if pdir is None:
            return {"ok": False, "status": 400, "message": "作品不存在或已被删除"}
        rt = providers.image_provider_runtime()
        if not rt["configured"]:
            return {"ok": False, "status": 400,
                    "message": "请先在设置→供应商配置图像模型（模型与供应商→添加供应商，"
                               "类型选「图像」，填 OpenAI 兼容的 images 接口地址与模型名）"}
        cfg = (plan.get("config") or {}) if isinstance(plan, dict) else {}
        prompt_text = build_cover_prompt(
            title=str((plan or {}).get("novelName") or (cfg.get("novelName") or "") or key),
            genre=str(cfg.get("genre") or ""),
            idea=str(cfg.get("idea") or ""),
            style=style, extra=prompt,
        )
        try:
            img = generate_image(rt, prompt_text)
        except ImageAPIError as e:
            return {"ok": False, "status": 400, "message": str(e)}
    else:
        img = mock_cover_png()
    res = save_cover(key, img)      # 复用 weblib：文件头校验 + 原子写 + 清理旧封面
    if not res.get("ok"):
        return {"ok": False, "status": 400, "message": str(res.get("note") or "封面保存失败")}
    return {"ok": True, "cover": res.get("cover"),
            "url": _cover_url({"key": res.get("key"), "mtime": res.get("mtime")}),
            "mtime": res.get("mtime"), "mock": mock, "removed_old": res.get("removed_old", 0)}


def _bad(message: str, status: int = 400):
    return JSONResponse({"ok": False, "error": str(message), "detail": str(message)},
                        status_code=status)


# ---------------------------------------------------------------- HTTP 端点

@router.post("/api/library/cover/ai")
async def api_cover_ai(request: Request):
    """AI 生成封面：{key, prompt?, style?, mock?}。生成即落盘，「采用」= 前端刷新封面。"""
    try:
        payload = await request.json()
    except Exception:
        return _bad("请求体不是合法 JSON")
    p = payload if isinstance(payload, dict) else {}
    key = str(p.get("key") or "").strip()
    if not key:
        return _bad("缺少作品 key")
    mock = p.get("mock") is True or request.query_params.get("mock") == "1"
    out = generate_and_save(key,
                            prompt=str(p.get("prompt") or ""),
                            style=str(p.get("style") or ""),
                            mock=mock)
    if not out.get("ok"):
        return _bad(out.get("message") or "AI 封面生成失败", int(out.get("status") or 400))
    return out


@router.get("/api/library/cover/ai/config")
def api_cover_ai_config_get():
    """图像供应商配置（key 打码；设置页图像默认槽与封面弹窗的无配置判断共用）。
    v5.9.0：图像能力并入 providers[] 的 kind=image 条目——返回 v5.7.0 兼容字段
    （取自默认图像条目）+ default_image_provider + 图像条目列表。"""
    try:
        return providers.image_config_view()
    except Exception:
        return {"ok": True, "configured": False, "base_url": "", "model": "",
                "size": providers.DEFAULT_IMAGE_SIZE, "has_key": False,
                "key_display": "未设置（可留空：本地免鉴权接口）",
                "default_image_provider": "", "providers": []}


@router.post("/api/library/cover/ai/config")
def api_cover_ai_config_post(payload: dict = None):
    """保存图像供应商配置（写 providers[] 的 kind=image 条目，兼容 v5.7.0 字段口径）。
    provider_id 显式给出时保存到该条目并设为图像默认；api_key 留空 = 保留已保存值。"""
    p = payload if isinstance(payload, dict) else {}
    try:
        cfg = providers.save_image_provider(p)
    except providers.ProviderError as e:
        return _bad(str(e))
    return {"ok": True, **cfg}


@router.post("/api/library/cover/ai/test")
def api_cover_ai_test(payload: dict = None):
    """探活：{provider_id?} 按条目请求一张 512x512 测试图（缺省=当前默认图像供应商）。
    HTTP 恒 200，结果看 ok/detail（全中文）。"""
    p = payload if isinstance(payload, dict) else {}
    try:
        size = str(p.get("size") or "512x512")
        r = providers.test_image_provider(provider_id=str(p.get("provider_id") or ""),
                                          size=size, timeout=int(p.get("timeout") or 30))
    except Exception as e:
        r = {"ok": False, "detail": "测试失败：%s" % str(e)[:160]}
    return r
