"""Knowledge base persistence + search index.

POC backend: SQLite. Keyword search uses an FTS5 virtual table (`knowledge_fts`, porter stemming)
kept in sync by triggers, ranked with bm25. If FTS5 is unavailable (or the database is not SQLite),
a portable Python fallback is used. Semantic vectors are stored as JSON and compared in Python.

PostgreSQL path (no service changes): implement `keyword_candidates` with a tsvector column +
`ts_rank`, and `semantic candidates` with pgvector (`embedding <=> :q`). See docs/design-decisions.md.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.text import STOPWORDS, normalise_tokens
from app.models.knowledge import KnowledgeEntry

logger = logging.getLogger("opsbridge.knowledge")

_FTS_FLAG = "_opsbridge_fts5_available"
_WORD = re.compile(r"[a-z0-9]+")

_FTS_DDL = [
    "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5("
    "api_name, environment, problem_text, resolution_text, "
    "content='knowledge_entries', content_rowid='id', tokenize='porter unicode61')",
    "CREATE TRIGGER IF NOT EXISTS knowledge_fts_ai AFTER INSERT ON knowledge_entries BEGIN "
    "INSERT INTO knowledge_fts(rowid, api_name, environment, problem_text, resolution_text) "
    "VALUES (new.id, new.api_name, new.environment, new.problem_text, new.resolution_text); END",
    "CREATE TRIGGER IF NOT EXISTS knowledge_fts_ad AFTER DELETE ON knowledge_entries BEGIN "
    "INSERT INTO knowledge_fts(knowledge_fts, rowid, api_name, environment, problem_text, resolution_text) "
    "VALUES ('delete', old.id, old.api_name, old.environment, old.problem_text, old.resolution_text); END",
    "CREATE TRIGGER IF NOT EXISTS knowledge_fts_au AFTER UPDATE OF api_name, environment, problem_text, "
    "resolution_text ON knowledge_entries BEGIN "
    "INSERT INTO knowledge_fts(knowledge_fts, rowid, api_name, environment, problem_text, resolution_text) "
    "VALUES ('delete', old.id, old.api_name, old.environment, old.problem_text, old.resolution_text); "
    "INSERT INTO knowledge_fts(rowid, api_name, environment, problem_text, resolution_text) "
    "VALUES (new.id, new.api_name, new.environment, new.problem_text, new.resolution_text); END",
]


def ensure_search_index(engine: Engine) -> bool:
    """Create the FTS5 index + sync triggers if possible. Returns whether FTS5 is available."""
    available = False
    if engine.dialect.name == "sqlite":
        try:
            with engine.begin() as conn:
                existed = conn.execute(text("SELECT 1 FROM sqlite_master WHERE name='knowledge_fts'")).first()
                for ddl in _FTS_DDL:
                    conn.execute(text(ddl))
                if not existed:  # index rows that existed before the index did
                    conn.execute(text("INSERT INTO knowledge_fts(knowledge_fts) VALUES ('rebuild')"))
            available = True
        except OperationalError as exc:  # e.g. "no such module: fts5"
            logger.warning("SQLite FTS5 unavailable; using Python keyword search", extra={"error": str(exc)[:200]})
    setattr(engine, _FTS_FLAG, available)
    return available


@dataclass(frozen=True)
class KeywordHit:
    entry_id: int
    bm25: float  # FTS5 bm25 (lower = better); fallback uses -matched_terms


def query_terms(question: str) -> list[str]:
    seen: dict[str, None] = {}
    for w in _WORD.findall(question.lower()):
        if len(w) >= 2 and w not in STOPWORDS:
            seen.setdefault(w, None)
    return list(seen)[:24]


class KnowledgeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @property
    def fts_available(self) -> bool:
        return bool(getattr(self.session.get_bind(), _FTS_FLAG, False))

    # ---- writes ----------------------------------------------------------------------------------
    def upsert_for_request(self, support_request_id: int, **values: object) -> KnowledgeEntry:
        entry = self.session.scalar(select(KnowledgeEntry).where(KnowledgeEntry.support_request_id == support_request_id))
        if entry is None:
            entry = KnowledgeEntry(support_request_id=support_request_id, **values)
            self.session.add(entry)
        else:
            for key, value in values.items():
                setattr(entry, key, value)
        self.session.flush()
        return entry

    def set_embedding(self, entry_id: int, vector: list[float], model: str) -> None:
        self.session.execute(update(KnowledgeEntry).where(KnowledgeEntry.id == entry_id)
                             .values(embedding=json.dumps(vector), embedding_model=model)
                             .execution_options(synchronize_session=False))

    def adjust_feedback(self, entry_ids: list[int], *, helpful: bool) -> None:
        if not entry_ids:
            return
        column = KnowledgeEntry.helpful_count if helpful else KnowledgeEntry.not_helpful_count
        self.session.execute(update(KnowledgeEntry).where(KnowledgeEntry.id.in_(entry_ids))
                             .values({column: column + 1}).execution_options(synchronize_session=False))

    def delete(self, entry_id: int) -> bool:
        entry = self.session.get(KnowledgeEntry, entry_id)
        if entry is None:
            return False
        self.session.delete(entry)
        return True

    # ---- reads -------------------------------------------------------------------------------------
    def get(self, entry_id: int) -> KnowledgeEntry | None:
        return self.session.get(KnowledgeEntry, entry_id)

    def get_many(self, entry_ids: list[int]) -> list[KnowledgeEntry]:
        if not entry_ids:
            return []
        return list(self.session.scalars(select(KnowledgeEntry).where(KnowledgeEntry.id.in_(entry_ids))).all())

    def list(self, *, api_name: str | None = None, environment: str | None = None, limit: int = 20,
             offset: int = 0) -> tuple[list[KnowledgeEntry], int]:
        query = select(KnowledgeEntry)
        if api_name:
            query = query.where(func.lower(KnowledgeEntry.api_name) == api_name.lower())
        if environment:
            query = query.where(func.upper(KnowledgeEntry.environment) == environment.upper())
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        items = self.session.scalars(query.order_by(KnowledgeEntry.id.desc()).limit(limit).offset(offset)).all()
        return list(items), total

    def known_api_names(self) -> list[str]:
        return [n for n in self.session.scalars(select(KnowledgeEntry.api_name).distinct()).all() if n]

    def with_embeddings(self, model: str) -> list[tuple[int, list[float]]]:
        rows = self.session.execute(select(KnowledgeEntry.id, KnowledgeEntry.embedding)
                                    .where(KnowledgeEntry.embedding.is_not(None),
                                           KnowledgeEntry.embedding_model == model)).all()
        return [(entry_id, json.loads(vector)) for entry_id, vector in rows]

    def missing_embeddings(self, model: str, limit: int = 100) -> list[KnowledgeEntry]:
        return list(self.session.scalars(
            select(KnowledgeEntry)
            .where((KnowledgeEntry.embedding.is_(None)) | (KnowledgeEntry.embedding_model != model))
            .limit(limit)
        ).all())

    def resolved_since(self, since: datetime) -> int:
        return self.session.scalar(select(func.count()).select_from(KnowledgeEntry)
                                   .where(KnowledgeEntry.resolved_at >= since)) or 0

    # ---- keyword search ---------------------------------------------------------------------------------
    def keyword_candidates(self, terms: list[str], limit: int = 50) -> list[KeywordHit]:
        if not terms:
            return []
        if self.fts_available:
            match = " OR ".join(f'"{t}"' for t in terms)  # terms are [a-z0-9]+ only: safe to quote
            rows = self.session.execute(
                text("SELECT rowid, bm25(knowledge_fts) AS score FROM knowledge_fts "
                     "WHERE knowledge_fts MATCH :q ORDER BY score LIMIT :n"),
                {"q": match, "n": limit},
            ).all()
            return [KeywordHit(int(r[0]), float(r[1])) for r in rows]
        # Portable fallback: count matched (stemmed) terms per entry.
        wanted = set(normalise_tokens(" ".join(terms)))
        hits = []
        for entry in self.session.scalars(select(KnowledgeEntry)).all():
            tokens = set(normalise_tokens(f"{entry.api_name} {entry.environment} {entry.problem_text} "
                                          f"{entry.resolution_text}"))
            matched = len(wanted & tokens)
            if matched:
                hits.append(KeywordHit(entry.id, -float(matched)))
        return sorted(hits, key=lambda h: h.bm25)[:limit]
