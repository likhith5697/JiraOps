"""Persistence for support requests: idempotent insert, atomic lease-based claims, queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.domain.enums import ProcessingStatus
from app.models.support_request import SupportRequest
from app.repositories.leasing import claim_row, due_row_ids, release_row


@dataclass(frozen=True)
class InsertResult:
    request_id: int
    created: bool  # False => duplicate notification for an already-known message


class SupportRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---- ingestion ------------------------------------------------------------------------
    def insert_received(
        self, *, message_id: str, team_id: str, channel_id: str, trace_id: str | None
    ) -> InsertResult:
        """Insert a RECEIVED row and commit; on unique-key collision return the existing row id.

        Must be the first operation on a fresh session (it commits / rolls back itself).
        The INSERT is issued *before* any read so that SQLite takes the write lock up front
        (waiting on busy_timeout) instead of failing a read->write lock upgrade, and so that
        concurrent deliveries of the same message race only on the unique index.
        """
        record = SupportRequest(
            teams_message_id=message_id,
            teams_team_id=team_id,
            teams_channel_id=channel_id,
            processing_status=ProcessingStatus.RECEIVED,
            trace_id=trace_id,
            next_attempt_at=utcnow(),
        )
        try:
            self.session.add(record)
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.get_by_message_id(message_id)
            if existing is None:  # pragma: no cover - would indicate a different constraint failed
                raise
            return InsertResult(existing.id, created=False)
        return InsertResult(record.id, created=True)

    # ---- reads -----------------------------------------------------------------------------
    def get(self, request_id: int) -> SupportRequest | None:
        return self.session.get(SupportRequest, request_id)

    def get_by_message_id(self, message_id: str) -> SupportRequest | None:
        return self.session.scalar(
            select(SupportRequest).where(SupportRequest.teams_message_id == message_id)
        )

    def list(
        self, *, status: ProcessingStatus | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[SupportRequest], int]:
        query = select(SupportRequest)
        count_query = select(func.count()).select_from(SupportRequest)
        if status is not None:
            query = query.where(SupportRequest.processing_status == status)
            count_query = count_query.where(SupportRequest.processing_status == status)
        total = self.session.scalar(count_query) or 0
        items = self.session.scalars(
            query.order_by(SupportRequest.id.desc()).limit(limit).offset(offset)
        ).all()
        return list(items), total

    def due_ids(self, *, limit: int = 50, now: datetime | None = None) -> list[int]:
        return due_row_ids(self.session, SupportRequest, status_column=SupportRequest.processing_status,
                           active=ProcessingStatus.active(), limit=limit, now=now)

    def status_counts(self) -> dict[str, int]:
        rows = self.session.execute(
            select(SupportRequest.processing_status, func.count()).group_by(
                SupportRequest.processing_status
            )
        ).all()
        return {str(status): count for status, count in rows}

    # ---- concurrency control ----------------------------------------------------------------
    def claim(self, request_id: int, *, worker_id: str, lease_seconds: int) -> bool:
        """Atomically take a lease on a due, active row. Exactly one concurrent caller wins."""
        return claim_row(self.session, SupportRequest, request_id, status_column=SupportRequest.processing_status,
                         active=ProcessingStatus.active(), worker_id=worker_id, lease_seconds=lease_seconds)

    def release(self, request_id: int, *, worker_id: str) -> None:
        release_row(self.session, SupportRequest, request_id, worker_id=worker_id)
