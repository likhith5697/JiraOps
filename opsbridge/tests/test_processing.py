"""End-to-end processing through the state machine with mocked Graph and Jira.

Covers scenarios 1, 6-11: valid processing, Jira success/failure, Graph failure,
acknowledgement failure, retry behaviour and ambiguous Jira timeouts.
"""

from __future__ import annotations

import threading

from app.domain.enums import ProcessingStatus as S
from app.services.jira_service import idempotency_label
from tests.conftest import VALID_MESSAGE, add_message, get_request, notification_for, post_message, run_again


# ---------------------------------------------------------------------------------------------
# 1 + 6: valid message -> Jira issue -> Teams acknowledgement
# ---------------------------------------------------------------------------------------------
def test_valid_message_creates_issue_and_acknowledges(container):
    rec = post_message(container, sender_name="Sample User")

    assert rec.processing_status is S.COMPLETED
    assert rec.jira_issue_key == "OPS-1"
    assert rec.jira_issue_url == "https://mock-jira.example.com/browse/OPS-1"
    assert rec.api_name == "CustomerOrderAPI" and rec.environment == "QA" and rec.priority == "P2"
    assert rec.correlation_id == "CORR-123456"
    assert rec.sender_name == "Sample User"
    assert rec.trace_id == "test-trace"
    assert rec.acknowledged_at is not None and rec.error_message is None

    issue = container.jira_client.issues["OPS-1"]["fields"]
    assert issue["project"] == {"key": "OPS"}
    assert issue["issuetype"] == {"name": "Task"}
    assert issue["summary"] == "[QA] CustomerOrderAPI: Order creation is failing"
    assert issue["priority"] == {"name": "High"}
    assert idempotency_label(rec.teams_message_id) in issue["labels"]
    assert {"env-qa", "priority-p2", "api-customerorderapi", "opsbridge"} <= set(issue["labels"])
    description = str(issue["description"])
    assert "CORR-123456" in description and "Order processing is blocked in QA." in description
    assert rec.teams_message_url in description  # link back to the Teams message

    replies = container.graph.replies[rec.teams_message_id]
    assert len(replies) == 1
    body = replies[0]["body"]["content"]
    assert "successfully registered" in body and "OPS-1" in body and "P2" in body and "QA" in body


def test_priority_mapping_is_configurable(settings):
    from app.container import build_container

    c = build_container(settings.model_copy(update={"jira_priority_map": {}}), sync_dispatch=True)
    c.db.create_all()
    post_message(c)
    assert "priority" not in c.jira_client.issues["OPS-1"]["fields"]


# ---------------------------------------------------------------------------------------------
# 2-4: invalid submissions -> rejection reply, no Jira ticket
# ---------------------------------------------------------------------------------------------
def test_invalid_submission_gets_rejection_reply_and_no_ticket(container):
    rec = post_message(container, "API Name: CustomerOrderAPI\nEnvironment: DEV\nPriority: P7")

    assert rec.processing_status is S.REJECTED
    assert rec.jira_issue_key is None
    assert container.jira_client.create_calls == 0
    fields = {e["field"] for e in rec.validation_errors}
    assert {"environment", "priority", "issue_summary", "correlation_id"} <= fields

    body = container.graph.replies[rec.teams_message_id][0]["body"]["content"]
    assert "No Jira ticket was created" in body
    assert "Missing required field: Correlation ID." in body
    assert "Priority &#x27;P7&#x27; is not supported" in body  # user input is HTML-escaped
    assert "Expected template" in body and "Business Impact:" in body


def test_user_content_is_escaped_in_replies(container):
    rec = post_message(container, VALID_MESSAGE.replace("Environment: QA", "Environment: <script>x</script>"))
    body = container.graph.replies[rec.teams_message_id][0]["body"]["content"]
    assert "<script>" not in body and "&lt;script&gt;" in body


# ---------------------------------------------------------------------------------------------
# Filtering: replies, bots, the app itself
# ---------------------------------------------------------------------------------------------
def test_bot_messages_are_ignored(container):
    rec = post_message(container, application=True)
    assert rec.processing_status is S.IGNORED
    assert "bot" in rec.error_message
    assert container.jira_client.create_calls == 0
    assert rec.teams_message_id not in container.graph.replies


def test_messages_from_opsbridge_service_account_are_ignored(container):
    rec = post_message(container, sender_id=container.graph.service_account_user_id)
    assert rec.processing_status is S.IGNORED
    assert "OpsBridge itself" in rec.error_message


def test_reply_discovered_only_after_fetch_is_ignored(container):
    parent = add_message(container)
    reply = add_message(container, reply_to_id=parent["id"])
    # notification without the /replies() segment, the fetched message reveals replyToId
    summary = container.ingestion.ingest([notification_for(container, reply["id"])], trace_id=None)
    rec = get_request(container, summary.request_ids[0])
    assert rec.processing_status is S.IGNORED
    assert container.jira_client.create_calls == 0


def test_system_event_messages_are_ignored(container):
    rec = post_message(container, message_type="systemEventMessage")
    assert rec.processing_status is S.IGNORED


