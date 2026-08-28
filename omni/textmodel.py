"""The fast text model side channel.

Design note (this is the decision that unblocked R3 in docs/design-risks-review.md):
we do **not** need the Realtime API to support function calling. Tools run out of band
on an ordinary one-shot text completion, and the result is injected back into the live
voice session as instructions. That is the same mechanism workforce already uses twice
(dictation cleanup and memory extraction both call qwen-turbo over
``compatible-mode/v1/chat/completions``), just pointed at a new job.

Two useful consequences:

- E2 (does Realtime support function calling) stops being a blocker. It would still be
  *nice*, but nothing is waiting on it.
- E3 (can the realtime session speak a filler phrase while a tool call is in flight)
  stops being a question at all -- there is no tool call inside the realtime session to
  speak during. The voice model just talks; the lookup happens beside it.

``TextModel`` is a protocol so tests can script responses without network or quota.
"""
from __future__ import annotations

import json
from typing import Protocol


class TextModel(Protocol):
    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str: ...


class TextModelError(RuntimeError):
    pass


class DashScopeTextModel:
    """One-shot completions against 百炼's OpenAI-compatible endpoint.

    Uses the workspace-specific host when a workspace id is configured, for the same
    reason the realtime connection does: the shared host was measured failing silently
    rather than erroring (workforce, 2026-08-23). Defaults to qwen-turbo -- workforce
    measured qwen3.5-flash at 17-35s on this same endpoint versus 0.9-4.3s for
    qwen-turbo on comparable work, which is a large enough gap to matter for something
    that runs on every turn.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "qwen-turbo",
        workspace_id: str = "",
        timeout_s: float = 8.0,
        session_factory=None,
    ) -> None:
        self._key = api_key
        self._model = model
        self._workspace = workspace_id
        self._timeout = timeout_s
        self._session_factory = session_factory

    @property
    def base_url(self) -> str:
        if self._workspace:
            return f"https://{self._workspace}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"

    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        import aiohttp

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        factory = self._session_factory or aiohttp.ClientSession
        async with factory() as session:
            async with session.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise TextModelError(f"text model {resp.status}: {data}")
                try:
                    return data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise TextModelError(f"unexpected completion shape: {data}") from exc


def parse_json_object(raw: str) -> dict:
    """Tolerant JSON parse for model output.

    workforce verified qwen-turbo's ``response_format: json_object`` returns clean,
    parseable JSON with no markdown fence -- so the fence stripping here is a belt-and-
    braces path for other models, not a workaround for a known behaviour."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise TextModelError(f"model returned non-JSON: {raw[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise TextModelError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
