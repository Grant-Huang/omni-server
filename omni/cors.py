"""Minimal hand-rolled CORS -- not pulling in aiohttp_cors for one middleware's worth
of behavior. Needed because the client (omni/web-demo) is a page served from its own
process, not from here (docs/roadmap.md's "接上 omni 客户端"): its `fetch("/api/config")`
is cross-origin against omni-server, and a fetch response without these headers is
opaque to the page's own JS even though the request itself succeeds server-side.
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
