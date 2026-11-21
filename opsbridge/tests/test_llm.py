"""LLM foundation: redaction, providers (HTTP shapes), gateway budget/usage/fallback, factory."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.config import Settings
from app.core.errors import ConfigurationError
from app.database.session import Database
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import BudgetExceeded, LLMError, LLMUnavailable
from app.llm.factory import build_chat_provider, build_embedding_provider
from app.llm.gateway import LLMGateway
from app.llm.mock_provider import MockEmbeddingProvider, MockLLMProvider
from app.llm.ollama_provider import OllamaEmbeddingProvider, OllamaProvider
from app.llm.openai_provider import OpenAIEmbeddingProvider, OpenAIProvider
from app.llm.prompts import ASK_ANSWER_V1
from app.llm.redaction import redact_text

NO_SLEEP = {"sleep": lambda _s: None}


# =============================================================================================
# Redaction
# =============================================================================================
@pytest.mark.parametrize(("raw", "secret"), [
    ("contact jane.doe@example.com", "jane.doe@example.com"),
    ("Authorization: Bearer abcdefghijklmnop123456", "abcdefghijklmnop123456"),
    ("password=Hunter2!", "Hunter2!"),
    ('{"client_secret": "s3cr3t-value"}', "s3cr3t-value"),
    ("api_key: sk-proj-abcdefghijklmnopqrstuvwxyz", "sk-proj-abcdefghijklmnopqrstuvwxyz"),
    ("token=ATATT3xFfGF0abcdefghijklmnopqrstuvwxyz", "ATATT3xFfGF0abcdefghijklmnopqrstuvwxyz"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dozjgNryP4J3jVmNHl0w5N", "eyJhbGciOiJIUzI1NiJ9"),
    ("sha 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", "9f86d081884c7d659a2feaa0c55ad015"),
    ("b64 dGhpcyBpcyBhIHNlY3JldCBrZXkgdmFsdWUgMTIzNDU2Nzg5MA==", "dGhpcyBpcyBhIHNlY3JldCBrZXkgdmFsdWUg"),
    ("call +1 415 555 2671", "415 555 2671"),
    ("call (555) 123-4567", "123-4567"),
    ("call 555-123-4567", "555-123-4567"),
])
def test_redaction_removes_secrets_and_pii(raw, secret):
    assert secret not in redact_text(raw)


def test_redaction_keeps_useful_identifiers():
    text = "CORR-123456 OPS-12 HTTP 500 on 2026-11-14 10:22 trace 3fa85f64-5717-4562-b3fc-2c963f66afa6 CustomerOrderAPI"
    assert redact_text(text) == text


# =============================================================================================
# Providers (HTTP shapes via MockTransport)
# =============================================================================================
def test_anthropic_request_shape_and_text_extraction():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "claude-opus-5-5", "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Answer [OPS-1]."}],
            "usage": {"input_tokens": 120, "output_tokens": 30},
        })

    p = AnthropicProvider(api_key="k", transport=httpx.MockTransport(handler), **NO_SLEEP)
    result = p.generate("sys", [{"role": "user", "content": "q"}], max_tokens=400, temperature=0.7)
    assert result.text == "Answer [OPS-1]." and result.input_tokens == 120 and result.output_tokens == 30
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "k" and seen["headers"]["anthropic-version"] == "2026-10-21"
    assert seen["headers"]["anthropic-beta"] == "server-side-fallback-2026-07-01"
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5" and body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": "q"}]
    assert "temperature" not in body  # rejected by the current API version
    assert body["output_config"] == {"effort": "low"} and body["fallbacks"] == "default"
    assert body["max_tokens"] >= 16000  # headroom so thinking cannot truncate the answer


def test_anthropic_refusal_and_overload():
    refusal = AnthropicProvider(api_key="k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"stop_reason": "refusal", "content": [], "usage": {}})), **NO_SLEEP)
    with pytest.raises(LLMError, match="refusal"):
        refusal.generate("s", [{"role": "user", "content": "q"}], 100)

    calls = []

    def overloaded_then_ok(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    p = AnthropicProvider(api_key="k", transport=httpx.MockTransport(overloaded_then_ok), **NO_SLEEP)
    assert p.generate("s", [{"role": "user", "content": "q"}], 100).text == "ok" and len(calls) == 2


def test_openai_chat_and_embeddings():
    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            assert body["messages"][0] == {"role": "system", "content": "sys"}
            assert request.headers["authorization"] == "Bearer k"
            return httpx.Response(200, json={"model": "gpt-4o-mini", "choices": [{"message": {"content": "hi"}}],
                                             "usage": {"prompt_tokens": 5, "completion_tokens": 1}})
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0.0, 1.0]},
                                                  {"index": 0, "embedding": [1.0, 0.0]}],
                                         "usage": {"prompt_tokens": 4}})

    t = httpx.MockTransport(handler)
    assert OpenAIProvider(api_key="k", transport=t).generate("sys", [{"role": "user", "content": "q"}], 50).text == "hi"
    emb = OpenAIEmbeddingProvider(api_key="k", transport=t)
    assert emb.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]] and emb.last_tokens == 4


def test_ollama_chat_and_embeddings():
    def handler(request):
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            assert body["stream"] is False and body["options"]["num_predict"] == 50
            return httpx.Response(200, json={"model": "llama3.1", "message": {"content": "local"},
                                             "prompt_eval_count": 9, "eval_count": 2})
        return httpx.Response(200, json={"embeddings": [[0.5, 0.5]]})

    t = httpx.MockTransport(handler)
    r = OllamaProvider(transport=t).generate("s", [{"role": "user", "content": "q"}], 50)
    assert (r.text, r.input_tokens, r.output_tokens) == ("local", 9, 2)
    assert OllamaEmbeddingProvider(transport=t).embed(["x"]) == [[0.5, 0.5]]


def test_mock_embeddings_are_deterministic_and_similarity_tracks_shared_words():
    import math

    e = MockEmbeddingProvider()
    a, b, c = e.embed(["order creation failing with HTTP 500", "orders fail to create, HTTP 500", "password reset email"])

    def cos(x, y):
        return sum(i * j for i, j in zip(x, y, strict=True)) / (math.sqrt(sum(i * i for i in x)) * math.sqrt(sum(j * j for j in y)))

    assert e.embed(["order creation failing with HTTP 500"])[0] == a
    assert cos(a, b) > cos(a, c)


# =============================================================================================
# Gateway: redaction, usage, budget, graceful failure
# =============================================================================================
@pytest.fixture
def db():
    d = Database("sqlite://")
    d.create_all()
    return d


def test_gateway_redacts_before_calling_provider_and_never_logs_prompts(db, caplog):
    chat = MockLLMProvider()
    gw = LLMGateway(db=db, chat=chat, embeddings=None, daily_token_budget=0)
    with caplog.at_level(logging.INFO, logger="opsbridge.llm"):
        gw.generate(ASK_ANSWER_V1, "my password=Hunter2! email bob@example.com question about orders")
    assert "Hunter2!" not in chat.calls[0]["prompt"] and "bob@example.com" not in chat.calls[0]["prompt"]
    record = next(r for r in caplog.records if r.getMessage() == "LLM call")
    assert record.provider == "mock" and record.prompt_version == "ASK_ANSWER_V1"
    assert record.input_tokens > 0 and record.latency_ms >= 0
    assert all("question about orders" not in r.getMessage() for r in caplog.records)


def test_gateway_tracks_usage_and_enforces_daily_budget(db):
    gw = LLMGateway(db=db, chat=MockLLMProvider(), embeddings=MockEmbeddingProvider(), daily_token_budget=10_000)
    gw.generate(ASK_ANSWER_V1, "x " * 100)
    after_chat = gw.tokens_used_today()
    assert after_chat > 0
    gw.daily_token_budget = after_chat + 1  # one more call allowed
    gw.embed(["some words here"])
    assert gw.tokens_used_today() > after_chat
    with pytest.raises(BudgetExceeded):
        gw.generate(ASK_ANSWER_V1, "again")
    with pytest.raises(BudgetExceeded):
        gw.embed(["again"])


def test_gateway_turns_provider_failures_into_llm_unavailable(db):
    chat = MockLLMProvider()
    chat.queue("!timeout", "!error")
    gw = LLMGateway(db=db, chat=chat, embeddings=None, daily_token_budget=0)
    for _ in range(2):
        with pytest.raises(LLMUnavailable):
            gw.generate(ASK_ANSWER_V1, "q")
    with pytest.raises(LLMUnavailable):
        gw.embed(["no embedding provider"])
    with pytest.raises(LLMUnavailable):
        LLMGateway(db=db, chat=None, embeddings=None, daily_token_budget=0).generate(ASK_ANSWER_V1, "q")


# =============================================================================================
# Factory / config
# =============================================================================================
def test_factory_builds_configured_providers():
    s = Settings(_env_file=None, llm_enabled=True, llm_provider="ollama", embedding_provider="ollama")
    assert isinstance(build_chat_provider(s), OllamaProvider)
    assert isinstance(build_embedding_provider(s), OllamaEmbeddingProvider)
    assert build_chat_provider(Settings(_env_file=None)) is None  # LLM disabled by default
    assert build_embedding_provider(Settings(_env_file=None, embedding_provider="none")) is None
    anthropic = build_chat_provider(Settings(_env_file=None, llm_enabled=True, llm_provider="anthropic", llm_api_key="k"))
    assert isinstance(anthropic, AnthropicProvider) and anthropic.model == "claude-opus-5-5"


def test_real_providers_require_api_keys():
    with pytest.raises(ConfigurationError, match="LLM_API_KEY"):
        Settings(_env_file=None, llm_enabled=True, llm_provider="anthropic").check()
    with pytest.raises(ConfigurationError, match="EMBEDDING_API_KEY"):
        Settings(_env_file=None, embedding_provider="openai").check()
