from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from config.settings import Settings
from domain.models import CreditInfo, ProviderUsage, UsageWindow
from shared.utils import (
    first_existing_path,
    jwt_payload,
    load_json_file,
    nested_get,
    parse_epoch_seconds,
    window_label_from_minutes,
)


class CodexUsageClient:
    """Client for Codex CLI / ChatGPT Codex usage limits.

    The implementation follows the current Codex CLI backend client behavior:
    GET https://chatgpt.com/backend-api/wham/usage with ChatGPT OAuth bearer token and
    optional ChatGPT-Account-Id header from ~/.codex/auth.json.
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    async def fetch(self, captured_at: datetime) -> ProviderUsage:
        if not self._settings.codex_enabled:
            return ProviderUsage(provider="codex", ok=True, captured_at=captured_at)

        auth = self._load_auth()
        if not auth.access_token:
            return ProviderUsage(
                provider="codex",
                ok=False,
                captured_at=captured_at,
                error="Codex 登录凭据不存在，或监控服务无法读取当前登录凭据。",
                error_kind="auth_required",
            )

        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "Accept": "application/json",
            "User-Agent": "codex-cli",
        }
        if auth.account_id:
            headers["ChatGPT-Account-Id"] = auth.account_id

        try:
            response = await self._http.get(self._settings.codex_usage_url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            is_auth_error = exc.response.status_code in {401, 403}
            error = (
                "Codex 登录已失效，或当前账号无权访问 usage API。"
                if is_auth_error
                else (
                    f"Codex usage API returned HTTP {exc.response.status_code}: "
                    f"{exc.response.text[:300]}"
                )
            )
            return ProviderUsage(
                provider="codex",
                ok=False,
                captured_at=captured_at,
                error=error,
                error_kind="auth_required" if is_auth_error else "request_failed",
            )
        except Exception as exc:
            return ProviderUsage(
                provider="codex",
                ok=False,
                captured_at=captured_at,
                error=f"Codex usage API request failed: {exc}",
                error_kind="request_failed",
            )

        try:
            windows, credits = self.parse_usage(payload)
        except Exception as exc:
            return ProviderUsage(
                provider="codex",
                ok=False,
                captured_at=captured_at,
                error=f"Codex usage payload parse failed: {exc}",
                error_kind="parse_failed",
                raw=payload if isinstance(payload, dict) else None,
            )

        return ProviderUsage(
            provider="codex",
            ok=True,
            captured_at=captured_at,
            windows=windows,
            credits=credits,
            plan_type=str(payload.get("plan_type")) if payload.get("plan_type") else auth.plan_type,
            raw=payload if isinstance(payload, dict) else None,
        )

    @staticmethod
    def parse_usage(payload: dict[str, Any]) -> tuple[list[UsageWindow], CreditInfo | None]:
        windows: list[UsageWindow] = []

        default_details = payload.get("rate_limit")
        if isinstance(default_details, dict):
            windows.extend(_parse_rate_limit_details("codex", "Codex", default_details))

        additional = payload.get("additional_rate_limits")
        if isinstance(additional, list):
            for item in additional:
                if not isinstance(item, dict):
                    continue
                details = item.get("rate_limit")
                if not isinstance(details, dict):
                    continue
                feature = str(item.get("metered_feature") or item.get("limit_name") or "extra")
                label = str(item.get("limit_name") or feature)
                windows.extend(_parse_rate_limit_details(feature, label, details))

        credits = None
        raw_credits = payload.get("credits")
        if isinstance(raw_credits, dict):
            credits = CreditInfo(
                has_credits=raw_credits.get("has_credits"),
                unlimited=raw_credits.get("unlimited"),
                balance=(
                    None if raw_credits.get("balance") is None else str(raw_credits.get("balance"))
                ),
            )

        spend_control = payload.get("spend_control")
        if isinstance(spend_control, dict):
            individual = spend_control.get("individual_limit")
            if isinstance(individual, dict):
                credits = CreditInfo(
                    has_credits=credits.has_credits if credits else None,
                    unlimited=credits.unlimited if credits else None,
                    balance=credits.balance if credits else None,
                    used=None if individual.get("used") is None else str(individual.get("used")),
                    limit=None if individual.get("limit") is None else str(individual.get("limit")),
                    remaining_percent=(
                        None
                        if individual.get("remaining_percent") is None
                        else float(individual.get("remaining_percent"))
                    ),
                    resets_at=parse_epoch_seconds(individual.get("reset_at")),
                )

        return windows, credits

    def _load_auth(self) -> CodexAuth:
        explicit_token = self._settings.codex_token_value
        if explicit_token:
            return CodexAuth(
                access_token=explicit_token,
                account_id=self._settings.codex_account_id,
                plan_type=None,
            )

        auth_path = first_existing_path(
            [
                self._settings.codex_auth_path,
                Path("~/.codex/auth.json"),
                Path("/auth/codex/auth.json"),
            ]
        )
        if not auth_path:
            return CodexAuth(access_token=None, account_id=self._settings.codex_account_id)

        try:
            auth = load_json_file(auth_path)
        except Exception:
            return CodexAuth(access_token=None, account_id=self._settings.codex_account_id)

        access_token = nested_get(auth, "tokens", "access_token")
        id_token = nested_get(auth, "tokens", "id_token")
        account_id = self._settings.codex_account_id or nested_get(auth, "tokens", "account_id")
        claims = jwt_payload(id_token if isinstance(id_token, str) else None)
        openai_auth = claims.get("https://api.openai.com/auth")
        if isinstance(openai_auth, dict):
            account_id = account_id or openai_auth.get("chatgpt_account_id")
            plan_type = openai_auth.get("chatgpt_plan_type")
        else:
            plan_type = None

        return CodexAuth(
            access_token=access_token if isinstance(access_token, str) else None,
            account_id=account_id if isinstance(account_id, str) else None,
            plan_type=plan_type if isinstance(plan_type, str) else None,
        )


class CodexAuth:
    def __init__(
        self,
        access_token: str | None,
        account_id: str | None = None,
        plan_type: str | None = None,
    ) -> None:
        self.access_token = access_token
        self.account_id = account_id
        self.plan_type = plan_type


def _parse_rate_limit_details(
    prefix: str,
    label_prefix: str,
    details: dict[str, Any],
) -> list[UsageWindow]:
    windows: list[UsageWindow] = []
    limit_reached = details.get("limit_reached")
    for field_name, is_secondary in (("primary_window", False), ("secondary_window", True)):
        raw_window = details.get(field_name)
        if not isinstance(raw_window, dict):
            continue
        used_percent = raw_window.get("used_percent")
        if used_percent is None:
            continue
        minutes = _seconds_to_minutes(raw_window.get("limit_window_seconds"))
        suffix = window_label_from_minutes(minutes, secondary=is_secondary)
        label = suffix if label_prefix == "Codex" else f"{label_prefix} {suffix}"
        windows.append(
            UsageWindow(
                key=f"{prefix}.{field_name}",
                label=label,
                used_percent=float(used_percent),
                resets_at=parse_epoch_seconds(raw_window.get("reset_at")),
                window_minutes=minutes,
                limit_reached=bool(limit_reached),
            )
        )
    return windows


def _seconds_to_minutes(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return (seconds + 59) // 60
