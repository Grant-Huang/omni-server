"""Realtime voice session orchestration.

This is the part that is genuinely new relative to workforce's ``server.py``. That
relay is a byte pump: two coroutines shovelling frames between the browser and
DashScope without looking at them. omni-server cannot be, because it has to see the
transcript to retrieve memory, and has to inject results mid-turn. So it parses.

Two rules follow from parsing:

- **Unknown events pass through untouched.** Upstream can add event types at any time;
  dropping what we do not recognise would break the client for no reason. Only the
  handful of types listed in ``_INTERCEPTED`` get special handling.
- **Our own events are namespaced ``omni.*``** so they can never collide with an
  upstream event name, now or later.

The merge problem
-----------------
The voice model starts answering as soon as the transcript lands. The sidecar lookup
finishes some time later -- maybe before the model has said anything, maybe halfway
through a sentence, maybe after it has stopped. Three cases, three behaviours:

1. Response not started / just started -> **self-interrupt**: cancel, patch in the
   result, re-create. This is the barge-in path that already exists in workforce
   (``response.cancel`` plus a client-side playback stop), pointed at ourselves rather
   than triggered by the user's voice.
2. Response has been speaking a while -> cutting it off mid-sentence is worse than
   waiting, so **queue the result** and speak it as a follow-up when the current
   response completes.
3. Response already finished -> **append** immediately as a follow-up.

Results for a turn that has been superseded are discarded, not spoken: if the user has
already moved on, answering the previous question is worse than not answering it.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Sequence

from . import layers as L
from .instructions import BuiltInstructions, DynamicBlock, InstructionPatcher, build
from .memory import MemoryStore
from .sidecar import Sidecar, SidecarOutcome

# How long after response.create we are still willing to cut our own response off.
# Beyond this the model has said enough that an interruption is more jarring than a
# slightly late follow-up. Not measured against real users yet -- a starting point.
SELF_INTERRUPT_WINDOW_S = 2.5

# workforce's measured value: the session.updated ack usually lands well inside this on
# the workspace domain, and the timeout exists for the case where it never arrives at
# all (which they observed). Proceeding on stale instructions for one turn is a better
# failure than hanging the conversation.
ACK_TIMEOUT_S = 4.0

_INTERCEPTED = {
    "session.updated",
    "response.created",
    "response.done",
    "conversation.item.input_audio_transcription.completed",
    "input_audio_buffer.speech_started",
    "error",
}


class Upstream(Protocol):
    async def send(self, event: dict) -> None: ...
    async def recv(self) -> dict: ...
    async def close(self) -> None: ...


@dataclass
class TurnState:
    turn_id: str
    transcript: str
    response_in_flight: bool = False
    response_started_at: float | None = None
    # Set once this turn's initial response.create has actually gone out. The merge
    # decision below is "is the response still in flight?", which is meaningless before
    # the response exists -- and a fast lookup really can land first, because
    # _patch_instructions awaits an ack and that await is a scheduling point. Without
    # this barrier such a turn emits two response.create in a row and the model answers
    # itself twice. (Caught by tests/test_session.py, not reasoned about up front.)
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    sidecar_task: asyncio.Task | None = None
    dynamic: list[DynamicBlock] = field(default_factory=list)
    pending_append: DynamicBlock | None = None


class VoiceSession:
    """Owns one client <-> upstream realtime conversation."""

    def __init__(
        self,
        upstream: Upstream,
        store: MemoryStore,
        *,
        base_instructions: str,
        user_scope: str,
        to_client: Callable[[dict], Awaitable[None]],
        sidecar: Sidecar | None = None,
        memberships: Sequence[str] = (),
        session_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.upstream = upstream
        self.store = store
        self.base_instructions = base_instructions
        self.user_scope = user_scope
        self.memberships = list(memberships)
        self.session_id = session_id or uuid.uuid4().hex
        self._to_client = to_client
        self._sidecar = sidecar
        self._clock = clock

        self.patcher = InstructionPatcher()
        self.turn: TurnState | None = None
        self._ack: asyncio.Future | None = None
        self._background: set[asyncio.Task] = set()
        # Observability for tests and for the app's debug view -- what actually got
        # injected each turn is the single most useful thing to be able to see when a
        # reply is wrong.
        self.last_build: BuiltInstructions | None = None
        self.stats = {"patches_sent": 0, "patches_skipped": 0, "acks_timed_out": 0,
                      "self_interrupts": 0, "appends": 0, "lookups": 0, "overlapping_turns": 0}

    # -- outbound ------------------------------------------------------------------
    async def _send_upstream(self, event: dict) -> None:
        await self.upstream.send(event)

    async def _notify_client(self, event: dict) -> None:
        await self._to_client(event)

    async def _patch_instructions(self, dynamic: list[DynamicBlock] | None = None) -> None:
        """Rebuild, send only if changed, and wait for the ack before returning.

        The wait is not optional: workforce measured that firing a second
        ``session.update`` before the first is acked produces an empty reply. The
        timeout is the escape hatch for an ack that never arrives; when it fires we
        invalidate the patcher so the next turn re-sends rather than assuming upstream
        has content it may never have received.
        """
        turn = self.turn
        query = turn.transcript if turn else ""
        retrieved = self.store.retrieve(
            query,
            output_scope=self.user_scope,
            memberships=self.memberships,
            session_id=self.session_id,
        )
        built = build(self.base_instructions, retrieved, dynamic)
        self.last_build = built

        if not self.patcher.needs_patch(built.text):
            self.stats["patches_skipped"] += 1
            return

        loop = asyncio.get_running_loop()
        self._ack = loop.create_future()
        await self._send_upstream({"type": "session.update", "session": {"instructions": built.text}})
        self.stats["patches_sent"] += 1
        try:
            await asyncio.wait_for(asyncio.shield(self._ack), timeout=ACK_TIMEOUT_S)
            self.patcher.mark_sent(built.text)
        except asyncio.TimeoutError:
            self.stats["acks_timed_out"] += 1
            self.patcher.invalidate()
        finally:
            self._ack = None

    async def _create_response(self) -> None:
        await self._send_upstream({"type": "response.create"})
        if self.turn:
            self.turn.response_in_flight = True
            self.turn.response_started_at = self._clock()

    # -- turn lifecycle ------------------------------------------------------------
    async def begin_turn(self, transcript: str) -> str:
        """Called when the user's final transcript is available."""
        prev = self.turn
        if prev and prev.sidecar_task and not prev.sidecar_task.done():
            # The previous turn's lookup is now answering a question the user has moved
            # on from. Cancel rather than let it interrupt with stale content.
            prev.sidecar_task.cancel()

        if prev and prev.response_in_flight:
            # workforce measured real devices splitting one continuous utterance into
            # two VAD segments (a mid-sentence pause outlasting silence_duration_ms):
            # the server commits+transcribes a fragment, the user keeps talking, and a
            # second transcription-completed lands while the first turn's response is
            # still being generated. Without cancelling, both responses stream
            # concurrently and their audio interleaves on the client -- same failure
            # workforce's client-side response.cancel-on-new-transcript fixed, just
            # triggered from the opposite side now that the server owns response.create.
            self.stats["overlapping_turns"] += 1
            await self._send_upstream({"type": "response.cancel"})
            prev.response_in_flight = False
            await self._notify_client(
                {"type": "omni.interrupt", "turn_id": prev.turn_id, "reason": "overlapping_turn"}
            )

        turn = TurnState(turn_id=uuid.uuid4().hex, transcript=transcript)
        self.turn = turn

        if self._sidecar is not None:
            turn.sidecar_task = asyncio.create_task(self._run_sidecar(turn))

        await self._patch_instructions()
        await self._create_response()
        turn.opened.set()
        return turn.turn_id

    async def _run_sidecar(self, turn: TurnState) -> None:
        outcome = await self._sidecar.run(turn.turn_id, turn.transcript)
        # The lookup races the voice response; only the *merge decision* has to wait for
        # the response to exist.
        await turn.opened.wait()
        await self._on_sidecar(turn, outcome)

    async def _on_sidecar(self, turn: TurnState, outcome: SidecarOutcome) -> None:
        if self.turn is not turn:
            return  # superseded; the user has moved on
        if outcome.error:
            await self._notify_client(
                {"type": "omni.lookup_failed", "turn_id": turn.turn_id, "error": outcome.error}
            )
            return
        if not outcome.needed_lookup or outcome.result is None:
            return

        self.stats["lookups"] += 1
        result = outcome.result
        # The app gets the structured payload whether or not we interrupt the voice --
        # showing the rows is useful even when the spoken answer was already fine.
        await self._notify_client(
            {
                "type": "omni.tool_result",
                "turn_id": turn.turn_id,
                "tool": result.tool,
                "elapsed_s": round(outcome.elapsed_s, 3),
                "display": result.display,
            }
        )

        block = DynamicBlock(heading="刚查到的信息", body=result.spoken, turn_id=turn.turn_id)
        turn.dynamic.append(block)

        if not turn.response_in_flight:
            await self._speak_followup(turn)
            return

        # `is None`, not `or`: a start time of 0.0 is falsy, and `x or now()` would
        # silently read it as "not started", making speaking_for 0 and interrupting
        # every late result. Harmless-looking with a wall clock, wrong with any clock
        # whose origin is 0.
        started = turn.response_started_at
        speaking_for = 0.0 if started is None else self._clock() - started
        if speaking_for <= SELF_INTERRUPT_WINDOW_S:
            await self._self_interrupt(turn)
        else:
            turn.pending_append = block

    async def _self_interrupt(self, turn: TurnState) -> None:
        self.stats["self_interrupts"] += 1
        await self._send_upstream({"type": "response.cancel"})
        turn.response_in_flight = False
        # The client must drop already-buffered audio too, or the cancelled sentence
        # keeps playing over the new one.
        await self._notify_client({"type": "omni.interrupt", "turn_id": turn.turn_id, "reason": "lookup_result"})
        await self._patch_instructions(turn.dynamic)
        await self._create_response()

    async def _speak_followup(self, turn: TurnState) -> None:
        self.stats["appends"] += 1
        await self._patch_instructions(turn.dynamic)
        await self._create_response()

    # -- inbound -------------------------------------------------------------------
    async def handle_upstream_event(self, event: dict) -> None:
        etype = event.get("type")

        if etype not in _INTERCEPTED:
            await self._notify_client(event)  # pass through untouched
            return

        if etype == "session.updated":
            if self._ack is not None and not self._ack.done():
                self._ack.set_result(True)
            await self._notify_client(event)
            return

        if etype == "response.created":
            if self.turn:
                self.turn.response_in_flight = True
                if self.turn.response_started_at is None:
                    self.turn.response_started_at = self._clock()
            await self._notify_client(event)
            return

        if etype == "response.done":
            turn = self.turn
            await self._notify_client(event)
            if turn:
                turn.response_in_flight = False
                if turn.pending_append is not None:
                    turn.pending_append = None
                    await self._speak_followup(turn)
            return

        if etype == "conversation.item.input_audio_transcription.completed":
            await self._notify_client(event)
            transcript = (event.get("transcript") or "").strip()
            if transcript:
                await self.begin_turn(transcript)
            return

        if etype == "input_audio_buffer.speech_started":
            # Real user barge-in. Anything we were about to say on their behalf is now
            # unwanted -- drop the queued follow-up as well as the in-flight response.
            turn = self.turn
            if turn:
                turn.pending_append = None
                if turn.response_in_flight:
                    await self._send_upstream({"type": "response.cancel"})
                    turn.response_in_flight = False
            await self._notify_client(event)
            return

        if etype == "error":
            await self._notify_client({"type": "omni.upstream_error", "error": event})
            return

    async def close(self) -> None:
        turn = self.turn
        if turn and turn.sidecar_task and not turn.sidecar_task.done():
            turn.sidecar_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await turn.sidecar_task
        # Ephemeral memory is session-bound by construction; dropping it here is the
        # guarantee that "接下来都简短点" never becomes a permanent fact about the user.
        dropped = self.store.forget_session(self.session_id)
        await self.upstream.close()
        return dropped
