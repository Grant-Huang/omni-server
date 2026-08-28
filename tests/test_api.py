from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from server.app import create_app


async def test_config_endpoint_reports_key_presence(make_config):
    app = create_app(make_config(qwen_api_key="k", qwen_workspace_id=""))
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/config")
        assert resp.status == 200
        body = await resp.json()
        assert body["hasKey"] is True
        assert body["hasWorkspaceId"] is False
        assert body["voice"] == "Serena"
        assert isinstance(body["voices"], list) and len(body["voices"]) > 0


async def test_health_endpoint(make_config):
    app = create_app(make_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert (await resp.json())["status"] == "ok"


async def test_memory_add_then_search_round_trip(make_config):
    app = create_app(make_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/memory", json={"text": "周二下午3点开会"})
        assert resp.status == 201
        created = await resp.json()
        assert created["text"] == "周二下午3点开会"

        resp = await client.get("/api/memory/search", params={"q": "周二有什么安排"})
        assert resp.status == 200
        body = await resp.json()
        assert any(e["id"] == created["id"] for e in body["entries"])


async def test_memory_add_rejects_empty_text(make_config):
    app = create_app(make_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/memory", json={"text": "  "})
        assert resp.status == 400


async def test_memory_search_requires_query(make_config):
    app = create_app(make_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/memory/search")
        assert resp.status == 400


async def test_memory_list_returns_all_entries_newest_first(make_config):
    app = create_app(make_config())
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/memory", json={"text": "第一条"})
        await client.post("/api/memory", json={"text": "第二条"})
        resp = await client.get("/api/memory")
        body = await resp.json()
        assert [e["text"] for e in body["entries"]] == ["第二条", "第一条"]


async def test_cors_headers_present_for_configured_origin(make_config):
    app = create_app(make_config(cors_origins=("https://example.com",)))
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/config", headers={"Origin": "https://example.com"})
        assert resp.headers["Access-Control-Allow-Origin"] == "https://example.com"


async def test_cors_headers_absent_for_unlisted_origin(make_config):
    app = create_app(make_config(cors_origins=("https://example.com",)))
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/config", headers={"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in resp.headers
