"""`ask`: hybrid search, grounded answers with citation validation, fallbacks, feedback, metrics, API."""

from __future__ import annotations

import html
import re
from datetime import timedelta

import pytest

from app.container import build_container
from app.domain.enums import AskStatus
from app.domain.enums import ProcessingStatus as S
from app.models.knowledge import AskQuery, KnowledgeEntry
from tests.conftest import VALID_MESSAGE, add_message, make_settings, notification_for, post_message
from tests.test_close_command import post_reply


def resolve(container, *, api="CustomerOrderAPI", env="QA", summary="Order creation is failing",
            description="The API returns HTTP 500 when creating an order.", fix="Rolled back the order-service deployment"):
    text = (VALID_MESSAGE.replace("CustomerOrderAPI", api).replace("Environment: QA", f"Environment: {env}")
            .replace("Order creation is failing", summary)
            .replace("The API returns HTTP 500 when creating an order.\nThis started after the latest deployment.",
                     description))
    rec = post_message(container, text)
    post_reply(container, rec.teams_message_id, f"close {fix}")
    return rec


def ask(container, question: str, *, sender_id="asker-1", reply_to: str | None = None, deliveries: int = 1):
    """Post `ask` top-level (or as a thread reply) and return (message, AskQuery)."""
    msg = add_message(container, f"ask {question}" if question else "ask", sender_id=sender_id, reply_to_id=reply_to)
    note = (notification_for(container, reply_to, reply_id=msg["id"]) if reply_to
            else notification_for(container, msg["id"]))
    for _ in range(deliveries):
        container.ingestion.ingest([note], trace_id="ask-trace")
    with container.db.session() as session:
        row = session.query(AskQuery).filter_by(teams_message_id=msg["id"]).one_or_none()
        if row is not None:
            session.expunge(row)
    return msg, row


def answer_html(container, thread_root: str) -> str:
    replies = [r["body"]["content"] for r in container.graph.replies.get(thread_root, []) if "(OpsBridge ask " in r["body"]["content"]]
    return replies[-1] if replies else ""


