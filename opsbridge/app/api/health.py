from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.api.deps import get_container
from app.container import Container
from app.core.time import utcnow
from app.domain.enums import SubscriptionStatus
from app.workers.background import BackgroundWorker

router = APIRouter(tags=["health"])
logger = logging.getLogger("opsbridge.health")


@router.get("/health", summary="Liveness and dependency status")
def health(container: Container = Depends(get_container)) -> JSONResponse:
    db_ok = True
    try:
        with container.db.session() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("database health check failed")
        db_ok = False

    dispatcher = container.dispatcher
    if isinstance(dispatcher, BackgroundWorker):
        worker = "running" if dispatcher.running else "stopped"
    else:
        worker = "inline"

    # Teams intake depends on a live Graph subscription. An expired one silently stops all new
    # requests (e.g. after the host was off past the renewal window), so surface it here.
    subscription, expires_at = "none", None
    if db_ok:
        active = [s for s in container.subscriptions.list() if s.status == SubscriptionStatus.ACTIVE]
        if active:
            latest = max(active, key=lambda s: s.expiration_at)
            expires_at = latest.expiration_at.isoformat()
            subscription = "active" if latest.expiration_at > utcnow() else "expired"

    # Live mode without an active subscription = no Teams intake at all.
    intake_ok = container.settings.mock_mode or subscription == "active"
    healthy = db_ok and worker != "stopped" and intake_ok
    body = {
        "status": "ok" if healthy else "degraded",
        "version": __version__,
        "environment": container.settings.app_env,
        "mock_mode": container.settings.mock_mode,
        "checks": {"database": "ok" if db_ok else "error", "worker": worker,
                   "teams_subscription": subscription, "teams_subscription_expires_at": expires_at},
    }
    return JSONResponse(body, status_code=200 if healthy else 503)
