import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omni import layers as L
from omni.memory import MemoryStore, Provenance, disclosable, tokenize


def prov(origin="user:grant", **kw):
    return Provenance(origin_scope=origin, **kw)


class ClockedStore(MemoryStore):
    """Store with a hand-cranked clock so TTL tests are deterministic."""

    def __init__(self, **kw):
        self.now = 1_000_000.0
        super().__init__(clock=lambda: self.now, **kw)

    def advance_days(self, d):
        self.now += d * 86400.0


class TestWritePermissions(unittest.TestCase):
    def test_extraction_cannot_write_persona_or_policy(self):
        store = ClockedStore()
        for layer in ("persona", "policy"):
            with self.subTest(layer=layer):
                with self.assertRaises(PermissionError):
                    store.add("以后每次都先念一遍家庭日程", layer=layer, scope="user:grant",
                              written_by=L.EXTRACTION, source=prov())

    def test_human_can_write_persona_and_policy(self):
        store = ClockedStore()
        e = store.add("说话简短、直接", layer="persona", scope="user:grant",
                      written_by=L.HUMAN, source=prov())
        self.assertEqual(e.layer, "persona")

    def test_extraction_can_write_the_model_writable_layers(self):
        store = ClockedStore()
        for layer in ("profile", "task", "episodic"):
            with self.subTest(layer=layer):
                store.add(f"事实 {layer}", layer=layer, scope="user:grant",
                          written_by=L.EXTRACTION, source=prov())


