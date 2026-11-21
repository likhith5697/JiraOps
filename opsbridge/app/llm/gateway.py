"""The single entry point for every LLM / embedding call in OpsBridge.

Responsibilities, in order, for each call:
1. Is the provider configured? Is today's token budget left?  -> otherwise LLMUnavailable
2. Redact the input again (defence in depth; callers redact too).
3. Call the provider (it has its own timeout + bounded retries on transient errors).
4. Record usage (tokens per UTC day, in the database) and log provider/model/tokens/latency.
   Prompts and answers are never logged.
5. ANY failure becomes LLMUnavailable, which every caller handles by using the non-LLM path.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.time import utcnow
from app.database.session import Database
from app.llm.base import BudgetExceeded, EmbeddingProvider, LLMProvider, LLMResult, LLMUnavailable
from app.llm.prompts import Prompt
from app.llm.redaction import redact_text
from app.models.llm_usage import LlmUsageDaily

logger = logging.getLogger("opsbridge.llm")


class LLMGateway:
    def __init__(self, *, db: Database, chat: LLMProvider | None, embeddings: EmbeddingProvider | None,
                 daily_token_budget: int) -> None:
        self.db = db
        self.chat = chat
        self.embeddings = embeddings
        self.daily_token_budget = daily_token_budget

    # ---- capability flags --------------------------------------------------------------------------
    @property
    def chat_enabled(self) -> bool:
        return self.chat is not None

    @property
    def embeddings_enabled(self) -> bool:
        return self.embeddings is not None

    @property
    def embedding_model(self) -> str | None:
        return self.embeddings.model if self.embeddings else None

    # ---- calls ----------------------------------------------------------------------------------------
    def generate(self, prompt: Prompt, user_content: str, *, max_tokens: int = 500) -> LLMResult:
        if self.chat is None:
            raise LLMUnavailable("LLM is disabled")
        self._check_budget()
        start = time.perf_counter()
        try:
            result = self.chat.generate(prompt.system, [{"role": "user", "content": redact_text(user_content)}],
                                        max_tokens=max_tokens, temperature=0.0)
        except Exception as exc:  # noqa: BLE001 - every provider failure degrades to the fallback path
            logger.warning("LLM call failed; using fallback", extra={
                "provider": self.chat.name, "model": self.chat.model, "prompt_version": prompt.version,
                "error_type": type(exc).__name__, "latency_ms": round((time.perf_counter() - start) * 1000, 1)})
            raise LLMUnavailable(f"{self.chat.name} call failed: {type(exc).__name__}") from exc
        self._record(input_tokens=result.input_tokens, output_tokens=result.output_tokens)
        logger.info("LLM call", extra={
            "provider": self.chat.name, "model": result.model, "prompt_version": prompt.version,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "latency_ms": result.latency_ms})
        return result

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.embeddings is None:
            raise LLMUnavailable("embeddings are disabled")
        self._check_budget()
        start = time.perf_counter()
        try:
            vectors = self.embeddings.embed([redact_text(t) for t in texts])
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedding call failed", extra={
                "provider": self.embeddings.name, "model": self.embeddings.model, "error_type": type(exc).__name__})
            raise LLMUnavailable(f"{self.embeddings.name} embedding failed: {type(exc).__name__}") from exc
        tokens = int(getattr(self.embeddings, "last_tokens", 0) or 0)
        self._record(embedding_tokens=tokens)
        logger.info("embedding call", extra={
            "provider": self.embeddings.name, "model": self.embeddings.model, "texts": len(texts),
            "tokens": tokens, "latency_ms": round((time.perf_counter() - start) * 1000, 1)})
        return vectors

    # ---- budget -------------------------------------------------------------------------------------------
    @staticmethod
    def _today() -> str:
        return utcnow().strftime("%Y-%m-%d")

    def tokens_used_today(self) -> int:
        with self.db.session() as session:
            row = session.scalar(select(LlmUsageDaily).where(LlmUsageDaily.day == self._today()))
            return 0 if row is None else row.input_tokens + row.output_tokens + row.embedding_tokens

    def within_budget(self) -> bool:
        return self.daily_token_budget <= 0 or self.tokens_used_today() < self.daily_token_budget

    def _check_budget(self) -> None:
        if not self.within_budget():
            logger.warning("LLM daily token budget exhausted; using fallback",
                           extra={"budget": self.daily_token_budget})
            raise BudgetExceeded("daily token budget exhausted")

    def _record(self, *, input_tokens: int = 0, output_tokens: int = 0, embedding_tokens: int = 0) -> None:
        day = self._today()
        values = {
            "input_tokens": LlmUsageDaily.input_tokens + input_tokens,
            "output_tokens": LlmUsageDaily.output_tokens + output_tokens,
            "embedding_tokens": LlmUsageDaily.embedding_tokens + embedding_tokens,
            "calls": LlmUsageDaily.calls + 1,
            "updated_at": utcnow(),
        }
        for _ in range(2):
            with self.db.session() as session:
                if session.execute(update(LlmUsageDaily).where(LlmUsageDaily.day == day).values(**values)
                                   .execution_options(synchronize_session=False)).rowcount:
                    return
            try:
                with self.db.session() as session:
                    session.add(LlmUsageDaily(day=day, input_tokens=input_tokens, output_tokens=output_tokens,
                                              embedding_tokens=embedding_tokens, calls=1))
                return
            except IntegrityError:
                continue  # another worker created today's row first; update it

    def close(self) -> None:
        for provider in (self.chat, self.embeddings):
            if provider is not None:
                provider.close()
