from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from config.settings import Settings
from domain.auth import LoginCompleteResult, LoginStartResult
from domain.models import CheckResult, ProviderUsage, UsageWindow
from shared.utils import (
    format_duration,
    html_escape,
    local_time,
    local_time_with_countdown,
    parse_iso_datetime,
)

_PROVIDER_ERRORS = {
    "auth_required": "需要重新登录",
    "request_failed": "查询失败",
    "parse_failed": "数据解析失败",
}
_CREDENTIAL_STATUSES = {
    "missing_access_token": "缺少访问凭据",
    "unknown_expiry": "有效期未知",
    "expired": "已过期",
    "expires_soon": "即将过期",
    "valid": "有效",
}


def format_report(
    result: CheckResult,
    settings: Settings,
    logins: dict[str, LoginStartResult] | None = None,
    reset_credits: str | None = None,
    codex_billing: str | None = None,
) -> str:
    lines = [
        "<b>AI订阅资源限额监控</b>",
        f"时间：{html_escape(local_time(result.captured_at, settings.timezone))}",
        "",
    ]
    for provider in result.providers:
        login = logins.get(provider.provider) if logins else None
        lines.extend(_format_provider(provider, settings, result.captured_at, login))
        lines.append("")
    if codex_billing:
        lines.extend([codex_billing, ""])
    if reset_credits:
        lines.append(reset_credits)
    return "\n".join(lines).strip()


def format_diagnostics(
    result: CheckResult,
    settings: Settings,
    claude_auth: dict[str, Any],
    codex_auth: dict[str, Any],
    quota_refresh: dict[str, Any] | None = None,
    reset_credits: str | None = None,
    codex_billing: str | None = None,
) -> str:
    lines = [
        "<b>AI订阅资源限额监控 诊断</b>",
        f"时间：{html_escape(local_time(result.captured_at, settings.timezone))}",
        f"检查间隔：{format_duration(settings.check_interval_seconds)}",
        "",
    ]

    for provider in result.providers:
        diagnostics = claude_auth if provider.provider == "claude" else codex_auth
        if not diagnostics.get("enabled", True):
            lines.extend([f"<b>{_provider_label(provider.provider)} 监控</b>：已禁用", ""])
            continue
        status = "正常" if provider.ok else _PROVIDER_ERRORS.get(provider.error_kind, "异常")
        lines.append(f"<b>{_format_provider_title(provider)}</b>：{status}")
        if provider.error:
            lines.append(f"最近错误：{html_escape(provider.error)}")
        lines.extend(_format_auth_diagnostics(diagnostics, settings.timezone, result.captured_at))
        lines.append("")

    if codex_billing:
        lines.extend([codex_billing, ""])
    if reset_credits:
        lines.extend([reset_credits, ""])
    if quota_refresh is not None:
        lines.append(format_quota_refresh_status(quota_refresh, settings, include_commands=False))
        lines.append("")
    return "\n".join(lines).strip()


def format_login_status(result: CheckResult, settings: Settings) -> str:
    lines = [
        "<b>登录状态检查</b>",
        f"时间：{html_escape(local_time(result.captured_at, settings.timezone))}",
        "",
    ]
    for provider in result.providers:
        status = "已登录" if provider.ok else "需要登录"
        lines.append(f"<b>{_format_provider_title(provider)}</b>：{status}")
        if not provider.ok:
            lines.append(f"原因：{html_escape(provider.error or '登录凭据不可用')}")
            if provider.error_kind == "auth_required":
                lines.append(f"操作：发送 {_login_command(provider.provider)}")
        lines.append("")
    return "\n".join(lines).strip()


def format_error(provider: ProviderUsage, settings: Settings) -> str:
    return "\n".join(
        [
            "<b>AI订阅资源限额监控 检测异常</b>",
            f"服务：{_provider_label(provider.provider)}",
            f"原因：{html_escape(provider.error or '未知错误')}",
        ]
    )


def format_login_required(
    provider: ProviderUsage,
    settings: Settings,
    login: LoginStartResult | None = None,
) -> str:
    lines = [
        "<b>AI订阅资源限额监控 需要重新登录</b>",
        f"服务：{_provider_label(provider.provider)}",
        f"原因：{html_escape(provider.error or '登录凭据不可用')}",
        "",
    ]
    lines.extend(_format_login_action(provider.provider, login))
    lines.append("")
    lines.append("登录完成后不需要修改配置文件或环境变量。")
    lines.append("下一轮检测会自动恢复。")
    return "\n".join(lines)


def format_login_start(login: LoginStartResult, settings: Settings) -> str:
    lines = [
        f"<b>{_provider_label(login.provider)} 登录</b>",
        html_escape(login.message),
        "",
    ]
    lines.extend(_format_login_action(login.provider, login))
    return "\n".join(lines).strip()


def format_login_complete(result: LoginCompleteResult) -> str:
    title = "登录完成" if result.ok else "登录失败"
    return "\n".join(
        [
            f"<b>{_provider_label(result.provider)} {title}</b>",
            html_escape(result.message),
        ]
    )


