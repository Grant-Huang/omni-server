import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp.test_utils import AioHTTPTestCase

from omni import layers as L
from omni.config import Config
from omni.memory import MemoryStore, Provenance
from omni.server import _client_text_turn, make_app


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


class TestCors(AioHTTPTestCase):
    """omni/web-demo's fetch("/api/config") is cross-origin against omni-server
    (docs/roadmap.md's "接上 omni 客户端") -- without these headers the request still
    reaches the server, but the browser hides the response from the page's own JS."""

    async def get_application(self):
        return make_app(Config(user_scope="user:test", cors_origins=("https://example.com",)))

    async def test_allowed_origin_gets_cors_headers(self):
        resp = await self.client.get("/api/config", headers={"Origin": "https://example.com"})
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "https://example.com")

    async def test_unlisted_origin_gets_no_cors_headers(self):
        resp = await self.client.get("/api/config", headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", resp.headers)


class TestClientTextTurn(unittest.TestCase):
    """_client_text_turn is what lets a typed (not spoken) message trigger the same
    memory-injection turn as a voice ASR completion (docs/roadmap.md's "接上 omni 客户端"
    item) -- voice_ws has no ASR step to hang that trigger off of for text, so it has to
    recognize the client's own conversation.item.create instead."""

    def test_extracts_text_from_a_user_message_item(self):
        event = {
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "你好"}]},
        }
        self.assertEqual(_client_text_turn(event), "你好")

    def test_joins_multiple_input_text_parts(self):
        event = {
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "第一段"},
                {"type": "input_text", "text": "第二段"},
            ]},
        }
        self.assertEqual(_client_text_turn(event), "第一段 第二段")

    def test_ignores_non_item_create_events(self):
        self.assertIsNone(_client_text_turn({"type": "input_audio_buffer.append", "audio": "xxx"}))

    def test_ignores_assistant_role_items(self):
        event = {
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "assistant", "content": [{"type": "input_text", "text": "hi"}]},
        }
        self.assertIsNone(_client_text_turn(event))

    def test_ignores_audio_content_items(self):
        event = {
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [{"type": "input_audio", "audio": "xxx"}]},
        }
        self.assertIsNone(_client_text_turn(event))

    def test_ignores_empty_text(self):
        event = {
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "   "}]},
        }
        self.assertIsNone(_client_text_turn(event))


if __name__ == "__main__":
    unittest.main()
