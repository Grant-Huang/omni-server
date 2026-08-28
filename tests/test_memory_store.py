from __future__ import annotations

import time

from server.memory_store import MemoryStore


def test_add_rejects_too_short_text(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    assert store.add("a") is None
    assert store.all() == []


def test_add_and_persist_round_trip(tmp_path):
    path = tmp_path / "memory.json"
    store = MemoryStore(path)
    entry = store.add("周二下午3点开会", layer="task")
    assert entry is not None
    assert entry["text"] == "周二下午3点开会"
    assert entry["layer"] == "task"

    # A fresh store reading the same file should see the persisted entry.
    reloaded = MemoryStore(path)
    assert [e["text"] for e in reloaded.all()] == ["周二下午3点开会"]


def test_search_scores_by_overlap_and_recency(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    now = time.time() * 1000
    # Bypass add()'s "now" timestamp to control recency directly.
    store._entries = [
        {"id": "old", "text": "周二下午3点开会，讨论产品评审", "timestamp": now - 30 * 86400000, "layer": None},
        {"id": "new", "text": "周四上午10点财务预算会议", "timestamp": now - 1 * 86400000, "layer": None},
        {"id": "unrelated", "text": "今天天气不错", "timestamp": now, "layer": None},
    ]

    results = store.search("我周二有什么安排", limit=5)
    ids = [e["id"] for e in results]
    assert "old" in ids  # shares 周二/会 tokens
    assert "unrelated" not in ids  # no token overlap at all


def test_search_with_no_matching_tokens_returns_empty(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    store.add("我爱人叫小明")
    assert store.search("今天天气怎么样") == []


def test_search_respects_limit(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    for i in range(10):
        store.add(f"记录{i}：开会安排")
    results = store.search("开会安排", limit=3)
    assert len(results) == 3
