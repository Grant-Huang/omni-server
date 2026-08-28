"""Persistent single-user memory: JSON-file-backed, keyword-overlap + recency search.

This is the server-side counterpart of workforce/web-demo/static/memory.js, ported to
use the *same* scoring algorithm (overlap + 1/(1+ageDays)) on purpose -- that algorithm
is already validated against real usage in web-demo, and MVP isn't the place to also
change the algorithm while also changing where it lives.

Deliberately not wired into the /ws relay yet (docs/mvp-plan.md section 2.1/section 5):
this module and its /api/memory routes exist so that step is just "call into an
already-tested store" later, not "design the store" later. No layers, no TTL, no
supersede -- design-risks-review.md section 7's full model is out of scope until this
actually needs to serve more than one client of a single user.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Optional

_TOKEN_SPLIT_RE = re.compile(r"[\s,.!?;:，。！？；：]+")


def _tokenize(text: str) -> set:
    tokens = set()
    for word in _TOKEN_SPLIT_RE.split(text.lower()):
        if not word:
            continue
        tokens.add(word)
        for ch in word:
            if ord(ch) > 0x7F:  # CJK etc.: also index individual characters, mirrors memory.js
                tokens.add(ch)
    return tokens


class MemoryStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = Lock()
        self._entries = self._load()

    def _load(self) -> list:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._entries, ensure_ascii=False, indent=2), encoding="utf-8")

    def add(self, text: str, layer: Optional[str] = None) -> Optional[dict]:
        trimmed = (text or "").strip()
        if len(trimmed) < 2:
            return None
        entry = {
            "id": str(uuid.uuid4()),
            "text": trimmed,
            "timestamp": time.time() * 1000,  # ms epoch, matches memory.js's Date.now()
            "layer": layer,
        }
        with self._lock:
            self._entries.append(entry)
            self._persist()
        return entry

    def search(self, query: str, limit: int = 5) -> list:
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        now = time.time() * 1000
        with self._lock:
            entries = list(self._entries)
        scored = []
        for entry in entries:
            entry_tokens = _tokenize(entry["text"])
            overlap = len(query_tokens & entry_tokens)
            if overlap == 0:
                continue
            age_days = max(now - entry["timestamp"], 0) / 86400000
            recency_boost = 1 / (1 + age_days)
            scored.append((overlap + recency_boost, entry))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for _, entry in scored[:limit]]

    def all(self) -> list:
        with self._lock:
            entries = list(self._entries)
        return sorted(entries, key=lambda e: e["timestamp"], reverse=True)

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._persist()
