import asyncio, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes import Collector, ExplodingTextModel, FakeTextModel, FakeUpstream

from omni import layers as L
from omni.memory import MemoryStore, Provenance
from omni.realtime import SELF_INTERRUPT_WINDOW_S, VoiceSession
from omni.sidecar import Sidecar, ToolResult, memory_search_tool

BASE = "你是一个语音助手。"


def prov():
    return Provenance(origin_scope="user:grant")


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


async def tool_returning(text, *, empty=False, delay_s=0.0):
    async def run(query):
        if delay_s:
            await asyncio.sleep(delay_s)
        return ToolResult(tool="stub", query=query, spoken=text,
                          display={"kind": "stub", "text": text}, empty=empty)
    return run


def build_session(*, store=None, sidecar=None, auto_ack=True, clock=None, memberships=()):
    store = store or MemoryStore()
    up = FakeUpstream(auto_ack=auto_ack)
    sink = Collector()
    s = VoiceSession(up, store, base_instructions=BASE, user_scope="user:grant",
                     to_client=sink, sidecar=sidecar, memberships=memberships,
                     session_id="sess-1", clock=clock or FakeClock())
    up.bind(s)
    return s, up, sink


class TestTurnBasics(unittest.IsolatedAsyncioTestCase):
    async def test_begin_turn_retrieves_once_shares_it_with_the_patch(self):
        """begin_turn computes retrieval once (for _render_ambient) and hands that
        same result to _patch_instructions via the retrieved= kwarg -- it must not
        pay for a second, redundant MemoryStore.retrieve() call for the same query."""
        store = MemoryStore()
        store.add("周二下午三点开会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        calls = []
        original_retrieve = store.retrieve

        def counting_retrieve(*args, **kwargs):
            calls.append((args, kwargs))
            return original_retrieve(*args, **kwargs)

        store.retrieve = counting_retrieve
        s, up, _ = build_session(store=store)
        await s.begin_turn("周二有什么安排")
        await s.drain_background()
        self.assertEqual(len(calls), 1)
        self.assertIn("周二下午三点开会", up.instructions()[-1])

    async def test_a_turn_patches_then_creates_a_response(self):
        """response.create goes out first -- that's the whole point of the dfe2cba
        latency fix -- and the patch that follows is a background task, so the test has
        to drain it before asserting instead of assuming it already ran."""
        s, up, _ = build_session()
        await s.begin_turn("周二有什么安排")
        await s.drain_background()
        self.assertEqual(up.types(), ["response.create", "session.update"])
        self.assertEqual(s.stats["patches_sent"], 1)

    async def test_an_identical_rebuild_skips_the_patch_entirely(self):
        """Two consecutive turns that retrieve nothing produce byte-identical
        instructions. The existing client re-sends anyway, paying a ~0.3s ack round trip
        for a no-op; this must not."""
        s, up, _ = build_session()
        await s.begin_turn("你好")
        await s.drain_background()
        # First turn's response finishing before the second begins keeps this test
        # about patch-skipping specifically, decoupled from the overlapping-turn
        # cancellation covered separately below.
        await s.handle_upstream_event({"type": "response.done"})
        await s.begin_turn("嗯")
        await s.drain_background()
        self.assertEqual(up.types(), ["response.create", "session.update", "response.create"])
        self.assertEqual(s.stats["patches_sent"], 1)
        self.assertEqual(s.stats["patches_skipped"], 1)

    async def test_changed_memory_forces_a_fresh_patch(self):
        store = MemoryStore()
        s, up, _ = build_session(store=store)
        await s.begin_turn("周二有什么安排")
        await s.drain_background()
        store.add("周二下午三点开会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        await s.begin_turn("周二有什么安排")
        await s.drain_background()
        self.assertEqual(s.stats["patches_sent"], 2)
        self.assertIn("周二下午三点开会", up.instructions()[-1])

    async def test_a_missing_ack_does_not_hang_the_turn(self):
        """workforce observed session.update vanishing with no error and no ack. The
        turn must still complete, and the next turn must re-send rather than assume
        upstream has instructions it may never have received."""
        import omni.realtime as rt
        original = rt.ACK_TIMEOUT_S
        rt.ACK_TIMEOUT_S = 0.01
        try:
            s, up, _ = build_session(auto_ack=False)
            await s.begin_turn("周二有什么安排")
            await s.drain_background()
            self.assertIn("response.create", up.types())
            self.assertEqual(s.stats["acks_timed_out"], 1)
            self.assertIsNone(s.patcher.last_sent)
            await s.begin_turn("周二有什么安排")
            await s.drain_background()
            self.assertEqual(s.stats["patches_sent"], 2)  # re-sent, not skipped
        finally:
            rt.ACK_TIMEOUT_S = original

    async def test_a_second_transcript_before_the_first_response_finishes_cancels_it(self):
        """workforce measured real devices splitting one continuous utterance into two
        VAD segments -- a second transcription-completed can land while the first
        turn's response is still generating. Without cancelling, both responses stream
        concurrently and interleave on the client."""
        s, up, sink = build_session()
        await s.begin_turn("前半句")
        await s.drain_background()
        await s.handle_upstream_event({"type": "response.created"})
        await s.begin_turn("前半句 后半句")
        await s.drain_background()
        # Both turns retrieve nothing, so the rebuilt instructions are byte-identical
        # and the second session.update is skipped (test_an_identical_rebuild_skips_
        # the_patch_entirely covers that path) -- what's under test here is only that
        # the overlap gets cancelled.
        self.assertEqual(
            up.types(),
            ["response.create", "session.update", "response.cancel", "response.create"],
        )
        self.assertEqual(s.stats["overlapping_turns"], 1)
        self.assertEqual(sink.of_type("omni.interrupt")[0]["reason"], "overlapping_turn")

    async def test_a_turn_after_the_previous_response_already_finished_does_not_cancel(self):
        s, up, _ = build_session()
        await s.begin_turn("第一句")
        await s.handle_upstream_event({"type": "response.done"})
        await s.begin_turn("第二句")
        self.assertNotIn("response.cancel", up.types())
        self.assertEqual(s.stats["overlapping_turns"], 0)


class TestPassthrough(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_upstream_events_reach_the_client_untouched(self):
        s, _, sink = build_session()
        event = {"type": "response.audio.delta", "delta": "AAA", "future_field": 1}
        await s.handle_upstream_event(event)
        self.assertEqual(sink.events, [event])

    async def test_a_transcript_event_starts_a_turn(self):
        s, up, sink = build_session()
        await s.handle_upstream_event({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "周二有什么安排",
        })
        self.assertIn("response.create", up.types())
        self.assertIn("conversation.item.input_audio_transcription.completed", sink.types())

    async def test_upstream_errors_are_relabelled_not_swallowed(self):
        s, _, sink = build_session()
        await s.handle_upstream_event({"type": "error", "code": "rate_limited"})
        self.assertEqual(sink.types(), ["omni.upstream_error"])
        self.assertEqual(sink.events[0]["error"]["code"], "rate_limited")


class TestLookupMerge(unittest.IsolatedAsyncioTestCase):
    async def _session_with_lookup(self, spoken="今天下午三点有个会", clock=None):
        sidecar = Sidecar(
            FakeTextModel(['{"tool": "stub", "query": "今天安排"}']),
            {"stub": await tool_returning(spoken)},
        )
        return build_session(sidecar=sidecar, clock=clock or FakeClock())

    async def test_a_result_arriving_early_interrupts_our_own_response(self):
        s, up, sink = await self._session_with_lookup()
        await s.begin_turn("我今天有什么安排")
        # The turn's own patch (background) and the sidecar lookup are two independent
        # tasks racing concurrently -- unlike response.cancel/response.create inside
        # _self_interrupt itself, which stay strictly ordered because they're sequential
        # awaits in one coroutine, there's no guarantee the turn's own session.update
        # lands before or after the sidecar resolves. So this asserts what actually
        # matters (both patches eventually go out, self-interrupt cancels and
        # re-creates, the final instructions carry the result) rather than one exact
        # global interleaving.
        await s.turn.sidecar_task
        await s.drain_background()
        types = up.types()
        self.assertEqual(types.count("response.create"), 2)
        self.assertEqual(types.count("response.cancel"), 1)
        self.assertEqual(types.count("session.update"), 2)
        # cancel must still fall strictly between the two response.create calls --
        # that ordering is guaranteed (see comment above), unlike the patches' position.
        last_create = len(types) - 1 - types[::-1].index("response.create")
        self.assertLess(types.index("response.cancel"), last_create)
        self.assertIn("今天下午三点有个会", up.instructions()[-1])

    async def test_begin_turn_tells_the_sidecar_what_is_already_ambiently_known(self):
        """Before this, the RETRIEVED layers (ambient, keyword-matched) and the
        sidecar's router (explicit, LLM-judged) called MemoryStore.retrieve
        independently and could both decide the same fact was worth surfacing -- the
        router had no way to know the voice model's instructions already carried it."""
        store = MemoryStore()
        store.add("周二下午三点开会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        text_model = FakeTextModel(['{"tool": null}'])
        sidecar = Sidecar(text_model, {"stub": await tool_returning("x")})
        s, up, _ = build_session(store=store, sidecar=sidecar)
        await s.begin_turn("周二有什么安排")
        await s.turn.sidecar_task
        router_prompt = text_model.calls[-1][0]
        self.assertIn("周二下午三点开会", router_prompt)

    async def test_no_ambient_hit_renders_as_the_placeholder(self):
        text_model = FakeTextModel(['{"tool": null}'])
        sidecar = Sidecar(text_model, {"stub": await tool_returning("x")})
        s, up, _ = build_session(sidecar=sidecar)
        await s.begin_turn("今天天气不错啊")
        await s.turn.sidecar_task
        router_prompt = text_model.calls[-1][0]
        self.assertIn("（无）", router_prompt)

    async def test_the_client_is_told_to_drop_buffered_audio_on_self_interrupt(self):
        """Cancelling upstream is not enough -- audio already buffered on the client
        keeps playing over the replacement answer."""
        s, _, sink = await self._session_with_lookup()
        await s.begin_turn("我今天有什么安排")
        await s.turn.sidecar_task
        self.assertEqual(len(sink.of_type("omni.interrupt")), 1)
        self.assertEqual(sink.of_type("omni.interrupt")[0]["reason"], "lookup_result")

    async def test_a_result_arriving_late_is_queued_and_spoken_after_the_response(self):
        """Cutting the model off two words from the end is worse than a follow-up."""
        clock = FakeClock()
        s, up, sink = await self._session_with_lookup(clock=clock)
        await s.begin_turn("我今天有什么安排")
        # No drain_background() here: it would let the (undelayed) sidecar task race
        # ahead and resolve before the clock advances, making _on_sidecar read
        # speaking_for against the wrong instant and self-interrupt when it shouldn't.
        clock.advance(SELF_INTERRUPT_WINDOW_S + 1)
        await s.turn.sidecar_task
        self.assertNotIn("response.cancel", up.types())
        self.assertIsNotNone(s.turn.pending_append)
        await s.handle_upstream_event({"type": "response.done"})
        await s.drain_background()
        self.assertEqual(s.stats["appends"], 1)
        self.assertIn("今天下午三点有个会", up.instructions()[-1])

    async def test_a_result_arriving_after_the_response_finished_is_appended(self):
        s, up, _ = await self._session_with_lookup()
        s._sidecar = None
        await s.begin_turn("我今天有什么安排")
        await s.handle_upstream_event({"type": "response.done"})
        sidecar = Sidecar(FakeTextModel(['{"tool": "stub", "query": "q"}']),
                          {"stub": await tool_returning("今天下午三点有个会")})
        outcome = await sidecar.run(s.turn.turn_id, "我今天有什么安排")
        await s._on_sidecar(s.turn, outcome)
        self.assertNotIn("response.cancel", up.types())
        self.assertEqual(s.stats["appends"], 1)

    async def test_a_result_for_a_superseded_turn_is_discarded(self):
        """The user has moved on. Answering the previous question now is worse than not
        answering it."""
        # Turn 2's router says "no lookup", so any tool_result that appears can only be
        # turn 1's stale one.
        sidecar = Sidecar(
            FakeTextModel(['{"tool": "stub", "query": "q"}', '{"tool": null}']),
            {"stub": await tool_returning("旧问题的答案", delay_s=0.05)},
        )
        s, up, sink = build_session(sidecar=sidecar)
        await s.begin_turn("第一个问题")
        await s.drain_background()
        first = s.turn
        await s.begin_turn("第二个完全不同的问题")
        await asyncio.sleep(0.1)
        await s.drain_background()
        self.assertTrue(first.sidecar_task.cancelled() or first.sidecar_task.done())
        self.assertEqual(sink.of_type("omni.tool_result"), [])
        # Turn 1's response.create had already gone out (never acked response.done) when
        # turn 2 began, so the overlapping-turn cancellation now cuts it off -- turn 1's
        # own stale lookup result being discarded (asserted above) is a separate thing
        # from this.
        self.assertIn("response.cancel", up.types())

    async def test_the_structured_result_always_reaches_the_app(self):
        """Voice cannot show its sources; the app can. The display payload goes out
        whether or not the spoken answer was interrupted."""
        s, _, sink = await self._session_with_lookup()
        await s.begin_turn("我今天有什么安排")
        await s.turn.sidecar_task
        results = sink.of_type("omni.tool_result")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["display"]["kind"], "stub")
        self.assertIn("elapsed_s", results[0])

    async def test_no_lookup_needed_means_no_extra_upstream_traffic(self):
        sidecar = Sidecar(FakeTextModel(['{"tool": null, "why": "闲聊"}']), {"stub": await tool_returning("x")})
        s, up, sink = build_session(sidecar=sidecar)
        await s.begin_turn("今天天气不错啊")
        await s.turn.sidecar_task
        await s.drain_background()
        self.assertEqual(up.types(), ["response.create", "session.update"])
        self.assertEqual(sink.of_type("omni.tool_result"), [])

    async def test_a_failing_text_model_degrades_to_no_lookup(self):
        """The voice model is already answering. A broken sidecar must cost a vaguer
        answer, never a dropped turn."""
        sidecar = Sidecar(ExplodingTextModel(), {"stub": await tool_returning("x")})
        s, up, sink = build_session(sidecar=sidecar)
        await s.begin_turn("我今天有什么安排")
        await s.turn.sidecar_task
        await s.drain_background()
        self.assertEqual(up.types(), ["response.create", "session.update"])
        self.assertEqual(len(sink.of_type("omni.lookup_failed")), 1)


class TestBargeIn(unittest.IsolatedAsyncioTestCase):
    async def test_user_barge_in_cancels_the_response_and_drops_a_queued_followup(self):
        clock = FakeClock()
        sidecar = Sidecar(FakeTextModel(['{"tool": "stub", "query": "q"}']),
                          {"stub": await tool_returning("迟到的查询结果")})
        s, up, _ = build_session(sidecar=sidecar, clock=clock)
        await s.begin_turn("我今天有什么安排")
        clock.advance(SELF_INTERRUPT_WINDOW_S + 1)
        await s.turn.sidecar_task
        self.assertIsNotNone(s.turn.pending_append)
        await s.handle_upstream_event({"type": "input_audio_buffer.speech_started"})
        self.assertIsNone(s.turn.pending_append)
        self.assertEqual(up.types().count("response.cancel"), 1)
        await s.handle_upstream_event({"type": "response.done"})
        self.assertEqual(s.stats["appends"], 0)


class TestMemorySearchTool(unittest.IsolatedAsyncioTestCase):
    async def test_it_finds_matching_entries_and_reports_them_to_the_app(self):
        store = MemoryStore()
        store.add("周二下午三点产品评审会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        tool = memory_search_tool(store, output_scope="user:grant")
        result = await tool("周二 安排")
        self.assertFalse(result.empty)
        self.assertIn("产品评审会", result.spoken)
        self.assertEqual(len(result.display["hits"]), 1)

    async def test_an_empty_result_instructs_the_model_to_admit_it(self):
        """The failure workforce measured: with nothing retrieved the model invented an
        answer. An empty lookup has to say so explicitly rather than inject nothing."""
        tool = memory_search_tool(MemoryStore(), output_scope="user:grant")
        result = await tool("年假")
        self.assertTrue(result.empty)
        self.assertIn("没有找到", result.spoken)
        self.assertEqual(result.display["hits"], [])

    async def test_the_tool_cannot_see_past_the_disclosure_filter(self):
        """A lookup tool must not be a privilege escalation path."""
        store = MemoryStore()
        store.add("季度目标下周三评审", layer="task", scope="group:work",
                  written_by=L.EXTRACTION, source=Provenance(origin_scope="group:work"))
        outbound = memory_search_tool(store, output_scope="group:family", memberships=["group:work"])
        self.assertTrue((await outbound("季度目标")).empty)
        private = memory_search_tool(store, output_scope="user:grant", memberships=["group:work"])
        self.assertFalse((await private("季度目标")).empty)


class TestWarmStartAndSpeculation(unittest.IsolatedAsyncioTestCase):
    """docs/design-risks-review.md §8: (c) send the ALWAYS-layer baseline before any
    turn begins, and (b) start retrieval on the partial transcript instead of waiting
    for speech_stopped, so these round trips overlap with time the user is already
    spending (connecting, or still talking) instead of stacking onto the gap between
    "user stops talking" and "AI starts answering"."""

    async def test_warm_start_pre_seeds_the_baseline_so_the_first_turn_has_nothing_new(self):
        s, up, _ = build_session()
        s.start_warm_up()
        await s.drain_background()
        self.assertEqual(up.types(), ["session.update"])
        self.assertEqual(s.stats["patches_sent"], 1)

        await s.begin_turn("你好")
        await s.drain_background()
        # response.create went out; the turn's own patch found nothing new to say
        # (warm_start already covers the ALWAYS layers) and was skipped.
        self.assertEqual(up.types(), ["session.update", "response.create"])
        self.assertEqual(s.stats["patches_sent"], 1)
        self.assertEqual(s.stats["patches_skipped"], 1)

    async def test_speculative_patch_fires_on_a_partial_transcript(self):
        store = MemoryStore()
        store.add("周二下午三点开会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        s, up, sink = build_session(store=store)
        await s.handle_upstream_event({"type": "input_audio_buffer.speech_started"})
        await s.handle_upstream_event({
            "type": "conversation.item.input_audio_transcription.delta", "delta": "周二有什么安排",
        })
        await s.drain_background()
        self.assertEqual(s.stats["speculative_patches"], 1)
        self.assertEqual(up.types(), ["session.update"])
        self.assertIn("周二下午三点开会", up.instructions()[-1])
        # The delta still reaches the client -- speculative injection observes it, it
        # doesn't consume it.
        self.assertIn("conversation.item.input_audio_transcription.delta", sink.types())

        # By the time the user actually finishes, the round trip already happened: the
        # final transcript retrieves the same content, so the per-turn patch in
        # begin_turn has nothing new to send.
        await s.handle_upstream_event({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "周二有什么安排",
        })
        await s.drain_background()
        self.assertEqual(up.types(), ["session.update", "response.create"])
        self.assertEqual(s.stats["patches_skipped"], 1)

    async def test_speculative_patch_fires_at_most_once_per_utterance(self):
        s, up, _ = build_session()
        await s.handle_upstream_event({"type": "input_audio_buffer.speech_started"})
        await s.handle_upstream_event({
            "type": "conversation.item.input_audio_transcription.delta", "delta": "周二有什么安",
        })
        await s.handle_upstream_event({
            "type": "conversation.item.input_audio_transcription.delta", "delta": "排",
        })
        await s.drain_background()
        self.assertEqual(s.stats["speculative_patches"], 1)

    async def test_a_short_delta_below_the_threshold_does_not_fire(self):
        s, up, _ = build_session()
        await s.handle_upstream_event({"type": "input_audio_buffer.speech_started"})
        await s.handle_upstream_event({
            "type": "conversation.item.input_audio_transcription.delta", "delta": "嗯",
        })
        await s.drain_background()
        self.assertEqual(s.stats["speculative_patches"], 0)
        self.assertEqual(up.types(), [])

    async def test_speech_started_resets_speculative_state_for_the_next_utterance(self):
        s, up, _ = build_session()
        await s.handle_upstream_event({"type": "input_audio_buffer.speech_started"})
        await s.handle_upstream_event({
            "type": "conversation.item.input_audio_transcription.delta", "delta": "第一句话的内容",
        })
        await s.drain_background()
        self.assertEqual(s.stats["speculative_patches"], 1)

        await s.handle_upstream_event({"type": "input_audio_buffer.speech_started"})
        await s.handle_upstream_event({
            "type": "conversation.item.input_audio_transcription.delta", "delta": "第二句话的内容",
        })
        await s.drain_background()
        self.assertEqual(s.stats["speculative_patches"], 2)

    async def test_concurrent_patches_do_not_cross_wire_their_acks(self):
        """Regression test for _patch_lock: warm_start, speculative injection, and the
        per-turn patch can now all be scheduled close together. Without the lock,
        handle_upstream_event routes an incoming session.updated to whatever self._ack
        currently points at -- so a second call's ack can resolve the first call's
        future (or vice versa), leaving the other one to sit until ACK_TIMEOUT_S even
        though its own ack genuinely arrived. Verified this reproduces (one call times
        out) with the lock swapped for a no-op before adding this test."""
        store = MemoryStore()
        store.add("周二下午三点开会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        store.add("周三下午五点体检", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        s, up, _ = build_session(store=store)
        s._spawn_background(s._patch_instructions(query="周二安排"))
        s._spawn_background(s._patch_instructions(query="周三安排"))
        await s.drain_background()
        self.assertEqual(s.stats["patches_sent"], 2)
        self.assertEqual(s.stats["acks_timed_out"], 0)
        self.assertIn("周二下午三点开会", up.instructions()[0])
        self.assertIn("周三下午五点体检", up.instructions()[-1])


class TestSessionClose(unittest.IsolatedAsyncioTestCase):
    async def test_closing_drops_ephemeral_memory_and_the_upstream(self):
        store = MemoryStore()
        store.add("接下来都简短点", layer="ephemeral", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov(), session_id="sess-1")
        store.add("周二开会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        s, up, _ = build_session(store=store)
        dropped = await s.close()
        self.assertEqual(dropped, 1)
        self.assertTrue(up.closed)
        self.assertEqual([e.layer for e in store.all_entries()], ["task"])


if __name__ == "__main__":
    unittest.main()
