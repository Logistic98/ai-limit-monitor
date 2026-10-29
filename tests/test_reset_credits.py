from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from application.monitor_service import LimitMonitor
from application.reset_credit_service import ResetCreditService
from cli import build_parser
from infrastructure.notifications.telegram import _safe_text
from infrastructure.providers.codex_reset_credits import CodexResetCreditClient
from infrastructure.storage.json_state import MonitorState
from presentation.reset_credits_formatter import format_reset_credits


def card(credit_id: str, expiry: float | None) -> dict:
    return {
        "id": credit_id, "expiresAt": expiry, "grantedAt": 1700000000,
        "status": "available", "resetType": "codexRateLimits",
    }


class ResetCreditTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.now = 1800000000.0
        self.expiry = self.now + 100000
        self.summary = {"availableCount": 1, "credits": [card("card-1", self.expiry)]}
        self.client = SimpleNamespace(
            read=AsyncMock(side_effect=lambda: self.summary),
            consume=AsyncMock(return_value="reset"),
        )
        self.notify = AsyncMock()
        self.service = self.new_service()

    def new_service(self) -> ResetCreditService:
        return ResetCreditService(
            self.path, self.client, self.notify, timezone="Asia/Shanghai",
            clock=lambda: self.now,
        )

    def confirmation_token(self, context: str = "cli") -> str:
        state = MonitorState.load(self.path.with_suffix(".reset-credits.json"))
        return next(
            job["confirmation"]["token"] for job in state.reset_credit_jobs.values()
            if job.get("confirmation", {}).get("context") == context
        )

    async def test_schedule_remind_day_before_use_and_consume_one_hour_before_expiry(self):
        report = await self.service.query()
        self.assertIn("可用数量：1", report)
        self.assertEqual(self.notify.await_count, 1)
        self.assertIn("已创建", self.notify.call_args.args[0])
        self.now = self.expiry - 90001
        await self.service.run_due()
        self.assertEqual(self.notify.await_count, 1)
        self.now += 1
        await self.service.run_due()
        self.assertIn("使用前提醒", self.notify.call_args.args[0])
        self.now = self.expiry - 3601
        await self.service.run_due()
        self.client.consume.assert_not_awaited()
        self.now += 1
        await self.service.run_due()
        self.assertEqual(self.client.consume.call_args.args[0], "card-1")
        self.assertIn("使用成功", self.notify.call_args.args[0])
        await self.new_service().run_due()
        self.assertEqual(self.client.consume.await_count, 1)
        self.assertEqual(self.notify.await_count, 3)

    async def test_late_discovery_warns_immediately_and_expired_unknown_cards_are_not_used(self):
        self.summary["credits"] = [
            card("soon", self.now + 1800), card("expired", self.now - 1),
            card("no-expiry", None), {**card("unknown", self.now + 100), "status": "unknown"},
        ]
        await self.service.run_due()
        self.assertEqual(self.client.consume.await_count, 1)
        self.assertEqual(self.client.consume.call_args.args[0], "soon")
        messages = [call.args[0] for call in self.notify.call_args_list]
        self.assertTrue(any("使用前提醒" in text for text in messages))
        self.assertTrue(any("已到期" in text for text in messages))

    async def test_disable_survives_restart_and_manual_use_picks_earliest(self):
        self.summary["credits"] = [card("later", self.now + 3000), card("first", self.now + 100)]
        await self.service.set_enabled(False)
        self.service = self.new_service()
        await self.service.run_due()
        self.client.consume.assert_not_awaited()
        prompt = await self.service.use()
        self.assertIn("二次确认", prompt)
        self.client.consume.assert_not_awaited()
        await self.service.use(confirmation=self.confirmation_token())
        self.assertEqual(self.client.consume.call_args.args[0], "first")

    async def test_confirmation_survives_restart_and_does_not_select_a_different_card(self):
        prompt = await self.service.use(context="telegram:1")
        self.assertIn("card-1", prompt)
        self.assertIn("/reset_codex_use confirm", prompt)
        token = self.confirmation_token("telegram:1")
        self.client.consume.assert_not_awaited()
        self.summary["credits"].append(card("earlier", self.now + 1000))
        await self.new_service().use(context="telegram:1", confirmation=token)
        self.assertEqual(self.client.consume.call_args.args[0], "card-1")

    async def test_wrong_chat_wrong_code_and_expired_code_cannot_use_a_card(self):
        await self.service.use(context="telegram:1")
        token = self.confirmation_token("telegram:1")
        self.assertIn("无效", await self.service.use(context="telegram:2", confirmation=token))
        self.assertIn("无效", await self.service.use(context="telegram:1", confirmation="wrong"))
        self.now += 300
        self.assertIn("过期", await self.service.use(context="telegram:1", confirmation=token))
        self.client.consume.assert_not_awaited()

    async def test_confirmation_is_one_time_even_with_concurrent_requests(self):
        await self.service.use()
        token = self.confirmation_token()
        replies = await asyncio.gather(
            self.service.use(confirmation=token), self.new_service().use(confirmation=token),
        )
        self.assertEqual(self.client.consume.await_count, 1)
        self.assertTrue(any("无效" in reply for reply in replies))

    async def test_new_request_replaces_old_confirmation(self):
        self.summary["credits"].append(card("card-2", self.now + 1000))
        await self.service.use("card-1")
        old = self.confirmation_token()
        await self.service.use("card-2")
        new = self.confirmation_token()
        self.assertNotEqual(old, new)
        self.assertIn("无效", await self.service.use(confirmation=old))
        self.client.consume.assert_not_awaited()
        await self.service.use(confirmation=new)
        self.assertEqual(self.client.consume.call_args.args[0], "card-2")

    async def test_missing_confirmed_card_never_falls_back_to_another(self):
        await self.service.use()
        token = self.confirmation_token()
        self.summary["credits"] = [card("other", self.now + 1000)]
        self.assertIn("没有找到", await self.service.use(confirmation=token))
        self.client.consume.assert_not_awaited()

    async def test_confirmation_save_failure_prevents_use(self):
        await self.service.use()
        token = self.confirmation_token()
        with (
            patch.object(self.service, "_save", side_effect=OSError("read only")),
            self.assertRaises(OSError),
        ):
            await self.service.use(confirmation=token)
        self.client.consume.assert_not_awaited()

    async def test_retries_uncertain_result_with_same_key_after_restart_even_past_expiry(self):
        self.summary["credits"] = [card("card-1", self.now + 100)]
        self.client.consume.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.service.run_due()
        key = self.client.consume.call_args.args[1]
        self.now += 301
        self.summary = {"availableCount": 0, "credits": []}
        self.client.consume.side_effect = None
        self.client.consume.return_value = "alreadyRedeemed"
        await self.new_service().run_due()
        self.assertEqual(self.client.consume.call_args.args, ("card-1", key))
        self.assertIn("此前已使用成功", self.notify.call_args.args[0])

    async def test_no_eligible_window_is_not_success_and_retries_after_cooldown(self):
        self.summary["credits"] = [card("card-1", self.now + 1000)]
        self.client.consume.return_value = "nothingToReset"
        await self.service.run_due()
        first_key = self.client.consume.call_args.args[1]
        self.assertIn("卡片未使用", self.notify.call_args.args[0])
        self.now += 299
        await self.service.run_due()
        self.assertEqual(self.client.consume.await_count, 1)
        self.now += 1
        await self.service.run_due()
        self.assertEqual(self.client.consume.await_count, 2)
        self.assertNotEqual(first_key, self.client.consume.call_args.args[1])
        self.assertEqual(self.notify.await_count, 3)

    async def test_storage_failure_prevents_redemption(self):
        self.summary["credits"] = [card("card-1", self.now + 100)]
        with (
            patch.object(MonitorState, "save", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            await self.service.run_due()
        self.client.consume.assert_not_awaited()

    async def test_result_notification_retries_without_redeeming_again(self):
        self.summary["credits"] = [card("card-1", self.now + 100)]
        await self.service.query()
        self.notify.side_effect = RuntimeError("telegram down")
        await self.service.run_due()
        self.notify.side_effect = None
        await self.new_service().run_due()
        self.assertEqual(self.client.consume.await_count, 1)
        self.assertIn("使用成功", self.notify.call_args.args[0])

    async def test_two_service_instances_cannot_redeem_the_same_card_twice(self):
        self.summary["credits"] = [card("card-1", self.now + 100)]
        await asyncio.gather(self.service.run_due(), self.new_service().run_due())
        self.assertEqual(self.client.consume.await_count, 1)

    async def test_no_details_is_not_zero_and_manual_unknown_id_is_rejected(self):
        self.summary = {"availableCount": 5, "credits": None}
        report = await self.service.query()
        self.assertIn("可用数量：5", report)
        self.assertIn("明细尚未提供", report)
        self.assertIn("没有找到", await self.service.use("unknown"))
        self.client.consume.assert_not_awaited()
        self.assertIn("无法确认数量", format_reset_credits(None, True, "Asia/Shanghai"))

    async def test_original_monitor_state_is_untouched(self):
        original = MonitorState(quota_refresh_scheduled_at={"claude:five_hour": 123})
        original.save(self.path)
        before = self.path.read_bytes()
        await self.service.query()
        self.assertEqual(before, self.path.read_bytes())

    async def test_telegram_outage_does_not_prevent_due_use_or_cause_busy_loop(self):
        self.summary["credits"] = [card("card-1", self.now + 100)]
        self.notify.side_effect = RuntimeError("offline")
        await self.service.run_due()
        self.assertEqual(self.client.consume.await_count, 1)
        self.summary["credits"] = [card("next", self.now + 10000)]
        await self.service.run_due()
        self.assertGreaterEqual(self.service._next_delay(), 1)

    async def test_reset_scheduler_storage_error_does_not_crash_monitor(self):
        stop = asyncio.Event()

        async def stop_after_error(*args):
            stop.set()

        with (
            patch.object(Path, "mkdir", side_effect=OSError("read only")),
            patch("application.reset_credit_service._wait_for_stop_or_wake", stop_after_error),
        ):
            await self.service.run_loop(stop)
        self.client.consume.assert_not_awaited()


class ResetCreditIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_handshake_filters_notifications_and_cleans_up_process(self):
        script = '''
import json, sys, time
assert json.loads(sys.stdin.readline())["method"] == "initialize"
print('{"id":1,"result":{}}', flush=True)
assert json.loads(sys.stdin.readline())["method"] == "initialized"
request = json.loads(sys.stdin.readline())
print('{"method":"account/updated","params":{}}', flush=True)
print(json.dumps({"id":request["id"], "result":{"rateLimitResetCredits":{
    "availableCount":2,"credits":[]}}}), flush=True)
time.sleep(30)
'''
        create = asyncio.create_subprocess_exec
        processes = []

        async def spawn(*args, **kwargs):
            self.assertEqual(args[:2], ("codex", "app-server"))
            self.assertTrue(kwargs["env"]["CODEX_HOME"].endswith(".codex"))
            process = await create(sys.executable, "-c", script, **kwargs)
            processes.append(process)
            return process

        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            summary = await CodexResetCreditClient(Path("/tmp/.codex/auth.json")).read()
        self.assertEqual(summary["availableCount"], 2)
        self.assertIsNotNone(processes[0].returncode)

    async def test_timeout_terminates_rpc_process(self):
        create = asyncio.create_subprocess_exec
        processes = []

        async def spawn(*args, **kwargs):
            process = await create(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
            processes.append(process)
            return process

        with (
            patch("asyncio.create_subprocess_exec", side_effect=spawn),
            self.assertRaises(TimeoutError),
        ):
            await CodexResetCreditClient(None, timeout_seconds=0.05).read()
        self.assertIsNotNone(processes[0].returncode)

    async def test_rpc_always_targets_requested_card_and_key(self):
        client = CodexResetCreditClient(None)
        rpc = AsyncMock(return_value={"outcome": "reset"})
        with patch.object(client, "_request", new=rpc):
            self.assertEqual(await client.consume("card-1", "key-1"), "reset")
        rpc.assert_awaited_once_with(
            "account/rateLimitResetCredit/consume",
            {"creditId": "card-1", "idempotencyKey": "key-1"},
        )

    async def test_read_rejects_malformed_count_or_expiry(self):
        client = CodexResetCreditClient(None)
        for summary in [
            {"availableCount": True},
            {"availableCount": 1, "credits": [card("one", "not a timestamp")]},
        ]:
            with patch.object(client, "_request", new=AsyncMock(return_value={
                "rateLimitResetCredits": summary,
            })), self.assertRaises(ValueError):
                await client.read()

    async def test_telegram_and_cli_manual_routes(self):
        monitor = object.__new__(LimitMonitor)
        monitor._settings = SimpleNamespace(codex_enabled=True)
        monitor._reset_credits = SimpleNamespace(
            query=AsyncMock(return_value="cards"), use=AsyncMock(return_value="used"),
            set_enabled=AsyncMock(return_value="off"),
        )
        self.assertEqual(await monitor.handle_command("1", "/reset_codex_cards@Bot"), "cards")
        self.assertEqual(await monitor.handle_command("1", "/reset_codex_use"), "used")
        monitor._reset_credits.use.assert_awaited_once_with(context="telegram:1")
        self.assertEqual(
            await monitor.handle_command("1", "/reset_codex_use@Bot confirm TOKEN"), "used",
        )
        monitor._reset_credits.use.assert_awaited_with(
            context="telegram:1", confirmation="TOKEN",
        )
        self.assertEqual(await monitor.handle_command("1", "/reset_codex_auto off"), "off")
        monitor._reset_credits.set_enabled.assert_awaited_once_with(False)
        args = build_parser().parse_args(["reset-codex-use"])
        self.assertEqual(args.command, "reset-codex-use")
        self.assertEqual(
            build_parser().parse_args(["reset-codex-use", "--confirm", "TOKEN"]).confirm, "TOKEN",
        )
        self.assertNotIn("TOKEN", _safe_text("/reset_codex_use confirm TOKEN"))
        self.assertEqual(build_parser().parse_args(["reset-codex-auto", "on"]).enabled, "on")
        self.assertEqual(
            build_parser().parse_args(["reset-codex-cards"]).command, "reset-codex-cards",
        )

    async def test_query_and_invalid_commands_cannot_use_a_card_or_change_settings(self):
        monitor = object.__new__(LimitMonitor)
        monitor._settings = SimpleNamespace(codex_enabled=True)
        monitor._reset_credits = SimpleNamespace(
            query=AsyncMock(), use=AsyncMock(), set_enabled=AsyncMock(),
        )
        for text in (
            "/reset_codex_cards use ID", "/reset_codex_cards off",
            "/reset_codex_use ID extra", "/reset_codex_auto", "/reset_codex_auto maybe",
        ):
            self.assertIn("用法", await monitor.handle_command("1", text))
        self.assertIn("Unknown command", await monitor.handle_command("1", "/reset_cards"))
        monitor._reset_credits.query.assert_not_awaited()
        monitor._reset_credits.use.assert_not_awaited()
        monitor._reset_credits.set_enabled.assert_not_awaited()
