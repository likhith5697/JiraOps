"""Knowledge capture on close, redaction, LLM summaries, embeddings, FTS index, sweep and backfill."""

from __future__ import annotations

import json

import pytest

from app.container import build_container
from app.domain.enums import ProcessingStatus as S
from app.models.knowledge import KnowledgeEntry
from app.repositories.knowledge_repository import KnowledgeRepository, query_terms
from tests.conftest import VALID_MESSAGE, get_request, make_settings, post_message
from tests.test_close_command import post_reply, thread_replies


def entries(container) -> list[KnowledgeEntry]:
    with container.db.session() as session:
        rows = session.query(KnowledgeEntry).order_by(KnowledgeEntry.id).all()
        session.expunge_all()
        return rows


def jira_comment(container, key: str, text: str) -> None:
    container.jira_client.add_comment(key, {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]}]})


@pytest.fixture
def llm_container():
    c = build_container(make_settings(llm_enabled=True, llm_provider="mock"), sync_dispatch=True)
    c.db.create_all()
    yield c
    c.close()


# =============================================================================================
# capture on close
# =============================================================================================
def test_close_with_reason_creates_entry(container):
    rec = post_message(container)
    post_reply(container, rec.teams_message_id, "close Rolled back the order-service deployment and cleared the cache")

    [entry] = entries(container)
    assert entry.support_request_id == rec.id and entry.jira_issue_key == "OPS-1"
    assert entry.api_name == "CustomerOrderAPI" and entry.environment == "QA" and entry.priority == "P2"
    assert "Order creation is failing" in entry.problem_text and "HTTP 500" in entry.problem_text
    assert entry.resolution_text == "Rolled back the order-service deployment and cleared the cache"
    assert entry.source == "close_reason" and entry.prompt_version is None
    assert entry.resolved_at is not None
    assert json.loads(entry.embedding) and entry.embedding_model == "mock-hashing-256"  # mock embeddings on
    assert get_request(container, rec.id).knowledge_status == "CAPTURED"


def test_jira_comments_are_included_but_opsbridge_audit_comments_are_not(container):
    rec = post_message(container)
    jira_comment(container, "OPS-1", "Root cause: connection pool exhausted after the deploy.")
    jira_comment(container, "OPS-1", "Raised max pool size to 50 and redeployed.")
    post_reply(container, rec.teams_message_id, "close pool size fix")
    [entry] = entries(container)
    assert entry.source == "jira_comment"
    assert entry.resolution_text.splitlines() == [
        "pool size fix", "Root cause: connection pool exhausted after the deploy.", "Raised max pool size to 50 and redeployed."]
    assert "Closed from Microsoft Teams" not in entry.resolution_text


def test_llm_summary_is_used_when_enabled(llm_container):
    rec = post_message(llm_container)
    post_reply(llm_container, rec.teams_message_id, "close Rolled back the deployment. Cache cleared. Verified orders.")
    [entry] = entries(llm_container)
    assert entry.source == "llm_summary" and entry.prompt_version == "RESOLUTION_SUMMARY_V2"
    assert entry.resolution_text.startswith("Resolution: Rolled back the deployment.")
    call = llm_container.llm.chat.calls[-1]
    assert "PROBLEM:" in call["prompt"] and "RESOLUTION NOTES:" in call["prompt"]


def test_llm_can_reject_notes_without_a_fix(llm_container):
    llm_container.llm.chat.queue("NO_RESOLUTION")
    rec = post_message(llm_container)
    jira_comment(llm_container, "OPS-1", "Assigned to the payments team for investigation.")
    post_reply(llm_container, rec.teams_message_id, "close")
    assert entries(llm_container) == []
    assert get_request(llm_container, rec.id).knowledge_status == "SKIPPED"


def test_llm_failure_falls_back_to_raw_notes(llm_container):
    llm_container.llm.chat.queue("!error")
    rec = post_message(llm_container)
    post_reply(llm_container, rec.teams_message_id, "close Restarted the payment gateway pods")
    [entry] = entries(llm_container)
    assert entry.source == "close_reason" and entry.resolution_text == "Restarted the payment gateway pods"


@pytest.mark.parametrize("reply", ["close", "close fixed", "/close done"])
def test_no_meaningful_resolution_is_skipped(container, reply):
    rec = post_message(container)
    post_reply(container, rec.teams_message_id, reply)
    assert entries(container) == []
    assert get_request(container, rec.id).knowledge_status == "SKIPPED"


def test_close_without_reason_gets_a_tip(container):
    rec = post_message(container)
    post_reply(container, rec.teams_message_id, "close")
    assert "Tip: add a reason next time" in thread_replies(container, rec.teams_message_id)[-1]
    post_reply(container, rec.teams_message_id, "reopen")
    post_reply(container, rec.teams_message_id, "close Increased timeout to 30s")
    assert "Tip:" not in thread_replies(container, rec.teams_message_id)[-1]


