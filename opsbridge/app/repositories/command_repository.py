"""Persistence for thread-reply commands: idempotent insert, leases, queries."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.domain.enums import CommandStatus
from app.models.teams_command import TeamsCommand
from app.repositories.leasing import claim_row, due_row_ids, release_row
from app.repositories.support_repository import InsertResult


class CommandRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_received(self, *, reply_message_id: str, parent_message_id: str, support_request_id: int | None,
                        team_id: str, channel_id: str, trace_id: str | None,
                        ask_query_id: int | None = None) -> InsertResult:
        """Insert + commit a RECEIVED command; a duplicate reply id returns the existing row.

        Same contract as SupportRepository.insert_received: first operation on a fresh session,
        INSERT before any read so concurrent redeliveries race only on the unique index.
        """
        row = TeamsCommand(
            reply_message_id=reply_message_id,
            parent_message_id=parent_message_id,
            support_request_id=support_request_id,
            ask_query_id=ask_query_id,
            teams_team_id=team_id,
            teams_channel_id=channel_id,
            status=CommandStatus.RECEIVED,
            trace_id=trace_id,
            next_attempt_at=utcnow(),
        )
        try:
            self.session.add(row)
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.get_by_reply_id(reply_message_id)
            if existing is None:  # pragma: no cover
                raise
            return InsertResult(existing.id, created=False)
        return InsertResult(row.id, created=True)

    def get(self, command_id: int) -> TeamsCommand | None:
        return self.session.get(TeamsCommand, command_id)

    def get_by_reply_id(self, reply_message_id: str) -> TeamsCommand | None:
        return self.session.scalar(select(TeamsCommand).where(TeamsCommand.reply_message_id == reply_message_id))

    def for_request(self, support_request_id: int) -> list[TeamsCommand]:
        return list(
            self.session.scalars(
                select(TeamsCommand)
                .where(TeamsCommand.support_request_id == support_request_id)
                .order_by(TeamsCommand.id)
            ).all()
        )

    def due_ids(self, *, limit: int = 50, now: datetime | None = None) -> list[int]:
        return due_row_ids(self.session, TeamsCommand, status_column=TeamsCommand.status,
                           active=CommandStatus.active(), limit=limit, now=now)

    def claim(self, command_id: int, *, worker_id: str, lease_seconds: int) -> bool:
        return claim_row(self.session, TeamsCommand, command_id, status_column=TeamsCommand.status,
                         active=CommandStatus.active(), worker_id=worker_id, lease_seconds=lease_seconds)

    def release(self, command_id: int, *, worker_id: str) -> None:
        release_row(self.session, TeamsCommand, command_id, worker_id=worker_id)