# ---------------------------------------------------------------------------------------------
# 7: Jira API failures + 10: retry behaviour
# ---------------------------------------------------------------------------------------------
def test_jira_transient_failure_is_retried_with_backoff(container):
    container.jira_client.fail_next("transient")
    rec = post_message(container)

    assert rec.processing_status is S.VALIDATED
    assert rec.retry_count == 1
    assert "connection refused" in rec.error_message
    assert rec.next_attempt_at > rec.updated_at  # backed off, not immediately retried
    assert container.jira_client.issues == {}

    assert run_again(container, rec.id) is S.COMPLETED
    rec = get_request(container, rec.id)
    assert rec.jira_issue_key == "OPS-1" and rec.retry_count == 0 and rec.error_message is None
    assert len(container.jira_client.issues) == 1


def test_backoff_grows_exponentially(container):
    container.jira_client.fail_next("transient", "transient")
    rec = post_message(container)
    first_delay = (rec.next_attempt_at - rec.updated_at).total_seconds()
    run_again(container, rec.id)
    rec = get_request(container, rec.id)
    second_delay = (rec.next_attempt_at - rec.updated_at).total_seconds()
    assert rec.retry_count == 2
    assert 45 <= first_delay <= 75  # base 60s +/- 20% jitter
    assert second_delay > first_delay * 1.3


def test_retries_exhausted_marks_failed_then_manual_retry_succeeds(container):
    container.jira_client.fail_next(*["transient"] * 4)  # max_retries=3 -> 4th failure is terminal
    rec = post_message(container)
    for _ in range(3):
        run_again(container, rec.id)
    rec = get_request(container, rec.id)
    assert rec.processing_status is S.FAILED
    assert rec.retry_count == 4
    assert container.jira_client.issues == {}

    container.support_requests.retry(rec.id)  # sync dispatcher processes immediately
    rec = get_request(container, rec.id)
    # earlier attempts existed, so the retry reconciled first (search) before creating
    assert container.jira_client.search_calls >= 1
    assert rec.processing_status is S.COMPLETED
    assert len(container.jira_client.issues) == 1


def test_jira_permanent_error_fails_without_retry(container):
    container.jira_client.fail_next("permanent")
    rec = post_message(container)
    assert rec.processing_status is S.FAILED
    assert "summary" in rec.error_message
    assert container.jira_client.create_calls == 1
    assert rec.teams_message_id not in container.graph.replies


def test_jira_rejecting_priority_falls_back_to_no_priority(container):
    from app.core.errors import PermanentError

    original = container.jira_client.create_issue
    calls = []

    def create(fields):
        calls.append(fields)
        if "priority" in fields:
            raise PermanentError("bad priority", service="jira", status_code=400,
                                 details={"field_errors": {"priority": "Field 'priority' cannot be set."}})
        return original(fields)

    container.jira_client.create_issue = create
    rec = post_message(container)
    assert rec.processing_status is S.COMPLETED
    assert len(calls) == 2 and "priority" not in calls[1]


# ---------------------------------------------------------------------------------------------
# 11: ambiguous Jira timeout - never blindly create a second ticket
# ---------------------------------------------------------------------------------------------
def test_ambiguous_timeout_after_issue_was_created_is_reconciled(container):
    container.jira_client.fail_next("ambiguous_after")  # Jira created the issue, response was lost
    rec = post_message(container)

    assert rec.processing_status is S.JIRA_UNKNOWN
    assert rec.jira_create_attempts == 1
    assert len(container.jira_client.issues) == 1
    # reconciliation waits at least JIRA_RECONCILE_DELAY_SECONDS (search index lag)
    assert (rec.next_attempt_at - rec.updated_at).total_seconds() >= 30

    assert run_again(container, rec.id) is S.COMPLETED
    rec = get_request(container, rec.id)
    assert container.jira_client.create_calls == 1  # no second create call at all
    assert len(container.jira_client.issues) == 1
    assert rec.jira_issue_key == "OPS-1"


def test_ambiguous_timeout_where_request_was_lost_creates_exactly_once(container):
    container.jira_client.fail_next("ambiguous_lost")
    rec = post_message(container)
    assert rec.processing_status is S.JIRA_UNKNOWN
    assert container.jira_client.issues == {}

    assert run_again(container, rec.id) is S.COMPLETED
    assert container.jira_client.search_calls == 1
    assert container.jira_client.create_calls == 2
    assert len(container.jira_client.issues) == 1


def test_reconciliation_search_failure_is_retried_not_skipped(container):
    container.jira_client.fail_next("ambiguous_after")
    rec = post_message(container)
    container.jira_client.fail_next("search_transient")
    run_again(container, rec.id)
    rec = get_request(container, rec.id)
    assert rec.processing_status is S.JIRA_UNKNOWN  # never falls through to a create
    assert container.jira_client.create_calls == 1

    assert run_again(container, rec.id) is S.COMPLETED
    assert len(container.jira_client.issues) == 1


