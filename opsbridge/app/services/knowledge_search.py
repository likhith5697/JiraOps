"""Hybrid retrieval over the knowledge base (keyword + semantic, with domain boosts).

score = 0.6 * semantic + 0.4 * keyword     (keyword only for entries/questions without embeddings)
      + 0.15 same API + 0.05 same environment + 0.05 same HTTP status code
      + up to 0.05 recency (linear over 90 days)
      + up to 0.05 from `helpful` feedback, minus up to 0.15 from `not helpful` feedback
clamped to 0..1; results below ASK_MIN_SCORE are dropped; top ASK_TOP_K returned.

The keyword score is absolute, not just relative rank: 70% term coverage (how many of the
question's content words appear in the entry, stemmed) + 30% bm25 rank relative to the best hit.
A purely relative score would always give the top hit 1.0 and make the threshold meaningless.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

from app.core.text import normalise_tokens
from app.core.time import utcnow
from app.database.session import Database
from app.llm.base import LLMUnavailable
from app.llm.gateway import LLMGateway
from app.llm.redaction import redact_text
from app.models.knowledge import KnowledgeEntry
from app.repositories.knowledge_repository import KnowledgeRepository, query_terms

logger = logging.getLogger("opsbridge.knowledge_search")

SEMANTIC_WEIGHT, KEYWORD_WEIGHT = 0.6, 0.4
API_BOOST, ENV_BOOST, HTTP_BOOST, RECENCY_BOOST = 0.15, 0.05, 0.05, 0.05
RECENCY_DAYS = 90
_ENV_ALIASES = {"prod": "PROD", "production": "PROD", "prd": "PROD", "uat": "UAT", "qa": "QA"}
_HTTP = re.compile(r"\b([45]\d\d)\b")


@dataclass(frozen=True)
class SearchHints:
    api_name: str | None = None
    environment: str | None = None
    http_codes: frozenset[str] = frozenset()


@dataclass
class SearchResult:
    entry: KnowledgeEntry
    score: float
    keyword: float = 0.0
    semantic: float | None = None
    boosts: dict[str, float] = field(default_factory=dict)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class KnowledgeSearch:
    def __init__(self, *, db: Database, llm: LLMGateway, min_score: float = 0.35, top_k: int = 3) -> None:
        self.db = db
        self.llm = llm
        self.min_score = min_score
        self.top_k = top_k

    def extract_hints(self, question: str, known_api_names: list[str]) -> SearchHints:
        api = next((name for name in sorted(known_api_names, key=len, reverse=True)
                    if re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", question, re.IGNORECASE)), None)
        env = next((_ENV_ALIASES[w] for w in re.findall(r"[a-z]+", question.lower()) if w in _ENV_ALIASES), None)
        return SearchHints(api_name=api, environment=env, http_codes=frozenset(_HTTP.findall(question)))

    def search(self, question: str) -> tuple[SearchHints, list[SearchResult]]:
        question = redact_text(question)
        with self.db.session() as session:
            repo = KnowledgeRepository(session)
            hints = self.extract_hints(question, repo.known_api_names())
            keyword_hits = repo.keyword_candidates(query_terms(question), limit=50)
            vectors = repo.with_embeddings(self.llm.embedding_model) if self.llm.embeddings_enabled else []
            candidate_ids = {h.entry_id for h in keyword_hits} | {entry_id for entry_id, _ in vectors}
            entries = {e.id: e for e in repo.get_many(sorted(candidate_ids))}
            session.expunge_all()

        keyword = self._keyword_scores(question, keyword_hits, entries)
        semantic = self._semantic_scores(question, vectors)

        results = []
        for entry_id, entry in entries.items():
            kw = keyword.get(entry_id, 0.0)
            sem = semantic.get(entry_id)
            base = SEMANTIC_WEIGHT * sem + KEYWORD_WEIGHT * kw if sem is not None else kw
            if base <= 0.05:
                continue  # boosts alone never make an unrelated fix relevant
            boosts = self._boosts(entry, hints)
            # Cap positives first, then apply penalties: otherwise a strong match (already >= 1.0)
            # would hide `not helpful` feedback completely.
            positive = sum(v for v in boosts.values() if v > 0)
            negative = sum(v for v in boosts.values() if v < 0)
            score = max(0.0, min(1.0, base + positive) + negative)
            results.append(SearchResult(entry, round(score, 4), round(kw, 4),
                                        None if sem is None else round(sem, 4), boosts))
        results.sort(key=lambda r: r.score, reverse=True)
        return hints, [r for r in results if r.score >= self.min_score][: self.top_k]

    # ---- components ------------------------------------------------------------------------------------
    @staticmethod
    def _keyword_scores(question: str, hits, entries: dict[int, KnowledgeEntry]) -> dict[int, float]:  # noqa: ANN001
        wanted = set(normalise_tokens(question))
        if not hits or not wanted:
            return {}
        best = min(h.bm25 for h in hits)  # bm25: lower (more negative) is better
        scores = {}
        for hit in hits:
            entry = entries.get(hit.entry_id)
            if entry is None:
                continue
            have = set(normalise_tokens(f"{entry.api_name} {entry.environment} {entry.problem_text} "
                                        f"{entry.resolution_text}"))
            coverage = len(wanted & have) / len(wanted)
            relative = (hit.bm25 / best) if best < 0 else 0.0
            scores[hit.entry_id] = 0.7 * coverage + 0.3 * max(0.0, min(1.0, relative))
        return scores

    def _semantic_scores(self, question: str, vectors: list[tuple[int, list[float]]]) -> dict[int, float]:
        if not vectors:
            return {}
        try:
            [qvec] = self.llm.embed([question])
        except LLMUnavailable:
            return {}  # keyword-only for this question
        return {entry_id: max(0.0, cosine(qvec, vec)) for entry_id, vec in vectors}

    @staticmethod
    def _boosts(entry: KnowledgeEntry, hints: SearchHints) -> dict[str, float]:
        boosts: dict[str, float] = {}
        if hints.api_name and entry.api_name.lower() == hints.api_name.lower():
            boosts["api"] = API_BOOST
        if hints.environment and entry.environment.upper() == hints.environment:
            boosts["environment"] = ENV_BOOST
        if hints.http_codes and hints.http_codes & set(_HTTP.findall(f"{entry.problem_text} {entry.resolution_text}")):
            boosts["http_status"] = HTTP_BOOST
        if entry.resolved_at:
            age_days = (utcnow() - entry.resolved_at).total_seconds() / 86400
            if age_days < RECENCY_DAYS:
                boosts["recency"] = round(RECENCY_BOOST * (1 - age_days / RECENCY_DAYS), 4)
        if entry.helpful_count:
            boosts["helpful"] = min(0.05, 0.01 * entry.helpful_count)
        if entry.not_helpful_count:
            boosts["not_helpful"] = -min(0.15, 0.05 * entry.not_helpful_count)
        return boosts
