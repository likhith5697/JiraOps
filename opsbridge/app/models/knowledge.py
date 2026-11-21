"""Knowledge captured from resolved tickets, and the `ask` questions answered from it."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.database.base import Base, UTCDateTime
from app.domain.enums import AskStatus


class KnowledgeEntry(Base):
    """One resolved ticket, distilled: what the problem was and what fixed it (redacted)."""

    __tablename__ = "knowledge_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    support_request_id: Mapped[int] = mapped_column(ForeignKey("support_requests.id"), unique=True, nullable=False)
    jira_issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    jira_issue_url: Mapped[str | None] = mapped_column(String(1024))
    api_name: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    priority: Mapped[str | None] = mapped_column(String(8))
    problem_text: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # close_reason | jira_comment | llm_summary
    prompt_version: Mapped[str | None] = mapped_column(String(64))   # set when source == llm_summary
    embedding: Mapped[str | None] = mapped_column(Text)               # JSON list[float]
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    helpful_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    not_helpful_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)


class AskQuery(Base):
    """One `ask` question and the answer OpsBridge gave (also the background work unit)."""

    __tablename__ = "ask_queries"
    __table_args__ = (Index("ix_ask_queries_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    teams_message_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)  # idempotency
    reply_to_message_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)  # thread root
    teams_team_id: Mapped[str] = mapped_column(String(128), nullable=False)
    teams_channel_id: Mapped[str] = mapped_column(String(256), nullable=False)
    sender_id: Mapped[str | None] = mapped_column(String(128), index=True)
    sender_name: Mapped[str | None] = mapped_column(String(256))
    question: Mapped[str] = mapped_column(Text, nullable=False)  # redacted
    api_hint: Mapped[str | None] = mapped_column(String(128))
    environment_hint: Mapped[str | None] = mapped_column(String(16))

    retrieved_keys: Mapped[list[str] | None] = mapped_column(JSON)
    retrieved_entry_ids: Mapped[list[int] | None] = mapped_column(JSON)
    cited_entry_ids: Mapped[list[int] | None] = mapped_column(JSON)
    top_score: Mapped[float | None] = mapped_column(Float)
    answer_text: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str | None] = mapped_column(String(16))           # llm | keyword | none
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    feedback: Mapped[str | None] = mapped_column(String(16))       # helpful | not_helpful (from the asker)

    status: Mapped[AskStatus] = mapped_column(Enum(AskStatus, native_enum=False, length=16, validate_strings=True),
                                              default=AskStatus.RECEIVED, nullable=False)
    reply_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    response_message_id: Mapped[str | None] = mapped_column(String(128))
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    locked_by: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False)


class AskFeedback(Base):
    """`helpful` / `not helpful` on an answer: once per user per question; reply id dedupes redelivery."""

    __tablename__ = "ask_feedback"
    __table_args__ = (UniqueConstraint("ask_query_id", "sender_id", name="uq_ask_feedback_once_per_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ask_query_id: Mapped[int] = mapped_column(ForeignKey("ask_queries.id"), nullable=False, index=True)
    sender_id: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(String(16), nullable=False)  # helpful | not_helpful
    reply_message_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
