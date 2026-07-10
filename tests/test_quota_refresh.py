from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from application.quota_refresh_service import ProactiveQuotaRefreshService
from domain.models import CheckResult, ProviderName, ProviderUsage, UsageWindow
from infrastructure.providers.quota_refresh import QuotaRefreshResult, _command_for
from infrastructure.storage.json_state import MonitorState


class FakeQuotaRefreshClient:
    def __init__(self) -> None:
        self.calls: list[ProviderName] = []
        self.results: dict[ProviderName, QuotaRefreshResult] = {}
        self.called = asyncio.Event()

    async def request(self, provider: ProviderName) -> QuotaRefreshResult:
        self.calls.append(provider)
        self.called.set()
        return self.results.get(provider, QuotaRefreshResult(provider=provider, ok=True))


class ProactiveQuotaRefreshServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self._state_path = Path(self._temporary_directory.name) / "state.json"
        self._now = 1_700_000_000.0
        self._state = MonitorState()
        self._client = FakeQuotaRefreshClient()
        self._service = ProactiveQuotaRefreshService(
            self._state,
            self._state_path,
            self._client,  # type: ignore[arg-type]
            claude_enabled=True,
            codex_enabled=True,
            clock=lambda: self._now,
            retry_seconds=900,
        )

    async def test_any_triggered_window_runs_at_its_reset_time(self) -> None:
        reset_at = self._now + 300
        self._service.observe(
            _check_result(
                self._now,
                _provider_usage(
                    "claude",
                    self._now,
                    _window("seven_day", reset_at, used_percent=100.0),
                ),
                _provider_usage(
                    "codex",
                    self._now,
                    _window("extra.primary_window", reset_at, limit_reached=True),
                ),
            )
        )

        self._now = reset_at - 0.1
        await self._service.run_due()
        self.assertEqual(self._client.calls, [])

        self._now = reset_at
        await self._service.run_due()

        self.assertCountEqual(self._client.calls, ["claude", "codex"])
        self.assertEqual(self._state.quota_refresh_scheduled_at, {})

    async def test_run_loop_wakes_at_reset_without_fixed_polling_delay(self) -> None:
        state = MonitorState(
            quota_refresh_scheduled_at={"claude:seven_day": time.time() + 0.05}
        )
        client = FakeQuotaRefreshClient()
        service = ProactiveQuotaRefreshService(
            state,
            self._state_path,
            client,  # type: ignore[arg-type]
            claude_enabled=True,
            codex_enabled=True,
        )
        stop_event = asyncio.Event()
        started_at = time.monotonic()
        task = asyncio.create_task(service.run_loop(stop_event))

        await asyncio.wait_for(client.called.wait(), timeout=1)
        elapsed = time.monotonic() - started_at
        stop_event.set()
        service.notify_setting_changed()
        await asyncio.wait_for(task, timeout=1)

        self.assertLess(elapsed, 0.8)
        self.assertEqual(client.calls, ["claude"])

    async def test_multiple_due_windows_for_one_provider_send_one_request(self) -> None:
        self._state.quota_refresh_scheduled_at = {
            "claude:five_hour": self._now,
            "claude:seven_day": self._now - 10,
            "claude:seven_day_sonnet": self._now + 100,
        }

        await self._service.run_due()

        self.assertEqual(self._client.calls, ["claude"])
        self.assertEqual(
            self._state.quota_refresh_scheduled_at,
            {"claude:seven_day_sonnet": self._now + 100},
        )

    async def test_later_window_is_not_blocked_by_earlier_success(self) -> None:
        self._state.quota_refresh_scheduled_at = {
            "claude:five_hour": self._now,
            "claude:seven_day": self._now + 60,
        }

        await self._service.run_due()
        self._now += 60
        await self._service.run_due()

        self.assertEqual(self._client.calls, ["claude", "claude"])
        self.assertEqual(self._state.quota_refresh_scheduled_at, {})

    async def test_untriggered_or_missing_windows_do_not_schedule_requests(self) -> None:
        self._service.observe(
            _check_result(
                self._now,
                _provider_usage(
                    "claude",
                    self._now,
                    _window("five_hour", self._now + 300, used_percent=99.9),
                ),
                _provider_usage("codex", self._now),
            )
        )
        self._now += 1_000

        await self._service.run_due()

        self.assertEqual(self._client.calls, [])
        self.assertEqual(self._state.quota_refresh_scheduled_at, {})

    async def test_trigger_without_reset_time_is_not_scheduled(self) -> None:
        usage = _provider_usage(
            "claude",
            self._now,
            UsageWindow(
                key="claude_rate_limited",
                label="额度已达上限",
                used_percent=100.0,
                limit_reached=True,
            ),
        )

        self._service.observe(_check_result(self._now, usage))

        self.assertEqual(self._state.quota_refresh_scheduled_at, {})

    async def test_failure_is_retried_only_after_cooldown(self) -> None:
        self._state.quota_refresh_scheduled_at["claude:seven_day"] = self._now
        self._client.results["claude"] = QuotaRefreshResult(
            provider="claude",
            ok=False,
            error="timeout",
        )

        await self._service.run_due()
        self._now += 899
        await self._service.run_due()
        self._now += 1
        await self._service.run_due()

        self.assertEqual(self._client.calls, ["claude", "claude"])
        self.assertEqual(self._state.quota_refresh_last_errors["claude"], "timeout")

    async def test_disabled_setting_prevents_requests(self) -> None:
        self._state.proactive_quota_refresh_enabled = False
        self._state.quota_refresh_scheduled_at["claude:five_hour"] = self._now

        await self._service.run_due()

        self.assertEqual(self._client.calls, [])

    async def test_new_trigger_updates_existing_window_reset(self) -> None:
        self._state.quota_refresh_scheduled_at["codex:codex.primary_window"] = self._now
        new_reset = self._now + 600

        self._service.observe(
            _check_result(
                self._now,
                _provider_usage(
                    "codex",
                    self._now,
                    _window("codex.primary_window", new_reset, limit_reached=True),
                ),
            )
        )

        self.assertEqual(
            self._state.quota_refresh_scheduled_at["codex:codex.primary_window"],
            new_reset,
        )

    async def test_legacy_provider_only_schedules_are_discarded(self) -> None:
        state = MonitorState(
            quota_refresh_scheduled_at={
                "claude": self._now,
                "codex": self._now,
                "claude:seven_day": self._now + 10,
            }
        )

        ProactiveQuotaRefreshService(
            state,
            self._state_path,
            self._client,  # type: ignore[arg-type]
            claude_enabled=True,
            codex_enabled=True,
            clock=lambda: self._now,
        )

        self.assertEqual(
            state.quota_refresh_scheduled_at,
            {"claude:seven_day": self._now + 10},
        )


