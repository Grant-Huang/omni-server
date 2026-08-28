from __future__ import annotations

import json

from aiohttp.test_utils import TestClient, TestServer

from server.app import create_app
from tests.mock_upstream import MockUpstream


async def test_relay_forwards_events_both_directions(make_config):
    upstream = MockUpstream()
    upstream_url = await upstream.start()
    try:
        config = make_config(qwen_ws_base_shared=upstream_url)
        app = create_app(config)
        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "session.update", "session": {"instructions": "hi"}})
            assert await ws.receive_json() == {"type": "session.created"}

            await ws.send_json({"type": "response.create"})
            delta = await ws.receive_json()
            assert delta == {"type": "response.audio_transcript.delta", "delta": "mock reply"}
            assert await ws.receive_json() == {"type": "response.done"}

            await ws.close()

        assert json.loads(upstream.received[0])["type"] == "session.update"
        assert json.loads(upstream.received[1])["type"] == "response.create"
    finally:
        await upstream.stop()


async def test_relay_passes_through_unrecognized_event_types(make_config):
    """The MVP relay never inspects message content (docs/mvp-plan.md section 2.1),
    so an event type it's never seen before must still reach the client untouched --
    the failure mode design-risks-review.md section 5(b) warns against ("只处理认识的
    事件类型，把不认识的丢掉") can't happen here by construction, but this locks in
    that the behavior doesn't regress if someone later adds content inspection."""
    upstream = MockUpstream()
    upstream_url = await upstream.start()
    try:
        config = make_config(qwen_ws_base_shared=upstream_url)
        app = create_app(config)
        async with TestClient(TestServer(app)) as client:
            ws = await client.ws_connect("/ws")
            await ws.send_json({"type": "trigger.unknown.event"})
            assert await ws.receive_json() == {"type": "some.future.event", "payload": 123}
            await ws.close()
    finally:
        await upstream.stop()


async def test_relay_errors_when_api_key_missing(make_config):
    config = make_config(qwen_api_key="")
    app = create_app(config)
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/ws")
        msg = await ws.receive_json()
        assert msg["type"] == "relay.error"
        assert "QWEN_API_KEY" in msg["message"]


async def test_relay_errors_when_upstream_unreachable(make_config, unused_tcp_port):
    config = make_config(qwen_ws_base_shared=f"ws://127.0.0.1:{unused_tcp_port}")
    app = create_app(config)
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/ws")
        msg = await ws.receive_json()
        assert msg["type"] == "relay.error"
        assert "upstream connect failed" in msg["message"]
