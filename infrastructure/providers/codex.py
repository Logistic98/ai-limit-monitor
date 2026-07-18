from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from config.settings import Settings
from domain.models import CreditInfo, ErrorKind, ProviderUsage, UsageWindow
from shared.utils import (
    first_existing_path,
    jwt_payload,
    load_json_file,
    nested_get,
    parse_epoch_seconds,
    window_label_from_minutes,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexAuth:
    access_token: str | None
    account_id: str | None = None
    plan_type: str | None = None
    refresh_token: str | None = None
    expires_at: datetime | None = None
    auth_path: Path | None = None

    def should_refresh(self, captured_at: datetime, refresh_before_seconds: int) -> bool:
        if not self.refresh_token or not self.expires_at:
            return False
        return self.expires_at <= captured_at + timedelta(seconds=refresh_before_seconds)


@dataclass(frozen=True)
class CodexRefreshResult:
    auth: CodexAuth | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None


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

        refreshed_before_request = False
        if auth.should_refresh(captured_at, self._settings.codex_refresh_before_seconds):
            refresh_result = await self._refresh_access_token(auth, captured_at)
            if refresh_result.auth and refresh_result.auth.access_token:
                auth = refresh_result.auth
                refreshed_before_request = True

        try:
            payload = await self._fetch_usage_payload(auth)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                return ProviderUsage(
                    provider="codex",
                    ok=True,
                    captured_at=captured_at,
                    windows=[_rate_limited_window(exc.response, captured_at)],
                    plan_type=auth.plan_type,
                )

            is_auth_error = exc.response.status_code in {401, 403}
            if is_auth_error and auth.refresh_token and not refreshed_before_request:
                refresh_result = await self._refresh_access_token(auth, captured_at)
                if refresh_result.auth and refresh_result.auth.access_token:
                    auth = refresh_result.auth
                    try:
                        payload = await self._fetch_usage_payload(auth)
                    except httpx.HTTPStatusError as retry_exc:
                        if retry_exc.response.status_code == 429:
                            return ProviderUsage(
                                provider="codex",
                                ok=True,
                                captured_at=captured_at,
                                windows=[_rate_limited_window(retry_exc.response, captured_at)],
                                plan_type=auth.plan_type,
                            )
                        return self._http_error_usage(retry_exc, captured_at, refreshed=True)
                    except Exception as retry_exc:
                        return ProviderUsage(
                            provider="codex",
                            ok=False,
                            captured_at=captured_at,
                            error=(
                                f"Codex usage API request failed after token refresh: {retry_exc}"
                            ),
                            error_kind="request_failed",
                        )
                elif refresh_result.error_kind == "request_failed":
                    return self._refresh_failure_usage(refresh_result, captured_at)
                else:
                    return self._http_error_usage(
                        exc,
                        captured_at,
                        refreshed=True,
                        refresh_error=refresh_result.error,
                    )
            else:
                return self._http_error_usage(exc, captured_at, refreshed=False)
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

    async def _fetch_usage_payload(self, auth: CodexAuth) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "Accept": "application/json",
            "User-Agent": "codex-cli",
        }
        if auth.account_id:
            headers["ChatGPT-Account-Id"] = auth.account_id
        response = await self._http.get(self._settings.codex_usage_url, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Codex usage API returned non-object JSON")
        return payload

    async def _refresh_access_token(
        self,
        auth: CodexAuth,
        captured_at: datetime,
    ) -> CodexRefreshResult:
        if not auth.refresh_token:
            return CodexRefreshResult(
                error="Codex OAuth refresh token 不存在，需要重新登录。",
                error_kind="auth_required",
            )

        try:
            response = await self._http.post(
                self._settings.codex_token_url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "codex-cli",
                },
                json={
                    "client_id": self._settings.codex_oauth_client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": auth.refresh_token,
                    "scope": "openid profile email",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            error = _safe_refresh_http_error(exc)
            logger.warning("failed to refresh codex oauth token: %s", error)
            return CodexRefreshResult(
                error=f"Codex OAuth token 刷新失败：{error}",
                error_kind=_refresh_http_error_kind(exc.response),
            )
        except Exception as exc:
            logger.warning("failed to refresh codex oauth token: %s", exc)
            return CodexRefreshResult(
                error=f"Codex OAuth token 刷新请求失败：{exc.__class__.__name__}",
                error_kind="request_failed",
            )

        if not isinstance(payload, dict):
            logger.warning("failed to refresh codex oauth token: non-object response")
            return CodexRefreshResult(
                error="Codex OAuth token 刷新响应不是 JSON object。",
                error_kind="parse_failed",
            )

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            logger.warning("failed to refresh codex oauth token: missing access token")
            return CodexRefreshResult(
                error="Codex OAuth token 刷新响应缺少 access token。",
                error_kind="parse_failed",
            )

        access_token = access_token.strip()
        id_token = payload.get("id_token")
        id_token = id_token.strip() if isinstance(id_token, str) and id_token.strip() else None
        refresh_token = payload.get("refresh_token")
        refresh_token = (
            refresh_token.strip()
            if isinstance(refresh_token, str) and refresh_token.strip()
            else auth.refresh_token
        )

        account_id = auth.account_id
        plan_type = auth.plan_type
        claims = jwt_payload(id_token)
        openai_auth = claims.get("https://api.openai.com/auth")
        if isinstance(openai_auth, dict):
            account_id = account_id or openai_auth.get("chatgpt_account_id")
            plan_type = openai_auth.get("chatgpt_plan_type") or plan_type

        refreshed = CodexAuth(
            access_token=access_token,
            account_id=account_id if isinstance(account_id, str) else None,
            plan_type=plan_type if isinstance(plan_type, str) else None,
            refresh_token=refresh_token,
            expires_at=_access_token_expiry(access_token),
            auth_path=auth.auth_path,
        )
        self._save_refreshed_auth(auth, refreshed, id_token, captured_at)
        logger.info(
            "refreshed codex oauth token expires_at=%s",
            refreshed.expires_at.isoformat() if refreshed.expires_at else "unknown",
        )
        return CodexRefreshResult(auth=refreshed)

    def _save_refreshed_auth(
        self,
        old_auth: CodexAuth,
        refreshed: CodexAuth,
        id_token: str | None,
        captured_at: datetime,
    ) -> None:
        path = old_auth.auth_path
        if not path or not refreshed.access_token:
            return

        try:
            data = load_json_file(path)
            tokens = data.get("tokens")
            if not isinstance(tokens, dict):
                tokens = {}
                data["tokens"] = tokens
            current_refresh = tokens.get("refresh_token")
            if (
                old_auth.refresh_token
                and isinstance(current_refresh, str)
                and current_refresh
                and current_refresh != old_auth.refresh_token
            ):
                logger.info("skip saving refreshed codex token because auth file changed")
                return

            tokens["access_token"] = refreshed.access_token
            if id_token:
                tokens["id_token"] = id_token
            if refreshed.refresh_token:
                tokens["refresh_token"] = refreshed.refresh_token
            data["last_refresh"] = captured_at.isoformat().replace("+00:00", "Z")
            _atomic_write_json(path, data)
        except Exception as exc:
            logger.warning("failed to save refreshed codex oauth token: %s", exc)

    @staticmethod
    def _refresh_failure_usage(
        refresh_result: CodexRefreshResult,
        captured_at: datetime,
    ) -> ProviderUsage:
        return ProviderUsage(
            provider="codex",
            ok=False,
            captured_at=captured_at,
            error=refresh_result.error or "Codex OAuth token 刷新失败。",
            error_kind=refresh_result.error_kind or "request_failed",
        )

    @staticmethod
    def _http_error_usage(
        exc: httpx.HTTPStatusError,
        captured_at: datetime,
        *,
        refreshed: bool,
        refresh_error: str | None = None,
    ) -> ProviderUsage:
        is_auth_error = exc.response.status_code in {401, 403}
        if is_auth_error:
            error = "Codex 登录已失效，或当前账号无权访问 usage API。"
            if refreshed:
                error = f"{error} 已尝试刷新 OAuth token 但仍失败。"
                if refresh_error:
                    error = f"{error}刷新原因：{refresh_error}"
        else:
            error = (
                f"Codex usage API returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:300]}"
            )
        return ProviderUsage(
            provider="codex",
            ok=False,
            captured_at=captured_at,
            error=error,
            error_kind="auth_required" if is_auth_error else "request_failed",
        )

    def diagnose_auth(self, captured_at: datetime) -> dict[str, Any]:
        if not self._settings.codex_enabled:
            return {"provider": "codex", "enabled": False, "status": "disabled"}

        auth_path = self._auth_path()
        load_error = None
        raw_auth: dict[str, Any] = {}
        if auth_path and not self._settings.codex_token_value:
            try:
                raw_auth = load_json_file(auth_path)
            except Exception as exc:
                load_error = f"{exc.__class__.__name__}: {exc}"

        auth = self._load_auth()
        seconds_until_expiry = None
        if auth.expires_at:
            seconds_until_expiry = int((auth.expires_at - captured_at).total_seconds())

        status = "missing_access_token"
        if auth.access_token:
            if auth.expires_at is None:
                status = "unknown_expiry"
            elif seconds_until_expiry is not None and seconds_until_expiry < 0:
                status = "expired"
            elif (
                seconds_until_expiry is not None
                and seconds_until_expiry <= self._settings.codex_refresh_before_seconds
            ):
                status = "expires_soon"
            else:
                status = "valid"

        last_refresh = raw_auth.get("last_refresh")
        return {
            "provider": "codex",
            "enabled": True,
            "status": status,
            "token_source": "env" if self._settings.codex_token_value else "auth_file",
            "auth_path": str(auth_path) if auth_path else None,
            "auth_load_error": load_error,
            "has_access_token": bool(auth.access_token),
            "has_refresh_token": bool(auth.refresh_token),
            "has_account_id": bool(auth.account_id),
            "can_auto_refresh": bool(auth.refresh_token and not self._settings.codex_token_value),
            "plan_type": auth.plan_type,
            "expires_at": auth.expires_at,
            "seconds_until_expiry": seconds_until_expiry,
            "refresh_before_seconds": self._settings.codex_refresh_before_seconds,
            "last_refresh": last_refresh if isinstance(last_refresh, str) else None,
        }

    def _auth_path(self) -> Path | None:
        return first_existing_path(
            [
                self._settings.codex_auth_path,
                Path("~/.codex/auth.json"),
                Path("/auth/codex/auth.json"),
            ]
        )

    def _load_auth(self) -> CodexAuth:
        explicit_token = self._settings.codex_token_value
        if explicit_token:
            return CodexAuth(
                access_token=explicit_token,
                account_id=self._settings.codex_account_id,
                plan_type=None,
            )

        auth_path = self._auth_path()
        if not auth_path:
            return CodexAuth(access_token=None, account_id=self._settings.codex_account_id)

        try:
            auth = load_json_file(auth_path)
        except Exception:
            return CodexAuth(
                access_token=None,
                account_id=self._settings.codex_account_id,
                auth_path=auth_path,
            )

        access_token = nested_get(auth, "tokens", "access_token")
        refresh_token = nested_get(auth, "tokens", "refresh_token")
        id_token = nested_get(auth, "tokens", "id_token")
        account_id = self._settings.codex_account_id or nested_get(auth, "tokens", "account_id")
        claims = jwt_payload(id_token if isinstance(id_token, str) else None)
        openai_auth = claims.get("https://api.openai.com/auth")
        if isinstance(openai_auth, dict):
            account_id = account_id or openai_auth.get("chatgpt_account_id")
            plan_type = openai_auth.get("chatgpt_plan_type")
        else:
            plan_type = None

        access_token = access_token if isinstance(access_token, str) else None
        return CodexAuth(
            access_token=access_token,
            account_id=account_id if isinstance(account_id, str) else None,
            plan_type=plan_type if isinstance(plan_type, str) else None,
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
            expires_at=_access_token_expiry(access_token),
            auth_path=auth_path,
        )


def _rate_limited_window(response: httpx.Response, captured_at: datetime) -> UsageWindow:
    return UsageWindow(
        key="codex_rate_limited",
        label="额度已达上限",
        used_percent=100.0,
        resets_at=_parse_rate_limit_reset(response, captured_at),
        limit_reached=True,
    )


def _parse_rate_limit_reset(response: httpx.Response, captured_at: datetime) -> datetime | None:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return captured_at + timedelta(seconds=int(float(retry_after)))
        except ValueError:
            pass
    return None


def _refresh_http_error_kind(response: httpx.Response) -> ErrorKind:
    if response.status_code in {400, 401, 403}:
        return "auth_required"
    return "request_failed"


def _safe_refresh_http_error(exc: httpx.HTTPStatusError) -> str:
    response = exc.response
    description = ""
    with contextlib.suppress(Exception):
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            error_description = payload.get("error_description")
            if isinstance(error, dict):
                description = str(error.get("message") or error.get("type") or "")
            elif error:
                description = str(error)
            if error_description:
                description = (
                    f"{description}: {error_description}" if description else str(error_description)
                )
    if not description:
        description = response.text[:200]
    suffix = f": {description}" if description else ""
    return f"HTTP {response.status_code}{suffix}"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    expanded = path.expanduser()
    tmp_path = expanded.with_name(f".{expanded.name}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        tmp_path.chmod(expanded.stat().st_mode & 0o777)
    tmp_path.replace(expanded)


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


def _access_token_expiry(access_token: str | None) -> datetime | None:
    exp = jwt_payload(access_token).get("exp")
    if isinstance(exp, int | float):
        try:
            return datetime.fromtimestamp(float(exp), tz=UTC)
        except (ValueError, OSError):
            return None
    return None


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
