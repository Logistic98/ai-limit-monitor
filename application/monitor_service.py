from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from datetime import UTC, datetime

import httpx

from application.quota_refresh_service import ProactiveQuotaRefreshService
from config.settings import Settings
from domain.auth import LoginStartResult, Provider
from domain.models import CheckResult, ProviderUsage, UsageWindow
from infrastructure.auth.cli_login import CliLoginManager
from infrastructure.notifications.telegram import TelegramClient
from infrastructure.providers.claude import ClaudeUsageClient
from infrastructure.providers.codex import CodexUsageClient
from infrastructure.providers.quota_refresh import QuotaRefreshClient, QuotaRefreshResult
from infrastructure.storage.json_state import MonitorState
from presentation.telegram_formatter import (
    format_diagnostics,
    format_error,
    format_login_complete,
    format_login_required,
    format_login_start,
    format_login_status,
    format_quota_refresh_result,
    format_quota_refresh_status,
    format_recovery,
    format_report,
)

logger = logging.getLogger(__name__)

# Old alert keys ended with the window's reset marker: "provider:window_key:<epoch|unknown>".
_LEGACY_ALERT_KEY_PATTERN = re.compile(r"^(claude|codex):.+:(\d+|unknown)$")


class LimitMonitor:
    """Coordinates provider usage checks and Telegram notifications."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        timeout = httpx.Timeout(settings.http_timeout_seconds)
        self._http = httpx.AsyncClient(timeout=timeout)
        self._telegram = TelegramClient(settings, self._http)
        self._login_manager = CliLoginManager()
        self._claude = ClaudeUsageClient(settings, self._http)
        self._codex = CodexUsageClient(settings, self._http)
        self._state = MonitorState.load(settings.state_path)
        self._quota_refresh = ProactiveQuotaRefreshService(
            self._state,
            settings.state_path,
            QuotaRefreshClient(),
            claude_enabled=settings.claude_enabled,
            codex_enabled=settings.codex_enabled,
            on_result=self._on_quota_refresh_result,
        )
        self._check_lock = asyncio.Lock()
        self._manual_refresh_running: set[str] = set()

        # Drop alert keys written by the earlier reset-timestamp key format; Claude's
        # resets_at jitters between checks, which made those keys re-alert repeatedly.
        for legacy_key in [
            key for key in self._state.alert_levels if _LEGACY_ALERT_KEY_PATTERN.match(key)
        ]:
            self._state.alert_levels.pop(legacy_key, None)

    async def close(self) -> None:
        await self._http.aclose()

    async def check(self) -> CheckResult:
        async with self._check_lock:
            logger.info("starting usage check")
            captured_at = datetime.now(tz=UTC)
            providers = await asyncio.gather(
                self._claude.fetch(captured_at),
                self._codex.fetch(captured_at),
            )
            result = CheckResult(captured_at=captured_at, providers=list(providers))
            logger.info(
                "usage check completed providers=%s",
                ",".join(f"{p.provider}:{'ok' if p.ok else p.error_kind}" for p in providers),
            )
            return result

    async def check_and_notify(self, *, force_report: bool = False) -> CheckResult:
        result = await self.check()
        self._quota_refresh.observe(result)
        await self._send_state_notifications(result, suppress_auth_required=force_report)
        if force_report:
            logins = await self._start_login_flows_for_report(result)
            await self._telegram.send_message(format_report(result, self._settings, logins))
        try:
            self._state.save(self._settings.state_path)
        except Exception as exc:
            logger.warning("failed to save monitor state: %s", exc)
        return result

    async def _on_quota_refresh_result(self, result: QuotaRefreshResult) -> None:
        await self._telegram.send_message(
            format_quota_refresh_result(
                result.provider,
                result.ok,
                result.error,
                attempt=result.attempt,
                max_attempts=result.max_attempts,
                gave_up=result.gave_up,
            )
        )
        if not result.ok:
            return
        check, logins = await self._monitor_once_for_command()
        await self._telegram.send_message(format_report(check, self._settings, logins))

    async def send_test_message(self) -> None:
        await self._telegram.send_message(
            "AI订阅资源限额监控 is running. Use /help to view commands."
        )

    async def run_forever(self) -> None:
        logger.info("starting ai-limit-monitor service")
        stop_event = asyncio.Event()
        tasks = [
            asyncio.create_task(self._monitor_loop(stop_event), name="monitor-loop"),
            asyncio.create_task(
                self._telegram.poll_commands(self.handle_command, stop_event),
                name="telegram-polling",
            ),
            asyncio.create_task(
                self._login_completion_loop(stop_event),
                name="login-completion-loop",
            ),
            asyncio.create_task(
                self._quota_refresh.run_loop(stop_event),
                name="quota-refresh-loop",
            ),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            stop_event.set()
            for task in tasks:
                task.cancel()

    async def handle_command(self, chat_id: str, text: str) -> str | None:
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        logger.info("handling command=%s chat_id=%s", command, chat_id)
        if command in {"/start", "/help"}:
            return self._help_message()
        if command == "/ping":
            return "pong"
        if command == "/check":
            result = await self.check()
            return format_login_status(result, self._settings)
        if command == "/monitor":
            result, logins = await self._monitor_once_for_command()
            return format_report(result, self._settings, logins)
        if command in {"/diagnose", "/debug"}:
            result = await self.check()
            claude_auth = self._claude.diagnose_auth(result.captured_at)
            codex_auth = self._codex.diagnose_auth(result.captured_at)
            return format_diagnostics(
                result,
                self._settings,
                claude_auth,
                codex_auth,
                self._quota_refresh.status_snapshot(),
            )
        if command in {"/login", "/login_claude", "/login_codex"}:
            return await self._handle_login_command(command, text)
        if command in {"/login-code", "/login_code"}:
            return await self._handle_login_code_command(text)
        if command == "/quota_refresh":
            return await self._handle_quota_refresh_command(chat_id, text)
        return "Unknown command. Use /help."

    def _help_message(self) -> str:
        return (
            "<b>AI订阅资源限额监控</b>\n"
            "命令：\n"
            "/check - 检查 Claude 和 Codex 登录状态\n"
            "/monitor - 立即监控并返回完整报告\n"
            "/diagnose - 输出脱敏诊断信息\n"
            "/login_claude - 生成 Claude 登录链接\n"
            "/login_codex - 生成 Codex 登录链接和验证码\n"
            "/login_code claude CODE - 提交 Claude 登录页返回的 code\n"
            "/quota_refresh on|off - 查看或切换限额恢复主动请求\n"
            "/quota_refresh claude|codex - 立即手动发起一次限额恢复请求\n"
            "/ping - 健康检查"
        )

    async def _handle_quota_refresh_command(self, chat_id: str, text: str) -> str:
        parts = text.split(maxsplit=1)
        if len(parts) == 1 or parts[1].strip().lower() == "status":
            return format_quota_refresh_status(
                self._quota_refresh.status_snapshot(), self._settings
            )

        option = parts[1].strip().lower()
        if option in {"claude", "codex"}:
            label = "Claude" if option == "claude" else "Codex"
            if option in self._manual_refresh_running:
                return f"{label} 手动限额恢复请求正在执行中，请等待结果。"
            self._manual_refresh_running.add(option)
            try:
                await self._telegram.send_message(
                    f"已发起 {label} 限额恢复请求，最长约 2 分钟，完成后会推送结果。",
                    chat_id=chat_id,
                )
                result = await self._quota_refresh.trigger_now(option)  # type: ignore[arg-type]
            finally:
                self._manual_refresh_running.discard(option)
            if not result.ok and result.error == "already_running":
                return f"{label} 的限额恢复请求正在执行中（自动调度已触发），请等待结果。"
            if result.ok:
                check, logins = await self._monitor_once_for_command()
                await self._telegram.send_message(
                    format_quota_refresh_result(result.provider, result.ok, result.error),
                    chat_id=chat_id,
                )
                return format_report(check, self._settings, logins)
            return format_quota_refresh_result(result.provider, result.ok, result.error)

        if option not in {"on", "off"}:
            return "用法：/quota_refresh on|off ｜ 手动触发：/quota_refresh claude|codex"

        previous = self._state.proactive_quota_refresh_enabled
        enabled = option == "on"
        self._state.proactive_quota_refresh_enabled = enabled
        try:
            self._state.save(self._settings.state_path)
        except Exception as exc:
            self._state.proactive_quota_refresh_enabled = previous
            logger.warning("failed to save quota refresh setting: %s", exc)
            return "限额恢复主动请求设置保存失败，请查看容器日志。"

        self._quota_refresh.notify_setting_changed()
        status = "已开启" if enabled else "已关闭"
        return f"限额恢复主动请求：{status}"

    async def _handle_login_command(self, command: str, text: str) -> str:
        provider: Provider | None = None
        if command == "/login_claude":
            provider = "claude"
        elif command == "/login_codex":
            provider = "codex"
        else:
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip().lower() in {"claude", "codex"}:
                provider = parts[1].strip().lower()  # type: ignore[assignment]

        if provider is None:
            return "用法：/login_claude 或 /login_codex"

        logger.info("starting login flow provider=%s", provider)
        login = await self._login_manager.start(provider)
        return format_login_start(login, self._settings)

    async def _handle_login_code_command(self, text: str) -> str:
        parts = text.split(maxsplit=2)
        if len(parts) != 3 or parts[1].lower() != "claude":
            return "用法：/login_code claude YOUR_CODE"
        logger.info("submitting claude login code")
        result = await self._login_manager.submit_code("claude", parts[2])
        return format_login_complete(result)

    async def _monitor_once_for_command(self) -> tuple[CheckResult, dict[str, LoginStartResult]]:
        result = await self.check()
        self._quota_refresh.observe(result)
        await self._send_state_notifications(result, suppress_auth_required=True)
        logins = await self._start_login_flows_for_report(result)
        if logins:
            logger.info("started login flows for command report providers=%s", ",".join(logins))
        try:
            self._state.save(self._settings.state_path)
        except Exception as exc:
            logger.warning("failed to save monitor state: %s", exc)
        return result, logins

    async def _monitor_loop(self, stop_event: asyncio.Event) -> None:
        logger.info("monitor loop started")
        if self._settings.send_startup_report:
            with suppress(Exception):
                await self.check_and_notify(force_report=True)

        while not stop_event.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._settings.check_interval_seconds
                )
            if stop_event.is_set():
                break
            try:
                await self._send_completed_login_notifications()
                await self.check_and_notify()
            except Exception:
                logger.exception("monitor loop iteration failed")
                await asyncio.sleep(5)

    async def _login_completion_loop(self, stop_event: asyncio.Event) -> None:
        logger.info("login completion loop started")
        while not stop_event.is_set():
            try:
                await self._send_completed_login_notifications()
            except Exception:
                logger.exception("failed to poll completed login flows")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=5)

    async def _send_completed_login_notifications(self) -> None:
        for result in await self._login_manager.poll_completed():
            logger.info("login flow completed provider=%s ok=%s", result.provider, result.ok)
            await self._telegram.send_message(format_login_complete(result))

    async def _start_login_flows_for_report(
        self,
        result: CheckResult,
    ) -> dict[str, LoginStartResult]:
        logins = {}
        for provider in result.providers:
            if provider.error_kind == "auth_required" and provider.provider in {"claude", "codex"}:
                login = await self._login_manager.start(provider.provider)
                logins[provider.provider] = login
        return logins

    async def _send_state_notifications(
        self,
        result: CheckResult,
        *,
        suppress_auth_required: bool = False,
    ) -> None:
        threshold_crossed = False
        for provider in result.providers:
            await self._send_error_or_recovery(
                provider,
                suppress_auth_required=suppress_auth_required,
            )
            if not provider.ok:
                continue
            for window in provider.windows:
                threshold = self._current_threshold(window.used_percent)
                key = _alert_key(provider, window)
                previous = self._state.alert_levels.get(key, 0.0)
                if threshold > previous:
                    threshold_crossed = True
                if threshold != previous:
                    self._state.alert_levels[key] = threshold

        if threshold_crossed:
            await self._telegram.send_message(format_report(result, self._settings))

    async def _send_error_or_recovery(
        self,
        provider: ProviderUsage,
        *,
        suppress_auth_required: bool = False,
    ) -> None:
        was_error = self._state.provider_errors.get(provider.provider, False)
        previous_kind = self._state.provider_error_kinds.get(provider.provider)
        current_kind = provider.error_kind or "unknown"

        if provider.ok and was_error:
            await self._telegram.send_message(format_recovery(provider, self._settings))
            self._state.provider_errors[provider.provider] = False
            self._state.provider_error_kinds.pop(provider.provider, None)
        elif not provider.ok and (not was_error or previous_kind != current_kind):
            if provider.error_kind == "auth_required":
                login = None
                if provider.provider in {"claude", "codex"}:
                    login = await self._login_manager.start(provider.provider)
                message = format_login_required(provider, self._settings, login)
            else:
                message = format_error(provider, self._settings)
            if not (suppress_auth_required and provider.error_kind == "auth_required"):
                await self._telegram.send_message(message)
            self._state.provider_errors[provider.provider] = True
            self._state.provider_error_kinds[provider.provider] = current_kind

    def _current_threshold(self, used_percent: float) -> float:
        level = 0.0
        for threshold in self._settings.alert_thresholds:
            if used_percent >= threshold:
                level = threshold
        return level


def _alert_key(provider: ProviderUsage, window: UsageWindow) -> str:
    # resets_at is deliberately excluded: Claude's reported reset time jitters between
    # checks, and a key change would re-fire the same threshold alert. When the window
    # actually resets, used_percent drops and the stored level is lowered instead.
    return f"{provider.provider}:{window.key}"
