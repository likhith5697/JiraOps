"""`ask <question>`: answer from knowledge captured from past resolved tickets.

Flow per AskQuery (background worker, same lease/backoff/429 handling as everything else):

    RECEIVED --search + compose answer (stored)--> REPLYING --post in thread--> ANSWERED

Answer modes:
* none    - nothing relevant (score < ASK_MIN_SCORE): say so, point to the support template.
* llm     - LLM enabled and within budget: ASK_ANSWER_V1 sees ONLY the retrieved entries. Every Jira
            key it cites must be one of them (and it must cite at least one), otherwise the answer
            is discarded and the keyword format is used. Grounding is enforced, not just requested.
* keyword - formatted list of the matching past fixes (also the fallback for any LLM problem).

An `ask` never creates a Jira ticket. The answer is computed once and stored, so a retried Teams
reply posts exactly the same text.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConfigurationError, IntegrationError, PermanentError
from app.core.logging import log_context
from app.core.retry import exponential_backoff
from app.core.time import utcnow
from app.database.session import Database
from app.domain.enums import AskStatus, ProcessingStatus
from app.llm.base import LLMUnavailable
from app.llm.gateway import LLMGateway
from app.llm.prompts import ASK_ANSWER_V1
from app.llm.redaction import redact_text
from app.models.knowledge import AskFeedback, AskQuery, KnowledgeEntry
from app.models.support_request import SupportRequest
from app.repositories.ask_repository import AskRepository
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.support_repository import InsertResult
from app.services.knowledge_search import KnowledgeSearch, SearchResult
from app.services.notification_service import NotificationService, ReplyTarget, _lines_to_html
from app.services.intake_template import template_text

logger = logging.getLogger("opsbridge.ask")

NO_MATCH_TEXT = ("I couldn't find a similar resolved issue. "
                 "Please post a support request using the template.")
USAGE_TEXT = "Usage: ask <your question>, e.g. ask why does CustomerOrderAPI return HTTP 500 in QA?"
FOOTER = "Reply `helpful` or `not helpful`. Still stuck? Post a support request and a ticket will be created."
_JIRA_KEY = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
_CITATION = re.compile(r"\[([A-Z][A-Z0-9_]+-\d+)\]")
FEEDBACK_VALUES = ("helpful", "not_helpful")


def ask_marker(ask_id: int) -> str:
    return f"(OpsBridge ask {ask_id})"


@dataclass(frozen=True)
class AskConfig:
    worker_lease_seconds: int = 120
    max_retries: int = 5
    retry_base_delay_seconds: float = 5.0
    retry_max_delay_seconds: float = 900.0


@dataclass(frozen=True)
class ComposedAnswer:
    text: str
    mode: str
    prompt_version: str | None
    cited_ids: list[int]


class LeaseLost(Exception):
    pass


class AskService:
    def __init__(self, *, db: Database, search: KnowledgeSearch, llm: LLMGateway, notifier: NotificationService,
                 config: AskConfig, dispatcher=None, worker_id: str = "worker") -> None:  # noqa: ANN001
        self.db = db
        self.search = search
        self.llm = llm
        self.notifier = notifier
        self.config = config
        self.dispatcher = dispatcher
        self.worker_id = worker_id

    # =========================================================================================
    # Intake
    # =========================================================================================
    def accept(self, *, teams_message_id: str, reply_to_message_id: str, team_id: str, channel_id: str,
               sender_id: str | None, sender_name: str | None, question: str | None,
               trace_id: str | None = None) -> InsertResult:
        with self.db.session() as session:
            result = AskRepository(session).insert_received(
                teams_message_id=teams_message_id, reply_to_message_id=reply_to_message_id,
                teams_team_id=team_id, teams_channel_id=channel_id, sender_id=sender_id, sender_name=sender_name,
                question=redact_text(question or "")[:2000], trace_id=trace_id,
            )
        if result.created:
            logger.info("ask received", extra={"ask_id": result.request_id})
            if self.dispatcher is not None:
                self.dispatcher.enqueue_ask(result.request_id)
        return result

    # =========================================================================================
    # Processing
    # =========================================================================================
    def process(self, ask_id: int, *, worker_id: str | None = None) -> AskStatus | None:
        worker = worker_id or self.worker_id
        with self.db.session() as session:
            repo = AskRepository(session)
            if not repo.claim(ask_id, worker_id=worker, lease_seconds=self.config.worker_lease_seconds):
                return None
            trace_id = repo.get(ask_id).trace_id  # type: ignore[union-attr]
        with log_context(trace_id=trace_id):
            try:
                for _ in range(3):
                    ask = self._snapshot(ask_id)
                    if ask.status not in AskStatus.active() or ask.next_attempt_at > utcnow():
                        break
                    try:
                        (self._stage_answer if ask.status == AskStatus.RECEIVED else self._stage_reply)(ask, worker)
                    except LeaseLost:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        if not isinstance(exc, (IntegrationError, ConfigurationError)):
                            logger.exception("unexpected error answering ask")
                        self._handle_failure(ask_id, worker, exc)
            except LeaseLost:
                logger.warning("ask lease lost; another worker owns it now")
                return None
            finally:
                with self.db.session() as session:
                    AskRepository(session).release(ask_id, worker_id=worker)
        return self._snapshot(ask_id).status

    def _stage_answer(self, ask: AskQuery, worker: str) -> None:
        if not ask.question.strip():
            self._update(ask.id, worker, status=AskStatus.REPLYING, mode="none", answer_text=USAGE_TEXT,
                         retrieved_keys=[], retrieved_entry_ids=[], cited_entry_ids=[], top_score=0.0)
            return
        hints, results = self.search.search(ask.question)
        answer = self.compose(ask.question, results)
        logger.info("ask answered", extra={"mode": answer.mode, "results": len(results),
                                           "top_score": results[0].score if results else 0.0})
        self._update(
            ask.id, worker, status=AskStatus.REPLYING, mode=answer.mode, answer_text=answer.text,
            prompt_version=answer.prompt_version, cited_entry_ids=answer.cited_ids,
            retrieved_keys=[r.entry.jira_issue_key for r in results],
            retrieved_entry_ids=[r.entry.id for r in results],
            top_score=results[0].score if results else 0.0,
            api_hint=hints.api_name, environment_hint=hints.environment, retry_count=0, error_message=None,
        )

    def compose(self, question: str, results: list[SearchResult]) -> ComposedAnswer:
        if not results:
            return ComposedAnswer(NO_MATCH_TEXT, "none", None, [])
        if self.llm.chat_enabled:
            by_key = {r.entry.jira_issue_key: r.entry.id for r in results}
            try:
                result = self.llm.generate(ASK_ANSWER_V1, self._prompt(question, results), max_tokens=400)
                cited = set(_CITATION.findall(result.text))
                # Any key of the same Jira project(s) mentioned anywhere must also be a retrieved one
                # (catches invented tickets); other ID shapes such as CORR-123456 are not tickets.
                prefixes = {key.rsplit("-", 1)[0] + "-" for key in by_key}
                mentioned = {k for k in _JIRA_KEY.findall(result.text) if k.startswith(tuple(prefixes))}
                if cited and cited <= set(by_key) and mentioned <= set(by_key):
                    return ComposedAnswer(result.text.strip(), "llm", ASK_ANSWER_V1.version,
                                          sorted(by_key[k] for k in cited))
                logger.warning("LLM answer failed citation validation; using keyword answer",
                               extra={"cited": sorted(cited | mentioned), "retrieved": sorted(by_key)})
            except LLMUnavailable:
                pass  # disabled / over budget / provider error -> keyword answer
        return ComposedAnswer(self._keyword_answer(results), "keyword", None, [r.entry.id for r in results])

    @staticmethod
    def _one_line(text: str, limit: int) -> str:
        text = " ".join((text or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _problem_summary(entry: KnowledgeEntry) -> str:
        first = (entry.problem_text or "").splitlines()[0] if entry.problem_text else ""
        return first.removeprefix("Summary: ")

    def _prompt(self, question: str, results: list[SearchResult]) -> str:
        blocks = []
        for i, r in enumerate(results, 1):
            e = r.entry
            resolved = e.resolved_at.strftime("%Y-%m-%d") if e.resolved_at else "unknown"
            blocks.append(f"[{e.jira_issue_key}] source={i} environment={e.environment or 'unknown'} "
                          f"api={e.api_name or 'unknown'} resolved={resolved}\n"
                          f"Problem: {self._one_line(e.problem_text, 800)}\n"
                          f"Fix: {self._one_line(e.resolution_text, 800)}")
        return f"QUESTION:\n{redact_text(question)}\n\nSOURCES:\n" + "\n\n".join(blocks)

    def _keyword_answer(self, results: list[SearchResult]) -> str:
        lines = ["Here are similar issues that were resolved before:"]
        for r in results:
            e = r.entry
            resolved = e.resolved_at.strftime("%Y-%m-%d") if e.resolved_at else "date unknown"
            lines += [f"• {e.jira_issue_key} · {e.environment or 'env unknown'} · resolved {resolved}",
                      f"  Problem: {self._one_line(self._problem_summary(e), 200)}",
                      f"  Fix: {self._one_line(e.resolution_text, 300)}"]
        return "\n".join(lines)

    def _stage_reply(self, ask: AskQuery, worker: str) -> None:
        target = ReplyTarget(0, ask.teams_team_id, ask.teams_channel_id, ask.reply_to_message_id)
        marker = ask_marker(ask.id)
        if ask.reply_attempts > 0:
            existing = self.notifier.already_replied(target, marker)
            if existing:
                self._update(ask.id, worker, status=AskStatus.ANSWERED, answered_at=utcnow(),
                             response_message_id=existing, error_message=None)
                return
        self._update(ask.id, worker, reply_attempts=ask.reply_attempts + 1)  # write-ahead
        reply_id = self.notifier.send(target, self.render(ask))
        self._update(ask.id, worker, status=AskStatus.ANSWERED, answered_at=utcnow(), response_message_id=reply_id,
                     error_message=None, retry_count=0)

    def render(self, ask: AskQuery) -> str:
        """Teams HTML: escaped text, Jira keys linked to the retrieved tickets, marker for idempotency."""
        urls = {}
        if ask.retrieved_entry_ids:
            with self.db.session() as session:
                urls = {e.jira_issue_key: e.jira_issue_url
                        for e in KnowledgeRepository(session).get_many(list(ask.retrieved_entry_ids))}

        def linkify(line: str) -> str:
            escaped = html.escape(line)
            for key, url in urls.items():
                if url:
                    escaped = re.sub(rf"\b{re.escape(key)}\b",
                                     f'<a href="{html.escape(url, quote=True)}">{key}</a>', escaped)
            return escaped

        lines = [linkify(line) for line in (ask.answer_text or "").splitlines()]
        if ask.mode in ("llm", "keyword"):
            lines += ["", f"<i>{html.escape(FOOTER)}</i>"]
        elif ask.answer_text == NO_MATCH_TEXT:
            lines += ["", "<b>Template</b>:", "<br>".join(html.escape(t) for t in template_text().split("\n"))]
        lines.append(f"<i>{html.escape(ask_marker(ask.id))}</i>")
        return _lines_to_html(lines)

    # =========================================================================================
    # Feedback
    # =========================================================================================
    def record_feedback(self, ask_id: int, *, sender_id: str, value: str, reply_message_id: str) -> str:
        """Returns 'recorded' or 'already'. Once per user per question; counts adjusted once."""
        if value not in FEEDBACK_VALUES:
            raise ValueError(value)
        try:
            with self.db.session() as session:
                ask = session.get(AskQuery, ask_id)
                if ask is None:
                    raise LookupError(ask_id)
                session.add(AskFeedback(ask_query_id=ask_id, sender_id=sender_id, value=value,
                                        reply_message_id=reply_message_id))
                session.flush()  # unique (ask, sender) / unique reply id -> IntegrityError
                KnowledgeRepository(session).adjust_feedback(
                    list(ask.cited_entry_ids or ask.retrieved_entry_ids or []), helpful=value == "helpful")
                if sender_id and sender_id == ask.sender_id:
                    ask.feedback = value  # the asker's verdict drives the deflection metric
        except IntegrityError:
            return "already"
        logger.info("ask feedback recorded", extra={"ask_id": ask_id, "value": value})
        return "recorded"

    # =========================================================================================
    # Metrics
    # =========================================================================================
    def deflection_metrics(self, window_hours: int = 24) -> dict:
        with self.db.session() as session:
            asks = AskRepository(session).all()
            avoided = 0
            for ask in asks:
                if ask.feedback != "helpful":
                    continue
                query = select(func.count()).select_from(SupportRequest).where(
                    SupportRequest.sender_id == ask.sender_id,
                    SupportRequest.created_at > ask.created_at,
                    SupportRequest.created_at <= ask.created_at + timedelta(hours=window_hours),
                    SupportRequest.processing_status != ProcessingStatus.IGNORED,  # the asks themselves
                )
                if ask.api_hint:
                    query = query.where(func.lower(SupportRequest.api_name) == ask.api_hint.lower())
                if not session.scalar(query):
                    avoided += 1
        total = len(asks)
        answered = sum(1 for a in asks if a.mode in ("llm", "keyword"))
        rated = [a for a in asks if a.feedback]
        helpful = sum(1 for a in rated if a.feedback == "helpful")
        pct = lambda part, whole: round(100.0 * part / whole, 1) if whole else 0.0  # noqa: E731
        return {
            "total_asks": total,
            "answered": answered,
            "answered_pct": pct(answered, total),
            "llm_answers": sum(1 for a in asks if a.mode == "llm"),
            "keyword_answers": sum(1 for a in asks if a.mode == "keyword"),
            "no_match": sum(1 for a in asks if a.mode == "none"),
            "rated": len(rated),
            "helpful": helpful,
            "helpful_pct": pct(helpful, len(rated)),
            "tickets_avoided": avoided,
            "window_hours": window_hours,
        }

    # =========================================================================================
    # Helpers
    # =========================================================================================
    def _handle_failure(self, ask_id: int, worker: str, exc: Exception) -> None:
        ask = self._snapshot(ask_id)
        message = f"{type(exc).__name__}: {exc}"[:2000]
        retry_count = ask.retry_count + 1
        if isinstance(exc, (PermanentError, ConfigurationError)) or retry_count > self.config.max_retries:
            logger.error("ask failed", extra={"error": message, "retry_count": retry_count})
            self._update(ask_id, worker, status=AskStatus.FAILED, error_message=message, retry_count=retry_count)
            return
        delay = exponential_backoff(retry_count - 1, base=self.config.retry_base_delay_seconds,
                                    cap=self.config.retry_max_delay_seconds)
        if isinstance(exc, IntegrationError) and exc.retry_after:
            delay = max(delay, exc.retry_after)
        logger.warning("ask failed; retry scheduled", extra={"error": message, "delay_s": round(delay, 1)})
        self._update(ask_id, worker, error_message=message, retry_count=retry_count,
                     next_attempt_at=utcnow() + timedelta(seconds=delay))

    def _snapshot(self, ask_id: int) -> AskQuery:
        with self.db.session() as session:
            row = session.get(AskQuery, ask_id)
            session.expunge(row)
            return row  # type: ignore[return-value]

    def _update(self, ask_id: int, worker: str, **values: object) -> None:
        with self.db.session() as session:
            row = session.get(AskQuery, ask_id)
            if row is None or row.locked_by != worker:
                raise LeaseLost()
            for key, value in values.items():
                setattr(row, key, value)
            row.locked_until = utcnow() + timedelta(seconds=self.config.worker_lease_seconds)
