"""The /ws relay: browser <-> omni-server <-> DashScope.

MVP scope is deliberately a pure transparent relay (docs/mvp-plan.md section 2.1),
same shape as workforce/web-demo/server.py's relay() -- omni-server just moves that
from a local dev script to a deployable service. It does NOT parse events, inject
memory, or do anything "stateful proxy" yet (design-risks-review.md section 5(b)'s
end-state); unrecognized/future event types pass through untouched by construction,
since nothing here looks at message content at all.
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets
from aiohttp import WSMsgType, web

from .config import Config

logger = logging.getLogger(__name__)


def make_relay_handler(config: Config):
    async def relay(request: web.Request) -> web.WebSocketResponse:
        ws_client = web.WebSocketResponse(max_msg_size=10 * 1024 * 1024)
        await ws_client.prepare(request)

        if not config.qwen_api_key:
            await ws_client.send_str(json.dumps({"type": "relay.error", "message": "QWEN_API_KEY not set"}))
            await ws_client.close()
            return ws_client

        upstream_url = f"{config.upstream_ws_base}?model={config.qwen_model}"
        headers = {"Authorization": f"Bearer {config.qwen_api_key}"}

        try:
            upstream = await websockets.connect(upstream_url, additional_headers=headers, open_timeout=10)
        except Exception as e:
            logger.warning("upstream connect failed: %s", e)
            await ws_client.send_str(json.dumps({"type": "relay.error", "message": f"upstream connect failed: {e}"}))
            await ws_client.close()
            return ws_client

        async def client_to_upstream():
            async for msg in ws_client:
                if msg.type == WSMsgType.TEXT:
                    await upstream.send(msg.data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    break

        async def upstream_to_client():
            async for message in upstream:
                if ws_client.closed:
                    break
                await ws_client.send_str(message)

        task_up = asyncio.create_task(client_to_upstream())
        task_down = asyncio.create_task(upstream_to_client())
        try:
            _, pending = await asyncio.wait({task_up, task_down}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        finally:
            await upstream.close()
            if not ws_client.closed:
                await ws_client.close()
        return ws_client

    return relay
