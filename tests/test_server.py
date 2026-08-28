import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import AioHTTPTestCase

from omni import layers as L
from omni.config import Config
from omni.memory import MemoryStore, Provenance
from omni.server import make_app


class TestMemoryApi(AioHTTPTestCase):
    async def get_application(self):
        self.store = MemoryStore()
        return make_app(Config(user_scope="user:test"), self.store)

    async def test_config_exposes_the_layer_model(self):
        body = await (await self.client.get("/api/config")).json()
        names = [l["name"] for l in body["layers"]]
        self.assertEqual(names, [s.name for s in L.ORDERED_LAYERS])
        human_only = {l["name"] for l in body["layers"] if l["humanOnly"]}
        self.assertEqual(human_only, {"persona", "policy"})

    async def test_a_human_can_write_persona(self):
        """Human-write-only layers still need a write path, or they are unusable."""
        resp = await self.client.post("/api/memory", json={"layer": "persona", "text": "说话简短"})
        self.assertEqual(resp.status, 200)
        listed = await (await self.client.get("/api/memory")).json()
        self.assertEqual([e["layer"] for e in listed], ["persona"])
        self.assertEqual(listed[0]["writtenBy"], "human")

    async def test_an_unknown_layer_is_a_400_not_a_500(self):
        resp = await self.client.post("/api/memory", json={"layer": "nope", "text": "x"})
        self.assertEqual(resp.status, 400)

    async def test_empty_text_is_rejected(self):
        resp = await self.client.post("/api/memory", json={"layer": "profile", "text": "   "})
        self.assertEqual(resp.status, 400)

    async def test_superseded_entries_are_hidden_by_default_but_available_for_audit(self):
        old = self.store.add("我住北京", layer="profile", scope="user:test",
                             written_by=L.HUMAN, source=Provenance(origin_scope="user:test"))
        await self.client.post("/api/memory", json={
            "layer": "profile", "text": "我搬到上海了", "supersedes": [old.id]})
        live = await (await self.client.get("/api/memory")).json()
        self.assertEqual([e["text"] for e in live], ["我搬到上海了"])
        audit = await (await self.client.get("/api/memory?include_superseded=1")).json()
        self.assertEqual(len(audit), 2)

    async def test_other_scopes_are_not_listed(self):
        self.store.add("别人的记忆", layer="profile", scope="user:someone-else",
                       written_by=L.HUMAN, source=Provenance(origin_scope="user:someone-else"))
        listed = await (await self.client.get("/api/memory")).json()
        self.assertEqual(listed, [])


if __name__ == "__main__":
    unittest.main()
