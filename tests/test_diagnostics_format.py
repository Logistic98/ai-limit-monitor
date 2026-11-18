from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from config.settings import Settings
from domain.models import CheckResult, ProviderUsage
from infrastructure.providers.codex import CodexUsageClient
from presentation.telegram_formatter import format_diagnostics, format_report


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="test-token",
        telegram_chat_id="1",
        timezone="Asia/Shanghai",
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
        self.assertIn("凭据状态：有效", text)
        self.assertIn("自动刷新：到期前 5 分钟", text)
        self.assertIn("凭据到期：2026-07-20 08:00（剩余 7 天）", text)
        self.assertIn("凭据到期：2026-07-13 08:42（剩余 42 分钟）", text)
        self.assertIn("检查间隔：10 分钟", text)
        self.assertIn("上次刷新：2026-07-12 16:00", text)
        for key in ("account id", "access token", "refresh token", "token 状态"):
            self.assertNotIn(key, text)

    def test_localizes_missing_and_expired_credentials(self) -> None:
        result = _result()
        result = CheckResult(result.captured_at, [
            ProviderUsage(
                "claude", False, result.captured_at, error_kind="auth_required",
            ),
            result.providers[1],
        ])
        text = format_diagnostics(
            result,
            _settings(),
            {"status": "missing_access_token"},
            {
                "status": "expired",
                "has_access_token": True,
                "has_account_id": False,
                "expires_at": result.captured_at - timedelta(minutes=13321),
            },
        )
        self.assertIn("<b>Claude</b>：需要重新登录", text)
        self.assertIn("凭据状态：缺少访问凭据", text)
        self.assertIn("凭据状态：已过期", text)
        self.assertIn("（已过 9 天 6 小时）", text)
        self.assertIn("账号标识：缺失", text)
        self.assertEqual(text.count("自动刷新："), 1)

    def test_disabled_codex_shows_disabled_line(self) -> None:
        text = format_diagnostics(
            _result(),
            _settings(),
            {"provider": "claude", "enabled": False},
            {"provider": "codex", "enabled": False},
        )

        self.assertIn("<b>Claude 监控</b>：已禁用", text)
        self.assertIn("<b>Codex 监控</b>：已禁用", text)

    def test_report_localizes_usage_credits_and_reset(self) -> None:
        now = _result().captured_at
        windows, credits = CodexUsageClient.parse_usage({
            "rate_limit": {
                "primary_window": {"used_percent": 10, "limit_window_seconds": 18000},
                "secondary_window": {
                    "used_percent": 38,
                    "limit_window_seconds": 604800,
                    "reset_at": int((now + timedelta(days=2, hours=22)).timestamp()),
                },
            },
            "credits": {"balance": 0},
        })
        text = format_report(
            CheckResult(now, [ProviderUsage("codex", True, now, windows, credits)]),
            _settings(),
        )
        self.assertIn("状态：正常", text)
        self.assertIn("5 小时额度：已使用 10.0%\n  重置时间未知", text)
        self.assertIn(
            "7 天额度：已使用 38.0%\n  重置：2026-07-16 06:00（剩余 2 天 22 小时）", text,
        )
        self.assertIn("积分：余额 0", text)
