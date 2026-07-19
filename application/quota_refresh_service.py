from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from domain.models import CheckResult, ProviderName, UsageWindow
from infrastructure.providers.quota_refresh import (
    QuotaRefreshClient,
    QuotaRefreshResult,
    is_auth_failure,
)
from infrastructure.storage.json_state import MonitorState

logger = logging.getLogger(__name__)

DEFAULT_RETRY_SECONDS = 15 * 60
# A persistently failing CLI request would otherwise retry (and notify) forever;
# give up on a scheduled window after this many failed attempts.
DEFAULT_MAX_ATTEMPTS = 4
class ProactiveQuotaRefreshService:
    """Requests a provider immediately after any known quota window resets.

    Usage percent is deliberately ignored: polling every ~10 minutes means a window
    can climb past any watermark and reset without ever being observed there, and a
    lightweight request after an unsaturated reset is harmless. Every window with a
    known reset time is scheduled, so windows keep rolling over automatically.
    """

    def __init__(
        self,
        state: MonitorState,
        state_path: Path,
        client: QuotaRefreshClient,
        *,
        claude_enabled: bool,
        codex_enabled: bool,
        clock: Callable[[], float] = time.time,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        on_result: Callable[[QuotaRefreshResult], Awaitable[None]] | None = None,
    ) -> None:
        self._state = state
        self._state_path = state_path
        self._client = client
        self._provider_enabled: dict[ProviderName, bool] = {
            "claude": claude_enabled,
            "codex": codex_enabled,
        }
        self._clock = clock
        self._retry_seconds = retry_seconds
        self._max_attempts = max(1, max_attempts)
        self._on_result = on_result
        self._lock = asyncio.Lock()
        self._wake_event = asyncio.Event()
        # Providers with a CLI request currently running (scheduled or manual); used to
        # avoid firing two concurrent requests for the same provider.
        self._in_flight: set[ProviderName] = set()

        # Discard schedules written by the earlier provider-wide 5h-only implementation.
        for legacy_key in ("claude", "codex"):
            self._state.quota_refresh_scheduled_at.pop(legacy_key, None)
            self._state.quota_refresh_last_attempt_at.pop(legacy_key, None)

    def observe(self, result: CheckResult) -> None:
        """Schedule a request at the reset time of every window that exposes one."""

        changed = False
        for provider in result.providers:
            if not self._provider_enabled.get(provider.provider, False):
                continue
            if not provider.ok:
                # A broken login blocks scheduled CLI requests: they would fail with the
                # same auth error and spam retry notifications until the user re-logs in.
                if provider.error_kind == "auth_required" and not (
                    self._state.quota_refresh_auth_blocked.get(provider.provider)
                ):
                    self._state.quota_refresh_auth_blocked[provider.provider] = True
                    logger.warning(
                        "quota refresh paused until login recovers provider=%s",
                        provider.provider,
                    )
                continue
            if self._state.quota_refresh_auth_blocked.pop(provider.provider, None):
                changed = True
                logger.info(
                    "quota refresh resumed after login recovered provider=%s",
                    provider.provider,
                )
            for window in provider.windows:
                if not window.resets_at:
                    if window.limit_reached:
                        logger.warning(
                            "quota refresh trigger dropped without reset time "
                            "provider=%s window=%s used=%.1f%%",
                            provider.provider,
                            window.key,
                            window.used_percent,
                        )
                    continue
                key = _schedule_key(provider.provider, window)
                reset_at = window.resets_at.timestamp()
                if reset_at <= self._clock():
                    # Stale usage data can keep reporting the old saturated window right
                    # after a refresh; rescheduling a past reset would refire immediately.
                    logger.info(
                        "quota refresh trigger ignored with past reset time key=%s reset_at=%s",
                        key,
                        window.resets_at.isoformat(),
                    )
                    continue
                if self._state.quota_refresh_scheduled_at.get(key) != reset_at:
                    self._state.quota_refresh_scheduled_at[key] = reset_at
                    self._state.quota_refresh_last_attempt_at.pop(key, None)
                    self._state.quota_refresh_attempt_counts.pop(key, None)
                    changed = True
                    logger.info(
                        "quota refresh scheduled key=%s used=%.1f%% reset_at=%s",
                        key,
                        window.used_percent,
                        window.resets_at.isoformat(),
                    )

        if changed:
            self._wake_event.set()

    def notify_setting_changed(self) -> None:
        self._wake_event.set()

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        logger.info("proactive quota refresh loop started")
        while not stop_event.is_set():
            self._wake_event.clear()
            try:
                await self.run_due()
            except Exception:
                logger.exception("proactive quota refresh loop failed")

            timeout = self._seconds_until_next_attempt()
            if timeout is not None:
                timeout = max(0.1, timeout)
            await _wait_for_stop_or_wake(stop_event, self._wake_event, timeout)

    async def run_due(self) -> None:
        finished = await self._run_due_once()
        if not self._on_result:
            return
        for result in finished:
            try:
                await self._on_result(result)
            except Exception:
                logger.exception(
                    "quota refresh result callback failed provider=%s",
                    result.provider,
                )

    async def _run_due_once(self) -> list[QuotaRefreshResult]:
        # The state lock is only held while selecting work and applying results;
        # CLI requests run unlocked so manual triggers and toggles never wait on them.
        async with self._lock:
            due_by_provider = self._select_due()
            if not due_by_provider:
                return []
            now = self._clock()
            for provider, due_windows in due_by_provider.items():
                for key in due_windows:
                    self._state.quota_refresh_last_attempt_at[key] = now
                self._in_flight.add(provider)
            self._save_state()

        providers = list(due_by_provider)
        try:
            results = await asyncio.gather(
                *(self._client.request(provider) for provider in providers),
                return_exceptions=True,
            )
            async with self._lock:
                return self._apply_results(providers, results, due_by_provider)
        finally:
            for provider in providers:
                self._in_flight.discard(provider)

    def _select_due(self) -> dict[ProviderName, dict[str, float]]:
        if not self._state.proactive_quota_refresh_enabled:
            return {}

        now = self._clock()
        due_by_provider: dict[ProviderName, dict[str, float]] = {}
        for key, reset_at in self._state.quota_refresh_scheduled_at.items():
            provider = _provider_from_schedule_key(key)
            if provider is None or not self._provider_enabled[provider] or now < reset_at:
                continue
            if provider in self._in_flight:
                continue
            if self._state.quota_refresh_auth_blocked.get(provider):
                continue
            last_attempt = self._state.quota_refresh_last_attempt_at.get(key, 0.0)
            if now < last_attempt + self._retry_seconds:
                continue
            due_by_provider.setdefault(provider, {})[key] = reset_at
        return due_by_provider

    def _apply_results(
        self,
        providers: list[ProviderName],
        results: list[QuotaRefreshResult | BaseException],
        due_by_provider: dict[ProviderName, dict[str, float]],
    ) -> list[QuotaRefreshResult]:
        finished_at = self._clock()
        finished: list[QuotaRefreshResult] = []
        for provider, result in zip(providers, results, strict=True):
            if isinstance(result, BaseException):
                result = QuotaRefreshResult(
                    provider=provider,
                    ok=False,
                    error=result.__class__.__name__,
                )
            if result.ok:
                logger.info("proactive quota refresh succeeded provider=%s", provider)
                self._state.quota_refresh_last_success_at[provider] = finished_at
                self._state.quota_refresh_last_errors.pop(provider, None)
                for key, reset_at in due_by_provider[provider].items():
                    if self._state.quota_refresh_scheduled_at.get(key) == reset_at:
                        self._state.quota_refresh_scheduled_at.pop(key, None)
                        self._state.quota_refresh_last_attempt_at.pop(key, None)
                    self._state.quota_refresh_attempt_counts.pop(key, None)
            else:
                error = result.error or "unknown"
                attempt = 0
                gave_up = True
                for key in due_by_provider[provider]:
                    count = self._state.quota_refresh_attempt_counts.get(key, 0) + 1
                    attempt = max(attempt, count)
                    if count >= self._max_attempts:
                        self._state.quota_refresh_scheduled_at.pop(key, None)
                        self._state.quota_refresh_last_attempt_at.pop(key, None)
                        self._state.quota_refresh_attempt_counts.pop(key, None)
                    else:
                        self._state.quota_refresh_attempt_counts[key] = count
                        gave_up = False
                auth_blocked = is_auth_failure(error)
                if auth_blocked:
                    # Do not retry on a schedule the CLI cannot satisfy; requests resume
                    # automatically once a later check sees the login working again.
                    self._state.quota_refresh_auth_blocked[provider] = True
                result = replace(
                    result,
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    gave_up=gave_up,
                    auth_blocked=auth_blocked,
                )
                logger.warning(
                    "proactive quota refresh failed provider=%s attempt=%s/%s "
                    "gave_up=%s error=%s",
                    provider,
                    attempt,
                    self._max_attempts,
                    gave_up,
                    error,
                )
                self._state.quota_refresh_last_errors[provider] = error
            finished.append(result)
        self._save_state()
        return finished

    async def trigger_now(self, provider: ProviderName) -> QuotaRefreshResult:
        """Fire one request immediately, bypassing schedules; used for manual triggers."""

        async with self._lock:
            if provider in self._in_flight:
                return QuotaRefreshResult(
                    provider=provider,
                    ok=False,
                    error="already_running",
                )
            self._in_flight.add(provider)

        try:
            result = await self._client.request(provider)
            async with self._lock:
                finished_at = self._clock()
                if result.ok:
                    self._state.quota_refresh_last_success_at[provider] = finished_at
                    self._state.quota_refresh_last_errors.pop(provider, None)
                    if self._state.quota_refresh_auth_blocked.pop(provider, None):
                        self._wake_event.set()
                else:
                    self._state.quota_refresh_last_errors[provider] = result.error or "unknown"
                    if is_auth_failure(result.error):
                        self._state.quota_refresh_auth_blocked[provider] = True
                        result = replace(result, auth_blocked=True)
                self._save_state()
            return result
        finally:
            self._in_flight.discard(provider)

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._state.proactive_quota_refresh_enabled,
            "providers_enabled": dict(self._provider_enabled),
            "scheduled": dict(self._state.quota_refresh_scheduled_at),
            "attempt_counts": dict(self._state.quota_refresh_attempt_counts),
            "last_success": dict(self._state.quota_refresh_last_success_at),
            "last_errors": dict(self._state.quota_refresh_last_errors),
            "auth_blocked": dict(self._state.quota_refresh_auth_blocked),
            "retry_seconds": self._retry_seconds,
            "max_attempts": self._max_attempts,
        }

    def _seconds_until_next_attempt(self) -> float | None:
        if not self._state.proactive_quota_refresh_enabled:
            return None

        now = self._clock()
        next_attempt: float | None = None
        for key, reset_at in self._state.quota_refresh_scheduled_at.items():
            provider = _provider_from_schedule_key(key)
            if provider is None or not self._provider_enabled[provider]:
                continue
            if self._state.quota_refresh_auth_blocked.get(provider):
                continue
            last_attempt = self._state.quota_refresh_last_attempt_at.get(key, 0.0)
            eligible_at = max(reset_at, last_attempt + self._retry_seconds)
            if provider in self._in_flight:
                # A request for this provider is running right now; re-check shortly
                # instead of spinning on an already-due schedule.
                eligible_at = max(eligible_at, now + 5.0)
            next_attempt = eligible_at if next_attempt is None else min(next_attempt, eligible_at)
        return None if next_attempt is None else max(0.0, next_attempt - now)

    def _save_state(self) -> None:
        try:
            self._state.save(self._state_path)
        except Exception as exc:
            logger.warning("failed to save proactive quota refresh state: %s", exc)


def _schedule_key(provider: ProviderName, window: UsageWindow) -> str:
    return f"{provider}:{window.key}"


def _provider_from_schedule_key(key: str) -> ProviderName | None:
    provider, separator, _ = key.partition(":")
    if separator and provider in {"claude", "codex"}:
        return provider  # type: ignore[return-value]
    return None


async def _wait_for_stop_or_wake(
    stop_event: asyncio.Event,
    wake_event: asyncio.Event,
    timeout: float | None,
) -> None:
    stop_task = asyncio.create_task(stop_event.wait())
    wake_task = asyncio.create_task(wake_event.wait())
    tasks = {stop_task, wake_task}
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
