"""Per-request trace id: accept a safe inbound X-Request-ID or generate one, echo it back."""

from __future__ import annotations

import logging
import re
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import log_context, new_trace_id

logger = logging.getLogger("opsbridge.http")
_SAFE_ID = re.compile(r"^[A-Za-z0-9\-_.]{1,64}$")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        inbound = request.headers.get("x-request-id", "")
        trace_id = inbound if _SAFE_ID.match(inbound) else new_trace_id()
        request.state.trace_id = trace_id
        start = time.perf_counter()
        with log_context(trace_id=trace_id):
            response = await call_next(request)
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.info(
                "%s %s -> %s",
                request.method,
                request.url.path,  # path only: query strings may carry validation tokens
                response.status_code,
                extra={"duration_ms": duration_ms},
            )
        response.headers["X-Request-ID"] = trace_id
        return response
