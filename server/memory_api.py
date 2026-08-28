"""Routes for MemoryStore. Not called from anywhere in the relay path yet -- see
memory_store.py's module docstring."""
from __future__ import annotations

from aiohttp import web

from .memory_store import MemoryStore


def make_memory_routes(store: MemoryStore):
    async def add_entry(request: web.Request) -> web.Response:
        body = await request.json()
        text = body.get("text")
        layer = body.get("layer")
        if not isinstance(text, str) or not text.strip():
            return web.json_response({"error": "text is required"}, status=400)
        entry = store.add(text, layer=layer)
        if entry is None:
            return web.json_response({"error": "text too short to store"}, status=400)
        return web.json_response(entry, status=201)

    async def search_entries(request: web.Request) -> web.Response:
        query = request.query.get("q", "")
        if not query.strip():
            return web.json_response({"error": "q is required"}, status=400)
        try:
            limit = int(request.query.get("limit", "5"))
        except ValueError:
            return web.json_response({"error": "limit must be an integer"}, status=400)
        return web.json_response({"entries": store.search(query, limit=limit)})

    async def list_entries(request: web.Request) -> web.Response:
        return web.json_response({"entries": store.all()})

    return add_entry, search_entries, list_entries
