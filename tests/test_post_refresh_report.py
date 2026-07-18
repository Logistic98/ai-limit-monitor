from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from application.monitor_service import LimitMonitor, _usage_reflects_refresh
from domain.models import CheckResult, ProviderUsage, UsageWindow

_NOW = datetime(2026, 7, 18, 10, 41, tzinfo=UTC)


def _result(*windows: UsageWindow, ok: bool = True) -> CheckResult:
    return CheckResult(
        captured_at=_NOW,
        providers=[
            ProviderUsage(
                provider="claude",
                ok=ok,
                captured_at=_NOW,
                windows=list(windows),
            )
        ],
    )


def _window(key: str, resets_at: datetime | None, used_percent: float = 0.0) -> UsageWindow:
    return UsageWindow(key=key, label=key, used_percent=used_percent, resets_at=resets_at)


class UsageReflectsRefreshTest(unittest.TestCase):
    def test_missing_reset_time_is_stale(self) -> None:
        result = _result(
            _window("five_hour", None),
            _window("seven_day", _NOW + timedelta(days=3)),
        )
        self.assertFalse(_usage_reflects_refresh(result, "claude"))

    def test_past_reset_time_is_stale(self) -> None:
        result = _result(
            _window("five_hour", _NOW - timedelta(minutes=1), used_percent=100.0),
            _window("seven_day", _NOW + timedelta(days=3)),
        )
        self.assertFalse(_usage_reflects_refresh(result, "claude"))

    def test_all_future_reset_times_are_fresh(self) -> None:
        result = _result(
            _window("five_hour", _NOW + timedelta(hours=5)),
            _window("seven_day", _NOW + timedelta(days=3)),
        )
        self.assertTrue(_usage_reflects_refresh(result, "claude"))

    def test_provider_error_or_absence_is_stale(self) -> None:
        self.assertFalse(_usage_reflects_refresh(_result(ok=False), "claude"))
        self.assertFalse(
            _usage_reflects_refresh(
                _result(_window("five_hour", _NOW + timedelta(hours=5))),
                "codex",
            )
        )


class MonitorAfterQuotaRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_report_waits_until_usage_shows_new_window(self) -> None:
        stale = _result(_window("five_hour", None))
        fresh = _result(_window("five_hour", _NOW + timedelta(hours=5)))
        results = [stale, stale, fresh]
        checks: list[int] = []

        monitor = object.__new__(LimitMonitor)

        async def fake_check() -> CheckResult:
            checks.append(1)
            return results[len(checks) - 1]

        finalized: list[CheckResult] = []

        async def fake_monitor_once(*, result: CheckResult | None = None):
            assert result is not None
            finalized.append(result)
            return result, {}

        monitor.check = fake_check  # type: ignore[method-assign]
        monitor._monitor_once_for_command = fake_monitor_once  # type: ignore[method-assign]

        with patch("application.monitor_service._REFRESH_REPORT_RETRY_SECONDS", 0):
            check, logins = await monitor._monitor_after_quota_refresh("claude")

        self.assertEqual(len(checks), 3)
        self.assertIs(check, fresh)
        self.assertEqual(finalized, [fresh])
        self.assertEqual(logins, {})

    async def test_report_sent_after_max_checks_even_if_stale(self) -> None:
        stale = _result(_window("five_hour", None))
        checks: list[int] = []

        monitor = object.__new__(LimitMonitor)

        async def fake_check() -> CheckResult:
            checks.append(1)
            return stale

        async def fake_monitor_once(*, result: CheckResult | None = None):
            return result, {}

        monitor.check = fake_check  # type: ignore[method-assign]
        monitor._monitor_once_for_command = fake_monitor_once  # type: ignore[method-assign]

        with patch("application.monitor_service._REFRESH_REPORT_RETRY_SECONDS", 0):
            check, _ = await monitor._monitor_after_quota_refresh("claude")

        self.assertEqual(len(checks), 6)
        self.assertIs(check, stale)


if __name__ == "__main__":
    unittest.main()