def format_quota_refresh_result(
    provider: str,
    ok: bool,
    error: str | None,
    *,
    attempt: int | None = None,
    max_attempts: int | None = None,
    gave_up: bool = False,
    auth_blocked: bool = False,
) -> str:
    lines = [
        "<b>限额恢复主动请求</b>",
        f"服务：{_provider_label(provider)}",
    ]
    if ok:
        lines.append("结果：成功，已发起一次轻量请求开启新的限额窗口。")
        lines.append("随后会自动执行一次 /monitor 并发送最新报告。")
    else:
        lines.append("结果：失败")
        lines.append(f"原因：{html_escape(error or 'unknown')}")
        if attempt is not None and max_attempts is not None:
            lines.append(f"尝试次数：{attempt}/{max_attempts}")
        if gave_up:
            lines.append("已达到最大尝试次数，本次触发不再重试。")
        elif auth_blocked:
            lines.append("检测到登录凭据失效，已暂停自动重试；重新登录并通过下一轮检测后会自动继续。")
        else:
            lines.append("将在冷却时间后自动重试。")
    return "\n".join(lines)


def format_quota_refresh_status(
    snapshot: dict[str, Any], settings: Settings, *, include_commands: bool = True,
) -> str:
    enabled = bool(snapshot.get("enabled"))
    lines = [
        "<b>限额恢复主动请求</b>",
        f"开关：{'已开启' if enabled else '已关闭'}",
    ]
    scheduled = snapshot.get("scheduled") or {}
    attempt_counts = snapshot.get("attempt_counts") or {}
    auth_blocked = snapshot.get("auth_blocked") or {}
    last_success = snapshot.get("last_success") or {}
    last_errors = snapshot.get("last_errors") or {}
    last_attempt = snapshot.get("last_attempt") or {}
    providers_enabled = snapshot.get("providers_enabled") or {}
    now = snapshot.get("captured_at", datetime.now(tz=UTC).timestamp())
    for provider in ("claude", "codex"):
        windows = sorted(
            ((key, when) for key, when in scheduled.items() if key.startswith(f"{provider}:")),
            key=lambda item: item[1],
        )
        blocked = auth_blocked.get(provider, False)
        active = enabled and providers_enabled.get(provider, True) and not blocked
        if not providers_enabled.get(provider, True):
            status = "监控已关闭"
        elif not enabled:
            status = "已关闭"
        elif blocked:
            status = "登录失效，已暂停"
        elif provider in (snapshot.get("in_flight") or []):
            status = "执行中"
        else:
            status = "已安排" if windows else "等待检测"
        lines.extend(["", f"<b>{_provider_label(provider)}｜{status}</b>"])
        if active and windows and status != "执行中":
            next_at = min(
                max(when, last_attempt.get(key, 0) + snapshot.get("retry_seconds", 0), now)
                for key, when in windows
            )
            lines.append(f"下次执行：{_epoch_local_time(next_at, settings.timezone, now=now)}")
        for key, when in windows:
            window = key.partition(":")[2].removeprefix(f"{provider}.")
            label = {
                "claude_rate_limited": "限流恢复窗口",
                "primary_window": "主窗口", "secondary_window": "次窗口",
                "five_hour": "5 小时窗口", "seven_day": "7 天窗口",
                "seven_day_sonnet": "Sonnet 7 天窗口", "seven_day_opus": "Opus 7 天窗口",
            }.get(window, window)
            lines.append(
                f"{html_escape(label)}：{_epoch_local_time(when, settings.timezone, now=now)}",
            )
            count = attempt_counts.get(key)
            if count:
                lines.append(f"失败次数：{count}/{snapshot.get('max_attempts', 4)}")
        if not windows:
            lines.append("待执行窗口：无")
        if provider in last_success:
            when = _epoch_local_time(last_success[provider], settings.timezone)
            lines.append(f"上次成功：{when}")
        if blocked:
            lines.append(f"恢复方式：{_login_command(provider)}，登录并通过检测后自动继续。")
        elif provider in last_errors:
            lines.append(f"最近错误：{html_escape(last_errors[provider])}")
    if include_commands:
        lines.extend([
            "", "开关：/quota_refresh on 或 off",
            "手动触发：/quota_refresh claude 或 codex",
        ])
    return "\n".join(lines)


def format_recovery(provider: ProviderUsage, settings: Settings) -> str:
    return "\n".join(
        [
            "<b>AI订阅资源限额监控 检测恢复</b>",
            f"服务：{_provider_label(provider.provider)}",
            f"时间：{html_escape(local_time(provider.captured_at, settings.timezone))}",
        ]
    )


