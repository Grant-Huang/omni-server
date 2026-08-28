"""One-shot text-model endpoints (not the Realtime WS): dictation cleanup and memory
extraction. Ported from workforce/web-demo/server.py, which already validated both
against real Qwen calls (see that file's comments on QWEN_TEXT_MODEL/MEMORY_EXTRACT_PROMPT
for the dated verification notes) -- these belong server-side per the product's own
"服务端工具" framing, so omni-server is where they live going forward, not workforce.
"""
from __future__ import annotations

import asyncio
import json

import aiohttp
from aiohttp import web

from .config import Config

DICTATION_CLEANUP_PROMPT = (
    "把用户口述的这段话，整理成一段通顺、结构清晰的书面文字。"
    "去掉口语里的语气词、重复、停顿词（\"呃\"\"就是\"\"然后\"\"那个\"这些），"
    "如果用户说话时想到哪说到哪、顺序乱，帮TA理顺逻辑顺序，"
    "但不要添加原话里没有的信息，不要过度概括丢失细节，保持第一人称语气，"
    "不要用列表/编号这种书面格式（除非原话本身就是在列举好几件事）。"
    "直接给出整理后的文字，不要加任何前缀说明。"
)

MEMORY_EXTRACT_PROMPT = (
    "你是一个记忆提炼助手。给定用户和助手在一轮对话里说的话，判断这轮对话里有没有值得长期记住的"
    "事实性内容——比如用户的偏好、计划、决定、个人信息，或者用户解释了一个团队/个人黑话、术语的含义。\n\n"
    "如果有，用简洁清楚的第一人称转述提炼成 0 条到多条独立的事实（每条一两句话），不要逐字复述原话，"
    "也不要加原话里没有的信息。如果这轮只是打招呼、闲聊、追问细节但没有新信息，返回空列表。\n\n"
    "如果某条事实是在解释一个黑话/术语的含义（\"我们说的 XX 意思是 YY\"这种），把 isJargon 设为 "
    "true；其他普通事实设为 false。\n\n"
    "已知的黑话/术语（避免重复提炼这些已经记录过的）：\n{known_jargon}\n\n"
    "严格按以下 JSON 格式输出，不要有任何其他文字，不要用 markdown 代码块包裹：\n"
    '{{"facts": [{{"text": "...", "isJargon": false}}]}}\n'
    '没有值得记的内容时：{{"facts": []}}'
)


def make_dictation_cleanup_handler(config: Config):
    async def dictation_cleanup(request: web.Request) -> web.Response:
        if not config.qwen_api_key:
            return web.json_response({"error": "QWEN_API_KEY not set"}, status=500)

        body = await request.json()
        raw_text = (body.get("text") or "").strip()
        if not raw_text:
            return web.json_response({"error": "text is required"}, status=400)

        url = f"{config.compatible_mode_base}/chat/completions"
        headers = {"Authorization": f"Bearer {config.qwen_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": config.qwen_text_model,
            "messages": [
                {"role": "system", "content": DICTATION_CLEANUP_PROMPT},
                {"role": "user", "content": raw_text},
            ],
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        return web.json_response({"error": f"cleanup call failed: {data}"}, status=502)
                    cleaned = data["choices"][0]["message"]["content"]
                    return web.json_response({"cleaned": cleaned})
        except asyncio.TimeoutError:
            return web.json_response({"error": "整理超时（超过60秒），请重试"}, status=504)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)

    return dictation_cleanup


def make_memory_extract_handler(config: Config):
    async def memory_extract(request: web.Request) -> web.Response:
        if not config.qwen_api_key:
            return web.json_response({"error": "QWEN_API_KEY not set"}, status=500)

        body = await request.json()
        user_text = (body.get("userText") or "").strip()
        assistant_text = (body.get("assistantText") or "").strip()
        known_jargon = body.get("knownJargon") or []
        if not user_text or not assistant_text:
            return web.json_response({"error": "userText and assistantText are both required"}, status=400)

        prompt = MEMORY_EXTRACT_PROMPT.format(known_jargon="（无）" if not known_jargon else "、".join(known_jargon))
        url = f"{config.compatible_mode_base}/chat/completions"
        headers = {"Authorization": f"Bearer {config.qwen_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": config.qwen_text_model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"用户说：{user_text}\n助手回复：{assistant_text}"},
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        return web.json_response({"error": f"extract call failed: {data}"}, status=502)
                    content = data["choices"][0]["message"]["content"]
                    try:
                        parsed = json.loads(content)
                    except (ValueError, TypeError):
                        return web.json_response({"error": f"model returned non-JSON content: {content[:200]}"}, status=502)
                    facts = parsed.get("facts") if isinstance(parsed, dict) else None
                    if not isinstance(facts, list):
                        return web.json_response({"error": "model response missing a facts list"}, status=502)
                    return web.json_response({"facts": facts})
        except asyncio.TimeoutError:
            return web.json_response({"error": "提炼超时（超过60秒），请重试"}, status=504)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)

    return memory_extract
