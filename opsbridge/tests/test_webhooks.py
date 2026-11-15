"""Scenario 12 (webhook validation) and 5 (duplicate delivery) at the HTTP layer."""

from __future__ import annotations

from datetime import timedelta

from app.core.time import utcnow
from app.domain.enums import ProcessingStatus as S
from app.domain.enums import SubscriptionStatus
from tests.conftest import add_message, notification_json


def test_subscription_validation_token_is_echoed_as_plain_text(client):
    response = client.post("/webhooks/teams?validationToken=Validation%3A%20Testing%20client%20application")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "Validation: Testing client application"


def test_lifecycle_validation_token_is_echoed(client):
    response = client.post("/webhooks/teams/lifecycle?validationToken=abc")
    assert response.status_code == 200 and response.text == "abc"


def test_invalid_client_state_is_rejected(client, container):
    message = add_message(container)
    response = client.post("/webhooks/teams", json=notification_json(container, message["id"], client_state="forged"))
    assert response.status_code == 401
    assert container.support_requests.list(status=None, limit=10, offset=0)[1] == 0


def test_missing_client_state_is_rejected(client, container):
    payload = {"value": [{"subscriptionId": "s", "changeType": "created", "resource": "teams('a')/channels('b')/messages('c')"}]}
    assert client.post("/webhooks/teams", json=payload).status_code == 401


def test_malformed_payload_returns_400(client):
    assert client.post("/webhooks/teams", content=b"not json", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/webhooks/teams", json={"value": [{"no": "subscriptionId"}]}).status_code == 400


def test_valid_notification_is_accepted_and_processed(client, container):
    message = add_message(container)
    response = client.post("/webhooks/teams", json=notification_json(container, message["id"]),
                           headers={"X-Request-ID": "trace-abc"})
    assert response.status_code == 202
    assert response.headers["X-Request-ID"] == "trace-abc"

    items, total = container.support_requests.list(status=None, limit=10, offset=0)
    assert total == 1
    assert items[0].processing_status is S.COMPLETED
    assert items[0].trace_id == "trace-abc"


def test_redelivered_notifications_are_idempotent(client, container):
    message = add_message(container)
    payload = notification_json(container, message["id"])
    for _ in range(3):
        assert client.post("/webhooks/teams", json=payload).status_code == 202
    assert container.support_requests.list(status=None, limit=10, offset=0)[1] == 1
    assert len(container.jira_client.issues) == 1
    assert len(container.graph.replies[message["id"]]) == 1


def test_reply_notifications_are_dropped_without_a_row(client, container):
    parent = add_message(container)
    payload = notification_json(container, parent["id"], reply_id="999")
    assert client.post("/webhooks/teams", json=payload).status_code == 202
    assert container.support_requests.list(status=None, limit=10, offset=0)[1] == 0


def test_notifications_for_other_channels_are_ignored(client, container):
    payload = notification_json(container, "123")
    payload["value"][0]["resource"] = "teams('other-team')/channels('19:other@thread.tacv2')/messages('123')"
    assert client.post("/webhooks/teams", json=payload).status_code == 202
    assert container.support_requests.list(status=None, limit=10, offset=0)[1] == 0


def test_path_style_resource_is_supported(client, container):
    message = add_message(container)
    s = container.settings
    payload = notification_json(container, message["id"])
    payload["value"][0]["resource"] = f"/teams/{s.effective_team_id}/channels/{s.effective_channel_id}/messages/{message['id']}"
    assert client.post("/webhooks/teams", json=payload).status_code == 202
    assert container.support_requests.list(status=None, limit=10, offset=0)[1] == 1


def _lifecycle(container, event: str, subscription_id: str) -> dict:
    return {"value": [{"subscriptionId": subscription_id, "clientState": container.settings.effective_client_state,
                       "lifecycleEvent": event, "resource": "x"}]}


def test_lifecycle_reauthorization_renews_subscription(client, container):
    outcome = container.subscriptions.create()
    before = outcome.subscription.expiration_at
    with container.db.session() as session:  # pretend it is about to expire
        from app.repositories.subscription_repository import SubscriptionRepository

        SubscriptionRepository(session).get_by_subscription_id(outcome.subscription.subscription_id).expiration_at = (
            utcnow() + timedelta(minutes=5)
        )
    response = client.post("/webhooks/teams/lifecycle",
                           json=_lifecycle(container, "reauthorizationRequired", outcome.subscription.subscription_id))
    assert response.status_code == 202
    renewed = container.subscriptions.list()[0]
    assert renewed.last_renewed_at is not None and renewed.expiration_at >= before - timedelta(seconds=5)


def test_lifecycle_subscription_removed_recreates(client, container):
    outcome = container.subscriptions.create()
    old_id = outcome.subscription.subscription_id
    client.post("/webhooks/teams/lifecycle", json=_lifecycle(container, "subscriptionRemoved", old_id))
    subs = {s.subscription_id: s.status for s in container.subscriptions.list()}
    assert subs[old_id] is SubscriptionStatus.REMOVED
    assert list(subs.values()).count(SubscriptionStatus.ACTIVE) == 1


def test_lifecycle_missed_triggers_catch_up(client, container):
    message = add_message(container)  # posted while notifications were being missed
    response = client.post("/webhooks/teams/lifecycle", json=_lifecycle(container, "missed", "sub-1"))
    assert response.status_code == 202
    rec = container.support_requests.list(status=None, limit=10, offset=0)[0][0]
    assert rec.teams_message_id == message["id"] and rec.processing_status is S.COMPLETED


def test_lifecycle_with_invalid_client_state_is_rejected(client, container):
    payload = _lifecycle(container, "subscriptionRemoved", "sub-1")
    payload["value"][0]["clientState"] = "forged"
    assert client.post("/webhooks/teams/lifecycle", json=payload).status_code == 401
