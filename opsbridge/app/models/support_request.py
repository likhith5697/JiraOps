from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.database.base import Base, UTCDateTime
from app.domain.enums import ProcessingStatus


class SupportRequest(Base):
    __tablename__ = "support_requests"
    __table_args__ = (
        Index("ix_support_requests_due", "processing_status", "next_attempt_at"),
        # Declared as a named unique index (not column unique=True) so the additive migration
        # can create it on existing databases (SQLite cannot ADD COLUMN ... UNIQUE).
        Index("uq_support_requests_close_command", "close_command_message_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # --- Teams origin (teams_message_id is the idempotency key) -------------------------
    teams_message_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    teams_team_id: Mapped[str] = mapped_column(String(128), nullable=False)
    teams_channel_id: Mapped[str] = mapped_column(String(256), nullable=False)
    teams_message_url: Mapped[str | None] = mapped_column(String(1024))
    teams_message_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sender_id: Mapped[str | None] = mapped_column(String(128))
    sender_name: Mapped[str | None] = mapped_column(String(256))
    raw_message: Mapped[str | None] = mapped_column(Text)

    # --- Extracted request ------------------------------------------------------------
    api_name: Mapped[str | None] = mapped_column(String(128))
    environment: Mapped[str | None] = mapped_column(String(16))
    issue_summary: Mapped[str | None] = mapped_column(String(255))
    issue_description: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(String(128))
    business_impact: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str | None] = mapped_column(String(8))
    optional_fields: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    validation_errors: Mapped[list[dict[str, str]] | None] = mapped_column(JSON)
    parser_name: Mapped[str | None] = mapped_column(String(64))

    # --- Jira ---------------------------------------------------------------------------------
    # Not unique: issue keys are only unique within one Jira site (e.g. a mock OPS-1 and a real
    # OPS-1 can coexist). Idempotency is enforced by teams_message_id + the Jira label search.
    jira_issue_key: Mapped[str | None] = mapped_column(String(64), index=True)
    jira_issue_id: Mapped[str | None] = mapped_column(String(64))
    jira_issue_url: Mapped[str | None] = mapped_column(String(1024))
    jira_idempotency_label: Mapped[str | None] = mapped_column(String(64))
    jira_create_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- Teams acknowledgement ---------------------------------------------------------------
    acknowledgement_message_id: Mapped[str | None] = mapped_column(String(128))
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ack_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- Closed from Teams (`close` thread command) ------------------------------------------
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    closed_by_id: Mapped[str | None] = mapped_column(String(128))
    closed_by_name: Mapped[str | None] = mapped_column(String(256))
    close_reason: Mapped[str | None] = mapped_column(Text)
    close_command_message_id: Mapped[str | None] = mapped_column(String(128))

    # --- Reopened from Teams (`reopen`) - the latest reopen; full history is in teams_commands --
    reopened_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reopened_by_id: Mapped[str | None] = mapped_column(String(128))
    reopened_by_name: Mapped[str | None] = mapped_column(String(256))
    reopen_reason: Mapped[str | None] = mapped_column(Text)

    # --- Escalated from Teams (`escalate P1`) ------------------------------------------------
    escalated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    escalated_by_id: Mapped[str | None] = mapped_column(String(128))
    escalated_by_name: Mapped[str | None] = mapped_column(String(256))
    escalation_reason: Mapped[str | None] = mapped_column(Text)
    escalated_from_priority: Mapped[str | None] = mapped_column(String(8))

    # --- Last Jira state seen (Jira -> Teams sync). Changes vs. this snapshot are posted. -------
    jira_status: Mapped[str | None] = mapped_column(String(128))
    jira_assignee_id: Mapped[str | None] = mapped_column(String(128))
    jira_assignee_name: Mapped[str | None] = mapped_column(String(256))
    jira_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())  # Jira `updated` of that state
    # Set by close/reopen right before their transition: a change to exactly this status is
    # OpsBridge's own (already announced by the command reply) and is not posted again.
    jira_expected_status: Mapped[str | None] = mapped_column(String(128))

    # --- Knowledge capture (after close) --------------------------------------------------------
    knowledge_status: Mapped[str | None] = mapped_column(String(16))  # CAPTURED | SKIPPED | FAILED
    knowledge_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- Processing state ---------------------------------------------------------------------
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        Enum(ProcessingStatus, native_enum=False, length=32, validate_strings=True),
        default=ProcessingStatus.RECEIVED,
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    locked_by: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SupportRequest id={self.id} msg={self.teams_message_id} status={self.processing_status}>"
