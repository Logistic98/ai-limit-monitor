from __future__ import annotations

import asyncio
import fcntl
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from html import escape
from pathlib import Path
from secrets import token_hex
from typing import Any
from uuid import uuid4

from application.quota_refresh_service import _wait_for_stop_or_wake
from infrastructure.providers.codex_reset_credits import CodexResetCreditClient
from infrastructure.storage.json_state import MonitorState
from presentation.reset_credits_formatter import credit_time, format_reset_credits, format_reset_tasks

logger = logging.getLogger(__name__)
_SUCCESS = {"reset", "alreadyRedeemed"}
_OUTCOMES = {
    "reset": "使用成功，限额已重置",
    "alreadyRedeemed": "此前已使用成功，已确认结果",
    "nothingToReset": "当前没有可重置的限额，卡片未使用，将在到期前重试",
    "noCredit": "指定卡片不可用，未使用其他卡片",
}


class ResetCreditService:
    def __init__(
        self, state_path: Path, client: CodexResetCreditClient,
        notify: Callable[[str], Awaitable[None]], *, timezone: str,
        query_interval: float = 600, auto_enabled: bool = True,
        use_before_seconds: float = 3600, notify_before_seconds: float = 86400,
        retry_seconds: float = 300, clock: Callable[[], float] = time.time,
    ) -> None:
        # A separate file and process lock keep CLI actions from overwriting monitor state.
        self._path = state_path.with_suffix(".reset-credits.json").expanduser()
        self._client = client
        self._notify = notify
        self._timezone = timezone
        self._query_interval = query_interval
        self._use_before_seconds = use_before_seconds
        self._notify_before_seconds = notify_before_seconds
        self._retry_seconds = retry_seconds
        self._config = {
            "auto_enabled": auto_enabled, "query_interval": query_interval,
            "use_before_seconds": use_before_seconds,
            "notify_before_seconds": notify_before_seconds, "retry_seconds": retry_seconds,
        }
        self._clock = clock
        self._state = MonitorState.load(self._path)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._next_query = 0.0

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[None]:
        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.with_suffix(".lock").open("a") as lock:
                async with asyncio.timeout(60):
                    while True:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            await asyncio.sleep(0.1)
                try:
                    self._state = MonitorState.load(self._path)
                    self._apply_config()
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _save(self) -> None:
        self._state.save(self._path)

    def _apply_config(self) -> None:
        previous = self._state.reset_credit_config
        if previous == self._config:
            return
        if previous.get("auto_enabled") != self._config["auto_enabled"]:
            self._state.reset_credit_auto_enabled = self._config["auto_enabled"]
        if any(
            previous.get(key, self._config[key]) != self._config[key]
            for key in ("use_before_seconds", "notify_before_seconds")
        ):
            for job in self._state.reset_credit_jobs.values():
                job.pop("scheduled_notified", None)
                job.pop("warning_notified", None)
        self._state.reset_credit_config = dict(self._config)
        self._save()

    async def _send(self, message: str) -> bool:
        try:
            await self._notify(message)
            return True
        except Exception:
            logger.warning("reset credit notification failed; will retry")
            return False

    async def _read(self) -> dict[str, Any] | None:
        summary = await self._client.read()
        jobs = self._state.reset_credit_jobs
        for card in (summary or {}).get("credits") or []:
            if card.get("status") != "available" or card.get("resetType") != "codexRateLimits":
                continue
            job = jobs.setdefault(card["id"], {})
            expiry = card.get("expiresAt")
            if job.get("expires_at") != expiry:
                job.pop("scheduled_notified", None)
                job.pop("warning_notified", None)
            job["expires_at"] = expiry
        self._save()
        self._next_query = self._clock() + self._query_interval
        return summary

    async def query(self) -> str:
        async with self._locked():
            summary = await self._read()
            await self._notify_jobs()
            self._wake.set()
            return format_reset_credits(
                summary, self._state.reset_credit_auto_enabled, self._timezone,
                self._use_before_seconds,
            )

    async def tasks(self) -> str:
        async with self._locked():
            return format_reset_tasks(
                self._state.reset_credit_jobs, self._state.reset_credit_auto_enabled,
                self._timezone, self._config, self._clock(),
            )

    async def diagnostics(self) -> str:
        summary = None
        query_status = "正常"
        try:
            async with self._locked():
                summary = await self._read()
        except Exception as exc:
            query_status = f"查询失败（{type(exc).__name__}），下方任务来自本地状态"
        if summary is None and query_status == "正常":
            query_status = "服务端未提供重置卡信息"
        return format_reset_tasks(
            self._state.reset_credit_jobs, self._state.reset_credit_auto_enabled,
            self._timezone, self._config, self._clock(),
            query_status=query_status, available_count=(summary or {}).get("availableCount"),
        )

    async def statistics(self) -> str:
        try:
            async with self._locked():
                summary = await self._read()
                lines = [
                    "<b>Codex 重置卡</b>",
                    f"自动使用：{'已开启' if self._state.reset_credit_auto_enabled else '已关闭'}",
                ]
                if summary is None:
                    return "\n".join([*lines, "可用数量：未知（服务端未提供信息）"])
                lines.append(f"可用数量：{summary['availableCount']}")
                cards = [
                    card for card in summary.get("credits") or []
                    if card.get("status") == "available"
                    and card.get("resetType") == "codexRateLimits"
                    and card.get("expiresAt") is not None
                    and card["expiresAt"] > self._clock()
                ]
                if cards:
                    expiry = min(card["expiresAt"] for card in cards)
                    lines.append(f"最近到期：{credit_time(expiry, self._timezone)}")
                    if self._state.reset_credit_auto_enabled:
                        use_at = expiry - self._use_before_seconds
                        lines.append(f"计划使用：{credit_time(use_at, self._timezone)}")
                elif summary["availableCount"]:
                    lines.append("最近到期：暂无明确到期信息")
                return "\n".join(lines)
        except Exception as exc:
            return f"<b>Codex 重置卡</b>\n统计查询失败（{type(exc).__name__}）"

    async def set_enabled(self, enabled: bool) -> str:
        async with self._locked():
            self._state.reset_credit_auto_enabled = enabled
            self._save()
        self._next_query = 0
        self._wake.set()
        return f"Codex 重置卡自动使用：{'已开启' if enabled else '已关闭'}"

    async def use(
        self, credit_id: str | None = None, *, context: str = "cli",
        confirmation: str | None = None,
    ) -> str:
        async with self._locked():
            jobs = self._state.reset_credit_jobs
            if confirmation is not None:
                credit_id = next((
                    key for key, job in jobs.items()
                    if job.get("confirmation", {}).get("context") == context
                    and job["confirmation"].get("token") == confirmation
                ), None)
                if credit_id is None:
                    return "确认码无效或已使用，请重新发起使用请求。"
                if jobs[credit_id]["confirmation"]["expires_at"] <= self._clock():
                    jobs[credit_id].pop("confirmation")
                    self._save()
                    return "确认码已过期，请重新发起使用请求。"
            summary = await self._read()
            cards = [
                card for card in (summary or {}).get("credits") or []
                if card.get("status") == "available"
                and card.get("resetType") == "codexRateLimits"
                and (card.get("expiresAt") is None or card["expiresAt"] > self._clock())
                and jobs.get(card["id"], {}).get("outcome") not in _SUCCESS
            ]
            if credit_id is None and cards:
                credit_id = min(cards, key=lambda c: c.get("expiresAt") or float("inf"))["id"]
            pending = self._state.reset_credit_jobs.get(credit_id or "", {}).get("pending")
            if not pending and not any(card["id"] == credit_id for card in cards):
                return "没有找到指定的可用重置卡，请先查询 /reset_codex_cards。"
            assert credit_id is not None
            if confirmation is None:
                for job in jobs.values():
                    if job.get("confirmation", {}).get("context") == context:
                        job.pop("confirmation")
                token = token_hex(8)
                jobs[credit_id]["confirmation"] = {
                    "context": context, "token": token, "expires_at": self._clock() + 300,
                }
                self._save()
                command = (
                    f"ai-limit-monitor reset-codex-use --confirm {token}" if context == "cli"
                    else f"/reset_codex_use confirm {token}"
                )
                return (
                    "<b>请二次确认使用 Codex 重置卡</b>\n"
                    f"卡片：<code>{escape(credit_id)}</code>\n"
                    f"到期：{credit_time(jobs[credit_id].get('expires_at'), self._timezone)}\n"
                    "使用后无法撤销。请在 5 分钟内发送以下命令确认：\n"
                    f"<code>{command}</code>\n未确认不会发起本次手动使用。"
                )
            jobs[credit_id].pop("confirmation")
            self._save()
            await self._consume(credit_id)
            outcome = self._state.reset_credit_jobs[credit_id].get("outcome")
            await self._read()
            self._wake.set()
            return f"Codex 重置卡：{_OUTCOMES.get(outcome, '结果尚未确认，请稍后查询')}"

    async def _consume(self, credit_id: str) -> None:
        job = self._state.reset_credit_jobs[credit_id]
        if job.get("outcome") in _SUCCESS:
            return
        job.setdefault("idempotency_key", str(uuid4()))
        job["pending"] = True
        job["retry_at"] = self._clock() + self._retry_seconds
        # Persist the key before any irreversible request; fail closed if storage fails.
        self._save()
        outcome = await self._client.consume(credit_id, job["idempotency_key"])
        job["pending"] = False
        job["outcome"] = outcome
        if outcome not in _SUCCESS:
            job.pop("idempotency_key", None)
        self._save()
        logger.info("reset credit consume completed credit=%s outcome=%s", credit_id, outcome)
        await self._notify_outcome(credit_id, job)

    async def _notify_outcome(self, credit_id: str, job: dict) -> None:
        outcome = job.get("outcome")
        if outcome and job.get("notified_outcome") != outcome:
            sent = await self._send(
                f"<b>Codex 重置卡使用结果</b>\n卡片：<code>{escape(credit_id)}</code>\n"
                f"{_OUTCOMES[outcome]}\n"
                f"到期：{credit_time(job.get('expires_at'), self._timezone)}"
            )
            if sent:
                job["notified_outcome"] = outcome
                self._save()

    async def _notify_jobs(self) -> None:
        for credit_id, job in self._state.reset_credit_jobs.items():
            await self._notify_outcome(credit_id, job)
            expiry = job.get("expires_at")
            if job.get("outcome") in _SUCCESS or expiry is None:
                continue
            label = f"卡片：<code>{escape(credit_id)}</code>\n"
            if expiry <= self._clock():
                if not job.get("pending") and not job.get("expired_notified"):
                    job["expired_notified"] = await self._send(
                        f"Codex 重置卡已到期，未再发起使用请求。\n{label}",
                    )
                    self._save()
                continue
            if not self._state.reset_credit_auto_enabled:
                continue
            use_at = expiry - self._use_before_seconds
            if not job.get("scheduled_notified"):
                job["scheduled_notified"] = await self._send(
                    f"<b>已创建 Codex 重置卡定时任务</b>\n{label}"
                    f"到期：{credit_time(expiry, self._timezone)}\n"
                    f"计划使用：{credit_time(use_at, self._timezone)}\n"
                    f"提前提醒：{credit_time(use_at - self._notify_before_seconds, self._timezone)}"
                )
                self._save()
            if (
                not job.get("warning_notified")
                and self._clock() >= use_at - self._notify_before_seconds
            ):
                job["warning_notified"] = await self._send(
                    f"<b>Codex 重置卡使用前提醒</b>\n{label}"
                    f"计划使用：{credit_time(use_at, self._timezone)}\n"
                    f"到期：{credit_time(expiry, self._timezone)}\n"
                    "距计划使用已进入提前提醒时段，将按计划自动使用。"
                )
                self._save()

    async def run_due(self) -> None:
        async with self._locked():
            now = self._clock()
            jobs = self._state.reset_credit_jobs
            due = any(self._is_due(job, now) for job in jobs.values())
            if now >= self._next_query or due:
                await self._read()
            await self._notify_jobs()
            for credit_id, job in sorted(
                jobs.items(), key=lambda item: item[1].get("expires_at") or float("inf"),
            ):
                if self._is_due(job, self._clock()):
                    await self._consume(credit_id)
                    await self._read()
            if self._state.provider_errors.get("reset_credits"):
                await self._notify("Codex 重置卡查询与调度已恢复。")
                self._state.provider_errors.pop("reset_credits", None)
                self._save()

    def _is_due(self, job: dict, now: float) -> bool:
        if job.get("outcome") in _SUCCESS or now < job.get("retry_at", 0):
            return False
        if job.get("pending"):
            return True
        expiry = job.get("expires_at")
        return (
            self._state.reset_credit_auto_enabled and expiry is not None
            and expiry - self._use_before_seconds <= now < expiry
        )

    def _next_delay(self) -> float:
        now = self._clock()
        deadlines = [self._next_query, now + 60]
        if self._state.reset_credit_auto_enabled:
            for job in self._state.reset_credit_jobs.values():
                expiry = job.get("expires_at")
                if expiry is None or expiry <= now or job.get("outcome") in _SUCCESS:
                    continue
                use_at = expiry - self._use_before_seconds
                deadlines.append(max(use_at, job.get("retry_at", 0)))
                warn_at = use_at - self._notify_before_seconds
                if not job.get("warning_notified") and warn_at > now:
                    deadlines.append(warn_at)
        return max(0.05, min(deadlines) - now)

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            self._wake.clear()
            try:
                await self.run_due()
            except Exception as exc:
                logger.warning("reset credit scheduler failed: %s", type(exc).__name__)
                with suppress(Exception):
                    async with self._locked():
                        if not self._state.provider_errors.get("reset_credits"):
                            await self._notify(
                                "Codex 重置卡查询或使用失败，将稍后重试。"
                                "请检查 Codex 登录状态与 CLI 版本。"
                            )
                            self._state.provider_errors["reset_credits"] = True
                            self._save()
                await _wait_for_stop_or_wake(stop_event, self._wake, 60)
                continue
            await _wait_for_stop_or_wake(stop_event, self._wake, self._next_delay())
