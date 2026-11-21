"""Thread-reply commands: intake, authorization, execution.

Commands (see command_parser.py for the grammar):

    close [reason]            requester/admin   transition to JIRA_CLOSE_STATUS, comment, reply
    reopen [reason]           requester/admin   transition a closed issue to JIRA_REOPEN_STATUS, comment, reply
    status                    anyone            reply with status, assignee, priority, last update
    escalate P1|P2|P3 [why]   requester/admin   raise priority + label, comment, alert channel, reply

State machine per TeamsCommand row (one row per Teams reply message):

    RECEIVED  --fetch reply, parse, authorize-->  EXECUTING | REPLYING(refusal) | IGNORED
    EXECUTING --run executor (Jira / Teams side effects)--> REPLYING(outcome)
    REPLYING  --post Teams reply-->  COMPLETED (success outcomes) | REJECTED (refusals)
    any active stage --retries exhausted / permanent error--> FAILED

Idempotency:
* Webhook redelivery: unique `reply_message_id` -> the command row is created once.
* Every non-idempotent side effect has a write-ahead attempt counter; after an ambiguous
  timeout the retry first checks whether the effect already happened:
    transition -> re-read the issue status        comment -> search the issue's comments
    alert post -> search the escalation channel   reply   -> search the thread for the marker
* `update_issue` (priority + label) is an idempotent PUT and is simply retried.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Any, Callable

from app.core.errors import ConfigurationError, IntegrationError, NotFoundError, PermanentError
from app.core.logging import log_context
from app.core.retry import exponential_backoff
from app.core.time import utcnow
from app.database.session import Database
from app.domain.enums import CommandOutcome, CommandStatus, ProcessingStatus
from app.domain.messages import InboundMessage
from app.integrations.jira_client import JiraClient
from app.integrations.microsoft_graph import GraphClient
from app.models.support_request import SupportRequest
from app.models.teams_command import TeamsCommand
from app.repositories.command_repository import CommandRepository
from app.repositories.support_repository import InsertResult, SupportRepository
from app.services.command_parser import CommandParser
from app.services.notification_service import (
    NotificationService,
    ReplyTarget,
    command_marker,
    escalation_alert_marker,
    render_close_already,
    render_close_success,
    render_escalated,
    render_escalation_alert,
    render_feedback_ack,
    render_invalid_args,
    render_no_transition,
    render_not_closed,
    render_not_higher,
    render_reopen_success,
    render_status,
    render_ticket_closed,
    render_unauthorized,
)

logger = logging.getLogger("opsbridge.commands")

C = CommandStatus
O = CommandOutcome


@dataclass(frozen=True)
class CommandConfig:
    service_account_user_id: str | None
    admin_user_ids: frozenset[str]
    close_status: str = "Done"
    reopen_status: str = "To Do"
    escalation_label: str = "escalated"
    priority_map: dict[str, str] = field(default_factory=dict)
    escalation_team_id: str = ""
    escalation_channel_id: str = ""  # empty = no channel alert
    worker_lease_seconds: int = 120
    max_retries: int = 5
    retry_base_delay_seconds: float = 5.0
    retry_max_delay_seconds: float = 900.0


@dataclass(frozen=True)
class CommandSpec:
    executor: Callable[[TeamsCommand, SupportRequest, str], None]
    privileged: bool  # True: requester or admin only
    verb: str         # used in messages: "You're not allowed to {verb} OPS-1"
    # Where the command may be used: "ticket" = thread of a request with a Jira issue,
    # "ask" = thread of an `ask` answer.
    contexts: frozenset[str] = frozenset({"ticket"})


class LeaseLost(Exception):
    pass


def close_comment_text(sender_name: str, reason: str | None) -> str:
    return f"Closed from Microsoft Teams by {sender_name}. Reason: {reason or 'Not provided'}"


def reopen_comment_text(sender_name: str, reason: str | None) -> str:
    return f"Reopened from Microsoft Teams by {sender_name}. Reason: {reason or 'Not provided'}"


def escalate_comment_text(sender_name: str, from_priority: str, to_priority: str, reason: str | None) -> str:
    return (f"Escalated from {from_priority} to {to_priority} from Microsoft Teams by {sender_name}. "
            f"Reason: {reason or 'Not provided'}")


def _adf_paragraph(text: str) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _same_status(a: str | None, b: str | None) -> bool:
    return (a or "").casefold() == (b or "").casefold()


def _priority_rank(priority: str | None) -> int:
    """P1 -> 1 (highest). Unknown/missing priority ranks below P3."""
    try:
        return int((priority or "")[1:])
    except ValueError:
        return 99


# =============================================================================================
# Intake: turn a reply into a command row (from the webhook, or from the message processor)
# =============================================================================================
class CommandIntake:
    def __init__(self, *, db: Database, dispatcher: Any = None) -> None:
        self.db = db
        self.dispatcher = dispatcher

    def accept_reply(self, *, team_id: str, channel_id: str, parent_message_id: str, reply_message_id: str,
                     trace_id: str | None) -> InsertResult | None:
        """Persist a reply as a command candidate if it is in the thread of a ticketed request or of
        an `ask` question.

        Returns None (ignored, nothing stored) for replies in unknown threads or in threads whose
        request has no Jira issue yet: those can never be commands.
        """
        from app.repositories.ask_repository import AskRepository

        with self.db.session() as session:
            parent = SupportRepository(session).get_by_message_id(parent_message_id)
            parent_id = parent.id if parent and parent.jira_issue_key else None
            ask = None if parent_id else AskRepository(session).get_by_message_id(parent_message_id)
            ask_id = ask.id if ask else None
        if parent_id is None and ask_id is None:
            return None
        with self.db.session() as session:
            result = CommandRepository(session).insert_received(
                reply_message_id=reply_message_id, parent_message_id=parent_message_id,
                support_request_id=parent_id, ask_query_id=ask_id, team_id=team_id, channel_id=channel_id,
                trace_id=trace_id,
            )
        if result.created and self.dispatcher is not None:
            self.dispatcher.enqueue_command(result.request_id)
        return result

    def accept_message(self, message: InboundMessage, trace_id: str | None = None) -> InsertResult | None:
        """Route a fetched reply (discovered by the message processor) into command handling."""
        if not message.reply_to_id:
            return None
        return self.accept_reply(team_id=message.team_id, channel_id=message.channel_id,
                                 parent_message_id=message.reply_to_id, reply_message_id=message.message_id,
                                 trace_id=trace_id)


# =============================================================================================
# Handler: authorization + execution
# =============================================================================================
class CommandHandler:
    def __init__(self, *, db: Database, graph: GraphClient, jira: JiraClient, notifier: NotificationService,
                 parser: CommandParser, config: CommandConfig, worker_id: str = "worker") -> None:
        self.db = db
        self.graph = graph
        self.jira = jira
        self.notifier = notifier
        self.parser = parser
        self.config = config
        self.worker_id = worker_id
        # Set by the container: called with the request id after a successful close.
        self.on_closed: Callable[[int], None] | None = None
        # Set by the container: AskService (for `ask` and feedback commands).
        self.ask_service: Any = None
        # Command registry. A new command = one parser entry + one CommandSpec here.
        self._commands: dict[str, CommandSpec] = {
            "close": CommandSpec(self._execute_close, privileged=True, verb="close"),
            "reopen": CommandSpec(self._execute_reopen, privileged=True, verb="reopen"),
            "status": CommandSpec(self._execute_status, privileged=False, verb="view"),
            "escalate": CommandSpec(self._execute_escalate, privileged=True, verb="escalate"),
            "ask": CommandSpec(self._execute_ask, privileged=False, verb="ask",
                               contexts=frozenset({"ticket", "ask"})),
            "helpful": CommandSpec(self._execute_feedback, privileged=False, verb="rate",
                                   contexts=frozenset({"ticket", "ask"})),
            "not_helpful": CommandSpec(self._execute_feedback, privileged=False, verb="rate",
                                       contexts=frozenset({"ticket", "ask"})),
        }

    # ---- entry point -----------------------------------------------------------------------------
    def process(self, command_id: int, *, worker_id: str | None = None) -> CommandStatus | None:
        worker = worker_id or self.worker_id
        with self.db.session() as session:
            repo = CommandRepository(session)
            if not repo.claim(command_id, worker_id=worker, lease_seconds=self.config.worker_lease_seconds):
                return None
            row = repo.get(command_id)
            trace_id, request_id = row.trace_id, row.support_request_id  # type: ignore[union-attr]
        with log_context(trace_id=trace_id, support_request_id=request_id):
            try:
                return self._run(command_id, worker)
            except LeaseLost:
                logger.warning("command lease lost; another worker owns it now", extra={"command_id": command_id})
                return None
            finally:
                with self.db.session() as session:
                    CommandRepository(session).release(command_id, worker_id=worker)

    def _run(self, command_id: int, worker: str) -> CommandStatus:
        stages = {C.RECEIVED: self._stage_interpret, C.EXECUTING: self._stage_execute, C.REPLYING: self._stage_reply}
        for _ in range(5):
            cmd = self._snapshot(command_id)
            if cmd.status not in stages or cmd.next_attempt_at > utcnow():
                return cmd.status
            stage = cmd.status
            try:
                stages[stage](cmd, worker)
            except LeaseLost:
                raise
            except (IntegrationError, ConfigurationError) as exc:
                self._handle_failure(command_id, worker, stage, exc)
            except Exception as exc:  # unexpected bug: keep the command, retry with backoff
                logger.exception("unexpected error in command stage %s", stage)
                self._handle_failure(command_id, worker, stage, exc)
        return self._snapshot(command_id).status

    # ---- stage 1: fetch, parse, authorize ---------------------------------------------------------
    def _stage_interpret(self, cmd: TeamsCommand, worker: str) -> None:
        try:
            reply = self.graph.get_reply(cmd.teams_team_id, cmd.teams_channel_id, cmd.parent_message_id,
                                         cmd.reply_message_id)
        except NotFoundError:
            self._update(cmd.id, worker, status=C.IGNORED, error_message="Reply no longer exists in Teams")
            return

        info: dict[str, Any] = {"sender_id": reply.sender_id, "sender_name": reply.sender_name,
                                "raw_text": reply.body_text[:5000]}
        reason = self._ignore_reason(reply)
        parsed = None if reason else self.parser.parse(reply.body_text)
        spec = self._commands.get(parsed.name) if parsed else None
        if reason or parsed is None or spec is None:
            self._update(cmd.id, worker, status=C.IGNORED, error_message=reason or "Not a command", **info)
            return

        context = "ticket" if cmd.support_request_id else "ask"
        if context not in spec.contexts:
            self._update(cmd.id, worker, status=C.IGNORED,
                         error_message=f"'{parsed.name}' is not applicable in this thread", **info)
            return
        request = self._request(cmd.support_request_id) if cmd.support_request_id else None
        if request is not None and not request.jira_issue_key:  # defensive: intake already checks this
            self._update(cmd.id, worker, status=C.IGNORED, error_message="Request has no Jira issue", **info)
            return

        info.update(command=parsed.name, reason=parsed.reason, details=dict(parsed.args) or None, retry_count=0)
        if spec.privileged and (request is None or not self._is_authorized(reply.sender_id, request)):
            logger.warning("unauthorized command", extra={"command": parsed.name})
            self._update(cmd.id, worker, status=C.REPLYING, outcome=O.UNAUTHORIZED, **info)
            return
        if parsed.error:
            info["details"] = {**(info["details"] or {}), "error": parsed.error}
            self._update(cmd.id, worker, status=C.REPLYING, outcome=O.INVALID_ARGS, **info)
            return
        logger.info("command accepted", extra={"command": parsed.name,
                                               "jira_key": request.jira_issue_key if request else None})
        self._update(cmd.id, worker, status=C.EXECUTING, **info)

    def _ignore_reason(self, m: InboundMessage) -> str | None:
        if m.deleted:
            return "Reply was deleted"
        if m.message_type != "message":
            return f"Unsupported message type '{m.message_type}'"
        if m.sender_is_application:
            return "Reply was posted by a bot or application"
        if self.config.service_account_user_id and m.sender_id == self.config.service_account_user_id:
            return "Reply was posted by OpsBridge itself"
        return None

    def _is_authorized(self, sender_id: str | None, request: SupportRequest) -> bool:
        if not sender_id:
            return False
        return sender_id == request.sender_id or sender_id in self.config.admin_user_ids

    # ---- stage 2: execute -------------------------------------------------------------------------
    def _stage_execute(self, cmd: TeamsCommand, worker: str) -> None:
        spec = self._commands.get(cmd.command or "")
        if spec is None:
            raise PermanentError(f"no executor registered for command '{cmd.command}'", service="opsbridge")
        spec.executor(cmd, self._request(cmd.support_request_id) if cmd.support_request_id else None, worker)

    # -- ask (thread reply) ---------------------------------------------------------------------------------
    def _execute_ask(self, cmd: TeamsCommand, request: SupportRequest | None, worker: str) -> None:
        """Hand the question to AskService, which answers in this thread. Never touches Jira."""
        if self.ask_service is None:
            raise ConfigurationError("ask service is not configured")
        self.ask_service.accept(
            teams_message_id=cmd.reply_message_id, reply_to_message_id=cmd.parent_message_id,
            team_id=cmd.teams_team_id, channel_id=cmd.teams_channel_id, sender_id=cmd.sender_id,
            sender_name=cmd.sender_name, question=cmd.reason, trace_id=cmd.trace_id,
        )
        # The answer is the reply; this command row needs none of its own.
        self._update(cmd.id, worker, status=C.COMPLETED, outcome=O.ASK_ROUTED, error_message=None, retry_count=0)

    # -- helpful / not helpful ----------------------------------------------------------------------------
    def _execute_feedback(self, cmd: TeamsCommand, request: SupportRequest | None, worker: str) -> None:
        if self.ask_service is None:
            raise ConfigurationError("ask service is not configured")
        ask_id = cmd.ask_query_id
        if ask_id is None:  # feedback in a ticket thread: the latest answer given in that thread
            from app.repositories.ask_repository import AskRepository

            with self.db.session() as session:
                latest = AskRepository(session).latest_answered_in_thread(cmd.parent_message_id)
                ask_id = latest.id if latest else None
        if ask_id is None:
            self._update(cmd.id, worker, status=C.IGNORED, error_message="No answer to rate in this thread")
            return
        result = self.ask_service.record_feedback(
            ask_id, sender_id=cmd.sender_id or "", value=cmd.command or "", reply_message_id=cmd.reply_message_id)
        outcome = O.FEEDBACK_RECORDED if result == "recorded" else O.FEEDBACK_ALREADY_GIVEN
        self._update(cmd.id, worker, status=C.REPLYING, outcome=outcome, retry_count=0,
                     details=self._merge(cmd, ask_query_id=ask_id))

    # -- close ------------------------------------------------------------------------------------------
    def _execute_close(self, cmd: TeamsCommand, request: SupportRequest, worker: str) -> None:
        key = request.jira_issue_key or ""
        target = self.config.close_status
        done = self._transition_step(
            cmd, key, worker, target=target,
            refuse=lambda current: (O.ALREADY_CLOSED, {}) if _same_status(current, target) else None,
        )
        if not done:
            return
        cmd = self._snapshot(cmd.id)
        self._comment_step(cmd, key, worker, close_comment_text(cmd.sender_name or "Unknown user", cmd.reason))
        self._assert_lease(cmd.id, worker)
        with self.db.session() as session:
            req = session.get(SupportRequest, cmd.support_request_id)
            assert req is not None
            req.closed_at = utcnow()
            req.closed_by_id = cmd.sender_id
            req.closed_by_name = cmd.sender_name
            req.close_reason = cmd.reason
            req.close_command_message_id = cmd.reply_message_id
            # If the acknowledgement is still pending, the processor marks CLOSED once it is sent.
            if req.processing_status not in ProcessingStatus.active():
                req.processing_status = ProcessingStatus.CLOSED
        logger.info("support request marked closed", extra={"request_id": cmd.support_request_id})
        self._update(cmd.id, worker, status=C.REPLYING, outcome=O.CLOSED, error_message=None, retry_count=0)
        if self.on_closed is not None:  # knowledge capture (background; never blocks the close)
            self.on_closed(cmd.support_request_id)

    # -- reopen -----------------------------------------------------------------------------------------
    def _execute_reopen(self, cmd: TeamsCommand, request: SupportRequest, worker: str) -> None:
        key = request.jira_issue_key or ""
        close_status = self.config.close_status
        done = self._transition_step(
            cmd, key, worker, target=self.config.reopen_status,
            refuse=lambda current: None if _same_status(current, close_status)
            else (O.NOT_CLOSED, {"current_status": current}),
        )
        if not done:
            return
        cmd = self._snapshot(cmd.id)
        self._comment_step(cmd, key, worker, reopen_comment_text(cmd.sender_name or "Unknown user", cmd.reason))
        self._assert_lease(cmd.id, worker)
        with self.db.session() as session:
            req = session.get(SupportRequest, cmd.support_request_id)
            assert req is not None
            req.reopened_at = utcnow()
            req.reopened_by_id = cmd.sender_id
            req.reopened_by_name = cmd.sender_name
            req.reopen_reason = cmd.reason
            req.closed_at = req.closed_by_id = req.closed_by_name = req.close_reason = None
            req.close_command_message_id = None
            if req.processing_status == ProcessingStatus.CLOSED:
                req.processing_status = ProcessingStatus.COMPLETED
        logger.info("support request reopened", extra={"request_id": cmd.support_request_id})
        self._update(cmd.id, worker, status=C.REPLYING, outcome=O.REOPENED, error_message=None, retry_count=0)

    # -- status -----------------------------------------------------------------------------------------
    def _execute_status(self, cmd: TeamsCommand, request: SupportRequest, worker: str) -> None:
        info = self.jira.get_issue_details(request.jira_issue_key or "")
        updated = info.updated.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if info.updated else None
        self._update(cmd.id, worker, status=C.REPLYING, outcome=O.STATUS_REPORTED, error_message=None, retry_count=0,
                     details=self._merge(cmd, status=info.status, assignee=info.assignee, priority=info.priority,
                                         updated=updated))

    # -- escalate ---------------------------------------------------------------------------------------
    def _execute_escalate(self, cmd: TeamsCommand, request: SupportRequest, worker: str) -> None:
        key = request.jira_issue_key or ""
        details = dict(cmd.details or {})
        to_priority = details["priority"]

        if "from_priority" not in details:  # nothing done yet: check preconditions once
            if request.processing_status == ProcessingStatus.CLOSED:
                self._update(cmd.id, worker, status=C.REPLYING, outcome=O.TICKET_CLOSED, retry_count=0)
                return
            current = request.priority or "unknown"
            if _priority_rank(to_priority) >= _priority_rank(request.priority):
                self._update(cmd.id, worker, status=C.REPLYING, outcome=O.NOT_HIGHER, retry_count=0,
                             details=self._merge(cmd, current_priority=current))
                return
            self._update(cmd.id, worker, details=self._merge(cmd, from_priority=current))
            cmd = self._snapshot(cmd.id)

        # 1. Jira: raise priority + add the escalation label (idempotent PUT)
        if cmd.jira_updated_at is None:
            labels = {"labels": [{"add": self.config.escalation_label}]}
            jira_priority = self.config.priority_map.get(to_priority)
            priority_set = bool(jira_priority)
            try:
                self.jira.update_issue(key, fields={"priority": {"name": jira_priority}} if jira_priority else None,
                                       update=labels)
            except PermanentError as exc:
                field_errors = exc.details.get("field_errors") or {}
                if not (jira_priority and exc.status_code == 400 and "priority" in field_errors):
                    raise
                logger.warning("Jira rejected the priority change; labelling only", extra={"jira_key": key})
                self.jira.update_issue(key, update=labels)
                priority_set = False
            self._update(cmd.id, worker, jira_updated_at=utcnow(), details=self._merge(cmd, priority_set=priority_set))
            cmd = self._snapshot(cmd.id)

        # 2. Jira comment
        from_priority = (cmd.details or {})["from_priority"]
        self._comment_step(cmd, key, worker,
                           escalate_comment_text(cmd.sender_name or "Unknown user", from_priority, to_priority, cmd.reason))
        cmd = self._snapshot(cmd.id)

        # 3. Alert in the escalation channel (optional)
        if self.config.escalation_channel_id and cmd.alerted_at is None:
            team, channel = self.config.escalation_team_id, self.config.escalation_channel_id
            existing = (self.graph.find_channel_message_containing(team, channel, escalation_alert_marker(cmd.id))
                        if cmd.alert_attempts > 0 else None)
            if existing is None:
                self._update(cmd.id, worker, alert_attempts=cmd.alert_attempts + 1)  # write-ahead
                existing = self.graph.post_channel_message(team, channel, render_escalation_alert(
                    command_id=cmd.id, jira_key=key, jira_url=request.jira_issue_url or "",
                    summary=f"[{request.environment}] {request.api_name}: {request.issue_summary}",
                    from_priority=from_priority, to_priority=to_priority,
                    sender_name=cmd.sender_name or "Unknown user", reason=cmd.reason,
                    teams_message_url=request.teams_message_url,
                ))
                logger.info("posted escalation alert", extra={"jira_key": key})
            self._update(cmd.id, worker, alerted_at=utcnow(), alert_message_id=existing)

        # 4. Persist on the support request
        self._assert_lease(cmd.id, worker)
        with self.db.session() as session:
            req = session.get(SupportRequest, cmd.support_request_id)
            assert req is not None
            req.escalated_from_priority = from_priority
            req.priority = to_priority
            req.escalated_at = utcnow()
            req.escalated_by_id = cmd.sender_id
            req.escalated_by_name = cmd.sender_name
            req.escalation_reason = cmd.reason
        logger.info("support request escalated", extra={"request_id": cmd.support_request_id, "to": to_priority})
        self._update(cmd.id, worker, status=C.REPLYING, outcome=O.ESCALATED, error_message=None, retry_count=0)

    # -- shared steps ------------------------------------------------------------------------------------
    def _transition_step(self, cmd: TeamsCommand, key: str, worker: str, *, target: str,
                         refuse: Callable[[str], tuple[CommandOutcome, dict[str, Any]] | None]) -> bool:
        """Move the issue to `target` via a workflow transition (never a hard-coded id).

        Returns True when the issue is now in `target` because of this command. Returns False when
        the command ended in a refusal outcome (already recorded on the row; nothing changed in Jira).
        """
        if cmd.transitioned_at is not None:
            return True
        current = self.jira.get_issue_status(key)
        if cmd.transition_attempts > 0 and _same_status(current, target):
            self._update(cmd.id, worker, transitioned_at=utcnow())  # our ambiguous attempt went through
            return True
        refusal = refuse(current)
        if refusal is not None:
            outcome, extra = refusal
            logger.info("command refused", extra={"jira_key": key, "outcome": outcome.value, "status": current})
            self._update(cmd.id, worker, status=C.REPLYING, outcome=outcome, retry_count=0,
                         details=self._merge(cmd, **extra) if extra else cmd.details)
            return False
        transition = next(
            (t for t in self.jira.get_transitions(key) if _same_status(t.to_status, target)), None
        )
        if transition is None:
            logger.warning("Jira workflow has no transition to the target status",
                           extra={"jira_key": key, "target_status": target, "current_status": current})
            self._update(cmd.id, worker, status=C.REPLYING, outcome=O.NO_TRANSITION, retry_count=0,
                         details=self._merge(cmd, target_status=target, current_status=current))
            return False
        self._set_expected_status(cmd, worker, target)  # Jira->Teams sync: don't re-announce our own change
        self._update(cmd.id, worker, transition_attempts=cmd.transition_attempts + 1)  # write-ahead
        self.jira.transition_issue(key, transition.id)
        logger.info("transitioned Jira issue", extra={"jira_key": key, "transition": transition.name})
        self._update(cmd.id, worker, transitioned_at=utcnow())
        return True

    def _comment_step(self, cmd: TeamsCommand, key: str, worker: str, text: str) -> None:
        if cmd.commented_at is not None:
            return
        existing = self.jira.find_comment_containing(key, text) if cmd.comment_attempts > 0 else None
        if existing is None:
            self._update(cmd.id, worker, comment_attempts=cmd.comment_attempts + 1)  # write-ahead
            self.jira.add_comment(key, _adf_paragraph(text))
        self._update(cmd.id, worker, commented_at=utcnow())

    # ---- stage 3: reply in the Teams thread ------------------------------------------------------------
    def _stage_reply(self, cmd: TeamsCommand, worker: str) -> None:
        request = self._request(cmd.support_request_id) if cmd.support_request_id else None
        target = ReplyTarget(request.id if request else 0, cmd.teams_team_id, cmd.teams_channel_id,
                             cmd.parent_message_id)
        marker = command_marker(cmd.id)
        done = C.COMPLETED if cmd.outcome and cmd.outcome.is_success else C.REJECTED

        if cmd.reply_attempts > 0:
            existing = self.notifier.already_replied(target, marker)
            if existing:
                self._update(cmd.id, worker, status=done, replied_at=utcnow(), response_message_id=existing,
                             error_message=None)
                return

        self._update(cmd.id, worker, reply_attempts=cmd.reply_attempts + 1)  # write-ahead
        reply_id = self.notifier.send(target, self._render(cmd, request))
        self._update(cmd.id, worker, status=done, replied_at=utcnow(), response_message_id=reply_id,
                     error_message=None, retry_count=0)

    def _render(self, cmd: TeamsCommand, request: SupportRequest | None) -> str:
        key = (request.jira_issue_key or "") if request else ""
        url = (request.jira_issue_url or "") if request else ""
        d = cmd.details or {}
        sender = cmd.sender_name or "Unknown user"
        verb = self._commands[cmd.command].verb if cmd.command in self._commands else "change"
        outcome = cmd.outcome
        if outcome in (O.FEEDBACK_RECORDED, O.FEEDBACK_ALREADY_GIVEN):
            return render_feedback_ack(command_id=cmd.id, recorded=outcome == O.FEEDBACK_RECORDED,
                                       helpful=cmd.command == "helpful")
        if outcome == O.UNAUTHORIZED and request is None:
            return render_invalid_args(command_id=cmd.id, message=f"`{cmd.command}` only works in a ticket's thread.")
        if outcome == O.CLOSED:
            return render_close_success(command_id=cmd.id, jira_key=key, jira_url=url, sender_name=sender,
                                        reason=cmd.reason)
        if outcome == O.ALREADY_CLOSED:
            return render_close_already(command_id=cmd.id, jira_key=key)
        if outcome == O.REOPENED:
            return render_reopen_success(command_id=cmd.id, jira_key=key, jira_url=url, sender_name=sender,
                                         reason=cmd.reason)
        if outcome == O.NOT_CLOSED:
            return render_not_closed(command_id=cmd.id, jira_key=key, current_status=d.get("current_status", ""))
        if outcome == O.STATUS_REPORTED:
            return render_status(command_id=cmd.id, jira_key=key, jira_url=url, status=d.get("status", ""),
                                 assignee=d.get("assignee"), priority=d.get("priority"), updated=d.get("updated"))
        if outcome == O.ESCALATED:
            return render_escalated(command_id=cmd.id, jira_key=key, jira_url=url, sender_name=sender,
                                    from_priority=d.get("from_priority", "?"), to_priority=d.get("priority", "?"),
                                    reason=cmd.reason, alerted=cmd.alerted_at is not None,
                                    jira_priority_set=d.get("priority_set", True), label=self.config.escalation_label)
        if outcome == O.NOT_HIGHER:
            return render_not_higher(command_id=cmd.id, jira_key=key, current=d.get("current_priority", "?"),
                                     requested=d.get("priority", "?"))
        if outcome == O.TICKET_CLOSED:
            return render_ticket_closed(command_id=cmd.id, jira_key=key)
        if outcome == O.INVALID_ARGS:
            return render_invalid_args(command_id=cmd.id, message=d.get("error", "Invalid command."))
        if outcome == O.UNAUTHORIZED:
            return render_unauthorized(command_id=cmd.id, jira_key=key, verb=verb)
        return render_no_transition(command_id=cmd.id, jira_key=key, verb=verb,
                                    target_status=d.get("target_status", ""))

    # ---- failure handling (same policy as MessageProcessor) ----------------------------------------
    def _handle_failure(self, command_id: int, worker: str, stage: CommandStatus, exc: Exception) -> None:
        cmd = self._snapshot(command_id)
        message = f"[{stage.value}] {type(exc).__name__}: {exc}"[:2000]
        if isinstance(exc, (PermanentError, ConfigurationError)):
            logger.error("command failed permanently", extra={"error": message})
            self._update(command_id, worker, status=C.FAILED, error_message=message)
            self._clear_expected_status(cmd)
            return
        retry_count = cmd.retry_count + 1
        if retry_count > self.config.max_retries:
            logger.error("command retries exhausted", extra={"error": message, "retry_count": retry_count})
            self._update(command_id, worker, status=C.FAILED, error_message=message, retry_count=retry_count)
            self._clear_expected_status(cmd)
            return
        delay = exponential_backoff(retry_count - 1, base=self.config.retry_base_delay_seconds,
                                    cap=self.config.retry_max_delay_seconds)
        if isinstance(exc, IntegrationError) and exc.retry_after:
            delay = max(delay, exc.retry_after)
        logger.warning("command stage failed; retry scheduled",
                       extra={"error": message, "retry_count": retry_count, "delay_s": round(delay, 1)})
        self._update(command_id, worker, error_message=message, retry_count=retry_count,
                     next_attempt_at=utcnow() + timedelta(seconds=delay))

    # ---- persistence helpers ---------------------------------------------------------------------------
    def _set_expected_status(self, cmd: TeamsCommand, worker: str, status: str) -> None:
        self._assert_lease(cmd.id, worker)
        with self.db.session() as session:
            req = session.get(SupportRequest, cmd.support_request_id)
            if req is not None:
                req.jira_expected_status = status

    def _clear_expected_status(self, cmd: TeamsCommand) -> None:
        if cmd.command not in ("close", "reopen"):
            return
        with self.db.session() as session:
            req = session.get(SupportRequest, cmd.support_request_id)
            if req is not None:
                req.jira_expected_status = None

    @staticmethod
    def _merge(cmd: TeamsCommand, **values: Any) -> dict[str, Any]:
        return {**(cmd.details or {}), **values}  # new dict so the JSON column change is detected

    def _snapshot(self, command_id: int) -> TeamsCommand:
        with self.db.session() as session:
            row = session.get(TeamsCommand, command_id)
            if row is None:
                raise LookupError(f"command {command_id} not found")
            session.expunge(row)
            return row

    def _request(self, request_id: int) -> SupportRequest:
        with self.db.session() as session:
            req = session.get(SupportRequest, request_id)
            if req is None:
                raise PermanentError(f"support request {request_id} not found", service="opsbridge")
            session.expunge(req)
            return req

    def _assert_lease(self, command_id: int, worker: str) -> None:
        with self.db.session() as session:
            row = session.get(TeamsCommand, command_id)
            if row is None or row.locked_by != worker:
                raise LeaseLost()

    def _update(self, command_id: int, worker: str, **values: object) -> None:
        with self.db.session() as session:
            row = session.get(TeamsCommand, command_id)
            if row is None or row.locked_by != worker:
                raise LeaseLost()
            for key, value in values.items():
                setattr(row, key, value)
            row.locked_until = utcnow() + timedelta(seconds=self.config.worker_lease_seconds)
