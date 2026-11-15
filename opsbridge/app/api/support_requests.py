from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app.api.deps import get_container
from app.container import Container
from app.core.security import require_admin
from app.domain.enums import ProcessingStatus
from app.schemas.support_request import (
    ErrorResponse,
    Page,
    SupportRequestDetail,
    SupportRequestSummary,
    TeamsCommandResponse,
    TeamsNotificationResponse,
)
from app.services.support_request_service import RetryNotAllowed

router = APIRouter(prefix="/support-requests", tags=["support requests"], dependencies=[Depends(require_admin)])


@router.get("", response_model=Page[SupportRequestSummary], summary="List support requests (newest first)")
def list_support_requests(
    status_filter: ProcessingStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    container: Container = Depends(get_container),
) -> Page[SupportRequestSummary]:
    items, total = container.support_requests.list(status=status_filter, limit=limit, offset=offset)
    return Page[SupportRequestSummary](
        items=[SupportRequestSummary.model_validate(i) for i in items], total=total, limit=limit, offset=offset
    )


@router.get(
    "/{request_id}",
    response_model=SupportRequestDetail,
    responses={404: {"model": ErrorResponse}},
    summary="Get one support request with full processing details",
)
def get_support_request(
    request_id: int = Path(ge=1), container: Container = Depends(get_container)
) -> SupportRequestDetail:
    rec = container.support_requests.get(request_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Support request not found")
    return _detail(container, rec)


@router.post(
    "/{request_id}/retry",
    response_model=SupportRequestDetail,
    status_code=status.HTTP_202_ACCEPTED,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Retry a FAILED request (or expedite a scheduled one). Never duplicates Jira issues.",
)
def retry_support_request(
    request_id: int = Path(ge=1), container: Container = Depends(get_container)
) -> SupportRequestDetail:
    try:
        rec = container.support_requests.retry(request_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Support request not found") from None
    except RetryNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _detail(container, rec)


def _detail(container: Container, rec) -> SupportRequestDetail:  # noqa: ANN001
    detail = SupportRequestDetail.model_validate(rec)
    detail.commands = [TeamsCommandResponse.model_validate(c) for c in container.support_requests.commands_for(rec.id)]
    detail.notifications = [TeamsNotificationResponse.model_validate(n)
                            for n in container.support_requests.notifications_for(rec.id)]
    return detail
