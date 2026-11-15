"""Admin operations on support requests: query and safe manual retry."""

from __future__ import annotations

import logging

from app.core.time import utcnow
from app.database.session import Database
from app.domain.enums import ProcessingStatus
from app.models.support_request import SupportRequest
from app.models.teams_command import TeamsCommand
from app.models.teams_notification import TeamsNotification
from app.repositories.command_repository import CommandRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.support_repository import SupportRepository
from app.services.ingestion_service import Dispatcher

logger = logging.getLogger("opsbridge.admin")

S = ProcessingStatus


class RetryNotAllowed(Exception):
    pass


def resume_status(rec: SupportRequest) -> ProcessingStatus:
    """Pick the stage a FAILED request should resume from, based on what has been persisted.

    A request that ever attempted Jira creation resumes in JIRA_UNKNOWN so that the worker
    reconciles (searches by idempotency label) before any new create call.
    """
    if rec.jira_issue_key:
        return S.JIRA_CREATED
    if rec.parser_name and rec.validation_errors:
        return S.INVALID
    if rec.jira_create_attempts > 0:
        return S.JIRA_UNKNOWN
    if rec.parser_name:
        return S.VALIDATED
    return S.RECEIVED


class SupportRequestService:
    def __init__(self, *, db: Database, dispatcher: Dispatcher) -> None:
        self.db = db
        self.dispatcher = dispatcher

    def list(self, *, status: ProcessingStatus | None, limit: int, offset: int) -> tuple[list[SupportRequest], int]:
        with self.db.session() as session:
            return SupportRepository(session).list(status=status, limit=limit, offset=offset)

    def get(self, request_id: int) -> SupportRequest | None:
        with self.db.session() as session:
            return SupportRepository(session).get(request_id)

    def commands_for(self, request_id: int) -> list[TeamsCommand]:
        with self.db.session() as session:
            return CommandRepository(session).for_request(request_id)

    def notifications_for(self, request_id: int) -> list[TeamsNotification]:
        with self.db.session() as session:
            return NotificationRepository(session).for_request(request_id)

    def retry(self, request_id: int) -> SupportRequest:
        with self.db.session() as session:
            rec = SupportRepository(session).get(request_id)
            if rec is None:
                raise LookupError(request_id)
            if rec.processing_status in (S.COMPLETED, S.CLOSED, S.REJECTED, S.IGNORED):
                raise RetryNotAllowed(f"request is {rec.processing_status.value}; nothing to retry")
            if rec.locked_until and rec.locked_until > utcnow():
                raise RetryNotAllowed("request is currently being processed")
            previous = rec.processing_status
            if previous == S.FAILED:
                rec.processing_status = resume_status(rec)
            rec.retry_count = 0
            rec.next_attempt_at = utcnow()
            logger.info("manual retry requested", extra={"request_id": request_id, "from": previous.value,
                                                         "resume_at": rec.processing_status.value})
        self.dispatcher.enqueue(request_id)
        with self.db.session() as session:
            return SupportRepository(session).get(request_id)  # type: ignore[return-value]
