"""本地访问安全：一次性令牌 + Host 白名单。

本应用默认只监听回环地址。为了防止 DNS rebinding / 本机其它网页直接调用 API，
启动时生成一次性令牌：访问首页时下发 SameSite=Strict 的 HttpOnly Cookie，
所有 /api/* 请求必须携带该 Cookie（或 X-Shenbi-Token 头，供脚本使用）。

设置环境变量 SHENBI_AUTH=0 可关闭（仅用于自动化测试/受控环境）。
"""
from __future__ import annotations

import os
import secrets

TOKEN = secrets.token_urlsafe(32)
AUTH_ENABLED = os.environ.get("SHENBI_AUTH", "1").strip().lower() not in ("0", "false", "off", "no")
COOKIE_NAME = "shenbi_token"


def trusted_hosts() -> list[str]:
    hosts = {"127.0.0.1", "localhost", "::1", "testserver"}
    extra = os.environ.get("SHENBI_TRUSTED_HOSTS", "")
    for h in extra.replace(",", " ").split():
        h = h.strip().strip("[]")
        if h:
            hosts.add(h)
    return sorted(hosts)
