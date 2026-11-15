"""Integration error taxonomy.

The distinction between these classes drives every retry/idempotency decision:

* TransientError  - the remote side definitely did NOT apply the request; safe to resend.
* AmbiguousError  - the request MAY have been applied (timeout after send, 5xx); reconcile before resending.
* PermanentError  - retrying the same request will not help (bad request, auth, not found).
"""

from __future__ import annotations


class IntegrationError(Exception):
    def __init__(
        self,
        message: str,
        *,
        service: str,
        status_code: int | None = None,
        retry_after: float | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.service = service
        self.status_code = status_code
        self.retry_after = retry_after
        self.details = details or {}

    def __str__(self) -> str:
        code = f" (HTTP {self.status_code})" if self.status_code else ""
        return f"{self.service}: {self.args[0]}{code}"


class TransientError(IntegrationError):
    pass


class AmbiguousError(IntegrationError):
    pass


class PermanentError(IntegrationError):
    pass


class NotFoundError(PermanentError):
    pass


class ConfigurationError(Exception):
    pass