def plain(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


def feedback(container, thread_root: str, text: str, sender_id: str, deliveries: int = 1):
    msg = add_message(container, text, reply_to_id=thread_root, sender_id=sender_id)
    for _ in range(deliveries):
        container.ingestion.ingest([notification_for(container, thread_root, reply_id=msg["id"])], trace_id=None)
    return msg


def kb(container) -> dict[str, KnowledgeEntry]:
    with container.db.session() as session:
        rows = session.query(KnowledgeEntry).all()
        session.expunge_all()
        return {r.jira_issue_key: r for r in rows}


@pytest.fixture
def keyword_container():
    """No LLM, no embeddings: pure keyword search."""
    c = build_container(make_settings(embedding_provider="none"), sync_dispatch=True)
    c.db.create_all()
    yield c
    c.close()


@pytest.fixture
def llm_container():
    c = build_container(make_settings(llm_enabled=True), sync_dispatch=True)
    c.db.create_all()
    yield c
    c.close()


# =============================================================================================
# Search
# =============================================================================================
def test_keyword_search_ranks_same_api_and_environment_first(keyword_container):
    c = keyword_container
    resolve(c, api="InventoryAPI", env="PROD", fix="Restarted the order cache after the deployment")    # OPS-1
    resolve(c, api="CustomerOrderAPI", env="QA", fix="Restarted the order cache after the deployment")  # OPS-2
    resolve(c, api="CustomerOrderAPI", env="PROD", fix="Restarted the order cache after the deployment")  # OPS-3
    hints, results = c.search.search("CustomerOrderAPI order creation failing in QA after deployment")
    assert hints.api_name == "CustomerOrderAPI" and hints.environment == "QA"
    assert [r.entry.jira_issue_key for r in results] == ["OPS-2", "OPS-3", "OPS-1"]
    assert results[0].semantic is None and results[0].boosts.keys() >= {"api", "environment"}


def test_hybrid_search_uses_embeddings(container):
    resolve(container, summary="Payment authorization timing out", description="Calls to /payments/authorize time out.",
            api="PaymentGatewayAPI", fix="Renewed the expired TLS certificate on the payment gateway")
    resolve(container)
    _, results = container.search.search("payment authorization requests time out")
    assert results[0].entry.api_name == "PaymentGatewayAPI"
    assert results[0].semantic is not None and results[0].semantic > 0


def test_unrelated_question_scores_below_threshold(container):
    resolve(container)
    _, results = container.search.search("how do I reset my laptop VPN password?")
    assert results == []


def test_question_with_http_code_gets_boost(keyword_container):
    resolve(keyword_container, summary="Checkout errors", description="Checkout returns HTTP 502 from the gateway.",
            fix="Raised the gateway upstream timeout")
    _, [result] = keyword_container.search.search("checkout gateway errors 502")
    assert result.boosts.get("http_status") == 0.05


# =============================================================================================
# Answers: keyword, llm, fallbacks
# =============================================================================================
def test_keyword_answer_lists_past_fixes_with_links(keyword_container):
    resolve(keyword_container)
    msg, row = ask(keyword_container, "order creation failing with HTTP 500 in QA?")
    assert row.status is AskStatus.ANSWERED and row.mode == "keyword" and row.retrieved_keys == ["OPS-1"]
    reply = answer_html(keyword_container, msg["id"])
    assert '<a href="https://mock-jira.example.com/browse/OPS-1">OPS-1</a>' in reply
    text = plain(reply)
    assert "QA" in text and "Problem: Order creation is failing" in text
    assert "Fix: Rolled back the order-service deployment" in text
    assert "Reply `helpful` or `not helpful`. Still stuck? Post a support request" in text


def test_no_match_says_so_and_shows_template(container):
    resolve(container)
    msg, row = ask(container, "how do I reset my laptop VPN password?")
    assert row.mode == "none" and row.retrieved_keys == []
    text = plain(answer_html(container, msg["id"]))
    assert "I couldn't find a similar resolved issue. Please post a support request using the template." in text
    assert "API Name:" in text


def test_llm_answer_cites_only_retrieved_keys(llm_container):
    resolve(llm_container)
    msg, row = ask(llm_container, "order creation failing in QA?")
    assert row.mode == "llm" and row.prompt_version == "ASK_ANSWER_V1"
    assert "[OPS-1]" in row.answer_text and row.cited_entry_ids == [kb(llm_container)["OPS-1"].id]
    prompt = llm_container.llm.chat.calls[-1]["prompt"]
    assert "SOURCES:" in prompt and "[OPS-1]" in prompt and "environment=QA" in prompt


@pytest.mark.parametrize("bad_answer", [
    "This was fixed in [OPS-99] by restarting.",                 # cites a ticket that was not retrieved
    "Restart the service; see OPS-77 for details.",              # mentions an invented ticket, no citation
    "Restart the order service.",                                # no citation at all
])
def test_ungrounded_llm_answer_falls_back_to_keyword_format(llm_container, bad_answer):
    resolve(llm_container)
    llm_container.llm.chat.queue(bad_answer)
    msg, row = ask(llm_container, "order creation failing in QA?")
    assert row.mode == "keyword" and row.prompt_version is None
    assert "OPS-99" not in row.answer_text and "OPS-77" not in row.answer_text
    assert "Here are similar issues" in row.answer_text


def test_correlation_ids_in_llm_answer_are_not_mistaken_for_tickets(llm_container):
    resolve(llm_container)
    llm_container.llm.chat.queue("Same symptom as CORR-123456: rolled back the deployment [OPS-1] (QA).")
    _, row = ask(llm_container, "order creation failing in QA?")
    assert row.mode == "llm"


@pytest.mark.parametrize("failure", ["!timeout", "!error"])
def test_llm_failure_falls_back_and_still_replies(llm_container, failure):
    resolve(llm_container)
    llm_container.llm.chat.queue(failure)
    msg, row = ask(llm_container, "order creation failing in QA?")
    assert row.status is AskStatus.ANSWERED and row.mode == "keyword"
    assert "OPS-1" in plain(answer_html(llm_container, msg["id"]))


def test_budget_exceeded_falls_back_to_keyword(llm_container):
    resolve(llm_container)
    llm_container.llm.daily_token_budget = 1  # already used by the capture summary
    _, row = ask(llm_container, "order creation failing in QA?")
    assert row.mode == "keyword"


def test_question_is_redacted_before_storage_and_llm(llm_container):
    resolve(llm_container)
    _, row = ask(llm_container, "order creation fails, my password=Hunter2! email me at dev@example.com")
    assert "Hunter2!" not in row.question and "dev@example.com" not in row.question
    assert "Hunter2!" not in llm_container.llm.chat.calls[-1]["prompt"]


def test_empty_ask_gets_usage(container):
    msg, row = ask(container, "")
    assert row.mode == "none" and "Usage: ask" in plain(answer_html(container, msg["id"]))


# =============================================================================================
# Routing & idempotency
# =============================================================================================
def test_ask_never_creates_a_jira_ticket_and_duplicates_are_answered_once(container):
    resolve(container)
    issues_before = len(container.jira_client.issues)
    msg, row = ask(container, "order creation failing in QA?", deliveries=3)
    assert len(container.jira_client.issues) == issues_before
    with container.db.session() as session:
        assert session.query(AskQuery).count() == 1
        from app.models.support_request import SupportRequest

        routed = session.query(SupportRequest).filter_by(teams_message_id=msg["id"]).one()
        assert routed.processing_status is S.IGNORED and "Routed to ask" in routed.error_message
    answers = [r for r in container.graph.replies[msg["id"]] if "(OpsBridge ask " in r["body"]["content"]]
    assert len(answers) == 1


def test_ask_inside_a_ticket_thread_is_answered_in_that_thread(container):
    resolve(container)
    other = post_message(container, VALID_MESSAGE.replace("CORR-123456", "CORR-9"))
    msg, row = ask(container, "order creation failing, what fixed it last time?", reply_to=other.teams_message_id)
    assert row.reply_to_message_id == other.teams_message_id and row.status is AskStatus.ANSWERED
    assert "OPS-1" in plain(answer_html(container, other.teams_message_id))


def test_follow_up_ask_inside_an_ask_thread(container):
    resolve(container)
    first, _ = ask(container, "order creation failing in QA?")
    _, follow_up = ask(container, "and what about HTTP 500 on order creation?", reply_to=first["id"])
    assert follow_up.reply_to_message_id == first["id"] and follow_up.status is AskStatus.ANSWERED


def test_ask_from_a_bot_is_ignored(container):
    resolve(container)
    msg = add_message(container, "ask order creation failing?", application=True)
    container.ingestion.ingest([notification_for(container, msg["id"])], trace_id=None)
    with container.db.session() as session:
        assert session.query(AskQuery).count() == 0


def test_teams_reply_failure_is_retried_without_double_posting(container):
    resolve(container)
    container.graph.fail_next("reply_ambiguous_after")
    msg, row = ask(container, "order creation failing in QA?")
    assert row.status is AskStatus.REPLYING
    with container.db.session() as session:
        session.get(AskQuery, row.id).next_attempt_at -= timedelta(days=1)
    assert container.asks.process(row.id) is AskStatus.ANSWERED
    answers = [r for r in container.graph.replies[msg["id"]] if "(OpsBridge ask " in r["body"]["content"]]
    assert len(answers) == 1


# =============================================================================================
# Feedback
# =============================================================================================
def test_feedback_updates_counts_once_per_user(container):
    resolve(container)
    msg, row = ask(container, "order creation failing in QA?", sender_id="asker-1")
    feedback(container, msg["id"], "helpful", "asker-1", deliveries=3)    # redelivered
    feedback(container, msg["id"], "helpful", "asker-1")                  # same user again
    feedback(container, msg["id"], "not helpful", "colleague-2")          # another user
    entry = kb(container)["OPS-1"]
    assert entry.helpful_count == 1 and entry.not_helpful_count == 1
    with container.db.session() as session:
        assert session.get(AskQuery, row.id).feedback == "helpful"  # the asker's verdict
    texts = [plain(r["body"]["content"]) for r in container.graph.replies[msg["id"]]]
    assert any("You've already rated this answer" in t for t in texts)


def test_not_helpful_feedback_lowers_ranking(keyword_container):
    resolve(keyword_container)
    _, before = keyword_container.search.search("order creation failing in QA")
    msg, _ = ask(keyword_container, "order creation failing in QA?")
    feedback(keyword_container, msg["id"], "not helpful", "asker-1")
    _, after = keyword_container.search.search("order creation failing in QA")
    assert after[0].score < before[0].score and after[0].boosts["not_helpful"] < 0


def test_feedback_in_ticket_thread_applies_to_latest_answer_there(container):
    resolve(container)
    ticket = post_message(container, VALID_MESSAGE.replace("CORR-123456", "CORR-7"))
    ask(container, "order creation failing?", reply_to=ticket.teams_message_id)
    feedback(container, ticket.teams_message_id, "helpful", "colleague-2")
    assert kb(container)["OPS-1"].helpful_count == 1


def test_feedback_without_an_answer_is_ignored(container):
    ticket = resolve(container)
    feedback(container, ticket.teams_message_id, "helpful", "mock-user-1")
    assert kb(container)["OPS-1"].helpful_count == 0


# =============================================================================================
# Metrics + API
# =============================================================================================
def test_deflection_metrics(container):
    resolve(container)
    m1, _ = ask(container, "order creation failing in QA?", sender_id="a")
    feedback(container, m1["id"], "helpful", "a")                       # avoided: no ticket afterwards
    m2, _ = ask(container, "order creation failing in QA?", sender_id="b")
    feedback(container, m2["id"], "helpful", "b")
    post_message(container, sender_id="b")                              # ...but b filed a ticket anyway
    m3, _ = ask(container, "order creation failing in QA?", sender_id="c")
    feedback(container, m3["id"], "not helpful", "c")
    ask(container, "how do I reset my laptop VPN password?", sender_id="d")  # no match

    m = container.asks.deflection_metrics()
    assert m["total_asks"] == 4 and m["answered"] == 3 and m["answered_pct"] == 75.0
    assert m["rated"] == 3 and m["helpful"] == 2 and m["helpful_pct"] == 66.7
    assert m["tickets_avoided"] == 1 and m["no_match"] == 1


def test_knowledge_and_ask_api(client, container, admin_headers):
    resolve(container)
    resolve(container, api="PaymentGatewayAPI", env="UAT", fix="Renewed the expired TLS certificate")
    ask(container, "order creation failing in QA?")

    page = client.get("/knowledge?api_name=paymentgatewayapi", headers=admin_headers).json()
    assert page["total"] == 1 and page["items"][0]["environment"] == "UAT" and page["items"][0]["has_embedding"]
    entry_id = page["items"][0]["id"]
    assert client.get(f"/knowledge/{entry_id}", headers=admin_headers).json()["jira_issue_key"] == "OPS-2"
    assert client.get("/knowledge?environment=qa", headers=admin_headers).json()["total"] == 1

    asks = client.get("/ask-queries", headers=admin_headers).json()
    assert asks["total"] == 1 and asks["items"][0]["mode"] == "keyword"
    assert client.get("/metrics/deflection", headers=admin_headers).json()["total_asks"] == 1

    assert client.delete(f"/knowledge/{entry_id}", headers=admin_headers).status_code == 204
    assert client.get(f"/knowledge/{entry_id}", headers=admin_headers).status_code == 404
    _, results = container.search.search("expired TLS certificate payment")
    assert all(r.entry.id != entry_id for r in results)  # gone from search too


@pytest.mark.parametrize("path", ["/knowledge", "/knowledge/1", "/ask-queries", "/metrics/deflection"])
def test_new_endpoints_require_api_key(client, path):
    assert client.get(path).status_code == 401
