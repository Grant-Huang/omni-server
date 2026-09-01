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
from .diagnostics import log_upstream_event, log_session_event, log_client_send, log_error, log_debug
from .instructions import BuiltInstructions, DynamicBlock, InstructionPatcher, build
from .memory import MemoryStore, Retrieved
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

# How much of the user's partial transcript to wait for before firing a speculative
# patch (docs/design-risks-review.md §8c). Too low wastes a round trip on "嗯"/"那个"
# false starts; too high leaves less of the utterance to hide the round trip inside.
# Not measured against real users yet -- a starting point, same status as
# SELF_INTERRUPT_WINDOW_S above.
SPECULATIVE_MIN_CHARS = 6

_INTERCEPTED = {
    "session.updated",
    "response.created",
    "response.done",
    "conversation.item.input_audio_transcription.delta",
    "conversation.item.input_audio_transcription.completed",
    "input_audio_buffer.speech_started",
    "error",
}


def _render_ambient(retrieved: dict[str, list[Retrieved]]) -> str:
    """What the RETRIEVED layers (task/episodic/shared) already matched for this
    turn's query, rendered for the sidecar router (Sidecar.run's ``already_known``).

    Before this existed, the ambient layers and the sidecar both called
    ``MemoryStore.retrieve`` independently and could both decide the same fact was
    worth surfacing -- the router had no way to know the voice model's instructions
    already carried it, and would trigger a self-interrupt or follow-up for
    information that wasn't new. Telling it what's already there lets it say
    "not needed" instead."""
    lines = [
        r.entry.text
        for lspec in L.ORDERED_LAYERS
        if lspec.injection == L.RETRIEVED
        for r in (retrieved.get(lspec.name) or [])
    ]
    return "\n".join(f"- {t}" for t in lines)


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
        # Serializes the send-and-await-ack section of _patch_instructions. Required,
        # not defensive: workforce measured that firing a second session.update before
        # the first is acked produces an empty reply (see _patch_instructions), and with
        # warm_start + speculative injection + the per-turn patch all now able to fire
        # close together, overlap is the common case rather than a rare race.
        self._patch_lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()
        # Speculative-injection state (docs/design-risks-review.md §8c): accumulated
        # from ASR delta events while the user is still talking, reset per utterance.
        self._partial_transcript = ""
        self._speculative_fired = False
        # Observability for tests and for the app's debug view -- what actually got
        # injected each turn is the single most useful thing to be able to see when a
        # reply is wrong.
        self.last_build: BuiltInstructions | None = None
        self.stats = {"patches_sent": 0, "patches_skipped": 0, "acks_timed_out": 0,
                      "self_interrupts": 0, "appends": 0, "lookups": 0, "overlapping_turns": 0,
                      "speculative_patches": 0}

    # -- outbound ------------------------------------------------------------------
    async def _send_upstream(self, event: dict) -> None:
        await self.upstream.send(event)

    async def _notify_client(self, event: dict) -> None:
        try:
            await self._to_client(event)
            log_client_send(event.get("type", "unknown"), self.session_id, success=True)
        except Exception as e:
            log_error("_notify_client", str(e), self.session_id, f"event_type={event.get('type')}")
            raise

    def _spawn_background(self, coro) -> asyncio.Task:
        """Run a coroutine in the background, tracked so it can be waited on (tests'
        drain_background) or torn down (close()) instead of leaking an untracked task."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def drain_background(self) -> None:
        """Wait for every currently-tracked background task to finish. Production code
        never needs this -- background patches are deliberately fire-and-forget on the
        critical path -- but tests need a deterministic point to assert from instead of
        guessing how many event-loop ticks a patch takes."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    def start_warm_up(self) -> None:
        """Kick off the stable-layer baseline (persona/policy/profile -- the ALWAYS
        layers, docs/family-app-architecture.md's layers.py) in the background, right
        after connecting. This round trip then overlaps with the client's own
        session-setup wait instead of stacking onto the first turn's latency: by the
        time the user finishes speaking, patcher.last_sent is usually already set to
        the baseline, so the first turn's own patch has nothing new to send and is
        skipped entirely (needs_patch in instructions.py). Call once, right after
        construction."""
        self._spawn_background(self._patch_instructions())

    async def _patch_instructions(
        self,
        dynamic: list[DynamicBlock] | None = None,
        *,
        query: str | None = None,
        retrieved: dict[str, list[Retrieved]] | None = None,
    ) -> None:
        """Rebuild, send only if changed, and wait for the ack before returning.

        ``query`` overrides what drives retrieval. Defaults to the current turn's
        transcript; warm_start passes nothing (only ALWAYS/SESSION layers are
        query-independent, so an empty query still builds them), and speculative
        injection passes the partial transcript accumulated so far, since there is no
        ``self.turn`` yet when it fires.

        ``retrieved`` lets a caller that already ran ``store.retrieve()`` for this
        query hand the result straight through instead of paying for a second,
        redundant call -- ``begin_turn`` does this because it also needs the same
        retrieval to tell the sidecar router what's already ambiently known (see
        ``_render_ambient``). Ignored if ``query`` is also given, since the two would
        otherwise silently disagree about what was actually searched for.

        The wait is not optional: workforce measured that firing a second
        ``session.update`` before the first is acked produces an empty reply. The
        timeout is the escape hatch for an ack that never arrives; when it fires we
        invalidate the patcher so the next turn re-sends rather than assuming upstream
        has content it may never have received. The send-and-await section is guarded
        by ``_patch_lock`` for the same reason: warm_start, speculative injection, and
        the per-turn patch can now all be scheduled close together, and only one
        session.update may be in flight at a time.

        Runs in the background (via _spawn_background) rather than blocking
        response.create, to avoid re-adding the ~4-6s this was fixed to remove.
        Instructions patch failures don't block the response; the next turn will
        re-patch if needed.
        """
        try:
            turn = self.turn
            if retrieved is None or query is not None:
                if query is None:
                    query = turn.transcript if turn else ""
                retrieved = self.store.retrieve(
                    query,
                    output_scope=self.user_scope,
                    memberships=self.memberships,
                    session_id=self.session_id,
                )
            built = build(self.base_instructions, retrieved, dynamic)
            self.last_build = built

            async with self._patch_lock:
                # Re-check inside the lock: another call queued ahead of us (e.g.
                # warm_start) may have already sent this exact content while we were
                # computing retrieval, which would otherwise make this a redundant
                # duplicate send.
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
                    log_debug("instructions_patched", self.session_id, "bg_async=true")
                except asyncio.TimeoutError:
                    self.stats["acks_timed_out"] += 1
                    self.patcher.invalidate()
                    log_debug("instructions_patch_timeout", self.session_id, "bg_async=true")
                finally:
                    self._ack = None
        except Exception as e:
            log_error("_patch_instructions_bg", str(e), self.session_id)

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
        # The utterance this transcript came from is over; a stray delta from a
        # different utterance should never bleed into the next one's speculative patch.
        self._partial_transcript = ""
        self._speculative_fired = False

        # Retrieved once, used twice: this is what instructions.build() turns into the
        # RETRIEVED-layer (task/episodic/shared) section below, and also what tells the
        # sidecar router what's already ambiently known (_render_ambient) so it doesn't
        # independently re-decide the same fact is worth a self-interrupt.
        retrieved = self.store.retrieve(
            transcript, output_scope=self.user_scope,
            memberships=self.memberships, session_id=self.session_id,
        )

        if self._sidecar is not None:
            turn.sidecar_task = asyncio.create_task(
                self._run_sidecar(turn, _render_ambient(retrieved))
            )

        # Fire response.create immediately without waiting for _patch_instructions ack.
        # This reduces latency from ASR→response by ~4s (the ACK_TIMEOUT). Instructions
        # will be patched in background; if patch fails, the next turn re-sends. Usually
        # this patch finds nothing new to send: warm_start already covers the ALWAYS
        # layers, and a speculative patch fired while the user was still talking
        # (below) usually already covers the RETRIEVED layers for this query.
        await self._create_response()
        self._spawn_background(self._patch_instructions(retrieved=retrieved))
        turn.opened.set()
        return turn.turn_id

    async def _run_sidecar(self, turn: TurnState, already_known: str) -> None:
        outcome = await self._sidecar.run(turn.turn_id, turn.transcript, already_known=already_known)
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
        # Fire response.create immediately; patch instructions in background.
        await self._create_response()
        self._spawn_background(self._patch_instructions(turn.dynamic))

    async def _speak_followup(self, turn: TurnState) -> None:
        self.stats["appends"] += 1
        # Fire response.create immediately; patch instructions in background.
        await self._create_response()
        self._spawn_background(self._patch_instructions(turn.dynamic))

    # -- inbound -------------------------------------------------------------------
    async def handle_upstream_event(self, event: dict) -> None:
        etype = event.get("type")

        # Diagnostics: log all upstream events
        size = len(str(event)) if event else 0
        log_upstream_event(etype, size=size, session_id=self.session_id)

        if etype not in _INTERCEPTED:
            # Pass through untouched, but log that we're forwarding it to client
            log_session_event(f"forward_{etype}", self.session_id, "→ client")
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

        if etype == "conversation.item.input_audio_transcription.delta":
            # Speculative injection (docs/design-risks-review.md §8c): retrieval is pure
            # in-memory lookup (memory.py:retrieve), so there is no cost to starting it
            # on a partial transcript instead of waiting for speech_stopped -- only the
            # session.update round trip is expensive, and firing it now lets that round
            # trip run concurrently with the rest of the user's utterance instead of
            # stacking onto the silence after they stop. Fired at most once per
            # utterance (reset in begin_turn / speech_started): repeatedly re-patching
            # on every delta would just queue redundant sends behind _patch_lock.
            await self._notify_client(event)
            chunk = event.get("delta") or event.get("transcript") or ""
            if chunk:
                self._partial_transcript += chunk
            if not self._speculative_fired and len(self._partial_transcript) >= SPECULATIVE_MIN_CHARS:
                self._speculative_fired = True
                self.stats["speculative_patches"] += 1
                self._spawn_background(self._patch_instructions(query=self._partial_transcript))
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
            # A new utterance is starting; last one's speculative state doesn't apply.
            self._partial_transcript = ""
            self._speculative_fired = False
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
        # warm_start / speculative / per-turn patches can all still be in flight
        # (awaiting an ack that will never come once upstream is closed below).
        for task in list(self._background):
            task.cancel()
        for task in list(self._background):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Ephemeral memory is session-bound by construction; dropping it here is the
        # guarantee that "接下来都简短点" never becomes a permanent fact about the user.
        dropped = self.store.forget_session(self.session_id)
        await self.upstream.close()
        return dropped
