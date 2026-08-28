"""App factory + entrypoint. See docs/mvp-plan.md for what this MVP does and
deliberately doesn't do yet.
"""
from __future__ import annotations

from aiohttp import web

from .config import VOICE_OPTIONS, Config, load_config
from .cors import cors_middleware
from .memory_api import make_memory_routes
from .memory_store import MemoryStore
from .relay import make_relay_handler
from .text_api import make_dictation_cleanup_handler, make_memory_extract_handler


def create_app(config: Config | None = None) -> web.Application:
    config = config or load_config()
    app = web.Application(middlewares=[cors_middleware(config.cors_origins)])
    store = MemoryStore(config.memory_path)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def api_config(request: web.Request) -> web.Response:
        return web.json_response({
            "voice": config.qwen_voice,
            "voices": VOICE_OPTIONS,
            "hasKey": bool(config.qwen_api_key),
            "hasWorkspaceId": bool(config.qwen_workspace_id),
        })

    add_entry, search_entries, list_entries = make_memory_routes(store)

    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/ws", make_relay_handler(config))
    app.router.add_post("/api/dictation-cleanup", make_dictation_cleanup_handler(config))
    app.router.add_post("/api/memory-extract", make_memory_extract_handler(config))
    app.router.add_post("/api/memory", add_entry)
    app.router.add_get("/api/memory", list_entries)
    app.router.add_get("/api/memory/search", search_entries)

    return app


def main() -> None:
    config = load_config()
    app = create_app(config)
    domain_kind = "workspace-specific" if config.qwen_workspace_id else "shared (consider setting QWEN_WORKSPACE_ID)"
    print(f"Model: {config.qwen_model}  Voice: {config.qwen_voice}  Key loaded: {bool(config.qwen_api_key)}")
    print(f"Realtime endpoint: {config.upstream_ws_base} [{domain_kind}]")
    print(f"CORS origins: {', '.join(config.cors_origins) or '(none)'}")
    print(f"Listening on {config.host}:{config.port}")
    web.run_app(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
