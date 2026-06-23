from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_csv(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _parse_float_csv(value: Any) -> list[float]:
    values = _parse_csv(value)
    return [float(item) for item in values]


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: SecretStr
    telegram_chat_id: str
    telegram_allowed_chat_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)

    check_interval_seconds: int = Field(default=600, ge=30)
    report_interval_seconds: int = Field(default=3600, ge=60)
    send_startup_report: bool = True
    alert_thresholds: Annotated[list[float], NoDecode] = Field(
        default_factory=lambda: [50, 70, 80, 90, 100]
    )
    timezone: str = "Asia/Shanghai"
    state_path: Path = Path("/data/state.json")
    http_timeout_seconds: float = Field(default=20.0, gt=0)

    claude_enabled: bool = True
    claude_usage_url: str = "https://api.anthropic.com/api/oauth/usage"
    claude_token_url: str = "https://platform.claude.com/v1/oauth/token"
    claude_oauth_client_id: str = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    claude_beta_header: str = "oauth-2025-04-20"
    claude_credentials_path: Path | None = Path("/root/.claude/.credentials.json")
    claude_refresh_before_seconds: int = Field(default=300, ge=0)
    claude_access_token: SecretStr | None = None

    codex_enabled: bool = True
    codex_usage_url: str = "https://chatgpt.com/backend-api/wham/usage"
    codex_auth_path: Path | None = Path("/root/.codex/auth.json")
    codex_access_token: SecretStr | None = None
    codex_account_id: str | None = None

    @field_validator("telegram_allowed_chat_ids", mode="before")
    @classmethod
    def parse_allowed_chat_ids(cls, value: Any) -> list[str]:
        return _parse_csv(value)

    @field_validator("alert_thresholds", mode="before")
    @classmethod
    def parse_alert_thresholds(cls, value: Any) -> list[float]:
        values = _parse_float_csv(value)
        return sorted(set(values))

    @model_validator(mode="after")
    def default_allowed_chat_ids(self) -> Settings:
        if not self.telegram_allowed_chat_ids:
            self.telegram_allowed_chat_ids = [self.telegram_chat_id]
        return self

    @property
    def telegram_token_value(self) -> str:
        return self.telegram_bot_token.get_secret_value()

    @property
    def claude_token_value(self) -> str | None:
        return self.claude_access_token.get_secret_value() if self.claude_access_token else None

    @property
    def codex_token_value(self) -> str | None:
        return self.codex_access_token.get_secret_value() if self.codex_access_token else None
