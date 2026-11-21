"""Knowledge capture: every closed ticket becomes a searchable "problem -> fix" entry.

Triggered right after a `close` (in the background) and by a periodic sweep that also catches
tickets closed directly in Jira and earlier failures. Steps per ticket:

1. Collect the resolution: the close reason + the newest Jira comments (OpsBridge's own audit
   comments excluded).
2. Skip (and record SKIPPED) when there is no meaningful resolution text.
3. If the LLM is enabled and within budget: summarise (RESOLUTION_SUMMARY_V2), or skip when the
   model finds no actual fix in the notes. Otherwise use the notes as-is. LLM problems never block
   capture.
4. Redact, save (one entry per request; re-closing updates it).
5. Embed `problem + resolution` if an embedding provider is configured. Failure leaves the entry
   keyword-searchable; `embed_missing` (sweep / backfill) retries later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import or_, select

from app.core.text import normalise_tokens
from app.database.session import Database
from app.domain.enums import ProcessingStatus
from app.integrations.jira_client import JiraClient
from app.llm.base import LLMUnavailable
from app.llm.gateway import LLMGateway
from app.llm.prompts import NO_RESOLUTION, RESOLUTION_SUMMARY_V2
from app.llm.redaction import redact_text
from app.models.support_request import SupportRequest
from app.repositories.knowledge_repository import KnowledgeRepository

logger = logging.getLogger("opsbridge.knowledge")

OPSBRIDGE_COMMENT_PREFIXES = ("Closed from Microsoft Teams", "Reopened from Microsoft Teams", "Escalated from ")
NON_REASONS = {"closed in jira"}
MAX_TEXT = 4000
MAX_ATTEMPTS = 3
COMMENTS_LIMIT = 5


@dataclass(frozen=True)
class CaptureResult:
    status: str  # CAPTURED | SKIPPED | FAILED | NOT_CLOSED
    entry_id: int | None = None
    reason: str | None = None


def is_meaningful(text: str) -> bool:
    """At least two content words (so 'fixed' / 'done' / 'n/a' don't become knowledge)."""
    return len(normalise_tokens(text)) >= 2


class KnowledgeService:
    def __init__(self, *, db: Database, jira: JiraClient, llm: LLMGateway, comments_limit: int = COMMENTS_LIMIT) -> None:
        self.db = db
        self.jira = jira
        self.llm = llm
        self.comments_limit = comments_limit

    # ---- capture ------------------------------------------------------------------------------------
    def capture_safely(self, request_id: int) -> CaptureResult:
        """Never raises: used from background hooks."""
        try:
            return self.capture(request_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("knowledge capture failed", extra={"request_id": request_id})
            self._mark(request_id, "FAILED", attempt=True)
            return CaptureResult("FAILED", reason=type(exc).__name__)

    def capture(self, request_id: int) -> CaptureResult:
        with self.db.session() as session:
            req = session.get(SupportRequest, request_id)
            if req is None or req.processing_status != ProcessingStatus.CLOSED or not req.jira_issue_key:
                return CaptureResult("NOT_CLOSED")
            session.expunge(req)

        notes, source = self._collect_resolution(req)
        if not is_meaningful(notes):
            logger.info("no meaningful resolution text; not added to knowledge base",
                        extra={"jira_key": req.jira_issue_key})
            self._mark(request_id, "SKIPPED", attempt=True)
            return CaptureResult("SKIPPED", reason="no meaningful resolution text")

        problem = redact_text("\n".join(p for p in (
            f"Summary: {req.issue_summary}" if req.issue_summary else "",
            f"Description: {req.issue_description}" if req.issue_description else "",
            f"Business impact: {req.business_impact}" if req.business_impact else "",
        ) if p))[:MAX_TEXT]
        resolution = redact_text(notes)[:MAX_TEXT]
        prompt_version = None
        if self.llm.chat_enabled:
            try:
                result = self.llm.generate(RESOLUTION_SUMMARY_V2,
                                           f"PROBLEM:\n{problem}\n\nRESOLUTION NOTES:\n{resolution}", max_tokens=300)
                if result.text.strip().strip(".").upper() == NO_RESOLUTION:
                    logger.info("LLM found no fix in the resolution notes; not added to knowledge base",
                                extra={"jira_key": req.jira_issue_key})
                    self._mark(request_id, "SKIPPED", attempt=True)
                    return CaptureResult("SKIPPED", reason="notes describe no fix")
                if is_meaningful(result.text):
                    resolution, source = redact_text(result.text)[:MAX_TEXT], "llm_summary"
                    prompt_version = RESOLUTION_SUMMARY_V2.version
            except LLMUnavailable:
                pass  # keep the raw notes

        with self.db.session() as session:
            entry = KnowledgeRepository(session).upsert_for_request(
                request_id,
                jira_issue_key=req.jira_issue_key, jira_issue_url=req.jira_issue_url,
                api_name=req.api_name or "", environment=req.environment or "", priority=req.priority,
                problem_text=problem, resolution_text=resolution, source=source, prompt_version=prompt_version,
                resolved_at=req.closed_at, embedding=None, embedding_model=None,
            )
            entry_id = entry.id
        self._embed(entry_id, problem, resolution)
        self._mark(request_id, "CAPTURED", attempt=True)
        logger.info("knowledge entry saved", extra={"jira_key": req.jira_issue_key, "source": source,
                                                    "entry_id": entry_id})
        return CaptureResult("CAPTURED", entry_id=entry_id)

    def _collect_resolution(self, req: SupportRequest) -> tuple[str, str]:
        parts: list[str] = []
        source = "close_reason"
        if req.close_reason and req.close_reason.strip().lower() not in NON_REASONS:
            parts.append(req.close_reason.strip())
        try:
            comments = self.jira.list_comments(req.jira_issue_key or "", self.comments_limit)
        except Exception as exc:  # noqa: BLE001 - comments are a bonus; never block on them
            logger.warning("could not read Jira comments", extra={"jira_key": req.jira_issue_key,
                                                                   "error": str(exc)[:200]})
            comments = []
        human = [c.strip() for c in reversed(comments)  # chronological
                 if c.strip() and not c.strip().startswith(OPSBRIDGE_COMMENT_PREFIXES)]
        if human:
            parts.extend(human)
            source = "jira_comment"
        return "\n".join(parts), source

    def _embed(self, entry_id: int, problem: str, resolution: str) -> bool:
        if not self.llm.embeddings_enabled:
            return False
        try:
            [vector] = self.llm.embed([f"{problem}\n{resolution}"])
        except LLMUnavailable:
            logger.info("embedding deferred; entry stays keyword-searchable", extra={"entry_id": entry_id})
            return False
        with self.db.session() as session:
            KnowledgeRepository(session).set_embedding(entry_id, vector, self.llm.embedding_model or "")
        return True

    def _mark(self, request_id: int, status: str, *, attempt: bool) -> None:
        with self.db.session() as session:
            req = session.get(SupportRequest, request_id)
            if req is not None:
                req.knowledge_status = status
                if attempt:
                    req.knowledge_attempts = (req.knowledge_attempts or 0) + 1

    # ---- sweep / backfill -------------------------------------------------------------------------------
    def pending_request_ids(self, limit: int = 25, *, include_skipped: bool = False) -> list[int]:
        """Closed tickets not yet captured (or failed, with attempts left)."""
        states = [SupportRequest.knowledge_status.is_(None),
                  (SupportRequest.knowledge_status == "FAILED") & (SupportRequest.knowledge_attempts < MAX_ATTEMPTS)]
        if include_skipped:
            states.append(SupportRequest.knowledge_status == "SKIPPED")
        with self.db.session() as session:
            return list(session.scalars(
                select(SupportRequest.id)
                .where(SupportRequest.processing_status == ProcessingStatus.CLOSED,
                       SupportRequest.jira_issue_key.is_not(None), or_(*states))
                .order_by(SupportRequest.id).limit(limit)
            ).all())

    def process_pending(self, limit: int = 25, *, include_skipped: bool = False) -> dict[str, int]:
        counts: dict[str, int] = {}
        for request_id in self.pending_request_ids(limit, include_skipped=include_skipped):
            status = self.capture_safely(request_id).status
            counts[status] = counts.get(status, 0) + 1
        return counts

    def embed_missing(self, limit: int = 100) -> int:
        if not self.llm.embeddings_enabled:
            return 0
        with self.db.session() as session:
            entries = [(e.id, e.problem_text, e.resolution_text)
                       for e in KnowledgeRepository(session).missing_embeddings(self.llm.embedding_model or "", limit)]
        return sum(1 for entry_id, problem, resolution in entries if self._embed(entry_id, problem, resolution))

    def sweep(self) -> None:
        """Periodic background job: capture pending closes, then fill in missing embeddings."""
        counts = self.process_pending()
        embedded = self.embed_missing(limit=25)
        if counts or embedded:
            logger.info("knowledge sweep", extra={"captured": counts, "embedded": embedded})
