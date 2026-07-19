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
        state = MonitorState(quota_refresh_scheduled_at={"claude:seven_day": time.time() + 0.05})
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

    async def test_windows_without_reset_time_do_not_schedule_requests(self) -> None:
        self._service.observe(
            _check_result(
                self._now,
                _provider_usage(
                    "claude",
                    self._now,
                    _window("five_hour", None, used_percent=98.9),
                ),
                _provider_usage("codex", self._now),
            )
        )
        self._now += 1_000

        await self._service.run_due()

        self.assertEqual(self._client.calls, [])
        self.assertEqual(self._state.quota_refresh_scheduled_at, {})

    async def test_any_window_with_reset_time_is_scheduled_regardless_of_usage(self) -> None:
        # Usage percent is ignored on purpose: a window polled at 92% can reset before
        # the next check ever observes it saturated, so every known reset is scheduled.
        reset_at = self._now + 300
        self._service.observe(
            _check_result(
                self._now,
                _provider_usage(
                    "claude",
                    self._now,
                    _window("five_hour", reset_at, used_percent=13.0),
                    _window("seven_day", reset_at + 600, used_percent=92.0),
                ),
            )
        )

        self.assertEqual(
            self._state.quota_refresh_scheduled_at,
            {
                "claude:five_hour": reset_at,
                "claude:seven_day": reset_at + 600,
            },
        )

    async def test_stale_trigger_with_past_reset_time_is_not_rescheduled(self) -> None:
        # Right after a successful refresh the usage API can briefly keep reporting the
        # old saturated window; rescheduling its past reset would refire immediately.
        self._service.observe(
            _check_result(
                self._now,
                _provider_usage(
                    "claude",
                    self._now,
                    _window("five_hour", self._now - 1, used_percent=100.0),
                ),
            )
        )

        await self._service.run_due()

        self.assertEqual(self._client.calls, [])
        self.assertEqual(self._state.quota_refresh_scheduled_at, {})

    async def test_on_result_callback_receives_success_and_failure(self) -> None:
        received: list[QuotaRefreshResult] = []

        async def on_result(result: QuotaRefreshResult) -> None:
            received.append(result)

        service = ProactiveQuotaRefreshService(
            self._state,
            self._state_path,
            self._client,  # type: ignore[arg-type]
            claude_enabled=True,
            codex_enabled=True,
            clock=lambda: self._now,
            retry_seconds=900,
            on_result=on_result,
        )
        self._state.quota_refresh_scheduled_at = {
            "claude:five_hour": self._now,
            "codex:codex.primary_window": self._now,
        }
        self._client.results["codex"] = QuotaRefreshResult(
            provider="codex",
            ok=False,
            error="timeout",
        )

        await service.run_due()

        self.assertEqual(
            {(result.provider, result.ok) for result in received},
            {("claude", True), ("codex", False)},
        )

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


class QuotaRefreshAuthGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self._state_path = Path(self._temporary_directory.name) / "state.json"
        self._now = 1_700_000_000.0
        self._state = MonitorState()
        self._client = FakeQuotaRefreshClient()
        self._results: list[QuotaRefreshResult] = []

        async def on_result(result: QuotaRefreshResult) -> None:
            self._results.append(result)

        self._service = ProactiveQuotaRefreshService(
            self._state,
            self._state_path,
            self._client,  # type: ignore[arg-type]
            claude_enabled=True,
            codex_enabled=True,
            clock=lambda: self._now,
            retry_seconds=900,
            on_result=on_result,
        )

    def _observe_auth_required(self) -> None:
        self._service.observe(
            _check_result(
                self._now,
                ProviderUsage(
                    provider="claude",
                    ok=False,
                    captured_at=datetime.fromtimestamp(self._now, tz=UTC),
                    error="Claude 登录已失效",
                    error_kind="auth_required",
                ),
            )
        )

    async def test_auth_required_check_blocks_scheduled_requests(self) -> None:
        self._state.quota_refresh_scheduled_at["claude:five_hour"] = self._now

        self._observe_auth_required()
        await self._service.run_due()

        self.assertEqual(self._client.calls, [])
        self.assertIn("claude:five_hour", self._state.quota_refresh_scheduled_at)
        self.assertIsNone(self._service._seconds_until_next_attempt())  # noqa: SLF001

    async def test_passing_check_resumes_blocked_requests(self) -> None:
        self._state.quota_refresh_scheduled_at["claude:five_hour"] = self._now
        self._observe_auth_required()
        await self._service.run_due()
        self.assertEqual(self._client.calls, [])

        self._service.observe(
            _check_result(
                self._now,
                _provider_usage(
                    "claude",
                    self._now,
                    _window("seven_day", self._now + 600, used_percent=17.0),
                ),
            )
        )
        await self._service.run_due()

        self.assertEqual(self._client.calls, ["claude"])

    async def test_cli_auth_failure_blocks_until_check_passes(self) -> None:
        self._state.quota_refresh_scheduled_at["claude:five_hour"] = self._now
        self._client.results["claude"] = QuotaRefreshResult(
            provider="claude",
            ok=False,
            error="exit_code_1: Failed to authenticate. API Error: 401 Invalid credentials",
        )

        await self._service.run_due()
        self.assertEqual(self._client.calls, ["claude"])
        self.assertTrue(self._results[-1].auth_blocked)

        # Cooldown expires but the login is still broken: no more requests.
        self._now += 900
        await self._service.run_due()
        self.assertEqual(self._client.calls, ["claude"])

        # A passing check clears the gate and the pending schedule fires again.
        self._client.results["claude"] = QuotaRefreshResult(provider="claude", ok=True)
        self._service.observe(
            _check_result(
                self._now,
                _provider_usage(
                    "claude",
                    self._now,
                    _window("seven_day", self._now + 600, used_percent=17.0),
                ),
            )
        )
        await self._service.run_due()
        self.assertEqual(self._client.calls, ["claude", "claude"])

    async def test_blocked_provider_does_not_gate_other_provider(self) -> None:
        self._state.quota_refresh_scheduled_at["claude:five_hour"] = self._now
        self._state.quota_refresh_scheduled_at["codex:codex.primary_window"] = self._now
        self._observe_auth_required()

        await self._service.run_due()

        self.assertEqual(self._client.calls, ["codex"])

    async def test_manual_trigger_success_clears_auth_gate(self) -> None:
        self._state.quota_refresh_auth_blocked["claude"] = True

        result = await self._service.trigger_now("claude")

        self.assertTrue(result.ok)
        self.assertEqual(self._state.quota_refresh_auth_blocked, {})


class FailingQuotaRefreshClient:
    def __init__(self) -> None:
        self.calls: list[ProviderName] = []

    async def request(self, provider: ProviderName) -> QuotaRefreshResult:
        self.calls.append(provider)
        return QuotaRefreshResult(provider=provider, ok=False, error="exit_code_1: boom")


class QuotaRefreshGiveUpTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self._state_path = Path(self._temporary_directory.name) / "state.json"
        self._now = 1_700_000_000.0
        self._state = MonitorState()
        self._client = FailingQuotaRefreshClient()
        self._results: list[QuotaRefreshResult] = []

        async def on_result(result: QuotaRefreshResult) -> None:
            self._results.append(result)

        self._service = ProactiveQuotaRefreshService(
            self._state,
            self._state_path,
            self._client,  # type: ignore[arg-type]
            claude_enabled=True,
            codex_enabled=True,
            clock=lambda: self._now,
            retry_seconds=900,
            max_attempts=2,
            on_result=on_result,
        )

    async def test_gives_up_after_max_failed_attempts(self) -> None:
        self._state.quota_refresh_scheduled_at["claude:five_hour"] = self._now

        await self._service.run_due()
        self._now += 900
        await self._service.run_due()
        self._now += 900
        await self._service.run_due()

        self.assertEqual(self._client.calls, ["claude", "claude"])
        self.assertEqual(self._state.quota_refresh_scheduled_at, {})
        self.assertEqual(self._state.quota_refresh_attempt_counts, {})
        self.assertEqual(
            [(result.attempt, result.gave_up) for result in self._results],
            [(1, False), (2, True)],
        )

    async def test_new_reset_time_clears_attempt_count(self) -> None:
        self._state.quota_refresh_scheduled_at["claude:five_hour"] = self._now
        await self._service.run_due()
        self.assertEqual(self._state.quota_refresh_attempt_counts, {"claude:five_hour": 1})

        new_reset = self._now + 600
        self._service.observe(
            _check_result(
                self._now,
                _provider_usage(
                    "claude",
                    self._now,
                    _window("five_hour", new_reset, used_percent=100.0),
                ),
            )
        )

        self.assertEqual(self._state.quota_refresh_attempt_counts, {})
        self.assertEqual(
            self._state.quota_refresh_scheduled_at,
            {"claude:five_hour": new_reset},
        )


