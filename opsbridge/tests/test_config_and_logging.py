from __future__ import annotations

import logging

import pytest

from app.config import Settings
from app.core.errors import ConfigurationError
from app.core.logging import JsonFormatter, log_context, redact


def test_mock_mode_needs_no_credentials():
    s = Settings(_env_file=None)
    assert s.mock_mode and s.effective_team_id and s.effective_client_state


def test_live_mode_lists_missing_settings_by_name_only():
    with pytest.raises(ConfigurationError) as exc:
        Settings(_env_file=None, mock_mode=False, jira_api_token="super-secret-value").check()
    message = str(exc.value)
    assert "MICROSOFT_CLIENT_SECRET" in message and "JIRA_BASE_URL" in message
    assert "JIRA_API_TOKEN" not in message  # provided
    assert "super-secret-value" not in message


def test_production_requires_strong_admin_key_and_live_mode():
    with pytest.raises(ConfigurationError, match="ADMIN_API_KEY"):
        Settings(_env_file=None, app_env="production").check()
    with pytest.raises(ConfigurationError, match="MOCK_MODE"):
        Settings(_env_file=None, app_env="production", admin_api_key="x" * 32).check()


def test_list_and_map_settings_parse_from_env(monkeypatch):
    monkeypatch.setenv("JIRA_EXTRA_LABELS", "opsbridge, support")
    monkeypatch.setenv("JIRA_PRIORITY_MAP", '{"P1": "Blocker"}')
    s = Settings(_env_file=None)
    assert s.jira_extra_labels == ["opsbridge", "support"]
    assert s.jira_priority_map == {"P1": "Blocker"}


def test_secrets_are_not_exposed_in_repr():
    s = Settings(_env_file=None, microsoft_client_secret="abc123", jira_api_token="tok456")
    assert "abc123" not in repr(s) and "tok456" not in repr(s)


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "client_secret=abc123&grant_type=client_credentials",
        '{"refresh_token": "0.AXoA-secret"}',
        "api_token: tok456",
    ],
)
def test_redaction(raw):
    redacted = redact(raw)
    for secret in ("eyJhbGciOiJIUzI1NiJ9", "abc123", "0.AXoA-secret", "tok456"):
        assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_json_log_includes_trace_context_and_redacts():
    formatter = JsonFormatter()
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "token Bearer abc.def", None, None)
    from app.core.logging import ContextFilter

    with log_context(trace_id="trace-1", support_request_id=42):
        ContextFilter().filter(record)
    out = formatter.format(record)
    assert '"trace_id": "trace-1"' in out and '"support_request_id": 42' in out
    assert "abc.def" not in out
