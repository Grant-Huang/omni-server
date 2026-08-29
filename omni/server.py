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
from .cors import cors_middleware
from .extraction import Extractor
from .memory import MemoryStore, Provenance
from .persistence import SqliteMemoryPersistence
from .photos import PhotoStore, analyze_photo_with_vlm
from .realtime import VoiceSession
from .sidecar import Sidecar, memory_search_tool
from .stories import StoryStore, StoryGenerator
from .textmodel import DashScopeTextModel
from .upstream import DashScopeUpstream

log = logging.getLogger("omni.server")
routes = web.RouteTableDef()

CONFIG = web.AppKey("config", Config)
STORE = web.AppKey("store", MemoryStore)
SESSIONS = web.AppKey("sessions", set)
PERSISTENCE = web.AppKey("persistence", SqliteMemoryPersistence)
PHOTOS = web.AppKey("photos", PhotoStore)
STORIES = web.AppKey("stories", StoryStore)


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


@routes.post("/api/memory/session/{session_id}")
async def end_session(request):
    """Mark a session as ended. Extraction happens asynchronously in the background
    when the WebSocket closes. This endpoint is a signal from the client that the
    session has ended, and can be used to trigger extraction if needed for offline flows."""
    session_id = request.match_info.get("session_id", "")
    if not session_id:
        return web.json_response({"error": "session_id required"}, status=400)
    # Extraction is already triggered when the WebSocket closes;
    # this endpoint exists for explicit confirmation and future features like polling.
    log.info("session end signal: %s", session_id)
    return web.json_response({"status": "session_ended", "session_id": session_id})


@routes.post("/api/photos/upload")
async def upload_photo(request):
    """Upload a photo for VLM analysis."""
    cfg: Config = request.app[CONFIG]
    photo_store: PhotoStore = request.app[PHOTOS]
    text_model = request.app.get("text_model")

    try:
        data = await request.post()
        if "photo" not in data:
            return web.json_response({"error": "photo field required"}, status=400)

        file_field = data["photo"]
        file_data = file_field.file.read()
        if not file_data:
            return web.json_response({"error": "empty file"}, status=400)

        # Store photo
        photo = photo_store.add(cfg.user_scope, file_data)

        # Analyze with VLM (async, but wait briefly for MVP)
        caption, participants = await analyze_photo_with_vlm(
            file_data, text_model, cfg.workspace_id
        )
        photo_store.update_caption(photo.id, caption, participants)

        return web.json_response({
            "id": photo.id,
            "caption": caption,
            "participants": participants,
        })
    except Exception as exc:
        log.exception("photo upload failed")
        return web.json_response({"error": str(exc)}, status=400)


@routes.get("/api/photos")
async def list_photos(request):
    """Get all photos for the current user."""
    cfg: Config = request.app[CONFIG]
    photo_store: PhotoStore = request.app[PHOTOS]

    try:
        photos = photo_store.list_for_scope(cfg.user_scope)
        return web.json_response([p.to_dict() for p in photos])
    except Exception as exc:
        log.exception("photo list failed")
        return web.json_response({"error": str(exc)}, status=400)


@routes.get("/api/stories")
async def list_stories(request):
    """Get all stories for the current user."""
    story_store: StoryStore = request.app[STORIES]
    try:
        stories = story_store.list_all()
        return web.json_response([s.to_dict() for s in stories])
    except Exception as exc:
        log.exception("story list failed")
        return web.json_response({"error": str(exc)}, status=400)


@routes.post("/api/stories/from-memory")
async def create_story_from_memory(request):
    """Generate a story from related memory entries."""
    cfg: Config = request.app[CONFIG]
    store: MemoryStore = request.app[STORE]
    story_store: StoryStore = request.app[STORIES]

    try:
        body = await request.json()
        entry_ids = body.get("entryIds", [])
        title = body.get("title")
        description = body.get("description")

        if not entry_ids:
            return web.json_response({"error": "entryIds required"}, status=400)

        story = StoryGenerator.story_from_memory_entries(
            entry_ids, store, title=title, description=description
        )
        if not story:
            return web.json_response({"error": "no entries found"}, status=404)

        story_store.add(story)
        return web.json_response(story.to_dict())
    except Exception as exc:
        log.exception("story creation from memory failed")
        return web.json_response({"error": str(exc)}, status=400)


@routes.post("/api/stories/from-photos")
async def create_story_from_photos(request):
    """Generate a story from related photos."""
    photo_store: PhotoStore = request.app[PHOTOS]
    story_store: StoryStore = request.app[STORIES]

    try:
        body = await request.json()
        photo_ids = body.get("photoIds", [])
        title = body.get("title")

        if not photo_ids:
            return web.json_response({"error": "photoIds required"}, status=400)

        story = StoryGenerator.story_from_photos(photo_ids, photo_store, title=title)
        if not story:
            return web.json_response({"error": "no photos found"}, status=404)

        story_store.add(story)
        return web.json_response(story.to_dict())
    except Exception as exc:
        log.exception("story creation from photos failed")
        return web.json_response({"error": str(exc)}, status=400)


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
                data = json.loads(msg.data)
                await upstream.send(data)
                # Voice turns begin from the upstream ASR-completion event (pump_upstream
                # above); typed text has no ASR step, so it needs its own trigger here.
                # Forwarding the item upstream first is still required -- DashScope has to
                # have it in context before response.create references it.
                text = _client_text_turn(data)
                if text:
                    # Only the upstream side of a turn is recorded by _record() above
                    # (it never sees client-authored events) -- without this, extraction
                    # would see the assistant's reply with no matching user turn for a
                    # typed conversation.
                    transcript.append(("user", text))
                    await session.begin_turn(text)
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


def _client_text_turn(data: dict) -> str | None:
    """A client-sent ``conversation.item.create`` for a typed (not spoken) user
    message, extracted to plain text -- or None if this event isn't one. Mirrors the
    Realtime API's own item shape: ``item.content`` is a list of parts, and a typed
    message's text lives in the ``input_text`` part(s)."""
    if data.get("type") != "conversation.item.create":
        return None
    item = data.get("item") or {}
    if item.get("type") != "message" or item.get("role") != "user":
        return None
    parts = item.get("content") or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "input_text"]
    text = " ".join(t for t in texts if t).strip()
    return text or None


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
    """Persistence only engages when the caller lets ``store`` default -- a caller that
    passes its own store (every test in tests/test_server.py does) opts out entirely,
    which is what keeps the whole test suite in-memory and millisecond-fast without any
    of them needing to know persistence exists."""
    cfg = config or Config.from_env()
    app = web.Application(middlewares=[cors_middleware(cfg.cors_origins)])
    app[CONFIG] = cfg

    if store is not None:
        app[STORE] = store
    elif cfg.db_path:
        persistence = SqliteMemoryPersistence(cfg.db_path)
        fresh = MemoryStore(persist=persistence)
        restored = fresh.restore(persistence.load_all())
        log.info("restored %d memory entries from %s", restored, cfg.db_path)
        app[STORE] = fresh
        app[PERSISTENCE] = persistence
        app.on_cleanup.append(_close_persistence)
    else:
        app[STORE] = MemoryStore()  # cfg.db_path == "" -- persistence explicitly off

    app[SESSIONS] = set()
    app[PHOTOS] = PhotoStore()
    app[STORIES] = StoryStore()
    app.add_routes(routes)
    return app


async def _close_persistence(app: web.Application) -> None:
    persistence = app.get(PERSISTENCE)
    if persistence is not None:
        persistence.close()


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
