from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo


def credit_time(timestamp: float | None, timezone: str) -> str:
    if timestamp is None:
        return "无到期时间"
    return datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S")


def format_reset_credits(
    summary: dict[str, Any] | None, enabled: bool, timezone: str,
    use_before_seconds: float = 3600,
) -> str:
    lines = ["<b>Codex 重置卡</b>", f"自动使用：{'已开启' if enabled else '已关闭'}"]
    if summary is None:
        return "\n".join([*lines, "服务端未提供重置卡信息，无法确认数量或创建计划。"])
    lines.append(f"可用数量：{summary['availableCount']}")
    credits = summary.get("credits")
    if credits is None or len(credits) < summary["availableCount"]:
        lines.append("部分或全部明细尚未提供，只为有到期时间的可用卡片安排任务。")
    for card in credits or []:
        lines.extend([
            f"\n卡片：<code>{escape(card['id'])}</code>",
            f"状态：{escape(str(card.get('status', 'unknown')))}",
            f"发放：{credit_time(card.get('grantedAt'), timezone)}",
            f"到期：{credit_time(card.get('expiresAt'), timezone)}",
        ])
        if card.get("expiresAt") is not None and card.get("status") == "available":
            lines.append(
                f"计划使用：{credit_time(card['expiresAt'] - use_before_seconds, timezone)}",
            )
    return "\n".join(lines)


def format_reset_tasks(
    jobs: dict[str, dict[str, Any]], enabled: bool, timezone: str,
    config: dict[str, Any], now: float,
    *, query_status: str | None = None, available_count: int | None = None,
) -> str:
    lines = ["<b>Codex 重置卡自动任务</b>"]
    if query_status is not None:
        lines.extend([
            f"查询状态：{escape(query_status)}",
            f"可用数量：{available_count if available_count is not None else '未知'}",
            "手动使用：需要二次确认",
        ])
    lines.extend([
        f"自动使用：{'已开启' if enabled else '已关闭'}",
        f"自动查询间隔：{config['query_interval']:g} 秒",
        f"使用时间：到期前 {config['use_before_seconds'] / 60:g} 分钟",
        f"提前提醒：计划使用前 {config['notify_before_seconds'] / 3600:g} 小时",
        f"失败重试间隔：{config['retry_seconds']:g} 秒",
    ])
    tasks = [
        (credit_id, job) for credit_id, job in jobs.items()
        if job.get("outcome") not in {"reset", "alreadyRedeemed"}
        and job.get("expires_at") is not None
        and (job["expires_at"] > now or job.get("pending"))
    ]
    if not tasks:
        lines.append("\n暂无待执行任务，自动查询发现新卡后会创建计划。")
    for credit_id, job in sorted(tasks, key=lambda item: item[1]["expires_at"]):
        expiry = job["expires_at"]
        use_at = expiry - config["use_before_seconds"]
        status = "已安排" if enabled else "已暂停"
        if job.get("pending"):
            status = "使用结果待确认"
        elif enabled and job.get("retry_at", 0) > now:
            status = "等待重试"
        lines.extend([
            f"\n卡片：<code>{escape(credit_id)}</code>",
            f"任务状态：{status}",
            f"到期：{credit_time(expiry, timezone)}",
            f"计划使用：{credit_time(use_at, timezone)}",
            f"提前提醒：{credit_time(use_at - config['notify_before_seconds'], timezone)}",
            f"提醒状态：{'已发送' if job.get('warning_notified') else '未发送'}",
        ])
        if job.get("retry_at"):
            lines.append(f"下次尝试：{credit_time(max(now, job['retry_at']), timezone)}")
        if job.get("outcome") == "nothingToReset":
            lines.append("最近结果：当前没有可重置的限额，卡片未使用。")
        elif job.get("outcome") == "noCredit":
            lines.append("最近结果：指定卡片不可用。")
    return "\n".join(lines)
