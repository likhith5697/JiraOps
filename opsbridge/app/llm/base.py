"""Provider-neutral LLM interfaces.

Everything AI-related in OpsBridge talks to these two protocols, never to a vendor API directly,
so providers (Anthropic, OpenAI, Ollama, mock) are interchangeable from configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict


class LLMMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    latency_ms: float


class LLMError(Exception):
    """A provider call failed (bad response, refusal, empty output...). Callers fall back."""


class LLMUnavailable(Exception):
    """Raised by the gateway when the LLM must not / could not be used (disabled, over budget,
    provider failure after retries). Always handled by falling back to the non-LLM path."""


class BudgetExceeded(LLMUnavailable):
    pass


class LLMProvider(Protocol):
    name: str
    model: str

    def generate(self, system: str, messages: list[LLMMessage], max_tokens: int,
                 temperature: float = 0.0) -> LLMResult: ...

    def close(self) -> None: ...


class EmbeddingProvider(Protocol):
    name: str
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def close(self) -> None: ...


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for providers that don't report usage."""
    return max(1, len(text) // 4)
