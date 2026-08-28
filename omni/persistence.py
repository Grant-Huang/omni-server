"""SQLite persistence for MemoryStore.

Explicitly the lightest thing that closes the "restart and it's gone" gap, not a
step toward an ORM. The brief was "MVP 用，后面需要时再换" -- so this is optimised for
zero setup (stdlib ``sqlite3``, one file, no server to run) and for being easy to
throw away, not for scaling. The swap-out point is deliberately narrow: everything
outside this module talks to ``MemoryStore``, never to SQL, through the
``PersistHook`` protocol defined in ``omni.memory`` -- replacing this file with a
Postgres-backed implementation of the same two methods is the entire migration.

Write-through, not batched: ``on_add``/``on_supersede`` fire synchronously inside
``MemoryStore.add()``/``supersede()``, one row at a time. That is the right trade for
the volumes this is built for (one household's memory, not a firehose) -- correctness
with no write-behind buffer to lose on a crash, at the cost of a write no longer being
"free" the way an in-memory dict's is. If that trade stops being right, that is the
signal to move off SQLite, not to make this module more clever.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .memory import MemoryEntry, Provenance

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_entries (
    id              TEXT PRIMARY KEY,
    text            TEXT NOT NULL,
    layer           TEXT NOT NULL,
    scope           TEXT NOT NULL,
    written_by      TEXT NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    expires_at      REAL,
    superseded_by   TEXT,
    session_id      TEXT,
    origin_scope    TEXT NOT NULL,
    speaker_id      TEXT,
    source_session_id TEXT,
    turn_id         TEXT,
    confidence      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_entries(scope);
"""


class SqliteMemoryPersistence:
    """Owns one SQLite file. Not thread-safe beyond what a single asyncio process
    doing synchronous writes on the event loop thread already gets for free -- if
    omni-server ever grows worker threads or processes touching the same store, this
    needs a real connection strategy, which is exactly the kind of complexity this
    module is deliberately deferring."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- PersistHook protocol --------------------------------------------------------
    def on_add(self, entry: MemoryEntry) -> None:
        if entry.layer == "ephemeral":
            # Session-bound by definition (docs/memory-design.md §2): no session from a
            # previous process is still open to bind it to on reload, so a persisted
            # ephemeral row would just be inert garbage forever. Simplest fix is to
            # never write it.
            return
        src = entry.source
        self._conn.execute(
            "INSERT OR REPLACE INTO memory_entries "
            "(id, text, layer, scope, written_by, created_at, updated_at, expires_at, "
            " superseded_by, session_id, origin_scope, speaker_id, source_session_id, "
            " turn_id, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry.id, entry.text, entry.layer, entry.scope, entry.written_by,
                entry.created_at, entry.updated_at, entry.expires_at,
                entry.superseded_by, entry.session_id,
                src.origin_scope, src.speaker_id, src.session_id, src.turn_id, src.confidence,
            ),
        )
        self._conn.commit()

    def on_supersede(self, old_id: str, by_id: str, updated_at: float) -> None:
        self._conn.execute(
            "UPDATE memory_entries SET superseded_by = ?, updated_at = ? WHERE id = ?",
            (by_id, updated_at, old_id),
        )
        self._conn.commit()

    # -- loading ----------------------------------------------------------------------
    def load_all(self) -> list[MemoryEntry]:
        """Everything on disk, reconstructed as MemoryEntry objects. Feeds
        ``MemoryStore.restore()`` at startup -- never ``add()``, which would re-run
        conflict detection on decisions that were already made."""
        rows = self._conn.execute(
            "SELECT id, text, layer, scope, written_by, created_at, updated_at, "
            "expires_at, superseded_by, session_id, origin_scope, speaker_id, "
            "source_session_id, turn_id, confidence FROM memory_entries"
        ).fetchall()
        out = []
        for row in rows:
            (eid, text, layer, scope, written_by, created_at, updated_at, expires_at,
             superseded_by, session_id, origin_scope, speaker_id, source_session_id,
             turn_id, confidence) = row
            out.append(MemoryEntry(
                id=eid, text=text, layer=layer, scope=scope, written_by=written_by,
                created_at=created_at, updated_at=updated_at, expires_at=expires_at,
                superseded_by=superseded_by, session_id=session_id,
                source=Provenance(
                    origin_scope=origin_scope, speaker_id=speaker_id,
                    session_id=source_session_id, turn_id=turn_id, confidence=confidence,
                ),
            ))
        return out
