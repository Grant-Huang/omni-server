"""Layer definitions for the memory system.

The single most important idea here: **a "layer" is not one property, it is three
independent ones**, and collapsing them into a single enum is the mistake this module
exists to prevent (see docs/memory-design.md §2).

- ``injection`` — *when* does this reach the model? Always in the prompt, only when
  retrieval hits, or only for the duration of one session.
- ``ttl`` — *when does it stop being true?* Never, on a deadline, on a rolling window,
  or at end of session.
- ``writable_by`` — *who is allowed to write it?* This one is a security boundary, not
  a preference: ``persona`` and ``policy`` are the system prompt's stable head, so
  letting the extraction pipeline write them would hand every speaker in every group
  the ability to rewrite the system prompt (docs/memory-design.md §5). Enforced in
  MemoryStore.add(), not by asking the extraction prompt nicely.

``order`` fixes the concatenation order in the built instructions string. It is
deliberately "most stable first": persona/policy bytes stay identical turn over turn,
so the only part that changes is the tail. That keeps the model's persona description
literally byte-stable across a conversation, and gives any prefix caching upstream
might do something to hold onto.

``budget_chars`` is per layer on purpose. A single global top-K lets whichever layer
happens to be busy today crowd every other layer out -- a heavy task day would silently
evict the persona and the assistant's voice would change mid-conversation. Budgets are
in characters, not tokens, because we can count them without a tokenizer; for Chinese
text 1 char is roughly 1-1.5 tokens, so treat these as approximations. The totals are
placeholders until E4 (docs/design-risks-review.md §2) measures what injected length
actually costs in time-to-first-audio.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet

# Who may write a layer. HUMAN means a real person acting through the UI/API;
# EXTRACTION means the automated distillation pipeline reading conversations.
HUMAN = "human"
EXTRACTION = "extraction"

# injection modes
ALWAYS = "always"        # in every prompt, no retrieval
RETRIEVED = "retrieved"  # only when this turn's retrieval selects it
SESSION = "session"      # in every prompt for one session, then discarded


@dataclass(frozen=True)
class LayerSpec:
    name: str
    injection: str
    writable_by: FrozenSet[str]
    budget_chars: int
    default_ttl_days: float | None
    order: int
    heading: str
    description: str

    @property
    def human_only(self) -> bool:
        return self.writable_by == frozenset({HUMAN})


LAYERS: dict[str, LayerSpec] = {
    "persona": LayerSpec(
        name="persona",
        injection=ALWAYS,
        writable_by=frozenset({HUMAN}),
        budget_chars=200,
        default_ttl_days=None,
        order=10,
        heading="你是谁、怎么说话",
        description="AI 的说话风格、称呼用户的方式。只有人能写。",
    ),
    "policy": LayerSpec(
        name="policy",
        injection=ALWAYS,
        writable_by=frozenset({HUMAN}),
        budget_chars=300,
        default_ttl_days=None,
        order=20,
        heading="必须遵守的规则",
        description="用户定下的硬规则，以及送达措辞纪律这类系统规则。只有人能写。",
    ),
    "profile": LayerSpec(
        name="profile",
        injection=ALWAYS,
        writable_by=frozenset({HUMAN, EXTRACTION}),
        budget_chars=300,
        default_ttl_days=None,
        order=30,
        heading="关于用户",
        description="长期稳定的身份/关系事实。模型可提炼，人可编辑。",
    ),
    "task": LayerSpec(
        name="task",
        injection=RETRIEVED,
        writable_by=frozenset({HUMAN, EXTRACTION}),
        budget_chars=400,
        default_ttl_days=30,
        order=40,
        heading="相关的事情/安排",
        description="有时效的事务与承诺。默认 30 天后过期，除非写入时给了明确期限。",
    ),
    "episodic": LayerSpec(
        name="episodic",
        injection=RETRIEVED,
        writable_by=frozenset({HUMAN, EXTRACTION}),
        budget_chars=300,
        default_ttl_days=14,
        order=50,
        heading="最近聊过的",
        description="近期对话里提炼出的片段，滚动窗口。",
    ),
    "shared": LayerSpec(
        name="shared",
        injection=RETRIEVED,
        writable_by=frozenset({HUMAN, EXTRACTION}),
        budget_chars=300,
        default_ttl_days=None,
        order=60,
        heading="群里的共识",
        description="群作用域的记忆。v0 不写入，但检索/注入路径已经按作用域过滤。",
    ),
    "ephemeral": LayerSpec(
        name="ephemeral",
        injection=SESSION,
        writable_by=frozenset({HUMAN, EXTRACTION}),
        budget_chars=150,
        default_ttl_days=0,
        order=70,
        heading="这次会话里的临时要求",
        description='会话内有效的临时指令（"接下来都简短点"）。会话结束即弃，永不进长期库。',
    ),
}

# Volatile content (tool/lookup results) is appended after every layer -- it changes
# most often, so it belongs at the very end of the string where it disturbs the least.
DYNAMIC_ORDER = 900

ORDERED_LAYERS: list[LayerSpec] = sorted(LAYERS.values(), key=lambda s: s.order)


def spec(layer: str) -> LayerSpec:
    try:
        return LAYERS[layer]
    except KeyError:
        raise ValueError(
            f"unknown layer {layer!r}; known layers: {sorted(LAYERS)}"
        ) from None
