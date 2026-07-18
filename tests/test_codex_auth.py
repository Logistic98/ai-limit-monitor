from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from config.settings import Settings
from infrastructure.providers.codex import CodexAuth, CodexUsageClient


def _settings(**overrides: object) -> Settings:
    return Settings(
        telegram_bot_token="test-token",
        telegram_chat_id="1",
        _env_file=None,
        **overrides,
    )


class CodexAuthShouldRefreshTest(unittest.TestCase):
    def test_refreshes_only_near_expiry_with_refresh_token(self) -> None:
        captured_at = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)

        near_expiry = CodexAuth(
            access_token="token",
            refresh_token="refresh",
            expires_at=captured_at + timedelta(seconds=100),
        )
        far_from_expiry = CodexAuth(
            access_token="token",
            refresh_token="refresh",
            expires_at=captured_at + timedelta(seconds=1_000),
        )
        no_refresh_token = CodexAuth(
            access_token="token",
            expires_at=captured_at + timedelta(seconds=100),
        )

        self.assertTrue(near_expiry.should_refresh(captured_at, 300))
        self.assertFalse(far_from_expiry.should_refresh(captured_at, 300))
        self.assertFalse(no_refresh_token.should_refresh(captured_at, 300))


class CodexSaveRefreshedAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self._auth_path = Path(self._temporary_directory.name) / "auth.json"
        self._client = CodexUsageClient(_settings(), http=None)  # type: ignore[arg-type]
        self._captured_at = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)

    def _write_auth(self, refresh_token: str) -> None:
        self._auth_path.write_text(
            json.dumps(
                {
                    "OPENAI_API_KEY": None,
                    "tokens": {
                        "access_token": "old-access",
                        "id_token": "old-id",
                        "refresh_token": refresh_token,
                        "account_id": "acc-1",
                    },
                    "last_refresh": "2026-07-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

    def test_saves_tokens_and_preserves_other_fields(self) -> None:
        self._write_auth("old-refresh")
        old_auth = CodexAuth(
            access_token="old-access",
            refresh_token="old-refresh",
            auth_path=self._auth_path,
        )
        refreshed = CodexAuth(
            access_token="new-access",
            refresh_token="new-refresh",
            auth_path=self._auth_path,
        )

        self._client._save_refreshed_auth(old_auth, refreshed, "new-id", self._captured_at)

        data = json.loads(self._auth_path.read_text(encoding="utf-8"))
        self.assertEqual(data["tokens"]["access_token"], "new-access")
        self.assertEqual(data["tokens"]["id_token"], "new-id")
        self.assertEqual(data["tokens"]["refresh_token"], "new-refresh")
        self.assertEqual(data["tokens"]["account_id"], "acc-1")
        self.assertIn("OPENAI_API_KEY", data)
        self.assertEqual(data["last_refresh"], "2026-07-13T00:00:00Z")

    def test_skips_save_when_auth_file_changed_concurrently(self) -> None:
        self._write_auth("rotated-by-cli")
        old_auth = CodexAuth(
            access_token="old-access",
            refresh_token="old-refresh",
            auth_path=self._auth_path,
        )
        refreshed = CodexAuth(
            access_token="new-access",
            refresh_token="new-refresh",
            auth_path=self._auth_path,
        )

        self._client._save_refreshed_auth(old_auth, refreshed, "new-id", self._captured_at)

        data = json.loads(self._auth_path.read_text(encoding="utf-8"))
        self.assertEqual(data["tokens"]["access_token"], "old-access")
        self.assertEqual(data["tokens"]["refresh_token"], "rotated-by-cli")
