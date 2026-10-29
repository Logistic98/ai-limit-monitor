from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from config.settings import Settings
from domain.auth import LoginCompleteResult, LoginStartResult
from domain.models import CheckResult, ProviderUsage, UsageWindow
from shared.utils import html_escape, local_time, parse_iso_datetime


def format_report(
    result: CheckResult,
    settings: Settings,
    logins: dict[str, LoginStartResult] | None = None,
    reset_credits: str | None = None,
) -> str:
    lines = [
        "<b>AI订阅资源限额监控</b>",
        f"时间：{html_escape(local_time(result.captured_at, settings.timezone))}",
        "",
    ]
    for provider in result.providers:
        login = logins.get(provider.provider) if logins else None
        lines.extend(_format_provider(provider, settings, login))
        lines.append("")
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
) -> str:
    lines = [
        "<b>AI订阅资源限额监控 诊断</b>",
        f"时间：{html_escape(local_time(result.captured_at, settings.timezone))}",
        f"检查间隔：{settings.check_interval_seconds}s",
        "",
    ]

    for provider in result.providers:
        status = "OK" if provider.ok else provider.error_kind or "异常"
        lines.append(f"<b>{_format_provider_title(provider)}</b>：{html_escape(status)}")
        if provider.error:
            lines.append(f"最近错误：{html_escape(provider.error)}")
        if provider.provider == "claude":
            lines.extend(_format_claude_auth_diagnostics(claude_auth, settings))
        elif provider.provider == "codex":
            lines.extend(_format_codex_auth_diagnostics(codex_auth, settings))
        lines.append("")

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
            f"原因：{html_escape(provider.error or 'unknown error')}",
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
            lines.append(f"下次执行：{_epoch_local_time(next_at, settings.timezone)}")
        for key, when in windows:
            window = key.partition(":")[2].removeprefix(f"{provider}.")
            label = {
                "claude_rate_limited": "限流恢复窗口",
                "primary_window": "主窗口", "secondary_window": "次窗口",
                "five_hour": "5 小时窗口", "seven_day": "7 天窗口",
                "seven_day_sonnet": "Sonnet 7 天窗口", "seven_day_opus": "Opus 7 天窗口",
            }.get(window, window)
            suffix = "（原定时间已过）" if when <= now and not active else ""
            lines.append(
                f"{html_escape(label)}：{_epoch_local_time(when, settings.timezone)}{suffix}",
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
    login: LoginStartResult | None = None,
) -> list[str]:
    title = _format_provider_title(provider)

    if not provider.ok:
        status = "需要重新登录" if provider.error_kind == "auth_required" else "异常"
        lines = [
            f"<b>{title}</b>",
            f"状态：{status}",
            f"原因：{html_escape(provider.error or 'unknown error')}",
        ]
        if provider.error_kind == "auth_required":
            lines.extend(_format_login_action(provider.provider, login))
        return lines

    lines = [f"<b>{title}</b>", "状态：OK"]
    if not provider.windows:
        lines.append("用量：接口未返回限额窗口")
    for window in provider.windows:
        lines.append(_format_window(window, settings))
    credit_line = _format_credits(provider)
    if credit_line:
        lines.append(credit_line)
    return lines


def _format_claude_auth_diagnostics(
    diagnostics: dict[str, Any],
    settings: Settings,
) -> list[str]:
    if not diagnostics.get("enabled", True):
        return ["Claude 监控：已禁用"]

    expires_text = _token_expiry_text(diagnostics, settings)

    lines = [
        f"凭据路径：{html_escape(diagnostics.get('credentials_path') or '未找到')}",
        f"access token：{_yes_no(bool(diagnostics.get('has_access_token')))}",
        f"refresh token：{_yes_no(bool(diagnostics.get('has_refresh_token')))}",
        f"可自动刷新：{_yes_no(bool(diagnostics.get('can_auto_refresh')))}",
        f"token 状态：{html_escape(diagnostics.get('status') or 'unknown')}",
        f"token 过期：{html_escape(expires_text)}",
        f"提前刷新窗口：{html_escape(diagnostics.get('refresh_before_seconds') or 0)}s",
    ]
    load_error = diagnostics.get("credentials_load_error")
    if load_error:
        lines.append(f"凭据读取错误：{html_escape(load_error)}")
    return lines


def _format_codex_auth_diagnostics(
    diagnostics: dict[str, Any],
    settings: Settings,
) -> list[str]:
    if not diagnostics.get("enabled", True):
        return ["Codex 监控：已禁用"]

    expires_text = _token_expiry_text(diagnostics, settings)

    lines = [
        f"凭据路径：{html_escape(diagnostics.get('auth_path') or '未找到')}",
        f"access token：{_yes_no(bool(diagnostics.get('has_access_token')))}",
        f"refresh token：{_yes_no(bool(diagnostics.get('has_refresh_token')))}",
        f"account id：{_yes_no(bool(diagnostics.get('has_account_id')))}",
        f"可自动刷新：{_yes_no(bool(diagnostics.get('can_auto_refresh')))}",
        f"token 状态：{html_escape(diagnostics.get('status') or 'unknown')}",
        f"token 过期：{html_escape(expires_text)}",
        f"提前刷新窗口：{html_escape(diagnostics.get('refresh_before_seconds') or 0)}s",
    ]
    if diagnostics.get("last_refresh"):
        refreshed = parse_iso_datetime(str(diagnostics["last_refresh"]))
        when = local_time(refreshed, settings.timezone) if refreshed else diagnostics["last_refresh"]
        lines.append(f"上次刷新：{html_escape(when)}")
    load_error = diagnostics.get("auth_load_error")
    if load_error:
        lines.append(f"凭据读取错误：{html_escape(load_error)}")
    return lines


def _token_expiry_text(diagnostics: dict[str, Any], settings: Settings) -> str:
    if not diagnostics.get("has_access_token"):
        return "未登录"
    expires_at = diagnostics.get("expires_at")
    if not isinstance(expires_at, datetime) or expires_at.timestamp() <= 0:
        return "未知"
    when = local_time(expires_at, settings.timezone)
    seconds = diagnostics.get("seconds_until_expiry")
    if not isinstance(seconds, int):
        return when
    minutes = abs(seconds) // 60
    remaining = f"已过期 {minutes} 分钟" if seconds < 0 else f"剩余 {minutes} 分钟"
    return f"{when}，{remaining}"


def _format_window(window: UsageWindow, settings: Settings) -> str:
    reset = html_escape(local_time(window.resets_at, settings.timezone))
    return f"• {html_escape(window.label)}：{window.used_percent:.1f}% ｜ 重置 {reset}"


def _format_credits(provider: ProviderUsage) -> str | None:
    if not provider.credits:
        return None
    parts = []
    if provider.credits.unlimited:
        parts.append("unlimited")
    if provider.credits.balance is not None:
        parts.append(f"balance {provider.credits.balance}")
    if provider.credits.used is not None and provider.credits.limit is not None:
        parts.append(f"used {provider.credits.used}/{provider.credits.limit}")
    if provider.credits.remaining_percent is not None:
        parts.append(f"{provider.credits.remaining_percent:.1f}% remaining")
    if not parts:
        return None
    return f"Credits：{html_escape(', '.join(parts))}"


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


def _epoch_local_time(value: float, timezone: str) -> str:
    try:
        return local_time(datetime.fromtimestamp(float(value), tz=UTC), timezone)
    except (TypeError, ValueError, OSError):
        return "未知"


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


def _provider_label(provider: str) -> str:
    return "Claude" if provider == "claude" else "Codex"