def test_secrets_are_redacted_before_storage_and_before_the_llm(llm_container):
    text = VALID_MESSAGE.replace("This started after the latest deployment.",
                                 "Contact ops.lead@example.com. Header: Authorization: Bearer abcdefghijklmnop12345")
    rec = post_message(llm_container, text)
    post_reply(llm_container, rec.teams_message_id, "close Rotated password=Hunter2! and api_key=sk-abcdefghijklmnopqrstuvwxyz1234")
    [entry] = entries(llm_container)
    stored = entry.problem_text + entry.resolution_text
    sent = llm_container.llm.chat.calls[-1]["prompt"]
    for secret in ("ops.lead@example.com", "abcdefghijklmnop12345", "Hunter2!", "sk-abcdefghijklmnopqrstuvwxyz1234"):
        assert secret not in stored and secret not in sent


def test_embedding_failure_does_not_block_and_is_filled_later(container):
    container.llm.embeddings.fail_next(1)
    rec = post_message(container)
    post_reply(container, rec.teams_message_id, "close Restarted the cache cluster nodes")
    [entry] = entries(container)
    assert entry.embedding is None  # saved anyway: keyword-searchable
    assert container.knowledge.embed_missing() == 1
    assert entries(container)[0].embedding is not None


def test_reclosing_updates_the_single_entry(container):
    rec = post_message(container)
    post_reply(container, rec.teams_message_id, "close first attempt restarted pods")
    post_reply(container, rec.teams_message_id, "reopen still failing")
    post_reply(container, rec.teams_message_id, "close real fix rotated the TLS certificate")
    [entry] = entries(container)
    assert entry.resolution_text == "real fix rotated the TLS certificate"


def test_ticket_closed_in_jira_is_captured_by_the_sweep(container):
    rec = post_message(container)
    jira_comment(container, "OPS-1", "Fixed by increasing the DB connection timeout.")
    container.jira_client.change_in_jira("OPS-1", status="Done")
    container.jira_sync.poll_once()
    assert get_request(container, rec.id).processing_status is S.CLOSED
    assert entries(container) == []  # no close command -> picked up by the sweep
    container.knowledge.sweep()
    [entry] = entries(container)
    assert entry.source == "jira_comment" and "DB connection timeout" in entry.resolution_text


# =============================================================================================
# FTS index + backfill
# =============================================================================================
def test_fts_index_stays_in_sync_on_insert_update_delete(container):
    rec = post_message(container)
    post_reply(container, rec.teams_message_id, "close Rotated the expired TLS certificate")
    with container.db.session() as session:
        repo = KnowledgeRepository(session)
        assert repo.fts_available
        [hit] = repo.keyword_candidates(query_terms("tls certificate expired"))
        entry = repo.get(hit.entry_id)
        entry.resolution_text = "Increased the gateway timeout"
    with container.db.session() as session:
        repo = KnowledgeRepository(session)
        assert repo.keyword_candidates(query_terms("certificate")) == []
        assert len(repo.keyword_candidates(query_terms("gateway timeout"))) == 1
        repo.delete(hit.entry_id)
    with container.db.session() as session:
        assert KnowledgeRepository(session).keyword_candidates(query_terms("gateway timeout")) == []


def test_keyword_search_works_without_fts5(container, monkeypatch):
    rec = post_message(container)
    post_reply(container, rec.teams_message_id, "close Rotated the expired TLS certificate")
    monkeypatch.setattr(KnowledgeRepository, "fts_available", property(lambda self: False))
    with container.db.session() as session:
        assert len(KnowledgeRepository(session).keyword_candidates(query_terms("certificates expiring"))) == 1


def test_backfill_script_captures_and_embeds(container):
    from scripts.backfill_knowledge import run

    first = post_message(container)
    second = post_message(container, VALID_MESSAGE.replace("CORR-123456", "CORR-2"))
    for rec in (first, second):  # simulate tickets closed before the feature existed
        with container.db.session() as session:
            from app.models.support_request import SupportRequest

            row = session.get(SupportRequest, rec.id)
            row.processing_status, row.close_reason = S.CLOSED, "Cleared the stuck queue consumers"
    container.llm.embeddings.fail_next(1)  # first embedding fails -> filled by the embed pass
    totals = run(container)
    assert totals["CAPTURED"] == 2 and totals["EMBEDDED"] == 1
    assert all(e.embedding for e in entries(container))
    assert run(container) == {"EMBEDDED": 0}  # idempotent
