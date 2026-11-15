"""Outbound Teams thread update (outbox row), e.g. "OPS-1 status: To Do -> In Progress".

`dedupe_key` is unique: the same Jira change observed twice (webhook + poll, or a redelivered
webhook) can only ever create one notification.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.database.base import Base, UTCDateTime
from app.domain.enums import NotificationStatus


class TeamsNotification(Base):
    __tablename__ = "teams_notifications"
    __table_args__ = (Index("ix_teams_notifications_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    support_request_id: Mapped[int] = mapped_column(ForeignKey("support_requests.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="jira_update")
    dedupe_key: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    # e.g. {"jira_key": "OPS-1", "old_status": "To Do", "new_status": "In Progress",
    #       "old_assignee": null, "new_assignee": "Jane", "actor": "Jane", "source": "webhook"}
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, native_enum=False, length=16, validate_strings=True),
        default=NotificationStatus.PENDING,
        nullable=False,
    )
    reply_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    response_message_id: Mapped[str | None] = mapped_column(String(128))
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    locked_by: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)
