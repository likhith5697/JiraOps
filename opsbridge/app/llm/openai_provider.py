"""OpenAI chat completions + embeddings over raw HTTPX."""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from app.integrations.http import send_with_retries
from app.llm.base import LLMError, LLMMessage, LLMResult

DEFAULT_CHAT_MODEL = "gpt-4o-mini"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


class _OpenAIBase:
    def __init__(self, *, api_key: str, timeout: float, max_attempts: int, base_url: str,
                 transport: httpx.BaseTransport | None, sleep: Callable[[float], None] | None) -> None:
        self._max_attempts = max_attempts
        self._extra = {"sleep": sleep} if sleep else {}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout), transport=transport,
                                    headers={"Authorization": f"Bearer {api_key}", "User-Agent": "OpsBridge/1.0"})

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return send_with_retries(self._client, "POST", path, service="llm:openai", idempotent=True,
                                 max_attempts=self._max_attempts, json=body, **self._extra).json()

    def close(self) -> None:
        self._client.close()


class OpenAIProvider(_OpenAIBase):
    name = "openai"

    def __init__(self, *, api_key: str, model: str = "", timeout: float = 30.0, max_attempts: int = 3,
                 base_url: str = "https://api.openai.com/v1", transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        super().__init__(api_key=api_key, timeout=timeout, max_attempts=max_attempts, base_url=base_url,
                         transport=transport, sleep=sleep)
        self.model = model or DEFAULT_CHAT_MODEL

    def generate(self, system: str, messages: list[LLMMessage], max_tokens: int,
                 temperature: float = 0.0) -> LLMResult:
        start = time.perf_counter()
        data = self._post("/chat/completions", {
            "model": self.model, "max_tokens": max_tokens, "temperature": temperature,
            "messages": [{"role": "system", "content": system}, *messages],
        })
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        choice = (data.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        if not text:
            raise LLMError(f"empty response (finish_reason={choice.get('finish_reason')})")
        usage = data.get("usage") or {}
        return LLMResult(text=text, input_tokens=int(usage.get("prompt_tokens", 0)),
                         output_tokens=int(usage.get("completion_tokens", 0)), model=data.get("model", self.model),
                         latency_ms=latency_ms)


class OpenAIEmbeddingProvider(_OpenAIBase):
    name = "openai"

    def __init__(self, *, api_key: str, model: str = "", timeout: float = 30.0, max_attempts: int = 3,
                 base_url: str = "https://api.openai.com/v1", transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        super().__init__(api_key=api_key, timeout=timeout, max_attempts=max_attempts, base_url=base_url,
                         transport=transport, sleep=sleep)
        self.model = model or DEFAULT_EMBEDDING_MODEL
        self.last_tokens = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/embeddings", {"model": self.model, "input": texts})
        self.last_tokens = int((data.get("usage") or {}).get("prompt_tokens", 0))
        rows = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        if len(rows) != len(texts):
            raise LLMError("embedding count mismatch")
        return [list(map(float, r["embedding"])) for r in rows]
