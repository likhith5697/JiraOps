from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import NotificationStatus
from app.models.teams_notification import TeamsNotification
from app.repositories.leasing import claim_row, due_row_ids, release_row


class NotificationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, notification_id: int) -> TeamsNotification | None:
        return self.session.get(TeamsNotification, notification_id)

    def get_by_dedupe_key(self, dedupe_key: str) -> TeamsNotification | None:
        return self.session.scalar(select(TeamsNotification).where(TeamsNotification.dedupe_key == dedupe_key))

    def for_request(self, support_request_id: int) -> list[TeamsNotification]:
        return list(self.session.scalars(
            select(TeamsNotification)
            .where(TeamsNotification.support_request_id == support_request_id)
            .order_by(TeamsNotification.id)
        ).all())

    def due_ids(self, *, limit: int = 50, now: datetime | None = None) -> list[int]:
        return due_row_ids(self.session, TeamsNotification, status_column=TeamsNotification.status,
                           active=NotificationStatus.active(), limit=limit, now=now)

    def claim(self, notification_id: int, *, worker_id: str, lease_seconds: int) -> bool:
        return claim_row(self.session, TeamsNotification, notification_id, status_column=TeamsNotification.status,
                         active=NotificationStatus.active(), worker_id=worker_id, lease_seconds=lease_seconds)

    def release(self, notification_id: int, *, worker_id: str) -> None:
        release_row(self.session, TeamsNotification, notification_id, worker_id=worker_id)
