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
    parse_iso_datetime,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaudeAuth:
    access_token: str | None
    refresh_token: str | None = None
    expires_at: datetime | None = None
    credentials_path: Path | None = None
    subscription_type: str | None = None

    def should_refresh(self, captured_at: datetime, refresh_before_seconds: int) -> bool:
        if not self.refresh_token or not self.expires_at:
            return False
        return self.expires_at <= captured_at + timedelta(seconds=refresh_before_seconds)


@dataclass(frozen=True)
class ClaudeRefreshResult:
    auth: ClaudeAuth | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None


class ClaudeUsageClient:
    """Client for Claude Code's OAuth usage endpoint.

    The endpoint is undocumented and can change. It currently mirrors the approach used by
    existing open-source Claude usage monitors: Claude Code OAuth bearer token plus the oauth
    beta header. Claude Code access tokens are short-lived, so this client refreshes them with
    the persisted refresh token before they expire and retries once on auth failure.
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http

    async def fetch(self, captured_at: datetime) -> ProviderUsage:
        if not self._settings.claude_enabled:
            return ProviderUsage(provider="claude", ok=True, captured_at=captured_at)

        auth = self._load_auth()
        if not auth.access_token:
            return self._auth_required(captured_at)

        plan_type = auth.subscription_type
        refreshed_before_request = False
        if auth.should_refresh(captured_at, self._settings.claude_refresh_before_seconds):
            refresh_result = await self._refresh_access_token(auth, captured_at)
            if refresh_result.auth and refresh_result.auth.access_token:
                auth = refresh_result.auth
                refreshed_before_request = True

        try:
            payload = await self._fetch_usage_payload(auth.access_token)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429 and self._is_rate_limit_error(exc.response):
                return ProviderUsage(
                    provider="claude",
                    ok=True,
                    captured_at=captured_at,
                    windows=[self._rate_limited_window(exc.response, captured_at)],
                    plan_type=plan_type,
                )

            is_auth_error = exc.response.status_code in {401, 403}
            if is_auth_error and auth.refresh_token and not refreshed_before_request:
                refresh_result = await self._refresh_access_token(auth, captured_at)
                if refresh_result.auth and refresh_result.auth.access_token:
                    try:
                        payload = await self._fetch_usage_payload(refresh_result.auth.access_token)
                    except httpx.HTTPStatusError as retry_exc:
                        if retry_exc.response.status_code == 429 and self._is_rate_limit_error(
                            retry_exc.response
                        ):
                            return ProviderUsage(
                                provider="claude",
                                ok=True,
                                captured_at=captured_at,
                                windows=[
                                    self._rate_limited_window(retry_exc.response, captured_at)
                                ],
                                plan_type=plan_type,
                            )
                        return self._http_error_usage(retry_exc, captured_at, refreshed=True)
                    except Exception as retry_exc:
                        return ProviderUsage(
                            provider="claude",
                            ok=False,
                            captured_at=captured_at,
                            error=(
                                f"Claude usage API request failed after token refresh: {retry_exc}"
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
                provider="claude",
                ok=False,
                captured_at=captured_at,
                error=f"Claude usage API request failed: {exc}",
                error_kind="request_failed",
            )

        try:
            windows, credits = self.parse_usage(payload)
        except Exception as exc:
            return ProviderUsage(
                provider="claude",
                ok=False,
                captured_at=captured_at,
                error=f"Claude usage payload parse failed: {exc}",
                error_kind="parse_failed",
                raw=payload if isinstance(payload, dict) else None,
            )

        return ProviderUsage(
            provider="claude",
            ok=True,
            captured_at=captured_at,
            windows=windows,
            credits=credits,
            plan_type=plan_type,
            raw=payload if isinstance(payload, dict) else None,
        )

    async def _fetch_usage_payload(self, token: str) -> dict[str, Any]:
        response = await self._http.get(
            self._settings.claude_usage_url,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": self._settings.claude_beta_header,
                "Accept": "application/json",
                "User-Agent": "ai-limit-monitor/0.1.0",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Claude usage API returned non-object JSON")
        return payload

    async def _refresh_access_token(
        self,
        auth: ClaudeAuth,
        captured_at: datetime,
    ) -> ClaudeRefreshResult:
        # The claude CLI (proactive quota refresh / login flows) shares this credentials
        # file and rotates the refresh token on its own refreshes. Re-read the file right
        # before refreshing so a token rotated mid-check is not replayed, which the OAuth
        # server rejects with invalid_grant and can invalidate the whole token family.
        if auth.credentials_path and not self._settings.claude_token_value:
            latest = self._load_auth()
            if latest.refresh_token and latest.refresh_token != auth.refresh_token:
                logger.info("claude refresh token rotated on disk, using the newer credentials")
                auth = latest

        if not auth.refresh_token:
            return ClaudeRefreshResult(
                error="Claude OAuth refresh token 不存在，需要重新登录。",
                error_kind="auth_required",
            )

        try:
            response = await self._http.post(
                self._settings.claude_token_url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "claude-code",
                },
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": auth.refresh_token,
                    "client_id": self._settings.claude_oauth_client_id,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            error = _safe_refresh_http_error(exc)
            logger.warning("failed to refresh claude oauth token: %s", error)
            return ClaudeRefreshResult(
                error=f"Claude OAuth token 刷新失败：{error}",
                error_kind=_refresh_http_error_kind(exc.response),
            )
        except Exception as exc:
            logger.warning("failed to refresh claude oauth token: %s", exc)
            return ClaudeRefreshResult(
                error=f"Claude OAuth token 刷新请求失败：{exc.__class__.__name__}",
                error_kind="request_failed",
            )

        if not isinstance(payload, dict):
            logger.warning("failed to refresh claude oauth token: non-object response")
            return ClaudeRefreshResult(
                error="Claude OAuth token 刷新响应不是 JSON object。",
                error_kind="parse_failed",
            )

        access_token = _first_string(payload, "access_token", "accessToken")
        if not access_token:
            logger.warning("failed to refresh claude oauth token: missing access token")
            return ClaudeRefreshResult(
                error="Claude OAuth token 刷新响应缺少 access token。",
                error_kind="parse_failed",
            )

        refresh_token = (
            _first_string(payload, "refresh_token", "refreshToken") or auth.refresh_token
        )
        expires_at = _expires_at_from_refresh_payload(payload, captured_at) or _parse_jwt_expiry(
            access_token
        )
        refreshed = ClaudeAuth(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            credentials_path=auth.credentials_path,
        )
        self._save_refreshed_credentials(auth, refreshed)
        logger.info(
            "refreshed claude oauth token expires_at=%s",
            expires_at.isoformat() if expires_at else "unknown",
        )
        return ClaudeRefreshResult(auth=refreshed)

    def _save_refreshed_credentials(self, old_auth: ClaudeAuth, refreshed: ClaudeAuth) -> None:
        path = old_auth.credentials_path
        if not path or not refreshed.access_token:
            return

        try:
            credentials = load_json_file(path)
            oauth = _oauth_container(credentials)
            current_refresh = _first_string(oauth, "refreshToken", "refresh_token")
            if (
                old_auth.refresh_token
                and current_refresh
                and current_refresh != old_auth.refresh_token
            ):
                logger.info("skip saving refreshed claude token because credentials changed")
                return

            _set_preferred_field(oauth, ("accessToken", "access_token"), refreshed.access_token)
            if refreshed.refresh_token:
                _set_preferred_field(
                    oauth,
                    ("refreshToken", "refresh_token"),
                    refreshed.refresh_token,
                )
            if refreshed.expires_at:
                _set_expiry_field(oauth, refreshed.expires_at)
            _atomic_write_json(path, credentials)
        except Exception as exc:
            logger.warning("failed to save refreshed claude oauth token: %s", exc)

    @staticmethod
    def parse_usage(payload: dict[str, Any]) -> tuple[list[UsageWindow], CreditInfo | None]:
        field_labels = {
            "five_hour": "5h limit",
            "seven_day": "7d limit",
            "seven_day_sonnet": "Sonnet 7d limit",
            "seven_day_opus": "Opus 7d limit",
            "seven_day_omelette": "Opus 7d limit",
            "seven_day_cowork": "Cowork 7d limit",
        }
        windows: list[UsageWindow] = []
        for key, label in field_labels.items():
            raw_window = payload.get(key)
            if not isinstance(raw_window, dict):
                continue
            utilization = raw_window.get("utilization")
            if utilization is None:
                continue
            windows.append(
                UsageWindow(
                    key=key,
                    label=label,
                    used_percent=float(utilization),
                    resets_at=parse_iso_datetime(raw_window.get("resets_at")),
                )
            )

        credits = None
        raw_extra = payload.get("extra_usage")
        if isinstance(raw_extra, dict):
            credits = CreditInfo(
                has_credits=raw_extra.get("is_enabled"),
                balance=(
                    None
                    if raw_extra.get("used_credits") is None
                    else str(raw_extra.get("used_credits"))
                ),
                limit=(
                    None
                    if raw_extra.get("monthly_limit") is None
                    else str(raw_extra.get("monthly_limit"))
                ),
                remaining_percent=(
                    None
                    if raw_extra.get("utilization") is None
                    else max(0.0, 100.0 - float(raw_extra.get("utilization")))
                ),
            )
        return windows, credits

    @staticmethod
    def _is_rate_limit_error(response: httpx.Response) -> bool:
        try:
            body = response.json()
        except Exception:
            return "rate_limit" in response.text.lower()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            return error.get("type") == "rate_limit_error"
        return False

    @classmethod
    def _rate_limited_window(cls, response: httpx.Response, captured_at: datetime) -> UsageWindow:
        return UsageWindow(
            key="claude_rate_limited",
            label="额度已达上限",
            used_percent=100.0,
            resets_at=cls._parse_rate_limit_reset(response, captured_at),
            limit_reached=True,
        )

    @staticmethod
    def _parse_rate_limit_reset(response: httpx.Response, captured_at: datetime) -> datetime | None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return captured_at + timedelta(seconds=int(float(retry_after)))
            except ValueError:
                pass
        for header in (
            "anthropic-ratelimit-unified-reset",
            "anthropic-ratelimit-unified-5h-reset",
        ):
            value = response.headers.get(header)
            if value:
                try:
                    return datetime.fromtimestamp(int(value), tz=UTC)
                except (ValueError, OSError):
                    pass
        return None

    def diagnose_auth(self, captured_at: datetime) -> dict[str, Any]:
        if not self._settings.claude_enabled:
            return {"provider": "claude", "enabled": False, "status": "disabled"}

        credentials_path = self._credentials_path()
        load_error = None
        if credentials_path and not self._settings.claude_token_value:
            try:
                load_json_file(credentials_path)
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
                and seconds_until_expiry <= self._settings.claude_refresh_before_seconds
            ):
                status = "expires_soon"
            else:
                status = "valid"

        return {
            "provider": "claude",
            "enabled": True,
            "status": status,
            "token_source": "env" if self._settings.claude_token_value else "credentials_file",
            "credentials_path": str(credentials_path) if credentials_path else None,
            "credentials_exists": bool(credentials_path),
            "credentials_load_error": load_error,
            "has_access_token": bool(auth.access_token),
            "has_refresh_token": bool(auth.refresh_token),
            "can_auto_refresh": bool(auth.refresh_token and not self._settings.claude_token_value),
            "plan_type": auth.subscription_type,
            "expires_at": auth.expires_at,
            "seconds_until_expiry": seconds_until_expiry,
            "refresh_before_seconds": self._settings.claude_refresh_before_seconds,
        }

    def _load_auth(self) -> ClaudeAuth:
        if self._settings.claude_token_value:
            return ClaudeAuth(access_token=self._settings.claude_token_value)

        credentials_path = self._credentials_path()
        if not credentials_path:
            return ClaudeAuth(access_token=None)

        try:
            credentials = load_json_file(credentials_path)
        except Exception:
            return ClaudeAuth(access_token=None, credentials_path=credentials_path)

        oauth = _oauth_container(credentials)
        access_token = _first_string(
            oauth,
            "accessToken",
            "access_token",
            "token",
        ) or _first_nested_string(
            credentials,
            ("claudeAiOauth", "accessToken"),
            ("claudeAiOauth", "access_token"),
            ("oauth", "accessToken"),
            ("oauth", "access_token"),
        )
        refresh_token = _first_string(oauth, "refreshToken", "refresh_token")
        expires_at = _parse_credentials_expiry(oauth, access_token)
        subscription_type = _first_string(oauth, "subscriptionType", "subscription_type")
        return ClaudeAuth(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            credentials_path=credentials_path,
            subscription_type=subscription_type,
        )

    def _credentials_path(self) -> Path | None:
        return first_existing_path(
            [
                self._settings.claude_credentials_path,
                Path("~/.claude/.credentials.json"),
                Path("~/.claude/credentials/default.json"),
                Path("/auth/claude/.credentials.json"),
            ]
        )

    @staticmethod
    def _auth_required(captured_at: datetime) -> ProviderUsage:
        return ProviderUsage(
            provider="claude",
            ok=False,
            captured_at=captured_at,
            error="Claude 登录凭据不存在，或监控服务无法读取当前登录凭据。",
            error_kind="auth_required",
        )

    @staticmethod
    def _refresh_failure_usage(
        refresh_result: ClaudeRefreshResult,
        captured_at: datetime,
    ) -> ProviderUsage:
        return ProviderUsage(
            provider="claude",
            ok=False,
            captured_at=captured_at,
            error=refresh_result.error or "Claude OAuth token 刷新失败。",
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
            error = "Claude 登录已失效，或当前账号无权访问 usage API。"
            if refreshed:
                error = f"{error} 已尝试刷新 OAuth token 但仍失败。"
                if refresh_error:
                    error = f"{error}刷新原因：{refresh_error}"
        else:
            error = (
                f"Claude usage API returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:300]}"
            )
        return ProviderUsage(
            provider="claude",
            ok=False,
            captured_at=captured_at,
            error=error,
            error_kind="auth_required" if is_auth_error else "request_failed",
        )


def _oauth_container(credentials: dict[str, Any]) -> dict[str, Any]:
    for key in ("claudeAiOauth", "oauth"):
        value = credentials.get(key)
        if isinstance(value, dict):
            return value
    return credentials


def _first_string(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_nested_string(data: dict[str, Any], *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        value = nested_get(data, *path)
        if isinstance(value, str) and value.strip():
            return value.strip()
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


def _parse_credentials_expiry(oauth: dict[str, Any], access_token: str | None) -> datetime | None:
    for key in ("expiresAt", "expires_at", "expiration", "expires"):
        if key in oauth:
            parsed = _parse_expiry_value(oauth.get(key))
            if parsed:
                return parsed
    return _parse_jwt_expiry(access_token)


def _parse_jwt_expiry(access_token: str | None) -> datetime | None:
    claims = jwt_payload(access_token)
    exp = claims.get("exp")
    if isinstance(exp, int | float):
        try:
            return datetime.fromtimestamp(float(exp), tz=UTC)
        except (ValueError, OSError):
            return None
    return None


def _parse_expiry_value(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        return _epoch_to_datetime(float(value))
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return _epoch_to_datetime(float(stripped))
        except ValueError:
            return parse_iso_datetime(stripped)
    return None


def _epoch_to_datetime(value: float) -> datetime | None:
    # Claude Code usually stores expiresAt in epoch milliseconds. Accept seconds too.
    seconds = value / 1000 if value > 10_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (ValueError, OSError):
        return None


def _expires_at_from_refresh_payload(
    payload: dict[str, Any], captured_at: datetime
) -> datetime | None:
    for key in ("expires_at", "expiresAt", "expiration", "expires"):
        if key in payload:
            parsed = _parse_expiry_value(payload.get(key))
            if parsed:
                return parsed

    expires_in = payload.get("expires_in") or payload.get("expiresIn")
    if expires_in in (None, ""):
        return None
    try:
        return captured_at + timedelta(seconds=int(float(expires_in)))
    except (TypeError, ValueError):
        return None


def _set_preferred_field(data: dict[str, Any], candidate_keys: tuple[str, ...], value: str) -> None:
    for key in candidate_keys:
        if key in data:
            data[key] = value
            return
    data[candidate_keys[0]] = value


def _set_expiry_field(data: dict[str, Any], expires_at: datetime) -> None:
    for key in ("expiresAt", "expires_at", "expiration", "expires"):
        if key in data:
            data[key] = _format_expiry_like(data[key], expires_at)
            return
    data["expiresAt"] = int(expires_at.timestamp() * 1000)


def _format_expiry_like(current_value: Any, expires_at: datetime) -> int | float | str:
    if isinstance(current_value, int):
        return (
            int(expires_at.timestamp() * 1000)
            if current_value > 10_000_000_000
            else int(expires_at.timestamp())
        )
    if isinstance(current_value, float):
        if current_value > 10_000_000_000:
            return expires_at.timestamp() * 1000
        return expires_at.timestamp()
    if isinstance(current_value, str):
        stripped = current_value.strip()
        try:
            number = float(stripped)
        except ValueError:
            return expires_at.isoformat().replace("+00:00", "Z")
        if number > 10_000_000_000:
            return str(int(expires_at.timestamp() * 1000))
        return str(int(expires_at.timestamp()))
    return int(expires_at.timestamp() * 1000)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    expanded = path.expanduser()
    tmp_path = expanded.with_name(f".{expanded.name}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        tmp_path.chmod(expanded.stat().st_mode & 0o777)
    tmp_path.replace(expanded)
