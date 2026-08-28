"""Layered memory store.

Pure stdlib and pure in-process on purpose: v0 is single-user, and the point of this
module is to pin down the *data model* before there is any real data, because three of
its properties are effectively impossible to retrofit (docs/memory-design.md §7):

1. **Provenance** (``origin_scope`` / ``speaker_id`` / ``turn_id``). Needed for cascade
   invalidation when someone leaves a group, for honouring deletion requests against
   derived facts, and for attribution. You cannot reconstruct it later.
2. **Supersede**. Facts change ("我住北京" -> "我搬到上海了"). Retrieval that scores on
   keyword overlap plus a recency bonus will happily return *both* -- the overlap term
   dominates the recency term -- and a model handed two contradictory facts produces a
   coin flip. Append-only stores cannot be upgraded to this after the fact, because you
   can no longer tell which historical pairs were supersessions.
3. **Scope**, on every entry, from the first write -- see ``disclosable`` below.

Only ``user:`` scopes are actually written in v0; group plumbing is present and tested
so that turning groups on is a matter of writing entries, not reworking retrieval.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Sequence

from . import layers as L

DAY_SECONDS = 86400.0


@dataclass(frozen=True)
class Provenance:
    """Where a memory came from. Every field here answers a question we cannot answer
    later if we did not record it now."""

    origin_scope: str            # the scope the content was *first said in*
    speaker_id: str | None = None   # who said it (None = unknown, e.g. imported)
    session_id: str | None = None
    turn_id: str | None = None
    confidence: float = 1.0      # speaker attribution confidence, see docs §8


@dataclass
class MemoryEntry:
    id: str
    text: str
    layer: str
    scope: str                   # owning domain: "user:<id>" or "group:<id>"
    source: Provenance
    written_by: str              # L.HUMAN | L.EXTRACTION
    created_at: float
    updated_at: float
    expires_at: float | None = None
    superseded_by: str | None = None
    session_id: str | None = None   # set for ephemeral entries only

    def is_live(self, now: float, session_id: str | None = None) -> bool:
        if self.superseded_by is not None:
            return False
        if self.expires_at is not None and self.expires_at <= now:
            return False
        if self.layer == "ephemeral":
            # Session-bound rather than clock-bound: an ephemeral entry is live only
            # inside the session that created it, and nowhere else, ever.
            return session_id is not None and self.session_id == session_id
        return True


# --- tokenisation ------------------------------------------------------------------
# Ported from workforce's web-demo/static/memory.js so retrieval behaviour is familiar
# rather than subtly different: split on whitespace/punctuation, and additionally index
# each individual CJK character, since Chinese has no spaces to split on.
_SPLIT = re.compile(r"[\s,.!?;:，。！？；：、（）()\[\]「」【】]+")


def tokenize(text: str) -> set[str]:
    tokens: set[str] = set()
    for word in _SPLIT.split(text.lower()):
        if not word:
            continue
        tokens.add(word)
        for ch in word:
            if ord(ch) > 127:
                tokens.add(ch)
    return tokens


# --- conflict detection ------------------------------------------------------------
# The *mechanism* (supersede on write) is what matters and is what is tested; which
# detector is plugged in is a tuning decision. ConflictDetector is (new, existing) ->
# bool, called only for entries in the same scope and layer -- see `_conflicts`.
ConflictDetector = Callable[[MemoryEntry, MemoryEntry], bool]


def never_conflicts(new: MemoryEntry, existing: MemoryEntry) -> bool:
    """The default, and deliberately so.

    Automatic supersede is **asymmetrically dangerous**: a false negative leaves two
    facts in the store and the assistant may waffle, which the user can correct in one
    sentence. A false positive silently deletes a fact the user told us, and neither
    side ever finds out. So the bar for firing automatically has to be high.

    Text similarity does not clear that bar, and measurably so. Because ``tokenize``
    indexes every CJK character individually (it must -- Chinese has no spaces), two
    genuinely different facts that share a sentence shape score as near-identical:
    "周二下午三点跟产品组开会" and "周三下午三点跟产品组开会" differ in one character and
    overlap on ~97% of tokens. A similarity detector deletes one of them. This was not a
    hypothesis -- it is what the first version of this module did, and what
    tests/test_instructions.py caught.

    Conflict is not a similarity question. Two facts conflict when they assert different
    values for the *same subject and attribute*, which needs to be understood, not
    measured. So: nothing supersedes automatically by default. Supersede happens when a
    caller says so (``add(..., supersedes=[...])`` / ``supersede()``), which is where
    the extraction pipeline puts an LLM judgment -- affordable there because writes are
    off the conversation critical path.
    """
    return False


def overlap_conflict_detector(threshold: float = 0.6) -> ConflictDetector:
    """Similarity-based detector. **Not suitable for CJK text** -- see
    ``never_conflicts`` for why. Kept because it makes the supersede *mechanism*
    testable with a deterministic trigger, and it is reasonable for whitespace-delimited
    languages at a high threshold. Do not make it the default."""

    def detect(new: MemoryEntry, existing: MemoryEntry) -> bool:
        a, b = tokenize(new.text), tokenize(existing.text)
        if not a or not b:
            return False
        return len(a & b) / min(len(a), len(b)) >= threshold

    return detect


# --- disclosure --------------------------------------------------------------------
def disclosable(entry: MemoryEntry, output_scope: str, memberships: Sequence[str]) -> bool:
    """Can this entry be put in front of the model for a turn whose output lands in
    ``output_scope``?

    This is the rule that stops cross-group leakage, and it is deliberately about the
    *destination* of the turn rather than about what the reader is allowed to see. Those
    two are different sets, and conflating them is the leak (docs/memory-design.md §4):
    the user is entitled to know what was said in group A, but a message being composed
    for group B must not be able to quote it.

    Three cases:

    - ``output_scope`` is the user's own private turn (``user:<id>``): everything the
      user can read is fair game -- their personal memory plus every group they are
      currently in. Nothing leaves the room.
    - ``output_scope`` is a group: entries owned by that same group are fine, and so is
      personal memory that *originated* personally.
    - Personal memory whose ``origin_scope`` is some *other* group is NOT disclosable to
      a group. Without this clause, personal memory becomes a laundering path -- group A
      content promoted into the user's personal layer, then quoted into group B -- which
      would defeat per-group scoping entirely while looking like it worked.

    ``memberships`` is passed in per call rather than cached on the entry, because
    membership must be evaluated *now*: an entry from a group the user has left is not
    disclosable any more, and a precomputed snapshot would keep leaking it.
    """
    if output_scope.startswith("user:"):
        if entry.scope == output_scope:
            return True
        return entry.scope in memberships
    # output_scope is a group
    if entry.scope == output_scope:
        return True
    if entry.scope.startswith("user:"):
        return entry.source.origin_scope == entry.scope
    return False


@dataclass
class Retrieved:
    entry: MemoryEntry
    score: float


class PersistHook:
    """Optional write-through hook. ``None`` (the default) keeps MemoryStore exactly
    what it always was: pure in-memory, zero dependencies, safe to construct by the
    hundred in a test file. Only a caller that wires an actual hook (omni.persistence)
    pays for persistence, and the store itself stays ignorant of *how* -- it calls
    ``on_add``/``on_supersede`` and does not know or care that SQLite is on the other
    end."""

    def on_add(self, entry: MemoryEntry) -> None: ...

    def on_supersede(self, old_id: str, by_id: str, updated_at: float) -> None: ...


class MemoryStore:
    def __init__(
        self,
        conflict_detector: ConflictDetector | None = None,
        clock: Callable[[], float] = time.time,
        persist: PersistHook | None = None,
    ) -> None:
        self._entries: dict[str, MemoryEntry] = {}
        self._detect = conflict_detector or never_conflicts
        self._clock = clock
        self._persist = persist

    # -- writing --------------------------------------------------------------------
    def add(
        self,
        text: str,
        *,
        layer: str,
        scope: str,
        written_by: str,
        source: Provenance,
        ttl_days: float | None = -1.0,
        session_id: str | None = None,
        supersedes: Sequence[str] = (),
    ) -> MemoryEntry:
        """``ttl_days=-1`` means "use the layer default"; ``None`` means "never expires".
        The two are distinct and the sentinel exists so a caller can explicitly request
        a permanent entry in a layer that normally expires."""
        lspec = L.spec(layer)
        if written_by not in lspec.writable_by:
            # The security boundary from docs/memory-design.md §5, enforced here rather
            # than in an extraction prompt: an extraction pipeline that could write
            # persona/policy would let anything said in any conversation rewrite the
            # stable head of the system prompt, and it would only show up next session.
            raise PermissionError(
                f"layer {layer!r} is writable only by {sorted(lspec.writable_by)}, "
                f"not {written_by!r}"
            )
        text = text.strip()
        if not text:
            raise ValueError("refusing to store empty memory text")
        if layer == "ephemeral" and not session_id:
            raise ValueError("ephemeral entries must carry a session_id")

        now = self._clock()
        if ttl_days == -1.0:
            ttl_days = lspec.default_ttl_days
        expires_at = None if ttl_days is None else now + ttl_days * DAY_SECONDS
        if layer == "ephemeral":
            expires_at = None  # session-bound, not clock-bound

        entry = MemoryEntry(
            id=uuid.uuid4().hex,
            text=text,
            layer=layer,
            scope=scope,
            source=source,
            written_by=written_by,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            session_id=session_id if layer == "ephemeral" else None,
        )
        self._entries[entry.id] = entry
        if self._persist is not None:
            self._persist.on_add(entry)
        for old in self._conflicts(entry):
            self.supersede(old.id, entry.id)
        for old_id in supersedes:
            self.supersede(old_id, entry.id)
        return entry

    def supersede(self, old_id: str, by_id: str) -> None:
        """Mark ``old_id`` as replaced by ``by_id``. Retrieval stops returning it;
        ``all_entries`` still does, so "when did I tell you that?" stays answerable.

        Refuses to cross scopes, for the reason in ``_conflicts``: a personal note and a
        group decision that disagree are a conflict to surface, not a stale fact to
        overwrite."""
        old, new = self._entries.get(old_id), self._entries.get(by_id)
        if old is None or new is None:
            raise KeyError(f"cannot supersede {old_id!r} by {by_id!r}: unknown entry id")
        if old.scope != new.scope:
            raise ValueError(
                f"refusing to supersede across scopes ({old.scope!r} -> {new.scope!r}); "
                "cross-scope disagreement is a conflict to surface, not a stale fact"
            )
        old.superseded_by = by_id
        old.updated_at = self._clock()
        if self._persist is not None:
            self._persist.on_supersede(old_id, by_id, old.updated_at)

    def _conflicts(self, new: MemoryEntry) -> list[MemoryEntry]:
        """Candidates the new entry supersedes.

        Scoped to the same (scope, layer) deliberately. **Supersede never crosses
        scopes** (docs/memory-design.md §4): if the family group says dinner is at 7 and
        the user's personal memory says they are working late, those are not a stale
        fact and a fresh one -- they are a genuine conflict that the assistant should
        surface to the user. Letting one silently supersede the other would delete
        exactly the signal that makes cross-scope memory worth having.
        """
        if new.layer in ("episodic", "ephemeral"):
            return []  # narrative fragments accumulate; they do not overwrite each other
        now = self._clock()
        return [
            e
            for e in self._entries.values()
            if e.id != new.id
            and e.scope == new.scope
            and e.layer == new.layer
            and e.is_live(now, session_id=new.session_id)
            and self._detect(new, e)
        ]

    def forget_session(self, session_id: str) -> int:
        """Drop a session's ephemeral entries. Called at session end; the guarantee is
        that ephemeral content never reaches the long-term store at all."""
        doomed = [
            e.id
            for e in self._entries.values()
            if e.layer == "ephemeral" and e.session_id == session_id
        ]
        for eid in doomed:
            del self._entries[eid]
        return len(doomed)

    def forget_origin(self, origin_scope: str) -> int:
        """Cascade invalidation: expire everything that originated in a scope the reader
        no longer belongs to. This is the concrete thing provenance buys us."""
        now = self._clock()
        n = 0
        for e in self._entries.values():
            if e.source.origin_scope == origin_scope and e.is_live(now):
                e.expires_at = now
                e.updated_at = now
                n += 1
        return n

    # -- reading --------------------------------------------------------------------
    def live(self, session_id: str | None = None) -> list[MemoryEntry]:
        now = self._clock()
        return [e for e in self._entries.values() if e.is_live(now, session_id)]

    def get(self, entry_id: str) -> MemoryEntry | None:
        return self._entries.get(entry_id)

    def all_entries(self) -> list[MemoryEntry]:
        """Everything, superseded and expired included -- for audit/traceability. The
        assistant never sees this; a user asking "when did I tell you that?" does."""
        return list(self._entries.values())

    def restore(self, entries: Iterable[MemoryEntry]) -> int:
        """Bulk-load already-resolved entries (from omni.persistence) at startup.

        Deliberately bypasses ``add()``: the conflict detection and supersede logic
        there is for *deciding* what supersedes what, and that decision was already
        made and recorded the first time each entry was written. Re-running it on
        reload would be redundant at best -- and with a non-default conflict_detector,
        actively wrong, since it could supersede pairs a human or an LLM judgment
        never asked to link. Ephemeral entries are dropped rather than restored: they
        are session-bound by definition (see MemoryEntry.is_live), and no session from
        a previous process is still open to bind them to."""
        n = 0
        for entry in entries:
            if entry.layer == "ephemeral":
                continue
            self._entries[entry.id] = entry
            n += 1
        return n

    def retrieve(
        self,
        query: str,
        *,
        output_scope: str,
        memberships: Sequence[str] = (),
        session_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, list[Retrieved]]:
        """Per-layer retrieval, returned grouped by layer.

        Grouped rather than a flat ranked list because the budget is per layer
        (docs/memory-design.md §3). A flat top-K would let a busy task day evict the
        persona; keeping the groups separate makes that structurally impossible.

        ``always``-injection layers are returned whole (no query scoring): they are in
        every prompt by definition. ``retrieved`` layers are scored. ``session`` layers
        come back whole but only for the matching session.
        """
        now = self._clock() if now is None else now
        qtokens = tokenize(query)
        out: dict[str, list[Retrieved]] = {}
        for lspec in L.ORDERED_LAYERS:
            candidates = [
                e
                for e in self._entries.values()
                if e.layer == lspec.name
                and e.is_live(now, session_id)
                and disclosable(e, output_scope, memberships)
            ]
            if lspec.injection in (L.ALWAYS, L.SESSION):
                # Newest last: stable, and for always-on layers the ordering is what the
                # model reads as priority.
                ranked = [Retrieved(e, 0.0) for e in sorted(candidates, key=lambda e: e.created_at)]
            else:
                ranked = []
                for e in candidates:
                    etokens = tokenize(e.text)
                    hit = len(qtokens & etokens)
                    if hit == 0:
                        continue
                    age_days = max((now - e.created_at) / DAY_SECONDS, 0.0)
                    ranked.append(Retrieved(e, hit + 1.0 / (1.0 + age_days)))
                ranked.sort(key=lambda r: r.score, reverse=True)
            if ranked:
                out[lspec.name] = ranked
        return out
