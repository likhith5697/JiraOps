"""Admin API authentication (static API key, constant-time comparison)."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Admin API key")


def require_admin(request: Request, api_key: str | None = Security(api_key_header)) -> None:
    # Use the settings the running app was built with (not a global), so injected configs are honoured.
    expected = request.app.state.container.settings.admin_api_key.get_secret_value()
    if not api_key or not expected or not secrets.compare_digest(api_key.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "APIKey"},
        )
