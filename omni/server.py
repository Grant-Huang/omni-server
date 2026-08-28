"""aiohttp entrypoint.

Run: ``python -m omni.server`` (reads QWEN_* from the environment / .env).

The websocket endpoint is deliberately *not* the byte pump workforce's server.py is:
every upstream frame goes through VoiceSession so memory can be retrieved and lookup
results injected. Frames from the client go straight up -- audio is the bulk of that
traffic and there is nothing useful to do with it here.

Also exposes a small REST surface over memory. That is not an afterthought: persona and
policy are human-write-only by design (docs/memory-design.md §5), so there has to be a
way for a human to actually write and read them. A layer nobody can inspect is a layer
nobody can audit.
"""
from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import WSMsgType, web

from . import layers as L
from .config import BASE_INSTRUCTIONS, Config
from .extraction import Extractor
from .memory import MemoryStore, Provenance
from .realtime import VoiceSession
from .sidecar import Sidecar, memory_search_tool
from .textmodel import DashScopeTextModel
from .upstream import DashScopeUpstream

log = logging.getLogger("omni.server")
routes = web.RouteTableDef()

CONFIG = web.AppKey("config", Config)
STORE = web.AppKey("store", MemoryStore)
SESSIONS = web.AppKey("sessions", set)


@routes.get("/api/config")
async def get_config(request):
    cfg: Config = request.app[CONFIG]
    return web.json_response({
        "voice": cfg.voice,
        "realtimeModel": cfg.realtime_model,
        "textModel": cfg.text_model,
        "hasKey": bool(cfg.api_key),
        "hasWorkspaceId": bool(cfg.workspace_id),
        "layers": [
            {
                "name": s.name,
                "injection": s.injection,
                "humanOnly": s.human_only,
                "budgetChars": s.budget_chars,
                "defaultTtlDays": s.default_ttl_days,
                "heading": s.heading,
                "description": s.description,
            }
            for s in L.ORDERED_LAYERS
        ],
    })


@routes.get("/api/memory")
async def list_memory(request):
    store: MemoryStore = request.app[STORE]
    cfg: Config = request.app[CONFIG]
    include_dead = request.query.get("include_superseded") == "1"
    entries = store.all_entries() if include_dead else store.live()
    return web.json_response([
        {
            "id": e.id, "text": e.text, "layer": e.layer, "scope": e.scope,
            "writtenBy": e.written_by, "createdAt": e.created_at,
            "expiresAt": e.expires_at, "supersededBy": e.superseded_by,
            "originScope": e.source.origin_scope, "speakerId": e.source.speaker_id,
        }
        for e in sorted(entries, key=lambda e: (L.spec(e.layer).order, e.created_at))
        if e.scope == cfg.user_scope
    ])


@routes.post("/api/memory")
async def write_memory(request):
    """Human writes. This is the only path that may touch persona/policy -- the
    extraction pipeline goes through Extractor, which cannot."""
    store: MemoryStore = request.app[STORE]
    cfg: Config = request.app[CONFIG]
    body = await request.json()
    try:
        entry = store.add(
            body.get("text", ""),
            layer=body.get("layer", ""),
            scope=cfg.user_scope,
            written_by=L.HUMAN,
            source=Provenance(origin_scope=cfg.user_scope, speaker_id=cfg.user_scope),
            ttl_days=body.get("ttlDays", -1.0),
            supersedes=body.get("supersedes", []),
        )
    except (ValueError, PermissionError, KeyError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({"id": entry.id, "layer": entry.layer})


@routes.get("/ws")
async def voice_ws(request):
    cfg: Config = request.app[CONFIG]
    store: MemoryStore = request.app[STORE]

    client = web.WebSocketResponse(max_msg_size=10 * 1024 * 1024)
    await client.prepare(request)

    if not cfg.api_key:
        await client.send_str(json.dumps({"type": "omni.error", "message": "QWEN_API_KEY not set"}))
        await client.close()
        return client

    try:
        upstream = await DashScopeUpstream.connect(
            api_key=cfg.api_key, workspace_id=cfg.workspace_id, model=cfg.realtime_model
        )
    except Exception as exc:
        # Never leave the client sitting on "connecting…": say what failed.
        await client.send_str(json.dumps({"type": "omni.error", "message": f"upstream connect failed: {exc}"}))
        await client.close()
        return client

    async def to_client(event: dict) -> None:
        if not client.closed:
            await client.send_str(json.dumps(event))

    text_model = DashScopeTextModel(cfg.api_key, model=cfg.text_model, workspace_id=cfg.workspace_id)
    sidecar = Sidecar(
        text_model,
        {"memory_search": memory_search_tool(store, output_scope=cfg.user_scope)},
    )
    session = VoiceSession(
        upstream, store,
        base_instructions=BASE_INSTRUCTIONS,
        user_scope=cfg.user_scope,
        to_client=to_client,
        sidecar=sidecar,
    )
    request.app[SESSIONS].add(session)

    transcript: list[tuple[str, str]] = []

    async def pump_upstream() -> None:
        while True:
            event = await upstream.recv()
            _record(transcript, event)
            await session.handle_upstream_event(event)

    up_task = asyncio.create_task(pump_upstream())
    try:
        async for msg in client:
            if msg.type == WSMsgType.TEXT:
                await upstream.send(json.loads(msg.data))
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                break
    finally:
        up_task.cancel()
        await session.close()
        request.app[SESSIONS].discard(session)
        # Extraction runs at the session boundary, not per turn: batching gives it the
        # whole conversation to judge from and nothing is waiting on the result.
        if transcript:
            asyncio.create_task(_extract(request.app, transcript, session.session_id, text_model))
        if not client.closed:
            await client.close()
    return client


def _record(transcript: list[tuple[str, str]], event: dict) -> None:
    etype = event.get("type")
    if etype == "conversation.item.input_audio_transcription.completed":
        text = (event.get("transcript") or "").strip()
        if text:
            transcript.append(("user", text))
    elif etype == "response.audio_transcript.done":
        text = (event.get("transcript") or "").strip()
        if text:
            transcript.append(("assistant", text))


async def _extract(app, turns, session_id, text_model) -> None:
    cfg: Config = app[CONFIG]
    store: MemoryStore = app[STORE]
    try:
        report = await Extractor(text_model, store).extract(
            turns,
            scope=cfg.user_scope,
            source=Provenance(origin_scope=cfg.user_scope, session_id=session_id),
            session_id=session_id,
            existing_hint=[e for e in store.live() if e.scope == cfg.user_scope][:20],
        )
        log.info("extraction: stored=%d rejected=%d superseded=%d",
                 len(report.stored), len(report.rejected), report.superseded)
        for text, reason in report.rejected:
            log.warning("extraction rejected %r: %s", text[:40], reason)
    except Exception:
        # A failed extraction loses one conversation's distillation. It must never take
        # anything else down with it -- the raw turns are still recoverable upstream.
        log.exception("extraction failed for session %s", session_id)


def make_app(config: Config | None = None, store: MemoryStore | None = None) -> web.Application:
    app = web.Application()
    app[CONFIG] = config or Config.from_env()
    app[STORE] = store or MemoryStore()
    app[SESSIONS] = set()
    app.add_routes(routes)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    cfg = Config.from_env()
    app = make_app(cfg)
    print(f"omni-server on {cfg.host}:{cfg.port}")
    print(f"  realtime: {cfg.realtime_model}  text: {cfg.text_model}  voice: {cfg.voice}")
    print(f"  workspace host: {'yes' if cfg.workspace_id else 'NO -- shared host, see omni/upstream.py'}")
    web.run_app(app, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
