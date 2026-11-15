"""Jira -> Teams sync: status/assignee changes in Jira are posted in the request thread."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.container import build_container
from app.domain.enums import NotificationStatus
from app.domain.enums import ProcessingStatus as S
from app.integrations.jira_client import HttpJiraClient
from app.models.teams_notification import TeamsNotification
from tests.conftest import get_request, make_settings, post_message
from tests.test_close_command import post_reply, thread_replies

SECRET = "hook-secret-123"


@pytest.fixture
def settings():
    return make_settings(jira_webhook_secret=SECRET)


@pytest.fixture
def ticket(container):
    rec = post_message(container)
    assert rec.jira_issue_key == "OPS-1"
    return rec


def notifications(container) -> list[TeamsNotification]:
    with container.db.session() as session:
        rows = session.query(TeamsNotification).order_by(TeamsNotification.id).all()
        session.expunge_all()
        return rows


def updates_in_thread(container, rec) -> list[str]:
    return [r for r in thread_replies(container, rec.teams_message_id) if "was updated in Jira" in r]


def jira_time(offset_seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f%z")


def webhook_body(key: str = "OPS-1", *, status: str = "In Progress", assignee: str | None = None,
                 updated: str | None = None, actor: str = "Jane Engineer", event: str = "jira:issue_updated") -> bytes:
    return json.dumps({
        "timestamp": 1, "webhookEvent": event, "user": {"displayName": actor, "accountId": "acct-actor"},
        "issue": {"key": key, "fields": {
            "status": {"name": status},
            "assignee": {"accountId": f"acct-{assignee}", "displayName": assignee} if assignee else None,
            "updated": updated or jira_time(60),
        }},
    }).encode()


def signed(body: bytes, secret: str = SECRET) -> dict[str, str]:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature": f"sha256={sig}", "Content-Type": "application/json"}


# =============================================================================================
# baseline + polling
# =============================================================================================
def test_initial_jira_state_is_recorded_when_ticket_is_created(container, ticket):
    rec = get_request(container, ticket.id)
    assert rec.jira_status == "To Do" and rec.jira_assignee_id is None and rec.jira_updated_at is not None
    assert notifications(container) == []


def test_status_change_in_jira_is_posted_in_thread(container, ticket):
    container.jira_client.change_in_jira("OPS-1", status="In Progress")
    assert container.jira_sync.poll_once() == 1

    [note] = notifications(container)
    assert note.status is NotificationStatus.SENT and note.payload["source"] == "poll"
    [reply] = updates_in_thread(container, ticket)
    assert "🔔 <b>OPS-1</b> was updated in Jira" in reply
    assert "Status: To Do → <b>In Progress</b>" in reply
    assert "Assignee" not in reply
    assert "https://mock-jira.example.com/browse/OPS-1" in reply
    assert get_request(container, ticket.id).jira_status == "In Progress"

    assert container.jira_sync.poll_once() == 0  # nothing new: no second post
    assert len(updates_in_thread(container, ticket)) == 1


def test_assignee_change_is_posted(container, ticket):
    container.jira_client.change_in_jira("OPS-1", assignee="Jane Engineer")
    container.jira_sync.poll_once()
    [reply] = updates_in_thread(container, ticket)
    assert "Assignee: Unassigned → <b>Jane Engineer</b>" in reply and "Status:" not in reply

    container.jira_client.change_in_jira("OPS-1", assignee=None)
    container.jira_sync.poll_once()
    assert "Assignee: Jane Engineer → <b>Unassigned</b>" in updates_in_thread(container, ticket)[-1]


def test_status_and_assignee_changed_together_make_one_post(container, ticket):
    container.jira_client.change_in_jira("OPS-1", status="In Progress", assignee="Jane Engineer")
    container.jira_sync.poll_once()
    [reply] = updates_in_thread(container, ticket)
    assert "Status: To Do → <b>In Progress</b>" in reply and "Assignee: Unassigned → <b>Jane Engineer</b>" in reply


def test_other_updates_are_not_posted(container, ticket):
    container.jira_client.add_comment("OPS-1", {"type": "doc", "version": 1, "content": []})  # bumps `updated`
    container.jira_client.update_issue("OPS-1", update={"labels": [{"add": "x"}]})
    assert container.jira_sync.poll_once() == 0
    assert updates_in_thread(container, ticket) == []


def test_first_observation_without_baseline_only_records_it(container, ticket):
    with container.db.session() as session:
        from app.models.support_request import SupportRequest

        row = session.get(SupportRequest, ticket.id)
        row.jira_status = row.jira_updated_at = None  # e.g. ticket created before this feature existed
    container.jira_client.change_in_jira("OPS-1", status="In Progress")
    assert container.jira_sync.poll_once() == 0
    assert get_request(container, ticket.id).jira_status == "In Progress"
    container.jira_client.change_in_jira("OPS-1", status="Blocked")
    assert container.jira_sync.poll_once() == 1


def test_issues_not_created_by_opsbridge_are_ignored(container, ticket):
    container.jira_client.create_issue({"project": {"key": "OPS"}, "labels": []})  # OPS-2, created in Jira directly
    container.jira_client.change_in_jira("OPS-2", status="In Progress")
    assert container.jira_sync.poll_once() == 0


def test_sync_disabled_does_nothing():
    c = build_container(make_settings(jira_sync_enabled=False), sync_dispatch=True)
    c.db.create_all()
    post_message(c)
    c.jira_client.change_in_jira("OPS-1", status="In Progress")
    assert c.jira_sync.poll_once() == 0
    c.close()


# =============================================================================================
# keeping OpsBridge in step / not echoing our own changes
# =============================================================================================
def test_close_from_teams_is_not_announced_twice(container, ticket):
    post_reply(container, ticket.teams_message_id, "close fixed")
    assert container.jira_sync.poll_once() == 0  # the ✅ command reply already said it
    rec = get_request(container, ticket.id)
    assert rec.jira_status == "Done" and rec.jira_expected_status is None
    assert updates_in_thread(container, ticket) == []

    container.jira_client.change_in_jira("OPS-1", status="In Progress")  # a person reopens it in Jira
    assert container.jira_sync.poll_once() == 1
    assert "Status: Done → <b>In Progress</b>" in updates_in_thread(container, ticket)[-1]


def test_closed_and_reopened_directly_in_jira_updates_request(client, container, ticket):
    container.jira_client.change_in_jira("OPS-1", status="Done")
    client.post("/webhooks/jira", content=(b := webhook_body(status="Done", actor="Jane Engineer")), headers=signed(b))
    rec = get_request(container, ticket.id)
    assert rec.processing_status is S.CLOSED
    assert rec.closed_by_name == "Jane Engineer" and rec.close_reason == "Closed in Jira"
    assert "Changed by: Jane Engineer" in updates_in_thread(container, ticket)[-1]

    body = webhook_body(status="To Do", updated=jira_time(120), actor="Sam Lead")
    client.post("/webhooks/jira", content=body, headers=signed(body))
    rec = get_request(container, ticket.id)
    assert rec.processing_status is S.COMPLETED and rec.closed_at is None
    assert rec.reopened_by_name == "Sam Lead" and rec.reopen_reason == "Reopened in Jira"


def test_failed_close_command_clears_expected_status(container, ticket):
    container.jira_client.fail_next("transition_permanent")
    post_reply(container, ticket.teams_message_id, "close")
    assert get_request(container, ticket.id).jira_expected_status is None
    container.jira_client.change_in_jira("OPS-1", status="Done")  # a person then closes it in Jira
    assert container.jira_sync.poll_once() == 1  # announced: it was not OpsBridge's change


# =============================================================================================
# webhook
# =============================================================================================
def test_webhook_with_valid_signature_posts_update(client, container, ticket):
    body = webhook_body(status="In Progress", assignee="Jane Engineer")
    response = client.post("/webhooks/jira", content=body, headers=signed(body))
    assert response.status_code == 202
    [reply] = updates_in_thread(container, ticket)
    assert "Status: To Do → <b>In Progress</b>" in reply and "Jane Engineer" in reply
    assert notifications(container)[0].payload["source"] == "webhook"


def test_webhook_query_secret_is_accepted(client, container, ticket):
    response = client.post(f"/webhooks/jira?secret={SECRET}", content=webhook_body(),
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 202 and len(updates_in_thread(container, ticket)) == 1


@pytest.mark.parametrize("headers", [
    {"X-Hub-Signature": "sha256=" + "0" * 64},
    {},
])
def test_webhook_with_bad_or_missing_signature_is_rejected(client, container, ticket, headers):
    response = client.post("/webhooks/jira", content=webhook_body(), headers=headers)
    assert response.status_code == 401
    assert client.post("/webhooks/jira?secret=wrong", content=webhook_body()).status_code == 401
    assert notifications(container) == []


def test_webhook_disabled_without_secret(container):
    from fastapi.testclient import TestClient

    from app.main import create_app

    c = build_container(make_settings(), sync_dispatch=True)
    with TestClient(create_app(container=c)) as tc:
        assert tc.post("/webhooks/jira", content=b"{}").status_code == 404


def test_webhook_rejects_malformed_json(client):
    body = b"not json"
    assert client.post("/webhooks/jira", content=body, headers=signed(body)).status_code == 400


def test_webhook_ignores_other_events(client, container, ticket):
    body = webhook_body(event="comment_created")
    assert client.post("/webhooks/jira", content=body, headers=signed(body)).status_code == 202
    assert notifications(container) == []


def test_duplicate_and_out_of_order_webhooks(client, container, ticket):
    newer = webhook_body(status="In Review", updated=jira_time(120))
    older = webhook_body(status="In Progress", updated=jira_time(60))
    for _ in range(3):  # redelivered
        client.post("/webhooks/jira", content=newer, headers=signed(newer))
    client.post("/webhooks/jira", content=older, headers=signed(older))  # arrives late
    assert len(notifications(container)) == 1
    assert get_request(container, ticket.id).jira_status == "In Review"


def test_webhook_and_poll_seeing_the_same_change_post_once(client, container, ticket):
    container.jira_client.change_in_jira("OPS-1", status="In Progress")
    updated = container.jira_client.issues["OPS-1"]["updated"].strftime("%Y-%m-%dT%H:%M:%S.%f%z")
    body = webhook_body(status="In Progress", updated=updated)
    client.post("/webhooks/jira", content=body, headers=signed(body))
    assert container.jira_sync.poll_once() == 0
    assert len(updates_in_thread(container, ticket)) == 1


def test_concurrent_observers_create_one_notification(tmp_path):
    c = build_container(make_settings(database_url=f"sqlite:///{tmp_path / 'sync.db'}"), sync_dispatch=False)
    c.db.create_all()
    rec = post_message(c)
    c.processor.process(rec.id)
    c.jira_client.change_in_jira("OPS-1", status="In Progress")
    [state] = c.jira_client.search_recently_updated("OPS", 10)
    barrier = threading.Barrier(6)
    results = []

    def observe():
        barrier.wait()
        results.append(c.jira_sync.observe(state, source="race"))

    threads = [threading.Thread(target=observe) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len([r for r in results if r is not None]) == 1
    assert len(notifications(c)) == 1
    c.close()


# =============================================================================================
# delivery reliability
# =============================================================================================
def test_teams_failure_is_retried(container, ticket):
    container.graph.fail_next("reply_transient")
    container.jira_client.change_in_jira("OPS-1", status="In Progress")
    container.jira_sync.poll_once()
    [note] = notifications(container)
    assert note.status is NotificationStatus.PENDING and note.retry_count == 1

    with container.db.session() as session:
        session.get(TeamsNotification, note.id).next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert container.thread_updates.process(note.id) is NotificationStatus.SENT
    assert len(updates_in_thread(container, ticket)) == 1


def test_ambiguous_teams_reply_is_not_doubled(container, ticket):
    container.graph.fail_next("reply_ambiguous_after")
    container.jira_client.change_in_jira("OPS-1", status="In Progress")
    container.jira_sync.poll_once()
    [note] = notifications(container)
    with container.db.session() as session:
        session.get(TeamsNotification, note.id).next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert container.thread_updates.process(note.id) is NotificationStatus.SENT
    assert len(updates_in_thread(container, ticket)) == 1


def test_permanent_teams_failure_marks_failed(container, ticket):
    container.graph.fail_next("reply_permanent")
    container.jira_client.change_in_jira("OPS-1", status="In Progress")
    container.jira_sync.poll_once()
    [note] = notifications(container)
    assert note.status is NotificationStatus.FAILED and "403" in note.error_message


# =============================================================================================
# API, dev endpoint, HTTP client
# =============================================================================================
def test_api_detail_shows_jira_state_and_notifications(client, container, admin_headers, ticket):
    container.jira_client.change_in_jira("OPS-1", status="In Progress", assignee="Jane Engineer")
    container.jira_sync.poll_once()
    detail = client.get(f"/support-requests/{ticket.id}", headers=admin_headers).json()
    assert detail["jira_status"] == "In Progress" and detail["jira_assignee_name"] == "Jane Engineer"
    assert detail["notifications"][0]["status"] == "SENT"
    assert detail["notifications"][0]["payload"]["new_status"] == "In Progress"


def test_dev_jira_change_endpoint(client, container, admin_headers, ticket):
    response = client.post("/dev/jira-change", json={"jira_key": "OPS-1", "status": "In Progress"},
                           headers=admin_headers)
    assert response.json() == {"notifications_created": 1}


def test_http_search_recently_updated_paginates():
    pages = [
        {"issues": [{"key": "OPS-1", "fields": {"status": {"name": "To Do"}, "assignee": None,
                                                 "updated": "2026-01-01T10:00:00.000+0000"}}],
         "nextPageToken": "p2", "isLast": False},
        {"issues": [{"key": "OPS-2", "fields": {"status": {"name": "Done"},
                                                 "assignee": {"accountId": "a1", "displayName": "Jane"},
                                                 "updated": "2026-01-01T11:00:00.000+0000"}}],
         "isLast": True},
    ]
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=pages[len(bodies) - 1])

    client = HttpJiraClient(base_url="https://x", email="e", api_token="t", transport=httpx.MockTransport(handler))
    states = client.search_recently_updated("OPS", 6)
    assert [(s.key, s.status, s.assignee_id, s.assignee_name) for s in states] == [
        ("OPS-1", "To Do", None, None), ("OPS-2", "Done", "a1", "Jane")]
    assert bodies[0]["jql"] == 'project = "OPS" AND updated >= "-6m" ORDER BY updated ASC'
    assert bodies[0]["fields"] == ["status", "assignee", "updated"]
    assert bodies[1]["nextPageToken"] == "p2"


def test_missing_baseline_is_backfilled_so_first_change_is_announced(container, ticket):
    """Tickets from before the sync existed (no baseline, not recently updated) must not lose their first change."""
    with container.db.session() as session:
        from app.models.support_request import SupportRequest

        row = session.get(SupportRequest, ticket.id)
        row.jira_status = row.jira_assignee_id = row.jira_assignee_name = row.jira_updated_at = None
    container.jira_client.issues["OPS-1"]["updated"] -= timedelta(days=3)  # outside the poll window

    assert container.jira_sync.poll_once() == 0  # backfill only
    assert get_request(container, ticket.id).jira_status == "To Do"

    container.jira_client.change_in_jira("OPS-1", status="In Progress")
    assert container.jira_sync.poll_once() == 1
    assert "Status: To Do → <b>In Progress</b>" in updates_in_thread(container, ticket)[-1]
