"""Jira -> Teams sync: when a ticket's status or assignee changes in Jira, post it in the thread.

Two sources feed one change detector (`JiraSyncService.observe`):

* Polling (default): every JIRA_SYNC_POLL_INTERVAL_SECONDS, search the project for issues
  updated recently. Needs no Jira admin setup.
* Webhook (optional): Jira calls POST /webhooks/jira for near-real-time updates.

Change detection compares the observed state with the last state stored on the support request
(`jira_status`, `jira_assignee_id`, `jira_updated_at`):

* Out-of-order / duplicate events: an observation whose Jira `updated` is not newer than the
  stored one is ignored.
* Concurrency (webhook + poll at the same moment): the snapshot is advanced with a
  compare-and-set UPDATE, and the notification's `dedupe_key` is unique.
* First sighting: only records a baseline; nothing is posted.
* OpsBridge's own close/reopen: the command records `jira_expected_status` before transitioning;
  a change to exactly that status is not announced again.

Notifications are outbox rows (`teams_notifications`) delivered by `ThreadUpdateNotifier`
through the background worker, with the usual lease, backoff and ambiguous-reply protection.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConfigurationError, IntegrationError, PermanentError
from app.core.logging import log_context
from app.core.retry import exponential_backoff
from app.core.time import utcnow
from app.database.session import Database
from app.domain.enums import NotificationStatus, ProcessingStatus
from app.integrations.jira_client import JiraClient, JiraIssueState, issue_state_from_json
from app.models.support_request import SupportRequest
from app.models.teams_notification import TeamsNotification
from app.repositories.notification_repository import NotificationRepository
from app.services.notification_service import NotificationService, ReplyTarget, render_jira_update, update_marker

logger = logging.getLogger("opsbridge.jira_sync")


def _same(a: str | None, b: str | None) -> bool:
    return (a or "").casefold() == (b or "").casefold()


@dataclass(frozen=True)
class SyncConfig:
    enabled: bool = True
    project_key: str = "OPS"
    jira_base_url: str = ""
    close_status: str = "Done"
    poll_interval_seconds: int = 120


class JiraSyncService:
    def __init__(self, *, db: Database, jira: JiraClient, dispatcher: Any, config: SyncConfig) -> None:
        self.db = db
        self.jira = jira
        self.dispatcher = dispatcher
        self.config = config

    # ---- sources ----------------------------------------------------------------------------------
    def poll_once(self) -> int:
        """Look for recently updated issues; returns the number of notifications created."""
        if not self.config.enabled:
            return 0
        # Window comfortably larger than the interval so a slow/failed poll never leaves a gap;
        # re-observing an unchanged issue is a no-op.
        minutes = max(5, math.ceil(2 * self.config.poll_interval_seconds / 60) + 2)
        self.backfill_baselines()
        created = 0
        for state in self.jira.search_recently_updated(self.config.project_key, minutes):
            if self.observe(state, source="poll") is not None:
                created += 1
        if created:
            logger.info("Jira poll found changes", extra={"notifications": created})
        return created

    def backfill_baselines(self, limit: int = 25) -> int:
        """Record the current Jira state for ticketed requests that have none yet (e.g. created
        before the sync existed, or the post-create read failed), so their FIRST change after this
        is announced. Read-only towards Jira; never posts anything."""
        with self.db.session() as session:
            rows = session.execute(
                select(SupportRequest.id, SupportRequest.jira_issue_key)
                .where(SupportRequest.jira_issue_key.is_not(None), SupportRequest.jira_status.is_(None),
                       SupportRequest.jira_issue_url.like(f"{self.config.jira_base_url}/%"))
                .order_by(SupportRequest.id.desc())
                .limit(limit)
            ).all()
        recorded = 0
        for request_id, key in rows:
            try:
                d = self.jira.get_issue_details(key)
            except Exception as exc:  # noqa: BLE001 - e.g. issue deleted; try again next poll
                logger.warning("could not read Jira state for baseline", extra={"jira_key": key, "error": str(exc)[:200]})
                continue
            with self.db.session() as session:
                result = session.execute(
                    update(SupportRequest)
                    .where(SupportRequest.id == request_id, SupportRequest.jira_status.is_(None))
                    .values(jira_status=d.status, jira_assignee_id=d.assignee_id, jira_assignee_name=d.assignee,
                            jira_updated_at=d.updated)
                    .execution_options(synchronize_session=False)
                )
                recorded += result.rowcount
        if recorded:
            logger.info("recorded Jira baselines", extra={"count": recorded})
        return recorded

    def handle_webhook(self, payload: dict[str, Any], *, trace_id: str | None = None) -> int | None:
        event = str(payload.get("webhookEvent") or "")
        issue = payload.get("issue")
        if event not in ("jira:issue_updated", "jira:issue_created") or not isinstance(issue, dict):
            return None
        actor = (payload.get("user") or {}).get("displayName")
        return self.observe(issue_state_from_json(issue), source="webhook", actor_name=actor, trace_id=trace_id)

    # ---- change detection ---------------------------------------------------------------------------
    def observe(self, state: JiraIssueState, *, source: str, actor_name: str | None = None,
                trace_id: str | None = None) -> int | None:
        """Compare an observed issue state with the stored snapshot. Returns a notification id
        when a change worth posting was detected, else None."""
        if not self.config.enabled or not state.key:
            return None
        try:
            with self.db.session() as session:
                req = session.scalar(
                    select(SupportRequest)
                    .where(SupportRequest.jira_issue_key == state.key,
                           SupportRequest.jira_issue_url.like(f"{self.config.jira_base_url}/%"))
                    .order_by(SupportRequest.id.desc())
                )
                if req is None:
                    return None  # not an OpsBridge ticket
                if state.updated and req.jira_updated_at and state.updated <= req.jira_updated_at:
                    return None  # duplicate or out-of-order event

                baseline = req.jira_status is None
                status_changed = not baseline and not _same(req.jira_status, state.status)
                assignee_changed = not baseline and (req.jira_assignee_id or None) != (state.assignee_id or None)
                self_caused = (status_changed and not assignee_changed
                               and req.jira_expected_status and _same(state.status, req.jira_expected_status))
                old = {"status": req.jira_status, "assignee": req.jira_assignee_name}

                # Compare-and-set: only one concurrent observer may advance the snapshot.
                guard = (SupportRequest.jira_updated_at.is_(None) if req.jira_updated_at is None
                         else SupportRequest.jira_updated_at == req.jira_updated_at)
                values: dict[str, Any] = {
                    "jira_status": state.status, "jira_assignee_id": state.assignee_id,
                    "jira_assignee_name": state.assignee_name, "jira_updated_at": state.updated or utcnow(),
                }
                if self_caused:
                    values["jira_expected_status"] = None
                result = session.execute(
                    update(SupportRequest).where(SupportRequest.id == req.id, guard).values(**values)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    return None  # someone else observed a newer state first
                if baseline or not (status_changed or assignee_changed):
                    return None
                if self_caused:
                    logger.info("Jira status change made by OpsBridge; not announced", extra={"jira_key": state.key})
                    return None

                self._sync_request_status(session, req.id, req.processing_status, old["status"], state.status,
                                          actor_name)
                notification = TeamsNotification(
                    support_request_id=req.id,
                    kind="jira_update",
                    dedupe_key=f"jira:{state.key}:{(state.updated or utcnow()).isoformat()}:"
                               f"{state.status}:{state.assignee_id or '-'}"[:256],
                    payload={
                        "jira_key": state.key, "jira_url": req.jira_issue_url,
                        "status_changed": status_changed, "old_status": old["status"], "new_status": state.status,
                        "assignee_changed": assignee_changed, "old_assignee": old["assignee"],
                        "new_assignee": state.assignee_name, "actor": actor_name, "source": source,
                    },
                    status=NotificationStatus.PENDING,
                    trace_id=trace_id or req.trace_id,
                    next_attempt_at=utcnow(),
                )
                session.add(notification)
                session.flush()
                notification_id = notification.id
        except IntegrityError:
            return None  # the same change was already recorded (unique dedupe_key)
        logger.info("Jira change detected", extra={"jira_key": state.key, "source": source,
                                                   "status_changed": status_changed,
                                                   "assignee_changed": assignee_changed})
        if self.dispatcher is not None:
            self.dispatcher.enqueue_notification(notification_id)
        return notification_id

    def _sync_request_status(self, session, request_id: int, processing_status: ProcessingStatus,  # noqa: ANN001
                             old_status: str | None, new_status: str, actor_name: str | None) -> None:
        """Keep OpsBridge's view in step when a ticket is closed/reopened directly in Jira."""
        close = self.config.close_status
        req = session.get(SupportRequest, request_id)
        if _same(new_status, close) and processing_status == ProcessingStatus.COMPLETED:
            req.processing_status = ProcessingStatus.CLOSED
            req.closed_at = utcnow()
            req.closed_by_id = None
            req.closed_by_name = actor_name or "Jira"
            req.close_reason = "Closed in Jira"
        elif _same(old_status, close) and not _same(new_status, close) and processing_status == ProcessingStatus.CLOSED:
            req.processing_status = ProcessingStatus.COMPLETED
            req.reopened_at = utcnow()
            req.reopened_by_id = None
            req.reopened_by_name = actor_name or "Jira"
            req.reopen_reason = "Reopened in Jira"
            req.closed_at = req.closed_by_id = req.closed_by_name = req.close_reason = None
            req.close_command_message_id = None


