from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import CommandOutcome, CommandStatus, NotificationStatus, ProcessingStatus, SubscriptionStatus

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class SupportRequestSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    teams_message_id: str
    sender_name: str | None
    api_name: str | None
    environment: str | None
    priority: str | None
    issue_summary: str | None
    processing_status: ProcessingStatus
    jira_issue_key: str | None
    jira_issue_url: str | None
    retry_count: int
    closed_at: datetime | None = None
    escalated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ValidationErrorItem(BaseModel):
    field: str
    message: str


class TeamsCommandResponse(BaseModel):
    """A thread-reply command (e.g. `close`) and how it was handled."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    reply_message_id: str
    command: str | None
    reason: str | None
    sender_id: str | None
    sender_name: str | None
    status: CommandStatus
    outcome: CommandOutcome | None
    details: dict[str, Any] | None = None
    transitioned_at: datetime | None
    jira_updated_at: datetime | None = None
    commented_at: datetime | None
    alerted_at: datetime | None = None
    replied_at: datetime | None
    retry_count: int
    error_message: str | None
    created_at: datetime


class TeamsNotificationResponse(BaseModel):
    """An update posted (or to be posted) in the request thread, e.g. a Jira status change."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    payload: dict[str, Any]
    status: NotificationStatus
    sent_at: datetime | None
    retry_count: int
    error_message: str | None
    created_at: datetime


class SupportRequestDetail(SupportRequestSummary):
    teams_team_id: str
    teams_channel_id: str
    teams_message_url: str | None
    teams_message_created_at: datetime | None
    sender_id: str | None
    issue_description: str | None
    correlation_id: str | None
    business_impact: str | None
    optional_fields: dict[str, Any] | None
    validation_errors: list[ValidationErrorItem] | None
    parser_name: str | None
    jira_issue_id: str | None
    jira_create_attempts: int
    acknowledgement_message_id: str | None
    acknowledged_at: datetime | None
    ack_attempts: int
    error_message: str | None
    next_attempt_at: datetime
    trace_id: str | None
    closed_by_id: str | None
    closed_by_name: str | None
    close_reason: str | None
    close_command_message_id: str | None
    reopened_at: datetime | None = None
    reopened_by_id: str | None = None
    reopened_by_name: str | None = None
    reopen_reason: str | None = None
    escalated_by_id: str | None = None
    escalated_by_name: str | None = None
    escalation_reason: str | None = None
    escalated_from_priority: str | None = None
    jira_status: str | None = None
    jira_assignee_name: str | None = None
    jira_updated_at: datetime | None = None
    notifications: list[TeamsNotificationResponse] = Field(default_factory=list)
    commands: list[TeamsCommandResponse] = Field(default_factory=list)


class SubscriptionCreateRequest(BaseModel):
    force: bool = Field(default=False, description="Create a new subscription even if an active one exists")


class SubscriptionRenewRequest(BaseModel):
    subscription_id: str | None = Field(default=None, max_length=128,
                                        description="Renew one subscription; omit to renew all active ones")


class SubscriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    subscription_id: str
    resource: str
    change_type: str
    notification_url: str
    lifecycle_notification_url: str | None
    expiration_at: datetime
    status: SubscriptionStatus
    last_renewed_at: datetime | None
    last_error: str | None
    created_at: datetime


class SubscriptionActionResponse(BaseModel):
    action: str
    subscription: SubscriptionResponse


class ErrorResponse(BaseModel):
    detail: str
