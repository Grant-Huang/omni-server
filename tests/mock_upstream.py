"""A minimal scripted fake DashScope Realtime endpoint, for testing omni-server's /ws
relay without a real API key, real network access, or burning real quota
(design-risks-review.md section 12's "recording-and-replay fixture" recommendation,
applied to the relay layer specifically).

It does NOT try to be a faithful simulation of Qwen's actual behavior -- it exists to
drive the *relay's* forwarding logic (does it pass bytes through untouched in both
directions, does it handle connect failures), not to validate what Qwen itself does
with any of these events. That validation only happens against the real endpoint.
"""
from __future__ import annotations

import json

import websockets


class MockUpstream:
    def __init__(self):
        self.received: list[str] = []
        self._server = None
        self._seen_first_update = False
        self.url: str | None = None

    async def _handler(self, websocket):
        async for raw in websocket:
            self.received.append(raw)
            try:
                msg = json.loads(raw)
            except ValueError:
                continue

            msg_type = msg.get("type")
            if msg_type == "session.update":
                reply_type = "session.updated" if self._seen_first_update else "session.created"
                self._seen_first_update = True
                await websocket.send(json.dumps({"type": reply_type}))
            elif msg_type == "response.create":
                await websocket.send(json.dumps({"type": "response.audio_transcript.delta", "delta": "mock reply"}))
                await websocket.send(json.dumps({"type": "response.done"}))
            elif msg_type == "trigger.unknown.event":
                # Used by test_relay.py to confirm an event type the relay has never
                # heard of still reaches the client untouched (design-risks-review.md
                # section 5(b): "只处理认识的事件类型，把不认识的丢掉" is the failure
                # mode this guards against -- but the MVP relay never inspects message
                # content at all, so this should trivially pass by construction).
                await websocket.send(json.dumps({"type": "some.future.event", "payload": 123}))

    async def start(self) -> str:
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
