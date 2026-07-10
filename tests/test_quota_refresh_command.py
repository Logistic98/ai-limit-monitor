from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from application.monitor_service import LimitMonitor
from infrastructure.storage.json_state import MonitorState


class QuotaRefreshTelegramCommandTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self._state_path = Path(self._temporary_directory.name) / "state.json"
        self._monitor = object.__new__(LimitMonitor)
        self._monitor._state = MonitorState()  # noqa: SLF001
        self._monitor._settings = SimpleNamespace(  # noqa: SLF001
            state_path=self._state_path
        )
        self._monitor._quota_refresh = SimpleNamespace(  # noqa: SLF001
            notify_setting_changed=lambda: None
        )

    async def test_query_and_bot_suffix_toggle_persisted_setting(self) -> None:
        status = await self._monitor.handle_command("1", "/quota_refresh")
        disabled = await self._monitor.handle_command(
            "1",
            "/quota_refresh@LimitBot off",
        )

        self.assertIn("已开启", status or "")
        self.assertIn("已关闭", disabled or "")
        self.assertFalse(
            MonitorState.load(self._state_path).proactive_quota_refresh_enabled
        )

    async def test_invalid_option_does_not_change_setting(self) -> None:
        response = await self._monitor.handle_command("1", "/quota_refresh maybe")

        self.assertEqual(response, "用法：/quota_refresh on|off")
        self.assertTrue(self._monitor._state.proactive_quota_refresh_enabled)  # noqa: SLF001

    async def test_save_failure_rolls_back_setting(self) -> None:
        with patch.object(self._monitor._state, "save", side_effect=OSError("read only")):
            response = await self._monitor.handle_command("1", "/quota_refresh off")

        self.assertIn("保存失败", response or "")
        self.assertTrue(self._monitor._state.proactive_quota_refresh_enabled)  # noqa: SLF001
