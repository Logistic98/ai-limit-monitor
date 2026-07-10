from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from infrastructure.storage.json_state import MonitorState


class MonitorStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self._path = Path(self._temporary_directory.name) / "state.json"

    def test_new_and_legacy_state_default_quota_refresh_to_enabled(self) -> None:
        self.assertTrue(MonitorState.load(self._path).proactive_quota_refresh_enabled)

        self._path.write_text(json.dumps({"last_report_at": 12}), encoding="utf-8")
        state = MonitorState.load(self._path)

        self.assertTrue(state.proactive_quota_refresh_enabled)

    def test_disabled_setting_and_schedule_round_trip(self) -> None:
        state = MonitorState(
            proactive_quota_refresh_enabled=False,
            quota_refresh_scheduled_at={"claude:five_hour": 123.0},
            quota_refresh_last_attempt_at={"claude:five_hour": 124.0},
            quota_refresh_last_success_at={"claude": 125.0},
            quota_refresh_last_errors={"codex": "timeout"},
        )

        state.save(self._path)
        loaded = MonitorState.load(self._path)

        self.assertFalse(loaded.proactive_quota_refresh_enabled)
        self.assertEqual(loaded.quota_refresh_scheduled_at, {"claude:five_hour": 123.0})
        self.assertEqual(
            loaded.quota_refresh_last_attempt_at,
            {"claude:five_hour": 124.0},
        )
        self.assertEqual(loaded.quota_refresh_last_success_at, {"claude": 125.0})
        self.assertEqual(loaded.quota_refresh_last_errors, {"codex": "timeout"})

    def test_non_boolean_setting_falls_back_to_enabled(self) -> None:
        self._path.write_text(
            json.dumps({"proactive_quota_refresh_enabled": "false"}),
            encoding="utf-8",
        )

        state = MonitorState.load(self._path)

        self.assertTrue(state.proactive_quota_refresh_enabled)
