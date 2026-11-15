"""`status`, `reopen` and `escalate` thread commands (same framework as `close`)."""

from __future__ import annotations

import dataclasses

import pytest

from app.config import Settings
from app.container import build_container
from app.core.errors import ConfigurationError
from app.domain.enums import CommandOutcome as O
from app.domain.enums import CommandStatus
from app.domain.enums import ProcessingStatus as S
from app.integrations.jira_client import JiraTransition, adf_plain_text
from tests.conftest import get_request, make_settings, post_message
from tests.test_close_command import forbid_jira_calls, get_command, post_reply, run_command_again, thread_replies

REQUESTER = "mock-user-1"
ADMIN = "admin-object-id-2"
ESCALATION_CHANNEL = "19:mock-escalations@thread.tacv2"


@pytest.fixture
def settings():
    # the new variable name; TEAMS_CLOSE_ALLOWED_USER_IDS keeps working (tested in test_close_command)
    return make_settings(teams_command_admin_user_ids=ADMIN)


@pytest.fixture
def ticket(container):
    rec = post_message(container)  # P2 / QA, Jira OPS-1 with priority "High"
    assert rec.jira_issue_key == "OPS-1" and rec.priority == "P2"
    return rec


def command_of(container, summary):
    return get_command(container, summary.command_ids[0])


def last_reply(container, rec) -> str:
    return thread_replies(container, rec.teams_message_id)[-1]


def comments(container, key="OPS-1") -> list[str]:
    return [adf_plain_text(c["body"]) for c in container.jira_client.issues[key]["comments"]]


def escalation_alerts(container) -> list[str]:
    return [m["body"]["content"] for m in container.graph.messages.values()
            if m["channelIdentity"]["channelId"] == ESCALATION_CHANNEL]


# =============================================================================================
# status
# =============================================================================================
def test_status_reports_status_assignee_priority_and_last_update(container, ticket):
    container.jira_client.issues["OPS-1"]["assignee"] = "Jane Engineer"
    container.jira_client.issues["OPS-1"]["status"] = "In Progress"
    _, summary = post_reply(container, ticket.teams_message_id, "status")
    cmd = command_of(container, summary)
    assert cmd.status is CommandStatus.COMPLETED and cmd.outcome is O.STATUS_REPORTED
    reply = last_reply(container, ticket)
    assert "<b>OPS-1</b> status: In Progress" in reply
    assert "Assignee: Jane Engineer" in reply and "Priority: High" in reply
    assert "Last updated: " in reply and " UTC" in reply
    assert "https://mock-jira.example.com/browse/OPS-1" in reply


def test_status_is_open_to_anyone_and_writes_nothing(container, ticket):
    _, summary = post_reply(container, ticket.teams_message_id, "/status", sender_id="random-viewer")
    assert command_of(container, summary).outcome is O.STATUS_REPORTED
    assert "Assignee: Unassigned" in last_reply(container, ticket)
    jira = container.jira_client
    assert jira.transition_calls == jira.comment_calls == jira.update_calls == 0
    assert get_request(container, ticket.id).processing_status is S.COMPLETED


def test_status_transient_failure_is_retried(container, ticket):
    container.jira_client.fail_next("details_transient")
    _, summary = post_reply(container, ticket.teams_message_id, "status")
    assert command_of(container, summary).status is CommandStatus.EXECUTING
    assert run_command_again(container, summary.command_ids[0]) is CommandStatus.COMPLETED


