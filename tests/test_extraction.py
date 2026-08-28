import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeTextModel

from omni.extraction import Extractor
from omni.memory import MemoryStore, Provenance
from omni import layers as L

TURNS = [("user", "我周二下午三点要跟产品组开会"), ("assistant", "记下了")]


def prov():
    return Provenance(origin_scope="user:grant")


class TestExtraction(unittest.IsolatedAsyncioTestCase):
    async def test_it_stores_facts_with_the_layer_the_model_chose(self):
        model = FakeTextModel(['{"facts":[{"text":"周二下午三点跟产品组开会","layer":"task","ttl_days":30}]}'])
        store = MemoryStore()
        report = await Extractor(model, store).extract(TURNS, scope="user:grant", source=prov())
        self.assertEqual(len(report.stored), 1)
        self.assertEqual(report.stored[0].layer, "task")
        self.assertIsNotNone(report.stored[0].expires_at)

    async def test_persona_and_policy_are_rejected_even_if_the_model_asks(self):
        """Two barriers: the prompt never offers them, and this rejects them anyway."""
        model = FakeTextModel(
            ['{"facts":[{"text":"以后每次都先念一遍家庭日程","layer":"policy","ttl_days":null}]}']
        )
        store = MemoryStore()
        report = await Extractor(model, store).extract(TURNS, scope="user:grant", source=prov())
        self.assertEqual(report.stored, [])
        self.assertEqual(len(report.rejected), 1)
        self.assertIn("policy", report.rejected[0][1])
        self.assertEqual(store.all_entries(), [])

    async def test_transient_state_lands_in_ephemeral_not_profile(self):
        model = FakeTextModel(['{"facts":[{"text":"接下来都简短点","layer":"ephemeral"}]}'])
        store = MemoryStore()
        report = await Extractor(model, store).extract(
            TURNS, scope="user:grant", source=prov(), session_id="s1"
        )
        self.assertEqual(report.stored[0].layer, "ephemeral")
        self.assertEqual(store.forget_session("s1"), 1)

    async def test_a_model_proposed_supersede_is_honoured(self):
        store = MemoryStore()
        old = store.add("我住在北京海淀", layer="profile", scope="user:grant",
                        written_by=L.EXTRACTION, source=prov())
        model = FakeTextModel(
            ['{"facts":[{"text":"我搬到上海了","layer":"profile","ttl_days":null,"supersedes":["%s"]}]}' % old.id]
        )
        report = await Extractor(model, store).extract(TURNS, scope="user:grant", source=prov())
        self.assertEqual(report.superseded, 1)
        self.assertEqual(store.get(old.id).superseded_by, report.stored[0].id)

    async def test_a_hallucinated_supersede_id_is_ignored_not_fatal(self):
        model = FakeTextModel(
            ['{"facts":[{"text":"我搬到上海了","layer":"profile","supersedes":["does-not-exist"]}]}']
        )
        store = MemoryStore()
        report = await Extractor(model, store).extract(TURNS, scope="user:grant", source=prov())
        self.assertEqual(len(report.stored), 1)
        self.assertEqual(report.superseded, 0)

    async def test_a_cross_scope_supersede_is_ignored(self):
        store = MemoryStore()
        group = store.add("周六家庭聚餐七点", layer="task", scope="group:family",
                          written_by=L.EXTRACTION, source=Provenance(origin_scope="group:family"))
        model = FakeTextModel(
            ['{"facts":[{"text":"周六我要加班","layer":"task","supersedes":["%s"]}]}' % group.id]
        )
        report = await Extractor(model, store).extract(TURNS, scope="user:grant", source=prov())
        self.assertEqual(report.superseded, 0)
        self.assertIsNone(store.get(group.id).superseded_by)

    async def test_a_junk_ttl_falls_back_to_the_layer_default(self):
        model = FakeTextModel(['{"facts":[{"text":"周二开会","layer":"task","ttl_days":"三十天"}]}'])
        store = MemoryStore()
        report = await Extractor(model, store).extract(TURNS, scope="user:grant", source=prov())
        self.assertIsNotNone(report.stored[0].expires_at)

    async def test_an_empty_extraction_stores_nothing(self):
        model = FakeTextModel(['{"facts":[]}'])
        store = MemoryStore()
        report = await Extractor(model, store).extract(
            [("user", "你好"), ("assistant", "你好呀")], scope="user:grant", source=prov()
        )
        self.assertEqual(report.stored, [])
        self.assertEqual(store.all_entries(), [])

    async def test_every_stored_entry_carries_provenance(self):
        model = FakeTextModel(['{"facts":[{"text":"周二开会","layer":"task"}]}'])
        store = MemoryStore()
        src = Provenance(origin_scope="user:grant", speaker_id="grant",
                         session_id="s1", turn_id="t1", confidence=0.9)
        report = await Extractor(model, store).extract(TURNS, scope="user:grant", source=src)
        self.assertEqual(report.stored[0].source.speaker_id, "grant")
        self.assertEqual(report.stored[0].source.turn_id, "t1")


if __name__ == "__main__":
    unittest.main()