class QuotaRefreshCommandTest(unittest.TestCase):
    def test_commands_are_minimal_and_do_not_contain_credentials(self) -> None:
        claude = _command_for("claude")
        codex = _command_for("codex")

        self.assertIn("--tools", claude)
        self.assertIn("--no-session-persistence", claude)
        self.assertIn("--sandbox", codex)
        self.assertIn("read-only", codex)
        self.assertNotIn("token", " ".join(claude).lower())
        self.assertNotIn("token", " ".join(codex).lower())


def _check_result(timestamp: float, *providers: ProviderUsage) -> CheckResult:
    return CheckResult(
        captured_at=datetime.fromtimestamp(timestamp, tz=UTC),
        providers=list(providers),
    )


def _provider_usage(
    provider: ProviderName,
    timestamp: float,
    *windows: UsageWindow,
) -> ProviderUsage:
    return ProviderUsage(
        provider=provider,
        ok=True,
        captured_at=datetime.fromtimestamp(timestamp, tz=UTC),
        windows=list(windows),
    )


def _window(
    key: str,
    reset_at: float,
    *,
    used_percent: float = 50.0,
    limit_reached: bool | None = None,
) -> UsageWindow:
    return UsageWindow(
        key=key,
        label=key,
        used_percent=used_percent,
        resets_at=datetime.fromtimestamp(reset_at, tz=UTC),
        limit_reached=limit_reached,
    )