# =============================================================================================
# reopen
# =============================================================================================
def test_reopen_a_closed_ticket(container, ticket):
    post_reply(container, ticket.teams_message_id, "close Fixed after redeploy")
    assert get_request(container, ticket.id).processing_status is S.CLOSED

    reply, summary = post_reply(container, ticket.teams_message_id, "reopen still failing after deploy")
    cmd = command_of(container, summary)
    assert cmd.status is CommandStatus.COMPLETED and cmd.outcome is O.REOPENED
    assert container.jira_client.issues["OPS-1"]["status"] == "To Do"
    assert comments(container)[-1] == "Reopened from Microsoft Teams by Sample User. Reason: still failing after deploy"
    assert "🔄 OPS-1 has been reopened by Sample User." in last_reply(container, ticket)

    rec = get_request(container, ticket.id)
    assert rec.processing_status is S.COMPLETED
    assert rec.closed_at is None and rec.close_command_message_id is None
    assert rec.reopened_by_id == REQUESTER and rec.reopen_reason == "still failing after deploy"


def test_close_reopen_close_cycle(container, ticket):
    post_reply(container, ticket.teams_message_id, "close one")
    post_reply(container, ticket.teams_message_id, "reopen two")
    reply, _ = post_reply(container, ticket.teams_message_id, "close three")
    rec = get_request(container, ticket.id)
    assert rec.processing_status is S.CLOSED and rec.close_reason == "three"
    assert rec.close_command_message_id == reply["id"]
    assert container.jira_client.issues["OPS-1"]["status"] == "Done"
    assert container.jira_client.transition_calls == 3


def test_reopen_when_not_closed_is_refused(container, ticket):
    container.jira_client.issues["OPS-1"]["status"] = "In Progress"
    _, summary = post_reply(container, ticket.teams_message_id, "reopen")
    cmd = command_of(container, summary)
    assert cmd.status is CommandStatus.REJECTED and cmd.outcome is O.NOT_CLOSED
    assert container.jira_client.transition_calls == 0
    assert "OPS-1 is not closed (current status: In Progress)" in last_reply(container, ticket)


def test_reopen_unauthorized_makes_no_jira_call(container, ticket):
    post_reply(container, ticket.teams_message_id, "close")
    forbid_jira_calls(container)
    _, summary = post_reply(container, ticket.teams_message_id, "reopen", sender_id="someone-else")
    assert command_of(container, summary).outcome is O.UNAUTHORIZED
    assert ("You're not allowed to reopen OPS-1. Only the requester or a support admin can."
            in last_reply(container, ticket))


def test_admin_can_reopen(container, ticket):
    post_reply(container, ticket.teams_message_id, "close")
    _, summary = post_reply(container, ticket.teams_message_id, "reopen", sender_id=ADMIN, sender_name="Support Admin")
    assert command_of(container, summary).outcome is O.REOPENED
    assert get_request(container, ticket.id).reopened_by_name == "Support Admin"


def test_reopen_without_matching_transition(container, ticket):
    post_reply(container, ticket.teams_message_id, "close")
    container.jira_client.workflow = [JiraTransition("31", "Done", "Done")]  # no way back
    _, summary = post_reply(container, ticket.teams_message_id, "reopen")
    assert command_of(container, summary).outcome is O.NO_TRANSITION
    assert "Could not reopen OPS-1: the Jira workflow has no transition to 'To Do'" in last_reply(container, ticket)
    assert get_request(container, ticket.id).processing_status is S.CLOSED


def test_ambiguous_reopen_transition_is_not_repeated(container, ticket):
    post_reply(container, ticket.teams_message_id, "close")
    container.jira_client.fail_next("transition_ambiguous_after")
    _, summary = post_reply(container, ticket.teams_message_id, "reopen")
    assert run_command_again(container, summary.command_ids[0]) is CommandStatus.COMPLETED
    assert container.jira_client.transition_calls == 2  # one close + one reopen
    assert command_of(container, summary).outcome is O.REOPENED


