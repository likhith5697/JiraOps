"""Support-request state machine.

Each stage follows the same pattern:
    1. read a snapshot (short transaction)
    2. call the external system (NO transaction held: SQLite stays unlocked during HTTP)
    3. apply the outcome in a short transaction, guarded by our lease

See docs/architecture.md section 3 for the state diagram and idempotency guarantees.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from app.core.errors import AmbiguousError, ConfigurationError, IntegrationError, NotFoundError, PermanentError
from app.core.logging import log_context
from app.core.retry import exponential_backoff
from app.core.time import utcnow
from app.database.session import Database
from app.domain.enums import Environment, Priority, ProcessingStatus
from app.domain.messages import InboundMessage, SupportRequestDraft
from app.integrations.microsoft_graph import GraphClient
from app.models.support_request import SupportRequest
from app.repositories.support_repository import SupportRepository
from app.services.jira_service import JiraService, TicketRequest, idempotency_label
from app.services.notification_service import NotificationService, ReplyTarget, render_rejection, render_success
from app.services.request_parser import RequestParser
from app.services.request_validator import RequestValidator

logger = logging.getLogger("opsbridge.processor")

S = ProcessingStatus


@dataclass(frozen=True)
class ProcessorConfig:
    team_id: str
    channel_id: str
    service_account_user_id: str | None
    worker_lease_seconds: int = 120
    max_retries: int = 5
    retry_base_delay_seconds: float = 5.0
    retry_max_delay_seconds: float = 900.0
    jira_reconcile_delay_seconds: int = 60


class LeaseLost(Exception):
    """Another worker took over this request (our lease expired). Stop without writing."""


class MessageProcessor:
    def __init__(
        self,
        *,
        db: Database,
        graph: GraphClient,
        parser: RequestParser,
        validator: RequestValidator,
        jira: JiraService,
        notifier: NotificationService,
        config: ProcessorConfig,
        worker_id: str = "worker",
    ) -> None:
        self.db = db
        self.graph = graph
        self.parser = parser
        self.validator = validator
        self.jira = jira
        self.notifier = notifier
        self.config = config
        self.worker_id = worker_id
        # Set by the container: routes a fetched reply to thread-command handling.
        # Returns a truthy value when the reply was accepted as a command candidate.
        self.on_reply: Callable[[InboundMessage, str | None], object] | None = None

    # =========================================================================================
    # Entry point
    # =========================================================================================
    def process(self, request_id: int, *, worker_id: str | None = None) -> ProcessingStatus | None:
        """Claim and advance a request as far as possible. Returns final status, or None if not claimed."""
        worker = worker_id or self.worker_id
        with self.db.session() as session:
            if not SupportRepository(session).claim(
                request_id, worker_id=worker, lease_seconds=self.config.worker_lease_seconds
            ):
                return None
            trace_id = session.get(SupportRequest, request_id).trace_id  # type: ignore[union-attr]

        with log_context(trace_id=trace_id, support_request_id=request_id):
            try:
                return self._run(request_id, worker)
            except LeaseLost:
                logger.warning("lease lost; another worker owns this request now")
                return None
            finally:
                with self.db.session() as session:
                    SupportRepository(session).release(request_id, worker_id=worker)

    def _run(self, request_id: int, worker: str) -> ProcessingStatus:
        stages: dict[ProcessingStatus, Callable[[SupportRequest, str], None]] = {
            S.RECEIVED: self._stage_fetch_and_validate,
            S.VALIDATED: self._stage_create_jira_issue,
            S.JIRA_UNKNOWN: self._stage_create_jira_issue,
            S.JIRA_CREATED: self._stage_acknowledge,
            S.INVALID: self._stage_acknowledge,
        }
        for _ in range(6):  # bounded: at most RECEIVED -> VALIDATED -> JIRA_CREATED -> COMPLETED
            snapshot = self._snapshot(request_id)
            if snapshot.processing_status not in stages or snapshot.next_attempt_at > utcnow():
                return snapshot.processing_status
            before = snapshot.processing_status
            try:
                stages[before](snapshot, worker)
            except LeaseLost:
                raise
            except (IntegrationError, ConfigurationError) as exc:
                self._handle_failure(request_id, worker, before, exc)
            except Exception as exc:  # unexpected bug: never lose the request, retry with backoff
                logger.exception("unexpected error while processing stage %s", before)
                self._handle_failure(request_id, worker, before, exc)
        return self._snapshot(request_id).processing_status

    # =========================================================================================
    # Stage 1: fetch message from Graph, filter, parse, validate
    # =========================================================================================
    def _stage_fetch_and_validate(self, rec: SupportRequest, worker: str) -> None:
        try:
            message = self.graph.get_channel_message(rec.teams_team_id, rec.teams_channel_id, rec.teams_message_id)
        except NotFoundError:
            self._update(rec.id, worker, processing_status=S.IGNORED, error_message="Message no longer exists in Teams")
            return

        reason = self._ignore_reason(message)
        common = {
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "teams_message_url": message.web_url,
            "teams_message_created_at": message.created_at,
            "raw_message": message.body_text[:50_000],
        }
        if message.is_reply and not message.sender_is_application and self.on_reply is not None:
            # Routing: top-level message -> intake (below); reply -> command handling; else ignored.
            routed = self.on_reply(message, rec.trace_id)
            reason = "Reply routed to thread-command handling" if routed else reason
        if reason:
            logger.info("ignoring message: %s", reason)
            self._update(rec.id, worker, processing_status=S.IGNORED, error_message=reason, **common)
            return

        parsed = self.parser.parse(message)
        result = self.validator.validate(parsed)
        if not result.is_valid:
            logger.info("submission invalid", extra={"issues": [i.message for i in result.issues]})
            self._update(
                rec.id, worker, **common,
                processing_status=S.INVALID,
                parser_name=parsed.parser_name,
                validation_errors=[{"field": i.field, "message": i.message} for i in result.issues],
                # keep whatever was extracted for auditability
                api_name=(parsed.fields.get("api_name") or "")[:128] or None,
                environment=(parsed.fields.get("environment") or "")[:16] or None,
                priority=(parsed.fields.get("priority") or "")[:8] or None,
                error_message=None, retry_count=0, next_attempt_at=utcnow(),
            )
            return

        d = result.draft
        assert d is not None
        self._update(
            rec.id, worker, **common,
            processing_status=S.VALIDATED,
            parser_name=parsed.parser_name,
            validation_errors=None,
            api_name=d.api_name,
            environment=d.environment.value,
            issue_summary=d.issue_summary,
            issue_description=d.issue_description,
            correlation_id=d.correlation_id,
            business_impact=d.business_impact,
            priority=d.priority.value,
            optional_fields=d.optional_fields or None,
            jira_idempotency_label=idempotency_label(rec.teams_message_id),
            error_message=None, retry_count=0, next_attempt_at=utcnow(),
        )

    def _ignore_reason(self, m: InboundMessage) -> str | None:
        if m.deleted:
            return "Message was deleted"
        if m.is_reply:
            return "Message is a reply, not a new support request"
        if m.message_type != "message":
            return f"Unsupported message type '{m.message_type}'"
        if m.sender_is_application:
            return "Message was posted by a bot or application"
        if self.config.service_account_user_id and m.sender_id == self.config.service_account_user_id:
            return "Message was posted by OpsBridge itself"
        if m.channel_id != self.config.channel_id or m.team_id != self.config.team_id:
            return "Message is not from the configured support channel"
        return None

    # =========================================================================================
    # Stage 2: create (or reconcile) the Jira issue - never duplicate
    # =========================================================================================
    def _stage_create_jira_issue(self, rec: SupportRequest, worker: str) -> None:
        if rec.jira_create_attempts > 0:
            # A previous attempt may have succeeded (timeout / crash mid-call). Look before leaping.
            existing = self.jira.find_existing(rec.teams_message_id)
            if existing:
                logger.info("reconciled existing Jira issue", extra={"jira_key": existing.key})
                self._mark_jira_created(rec.id, worker, existing.key, existing.id, existing.url)
                return
            logger.info("reconciliation found no Jira issue; safe to create",
                        extra={"previous_attempts": rec.jira_create_attempts})

        # Write-ahead: persist the attempt BEFORE the non-idempotent call.
        self._update(rec.id, worker, jira_create_attempts=rec.jira_create_attempts + 1)
        ref = self.jira.create_issue(self._ticket_request(rec))
        logger.info("created Jira issue", extra={"jira_key": ref.key})
        self._mark_jira_created(rec.id, worker, ref.key, ref.id, ref.url)

    def _mark_jira_created(self, request_id: int, worker: str, key: str, issue_id: str, url: str) -> None:
        self._update(
            request_id, worker,
            processing_status=S.JIRA_CREATED, jira_issue_key=key, jira_issue_id=issue_id, jira_issue_url=url,
            error_message=None, retry_count=0, next_attempt_at=utcnow(),
        )
        self._record_jira_baseline(request_id, worker, key)

    def _record_jira_baseline(self, request_id: int, worker: str, key: str) -> None:
        """Best effort: remember the issue's initial status/assignee so the Jira -> Teams sync can
        announce the very first change. A failure here never fails the request (the sync then
        records the baseline on its first observation)."""
        try:
            details = self.jira.client.get_issue_details(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read initial Jira state", extra={"jira_key": key, "error": str(exc)[:200]})
            return
        self._update(request_id, worker, jira_status=details.status, jira_assignee_id=details.assignee_id,
                     jira_assignee_name=details.assignee, jira_updated_at=details.updated)

    @staticmethod
    def _ticket_request(rec: SupportRequest) -> TicketRequest:
        draft = SupportRequestDraft(
            api_name=rec.api_name or "",
            environment=Environment(rec.environment),
            issue_summary=rec.issue_summary or "",
            issue_description=rec.issue_description or "",
            correlation_id=rec.correlation_id or "",
            business_impact=rec.business_impact or "",
            priority=Priority(rec.priority),
            optional_fields=dict(rec.optional_fields or {}),
        )
        return TicketRequest(
            draft=draft,
            teams_message_id=rec.teams_message_id,
            teams_message_url=rec.teams_message_url,
            sender_name=rec.sender_name,
            opsbridge_request_id=rec.id,
        )

    # =========================================================================================
    # Stage 3: reply in the Teams thread (success or rejection)
    # =========================================================================================
    def _stage_acknowledge(self, rec: SupportRequest, worker: str) -> None:
        target = ReplyTarget(rec.id, rec.teams_team_id, rec.teams_channel_id, rec.teams_message_id)
        done_status = S.COMPLETED if rec.processing_status == S.JIRA_CREATED else S.REJECTED

        if rec.ack_attempts > 0:
            existing_reply = self.notifier.already_replied(target)
            if existing_reply:
                logger.info("acknowledgement already posted; not re-sending")
                self._mark_acknowledged(rec.id, worker, done_status, existing_reply)
                return

        if rec.processing_status == S.JIRA_CREATED:
            body = render_success(
                request_id=rec.id, jira_key=rec.jira_issue_key or "", jira_url=rec.jira_issue_url or "",
                priority=rec.priority or "", environment=rec.environment or "",
            )
        else:
            body = render_rejection(
                request_id=rec.id, problems=[e["message"] for e in (rec.validation_errors or [])]
            )
        self._update(rec.id, worker, ack_attempts=rec.ack_attempts + 1)
        reply_id = self.notifier.send(target, body)
        self._mark_acknowledged(rec.id, worker, done_status, reply_id)

    def _mark_acknowledged(self, request_id: int, worker: str, status: ProcessingStatus, reply_id: str) -> None:
        if status == S.COMPLETED and self._snapshot(request_id).closed_at is not None:
            status = S.CLOSED  # closed from Teams while the acknowledgement was still pending
        self._update(
            request_id, worker,
            processing_status=status, acknowledgement_message_id=reply_id, acknowledged_at=utcnow(),
            error_message=None, retry_count=0, next_attempt_at=utcnow(),
        )

    # =========================================================================================
    # Failure handling
    # =========================================================================================
    def _handle_failure(self, request_id: int, worker: str, stage: ProcessingStatus, exc: Exception) -> None:
        rec = self._snapshot(request_id)
        message = f"[{stage.value}] {type(exc).__name__}: {exc}"[:2000]

        if isinstance(exc, (PermanentError, ConfigurationError)):
            logger.error("permanent failure; marking FAILED", extra={"error": message})
            self._update(request_id, worker, processing_status=S.FAILED, error_message=message)
            return

        retry_count = rec.retry_count + 1
        if retry_count > self.config.max_retries:
            logger.error("retries exhausted; marking FAILED", extra={"error": message, "retry_count": retry_count})
            # An ambiguous Jira outcome stays recoverable: manual retry will reconcile first.
            self._update(request_id, worker, processing_status=S.FAILED, error_message=message,
                         retry_count=retry_count)
            return

        delay = exponential_backoff(
            retry_count - 1, base=self.config.retry_base_delay_seconds, cap=self.config.retry_max_delay_seconds
        )
        if isinstance(exc, IntegrationError) and exc.retry_after:
            delay = max(delay, exc.retry_after)
        new_status = stage
        if stage in (S.VALIDATED, S.JIRA_UNKNOWN) and (isinstance(exc, AmbiguousError) or stage == S.JIRA_UNKNOWN):
            new_status = S.JIRA_UNKNOWN
            delay = max(delay, float(self.config.jira_reconcile_delay_seconds))

        logger.warning(
            "stage failed; retry scheduled",
            extra={"error": message, "retry_count": retry_count, "delay_s": round(delay, 1),
                   "status": new_status.value},
        )
        self._update(
            request_id, worker, processing_status=new_status, error_message=message, retry_count=retry_count,
            next_attempt_at=utcnow() + timedelta(seconds=delay),
        )

    # =========================================================================================
    # Persistence helpers
    # =========================================================================================
    def _snapshot(self, request_id: int) -> SupportRequest:
        with self.db.session() as session:
            rec = session.get(SupportRequest, request_id)
            if rec is None:
                raise LookupError(f"support request {request_id} not found")
            session.expunge(rec)
            return rec

    def _update(self, request_id: int, worker: str, **values: object) -> None:
        """Apply changes only while we still hold the lease; also extends the lease."""
        with self.db.session() as session:
            rec = session.get(SupportRequest, request_id)
            if rec is None or rec.locked_by != worker:
                raise LeaseLost()
            for key, value in values.items():
                setattr(rec, key, value)
            rec.locked_until = utcnow() + timedelta(seconds=self.config.worker_lease_seconds)
