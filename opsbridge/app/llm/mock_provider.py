"""Deterministic offline providers for tests and MOCK_MODE.

* MockLLMProvider: answers from the text it is given (it never knows more than the prompt), so it
  behaves like a well-grounded model. Tests can queue exact responses or failures.
* MockEmbeddingProvider: feature-hashing bag-of-words vectors. Not semantic, but deterministic,
  and texts sharing words get high cosine similarity - enough to exercise hybrid search.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

from app.core.errors import TransientError
from app.core.text import normalise_tokens
from app.llm.base import LLMError, LLMMessage, LLMResult, estimate_tokens

_SOURCE_RE = re.compile(r"^\[(?P<key>[A-Z][A-Z0-9_]+-\d+)\][^\n]*?environment=(?P<env>\w+)[^\n]*\n"
                        r"(?:.*\n)*?Fix: (?P<fix>[^\n]+)", re.MULTILINE)
@dataclass
class MockLLMProvider:
    name: str = "mock"
    model: str = "mock-grounded-1"
    calls: list[dict] = field(default_factory=list)
    _queued: list[str] = field(default_factory=list)  # exact texts, or "!timeout" / "!error"

    def queue(self, *responses: str) -> None:
        self._queued.extend(responses)

    def generate(self, system: str, messages: list[LLMMessage], max_tokens: int,
                 temperature: float = 0.0) -> LLMResult:
        prompt = "\n".join(m["content"] for m in messages)
        self.calls.append({"system": system, "prompt": prompt, "max_tokens": max_tokens})
        if self._queued:
            nxt = self._queued.pop(0)
            if nxt == "!timeout":
                raise TransientError("mock LLM timeout", service="llm:mock")
            if nxt == "!error":
                raise LLMError("mock LLM failure")
            text = nxt
        elif "SOURCES:" in prompt:
            text = self._answer(prompt)
        elif "RESOLUTION NOTES:" in prompt:
            notes = prompt.split("RESOLUTION NOTES:", 1)[1].strip()
            sentences = re.split(r"(?<=[.!?])\s+", " ".join(notes.split()))
            text = "Resolution: " + " ".join(sentences[:2])
        else:
            text = "OK"
        return LLMResult(text=text, input_tokens=estimate_tokens(system + prompt), output_tokens=estimate_tokens(text),
                         model=self.model, latency_ms=1.0)

    @staticmethod
    def _answer(prompt: str) -> str:
        sources = list(_SOURCE_RE.finditer(prompt.split("SOURCES:", 1)[1]))
        if not sources:
            return "The provided tickets do not answer this question."
        first = sources[0]
        return (f"This matches [{first['key']}] ({first['env']}): {first['fix'].strip()} "
                + " ".join(f"See also [{s['key']}] ({s['env']})." for s in sources[1:]))

    def close(self) -> None:
        pass


@dataclass
class MockEmbeddingProvider:
    name: str = "mock"
    model: str = "mock-hashing-256"
    dimensions: int = 256
    calls: int = 0
    last_tokens: int = 0
    _failures: list[str] = field(default_factory=list)

    def fail_next(self, count: int = 1) -> None:
        self._failures.extend(["error"] * count)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self._failures:
            self._failures.pop(0)
            raise TransientError("mock embedding service unavailable", service="llm:mock")
        self.last_tokens = sum(estimate_tokens(t) for t in texts)
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for token in normalise_tokens(text):
            digest = hashlib.md5(token.encode()).digest()  # noqa: S324 - hashing, not security
            index = int.from_bytes(digest[:4], "little") % self.dimensions
            vec[index] += 1.0 if digest[4] % 2 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def close(self) -> None:
        pass
