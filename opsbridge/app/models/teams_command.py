"""A command sent as a reply in a support-request thread (e.g. `close Fixed after redeploy`).

One row per Teams reply message. `reply_message_id` is the idempotency key: webhook
redelivery of the same reply can never execute the command twice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.database.base import Base, UTCDateTime
from app.domain.enums import CommandOutcome, CommandStatus


class TeamsCommand(Base):
    __tablename__ = "teams_commands"
    __table_args__ = (Index("ix_teams_commands_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- Teams origin ---------------------------------------------------------------------
    reply_message_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    parent_message_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    support_request_id: Mapped[int] = mapped_column(ForeignKey("support_requests.id"), nullable=False, index=True)
    teams_team_id: Mapped[str] = mapped_column(String(128), nullable=False)
    teams_channel_id: Mapped[str] = mapped_column(String(256), nullable=False)
    sender_id: Mapped[str | None] = mapped_column(String(128))
    sender_name: Mapped[str | None] = mapped_column(String(256))
    raw_text: Mapped[str | None] = mapped_column(Text)

    # --- Parsed command + decision ----------------------------------------------------------------
    command: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[CommandOutcome | None] = mapped_column(
        Enum(CommandOutcome, native_enum=False, length=32, validate_strings=True)
    )

    # --- Step progress (write-ahead attempt counters make retries safe) --------------------------
    transition_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transitioned_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    comment_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    commented_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    jira_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())   # escalate: priority + label
    alert_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # escalate: channel alert
    alerted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    alert_message_id: Mapped[str | None] = mapped_column(String(128))
    # Command arguments and data needed to render the reply after a retry, e.g.
    # {"priority": "P1", "from_priority": "P2"} or {"status": "In Progress", "assignee": "..."}.
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reply_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replied_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    response_message_id: Mapped[str | None] = mapped_column(String(128))

    # --- Processing state (same lease/retry model as support_requests) ---------------------------
    status: Mapped[CommandStatus] = mapped_column(
        Enum(CommandStatus, native_enum=False, length=32, validate_strings=True),
        default=CommandStatus.RECEIVED,
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    locked_by: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)
