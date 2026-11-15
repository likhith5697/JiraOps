"""Pure domain types shared by services. No I/O, no ORM."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.domain.enums import Environment, Priority


@dataclass(frozen=True)
class InboundMessage:
    """A Teams channel message, normalised from the Graph `chatMessage` resource."""

    message_id: str
    team_id: str
    channel_id: str
    body_text: str                      # plain text (HTML already stripped)
    created_at: datetime | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    sender_is_application: bool = False  # posted by a bot / app rather than a user
    reply_to_id: str | None = None
    message_type: str = "message"        # Graph: message | systemEventMessage | ...
    web_url: str | None = None
    deleted: bool = False

    @property
    def is_reply(self) -> bool:
        return bool(self.reply_to_id)


@dataclass(frozen=True)
class ParsedFields:
    """Raw extraction output of a RequestParser: label -> text, before any business validation."""

    fields: dict[str, str]
    parser_name: str
    unknown_content: str = ""
    duplicate_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    message: str


@dataclass(frozen=True)
class SupportRequestDraft:
    """A fully validated support request, ready for ticket creation."""

    api_name: str
    environment: Environment
    issue_summary: str
    issue_description: str
    correlation_id: str
    business_impact: str
    priority: Priority
    optional_fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    draft: SupportRequestDraft | None
    issues: list[ValidationIssue]

    @property
    def is_valid(self) -> bool:
        return self.draft is not None and not self.issues
