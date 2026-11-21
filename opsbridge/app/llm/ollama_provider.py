"""Ollama (local, free): chat + embeddings over its HTTP API.

    ollama pull llama3.1          # chat
    ollama pull nomic-embed-text  # embeddings
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from app.integrations.http import send_with_retries
from app.llm.base import LLMError, LLMMessage, LLMResult, estimate_tokens

DEFAULT_CHAT_MODEL = "llama3.1"
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text"


class _OllamaBase:
    def __init__(self, *, base_url: str, timeout: float, max_attempts: int, transport: httpx.BaseTransport | None,
                 sleep: Callable[[float], None] | None) -> None:
        self._max_attempts = max_attempts
        self._extra = {"sleep": sleep} if sleep else {}
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout), transport=transport)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return send_with_retries(self._client, "POST", path, service="llm:ollama", idempotent=True,
                                 max_attempts=self._max_attempts, json=body, **self._extra).json()

    def close(self) -> None:
        self._client.close()


class OllamaProvider(_OllamaBase):
    name = "ollama"

    def __init__(self, *, model: str = "", base_url: str = "http://localhost:11434", timeout: float = 30.0,
                 max_attempts: int = 3, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        super().__init__(base_url=base_url, timeout=timeout, max_attempts=max_attempts, transport=transport, sleep=sleep)
        self.model = model or DEFAULT_CHAT_MODEL

    def generate(self, system: str, messages: list[LLMMessage], max_tokens: int,
                 temperature: float = 0.0) -> LLMResult:
        start = time.perf_counter()
        data = self._post("/api/chat", {
            "model": self.model, "stream": False,
            "messages": [{"role": "system", "content": system}, *messages],
            "options": {"temperature": temperature, "num_predict": max_tokens},
        })
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        text = ((data.get("message") or {}).get("content") or "").strip()
        if not text:
            raise LLMError("empty response from Ollama")
        prompt_text = system + "".join(m["content"] for m in messages)
        return LLMResult(text=text,
                         input_tokens=int(data.get("prompt_eval_count") or estimate_tokens(prompt_text)),
                         output_tokens=int(data.get("eval_count") or estimate_tokens(text)),
                         model=data.get("model", self.model), latency_ms=latency_ms)


class OllamaEmbeddingProvider(_OllamaBase):
    name = "ollama"

    def __init__(self, *, model: str = "", base_url: str = "http://localhost:11434", timeout: float = 30.0,
                 max_attempts: int = 3, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        super().__init__(base_url=base_url, timeout=timeout, max_attempts=max_attempts, transport=transport, sleep=sleep)
        self.model = model or DEFAULT_EMBEDDING_MODEL
        self.last_tokens = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/api/embed", {"model": self.model, "input": texts})
        vectors = data.get("embeddings") or []
        if len(vectors) != len(texts):
            raise LLMError("embedding count mismatch")
        self.last_tokens = int(data.get("prompt_eval_count") or sum(estimate_tokens(t) for t in texts))
        return [list(map(float, v)) for v in vectors]
