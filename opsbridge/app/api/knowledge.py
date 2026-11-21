"""Admin API for the knowledge base, `ask` history and deflection metrics."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict

from app.api.deps import get_container
from app.container import Container
from app.core.security import require_admin
from app.domain.enums import AskStatus
from app.repositories.ask_repository import AskRepository
from app.repositories.knowledge_repository import KnowledgeRepository
from app.schemas.support_request import ErrorResponse, Page

router = APIRouter(dependencies=[Depends(require_admin)])


class KnowledgeEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    support_request_id: int
    jira_issue_key: str
    jira_issue_url: str | None
    api_name: str
    environment: str
    priority: str | None
    problem_text: str
    resolution_text: str
    source: str
    prompt_version: str | None
    embedding_model: str | None
    has_embedding: bool = False
    helpful_count: int
    not_helpful_count: int
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AskQueryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    teams_message_id: str
    sender_id: str | None
    sender_name: str | None
    question: str
    api_hint: str | None
    environment_hint: str | None
    retrieved_keys: list[str] | None
    top_score: float | None
    answer_text: str | None
    mode: str | None
    prompt_version: str | None
    feedback: str | None
    status: AskStatus
    error_message: str | None
    created_at: datetime
    answered_at: datetime | None


class DeflectionMetrics(BaseModel):
    total_asks: int
    answered: int
    answered_pct: float
    llm_answers: int
    keyword_answers: int
    no_match: int
    rated: int
    helpful: int
    helpful_pct: float
    tickets_avoided: int
    window_hours: int


def _entry(e) -> KnowledgeEntryResponse:  # noqa: ANN001
    out = KnowledgeEntryResponse.model_validate(e)
    out.has_embedding = bool(e.embedding)
    return out


@router.get("/knowledge", response_model=Page[KnowledgeEntryResponse], tags=["knowledge base"],
            summary="Knowledge entries captured from resolved tickets (newest first)")
def list_knowledge(
    api_name: str | None = Query(default=None, max_length=128),
    environment: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> Page[KnowledgeEntryResponse]:
    with container.db.session() as session:
        items, total = KnowledgeRepository(session).list(api_name=api_name, environment=environment,
                                                         limit=limit, offset=offset)
        return Page[KnowledgeEntryResponse](items=[_entry(e) for e in items], total=total, limit=limit, offset=offset)


@router.get("/knowledge/{entry_id}", response_model=KnowledgeEntryResponse, tags=["knowledge base"],
            responses={404: {"model": ErrorResponse}})
def get_knowledge(entry_id: int = Path(ge=1), container: Container = Depends(get_container)) -> KnowledgeEntryResponse:
    with container.db.session() as session:
        entry = KnowledgeRepository(session).get(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Knowledge entry not found")
        return _entry(entry)


@router.delete("/knowledge/{entry_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["knowledge base"],
               responses={404: {"model": ErrorResponse}},
               summary="Remove an entry (e.g. wrong or outdated fix). It is not re-created automatically.")
def delete_knowledge(entry_id: int = Path(ge=1), container: Container = Depends(get_container)) -> Response:
    with container.db.session() as session:
        if not KnowledgeRepository(session).delete(entry_id):
            raise HTTPException(status_code=404, detail="Knowledge entry not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/ask-queries", response_model=Page[AskQueryResponse], tags=["knowledge base"],
            summary="`ask` questions and the answers given (newest first)")
def list_ask_queries(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> Page[AskQueryResponse]:
    with container.db.session() as session:
        items, total = AskRepository(session).list(limit=limit, offset=offset)
        return Page[AskQueryResponse](items=[AskQueryResponse.model_validate(a) for a in items], total=total,
                                      limit=limit, offset=offset)


@router.get("/metrics/deflection", response_model=DeflectionMetrics, tags=["metrics"],
            summary="Self-service effectiveness: answered %, helpful %, tickets avoided")
def deflection(container: Container = Depends(get_container)) -> DeflectionMetrics:
    return DeflectionMetrics(**container.asks.deflection_metrics())