# =============================================================================================
# escalate
# =============================================================================================
def test_escalate_raises_priority_and_runs_escalation_flow(container, ticket):
    _, summary = post_reply(container, ticket.teams_message_id, "escalate P1 Customer-facing outage")
    cmd = command_of(container, summary)
    assert cmd.status is CommandStatus.COMPLETED and cmd.outcome is O.ESCALATED

    issue = container.jira_client.issues["OPS-1"]
    assert issue["fields"]["priority"] == {"name": "Highest"}
    assert "escalated" in issue["fields"]["labels"]
    assert comments(container)[-1] == (
        "Escalated from P2 to P1 from Microsoft Teams by Sample User. Reason: Customer-facing outage")

    alerts = escalation_alerts(container)
    assert len(alerts) == 1
    assert "Escalation: OPS-1 raised from P2 to P1" in alerts[0] and "Customer-facing outage" in alerts[0]
    assert "[QA] CustomerOrderAPI: Order creation is failing" in alerts[0]

    reply = last_reply(container, ticket)
    assert "🚨 OPS-1 has been escalated from P2 to P1 by Sample User." in reply
    assert "The escalation channel has been notified." in reply

    rec = get_request(container, ticket.id)
    assert rec.priority == "P1" and rec.escalated_from_priority == "P2"
    assert rec.escalated_by_id == REQUESTER and rec.escalation_reason == "Customer-facing outage"
    assert rec.escalated_at is not None


def test_admin_can_escalate(container, ticket):
    _, summary = post_reply(container, ticket.teams_message_id, "escalate P1", sender_id=ADMIN)
    assert command_of(container, summary).outcome is O.ESCALATED


def test_escalate_unauthorized_makes_no_jira_call(container, ticket):
    forbid_jira_calls(container)
    container.jira_client.update_issue = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Jira"))
    _, summary = post_reply(container, ticket.teams_message_id, "escalate P1", sender_id="someone-else")
    assert command_of(container, summary).outcome is O.UNAUTHORIZED
    assert "You're not allowed to escalate OPS-1." in last_reply(container, ticket)
    assert escalation_alerts(container) == []


@pytest.mark.parametrize("requested", ["P2", "P3"])
def test_escalate_must_raise_priority(container, ticket, requested):
    _, summary = post_reply(container, ticket.teams_message_id, f"escalate {requested}")
    cmd = command_of(container, summary)
    assert cmd.status is CommandStatus.REJECTED and cmd.outcome is O.NOT_HIGHER
    assert container.jira_client.update_calls == 0 and escalation_alerts(container) == []
    assert f"OPS-1 is already P2. `escalate` can only raise the priority ({requested} is not higher than P2)." \
        in last_reply(container, ticket)


@pytest.mark.parametrize("text", ["escalate", "escalate P9", "escalate urgent"])
def test_escalate_with_invalid_arguments_gets_usage(container, ticket, text):
    _, summary = post_reply(container, ticket.teams_message_id, text)
    cmd = command_of(container, summary)
    assert cmd.status is CommandStatus.REJECTED and cmd.outcome is O.INVALID_ARGS
    assert container.jira_client.update_calls == 0
    assert "Usage: escalate P1|P2|P3 [reason]" in last_reply(container, ticket)


def test_escalate_closed_ticket_is_refused(container, ticket):
    post_reply(container, ticket.teams_message_id, "close")
    _, summary = post_reply(container, ticket.teams_message_id, "escalate P1")
    assert command_of(container, summary).outcome is O.TICKET_CLOSED
    assert "OPS-1 is closed. Reply `reopen` first" in last_reply(container, ticket)


def test_escalate_twice_p3_to_p2_to_p1():
    c = build_container(make_settings(), sync_dispatch=True)
    c.db.create_all()
    rec = post_message(c, "API Name: X\nEnvironment: UAT\nIssue Summary: s\nIssue Description: d\n"
                          "Correlation ID: C-1\nBusiness Impact: b\nPriority: P3")
    post_reply(c, rec.teams_message_id, "escalate P2")
    post_reply(c, rec.teams_message_id, "escalate P1")
    assert get_request(c, rec.id).priority == "P1"
    assert get_request(c, rec.id).escalated_from_priority == "P2"
    assert c.jira_client.issues["OPS-1"]["fields"]["labels"].count("escalated") == 1  # label added once
    c.close()


