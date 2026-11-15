"""Microsoft Graph webhook endpoints. These must answer within a few seconds:
they only verify, persist and enqueue; all downstream work happens in the background worker.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_container
from app.container import Container
from app.schemas.graph import ChangeNotificationCollection
from app.services.ingestion_service import client_state_matches

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger("opsbridge.webhooks")

MAX_BODY_BYTES = 1_000_000
MAX_VALIDATION_TOKEN = 2048


def _validation_response(token: str) -> Response:
    # Graph subscription validation: echo the token as text/plain within 10 seconds.
    if len(token) > MAX_VALIDATION_TOKEN:
        return PlainTextResponse("validation token too long", status_code=400)
    logger.info("answered Graph subscription validation request")
    return PlainTextResponse(token, status_code=200)


async def _read_notifications(request: Request) -> ChangeNotificationCollection | Response:
    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "payload too large"}, status_code=413)
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "payload too large"}, status_code=413)
    try:
        return ChangeNotificationCollection.model_validate(json.loads(raw or b"{}"))
    except (ValueError, ValidationError):
        logger.warning("malformed Graph notification payload")
        return JSONResponse({"detail": "invalid notification payload"}, status_code=400)


@router.post(
    "/teams",
    summary="Graph change notifications for Teams channel messages",
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"description": "Subscription validation token echoed"}, 401: {"description": "Invalid clientState"}},
)
async def teams_notifications(
    request: Request,
    validation_token: str | None = Query(default=None, alias="validationToken"),
    container: Container = Depends(get_container),
) -> Response:
    if validation_token is not None:
        return _validation_response(validation_token)

    payload = await _read_notifications(request)
    if isinstance(payload, Response):
        return payload

    # The DB insert is synchronous; run it off the event loop.
    summary = await run_in_threadpool(
        container.ingestion.ingest, payload.value, trace_id=getattr(request.state, "trace_id", None)
    )
    if summary.all_rejected:
        return JSONResponse({"detail": "invalid clientState"}, status_code=401)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/teams/lifecycle",
    summary="Graph lifecycle notifications (reauthorizationRequired, subscriptionRemoved, missed)",
    status_code=status.HTTP_202_ACCEPTED,
)
async def teams_lifecycle(
    request: Request,
    validation_token: str | None = Query(default=None, alias="validationToken"),
    container: Container = Depends(get_container),
) -> Response:
    if validation_token is not None:
        return _validation_response(validation_token)

    payload = await _read_notifications(request)
    if isinstance(payload, Response):
        return payload

    accepted = 0
    for event in payload.value:
        if not client_state_matches(event.clientState, container.settings.effective_client_state):
            logger.warning("discarding lifecycle event with invalid clientState")
            continue
        accepted += 1
        kind = event.lifecycleEvent
        logger.info("lifecycle event received", extra={"event": kind, "subscription_id": event.subscriptionId})
        if kind == "reauthorizationRequired":
            container.dispatcher.submit(container.subscriptions.renew, event.subscriptionId)
        elif kind == "subscriptionRemoved":
            container.dispatcher.submit(container.subscriptions.handle_removed, event.subscriptionId)
        elif kind == "missed":
            container.dispatcher.submit(container.ingestion.catch_up, container.graph)
        else:
            logger.warning("unknown lifecycle event", extra={"event": kind})
    if payload.value and accepted == 0:
        return JSONResponse({"detail": "invalid clientState"}, status_code=401)
    return Response(status_code=status.HTTP_202_ACCEPTED)


# ------------------------------------------------------------------------------------------------
# Jira -> Teams status sync (optional; polling works without it)
# ------------------------------------------------------------------------------------------------
def _jira_signature_ok(secret: str, body: bytes, signature_header: str | None, query_secret: str | None) -> bool:
    """Accept a Jira-signed request (X-Hub-Signature: sha256=<hmac>) or a ?secret= value
    (Jira Automation "Send web request" cannot sign). Constant-time comparisons only."""
    if signature_header and signature_header.startswith("sha256="):
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature_header[7:].strip().lower(), expected)
    if query_secret:
        return hmac.compare_digest(query_secret.encode(), secret.encode())
    return False


@router.post(
    "/jira",
    summary="Jira issue webhooks: post status/assignee changes in the Teams thread",
    status_code=status.HTTP_202_ACCEPTED,
    responses={401: {"description": "Bad signature/secret"}, 404: {"description": "JIRA_WEBHOOK_SECRET not set"}},
)
async def jira_webhook(
    request: Request,
    secret: str | None = Query(default=None, max_length=256),
    container: Container = Depends(get_container),
) -> Response:
    configured = container.settings.jira_webhook_secret.get_secret_value()
    if not configured:
        return JSONResponse({"detail": "Jira webhook is not configured"}, status_code=404)
    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "payload too large"}, status_code=413)
    raw = await request.body()
    if not _jira_signature_ok(configured, raw, request.headers.get("x-hub-signature"), secret):
        logger.warning("rejected Jira webhook with invalid signature/secret")
        return JSONResponse({"detail": "invalid signature"}, status_code=401)
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return JSONResponse({"detail": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"detail": "invalid payload"}, status_code=400)
    await run_in_threadpool(container.jira_sync.handle_webhook, payload,
                            trace_id=getattr(request.state, "trace_id", None))
    return Response(status_code=status.HTTP_202_ACCEPTED)
