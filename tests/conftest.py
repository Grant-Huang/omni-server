from __future__ import annotations

import socket

import pytest

from server.config import Config


@pytest.fixture
def make_config(tmp_path):
    """Builds a Config with sane test defaults; pass overrides as kwargs (most tests
    only need to override qwen_ws_base_shared and/or qwen_api_key)."""

    def _make(**overrides) -> Config:
        defaults = dict(
            qwen_api_key="test-key",
            qwen_workspace_id="",
            qwen_ws_base_shared="ws://127.0.0.1:1",  # deliberately unreachable unless overridden
            qwen_model="qwen3.5-omni-flash-realtime",
            qwen_voice="Serena",
            qwen_text_model="qwen-turbo",
            host="127.0.0.1",
            port=0,
            cors_origins=("*",),
            memory_path=tmp_path / "memory.json",
        )
        defaults.update(overrides)
        return Config(**defaults)

    return _make


@pytest.fixture
def unused_tcp_port() -> int:
    """A port nothing is listening on, for testing the relay's upstream-unreachable
    path. Bind-then-close rather than a hardcoded number -- avoids clashing with
    anything else running on the machine running the tests."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
