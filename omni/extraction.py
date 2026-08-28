"""Distils finished conversation into layered memory.

Three deliberate differences from the per-turn extraction workforce runs today
(web-demo/static/memoryExtraction.js):

1. **Batched, not per-turn.** Per-turn costs one model call per user per turn, which
   grows without bound; gives the model too little context to tell a durable fact from
   an aside; and produces near-duplicates when one topic spans three turns. Extraction
   is off the conversation critical path either way, so batching costs nothing the user
   can feel.
2. **Assigns a layer and a TTL**, not just text. Without this everything lands in one
   flat bucket and "我今天有点头疼" becomes a permanent fact about the user -- a slow,
   invisible poisoning of the store (R8 in docs/design-risks-review.md). The default for
   anything the model is unsure about is *short-lived*, not permanent: a wrong TTL that
   expires costs a re-learn, a wrong permanent fact costs forever.
3. **Cannot write persona or policy.** The prompt does not offer them, and
   MemoryStore.add refuses them for EXTRACTION writers regardless of what the model
   says. Two independent barriers, because this one is a security boundary
   (docs/memory-design.md §5) rather than a quality preference.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from . import layers as L
from .memory import MemoryEntry, MemoryStore, Provenance
from .textmodel import TextModel, TextModelError, parse_json_object

# Only the layers extraction is allowed to propose. persona/policy are absent on
# purpose; see the module docstring.
EXTRACTABLE = [name for name, spec in L.LAYERS.items() if L.EXTRACTION in spec.writable_by]

EXTRACT_PROMPT = """你是一个记忆提炼助手。下面是一段已经结束的对话。请提炼出值得记住的内容。

每条记忆要指定一个「层」，层决定它以后怎么被使用、多久之后失效：

- profile：长期稳定的身份/关系事实（"我爱人叫小林""我在 Y 公司做后端"）。永不过期。
- task：有时效的事情和承诺（"周二下午三点开会""答应给孩子买书"）。默认 30 天后过期。
- episodic：这次聊过什么的概括，用来以后想起上下文。默认 14 天。
- ephemeral：只对这次会话有效的临时要求（"接下来都简短点""别用英文"）。会话结束就丢掉。

判断规则：
- 状态性、一过性的表述（今天累不累、这周忙不忙、当下的情绪和身体状况）一律 ephemeral 或者短 TTL 的 task，**不要**放进 profile。
- 拿不准是长期还是短期，选短的那个。记错了会一直错下去，记短了最多是以后重新问一遍。
- 只是打招呼、追问细节、没有新信息的对话，返回空列表。
- 不要逐字复述原话，用简洁的第一人称转述。不要添加原话里没有的信息。
- 如果某条新事实明显**推翻**了下面列出的某条已有记忆，把那条的 id 写进 supersedes。
  只有真的矛盾才写（同一件事改了），只是相关不算。

已有的相关记忆（可能为空）：
{existing}

严格按这个 JSON 输出，不要有其他文字：
{{"facts": [{{"text": "...", "layer": "task", "ttl_days": 30, "supersedes": []}}]}}
没有值得记的内容时：{{"facts": []}}"""


@dataclass
class ExtractionReport:
    stored: list[MemoryEntry]
    rejected: list[tuple[str, str]]   # (text, reason)
    superseded: int = 0


def render_turns(turns: Sequence[tuple[str, str]]) -> str:
    return "\n".join(f"{'用户' if who == 'user' else '助手'}：{text}" for who, text in turns)


class Extractor:
    def __init__(self, model: TextModel, store: MemoryStore) -> None:
        self._model = model
        self._store = store

    async def extract(
        self,
        turns: Sequence[tuple[str, str]],
        *,
        scope: str,
        source: Provenance,
        session_id: str | None = None,
        existing_hint: Sequence[MemoryEntry] = (),
    ) -> ExtractionReport:
        if not turns:
            return ExtractionReport([], [])

        existing = "\n".join(f"- [{e.id}] {e.text}" for e in existing_hint) or "（无）"
        raw = await self._model.complete(
            EXTRACT_PROMPT.format(existing=existing), render_turns(turns), json_mode=True
        )
        parsed = parse_json_object(raw)
        facts = parsed.get("facts")
        if not isinstance(facts, list):
            raise TextModelError("extraction response has no facts list")

        stored: list[MemoryEntry] = []
        rejected: list[tuple[str, str]] = []
        superseded = 0

        for fact in facts:
            if not isinstance(fact, dict):
                continue
            text = (fact.get("text") or "").strip()
            if not text:
                continue
            layer = (fact.get("layer") or "").strip()
            if layer not in EXTRACTABLE:
                # Includes the case where the model tried for persona/policy. Rejected
                # and reported rather than silently downgraded, because a model reaching
                # for those layers is worth being able to see.
                rejected.append((text, f"layer {layer!r} not writable by extraction"))
                continue

            ttl = fact.get("ttl_days", -1.0)
            if ttl is not None and not isinstance(ttl, (int, float)):
                ttl = -1.0  # fall back to the layer default rather than trusting junk

            supersedes = [
                sid for sid in (fact.get("supersedes") or [])
                if isinstance(sid, str) and self._is_supersedable(sid, scope)
            ]
            try:
                entry = self._store.add(
                    text,
                    layer=layer,
                    scope=scope,
                    written_by=L.EXTRACTION,
                    source=source,
                    ttl_days=ttl,
                    session_id=session_id if layer == "ephemeral" else None,
                    supersedes=supersedes,
                )
            except (PermissionError, ValueError) as exc:
                rejected.append((text, str(exc)))
                continue
            superseded += len(supersedes)
            stored.append(entry)

        return ExtractionReport(stored, rejected, superseded)

    def _is_supersedable(self, entry_id: str, scope: str) -> bool:
        """A model-proposed supersede is only honoured for an entry that exists and sits
        in the same scope. A hallucinated id, or one belonging to a group, is ignored --
        the store would refuse the cross-scope case anyway, but silently dropping it
        here keeps one bad id from failing the whole extraction."""
        entry = self._store.get(entry_id)
        return entry is not None and entry.scope == scope
