import json
import os
import httpx
from engine.core.log import log, section


CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-6"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-5.1"

# Stream so response headers arrive immediately and the read timeout applies
# per-chunk instead of to the whole generation. A long connect budget guards
# against a slow handshake; reads can idle up to `read` seconds between chunks.
STREAM_TIMEOUT = httpx.Timeout(600.0, connect=15.0, read=120.0)


async def call_claude(system_prompt: str, user_message: str, max_tokens: int = 256) -> str:
    api_key = os.environ["ANTHROPIC_API_KEY"]
    async with httpx.AsyncClient(timeout=STREAM_TIMEOUT) as client:
        async with client.stream(
            "POST",
            CLAUDE_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
                "stream": True,
            },
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                response.raise_for_status()

            parts = []
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data:
                    continue
                event = json.loads(data)
                if (
                    event.get("type") == "content_block_delta"
                    and event.get("delta", {}).get("type") == "text_delta"
                ):
                    parts.append(event["delta"]["text"])
            return "".join(parts)


async def call_openai(system_prompt: str, user_message: str, max_tokens: int = 256) -> str:
    api_key = os.environ["OPENAI_API_KEY"]
    effective_tokens = max(max_tokens * 4, 4096)
    async with httpx.AsyncClient(timeout=STREAM_TIMEOUT) as client:
        async with client.stream(
            "POST",
            OPENAI_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "max_completion_tokens": effective_tokens,
                "messages": [
                    {"role": "developer", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "stream": True,
            },
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                response.raise_for_status()

            parts = []
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                event = json.loads(data)
                choices = event.get("choices")
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content:
                    parts.append(content)
            return "".join(parts)


async def call_llm(system_prompt: str, user_message: str, max_tokens: int = 256) -> str:
    provider = os.environ.get("DEFAULT_PROVIDER", "claude").lower()
    model = OPENAI_MODEL if provider == "openai" else CLAUDE_MODEL

    section(f"LLM CALL  provider={provider}  model={model}  max_tokens={max_tokens}")
    log(f"[SYSTEM PROMPT] ({len(system_prompt)} chars)")
    log(system_prompt)
    log(f"[USER MESSAGE]")
    log(user_message)

    if provider == "openai":
        raw = await call_openai(system_prompt, user_message, max_tokens)
    else:
        raw = await call_claude(system_prompt, user_message, max_tokens)

    log(f"[LLM RESPONSE] ({len(raw)} chars)")
    log(raw)
    return raw
