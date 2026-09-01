"""Decides whether a turn needs a lookup, runs it, and produces the two things that
come back: something to say, and something to show.

Runs *beside* the voice turn, not before it. The realtime model starts answering the
moment the transcript lands; this pipeline races it. Most turns need no lookup, and on
those the coordinator never hears from the sidecar at all -- so the common path pays
nothing (see docs/memory-design.md §9 for the timing).

Every result carries two payloads on purpose:

- ``spoken`` — folded into the live session's instructions so the voice model can say it
  in its own words.
- ``display`` — a structured payload pushed to the app. This is the answer to a real
  limitation of voice: a spoken reply cannot show its sources, so the user has no way to
  check it. The app can show the rows the answer came from while the voice gives the
  summary.

``Sidecar.run``'s ``already_known`` tells the router what the ambient RETRIEVED layers
(task/episodic/shared -- see instructions.py/layers.py) already put in the voice
model's instructions this turn. Before this parameter existed, the ambient layers and
this router both called ``MemoryStore.retrieve`` independently and could both decide
the same fact was worth surfacing -- the router would then trigger a needless
self-interrupt (or a redundant follow-up) for something the model already had.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

from .memory import MemoryStore, tokenize
from .textmodel import TextModel, TextModelError, parse_json_object

ROUTER_PROMPT = """你是一个语音助手的调度器。用户刚说了一句话，语音模型已经在回答了。
你的唯一任务是判断：这句话要答得准，是否需要一次「查询」——也就是去翻用户的记录，
而不是靠常识或者闲聊就能答。

可用的查询工具：
{tool_list}

语音模型这一轮已经知道的背景信息（已经在它的提示词里，不用你再查一遍）：
{already_known}

判断标准：
- 需要查具体记录（日程、承诺、过去说过的某件事、某个数字），且上面「已经知道」没有覆盖 → 选一个工具，给出检索用的关键词。
- 上面「已经知道」已经包含能回答这句话的内容 → 不需要查，语音模型自己会用，不用再打断它。
- 闲聊、问候、常识问题、纯粹的情绪表达、用户只是在陈述而不是提问 → 不需要查。
- 拿不准就选不查——多查一次的代价是打断用户，不查的代价只是回答笼统一点。

严格输出这个 JSON，不要有其他文字：
{{"tool": "工具名或者 null", "query": "检索关键词", "why": "一句话理由"}}"""


@dataclass
class ToolResult:
    tool: str
    query: str
    spoken: str                       # goes into the voice session's instructions
    display: dict = field(default_factory=dict)   # goes to the app
    empty: bool = False               # nothing found -- caller may skip interrupting


@dataclass
class SidecarOutcome:
    turn_id: str
    needed_lookup: bool
    result: ToolResult | None = None
    elapsed_s: float = 0.0
    error: str | None = None


Tool = Callable[[str], Awaitable[ToolResult]]


class Sidecar:
    def __init__(
        self,
        model: TextModel,
        tools: dict[str, Tool],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model = model
        self._tools = tools
        self._clock = clock

    async def run(self, turn_id: str, transcript: str, *, already_known: str = "") -> SidecarOutcome:
        started = self._clock()
        tool_list = "\n".join(f"- {name}" for name in sorted(self._tools)) or "- （无）"
        try:
            raw = await self._model.complete(
                ROUTER_PROMPT.format(tool_list=tool_list, already_known=already_known or "（无）"),
                transcript, json_mode=True,
            )
            decision = parse_json_object(raw)
        except (TextModelError, OSError) as exc:
            # A failed lookup must never take the turn down with it: the voice model is
            # already answering from what it had. Degrading to "no lookup" is the right
            # failure mode -- a slightly vaguer answer, not a dropped turn.
            return SidecarOutcome(turn_id, False, elapsed_s=self._clock() - started, error=str(exc))

        name = decision.get("tool")
        if not name or name not in self._tools:
            return SidecarOutcome(turn_id, False, elapsed_s=self._clock() - started)

        query = (decision.get("query") or transcript).strip()
        try:
            result = await self._tools[name](query)
        except Exception as exc:  # a broken tool is the same class of problem
            return SidecarOutcome(turn_id, False, elapsed_s=self._clock() - started, error=str(exc))
        return SidecarOutcome(turn_id, True, result=result, elapsed_s=self._clock() - started)


def memory_search_tool(
    store: MemoryStore,
    *,
    output_scope: str,
    memberships: Sequence[str] = (),
    limit: int = 6,
) -> Tool:
    """Search the user's own memory more deeply than per-turn injection does.

    Per-turn retrieval is budget-capped and biased toward *this* utterance; this tool
    exists for the case where the user is explicitly asking to be looked something up,
    where spending a wider search is worth it. It runs against the same store and the
    same ``disclosable`` filter, so it cannot see anything the turn itself could not --
    a lookup tool is not a privilege escalation path.
    """

    async def run(query: str) -> ToolResult:
        grouped = store.retrieve(query, output_scope=output_scope, memberships=memberships)
        flat = [r for hits in grouped.values() for r in hits if r.score > 0]
        flat.sort(key=lambda r: r.score, reverse=True)
        top = flat[:limit]
        if not top:
            return ToolResult(
                tool="memory_search",
                query=query,
                spoken=f"关于「{query}」，记录里没有找到相关内容。请如实告诉用户你没有这方面的记录，不要编。",
                display={"kind": "memory_search", "query": query, "hits": []},
                empty=True,
            )
        lines = "\n".join(f"- {r.entry.text}" for r in top)
        return ToolResult(
            tool="memory_search",
            query=query,
            spoken=f"刚查到跟「{query}」相关的记录：\n{lines}\n用自己的话自然说出来，别念清单，也别提「查到」这个动作。",
            display={
                "kind": "memory_search",
                "query": query,
                "hits": [
                    {
                        "id": r.entry.id,
                        "text": r.entry.text,
                        "layer": r.entry.layer,
                        "score": round(r.score, 3),
                        "created_at": r.entry.created_at,
                    }
                    for r in top
                ],
            },
        )

    return run
