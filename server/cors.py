"""Minimal hand-rolled CORS support -- not pulling in aiohttp_cors for one
middleware's worth of behavior. workforce/web-demo's server.py never needed this
(browser page and API were always same-origin); omni-server is a separately deployed
service the frontend now talks to cross-origin (docs/mvp-plan.md section 2.3).
"""
from __future__ import annotations

from aiohttp import web


def cors_middleware(allowed_origins: tuple):
    allow_all = "*" in allowed_origins

    @web.middleware
    async def middleware(request: web.Request, handler):
        origin = request.headers.get("Origin")

        if request.method == "OPTIONS":
            resp = web.Response()
        else:
            resp = await handler(request)

        if origin and (allow_all or origin in allowed_origins):
            resp.headers["Access-Control-Allow-Origin"] = "*" if allow_all else origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            if not allow_all:
                resp.headers["Vary"] = "Origin"
        return resp

    return middleware
