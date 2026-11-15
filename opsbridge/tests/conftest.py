from __future__ import annotations

from datetime import timedelta
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import Container, build_container
from app.core.time import utcnow
from app.domain.enums import ProcessingStatus
from app.main import create_app
from app.models.support_request import SupportRequest
from app.schemas.graph import ChangeNotification

ADMIN_KEY = "test-admin-key"

VALID_MESSAGE = """API Name: CustomerOrderAPI
Environment: QA

Issue Summary: Order creation is failing

Issue Description:
The API returns HTTP 500 when creating an order.
This started after the latest deployment.

Correlation ID: CORR-123456

Business Impact: Order processing is blocked in QA.

Priority: P2"""


def make_settings(**overrides) -> Settings:
    values = dict(
        _env_file=None,
        app_env="test",
        mock_mode=True,
        database_url="sqlite://",
        admin_api_key=ADMIN_KEY,
        background_worker_enabled=False,
        log_format="text",
        log_level="WARNING",
        max_retries=3,
        retry_base_delay_seconds=60,  # retries only happen when a test makes the row due again
        retry_max_delay_seconds=600,
        jira_reconcile_delay_seconds=30,
        jira_project_key="OPS",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    c = build_container(settings, sync_dispatch=True)
    c.db.create_all()
    yield c
    c.close()


@pytest.fixture
def client(container: Container) -> Iterator[TestClient]:
    app = create_app(container=container)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-API-Key": ADMIN_KEY}


# ---- helpers ------------------------------------------------------------------------------
def notification_for(container: Container, message_id: str, *, reply_id: str | None = None,
                     client_state: str | None = None) -> ChangeNotification:
    s = container.settings
    resource = f"teams('{s.effective_team_id}')/channels('{s.effective_channel_id}')/messages('{message_id}')"
    if reply_id:
        resource += f"/replies('{reply_id}')"
    return ChangeNotification(
        subscriptionId="sub-1",
        clientState=client_state if client_state is not None else s.effective_client_state,
        changeType="created",
        resource=resource,
    )


def notification_json(container: Container, message_id: str, **kw) -> dict:
    return {"value": [notification_for(container, message_id, **kw).model_dump(exclude_none=True)]}


def add_message(container: Container, text: str = VALID_MESSAGE, **kw) -> dict:
    s = container.settings
    return container.graph.add_message(  # type: ignore[attr-defined]
        team_id=s.effective_team_id, channel_id=s.effective_channel_id, content=text,
        content_type=kw.pop("content_type", "text"), **kw,
    )


def post_message(container: Container, text: str = VALID_MESSAGE, **kw) -> SupportRequest:
    """Simulate a Teams post + Graph notification; processing runs inline (sync dispatcher)."""
    message = add_message(container, text, **kw)
    summary = container.ingestion.ingest([notification_for(container, message["id"])], trace_id="test-trace")
    return get_request(container, summary.request_ids[0])


def get_request(container: Container, request_id: int) -> SupportRequest:
    rec = container.support_requests.get(request_id)
    assert rec is not None
    return rec


def make_due(container: Container, request_id: int) -> None:
    with container.db.session() as session:
        rec = session.get(SupportRequest, request_id)
        rec.next_attempt_at = utcnow() - timedelta(seconds=1)


def run_again(container: Container, request_id: int) -> ProcessingStatus | None:
    make_due(container, request_id)
    return container.processor.process(request_id)
