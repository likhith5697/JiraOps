"""Mock-mode-only endpoints to exercise the full pipeline locally without Microsoft or Jira.

`/dev/simulate-message` stores a Graph-shaped message in the mock Graph client and then feeds
a Graph-shaped change notification through the *same* ingestion path the real webhook uses.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_container
from app.container import Container
from app.core.security import require_admin
from app.integrations.jira_client import MockJiraClient, adf_plain_text
from app.integrations.microsoft_graph import MockGraphClient
from app.schemas.graph import ChangeNotification

router = APIRouter(prefix="/dev", tags=["dev (mock mode only)"], dependencies=[Depends(require_admin)])


class SimulateMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000, description="Message body (plain text or HTML)")
    content_type: Literal["text", "html"] = "text"
    sender_name: str = "Sample User"
    sender_id: str = Field(default="mock-user-1", description="Use the same id as the requester to close their ticket")
    as_bot: bool = False
    reply_to_message_id: str | None = None
    duplicate_deliveries: int = Field(default=1, ge=1, le=10,
                                      description="Deliver the same notification N times (redelivery test)")


class SimulateMessageResponse(BaseModel):
    message_id: str
    request_id: int | None
    command_id: int | None = None  # set when a reply was accepted as a thread command
    accepted: int
    duplicates: int
    ignored: int


class FailNextRequest(BaseModel):
    target: Literal["jira", "graph"]
    modes: list[str] = Field(min_length=1, max_length=20)


def _mocks(container: Container) -> tuple[MockGraphClient, MockJiraClient]:
    graph, jira = container.graph, container.jira_client
    if not isinstance(graph, MockGraphClient) or not isinstance(jira, MockJiraClient):
        raise HTTPException(status_code=404, detail="Not available outside mock mode")
    return graph, jira


@router.post("/simulate-message", response_model=SimulateMessageResponse)
async def simulate_message(
    body: SimulateMessageRequest, request: Request, container: Container = Depends(get_container)
) -> SimulateMessageResponse:
    graph, _ = _mocks(container)
    team, channel = container.settings.effective_team_id, container.settings.effective_channel_id
    message = graph.add_message(
        team_id=team, channel_id=channel, content=body.text, content_type=body.content_type,
        sender_id=body.sender_id, sender_name=body.sender_name, application=body.as_bot,
        reply_to_id=body.reply_to_message_id,
    )
    resource = f"teams('{team}')/channels('{channel}')/messages('{body.reply_to_message_id or message['id']}')"
    if body.reply_to_message_id:
        resource += f"/replies('{message['id']}')"
    notification = ChangeNotification(
        subscriptionId="mock-subscription", clientState=container.settings.effective_client_state,
        changeType="created", resource=resource,
    )
    summary = await run_in_threadpool(
        container.ingestion.ingest, [notification] * body.duplicate_deliveries,
        trace_id=getattr(request.state, "trace_id", None),
    )
    return SimulateMessageResponse(
        message_id=message["id"], request_id=summary.request_ids[0] if summary.request_ids else None,
        command_id=summary.command_ids[0] if summary.command_ids else None,
        accepted=summary.accepted, duplicates=summary.duplicates, ignored=summary.ignored,
    )


class JiraChangeRequest(BaseModel):
    jira_key: str = Field(examples=["OPS-1"])
    status: str | None = Field(default=None, examples=["In Progress"])
    assignee: str | None = Field(default=None, examples=["Jane Engineer"],
                                 description="Display name; send an empty string to unassign")


@router.post("/jira-change", summary="Simulate someone changing an issue in Jira, then run the Jira poll")
def jira_change(body: JiraChangeRequest, container: Container = Depends(get_container)) -> dict:
    _, jira = _mocks(container)
    if body.jira_key not in jira.issues:
        raise HTTPException(status_code=404, detail="Unknown mock Jira issue")
    kwargs: dict = {"status": body.status}
    if body.assignee is not None:
        kwargs["assignee"] = body.assignee or None
    jira.change_in_jira(body.jira_key, **kwargs)
    return {"notifications_created": container.jira_sync.poll_once()}


@router.get("/mock-state", summary="Inspect mock Jira issues and Teams replies")
def mock_state(container: Container = Depends(get_container)) -> dict:
    graph, jira = _mocks(container)
    return {
        "jira_issues": [
            {"key": k, "summary": v["fields"].get("summary"), "labels": v["fields"].get("labels"),
             "priority": (v["fields"].get("priority") or {}).get("name"), "status": v.get("status"),
             "assignee": v.get("assignee"),
             "comments": [adf_plain_text(c["body"]) for c in v.get("comments", [])]}
            for k, v in jira.issues.items()
        ],
        "teams_replies": {mid: [r["body"]["content"] for r in replies] for mid, replies in graph.replies.items()},
        "subscriptions": [s.__dict__ for s in graph.subscriptions.values()],
    }


@router.post("/fail-next", summary="Inject failures into the mock clients")
def fail_next(body: FailNextRequest, container: Container = Depends(get_container)) -> dict:
    graph, jira = _mocks(container)
    (jira if body.target == "jira" else graph).fail_next(*body.modes)
    return {"queued": body.modes, "target": body.target}
