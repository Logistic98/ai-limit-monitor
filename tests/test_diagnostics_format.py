from __future__ import annotations

import unittest
from datetime import UTC, datetime

from config.settings import Settings
from domain.models import CheckResult, ProviderUsage
from presentation.telegram_formatter import format_diagnostics


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="test-token",
        telegram_chat_id="1",
        _env_file=None,
    )


def _result() -> CheckResult:
    captured_at = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
    return CheckResult(
        captured_at=captured_at,
        providers=[
            ProviderUsage(provider="claude", ok=True, captured_at=captured_at),
            ProviderUsage(provider="codex", ok=True, captured_at=captured_at),
        ],
    )


class FormatDiagnosticsTest(unittest.TestCase):
    def test_includes_claude_and_codex_auth_sections(self) -> None:
        claude_auth = {
            "provider": "claude",
            "enabled": True,
            "status": "valid",
            "credentials_path": "/root/.claude/.credentials.json",
            "has_access_token": True,
            "has_refresh_token": True,
            "can_auto_refresh": True,
            "expires_at": datetime(2026, 7, 13, 0, 42, tzinfo=UTC),
            "seconds_until_expiry": 2520,
            "refresh_before_seconds": 300,
        }
        codex_auth = {
            "provider": "codex",
            "enabled": True,
            "status": "valid",
            "auth_path": "/root/.codex/auth.json",
            "has_access_token": True,
            "has_refresh_token": True,
            "has_account_id": True,
            "can_auto_refresh": True,
            "expires_at": datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
            "seconds_until_expiry": 604800,
            "refresh_before_seconds": 300,
            "last_refresh": "2026-07-12T08:00:00Z",
        }

        text = format_diagnostics(_result(), _settings(), claude_auth, codex_auth)

        self.assertIn("/root/.claude/.credentials.json", text)
        self.assertIn("/root/.codex/auth.json", text)
        self.assertIn("account id：是", text)
        self.assertIn("可自动刷新：是", text)
        self.assertIn("提前刷新窗口：300s", text)
        self.assertIn("上次刷新：2026-07-12T08:00:00Z", text)

    def test_disabled_codex_shows_disabled_line(self) -> None:
        text = format_diagnostics(
            _result(),
            _settings(),
            {"provider": "claude", "enabled": False},
            {"provider": "codex", "enabled": False},
        )

        self.assertIn("Claude 监控：已禁用", text)
        self.assertIn("Codex 监控：已禁用", text)
