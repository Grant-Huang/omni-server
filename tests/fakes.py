"""Test doubles.

The whole point: omni-server's orchestration logic -- retrieval, budgeting, patch
skipping, ack timeouts, the self-interrupt merge -- can be tested exhaustively without
ever opening a socket to DashScope. workforce's development was repeatedly blocked by
rate limits ("满打满算只够跑一次 5 轮的完整测试"), and by tests that each opened their own
upstream connection. Every test in this suite runs offline and in milliseconds.
"""
from __future__ import annotations

import asyncio


class FakeUpstream:
    """Scriptable stand-in for the DashScope realtime WebSocket.

    ``auto_ack`` mirrors the real server acknowledging a ``session.update`` with a
    ``session.updated``. Turning it off reproduces the failure workforce actually
    observed -- the update vanishing with no error and no ack -- which is what the
    timeout fallback exists for.
    """

    def __init__(self, *, auto_ack: bool = True, session_handler=None) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self.auto_ack = auto_ack
        self._session = session_handler  # set by the test to receive injected acks

    def bind(self, session) -> None:
        self._session = session

    async def send(self, event: dict) -> None:
        self.sent.append(event)
        if event.get("type") == "session.update" and self.auto_ack and self._session:
            # Deliver on the next loop tick, like a real network round trip: the caller
            # must actually be awaiting the ack, not already holding it.
            asyncio.get_running_loop().call_soon(
                asyncio.create_task, self._session.handle_upstream_event({"type": "session.updated"})
            )

    async def recv(self) -> dict:  # pragma: no cover - tests drive events directly
        await asyncio.sleep(3600)
        return {}

    async def close(self) -> None:
        self.closed = True

    # -- assertions helpers ---------------------------------------------------------
    def types(self) -> list[str]:
        return [e.get("type") for e in self.sent]

    def instructions(self) -> list[str]:
        return [
            e["session"]["instructions"]
            for e in self.sent
            if e.get("type") == "session.update" and "instructions" in e.get("session", {})
        ]


class FakeTextModel:
    """Returns queued responses; records the prompts it was asked."""

    def __init__(self, responses: list[str] | None = None, *, delay_s: float = 0.0) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str]] = []
        self.delay_s = delay_s

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        self.calls.append((system, user))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if not self.responses:
            return '{"tool": null}'
        return self.responses.pop(0)


class ExplodingTextModel:
    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        raise OSError("upstream text model unreachable")


class Collector:
    """Captures everything sent toward the client."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, event: dict) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.get("type") for e in self.events]

    def of_type(self, t: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == t]
