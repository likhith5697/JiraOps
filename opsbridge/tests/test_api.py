"""Admin API: auth, pagination, detail, retry, subscriptions, health."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.time import utcnow
from app.domain.enums import ProcessingStatus as S
from tests.conftest import post_message


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/support-requests"),
        ("get", "/support-requests/1"),
        ("post", "/support-requests/1/retry"),
        ("post", "/subscriptions/create"),
        ("post", "/subscriptions/renew"),
        ("post", "/dev/simulate-message"),
    ],
)
def test_admin_endpoints_require_api_key(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"X-API-Key": "wrong"}).status_code == 401


def test_health_is_public(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "worker": "inline", "teams_subscription": "none",
                              "teams_subscription_expires_at": None}
    assert body["mock_mode"] is True


def test_list_pagination_and_status_filter(client, container, admin_headers):
    for _ in range(3):
        post_message(container)
    post_message(container, "Priority: P9")

    page = client.get("/support-requests?limit=2&offset=0", headers=admin_headers).json()
    assert page["total"] == 4 and page["limit"] == 2 and len(page["items"]) == 2
    assert page["items"][0]["id"] > page["items"][1]["id"]  # newest first

    last = client.get("/support-requests?limit=2&offset=2", headers=admin_headers).json()
    assert len(last["items"]) == 2

    rejected = client.get("/support-requests?status=REJECTED", headers=admin_headers).json()
    assert rejected["total"] == 1 and rejected["items"][0]["processing_status"] == "REJECTED"


def test_list_validates_query_parameters(client, admin_headers):
    assert client.get("/support-requests?limit=0", headers=admin_headers).status_code == 422
    assert client.get("/support-requests?limit=500", headers=admin_headers).status_code == 422
    assert client.get("/support-requests?status=BOGUS", headers=admin_headers).status_code == 422


def test_get_detail_and_404(client, container, admin_headers):
    rec = post_message(container)
    detail = client.get(f"/support-requests/{rec.id}", headers=admin_headers).json()
    assert detail["jira_issue_key"] == "OPS-1"
    assert detail["correlation_id"] == "CORR-123456"
    assert detail["acknowledgement_message_id"]
    assert client.get("/support-requests/9999", headers=admin_headers).status_code == 404


def test_retry_endpoint(client, container, admin_headers):
    container.jira_client.fail_next("permanent")
    rec = post_message(container)
    assert rec.processing_status is S.FAILED

    response = client.post(f"/support-requests/{rec.id}/retry", headers=admin_headers)
    assert response.status_code == 202
    assert response.json()["processing_status"] == "COMPLETED"

    # completed requests cannot be retried
    assert client.post(f"/support-requests/{rec.id}/retry", headers=admin_headers).status_code == 409
    assert client.post("/support-requests/9999/retry", headers=admin_headers).status_code == 404


def test_retry_rejects_locked_request(client, container, admin_headers):
    container.jira_client.fail_next("permanent")
    rec = post_message(container)
    with container.db.session() as session:
        from app.models.support_request import SupportRequest

        session.get(SupportRequest, rec.id).locked_until = utcnow() + timedelta(minutes=1)
    response = client.post(f"/support-requests/{rec.id}/retry", headers=admin_headers)
    assert response.status_code == 409


def test_subscription_create_is_idempotent_and_renewable(client, container, admin_headers):
    first = client.post("/subscriptions/create", headers=admin_headers)
    assert first.status_code == 201 and first.json()["action"] == "created"
    sub = first.json()["subscription"]
    assert sub["resource"].endswith("/messages")
    assert sub["notification_url"].endswith("/webhooks/teams")
    assert sub["lifecycle_notification_url"].endswith("/webhooks/teams/lifecycle")

    second = client.post("/subscriptions/create", headers=admin_headers)
    assert second.status_code == 200 and second.json()["action"] == "existing"

    forced = client.post("/subscriptions/create", json={"force": True}, headers=admin_headers)
    assert forced.status_code == 201

    renewed = client.post("/subscriptions/renew", json={"subscription_id": sub["subscription_id"]},
                          headers=admin_headers)
    assert renewed.status_code == 200 and renewed.json()[0]["action"] == "renewed"
    assert renewed.json()[0]["subscription"]["last_renewed_at"] is not None

    assert client.post("/subscriptions/renew", json={"subscription_id": "nope"},
                       headers=admin_headers).status_code == 404
    assert len(client.get("/subscriptions", headers=admin_headers).json()) == 2


def test_renewal_recreates_subscription_missing_in_graph(client, container, admin_headers):
    sub_id = client.post("/subscriptions/create", headers=admin_headers).json()["subscription"]["subscription_id"]
    assert sub_id in container.graph.subscriptions
    container.graph.subscriptions.clear()  # Graph deleted it (e.g. expired)
    response = client.post("/subscriptions/renew", headers=admin_headers)
    assert response.json()[0]["action"] == "recreated"


def test_dev_simulation_endpoint_runs_full_pipeline(client, admin_headers):
    from tests.conftest import VALID_MESSAGE

    body = client.post("/dev/simulate-message", json={"text": VALID_MESSAGE, "duplicate_deliveries": 3},
                       headers=admin_headers).json()
    assert body["accepted"] == 1 and body["duplicates"] == 2
    state = client.get("/dev/mock-state", headers=admin_headers).json()
    assert len(state["jira_issues"]) == 1
    assert len(state["teams_replies"][body["message_id"]]) == 1


def test_unhandled_errors_return_trace_id(container, admin_headers):
    from fastapi.testclient import TestClient

    from app.main import create_app

    def boom(**_kw):
        raise RuntimeError("database exploded")

    container.support_requests.list = boom
    with TestClient(create_app(container=container), raise_server_exceptions=False) as c:
        response = c.get("/support-requests", headers=admin_headers)
    assert response.status_code == 500
    assert response.json()["detail"] == "Internal server error"
    assert response.json()["trace_id"]
    assert "exploded" not in response.text


def test_health_reports_subscription_state(client, container, admin_headers):
    from datetime import timedelta

    from app.core.time import utcnow
    from app.models.graph_subscription import GraphSubscription

    client.post("/subscriptions/create", headers=admin_headers)
    assert client.get("/health").json()["checks"]["teams_subscription"] == "active"
    with container.db.session() as session:  # e.g. host was off past the renewal window
        for sub in session.query(GraphSubscription).all():
            sub.expiration_at = utcnow() - timedelta(hours=1)
    body = client.get("/health").json()
    assert body["checks"]["teams_subscription"] == "expired"
    assert body["status"] == "ok"  # mock mode: reported, not degraded


def test_periodic_jobs_run_on_first_sweep_even_right_after_boot(monkeypatch, container):
    """time.monotonic() starts near 0 after a (Docker VM) boot; the first renewal/poll/sweep must not wait."""
    from app.workers import background
    from app.workers.background import BackgroundWorker

    monkeypatch.setattr(background.time, "monotonic", lambda: 5.0)  # 5 s after boot
    calls = []

    class Spy:
        def renew(self, **kw):
            calls.append(("renew", kw))
            return []

        def ensure_active(self):
            calls.append(("ensure_active", {}))

    worker = BackgroundWorker(db=container.db, processor=container.processor, subscriptions=Spy(), worker_count=1,
                              poll_interval=10, subscription_check_interval=300, jira_sync=container.jira_sync,
                              jira_poll_interval=120)
    worker.knowledge = container.knowledge
    submitted = []
    worker.submit = lambda fn, *a: submitted.append(fn.__name__)
    worker._maybe_renew_subscriptions()
    worker._maybe_poll_jira()
    worker._maybe_sweep_knowledge()
    assert calls == [("renew", {"only_expiring": True}), ("ensure_active", {})]
    assert submitted == ["poll_once", "sweep"]


def test_live_mode_without_active_subscription_is_degraded(container):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(container=container)) as c:
        container.settings = container.settings.model_copy(update={"mock_mode": False})  # after startup checks
        response = c.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded" and response.json()["checks"]["teams_subscription"] == "none"


def test_subscription_self_heals_after_failed_recreate(client, container, admin_headers):
    from app.core.errors import TransientError

    assert container.subscriptions.ensure_active() is None  # never creates the first one on its own
    old_id = client.post("/subscriptions/create", headers=admin_headers).json()["subscription"]["subscription_id"]
    container.graph.subscriptions.clear()  # expired in Graph while the host was off

    real_create = container.graph.create_subscription
    container.graph.create_subscription = lambda **kw: (_ for _ in ()).throw(
        TransientError("tunnel down", service="microsoft_graph"))
    with pytest.raises(TransientError):
        container.subscriptions.renew(only_expiring=False)  # 404 -> REMOVED -> recreate fails
    assert client.get("/health").json()["checks"]["teams_subscription"] == "none"

    container.graph.create_subscription = real_create  # tunnel is back
    healed = container.subscriptions.ensure_active()
    assert healed is not None and healed.action == "recreated" and healed.subscription.subscription_id != old_id
    assert client.get("/health").json()["checks"]["teams_subscription"] == "active"
    assert container.subscriptions.ensure_active() is None  # nothing to do once active