# =============================================================================================
# Delivery: post pending notifications in the request thread
# =============================================================================================
@dataclass(frozen=True)
class DeliveryConfig:
    worker_lease_seconds: int = 120
    max_retries: int = 5
    retry_base_delay_seconds: float = 5.0
    retry_max_delay_seconds: float = 900.0


class LeaseLost(Exception):
    pass


class ThreadUpdateNotifier:
    def __init__(self, *, db: Database, notifier: NotificationService, config: DeliveryConfig,
                 worker_id: str = "worker") -> None:
        self.db = db
        self.notifier = notifier
        self.config = config
        self.worker_id = worker_id

    def process(self, notification_id: int, *, worker_id: str | None = None) -> NotificationStatus | None:
        worker = worker_id or self.worker_id
        with self.db.session() as session:
            repo = NotificationRepository(session)
            if not repo.claim(notification_id, worker_id=worker, lease_seconds=self.config.worker_lease_seconds):
                return None
            row = repo.get(notification_id)
            trace_id, request_id = row.trace_id, row.support_request_id  # type: ignore[union-attr]
        with log_context(trace_id=trace_id, support_request_id=request_id):
            try:
                self._deliver(notification_id, worker)
            except LeaseLost:
                logger.warning("notification lease lost; another worker owns it now")
                return None
            except (IntegrationError, ConfigurationError) as exc:
                self._handle_failure(notification_id, worker, exc)
            except Exception as exc:  # unexpected: keep the row, retry with backoff
                logger.exception("unexpected error delivering thread update")
                self._handle_failure(notification_id, worker, exc)
            finally:
                with self.db.session() as session:
                    NotificationRepository(session).release(notification_id, worker_id=worker)
            with self.db.session() as session:
                return session.get(TeamsNotification, notification_id).status  # type: ignore[union-attr]

    def _deliver(self, notification_id: int, worker: str) -> None:
        with self.db.session() as session:
            row = session.get(TeamsNotification, notification_id)
            req = session.get(SupportRequest, row.support_request_id)  # type: ignore[union-attr]
            session.expunge_all()
        target = ReplyTarget(req.id, req.teams_team_id, req.teams_channel_id, req.teams_message_id)  # type: ignore[union-attr]
        marker = update_marker(row.id)  # type: ignore[union-attr]
        if row.reply_attempts > 0:  # type: ignore[union-attr]
            existing = self.notifier.already_replied(target, marker)
            if existing:
                self._update(notification_id, worker, status=NotificationStatus.SENT, sent_at=utcnow(),
                             response_message_id=existing, error_message=None)
                return
        self._update(notification_id, worker, reply_attempts=row.reply_attempts + 1)  # type: ignore[union-attr]
        reply_id = self.notifier.send(target, render_jira_update(notification_id=row.id, **row.payload))  # type: ignore[union-attr]
        self._update(notification_id, worker, status=NotificationStatus.SENT, sent_at=utcnow(),
                     response_message_id=reply_id, error_message=None, retry_count=0)
        logger.info("posted Jira update in thread", extra={"jira_key": row.payload.get("jira_key")})  # type: ignore[union-attr]

    def _handle_failure(self, notification_id: int, worker: str, exc: Exception) -> None:
        with self.db.session() as session:
            retry_count = session.get(TeamsNotification, notification_id).retry_count + 1  # type: ignore[union-attr]
        message = f"{type(exc).__name__}: {exc}"[:2000]
        if isinstance(exc, (PermanentError, ConfigurationError)) or retry_count > self.config.max_retries:
            logger.error("thread update failed", extra={"error": message, "retry_count": retry_count})
            self._update(notification_id, worker, status=NotificationStatus.FAILED, error_message=message,
                         retry_count=retry_count)
            return
        delay = exponential_backoff(retry_count - 1, base=self.config.retry_base_delay_seconds,
                                    cap=self.config.retry_max_delay_seconds)
        if isinstance(exc, IntegrationError) and exc.retry_after:
            delay = max(delay, exc.retry_after)
        logger.warning("thread update failed; retry scheduled", extra={"error": message, "delay_s": round(delay, 1)})
        self._update(notification_id, worker, error_message=message, retry_count=retry_count,
                     next_attempt_at=utcnow() + timedelta(seconds=delay))

    def _update(self, notification_id: int, worker: str, **values: object) -> None:
        with self.db.session() as session:
            row = session.get(TeamsNotification, notification_id)
            if row is None or row.locked_by != worker:
                raise LeaseLost()
            for key, value in values.items():
                setattr(row, key, value)
            row.locked_until = utcnow() + timedelta(seconds=self.config.worker_lease_seconds)