class SlowQuotaRefreshClient:
    """Client whose request blocks until released, for concurrency tests."""

    def __init__(self) -> None:
        self.calls: list[ProviderName] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def request(self, provider: ProviderName) -> QuotaRefreshResult:
        self.calls.append(provider)
        self.started.set()
        await self.release.wait()
        return QuotaRefreshResult(provider=provider, ok=True)


class QuotaRefreshConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self._state_path = Path(self._temporary_directory.name) / "state.json"
        self._state = MonitorState()
        self._client = SlowQuotaRefreshClient()
        self._service = ProactiveQuotaRefreshService(
            self._state,
            self._state_path,
            self._client,  # type: ignore[arg-type]
            claude_enabled=True,
            codex_enabled=True,
            retry_seconds=900,
        )

    async def test_manual_trigger_is_rejected_while_scheduled_request_runs(self) -> None:
        self._state.quota_refresh_scheduled_at["claude:five_hour"] = time.time() - 1

        run_task = asyncio.create_task(self._service.run_due())
        await asyncio.wait_for(self._client.started.wait(), timeout=1)

        result = await asyncio.wait_for(self._service.trigger_now("claude"), timeout=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "already_running")

        self._client.release.set()
        await asyncio.wait_for(run_task, timeout=1)
        self.assertEqual(self._client.calls, ["claude"])

    async def test_scheduled_run_skips_provider_with_manual_request_in_flight(self) -> None:
        trigger_task = asyncio.create_task(self._service.trigger_now("claude"))
        await asyncio.wait_for(self._client.started.wait(), timeout=1)

        self._state.quota_refresh_scheduled_at["claude:five_hour"] = time.time() - 1
        await asyncio.wait_for(self._service.run_due(), timeout=1)
        self.assertEqual(self._client.calls, ["claude"])

        self._client.release.set()
        result = await asyncio.wait_for(trigger_task, timeout=1)
        self.assertTrue(result.ok)
        # The schedule remains pending for the next loop pass.
        self.assertIn("claude:five_hour", self._state.quota_refresh_scheduled_at)

    async def test_state_lock_is_free_while_request_runs(self) -> None:
        self._state.quota_refresh_scheduled_at["codex:codex.primary_window"] = time.time() - 1

        run_task = asyncio.create_task(self._service.run_due())
        await asyncio.wait_for(self._client.started.wait(), timeout=1)

        # The snapshot path acquires nothing, but the lock itself must be available.
        acquired = self._service._lock.locked()  # noqa: SLF001
        self.assertFalse(acquired)

        self._client.release.set()
        await asyncio.wait_for(run_task, timeout=1)


class QuotaRefreshClientFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_codex_sandbox_failure_retries_without_kernel_sandbox(self) -> None:
        from infrastructure.providers.quota_refresh import QuotaRefreshClient

        client = QuotaRefreshClient()
        commands: list[tuple[str, ...]] = []

        async def fake_run(provider: ProviderName, command: tuple[str, ...]):
            commands.append(command)
            if len(commands) == 1:
                return QuotaRefreshResult(
                    provider=provider,
                    ok=False,
                    error="exit_code_1: Sandbox was mandated, but Landlock is unavailable",
                )
            return QuotaRefreshResult(provider=provider, ok=True)

        client._run = fake_run  # type: ignore[method-assign]
        result = await client.request("codex")

        self.assertTrue(result.ok)
        self.assertEqual(len(commands), 2)
        self.assertIn("danger-full-access", commands[1])

    async def test_claude_failure_is_not_retried_without_sandbox(self) -> None:
        from infrastructure.providers.quota_refresh import QuotaRefreshClient

        client = QuotaRefreshClient()
        commands: list[tuple[str, ...]] = []

        async def fake_run(provider: ProviderName, command: tuple[str, ...]):
            commands.append(command)
            return QuotaRefreshResult(
                provider=provider,
                ok=False,
                error="exit_code_1: Failed to authenticate",
            )

        client._run = fake_run  # type: ignore[method-assign]
        result = await client.request("claude")

        self.assertFalse(result.ok)
        self.assertEqual(len(commands), 1)


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
    reset_at: float | None,
    *,
    used_percent: float = 50.0,
    limit_reached: bool | None = None,
) -> UsageWindow:
    return UsageWindow(
        key=key,
        label=key,
        used_percent=used_percent,
        resets_at=None if reset_at is None else datetime.fromtimestamp(reset_at, tz=UTC),
        limit_reached=limit_reached,
    )
