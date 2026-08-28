"""Tests for the two ported one-shot endpoints (server/text_api.py). These mock the
outbound aiohttp.ClientSession.post call itself -- they verify omni-server's own
request/response handling (did the port from workforce/web-demo/server.py preserve
behavior), not Qwen's actual behavior, which is already validated in workforce's
history (see text_api.py's module docstring)."""
from __future__ import annotations

import json

from aiohttp.test_utils import TestClient, TestServer

from server.app import create_app


class _FakeResponse:
    def __init__(self, status, json_body):
        self.status = status
        self._json_body = json_body

    async def json(self):
        return self._json_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response):
        self._response = response

    def post(self, *args, **kwargs):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_upstream_call(monkeypatch, status, json_body):
    fake_response = _FakeResponse(status, json_body)
    monkeypatch.setattr("server.text_api.aiohttp.ClientSession", lambda: _FakeSession(fake_response))


async def test_dictation_cleanup_requires_api_key(make_config):
    app = create_app(make_config(qwen_api_key=""))
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/dictation-cleanup", json={"text": "呃就是那个"})
        assert resp.status == 500


async def test_dictation_cleanup_requires_text(make_config):
    app = create_app(make_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/dictation-cleanup", json={"text": "  "})
        assert resp.status == 400


async def test_dictation_cleanup_success(make_config, monkeypatch):
    _patch_upstream_call(monkeypatch, 200, {"choices": [{"message": {"content": "整理后的文字"}}]})
    app = create_app(make_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/dictation-cleanup", json={"text": "呃就是那个我想说"})
        assert resp.status == 200
        assert (await resp.json())["cleaned"] == "整理后的文字"


async def test_dictation_cleanup_upstream_error_status(make_config, monkeypatch):
    _patch_upstream_call(monkeypatch, 500, {"error": "boom"})
    app = create_app(make_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/dictation-cleanup", json={"text": "hi"})
        assert resp.status == 502


async def test_memory_extract_requires_both_texts(make_config):
    app = create_app(make_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/memory-extract", json={"userText": "hi"})
        assert resp.status == 400


async def test_memory_extract_success(make_config, monkeypatch):
    _patch_upstream_call(
        monkeypatch,
        200,
        {"choices": [{"message": {"content": json.dumps({"facts": [{"text": "用户住上海", "isJargon": False}]})}}]},
    )
    app = create_app(make_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/memory-extract", json={"userText": "我搬到上海了", "assistantText": "好的"})
        assert resp.status == 200
        body = await resp.json()
        assert body["facts"] == [{"text": "用户住上海", "isJargon": False}]


async def test_memory_extract_rejects_non_json_model_output(make_config, monkeypatch):
    _patch_upstream_call(monkeypatch, 200, {"choices": [{"message": {"content": "not json"}}]})
    app = create_app(make_config())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/memory-extract", json={"userText": "a", "assistantText": "b"})
        assert resp.status == 502
