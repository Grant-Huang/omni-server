"""Builds the instructions string sent in ``session.update``, and decides when *not* to
send one.

Everything here exists because of one upstream constraint: on Qwen Realtime, content
only reaches the model through ``session.update.session.instructions`` -- a
``conversation.item.create`` with role "system" is ignored (workforce measured this on
2026-08-21). That conclusion is due a re-test on the workspace domain (E1,
docs/design-risks-review.md §3) because it was measured on the shared domain two days
before that domain was proven to swallow messages silently. Until then this is the
channel we have, and ``instructions`` is a whole-string replace, so every turn rebuilds
the entire prompt.

Two consequences shape this module:

- **Stable head, volatile tail.** Layers are concatenated in LayerSpec.order, most
  stable first, and looked-up results go last. Persona and policy bytes are then
  identical turn over turn, which keeps the model's voice from wobbling and gives any
  upstream prefix caching something to hold.
- **Skip the patch when nothing changed.** The ack round trip costs ~0.3s of a ~1s
  budget (workforce measured this on the workspace domain), and the existing client
  spends it even on turns where the built string is byte-identical to the last one --
  it re-sends the base instructions to clear the previous turn's memory. InstructionPatcher
  turns that into a no-op.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import layers as L
from .memory import Retrieved


def _fit(texts: list[str], budget_chars: int) -> list[str]:
    """Take entries in order until the budget runs out. Whole entries only -- a
    half-truncated memory reads as a different fact, which is worse than omitting it."""
    kept: list[str] = []
    used = 0
    for t in texts:
        cost = len(t) + 2
        if used + cost > budget_chars:
            continue
        kept.append(t)
        used += cost
    return kept


@dataclass
class DynamicBlock:
    """Volatile content injected mid-conversation -- a lookup result from the sidecar
    text model. Always rendered last (layers.DYNAMIC_ORDER) and never persisted: it is
    true for this turn only."""

    heading: str
    body: str
    turn_id: str | None = None


@dataclass
class BuiltInstructions:
    text: str
    layer_counts: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)


def build(
    base: str,
    retrieved: dict[str, list[Retrieved]],
    dynamic: list[DynamicBlock] | None = None,
) -> BuiltInstructions:
    parts = [base.strip()]
    counts: dict[str, int] = {}
    dropped: dict[str, int] = {}

    for lspec in L.ORDERED_LAYERS:
        hits = retrieved.get(lspec.name) or []
        if not hits:
            continue
        texts = [r.entry.text for r in hits]
        kept = _fit(texts, lspec.budget_chars)
        if not kept:
            dropped[lspec.name] = len(texts)
            continue
        if len(kept) < len(texts):
            dropped[lspec.name] = len(texts) - len(kept)
        counts[lspec.name] = len(kept)
        body = "\n".join(f"- {t}" for t in kept)
        parts.append(f"【{lspec.heading}】\n{body}")

    for block in dynamic or []:
        parts.append(f"【{block.heading}】\n{block.body.strip()}")

    return BuiltInstructions(text="\n\n".join(parts), layer_counts=counts, dropped=dropped)


class InstructionPatcher:
    """Tracks the last string actually accepted upstream, so an unchanged rebuild costs
    nothing.

    ``mark_sent`` is separate from ``needs_patch`` deliberately: the caller should only
    record a string as sent once the ``session.updated`` ack arrives. If we recorded it
    at send time and the ack never came (which workforce saw happen), we would believe
    the model has instructions it never received, and then skip the *next* patch too --
    compounding one dropped update into a conversation that silently never updates
    again. Treating an un-acked patch as un-sent means the next turn re-sends it.
    """

    def __init__(self) -> None:
        self._last_sent: str | None = None

    @property
    def last_sent(self) -> str | None:
        return self._last_sent

    def needs_patch(self, built: str) -> bool:
        return built != self._last_sent

    def mark_sent(self, built: str) -> None:
        self._last_sent = built

    def invalidate(self) -> None:
        """Forget what we think upstream has. Call on reconnect, and after any patch
        whose ack timed out."""
        self._last_sent = None
