"""Jira ticket composition and creation. Knows nothing about Teams parsing or HTTP details."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.core.errors import PermanentError
from app.domain.messages import SupportRequestDraft
from app.integrations.jira_client import JiraClient, JiraIssueRef
from app.services.intake_template import SPECS_BY_KEY

logger = logging.getLogger("opsbridge.jira_service")

SUMMARY_MAX = 255


@dataclass(frozen=True)
class TicketRequest:
    draft: SupportRequestDraft
    teams_message_id: str
    teams_message_url: str | None
    sender_name: str | None
    opsbridge_request_id: int


def idempotency_label(teams_message_id: str) -> str:
    """Deterministic label used to find an issue whose creation outcome was ambiguous."""
    digest = hashlib.sha256(teams_message_id.encode("utf-8")).hexdigest()[:16]
    return f"opsbridge-{digest}"


def _label_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "unknown"


# --- Atlassian Document Format helpers ------------------------------------------------------
def _text(value: str, *, strong: bool = False, href: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    marks = []
    if strong:
        marks.append({"type": "strong"})
    if href:
        marks.append({"type": "link", "attrs": {"href": href}})
    if marks:
        node["marks"] = marks
    return node


def _paragraphs(value: str) -> list[dict[str, Any]]:
    blocks = []
    for chunk in re.split(r"\n\s*\n", value.strip()):
        content: list[dict[str, Any]] = []
        for i, line in enumerate(chunk.split("\n")):
            if i:
                content.append({"type": "hardBreak"})
            if line:
                content.append(_text(line))
        if content:
            blocks.append({"type": "paragraph", "content": content})
    return blocks or [{"type": "paragraph", "content": [_text("-")]}]


def _heading(value: str) -> dict[str, Any]:
    return {"type": "heading", "attrs": {"level": 3}, "content": [_text(value)]}


def _bullets(items: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": c}]} for c in items],
    }


class JiraService:
    def __init__(
        self,
        client: JiraClient,
        *,
        project_key: str,
        issue_type: str,
        priority_map: dict[str, str],
        extra_labels: list[str],
    ) -> None:
        self.client = client
        self.project_key = project_key
        self.issue_type = issue_type
        self.priority_map = priority_map
        self.extra_labels = extra_labels

    # ---- composition -------------------------------------------------------------------------
    def build_summary(self, draft: SupportRequestDraft) -> str:
        summary = f"[{draft.environment.value}] {draft.api_name}: {draft.issue_summary}"
        return summary if len(summary) <= SUMMARY_MAX else summary[: SUMMARY_MAX - 1] + "…"

    def build_labels(self, ticket: TicketRequest) -> list[str]:
        d = ticket.draft
        labels = [
            *self.extra_labels,
            idempotency_label(ticket.teams_message_id),
            f"env-{d.environment.value.lower()}",
            f"priority-{d.priority.value.lower()}",
            f"api-{_label_token(d.api_name)}",
        ]
        return [label.replace(" ", "-") for label in dict.fromkeys(labels)]

    def build_description(self, ticket: TicketRequest) -> dict[str, Any]:
        d = ticket.draft
        details: list[list[dict[str, Any]]] = [
            [_text("API Name: ", strong=True), _text(d.api_name)],
            [_text("Environment: ", strong=True), _text(d.environment.value)],
            [_text("Priority: ", strong=True), _text(d.priority.value)],
            [_text("Correlation ID: ", strong=True), _text(d.correlation_id)],
            [_text("Reported by: ", strong=True), _text(ticket.sender_name or "Unknown")],
        ]
        if ticket.teams_message_url:
            details.append(
                [_text("Teams message: ", strong=True), _text("Open in Teams", href=ticket.teams_message_url)]
            )
        content: list[dict[str, Any]] = [
            _heading("Issue Description"),
            *_paragraphs(d.issue_description),
            _heading("Business Impact"),
            *_paragraphs(d.business_impact),
            _heading("Request Details"),
            _bullets(details),
        ]
        for key, value in d.optional_fields.items():
            content += [_heading(SPECS_BY_KEY[key].label), *_paragraphs(value)]
        content.append(
            {
                "type": "paragraph",
                "content": [
                    _text(
                        f"Created automatically by OpsBridge (request #{ticket.opsbridge_request_id}, "
                        f"Teams message {ticket.teams_message_id})."
                    )
                ],
            }
        )
        return {"type": "doc", "version": 1, "content": content}

    def build_fields(self, ticket: TicketRequest, *, include_priority: bool = True) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "project": {"key": self.project_key},
            "issuetype": {"name": self.issue_type},
            "summary": self.build_summary(ticket.draft),
            "description": self.build_description(ticket),
            "labels": self.build_labels(ticket),
        }
        jira_priority = self.priority_map.get(ticket.draft.priority.value)
        if include_priority and jira_priority:
            fields["priority"] = {"name": jira_priority}
        return fields

    # ---- operations ----------------------------------------------------------------------------
    def find_existing(self, teams_message_id: str) -> JiraIssueRef | None:
        return self.client.find_issue_by_label(self.project_key, idempotency_label(teams_message_id))

    def create_issue(self, ticket: TicketRequest) -> JiraIssueRef:
        """Create the issue. Raises TransientError / AmbiguousError / PermanentError.

        If Jira rejects the priority field (not on the create screen, or an unknown name),
        retry once without it: a 400 guarantees nothing was created, so this is safe.
        """
        try:
            return self.client.create_issue(self.build_fields(ticket))
        except PermanentError as exc:
            field_errors = exc.details.get("field_errors") or {}
            if exc.status_code == 400 and "priority" in field_errors and len(field_errors) == 1:
                logger.warning(
                    "Jira rejected priority field; creating issue without priority",
                    extra={"jira_error": str(field_errors["priority"])[:200]},
                )
                return self.client.create_issue(self.build_fields(ticket, include_priority=False))
            raise
