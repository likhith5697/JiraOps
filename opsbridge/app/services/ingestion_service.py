"""Webhook-side ingestion: verify, filter, persist (idempotently), enqueue. Must stay fast."""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.database.session import Database
from app.integrations.microsoft_graph import GraphClient
from app.repositories.support_repository import SupportRepository
from app.schemas.graph import ChangeNotification

logger = logging.getLogger("opsbridge.ingestion")

# Graph emits either OData-key style or path style resources.
_RESOURCE_PATTERNS = (
    re.compile(
        r"^/?teams\('(?P<team>[^']+)'\)/channels\('(?P<channel>[^']+)'\)/messages\('(?P<message>[^']+)'\)"
        r"(?:/replies\('(?P<reply>[^']+)'\))?$"
    ),
    re.compile(
        r"^/?teams/(?P<team>[^/]+)/channels/(?P<channel>[^/]+)/messages/(?P<message>[^/]+)"
        r"(?:/replies/(?P<reply>[^/]+))?$"
    ),
)


@dataclass(frozen=True)
class ParsedResource:
    team_id: str
    channel_id: str
    message_id: str
    reply_id: str | None


def parse_resource(resource: str | None) -> ParsedResource | None:
    if not resource:
        return None
    for pattern in _RESOURCE_PATTERNS:
        match = pattern.match(resource.strip())
        if match:
            return ParsedResource(match["team"], match["channel"], match["message"], match["reply"])
    return None


class Dispatcher(Protocol):
    def enqueue(self, request_id: int) -> None: ...

    def enqueue_command(self, command_id: int) -> None: ...

    def enqueue_notification(self, notification_id: int) -> None: ...

    def enqueue_ask(self, ask_id: int) -> None: ...

    def submit(self, fn, *args) -> None: ...  # noqa: ANN001 - generic background task


@dataclass
class IngestSummary:
    accepted: int = 0
    duplicates: int = 0
    ignored: int = 0
    rejected: int = 0
    commands: int = 0  # replies accepted as thread-command candidates
    request_ids: list[int] = field(default_factory=list)
    command_ids: list[int] = field(default_factory=list)

    @property
    def all_rejected(self) -> bool:
        return self.rejected > 0 and (self.accepted + self.commands + self.duplicates + self.ignored) == 0


def client_state_matches(received: str | None, expected: str) -> bool:
    return bool(received) and bool(expected) and secrets.compare_digest(received.encode(), expected.encode())


class IngestionService:
    def __init__(
        self,
        *,
        db: Database,
        dispatcher: Dispatcher,
        team_id: str,
        channel_id: str,
        client_state: str,
        tenant_id: str | None = None,
        command_intake: Any = None,  # CommandIntake; optional so intake works without commands
    ) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self.team_id = team_id
        self.channel_id = channel_id
        self.client_state = client_state
        self.tenant_id = tenant_id
        self.command_intake = command_intake

    def ingest(self, notifications: list[ChangeNotification], *, trace_id: str | None) -> IngestSummary:
        summary = IngestSummary()
        for n in notifications:
            if not client_state_matches(n.clientState, self.client_state):
                summary.rejected += 1
                logger.warning("discarding notification with invalid clientState",
                               extra={"subscription_id": n.subscriptionId})
                continue
            if self.tenant_id and n.tenantId and n.tenantId != self.tenant_id:
                summary.rejected += 1
                logger.warning("discarding notification from unexpected tenant")
                continue
            if (n.changeType or "created") != "created":
                summary.ignored += 1
                continue
            res = parse_resource(n.resource)
            if res is None:
                summary.ignored += 1
                logger.warning("unrecognised notification resource", extra={"resource": (n.resource or "")[:200]})
                continue
            if res.team_id != self.team_id or res.channel_id != self.channel_id:
                summary.ignored += 1
                logger.info("ignoring notification for a different team/channel")
                continue
            if res.reply_id:
                # Replies are never new support requests. In the thread of a ticketed request they
                # may be commands (`close ...`); everything else (incl. our own acks) is dropped.
                result = self.command_intake.accept_reply(
                    team_id=res.team_id, channel_id=res.channel_id, parent_message_id=res.message_id,
                    reply_message_id=res.reply_id, trace_id=trace_id,
                ) if self.command_intake is not None else None
                if result is None:
                    summary.ignored += 1
                elif result.created:
                    summary.commands += 1
                    summary.command_ids.append(result.request_id)
                else:
                    summary.duplicates += 1
                    summary.command_ids.append(result.request_id)
                continue
            self._persist_and_enqueue(res.team_id, res.channel_id, res.message_id, trace_id, summary)
        logger.info(
            "notifications ingested",
            extra={"accepted": summary.accepted, "commands": summary.commands, "duplicates": summary.duplicates,
                   "ignored": summary.ignored, "rejected": summary.rejected},
        )
        return summary

    def ingest_messages(self, messages, *, trace_id: str | None) -> IngestSummary:  # noqa: ANN001
        """Catch-up path (lifecycle `missed`): same idempotent insert as the webhook."""
        summary = IngestSummary()
        for m in messages:
            if m.is_reply or m.channel_id != self.channel_id:
                summary.ignored += 1
                continue
            self._persist_and_enqueue(m.team_id, m.channel_id, m.message_id, trace_id, summary)
        return summary

    def catch_up(self, graph: GraphClient, *, top: int = 50) -> IngestSummary:
        messages = graph.list_recent_channel_messages(self.team_id, self.channel_id, top=top)
        summary = self.ingest_messages(messages, trace_id=None)
        logger.info("catch-up sync complete", extra={"accepted": summary.accepted, "duplicates": summary.duplicates})
        return summary

    def _persist_and_enqueue(self, team_id: str, channel_id: str, message_id: str, trace_id: str | None,
                             summary: IngestSummary) -> None:
        with self.db.session() as session:
            result = SupportRepository(session).insert_received(
                message_id=message_id, team_id=team_id, channel_id=channel_id, trace_id=trace_id
            )
        summary.request_ids.append(result.request_id)
        if result.created:
            summary.accepted += 1
            self.dispatcher.enqueue(result.request_id)
        else:
            summary.duplicates += 1
            logger.info("duplicate notification ignored", extra={"request_id": result.request_id})
