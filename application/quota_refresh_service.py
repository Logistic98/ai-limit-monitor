from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from domain.models import CheckResult, ProviderName, UsageWindow
from infrastructure.providers.quota_refresh import QuotaRefreshClient, QuotaRefreshResult
from infrastructure.storage.json_state import MonitorState

logger = logging.getLogger(__name__)

DEFAULT_RETRY_SECONDS = 15 * 60


class ProactiveQuotaRefreshService:
    """Requests a provider immediately after any triggered limit resets."""

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
        self._lock = asyncio.Lock()
        self._wake_event = asyncio.Event()

        # Discard schedules written by the earlier provider-wide 5h-only implementation.
        for legacy_key in ("claude", "codex"):
            self._state.quota_refresh_scheduled_at.pop(legacy_key, None)
            self._state.quota_refresh_last_attempt_at.pop(legacy_key, None)

    def observe(self, result: CheckResult) -> None:
        """Schedule every window that has reached its limit and exposes a reset time."""

        changed = False
        for provider in result.providers:
            if not provider.ok or not self._provider_enabled.get(provider.provider, False):
                continue
            for window in provider.windows:
                if not _limit_triggered(window) or not window.resets_at:
                    continue
                key = _schedule_key(provider.provider, window)
                reset_at = window.resets_at.timestamp()
                if self._state.quota_refresh_scheduled_at.get(key) != reset_at:
                    self._state.quota_refresh_scheduled_at[key] = reset_at
                    self._state.quota_refresh_last_attempt_at.pop(key, None)
                    changed = True

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
        async with self._lock:
            if not self._state.proactive_quota_refresh_enabled:
                return

            now = self._clock()
            due_by_provider: dict[ProviderName, dict[str, float]] = {}
            for key, reset_at in self._state.quota_refresh_scheduled_at.items():
                provider = _provider_from_schedule_key(key)
                if provider is None or not self._provider_enabled[provider] or now < reset_at:
                    continue
                last_attempt = self._state.quota_refresh_last_attempt_at.get(key, 0.0)
                if now < last_attempt + self._retry_seconds:
                    continue
                due_by_provider.setdefault(provider, {})[key] = reset_at

            if not due_by_provider:
                return

            for due_windows in due_by_provider.values():
                for key in due_windows:
                    self._state.quota_refresh_last_attempt_at[key] = now
            self._save_state()

            providers = list(due_by_provider)
            results = await asyncio.gather(
                *(self._client.request(provider) for provider in providers),
                return_exceptions=True,
            )
            finished_at = self._clock()
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
                else:
                    error = result.error or "unknown"
                    logger.warning(
                        "proactive quota refresh failed provider=%s error=%s",
                        provider,
                        error,
                    )
                    self._state.quota_refresh_last_errors[provider] = error
            self._save_state()

    def _seconds_until_next_attempt(self) -> float | None:
        if not self._state.proactive_quota_refresh_enabled:
            return None

        now = self._clock()
        next_attempt: float | None = None
        for key, reset_at in self._state.quota_refresh_scheduled_at.items():
            provider = _provider_from_schedule_key(key)
            if provider is None or not self._provider_enabled[provider]:
                continue
            last_attempt = self._state.quota_refresh_last_attempt_at.get(key, 0.0)
            eligible_at = max(reset_at, last_attempt + self._retry_seconds)
            next_attempt = eligible_at if next_attempt is None else min(next_attempt, eligible_at)
        return None if next_attempt is None else max(0.0, next_attempt - now)

    def _save_state(self) -> None:
        try:
            self._state.save(self._state_path)
        except Exception as exc:
            logger.warning("failed to save proactive quota refresh state: %s", exc)


def _limit_triggered(window: UsageWindow) -> bool:
    return bool(window.limit_reached) or window.used_percent >= 100.0


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
