from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.domain.enums import AskStatus
from app.models.knowledge import AskQuery
from app.repositories.leasing import claim_row, due_row_ids, release_row
from app.repositories.support_repository import InsertResult


class AskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_received(self, **values: object) -> InsertResult:
        """Insert + commit; a duplicate teams_message_id returns the existing row (idempotency)."""
        row = AskQuery(status=AskStatus.RECEIVED, next_attempt_at=utcnow(), **values)
        try:
            self.session.add(row)
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.get_by_message_id(str(values["teams_message_id"]))
            if existing is None:  # pragma: no cover
                raise
            return InsertResult(existing.id, created=False)
        return InsertResult(row.id, created=True)

    def get(self, ask_id: int) -> AskQuery | None:
        return self.session.get(AskQuery, ask_id)

    def get_by_message_id(self, message_id: str) -> AskQuery | None:
        return self.session.scalar(select(AskQuery).where(AskQuery.teams_message_id == message_id))

    def latest_answered_in_thread(self, thread_root_id: str) -> AskQuery | None:
        return self.session.scalar(select(AskQuery).where(AskQuery.reply_to_message_id == thread_root_id,
                                                          AskQuery.status == AskStatus.ANSWERED)
                                   .order_by(AskQuery.id.desc()))

    def list(self, *, limit: int = 20, offset: int = 0) -> tuple[list[AskQuery], int]:
        total = self.session.scalar(select(func.count()).select_from(AskQuery)) or 0
        items = self.session.scalars(select(AskQuery).order_by(AskQuery.id.desc()).limit(limit).offset(offset)).all()
        return list(items), total

    def all(self) -> list[AskQuery]:
        return list(self.session.scalars(select(AskQuery)).all())

    def due_ids(self, *, limit: int = 50, now: datetime | None = None) -> list[int]:
        return due_row_ids(self.session, AskQuery, status_column=AskQuery.status, active=AskStatus.active(),
                           limit=limit, now=now)

    def claim(self, ask_id: int, *, worker_id: str, lease_seconds: int) -> bool:
        return claim_row(self.session, AskQuery, ask_id, status_column=AskQuery.status, active=AskStatus.active(),
                         worker_id=worker_id, lease_seconds=lease_seconds)

    def release(self, ask_id: int, *, worker_id: str) -> None:
        release_row(self.session, AskQuery, ask_id, worker_id=worker_id)
