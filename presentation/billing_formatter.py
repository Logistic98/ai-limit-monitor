from __future__ import annotations

from datetime import datetime

from domain.models import CodexSubscription
from shared.utils import local_time_with_countdown


def format_codex_subscription(
    subscription: CodexSubscription, now: datetime, timezone: str,
) -> str:
    if not subscription.active:
        return "当前无有效付费订阅。"
    if subscription.will_renew is False:
        if subscription.cancels_at:
            ends = local_time_with_countdown(subscription.cancels_at, timezone, now)
            return f"自动续费已关闭，订阅结束时间：{ends}"
        return "自动续费已关闭。"
    if subscription.renews_at is None:
        return "下次续费日期：未知"

    renews = local_time_with_countdown(subscription.renews_at, timezone, now)
    return f"下次续费日期：{renews}"
