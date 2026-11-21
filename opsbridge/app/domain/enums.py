from __future__ import annotations

from enum import StrEnum


class ProcessingStatus(StrEnum):
    RECEIVED = "RECEIVED"          # notification persisted, message not yet fetched/parsed
    VALIDATED = "VALIDATED"        # valid request, Jira issue not yet created
    JIRA_UNKNOWN = "JIRA_UNKNOWN"  # create outcome ambiguous; reconcile before any new create
    JIRA_CREATED = "JIRA_CREATED"  # issue exists, acknowledgement reply pending
    INVALID = "INVALID"            # template validation failed, rejection reply pending
    COMPLETED = "COMPLETED"        # terminal: issue created + acknowledged
    CLOSED = "CLOSED"              # terminal: issue closed from Teams via the `close` command
    REJECTED = "REJECTED"          # terminal: invalid + rejection reply sent
    IGNORED = "IGNORED"            # terminal: reply / bot / self / not in support channel
    FAILED = "FAILED"              # terminal until manual retry: retries exhausted or permanent error

    @classmethod
    def active(cls) -> tuple["ProcessingStatus", ...]:
        return (cls.RECEIVED, cls.VALIDATED, cls.JIRA_UNKNOWN, cls.JIRA_CREATED, cls.INVALID)

    @classmethod
    def terminal(cls) -> tuple["ProcessingStatus", ...]:
        return (cls.COMPLETED, cls.CLOSED, cls.REJECTED, cls.IGNORED, cls.FAILED)


class CommandStatus(StrEnum):
    """Lifecycle of a thread-reply command (e.g. `close`)."""

    RECEIVED = "RECEIVED"    # reply notification persisted; reply not yet fetched/parsed/authorized
    EXECUTING = "EXECUTING"  # authorized; Jira side effects (transition, comment) in progress
    REPLYING = "REPLYING"    # outcome decided; Teams reply pending
    COMPLETED = "COMPLETED"  # terminal: command succeeded (or was a no-op) and user was told
    REJECTED = "REJECTED"    # terminal: not allowed / not possible, and user was told
    IGNORED = "IGNORED"      # terminal: not a command, bot/self message, or unknown thread
    FAILED = "FAILED"        # terminal: retries exhausted or permanent error

    @classmethod
    def active(cls) -> tuple["CommandStatus", ...]:
        return (cls.RECEIVED, cls.EXECUTING, cls.REPLYING)


class CommandOutcome(StrEnum):
    # success outcomes
    CLOSED = "CLOSED"
    ALREADY_CLOSED = "ALREADY_CLOSED"
    REOPENED = "REOPENED"
    STATUS_REPORTED = "STATUS_REPORTED"
    ESCALATED = "ESCALATED"
    ASK_ROUTED = "ASK_ROUTED"                       # `ask` handed to AskService (it replies)
    FEEDBACK_RECORDED = "FEEDBACK_RECORDED"
    FEEDBACK_ALREADY_GIVEN = "FEEDBACK_ALREADY_GIVEN"
    # refusals (user is told why; nothing changed)
    UNAUTHORIZED = "UNAUTHORIZED"
    NO_TRANSITION = "NO_TRANSITION"
    NOT_CLOSED = "NOT_CLOSED"          # reopen on a ticket that is not closed
    NOT_HIGHER = "NOT_HIGHER"          # escalate to a priority that is not higher than the current one
    TICKET_CLOSED = "TICKET_CLOSED"    # escalate on a closed ticket
    INVALID_ARGS = "INVALID_ARGS"      # e.g. `escalate` without a valid priority

    @property
    def is_success(self) -> bool:
        return self in (CommandOutcome.CLOSED, CommandOutcome.ALREADY_CLOSED, CommandOutcome.REOPENED,
                        CommandOutcome.STATUS_REPORTED, CommandOutcome.ESCALATED, CommandOutcome.ASK_ROUTED,
                        CommandOutcome.FEEDBACK_RECORDED, CommandOutcome.FEEDBACK_ALREADY_GIVEN)


class NotificationStatus(StrEnum):
    """Lifecycle of an outbound Teams thread update (e.g. Jira status/assignee change)."""

    PENDING = "PENDING"  # to be posted (possibly after a backoff)
    SENT = "SENT"        # terminal: posted in the thread
    FAILED = "FAILED"    # terminal: retries exhausted or permanent error

    @classmethod
    def active(cls) -> tuple["NotificationStatus", ...]:
        return (cls.PENDING,)


class AskStatus(StrEnum):
    RECEIVED = "RECEIVED"  # question stored; search + answer pending
    REPLYING = "REPLYING"  # answer computed and stored; Teams reply pending
    ANSWERED = "ANSWERED"  # terminal
    FAILED = "FAILED"      # terminal: retries exhausted / permanent error

    @classmethod
    def active(cls) -> tuple["AskStatus", ...]:
        return (cls.RECEIVED, cls.REPLYING)


class Environment(StrEnum):
    PROD = "PROD"
    UAT = "UAT"
    QA = "QA"


class Priority(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class SubscriptionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REMOVED = "REMOVED"
