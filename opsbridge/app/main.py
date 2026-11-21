"""FastAPI application factory.

Run with:  uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.api import dev, health, knowledge, subscriptions, support_requests, webhooks
from app.config import Settings, get_settings
from app.container import Container, build_container
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.workers.background import BackgroundWorker

logger = logging.getLogger("opsbridge")


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = (settings or (container.settings if container else get_settings())).check()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        c = container or build_container(settings)
        c.db.create_all()
        app.state.container = c
        if isinstance(c.dispatcher, BackgroundWorker):
            c.dispatcher.start()
        logger.info(
            "OpsBridge started",
            extra={"env": settings.app_env, "mock_mode": settings.mock_mode, "version": __version__},
        )
        try:
            yield
        finally:
            c.close()
            logger.info("OpsBridge stopped")

    app = FastAPI(
        title="OpsBridge",
        version=__version__,
        description="Turns Microsoft Teams support-channel messages into tracked Jira issues.",
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
    )
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return JSONResponse(
            {"detail": "Internal server error", "trace_id": getattr(request.state, "trace_id", None)},
            status_code=500,
        )

    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(subscriptions.router)
    app.include_router(support_requests.router)
    app.include_router(knowledge.router)
    if settings.mock_mode:
        app.include_router(dev.router)
    return app