def _format_provider(
    provider: ProviderUsage,
    settings: Settings,
    now: datetime,
    login: LoginStartResult | None = None,
) -> list[str]:
    title = _format_provider_title(provider)
    enabled = settings.claude_enabled if provider.provider == "claude" else settings.codex_enabled
    if not enabled:
        return [f"<b>{title}</b>", "监控已禁用"]

    if not provider.ok:
        status = _PROVIDER_ERRORS.get(provider.error_kind, "异常")
        lines = [
            f"<b>{title}</b>",
            f"状态：{status}",
            f"原因：{html_escape(provider.error or '未知错误')}",
        ]
        if provider.error_kind == "auth_required":
            lines.extend(_format_login_action(provider.provider, login))
        return lines

    lines = [f"<b>{title}</b>", "状态：正常"]
    if not provider.windows:
        lines.append("用量：接口未返回限额窗口")
    for window in provider.windows:
        lines.append(_format_window(window, settings.timezone, now))
    credit_line = _format_credits(provider)
    if credit_line:
        lines.append(credit_line)
    return lines


def _format_auth_diagnostics(
    diagnostics: dict[str, Any],
    timezone: str,
    now: datetime,
) -> list[str]:
    status = _CREDENTIAL_STATUSES.get(diagnostics.get("status"), "未知")
    lines = [f"凭据状态：{status}"]
    if diagnostics.get("token_source") == "env":
        lines.append("凭据来源：环境变量")
    else:
        path = diagnostics.get("auth_path") or diagnostics.get("credentials_path") or "未找到"
        lines.append(f"凭据路径：{html_escape(path)}")
    if diagnostics.get("has_access_token"):
        expires_at = diagnostics.get("expires_at")
        if isinstance(expires_at, datetime):
            lines.append(f"凭据到期：{local_time_with_countdown(expires_at, timezone, now)}")
        if diagnostics.get("has_account_id") is False:
            lines.append("账号标识：缺失")
        if diagnostics.get("can_auto_refresh"):
            seconds = diagnostics.get("refresh_before_seconds") or 0
            refresh = "可用"
            if isinstance(expires_at, datetime) and seconds:
                refresh = f"到期前 {format_duration(seconds)}"
        else:
            refresh = "不可用"
        lines.append(f"自动刷新：{refresh}")
    if diagnostics.get("last_refresh"):
        last_refresh = diagnostics["last_refresh"]
        refreshed = parse_iso_datetime(str(last_refresh))
        when = local_time(refreshed, timezone) if refreshed else last_refresh
        lines.append(f"上次刷新：{html_escape(when)}")
    load_error = diagnostics.get("auth_load_error") or diagnostics.get("credentials_load_error")
    if load_error:
        lines.append(f"凭据读取错误：{html_escape(load_error)}")
    return lines


def _format_window(window: UsageWindow, timezone: str, now: datetime) -> str:
    usage = f"• {html_escape(window.label)}：已使用 {window.used_percent:.1f}%"
    if window.resets_at is None:
        return f"{usage}\n  重置时间未知"
    reset = local_time_with_countdown(window.resets_at, timezone, now)
    return f"{usage}\n  重置：{reset}"


def _format_credits(provider: ProviderUsage) -> str | None:
    if not provider.credits:
        return None
    parts = []
    if provider.credits.unlimited:
        parts.append("不限量")
    if provider.credits.balance is not None:
        parts.append(f"余额 {provider.credits.balance}")
    if provider.credits.used is not None and provider.credits.limit is not None:
        parts.append(f"已使用 {provider.credits.used}/{provider.credits.limit}")
    if provider.credits.remaining_percent is not None:
        parts.append(f"剩余 {provider.credits.remaining_percent:.1f}%")
    if not parts:
        return None
    return f"积分：{html_escape('，'.join(parts))}"


def _format_login_action(
    provider: str,
    login: LoginStartResult | None,
) -> list[str]:
    if login and not login.ok:
        return [f"无法生成登录链接：{html_escape(login.message)}"]

    if provider == "codex":
        if login and login.url and login.code:
            return [
                f'打开：<a href="{html_escape(login.url)}">Codex 登录页面</a>',
                f"验证码：{html_escape(login.code)}",
                "浏览器完成验证后，容器会自动保存登录态。",
            ]
        return ["发送 /login_codex 生成 Codex 浏览器登录链接和验证码。"]

    if login and login.url:
        return [
            f'打开：<a href="{html_escape(login.url)}">Claude 登录页面</a>',
            "登录页返回 code 后，发送：/login_code claude YOUR_CODE",
        ]
    return ["发送 /login_claude 生成 Claude 浏览器登录链接。"]


def _format_provider_title(provider: ProviderUsage) -> str:
    title = _provider_label(provider.provider)
    if provider.plan_type:
        title = f"{title} ({html_escape(provider.plan_type)})"
    return title


def _login_command(provider: str) -> str:
    return "/login_claude" if provider == "claude" else "/login_codex"


def _epoch_local_time(value: float, timezone: str, *, now: float | None = None) -> str:
    try:
        date = datetime.fromtimestamp(float(value), tz=UTC)
        if now is None:
            return local_time(date, timezone)
        return local_time_with_countdown(date, timezone, datetime.fromtimestamp(now, tz=UTC))
    except (TypeError, ValueError, OSError, OverflowError):
        return "未知"


def _provider_label(provider: str) -> str:
    return "Claude" if provider == "claude" else "Codex"
