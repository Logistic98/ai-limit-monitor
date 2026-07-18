from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from config.settings import Settings

CommandHandler = Callable[[str, str], Awaitable[str | None]]

logger = logging.getLogger(__name__)


class TelegramClient:
    """Small Telegram Bot API client with sendMessage and long polling."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._base_url = f"https://api.telegram.org/bot{settings.telegram_token_value}"
        self._command_tasks: set[asyncio.Task[None]] = set()

    async def send_message(self, text: str, chat_id: str | None = None) -> None:
        target_chat_id = chat_id or self._settings.telegram_chat_id
        logger.info("sending telegram message chat_id=%s chars=%s", target_chat_id, len(text))
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                response = await self._http.post(
                    f"{self._base_url}/sendMessage",
                    json={
                        "chat_id": target_chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                response.raise_for_status()
                return
            except httpx.TransportError as exc:
                if attempt == attempts:
                    raise
                logger.warning(
                    "telegram sendMessage transient failure (attempt %s/%s): %s",
                    attempt,
                    attempts,
                    exc.__class__.__name__,
                )
                await asyncio.sleep(2 * attempt)

    async def delete_webhook(self) -> None:
        response = await self._http.post(
            f"{self._base_url}/deleteWebhook",
            json={"drop_pending_updates": False},
        )
        response.raise_for_status()
        logger.info("telegram webhook disabled for long polling")

    async def set_commands(self) -> None:
        commands = [
            {"command": "check", "description": "检查登录状态"},
            {"command": "monitor", "description": "立即监控并返回完整报告"},
            {"command": "diagnose", "description": "输出脱敏诊断信息"},
            {"command": "login_claude", "description": "生成 Claude 登录链接"},
            {"command": "login_codex", "description": "生成 Codex 登录链接和验证码"},
            {"command": "login_code", "description": "提交 Claude 登录 code"},
            {"command": "quota_refresh", "description": "查看或切换限额恢复主动请求"},
            {"command": "ping", "description": "健康检查"},
            {"command": "help", "description": "显示帮助"},
        ]
        response = await self._http.post(
            f"{self._base_url}/setMyCommands",
            json={"commands": commands},
        )
        response.raise_for_status()
        logger.info("telegram command menu registered")

    async def set_chat_menu_button(self, chat_id: str) -> None:
        response = await self._http.post(
            f"{self._base_url}/setChatMenuButton",
            json={"chat_id": chat_id, "menu_button": {"type": "commands"}},
        )
        response.raise_for_status()
        logger.info("telegram chat menu button registered chat_id=%s", chat_id)

    async def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        response = await self._http.get(
            f"{self._base_url}/getUpdates",
            params=params,
            timeout=timeout + 10,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result", [])
        return result if isinstance(result, list) else []

    async def poll_commands(
        self,
        handler: CommandHandler,
        stop_event: asyncio.Event,
    ) -> None:
        with contextlib.suppress(Exception):
            await self.delete_webhook()
        with contextlib.suppress(Exception):
            await self.set_commands()
        for chat_id in self._settings.telegram_allowed_chat_ids:
            with contextlib.suppress(Exception):
                await self.set_chat_menu_button(chat_id)

        logger.info("telegram long polling started")
        offset: int | None = None
        conflict_reported = False
        while not stop_event.is_set():
            try:
                updates = await self.get_updates(offset=offset, timeout=30)
                conflict_reported = False
            except Exception as exc:
                if _is_get_updates_conflict(exc):
                    if not conflict_reported:
                        message = (
                            "Telegram 命令接收冲突：同一个 Bot token 正在被另一个程序接收消息。"
                            "当前服务可以发送通知，但收不到 /check、/monitor、/login_code 等命令。"
                            "请停止另一个 Bot 实例，或为本服务更换独立的 Bot token。"
                        )
                        logger.warning("telegram polling conflict: another bot instance is running")
                        with contextlib.suppress(Exception):
                            await self.send_message(message)
                        conflict_reported = True
                    await asyncio.sleep(60)
                    continue
                logger.warning("telegram getUpdates failed: %s", _safe_error(exc))
                await asyncio.sleep(5)
                continue

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1

                message = update.get("message")
                if not isinstance(message, dict):
                    continue
                chat = message.get("chat")
                if not isinstance(chat, dict):
                    continue
                chat_id = str(chat.get("id"))
                text = message.get("text")
                if not isinstance(text, str):
                    continue
                if not self._is_allowed(chat_id):
                    continue

                logger.info(
                    "received telegram command chat_id=%s text=%s",
                    chat_id,
                    _safe_text(text),
                )
                # Handle each command in its own task so a slow command (e.g. a manual
                # quota refresh running a provider CLI) never blocks polling.
                task = asyncio.create_task(self._run_command(handler, chat_id, text.strip()))
                self._command_tasks.add(task)
                task.add_done_callback(self._command_tasks.discard)

        if self._command_tasks:
            await asyncio.gather(*self._command_tasks, return_exceptions=True)

    async def _run_command(self, handler: CommandHandler, chat_id: str, text: str) -> None:
        try:
            reply = await handler(chat_id, text)
            if reply:
                await self.send_message(reply, chat_id=chat_id)
        except Exception:
            logger.exception("telegram command handling failed")
            with contextlib.suppress(Exception):
                await self.send_message("命令处理失败，请查看容器日志。", chat_id=chat_id)

    def _is_allowed(self, chat_id: str) -> bool:
        return chat_id in set(self._settings.telegram_allowed_chat_ids)


def _is_get_updates_conflict(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        description = ""
        with contextlib.suppress(Exception):
            payload = exc.response.json()
            if isinstance(payload, dict):
                description = str(payload.get("description") or "")
        suffix = f": {description}" if description else ""
        return f"HTTP {exc.response.status_code} from Telegram Bot API{suffix}"
    return exc.__class__.__name__


def _safe_text(text: str) -> str:
    parts = text.strip().split(maxsplit=2)
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    if len(parts) >= 2 and command in {"/login_code", "/login-code"}:
        return f"{parts[0]} {parts[1]} ***"
    return text.strip()[:120]
