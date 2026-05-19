import os
import httpx


CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-6"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-5.1"


async def call_claude(system_prompt: str, user_message: str, max_tokens: int = 256) -> str:
    api_key = os.environ["ANTHROPIC_API_KEY"]
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
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
            },
        )
        response.raise_for_status()
        return response.json()["content"][0]["text"]


async def call_openai(system_prompt: str, user_message: str, max_tokens: int = 256) -> str:
    api_key = os.environ["OPENAI_API_KEY"]
    effective_tokens = max(max_tokens * 4, 4096)
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
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
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


async def call_llm(system_prompt: str, user_message: str, max_tokens: int = 256) -> str:
    provider = os.environ.get("DEFAULT_PROVIDER", "claude").lower()
    if provider == "openai":
        return await call_openai(system_prompt, user_message, max_tokens)
    return await call_claude(system_prompt, user_message, max_tokens)
