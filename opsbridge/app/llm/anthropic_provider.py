"""Claude via the Anthropic Messages API (raw HTTPX, chat only - Anthropic has no embeddings API).

Notes for the current API version (2026-10-21) and models:
* `temperature` is not sent: sampling parameters are rejected (HTTP 400) on this API version.
  The interface argument is accepted and ignored.
* Thinking is always on for current models; depth is set with `output_config.effort`. These are
  short, grounded tasks, so effort is "low", and `max_tokens` keeps headroom so thinking can't
  truncate the visible answer (answer length is controlled by the prompt).
* Server-side refusal fallbacks are enabled (`fallbacks: "default"`). A response that still ends
  with stop_reason "refusal" is treated as a failure, so OpsBridge falls back to the keyword answer.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from app.integrations.http import send_with_retries
from app.llm.base import LLMError, LLMMessage, LLMResult

DEFAULT_MODEL = "claude-opus-5-5"
API_VERSION = "2026-10-21"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MIN_MAX_TOKENS = 16000


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, api_key: str, model: str = "", timeout: float = 30.0, max_attempts: int = 3,
                 base_url: str = "https://api.anthropic.com", effort: str = "low",
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] | None = None) -> None:
        self.model = model or DEFAULT_MODEL
        self._effort = effort
        self._max_attempts = max_attempts
        self._extra = {"sleep": sleep} if sleep else {}
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout),
            transport=transport,
            headers={"x-api-key": api_key, "anthropic-version": API_VERSION, "anthropic-beta": FALLBACK_BETA,
                     "content-type": "application/json", "User-Agent": "OpsBridge/1.0"},
        )

    def generate(self, system: str, messages: list[LLMMessage], max_tokens: int,
                 temperature: float = 0.0) -> LLMResult:  # noqa: ARG002 - see module docstring
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max(max_tokens, MIN_MAX_TOKENS),
            "system": system,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            "output_config": {"effort": self._effort},
            "fallbacks": "default",
        }
        start = time.perf_counter()
        # Generation has no side effects, so retrying timeouts/5xx/429/529 is safe (idempotent=True).
        data = send_with_retries(self._client, "POST", "/v1/messages", service="llm:anthropic", idempotent=True,
                                 max_attempts=self._max_attempts, json=body, **self._extra).json()
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        if data.get("stop_reason") == "refusal":
            raise LLMError("model declined the request (refusal)")
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        if not text:
            raise LLMError(f"empty response (stop_reason={data.get('stop_reason')})")
        usage = data.get("usage") or {}
        return LLMResult(text=text, input_tokens=int(usage.get("input_tokens", 0)),
                         output_tokens=int(usage.get("output_tokens", 0)), model=data.get("model", self.model),
                         latency_ms=latency_ms)

    def close(self) -> None:
        self._client.close()
