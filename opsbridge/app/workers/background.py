"""In-process background execution for the POC.

* A thread pool processes support requests (`enqueue`) and ad-hoc tasks (`submit`).
* A sweeper thread re-discovers due work from the database every WORKER_POLL_INTERVAL_SECONDS,
  so the in-memory queue is only a latency optimisation: nothing is lost on restart.
* The same sweeper renews Graph subscriptions that are close to expiry.

Limitations and the path to a durable broker are documented in docs/architecture.md (3.9).
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from app.core.logging import log_context
from app.database.session import Database
from app.repositories.command_repository import CommandRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.support_repository import SupportRepository
from app.services.command_handler import CommandHandler
from app.services.jira_sync_service import JiraSyncService, ThreadUpdateNotifier
from app.services.message_processor import MessageProcessor
from app.services.subscription_service import SubscriptionService

logger = logging.getLogger("opsbridge.worker")


class SyncDispatcher:
    """Runs work inline. Used by tests and one-off scripts."""

    def __init__(self, processor: MessageProcessor | None = None, commands: CommandHandler | None = None,
                 notifications: ThreadUpdateNotifier | None = None) -> None:
        self.processor = processor
        self.commands = commands
        self.notifications = notifications
        self.enqueued: list[int] = []
        self.enqueued_commands: list[int] = []
        self.enqueued_notifications: list[int] = []

    def enqueue(self, request_id: int) -> None:
        self.enqueued.append(request_id)
        if self.processor is not None:
            self.processor.process(request_id)

    def enqueue_command(self, command_id: int) -> None:
        self.enqueued_commands.append(command_id)
        if self.commands is not None:
            self.commands.process(command_id)

    def enqueue_notification(self, notification_id: int) -> None:
        self.enqueued_notifications.append(notification_id)
        if self.notifications is not None:
            self.notifications.process(notification_id)

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        fn(*args)


class BackgroundWorker:
    def __init__(
        self,
        *,
        db: Database,
        processor: MessageProcessor,
        subscriptions: SubscriptionService | None,
        commands: CommandHandler | None = None,
        notifications: ThreadUpdateNotifier | None = None,
        jira_sync: JiraSyncService | None = None,
        jira_poll_interval: float = 0,
        worker_count: int,
        poll_interval: float,
        subscription_check_interval: float,
    ) -> None:
        self.db = db
        self.processor = processor
        self.subscriptions = subscriptions
        self.commands = commands
        self.notifications = notifications
        self.jira_sync = jira_sync
        self.jira_poll_interval = jira_poll_interval
        self._last_jira_poll = 0.0
        self.worker_count = worker_count
        self.poll_interval = poll_interval
        self.subscription_check_interval = subscription_check_interval
        self.instance_id = f"{socket.gethostname()[:24]}-{os.getpid()}"
        self._executor: ThreadPoolExecutor | None = None
        self._sweeper: threading.Thread | None = None
        self._stop = threading.Event()
        self._inflight: set[tuple[str, int]] = set()  # (kind, id)
        self._lock = threading.Lock()
        self._last_subscription_check = 0.0

    @property
    def running(self) -> bool:
        return self._executor is not None and not self._stop.is_set()

    def start(self) -> None:
        self._stop.clear()
        self._executor = ThreadPoolExecutor(max_workers=self.worker_count, thread_name_prefix="opsbridge-worker")
        self._sweeper = threading.Thread(target=self._sweep_loop, name="opsbridge-sweeper", daemon=True)
        self._sweeper.start()
        logger.info("background worker started", extra={"workers": self.worker_count, "instance": self.instance_id})

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        if self._sweeper:
            self._sweeper.join(timeout=timeout)
        if self._executor:
            self._executor.shutdown(wait=True, cancel_futures=True)
        self._executor = None
        logger.info("background worker stopped")

    # ---- Dispatcher protocol ------------------------------------------------------------------
    def enqueue(self, request_id: int) -> None:
        self._enqueue("request", request_id)

    def enqueue_command(self, command_id: int) -> None:
        if self.commands is not None:
            self._enqueue("command", command_id)

    def enqueue_notification(self, notification_id: int) -> None:
        if self.notifications is not None:
            self._enqueue("notification", notification_id)

    def _enqueue(self, kind: str, item_id: int) -> None:
        with self._lock:
            if (kind, item_id) in self._inflight or self._executor is None:
                return  # already queued here; or not running (sweeper will pick it up later)
            self._inflight.add((kind, item_id))
        self._executor.submit(self._process, kind, item_id)

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        if self._executor is None:
            logger.warning("worker not running; executing task inline", extra={"task": fn.__name__})
            self._run_task(fn, *args)
            return
        self._executor.submit(self._run_task, fn, *args)

    # ---- internals -------------------------------------------------------------------------------
    def _process(self, kind: str, item_id: int) -> None:
        worker_id = f"{self.instance_id}-{threading.current_thread().name[-2:]}"
        try:
            if kind == "command":
                self.commands.process(item_id, worker_id=worker_id)  # type: ignore[union-attr]
            elif kind == "notification":
                self.notifications.process(item_id, worker_id=worker_id)  # type: ignore[union-attr]
            else:
                self.processor.process(item_id, worker_id=worker_id)
        except Exception:  # never kill the pool thread
            logger.exception("worker crashed while processing", extra={"kind": kind, "id": item_id})
        finally:
            with self._lock:
                self._inflight.discard((kind, item_id))

    @staticmethod
    def _run_task(fn: Callable[..., Any], *args: Any) -> None:
        with log_context():
            try:
                fn(*args)
            except Exception:
                logger.exception("background task failed", extra={"task": getattr(fn, "__name__", str(fn))})

    def _sweep_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self.db.session() as session:
                    due = SupportRepository(session).due_ids(limit=100)
                    due_commands = CommandRepository(session).due_ids(limit=100) if self.commands else []
                    due_notifications = (NotificationRepository(session).due_ids(limit=100)
                                         if self.notifications else [])
                for request_id in due:
                    self.enqueue(request_id)
                for command_id in due_commands:
                    self.enqueue_command(command_id)
                for notification_id in due_notifications:
                    self.enqueue_notification(notification_id)
                if due or due_commands:
                    logger.debug("sweeper enqueued due work", extra={"requests": len(due),
                                                                     "commands": len(due_commands)})
                self._maybe_renew_subscriptions()
                self._maybe_poll_jira()
            except Exception:
                logger.exception("sweeper iteration failed")
            self._stop.wait(self.poll_interval)

    def _maybe_poll_jira(self) -> None:
        if self.jira_sync is None or self.jira_poll_interval <= 0:
            return
        now = time.monotonic()  # interval pacing only; a sleep-skewed clock just delays one poll
        if now - self._last_jira_poll < self.jira_poll_interval:
            return
        self._last_jira_poll = now
        self.submit(self.jira_sync.poll_once)  # runs on the pool; failures are logged by _run_task

    def _maybe_renew_subscriptions(self) -> None:
        if self.subscriptions is None:
            return
        now = time.monotonic()
        if now - self._last_subscription_check < self.subscription_check_interval:
            return
        self._last_subscription_check = now
        with log_context():
            try:
                outcomes = self.subscriptions.renew(only_expiring=True)
                if outcomes:
                    logger.info("subscription renewal check", extra={"renewed": [o.action for o in outcomes]})
            except Exception:
                logger.exception("subscription renewal failed; will retry next interval")
