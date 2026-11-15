"""Shared HTTP execution: timeouts, error classification and bounded in-call retries.

Classification (see app/core/errors.py):

| Failure                                   | idempotent call | non-idempotent call (e.g. create issue) |
|-------------------------------------------|-----------------|-----------------------------------------|
| connect error / connect timeout / pool    | Transient       | Transient (request never left)          |
| read/write timeout, connection dropped    | Transient       | **Ambiguous** (may have been applied)   |
| 429                                       | Transient       | Transient (rejected before processing)  |
| 5xx                                       | Transient       | **Ambiguous**                           |
| 404                                       | NotFound        | NotFound                                |
| other 4xx                                 | Permanent       | Permanent                               |

Only Transient errors are retried in-call. Ambiguous errors propagate so the caller can reconcile.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from app.core.errors import (
    AmbiguousError,
    IntegrationError,
    NotFoundError,
    PermanentError,
    TransientError,
)
from app.core.retry import exponential_backoff

logger = logging.getLogger("opsbridge.http_client")

MAX_RETRY_AFTER_SECONDS = 30.0


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _error_summary(response: httpx.Response) -> tuple[str, dict]:
    """Extract a short, safe error description from Jira / Graph error bodies."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or response.reason_phrase or "error")[:300], {}
    if isinstance(body, dict):
        if isinstance(body.get("error"), dict):  # Microsoft Graph / Entra
            err = body["error"]
            return f"{err.get('code', 'error')}: {err.get('message', '')}"[:300], {"code": err.get("code")}
        if "error_description" in body:  # OAuth token endpoint
            return f"{body.get('error')}: {body['error_description']}"[:300], {"code": body.get("error")}
        messages = list(body.get("errorMessages") or [])  # Jira
        field_errors = body.get("errors") or {}
        if messages or field_errors:
            parts = messages + [f"{k}: {v}" for k, v in field_errors.items()]
            return "; ".join(parts)[:300], {"field_errors": field_errors}
    return str(body)[:300], {}


def classify_response(response: httpx.Response, *, service: str, idempotent: bool) -> IntegrationError:
    status = response.status_code
    summary, details = _error_summary(response)
    kwargs: dict[str, Any] = {"service": service, "status_code": status, "details": details}
    if status == 429:
        return TransientError(f"rate limited: {summary}", retry_after=_retry_after(response), **kwargs)
    if status >= 500:
        cls = TransientError if idempotent else AmbiguousError
        return cls(f"server error: {summary}", retry_after=_retry_after(response), **kwargs)
    if status == 404:
        return NotFoundError(f"not found: {summary}", **kwargs)
    return PermanentError(f"request rejected: {summary}", **kwargs)


def classify_exception(exc: httpx.HTTPError, *, service: str, idempotent: bool) -> IntegrationError:
    name = type(exc).__name__
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return TransientError(f"connection failed ({name})", service=service)
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        cls = TransientError if idempotent else AmbiguousError
        return cls(f"no response received ({name})", service=service)
    return TransientError(f"transport error ({name})", service=service)


def send_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    service: str,
    idempotent: bool,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    last_error: IntegrationError | None = None
    for attempt in range(max_attempts):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last_error = classify_exception(exc, service=service, idempotent=idempotent)
        else:
            if response.is_success:
                return response
            last_error = classify_response(response, service=service, idempotent=idempotent)

        if not isinstance(last_error, TransientError) or attempt == max_attempts - 1:
            raise last_error
        delay = exponential_backoff(attempt, base=base_delay, cap=10.0)
        if last_error.retry_after is not None:
            delay = min(MAX_RETRY_AFTER_SECONDS, max(delay, last_error.retry_after))
        logger.warning(
            "%s %s transient failure, retrying",
            service,
            method,
            extra={"attempt": attempt + 1, "delay_s": round(delay, 2), "error": str(last_error)},
        )
        sleep(delay)
    raise last_error  # pragma: no cover - loop always returns or raises
