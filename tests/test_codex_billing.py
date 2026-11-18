from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import ValidationError

from application.monitor_service import LimitMonitor
from config.settings import Settings
from domain.models import CheckResult, CodexSubscription, ProviderUsage
from infrastructure.providers.codex import CodexSubscriptionError, CodexUsageClient
from infrastructure.storage.json_state import MonitorState

_NOW = datetime(2026, 11, 21, 8, 23, tzinfo=UTC)
_RENEWS = _NOW + timedelta(days=7)


def _settings(**overrides: object) -> Settings:
    values = {
        "telegram_bot_token": "test-token",
        "telegram_chat_id": "1",
        "codex_access_token": "test-access-token",
        "codex_account_id": "selected",
        "timezone": "Asia/Shanghai",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def _account_response() -> dict:
    return {
        "accounts": {
            "selected": {
                "account": {"account_id": "selected"},
                "entitlement": {
                    "has_active_subscription": True,
                    "is_delinquent": False,
                    "cancels_at": None,
                    "renews_at": _RENEWS.isoformat(),
                    "expires_at": (_RENEWS + timedelta(hours=6)).isoformat(),
                },
                "last_active_subscription": {"will_renew": True},
            },
        },
    }


class CodexBillingClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_selects_current_account_and_uses_renewal_field(self) -> None:
        payload = _account_response()
        selected = payload["accounts"]["selected"]
        payload["accounts"]["default"] = {"account": {"account_id": "different"}}

        def respond(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/backend-api/accounts/check/v4-2023-04-27")
            self.assertEqual(request.headers["chatgpt-account-id"], "selected")
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = CodexUsageClient(_settings(), http)
            subscription = await client.fetch_subscription(_NOW)
            self.assertEqual(subscription.renews_at, _RENEWS)
            self.assertEqual(subscription.remaining_days(_NOW), 7)

            selected["entitlement"]["cancels_at"] = _RENEWS.isoformat()
            subscription = await client.fetch_subscription(_NOW)
            self.assertIsNone(subscription.renews_at)
            self.assertEqual(subscription.cancels_at, _RENEWS)
            self.assertFalse(subscription.will_renew)

            selected["account"]["account_id"] = "different"
            with self.assertRaises(CodexSubscriptionError):
                await client.fetch_subscription(_NOW)

    async def test_concurrent_readers_reuse_rotated_credentials(self) -> None:
        with TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(json.dumps({
                "tokens": {
                    "account_id": "selected",
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                },
            }))
            refreshes = 0
            old_requests = 0
            both_started = asyncio.Event()

            async def respond(request: httpx.Request) -> httpx.Response:
                nonlocal old_requests, refreshes
                if request.method == "POST":
                    refreshes += 1
                    await asyncio.sleep(0)
                    return httpx.Response(
                        200, json={"access_token": "new-access", "refresh_token": "new-refresh"},
                    )
                if request.headers["authorization"] == "Bearer old-access":
                    old_requests += 1
                    if old_requests == 2:
                        both_started.set()
                    await both_started.wait()
                    return httpx.Response(401)
                return httpx.Response(200, json=_account_response())

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                settings = _settings(codex_access_token=None, codex_auth_path=auth_path)
                client = CodexUsageClient(settings, http)
                second_client = CodexUsageClient(settings, http)
                async with asyncio.timeout(5):
                    results = await asyncio.gather(
                        client.fetch_subscription(_NOW), second_client.fetch_subscription(_NOW),
                    )
                self.assertEqual([result.renews_at for result in results], [_RENEWS, _RENEWS])
                self.assertEqual(refreshes, 1)


class CodexBillingMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_path = Path(directory.name) / "state.json"
        self.monitor = LimitMonitor(_settings(state_path=self.state_path))
        self.addAsyncCleanup(self.monitor.close)
        self.monitor._telegram.send_message = AsyncMock()

    async def test_each_reminder_threshold_and_restart_deduplication(self) -> None:
        subscription = CodexSubscription("selected", True, True, _RENEWS)
        self.monitor._codex.fetch_subscription = AsyncMock(return_value=subscription)
        send = self.monitor._telegram.send_message
        with patch("application.monitor_service.datetime") as clock:
            clock.now.return_value = _RENEWS - timedelta(days=14, seconds=30)
            self.assertEqual(await self.monitor._check_codex_renewal_reminder(), 30)
            send.assert_not_called()
            for days in (14, 7, 3, 1):
                clock.now.return_value = _RENEWS - timedelta(days=days)
                await self.monitor._check_codex_renewal_reminder()
                self.assertIn(f"{days} 天提醒", send.call_args.args[0])
                self.monitor._state = MonitorState.load(self.state_path)
                await self.monitor._check_codex_renewal_reminder()
            clock.now.return_value = _RENEWS
            await self.monitor._check_codex_renewal_reminder()
        self.assertEqual(send.await_count, 4)

    async def test_catch_up_retries_a_failed_send_and_resets_for_new_cycle(self) -> None:
        fetch = AsyncMock(return_value=CodexSubscription("selected", True, True, _RENEWS))
        self.monitor._codex.fetch_subscription = fetch
        send = self.monitor._telegram.send_message
        send.side_effect = [RuntimeError(), None, None]
        with patch("application.monitor_service.datetime") as clock:
            clock.now.return_value = _RENEWS - timedelta(days=2)
            await self.monitor._check_codex_renewal_reminder()
            self.assertFalse(self.state_path.exists())
            await self.monitor._check_codex_renewal_reminder()
            await self.monitor._check_codex_renewal_reminder()
            self.assertEqual(send.await_count, 2)
            self.assertIn("3 天提醒", send.call_args.args[0])

            renewed = _RENEWS + timedelta(days=30)
            fetch.return_value = CodexSubscription("selected", True, True, renewed)
            clock.now.return_value = renewed - timedelta(days=14)
            await self.monitor._check_codex_renewal_reminder()
            self.assertIn("14 天提醒", send.call_args.args[0])
            self.assertEqual(send.await_count, 3)

    async def test_monitor_and_diagnose_include_subscription(self) -> None:
        result = CheckResult(_NOW, [ProviderUsage("codex", True, _NOW)])
        self.monitor.check = AsyncMock(return_value=result)
        self.monitor._monitor_once_for_command = AsyncMock(return_value=(result, {}))
        self.monitor._reset_credits.statistics = AsyncMock(return_value="重置卡")
        self.monitor._reset_credits.diagnostics = AsyncMock(return_value="重置卡")
        self.monitor._codex.diagnose_auth = lambda _: {"enabled": True}
        self.monitor._codex.fetch_subscription = AsyncMock(
            return_value=CodexSubscription("selected", True, True, _RENEWS),
        )

        with patch("application.monitor_service.datetime") as clock:
            clock.now.return_value = _NOW
            for command in ("/monitor", "/diagnose"):
                reply = await self.monitor.handle_command("1", command)
                self.assertIn("2026-11-28 16:23", reply)
                self.assertIn("（剩余 7 天）", reply)
            self.monitor._codex.fetch_subscription.side_effect = CodexSubscriptionError("不可用")
            self.assertIn("查询失败", await self.monitor.handle_command("1", "/monitor"))

    def test_reminder_configuration(self) -> None:
        self.assertEqual(_settings().codex_renewal_reminder_days, [1, 3, 7, 14])
        self.assertEqual(
            _settings(codex_renewal_reminder_days="7, 1, 7").codex_renewal_reminder_days, [1, 7],
        )
        self.assertEqual(_settings(codex_renewal_reminder_days="").codex_renewal_reminder_days, [])
        with self.assertRaises(ValidationError):
            _settings(codex_renewal_reminder_days="0")
