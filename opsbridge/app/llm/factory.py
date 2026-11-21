"""Build chat and embedding providers from configuration (configured independently)."""

from __future__ import annotations

from app.config import Settings
from app.core.errors import ConfigurationError
from app.database.session import Database
from app.llm.base import EmbeddingProvider, LLMProvider
from app.llm.gateway import LLMGateway


def build_chat_provider(settings: Settings) -> LLMProvider | None:
    if not settings.llm_enabled:
        return None
    kind, key, model = settings.llm_provider, settings.llm_api_key.get_secret_value(), settings.llm_model
    common = {"timeout": settings.llm_timeout_seconds, "max_attempts": settings.http_max_attempts}
    if kind == "anthropic":
        from app.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=key, model=model, **common)
    if kind == "openai":
        from app.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key=key, model=model, **common)
    if kind == "ollama":
        from app.llm.ollama_provider import OllamaProvider

        return OllamaProvider(model=model, base_url=settings.ollama_base_url, **common)
    if kind == "mock":
        from app.llm.mock_provider import MockLLMProvider

        return MockLLMProvider()
    raise ConfigurationError(f"unknown LLM_PROVIDER '{kind}'")


def build_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    kind, model = settings.embedding_provider, settings.embedding_model
    common = {"timeout": settings.llm_timeout_seconds, "max_attempts": settings.http_max_attempts}
    if kind == "none":
        return None
    if kind == "openai":
        from app.llm.openai_provider import OpenAIEmbeddingProvider

        key = settings.embedding_api_key.get_secret_value() or settings.llm_api_key.get_secret_value()
        return OpenAIEmbeddingProvider(api_key=key, model=model, **common)
    if kind == "ollama":
        from app.llm.ollama_provider import OllamaEmbeddingProvider

        return OllamaEmbeddingProvider(model=model, base_url=settings.ollama_base_url, **common)
    if kind == "mock":
        from app.llm.mock_provider import MockEmbeddingProvider

        return MockEmbeddingProvider()
    raise ConfigurationError(f"unknown EMBEDDING_PROVIDER '{kind}'")


def build_llm_gateway(settings: Settings, db: Database) -> LLMGateway:
    return LLMGateway(db=db, chat=build_chat_provider(settings), embeddings=build_embedding_provider(settings),
                      daily_token_budget=settings.llm_daily_token_budget)
