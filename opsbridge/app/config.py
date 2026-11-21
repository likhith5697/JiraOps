"""Application configuration loaded from environment variables / `.env`.

All external identifiers (tenant, team, channel, Jira project, issue type, priority names)
come from configuration; nothing is hard-coded.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.errors import ConfigurationError

DEFAULT_DEV_ADMIN_KEY = "dev-admin-key"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application -------------------------------------------------------------------
    app_name: str = "OpsBridge"
    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_base_url: str = ""
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    admin_api_key: SecretStr = SecretStr(DEFAULT_DEV_ADMIN_KEY)
    mock_mode: bool = True

    # --- Microsoft Entra ID / Graph ----------------------------------------------------
    microsoft_tenant_id: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: SecretStr = SecretStr("")
    microsoft_client_state: SecretStr = SecretStr("")
    # Delegated identity used to post replies (Graph has no app-only "send channel message").
    microsoft_delegated_refresh_token: SecretStr = SecretStr("")
    microsoft_service_account_user_id: str = ""
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    login_base_url: str = "https://login.microsoftonline.com"
    graph_subscription_lifetime_minutes: int = Field(default=4200, ge=5, le=4230)
    graph_subscription_renewal_threshold_minutes: int = Field(default=720, ge=1)

    teams_team_id: str = ""
    teams_channel_id: str = ""
    # Entra object IDs (comma-separated) of support admins allowed to run write commands
    # (close, reopen, escalate) on ANY request. The original requester is always allowed for
    # their own request; `status` is open to everyone. Both variables are merged;
    # TEAMS_CLOSE_ALLOWED_USER_IDS is the original name and keeps working.
    teams_command_admin_user_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    teams_close_allowed_user_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Optional channel (same team) where `escalate` posts an alert. Must differ from the support channel.
    teams_escalation_channel_id: str = ""

    # --- Jira ------------------------------------------------------------------------------
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: SecretStr = SecretStr("")
    jira_project_key: str = "OPS"
    jira_issue_type: str = "Task"
    # JSON object mapping OpsBridge priorities to Jira priority *names*; empty = do not set priority.
    jira_priority_map: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=lambda: {"P1": "Highest", "P2": "High", "P3": "Medium"}
    )
    jira_extra_labels: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["opsbridge"])
    jira_reconcile_delay_seconds: int = Field(default=60, ge=0)
    # Target status name for the `close` command; matched case-insensitively against each
    # available transition's `to.name`. Transition IDs are never hard-coded.
    jira_close_status: str = Field(default="Done", min_length=1)
    # Target status for `reopen` (matched the same way as JIRA_CLOSE_STATUS).
    jira_reopen_status: str = Field(default="To Do", min_length=1)
    # Label added by `escalate` (in addition to raising the priority).
    jira_escalation_label: str = Field(default="escalated", min_length=1, pattern=r"^\S+$")

    # --- Jira -> Teams status sync --------------------------------------------------------
    jira_sync_enabled: bool = True
    # Poll Jira for recently updated issues every N seconds (0 = no polling; webhook only).
    jira_sync_poll_interval_seconds: int = Field(default=120, ge=0)
    # Shared secret for POST /webhooks/jira (HMAC header or ?secret=). Empty = endpoint disabled.
    jira_webhook_secret: SecretStr = SecretStr("")

    # --- AI / LLM (optional; everything works without it) ------------------------------------
    llm_enabled: bool = False
    llm_provider: Literal["anthropic", "openai", "ollama", "mock"] = "mock"
    llm_model: str = ""          # empty = provider default
    llm_api_key: SecretStr = SecretStr("")
    embedding_provider: Literal["openai", "ollama", "mock", "none"] = "mock"
    embedding_model: str = ""    # empty = provider default
    embedding_api_key: SecretStr = SecretStr("")  # falls back to LLM_API_KEY for openai
    ollama_base_url: str = "http://localhost:11434"
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_daily_token_budget: int = Field(default=200_000, ge=0)  # 0 = unlimited
    ask_min_score: float = Field(default=0.35, ge=0.0, le=1.0)
    ask_top_k: int = Field(default=3, ge=1, le=10)

    # --- Database --------------------------------------------------------------------------
    database_url: str = "sqlite:///./opsbridge.db"

    # --- Processing / reliability -------------------------------------------------------
    background_worker_enabled: bool = True
    worker_count: int = Field(default=2, ge=1, le=32)
    worker_poll_interval_seconds: float = Field(default=10.0, gt=0)
    worker_lease_seconds: int = Field(default=120, ge=10)
    subscription_check_interval_seconds: float = Field(default=300.0, gt=0)
    max_retries: int = Field(default=5, ge=0)
    retry_base_delay_seconds: float = Field(default=5.0, ge=0)
    retry_max_delay_seconds: float = Field(default=900.0, ge=0)
    http_timeout_seconds: float = Field(default=10.0, gt=0)
    http_max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator("jira_priority_map", mode="before")
    @classmethod
    def _parse_priority_map(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return json.loads(value) if value else {}
        return value

    @field_validator("jira_extra_labels", "teams_close_allowed_user_ids", "teams_command_admin_user_ids", mode="before")
    @classmethod
    def _parse_labels(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("["):
                return json.loads(value)
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @field_validator("app_base_url", "jira_base_url", "graph_base_url", "login_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    def check(self) -> "Settings":
        """Cross-field checks. Deliberately NOT a pydantic validator: pydantic's ValidationError
        echoes every input value, which would print secrets to stderr/logs on a bad deploy.
        """
        if self.app_env == "production":
            key = self.admin_api_key.get_secret_value()
            if key == DEFAULT_DEV_ADMIN_KEY or len(key) < 24:
                raise ConfigurationError("ADMIN_API_KEY must be set to a strong value (>= 24 chars) in production")
            if self.mock_mode:
                raise ConfigurationError("MOCK_MODE must be false in production")
        if not self.mock_mode:
            missing = [name.upper() for name in self.missing_live_settings()]
            if missing:
                raise ConfigurationError(
                    "MOCK_MODE=false but required settings are missing: " + ", ".join(missing)
                )
        if self.llm_enabled and self.llm_provider in ("anthropic", "openai") and not self.llm_api_key.get_secret_value():
            raise ConfigurationError(f"LLM_ENABLED=true with LLM_PROVIDER={self.llm_provider} requires LLM_API_KEY")
        if self.embedding_provider == "openai" and not (self.embedding_api_key.get_secret_value()
                                                         or self.llm_api_key.get_secret_value()):
            raise ConfigurationError("EMBEDDING_PROVIDER=openai requires EMBEDDING_API_KEY (or LLM_API_KEY)")
        if self.effective_escalation_channel_id and self.effective_escalation_channel_id == self.effective_channel_id:
            # Alerts posted into the support channel would be ingested as new support requests.
            raise ConfigurationError("TEAMS_ESCALATION_CHANNEL_ID must be a different channel from TEAMS_CHANNEL_ID")
        return self

    def missing_live_settings(self) -> list[str]:
        """Names (never values) of settings needed to talk to real Microsoft/Jira services."""
        required: dict[str, str | SecretStr] = {
            "app_base_url": self.app_base_url,
            "microsoft_tenant_id": self.microsoft_tenant_id,
            "microsoft_client_id": self.microsoft_client_id,
            "microsoft_client_secret": self.microsoft_client_secret,
            "microsoft_client_state": self.microsoft_client_state,
            "teams_team_id": self.teams_team_id,
            "teams_channel_id": self.teams_channel_id,
            "jira_base_url": self.jira_base_url,
            "jira_email": self.jira_email,
            "jira_api_token": self.jira_api_token,
            "jira_project_key": self.jira_project_key,
            "jira_issue_type": self.jira_issue_type,
        }
        missing = []
        for name, value in required.items():
            raw = value.get_secret_value() if isinstance(value, SecretStr) else value
            if not raw:
                missing.append(name)
        return missing

    # Effective values used in mock mode so the app runs with zero configuration.
    @property
    def effective_team_id(self) -> str:
        return self.teams_team_id or ("mock-team-id" if self.mock_mode else "")

    @property
    def effective_channel_id(self) -> str:
        return self.teams_channel_id or ("19:mock-support-channel@thread.tacv2" if self.mock_mode else "")

    @property
    def effective_client_state(self) -> str:
        value = self.microsoft_client_state.get_secret_value()
        return value or ("mock-client-state" if self.mock_mode else "")

    @property
    def effective_escalation_channel_id(self) -> str:
        return self.teams_escalation_channel_id or ("19:mock-escalations@thread.tacv2" if self.mock_mode else "")

    @property
    def command_admin_user_ids(self) -> frozenset[str]:
        return frozenset(self.teams_command_admin_user_ids) | frozenset(self.teams_close_allowed_user_ids)

    @property
    def effective_jira_base_url(self) -> str:
        return self.jira_base_url or ("https://mock-jira.example.com" if self.mock_mode else "")


@lru_cache
def get_settings() -> Settings:
    return Settings().check()
