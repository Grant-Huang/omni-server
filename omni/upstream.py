"""The real DashScope realtime connection.

Kept behind the same tiny interface the tests' FakeUpstream implements (send / recv /
close) so VoiceSession never knows which one it has.

Endpoint choice is not a preference. workforce spent several days on "session.update
sent, nothing comes back, no error" against the shared ``dashscope.aliyuncs.com`` host,
tried a dozen theories, and the answer turned out to be the host: on the
workspace-specific host the problem vanished entirely and unsupported parameters
started producing immediate, clear errors instead of silence. Configuring a workspace id
is therefore close to mandatory, and running without one logs a warning rather than
failing quietly.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

SHARED_WS_BASE = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"


def realtime_url(workspace_id: str, model: str) -> str:
    # QWEN_WS_BASE is a test-only escape hatch (points DashScopeUpstream.connect at a
    # local scripted fake instead of a real DashScope host) -- workforce's server.py has
    # the same override for the same reason. Not read from Config: nothing about a real
    # deployment should ever need it, so it does not appear in Config.from_env's surface.
    override = os.environ.get("QWEN_WS_BASE")
    if override:
        return f"{override}?model={model}"
    if workspace_id:
        base = f"wss://{workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
    else:
        log.warning(
            "no QWEN_WORKSPACE_ID set: falling back to the shared realtime host, which "
            "workforce measured silently dropping session.update messages"
        )
        base = SHARED_WS_BASE
    return f"{base}?model={model}"


def sanitize_workspace_id(raw: str) -> str:
    """Strip the quote characters that come along when a workspace id is pasted out of
    formatted text. workforce hit this for real: a trailing backtick from a Markdown
    code span produced ``invalid WebSocket URL``. These characters are never part of a
    genuine id, so removing them cannot corrupt a valid one."""
    return raw.strip().strip("`'\"‘’“”").strip()


class DashScopeUpstream:
    def __init__(self, ws) -> None:
        self._ws = ws

    @classmethod
    async def connect(cls, *, api_key: str, workspace_id: str, model: str, open_timeout: float = 10.0):
        import websockets

        url = realtime_url(sanitize_workspace_id(workspace_id), model)
        ws = await websockets.connect(
            url, additional_headers={"Authorization": f"Bearer {api_key}"}, open_timeout=open_timeout
        )
        return cls(ws)

    async def send(self, event: dict) -> None:
        await self._ws.send(json.dumps(event))

    async def recv(self) -> dict:
        raw = await self._ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def close(self) -> None:
        await self._ws.close()
