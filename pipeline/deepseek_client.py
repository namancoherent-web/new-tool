from __future__ import annotations

import json
import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from config import CONFIG

logger = logging.getLogger(__name__)


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    """Thin wrapper around DeepSeek's OpenAI-compatible chat completions API.

    Tracks call count and (rough) token usage so every run can report exactly
    how many paid calls it made, per the spec's cost-transparency requirement.
    """

    def __init__(self) -> None:
        if not CONFIG.deepseek_api_key:
            raise DeepSeekError(
                "DEEPSEEK_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        self.call_count = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._client = httpx.Client(
            base_url=CONFIG.deepseek_base_url,
            headers={"Authorization": f"Bearer {CONFIG.deepseek_api_key}"},
            timeout=60,
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=20))
    def chat_json(self, system: str, user: str, temperature: float = 0.0, max_tokens: int = 8000) -> dict:
        """Call DeepSeek and parse the response as JSON. Retries on transient errors.

        max_tokens defaults well above DeepSeek's own default cap (which was
        silently truncating long responses -- e.g. market understanding can
        generate 100+ search queries plus definition/boundary text -- mid-
        JSON, causing an unrecoverable "Unterminated string" parse error on
        every retry attempt, since nothing about the truncation-causing
        request changes between retries)."""
        resp = self._client.post(
            "/chat/completions",
            json={
                "model": CONFIG.deepseek_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self.call_count += 1
        usage = data.get("usage", {})
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)

        choice = data["choices"][0]
        content = choice["message"]["content"]
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            if choice.get("finish_reason") == "length":
                raise DeepSeekError(
                    f"DeepSeek response was truncated by the token limit (finish_reason=length, "
                    f"max_tokens={max_tokens}) before valid JSON could complete -- raise max_tokens further"
                ) from e
            raise DeepSeekError(f"DeepSeek returned non-JSON content: {content[:200]}") from e

    def close(self) -> None:
        self._client.close()