def test_failed_after_ambiguous_outcome_resumes_with_reconciliation(container):
    container.jira_client.fail_next("ambiguous_after", "search_transient", "search_transient", "search_transient")
    rec = post_message(container)
    for _ in range(3):
        run_again(container, rec.id)
    rec = get_request(container, rec.id)
    assert rec.processing_status is S.FAILED

    container.support_requests.retry(rec.id)
    rec = get_request(container, rec.id)
    assert rec.processing_status is S.COMPLETED
    assert container.jira_client.create_calls == 1 and len(container.jira_client.issues) == 1


# ---------------------------------------------------------------------------------------------
# 8: Microsoft Graph API failure
# ---------------------------------------------------------------------------------------------
def test_graph_fetch_failure_keeps_request_and_retries(container):
    container.graph.fail_next("get_transient")
    rec = post_message(container)
    assert rec.processing_status is S.RECEIVED
    assert rec.retry_count == 1 and "503" in rec.error_message

    assert run_again(container, rec.id) is S.COMPLETED


def test_message_deleted_before_fetch_is_ignored(container):
    container.graph.fail_next("get_not_found")
    rec = post_message(container)
    assert rec.processing_status is S.IGNORED
    assert container.jira_client.create_calls == 0


# ---------------------------------------------------------------------------------------------
# 9: Teams acknowledgement failure
# ---------------------------------------------------------------------------------------------
def test_ack_failure_keeps_ticket_and_retries_reply_only(container):
    container.graph.fail_next("reply_transient")
    rec = post_message(container)

    assert rec.processing_status is S.JIRA_CREATED
    assert rec.jira_issue_key == "OPS-1"
    assert rec.retry_count == 1

    assert run_again(container, rec.id) is S.COMPLETED
    assert container.jira_client.create_calls == 1  # ticket NOT re-created
    assert len(container.graph.replies[rec.teams_message_id]) == 1


def test_ambiguous_ack_timeout_does_not_double_post(container):
    container.graph.fail_next("reply_ambiguous_after")  # reply was posted, response lost
    rec = post_message(container)
    assert rec.processing_status is S.JIRA_CREATED

    assert run_again(container, rec.id) is S.COMPLETED
    assert container.graph.reply_calls == 1
    assert len(container.graph.replies[rec.teams_message_id]) == 1


def test_ack_permanent_failure_marks_failed_and_manual_retry_only_resends_reply(container):
    container.graph.fail_next("reply_permanent")
    rec = post_message(container)
    assert rec.processing_status is S.FAILED
    assert rec.jira_issue_key == "OPS-1"

    container.support_requests.retry(rec.id)
    rec = get_request(container, rec.id)
    assert rec.processing_status is S.COMPLETED
    assert container.jira_client.create_calls == 1


def test_rejection_reply_failure_is_retried(container):
    container.graph.fail_next("reply_transient")
    rec = post_message(container, "Priority: P9")
    assert rec.processing_status is S.INVALID
    assert run_again(container, rec.id) is S.REJECTED
    assert container.jira_client.create_calls == 0


def test_same_issue_key_from_another_jira_site_does_not_block_processing(container):
    # e.g. leftover mock-mode data: a request already holds key OPS-1 from a different Jira site
    first = post_message(container)
    assert first.jira_issue_key == "OPS-1"
    container.jira_client.issues.clear()  # the "new" Jira site starts numbering at OPS-1 again

    second = post_message(container)
    assert second.processing_status is S.COMPLETED
    assert second.jira_issue_key == "OPS-1"


# ---------------------------------------------------------------------------------------------
# 5 + concurrency: duplicate notifications and racing workers
# ---------------------------------------------------------------------------------------------
def test_duplicate_notifications_create_one_ticket(container):
    message = add_message(container)
    n = notification_for(container, message["id"])
    first = container.ingestion.ingest([n], trace_id=None)
    second = container.ingestion.ingest([n, n], trace_id=None)

    assert first.accepted == 1 and second.duplicates == 2
    assert len(set(first.request_ids + second.request_ids)) == 1
    assert len(container.jira_client.issues) == 1
    assert len(container.graph.replies[message["id"]]) == 1


def test_concurrent_workers_process_a_request_once(tmp_path):
    from app.container import build_container
    from tests.conftest import make_settings

    c = build_container(make_settings(database_url=f"sqlite:///{tmp_path / 'race.db'}"), sync_dispatch=False)
    c.db.create_all()
    message = add_message(c)
    with c.db.session() as session:
        from app.repositories.support_repository import SupportRepository

        request_id = SupportRepository(session).insert_received(
            message_id=message["id"], team_id=c.settings.effective_team_id,
            channel_id=c.settings.effective_channel_id, trace_id=None,
        ).request_id

    barrier = threading.Barrier(6)
    results = []

    def worker(i: int) -> None:
        barrier.wait()
        results.append(c.processor.process(request_id, worker_id=f"w{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    c.close()

    assert results.count(S.COMPLETED) == 1
    assert results.count(None) == 5  # the others could not claim the lease
    assert len(c.jira_client.issues) == 1
