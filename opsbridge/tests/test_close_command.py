"""Close-from-Teams: a `close` reply in a request thread closes the linked Jira issue."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.container import build_container
from app.core.time import utcnow
from app.domain.enums import CommandOutcome, CommandStatus
from app.domain.enums import ProcessingStatus as S
from app.integrations.jira_client import JiraTransition, adf_plain_text
from app.models.teams_command import TeamsCommand
from tests.conftest import add_message, get_request, make_settings, notification_for, post_message, run_again

REQUESTER = "mock-user-1"  # default sender used by post_message
ADMIN = "admin-object-id-1"


@pytest.fixture
def settings():
    return make_settings(teams_close_allowed_user_ids=ADMIN)


# ---- helpers -------------------------------------------------------------------------------------
def post_reply(container, parent_message_id: str, text: str, *, sender_id: str = REQUESTER,
               sender_name: str = "Sample User", deliveries: int = 1, **kw):
    reply = add_message(container, text, reply_to_id=parent_message_id, sender_id=sender_id,
                        sender_name=sender_name, **kw)
    notification = notification_for(container, parent_message_id, reply_id=reply["id"])
    summary = None
    for _ in range(deliveries):
        summary = container.ingestion.ingest([notification], trace_id="reply-trace")
    return reply, summary


def get_command(container, command_id: int) -> TeamsCommand:
    with container.db.session() as session:
        row = session.get(TeamsCommand, command_id)
        session.expunge(row)
        return row


def run_command_again(container, command_id: int):
    with container.db.session() as session:
        session.get(TeamsCommand, command_id).next_attempt_at = utcnow() - timedelta(seconds=1)
    return container.commands.process(command_id)


def thread_replies(container, parent_message_id: str) -> list[str]:
    return [r["body"]["content"] for r in container.graph.replies.get(parent_message_id, [])]


def forbid_jira_calls(container) -> None:
    def boom(*_a, **_kw):
        raise AssertionError("Jira must not be called")

    for name in ("get_issue_status", "get_transitions", "transition_issue", "add_comment"):
        setattr(container.jira_client, name, boom)


@pytest.fixture
def ticket(container):
    """A completed request with Jira issue OPS-1, opened by REQUESTER."""
    rec = post_message(container)
    assert rec.processing_status is S.COMPLETED and rec.jira_issue_key == "OPS-1"
    return rec


# ---- happy paths ---------------------------------------------------------------------------------
def test_requester_closes_ticket(container, ticket):
    reply, summary = post_reply(container, ticket.teams_message_id, "close Fixed after redeploy")
    command = get_command(container, summary.command_ids[0])

    # command record
    assert command.status is CommandStatus.COMPLETED and command.outcome is CommandOutcome.CLOSED
    assert command.command == "close" and command.reason == "Fixed after redeploy"
    assert command.transitioned_at and command.commented_at and command.replied_at

    # Jira: transitioned + ADF comment
    issue = container.jira_client.issues["OPS-1"]
    assert issue["status"] == "Done"
    assert container.jira_client.transition_calls == 1
    assert [adf_plain_text(c["body"]) for c in issue["comments"]] == [
        "Closed from Microsoft Teams by Sample User. Reason: Fixed after redeploy"
    ]
    assert issue["comments"][0]["body"]["type"] == "doc"

    # Teams reply in the original thread
    close_reply = thread_replies(container, ticket.teams_message_id)[-1]
    assert "✅ OPS-1 has been closed by Sample User." in close_reply
    assert "Reason: Fixed after redeploy" in close_reply
    assert "https://mock-jira.example.com/browse/OPS-1" in close_reply

    # support request persisted
    rec = get_request(container, ticket.id)
    assert rec.processing_status is S.CLOSED
    assert rec.closed_at is not None
    assert rec.closed_by_id == REQUESTER and rec.closed_by_name == "Sample User"
    assert rec.close_reason == "Fixed after redeploy"
    assert rec.close_command_message_id == reply["id"]


def test_slash_close_without_reason(container, ticket):
    post_reply(container, ticket.teams_message_id, "/close")
    assert "Reason: Not provided" in adf_plain_text(container.jira_client.issues["OPS-1"]["comments"][0]["body"])
    assert "Reason: Not provided" in thread_replies(container, ticket.teams_message_id)[-1]


def test_allow_listed_admin_can_close_someone_elses_ticket(container, ticket):
    post_reply(container, ticket.teams_message_id, "CLOSE duplicate of OPS-9", sender_id=ADMIN, sender_name="Support Admin")
    rec = get_request(container, ticket.id)
    assert rec.processing_status is S.CLOSED
    assert rec.closed_by_id == ADMIN and rec.closed_by_name == "Support Admin"
    assert container.jira_client.issues["OPS-1"]["status"] == "Done"


def test_close_status_is_configurable_and_case_insensitive():
    c = build_container(make_settings(jira_close_status="closed"), sync_dispatch=True)
    c.db.create_all()
    c.jira_client.workflow = [JiraTransition("5", "Resolve", "Resolved"), JiraTransition("9", "Close Issue", "Closed")]
    rec = post_message(c)
    post_reply(c, rec.teams_message_id, "close")
    assert c.jira_client.issues["OPS-1"]["status"] == "Closed"
    assert get_request(c, rec.id).processing_status is S.CLOSED
    c.close()


# ---- refusals ------------------------------------------------------------------------------------
def test_unauthorized_user_is_rejected_without_any_jira_call(container, ticket):
    forbid_jira_calls(container)
    _, summary = post_reply(container, ticket.teams_message_id, "close", sender_id="someone-else",
                            sender_name="Random Person")
    command = get_command(container, summary.command_ids[0])
    assert command.status is CommandStatus.REJECTED and command.outcome is CommandOutcome.UNAUTHORIZED
    assert get_request(container, ticket.id).processing_status is S.COMPLETED
    assert ("You're not allowed to close OPS-1. Only the requester or a support admin can."
            in thread_replies(container, ticket.teams_message_id)[-1])


def test_already_done_is_idempotent(container, ticket):
    container.jira_client.issues["OPS-1"]["status"] = "Done"  # closed directly in Jira
    _, summary = post_reply(container, ticket.teams_message_id, "close")
    command = get_command(container, summary.command_ids[0])
    assert command.status is CommandStatus.COMPLETED and command.outcome is CommandOutcome.ALREADY_CLOSED
    assert container.jira_client.transition_calls == 0 and container.jira_client.comment_calls == 0
    assert "OPS-1 is already closed." in thread_replies(container, ticket.teams_message_id)[-1]
    assert get_request(container, ticket.id).processing_status is S.COMPLETED  # no other changes


def test_second_close_after_success_reports_already_closed(container, ticket):
    post_reply(container, ticket.teams_message_id, "close first")
    post_reply(container, ticket.teams_message_id, "close again")
    assert container.jira_client.transition_calls == 1 and container.jira_client.comment_calls == 1
    assert "OPS-1 is already closed." in thread_replies(container, ticket.teams_message_id)[-1]


def test_no_matching_transition_gives_clear_reply(container, ticket, caplog):
    container.jira_client.workflow = [JiraTransition("11", "Start Progress", "In Progress")]
    _, summary = post_reply(container, ticket.teams_message_id, "close")
    command = get_command(container, summary.command_ids[0])
    assert command.status is CommandStatus.REJECTED and command.outcome is CommandOutcome.NO_TRANSITION
    assert container.jira_client.issues["OPS-1"]["status"] == "To Do"
    assert container.jira_client.transition_calls == 0
    assert "no transition to 'Done'" in thread_replies(container, ticket.teams_message_id)[-1]
    assert get_request(container, ticket.id).processing_status is S.COMPLETED
    assert any("no transition" in r.getMessage() for r in caplog.records)


# ---- reliability ----------------------------------------------------------------------------------
def test_jira_failure_is_retried_then_fails_with_error(container, ticket):
    container.jira_client.fail_next(*["transition_transient"] * 4)  # max_retries=3
    _, summary = post_reply(container, ticket.teams_message_id, "close")
    command_id = summary.command_ids[0]
    command = get_command(container, command_id)
    assert command.status is CommandStatus.EXECUTING and command.retry_count == 1
    assert "429" in command.error_message
    assert command.next_attempt_at > utcnow()  # backed off

    for _ in range(3):
        run_command_again(container, command_id)
    command = get_command(container, command_id)
    assert command.status is CommandStatus.FAILED and command.retry_count == 4
    assert "429" in command.error_message
    assert container.jira_client.issues["OPS-1"]["status"] == "To Do"
    assert get_request(container, ticket.id).processing_status is S.COMPLETED


def test_transient_jira_failure_then_success(container, ticket):
    container.jira_client.fail_next("status_transient")
    _, summary = post_reply(container, ticket.teams_message_id, "close")
    assert get_command(container, summary.command_ids[0]).status is CommandStatus.EXECUTING
    assert run_command_again(container, summary.command_ids[0]) is CommandStatus.COMPLETED
    assert get_request(container, ticket.id).processing_status is S.CLOSED


def test_ambiguous_transition_timeout_does_not_transition_twice(container, ticket):
    container.jira_client.fail_next("transition_ambiguous_after")  # applied, response lost
    _, summary = post_reply(container, ticket.teams_message_id, "close")
    assert run_command_again(container, summary.command_ids[0]) is CommandStatus.COMPLETED
    assert container.jira_client.transition_calls == 1
    assert len(container.jira_client.issues["OPS-1"]["comments"]) == 1
    assert get_command(container, summary.command_ids[0]).outcome is CommandOutcome.CLOSED  # not "already"


def test_ambiguous_comment_timeout_does_not_comment_twice(container, ticket):
    container.jira_client.fail_next("comment_ambiguous_after")
    _, summary = post_reply(container, ticket.teams_message_id, "close flaky network")
    assert run_command_again(container, summary.command_ids[0]) is CommandStatus.COMPLETED
    assert container.jira_client.comment_calls == 1
    assert len(container.jira_client.issues["OPS-1"]["comments"]) == 1


def test_teams_reply_failure_is_retried_without_redoing_jira(container, ticket):
    container.graph.fail_next("reply_ambiguous_after")  # reply posted, response lost
    _, summary = post_reply(container, ticket.teams_message_id, "close")
    assert get_command(container, summary.command_ids[0]).status is CommandStatus.REPLYING
    assert get_request(container, ticket.id).processing_status is S.CLOSED  # persisted before the reply

    assert run_command_again(container, summary.command_ids[0]) is CommandStatus.COMPLETED
    assert container.jira_client.transition_calls == 1 and container.jira_client.comment_calls == 1
    closes = [r for r in thread_replies(container, ticket.teams_message_id) if "has been closed" in r]
    assert len(closes) == 1


def test_duplicate_notifications_for_same_reply_are_processed_once(container, ticket):
    _, summary = post_reply(container, ticket.teams_message_id, "close", deliveries=3)
    with container.db.session() as session:
        assert session.query(TeamsCommand).count() == 1
    assert container.jira_client.transition_calls == 1 and container.jira_client.comment_calls == 1
    assert len([r for r in thread_replies(container, ticket.teams_message_id) if "has been closed" in r]) == 1
    assert summary.duplicates == 1  # the 3rd delivery summary


def test_close_while_acknowledgement_pending_ends_closed(container):
    container.graph.fail_next("reply_transient")  # the initial "registered" ack fails once
    rec = post_message(container)
    assert rec.processing_status is S.JIRA_CREATED
    post_reply(container, rec.teams_message_id, "close")
    assert get_request(container, rec.id).processing_status is S.JIRA_CREATED  # ack still owed
    assert get_request(container, rec.id).closed_at is not None
    assert run_again(container, rec.id) is S.CLOSED


# ---- ignored replies -------------------------------------------------------------------------------
def test_non_command_reply_is_ignored(container, ticket):
    forbid_jira_calls(container)
    _, summary = post_reply(container, ticket.teams_message_id, "Thanks, looking into it")
    command = get_command(container, summary.command_ids[0])
    assert command.status is CommandStatus.IGNORED and command.error_message == "Not a command"
    assert len(thread_replies(container, ticket.teams_message_id)) == 1  # only the original ack


def test_closed_word_is_not_a_command(container, ticket):
    _, summary = post_reply(container, ticket.teams_message_id, "closed the browser and retried")
    assert get_command(container, summary.command_ids[0]).status is CommandStatus.IGNORED
    assert container.jira_client.transition_calls == 0


def test_reply_to_unknown_thread_is_ignored_without_a_row(container):
    parent = add_message(container)  # never became a support request
    _, summary = post_reply(container, parent["id"], "close")
    assert summary.ignored == 1 and summary.command_ids == []
    with container.db.session() as session:
        assert session.query(TeamsCommand).count() == 0


def test_reply_to_request_without_ticket_is_ignored(container):
    rejected = post_message(container, "Priority: P9")  # invalid -> no Jira issue
    _, summary = post_reply(container, rejected.teams_message_id, "close")
    assert summary.ignored == 1 and summary.command_ids == []


@pytest.mark.parametrize("kind", ["bot", "self"])
def test_bot_and_self_replies_are_ignored(container, ticket, kind):
    forbid_jira_calls(container)
    kwargs = {"application": True} if kind == "bot" else {"sender_id": container.graph.service_account_user_id}
    _, summary = post_reply(container, ticket.teams_message_id, "close", **kwargs)
    assert get_command(container, summary.command_ids[0]).status is CommandStatus.IGNORED


def test_reply_discovered_by_processor_is_routed_to_commands(container, ticket):
    """Notification without the /replies() segment: the processor fetches it, sees replyToId, routes it."""
    reply = add_message(container, "close routed", reply_to_id=ticket.teams_message_id)
    summary = container.ingestion.ingest([notification_for(container, reply["id"])], trace_id=None)
    routed_row = get_request(container, summary.request_ids[0])
    assert routed_row.processing_status is S.IGNORED
    assert routed_row.error_message == "Reply routed to thread-command handling"
    assert get_request(container, ticket.id).processing_status is S.CLOSED


# ---- API + persistence -------------------------------------------------------------------------------
def test_api_exposes_close_fields_and_commands(client, container, admin_headers, ticket):
    post_reply(container, ticket.teams_message_id, "close via api test")
    detail = client.get(f"/support-requests/{ticket.id}", headers=admin_headers).json()
    assert detail["processing_status"] == "CLOSED"
    assert detail["close_reason"] == "via api test" and detail["closed_by_id"] == REQUESTER
    assert detail["commands"][0]["command"] == "close"
    assert detail["commands"][0]["outcome"] == "CLOSED" and detail["commands"][0]["status"] == "COMPLETED"

    listed = client.get("/support-requests?status=CLOSED", headers=admin_headers).json()
    assert listed["total"] == 1 and listed["items"][0]["closed_at"]
    assert client.post(f"/support-requests/{ticket.id}/retry", headers=admin_headers).status_code == 409


def test_additive_migration_upgrades_an_old_database(tmp_path):
    import sqlite3

    from app.database.session import Database

    path = tmp_path / "old.db"
    Database(f"sqlite:///{path}").create_all()
    con = sqlite3.connect(path)  # simulate a pre-feature schema
    con.execute("DROP TABLE teams_commands")
    con.execute("DROP INDEX uq_support_requests_close_command")
    for col in ("closed_at", "closed_by_id", "closed_by_name", "close_reason", "close_command_message_id"):
        con.execute(f"ALTER TABLE support_requests DROP COLUMN {col}")
    con.commit()
    con.close()

    db = Database(f"sqlite:///{path}")
    db.create_all()  # upgrade
    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(support_requests)")}
    assert {"closed_at", "close_command_message_id"} <= cols
    assert con.execute("SELECT name FROM sqlite_master WHERE name='teams_commands'").fetchone()
    unique = {r[1]: r[2] for r in con.execute("PRAGMA index_list(support_requests)")}
    assert unique["uq_support_requests_close_command"] == 1
    con.close()
    db.dispose()
