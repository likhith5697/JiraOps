from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.deps import get_container
from app.container import Container
from app.core.errors import IntegrationError
from app.core.security import require_admin
from app.schemas.support_request import (
    ErrorResponse,
    SubscriptionActionResponse,
    SubscriptionCreateRequest,
    SubscriptionRenewRequest,
    SubscriptionResponse,
)

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"], dependencies=[Depends(require_admin)])
logger = logging.getLogger("opsbridge.api.subscriptions")


def _upstream_error(exc: IntegrationError) -> HTTPException:
    logger.error("Graph subscription operation failed", extra={"error": str(exc)})
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Microsoft Graph error: {exc}")


@router.get("", response_model=list[SubscriptionResponse], summary="List known Graph subscriptions")
def list_subscriptions(container: Container = Depends(get_container)) -> list[SubscriptionResponse]:
    return [SubscriptionResponse.model_validate(s) for s in container.subscriptions.list()]


@router.post(
    "/create",
    response_model=SubscriptionActionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": SubscriptionActionResponse}, 502: {"model": ErrorResponse}},
    summary="Create the channel-message subscription (idempotent unless force=true)",
)
def create_subscription(
    response: Response,
    body: SubscriptionCreateRequest | None = None,
    container: Container = Depends(get_container),
) -> SubscriptionActionResponse:
    try:
        outcome = container.subscriptions.create(force=bool(body and body.force))
    except IntegrationError as exc:
        raise _upstream_error(exc) from exc
    if outcome.action == "existing":
        response.status_code = status.HTTP_200_OK
    return SubscriptionActionResponse(action=outcome.action,
                                      subscription=SubscriptionResponse.model_validate(outcome.subscription))


@router.post(
    "/renew",
    response_model=list[SubscriptionActionResponse],
    responses={404: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
    summary="Renew one or all active subscriptions (recreates if Graph no longer has it)",
)
def renew_subscriptions(
    body: SubscriptionRenewRequest | None = None,
    container: Container = Depends(get_container),
) -> list[SubscriptionActionResponse]:
    subscription_id = body.subscription_id if body else None
    try:
        outcomes = container.subscriptions.renew(subscription_id)
    except IntegrationError as exc:
        raise _upstream_error(exc) from exc
    if subscription_id and not outcomes:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return [
        SubscriptionActionResponse(action=o.action, subscription=SubscriptionResponse.model_validate(o.subscription))
        for o in outcomes
    ]
