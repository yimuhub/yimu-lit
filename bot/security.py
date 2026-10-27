"""本地访问安全：持久化令牌 + Host 白名单。

本应用默认只监听回环地址。为了防止 DNS rebinding / 本机其它网页直接调用 API，
启动时生成令牌：访问首页时下发 SameSite=Strict 的 HttpOnly Cookie，
所有 /api/* 请求必须携带该 Cookie（或 X-Shenbi-Token 头，供脚本使用）。

v2.7.0 修复：令牌持久化到 .shenbi_token 文件，服务器重启后复用同一令牌，
避免浏览器旧 Cookie 失效导致 401 Unauthorized。

设置环境变量 SHENBI_AUTH=0 可关闭（仅用于自动化测试/受控环境）。
"""
from __future__ import annotations

import os
import secrets

AUTH_ENABLED = os.environ.get("SHENBI_AUTH", "1").strip().lower() not in ("0", "false", "off", "no")
COOKIE_NAME = "shenbi_token"

_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".shenbi_token")


def _load_or_create_token() -> str:
    """v2.7.0: 令牌持久化到文件，服务器重启后复用同一令牌。"""
    try:
        if os.path.isfile(_TOKEN_FILE):
            with open(_TOKEN_FILE, "r") as f:
                tok = f.read().strip()
                if tok and len(tok) >= 16:
                    return tok
    except Exception:
        pass
    tok = secrets.token_urlsafe(32)
    try:
        with open(_TOKEN_FILE, "w") as f:
            f.write(tok)
        os.chmod(_TOKEN_FILE, 0o600)
    except Exception:
        pass
    return tok


TOKEN = _load_or_create_token()


def trusted_hosts() -> list[str]:
    hosts = {"127.0.0.1", "localhost", "::1", "testserver"}
    extra = os.environ.get("SHENBI_TRUSTED_HOSTS", "")
    for h in extra.replace(",", " ").split():
        h = h.strip().strip("[]")
        if h:
            hosts.add(h)
    return sorted(hosts)
