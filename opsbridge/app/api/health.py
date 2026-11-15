from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.api.deps import get_container
from app.container import Container
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

    healthy = db_ok and worker != "stopped"
    body = {
        "status": "ok" if healthy else "degraded",
        "version": __version__,
        "environment": container.settings.app_env,
        "mock_mode": container.settings.mock_mode,
        "checks": {"database": "ok" if db_ok else "error", "worker": worker},
    }
    return JSONResponse(body, status_code=200 if healthy else 503)
