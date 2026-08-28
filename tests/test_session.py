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
    async def test_a_turn_patches_then_creates_a_response(self):
        s, up, _ = build_session()
        await s.begin_turn("周二有什么安排")
        self.assertEqual(up.types(), ["session.update", "response.create"])
        self.assertEqual(s.stats["patches_sent"], 1)

    async def test_an_identical_rebuild_skips_the_patch_entirely(self):
        """Two consecutive turns that retrieve nothing produce byte-identical
        instructions. The existing client re-sends anyway, paying a ~0.3s ack round trip
        for a no-op; this must not."""
        s, up, _ = build_session()
        await s.begin_turn("你好")
        await s.begin_turn("嗯")
        self.assertEqual(up.types(), ["session.update", "response.create", "response.create"])
        self.assertEqual(s.stats["patches_sent"], 1)
        self.assertEqual(s.stats["patches_skipped"], 1)

    async def test_changed_memory_forces_a_fresh_patch(self):
        store = MemoryStore()
        s, up, _ = build_session(store=store)
        await s.begin_turn("周二有什么安排")
        store.add("周二下午三点开会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        await s.begin_turn("周二有什么安排")
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
            self.assertIn("response.create", up.types())
            self.assertEqual(s.stats["acks_timed_out"], 1)
            self.assertIsNone(s.patcher.last_sent)
            await s.begin_turn("周二有什么安排")
            self.assertEqual(s.stats["patches_sent"], 2)  # re-sent, not skipped
        finally:
            rt.ACK_TIMEOUT_S = original


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
        await s.turn.sidecar_task
        self.assertEqual(
            up.types(),
            ["session.update", "response.create", "response.cancel", "session.update", "response.create"],
        )
        self.assertIn("今天下午三点有个会", up.instructions()[-1])
        self.assertEqual(s.stats["self_interrupts"], 1)

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
        clock.advance(SELF_INTERRUPT_WINDOW_S + 1)
        await s.turn.sidecar_task
        self.assertNotIn("response.cancel", up.types())
        self.assertIsNotNone(s.turn.pending_append)
        await s.handle_upstream_event({"type": "response.done"})
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
        first = s.turn
        await s.begin_turn("第二个完全不同的问题")
        await asyncio.sleep(0.1)
        self.assertTrue(first.sidecar_task.cancelled() or first.sidecar_task.done())
        self.assertEqual(sink.of_type("omni.tool_result"), [])
        self.assertNotIn("response.cancel", up.types())

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
        self.assertEqual(up.types(), ["session.update", "response.create"])
        self.assertEqual(sink.of_type("omni.tool_result"), [])

    async def test_a_failing_text_model_degrades_to_no_lookup(self):
        """The voice model is already answering. A broken sidecar must cost a vaguer
        answer, never a dropped turn."""
        sidecar = Sidecar(ExplodingTextModel(), {"stub": await tool_returning("x")})
        s, up, sink = build_session(sidecar=sidecar)
        await s.begin_turn("我今天有什么安排")
        await s.turn.sidecar_task
        self.assertEqual(up.types(), ["session.update", "response.create"])
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