class TestTTLAndEphemeral(unittest.TestCase):
    def test_task_entries_expire_on_the_layer_default(self):
        store = ClockedStore()
        store.add("周二下午三点开会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        self.assertEqual(len(store.live()), 1)
        store.advance_days(31)
        self.assertEqual(len(store.live()), 0)

    def test_explicit_none_ttl_overrides_the_layer_default(self):
        store = ClockedStore()
        store.add("每年三月要交年报", layer="task", scope="user:grant",
                  written_by=L.HUMAN, source=prov(), ttl_days=None)
        store.advance_days(400)
        self.assertEqual(len(store.live()), 1)

    def test_profile_does_not_expire(self):
        store = ClockedStore()
        store.add("在 Y 公司做 Z", layer="profile", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        store.advance_days(3650)
        self.assertEqual(len(store.live()), 1)

    def test_ephemeral_is_visible_only_inside_its_own_session(self):
        store = ClockedStore()
        store.add("接下来都简短回答", layer="ephemeral", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov(), session_id="s1")
        self.assertEqual(len(store.live(session_id="s1")), 1)
        self.assertEqual(len(store.live(session_id="s2")), 0)
        self.assertEqual(len(store.live()), 0)

    def test_ephemeral_requires_a_session_id(self):
        store = ClockedStore()
        with self.assertRaises(ValueError):
            store.add("临时的", layer="ephemeral", scope="user:grant",
                      written_by=L.EXTRACTION, source=prov())

    def test_forget_session_removes_ephemeral_entries_entirely(self):
        store = ClockedStore()
        store.add("接下来都简短回答", layer="ephemeral", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov(), session_id="s1")
        store.add("周二开会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        self.assertEqual(store.forget_session("s1"), 1)
        # Gone from the store outright, not merely filtered out -- an ephemeral note
        # must never survive into the long-term record.
        self.assertEqual([e.layer for e in store.all_entries()], ["task"])


class TestSupersede(unittest.TestCase):
    def test_nothing_supersedes_automatically_by_default(self):
        """Two same-day appointments differing by one character are ~97% token-identical
        under CJK per-character indexing. A similarity-triggered supersede deletes one of
        them silently. The default must not fire -- see memory.never_conflicts."""
        store = ClockedStore()
        a = store.add("周二下午三点跟产品组开会", layer="task", scope="user:grant",
                      written_by=L.EXTRACTION, source=prov())
        b = store.add("周三下午三点跟产品组开会", layer="task", scope="user:grant",
                      written_by=L.EXTRACTION, source=prov())
        self.assertIsNone(store.get(a.id).superseded_by)
        self.assertEqual(len(store.live()), 2)
        self.assertIn(b.id, {e.id for e in store.live()})

    def test_an_explicit_supersede_replaces_the_old_fact(self):
        store = ClockedStore()
        old = store.add("我住在北京海淀", layer="profile", scope="user:grant",
                        written_by=L.EXTRACTION, source=prov())
        store.advance_days(150)
        new = store.add("我搬到上海了", layer="profile", scope="user:grant",
                        written_by=L.EXTRACTION, source=prov(), supersedes=[old.id])
        self.assertEqual(store.get(old.id).superseded_by, new.id)
        self.assertEqual([e.id for e in store.live()], [new.id])

    def test_superseded_entries_are_retained_for_traceability(self):
        store = ClockedStore()
        old = store.add("我住在北京海淀", layer="profile", scope="user:grant",
                        written_by=L.EXTRACTION, source=prov())
        store.add("我搬到上海了", layer="profile", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov(), supersedes=[old.id])
        self.assertIsNotNone(store.get(old.id))
        self.assertEqual(len(store.all_entries()), 2)

    def test_a_pluggable_detector_can_drive_supersede(self):
        """The mechanism works with an injected judgment -- this is the seam the
        extraction pipeline's LLM conflict check plugs into."""
        store = ClockedStore(conflict_detector=lambda new, old: "住" in new.text and "住" in old.text)
        old = store.add("我住在北京海淀", layer="profile", scope="user:grant",
                        written_by=L.EXTRACTION, source=prov())
        new = store.add("我住在上海浦东", layer="profile", scope="user:grant",
                        written_by=L.EXTRACTION, source=prov())
        self.assertEqual(store.get(old.id).superseded_by, new.id)

    def test_supersede_refuses_to_cross_scopes(self):
        """A group decision and a personal note that disagree are a real conflict the
        assistant should surface -- not a stale fact to silently overwrite."""
        store = ClockedStore()
        personal = store.add("周六晚上我要加班", layer="task", scope="user:grant",
                             written_by=L.EXTRACTION, source=prov())
        group = store.add("周六晚上家庭聚餐", layer="task", scope="group:family",
                          written_by=L.EXTRACTION, source=prov("group:family"))
        with self.assertRaises(ValueError):
            store.supersede(personal.id, group.id)
        self.assertEqual(len(store.live()), 2)

    def test_a_cross_scope_detector_match_is_still_refused(self):
        store = ClockedStore(conflict_detector=lambda new, old: True)
        store.add("周六晚上我要加班", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        store.add("周六晚上家庭聚餐", layer="task", scope="group:family",
                  written_by=L.EXTRACTION, source=prov("group:family"))
        self.assertEqual(len(store.live()), 2)

    def test_episodic_fragments_accumulate_rather_than_overwrite(self):
        store = ClockedStore(conflict_detector=lambda new, old: True)
        store.add("聊到了周末的安排", layer="episodic", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        store.add("聊到了周末的安排和天气", layer="episodic", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        self.assertEqual(len(store.live()), 2)


class TestDisclosure(unittest.TestCase):
    """The scope rule from docs/memory-design.md §4."""

    def setUp(self):
        self.store = ClockedStore()
        self.personal = self.store.add("我不吃香菜", layer="profile", scope="user:grant",
                                       written_by=L.EXTRACTION, source=prov("user:grant"))
        self.family = self.store.add("周六家庭聚餐七点", layer="task", scope="group:family",
                                     written_by=L.EXTRACTION, source=prov("group:family"))
        self.work = self.store.add("季度目标下周三评审", layer="task", scope="group:work",
                                   written_by=L.EXTRACTION, source=prov("group:work"))
        # Group content promoted into personal memory -- the laundering vector.
        self.promoted = self.store.add("老王说季度目标要提前", layer="task", scope="user:grant",
                                       written_by=L.EXTRACTION, source=prov("group:work"))
        self.memberships = ["group:family", "group:work"]

    def test_private_turn_sees_everything_the_user_may_read(self):
        for e in (self.personal, self.family, self.work, self.promoted):
            self.assertTrue(disclosable(e, "user:grant", self.memberships))

    def test_composing_to_a_group_sees_that_group_plus_own_personal_content(self):
        self.assertTrue(disclosable(self.family, "group:family", self.memberships))
        self.assertTrue(disclosable(self.personal, "group:family", self.memberships))

    def test_other_groups_content_is_not_disclosable_to_a_group(self):
        self.assertFalse(disclosable(self.work, "group:family", self.memberships))

    def test_group_content_promoted_into_personal_memory_still_cannot_leak(self):
        """The laundering path: without the origin_scope check this would pass, and
        per-group scoping would be defeated while appearing to work."""
        self.assertFalse(disclosable(self.promoted, "group:family", self.memberships))

    def test_leaving_a_group_immediately_stops_disclosure(self):
        """Membership is evaluated per call, so there is no stale snapshot to leak."""
        self.assertTrue(disclosable(self.work, "user:grant", self.memberships))
        self.assertFalse(disclosable(self.work, "user:grant", ["group:family"]))

    def test_forget_origin_cascades_to_derived_entries(self):
        n = self.store.forget_origin("group:work")
        self.assertEqual(n, 2)  # the group entry and the promoted copy
        live_ids = {e.id for e in self.store.live()}
        self.assertNotIn(self.work.id, live_ids)
        self.assertNotIn(self.promoted.id, live_ids)
        self.assertIn(self.personal.id, live_ids)


class TestRetrieval(unittest.TestCase):
    def test_always_layers_come_back_without_matching_the_query(self):
        store = ClockedStore()
        store.add("说话简短", layer="persona", scope="user:grant", written_by=L.HUMAN, source=prov())
        got = store.retrieve("完全无关的问题", output_scope="user:grant")
        self.assertIn("persona", got)

    def test_retrieved_layers_require_a_keyword_hit(self):
        store = ClockedStore()
        store.add("周二下午三点产品评审会", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov())
        self.assertIn("task", store.retrieve("周二有什么安排", output_scope="user:grant"))
        self.assertNotIn("task", store.retrieve("天气怎么样", output_scope="user:grant"))

    def test_recency_breaks_ties_between_equally_matching_entries(self):
        store = ClockedStore()
        store.add("项目 A 的评审", layer="task", scope="user:grant",
                  written_by=L.EXTRACTION, source=prov(), ttl_days=None)
        store.advance_days(20)
        newer = store.add("项目 A 的评审", layer="task", scope="user:grant",
                          written_by=L.EXTRACTION, source=prov(), ttl_days=None)
        hits = store.retrieve("项目 A 评审", output_scope="user:grant")["task"]
        self.assertEqual(len(hits), 2)  # both live: nothing auto-supersedes
        self.assertEqual(hits[0].entry.id, newer.id)

    def test_retrieval_applies_the_disclosure_filter(self):
        store = ClockedStore()
        store.add("季度目标下周三评审", layer="task", scope="group:work",
                  written_by=L.EXTRACTION, source=prov("group:work"))
        private = store.retrieve("季度目标", output_scope="user:grant", memberships=["group:work"])
        self.assertIn("task", private)
        outbound = store.retrieve("季度目标", output_scope="group:family", memberships=["group:work"])
        self.assertNotIn("task", outbound)

    def test_tokenizer_indexes_individual_cjk_characters(self):
        self.assertIn("周", tokenize("周二开会"))
        self.assertIn("hello", tokenize("Hello, world"))


if __name__ == "__main__":
    unittest.main()