def test_escalate_when_jira_rejects_priority_still_labels_and_alerts(container, ticket):
    container.jira_client.reject_priority_updates = True
    _, summary = post_reply(container, ticket.teams_message_id, "escalate P1")
    assert command_of(container, summary).outcome is O.ESCALATED
    issue = container.jira_client.issues["OPS-1"]
    assert issue["fields"]["priority"] == {"name": "High"}  # unchanged
    assert "escalated" in issue["fields"]["labels"]
    assert len(escalation_alerts(container)) == 1
    assert "Jira did not accept a priority change" in last_reply(container, ticket)
    assert get_request(container, ticket.id).priority == "P1"  # OpsBridge priority still raised


def test_escalate_without_escalation_channel_skips_alert():
    c = build_container(make_settings(mock_mode=True, teams_escalation_channel_id=""), sync_dispatch=True)
    c.db.create_all()
    # mock mode supplies a default escalation channel; disable it explicitly for this test
    c.commands.config = dataclasses.replace(c.commands.config, escalation_channel_id="")
    rec = post_message(c)
    _, summary = post_reply(c, rec.teams_message_id, "escalate P1")
    assert get_command(c, summary.command_ids[0]).outcome is O.ESCALATED
    assert "escalation channel has been notified" not in thread_replies(c, rec.teams_message_id)[-1]
    assert get_command(c, summary.command_ids[0]).alerted_at is None
    c.close()


def test_ambiguous_alert_post_is_not_duplicated(container, ticket):
    container.graph.fail_next("post_ambiguous_after")
    _, summary = post_reply(container, ticket.teams_message_id, "escalate P1")
    assert command_of(container, summary).status is CommandStatus.EXECUTING
    assert run_command_again(container, summary.command_ids[0]) is CommandStatus.COMPLETED
    assert len(escalation_alerts(container)) == 1
    assert len(comments(container)) == 1


def test_escalate_jira_update_transient_failure_is_retried(container, ticket):
    container.jira_client.fail_next("update_transient")
    _, summary = post_reply(container, ticket.teams_message_id, "escalate P1")
    cmd = command_of(container, summary)
    assert cmd.status is CommandStatus.EXECUTING and "503" in cmd.error_message
    assert run_command_again(container, summary.command_ids[0]) is CommandStatus.COMPLETED
    assert get_request(container, ticket.id).priority == "P1"


def test_duplicate_escalate_notifications_run_once(container, ticket):
    post_reply(container, ticket.teams_message_id, "escalate P1", deliveries=3)
    assert container.jira_client.update_calls == 1
    assert len(escalation_alerts(container)) == 1 and len(comments(container)) == 1


# =============================================================================================
# config + API
# =============================================================================================
def test_escalation_channel_must_differ_from_support_channel():
    with pytest.raises(ConfigurationError, match="TEAMS_ESCALATION_CHANNEL_ID"):
        Settings(_env_file=None, teams_channel_id="19:same@thread.tacv2",
                 teams_escalation_channel_id="19:same@thread.tacv2").check()


def test_both_admin_variables_are_merged():
    s = Settings(_env_file=None, teams_command_admin_user_ids="a, b", teams_close_allowed_user_ids="c")
    assert s.command_admin_user_ids == frozenset({"a", "b", "c"})


def test_api_shows_escalation_and_command_details(client, container, admin_headers, ticket):
    post_reply(container, ticket.teams_message_id, "escalate P1 outage")
    detail = client.get(f"/support-requests/{ticket.id}", headers=admin_headers).json()
    assert detail["priority"] == "P1" and detail["escalated_from_priority"] == "P2"
    assert detail["escalation_reason"] == "outage" and detail["escalated_at"]
    command = detail["commands"][-1]
    assert command["command"] == "escalate" and command["outcome"] == "ESCALATED"
    assert command["details"]["from_priority"] == "P2" and command["alerted_at"]
