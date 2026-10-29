from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from infrastructure.providers.quota_refresh import _terminate_process


class CodexResetCreditClient:
    """Use the logged-in CLI's account RPCs without starting a model request."""

    def __init__(self, auth_path: Path | None, *, timeout_seconds: float = 45) -> None:
        self._auth_path = auth_path
        self._timeout = timeout_seconds

    async def read(self) -> dict[str, Any] | None:
        result = await self._request("account/rateLimits/read")
        summary = result.get("rateLimitResetCredits")
        if summary is None:
            return None
        if not isinstance(summary, dict) or type(summary.get("availableCount")) is not int:
            raise ValueError("Codex 重置卡数量格式异常")
        credits = summary.get("credits")
        if credits is not None and not isinstance(credits, list):
            raise ValueError("Codex 重置卡明细格式异常")
        for card in credits or []:
            if (
                not isinstance(card, dict)
                or not isinstance(card.get("id"), str)
                or not card["id"]
                or any(
                    card.get(key) is not None and type(card[key]) is not int
                    for key in ("expiresAt", "grantedAt")
                )
            ):
                raise ValueError("Codex 重置卡明细格式异常")
        return summary

    async def consume(self, credit_id: str, idempotency_key: str) -> str:
        if not credit_id or not idempotency_key:
            raise ValueError("重置卡 ID 和幂等键不能为空")
        result = await self._request(
            "account/rateLimitResetCredit/consume",
            {"creditId": credit_id, "idempotencyKey": idempotency_key},
        )
        outcome = result.get("outcome")
        if outcome not in {"reset", "alreadyRedeemed", "nothingToReset", "noCredit"}:
            raise ValueError("Codex 返回未知重置结果，请查询账号状态")
        return outcome

    async def _request(self, method: str, params: dict | None = None) -> dict[str, Any]:
        env = os.environ.copy()
        if self._auth_path:
            auth_path = self._auth_path.expanduser()
            if auth_path.name != "auth.json":
                raise ValueError("重置卡功能要求 CODEX_AUTH_PATH 指向 auth.json")
            env["CODEX_HOME"] = str(auth_path.resolve().parent)
        process = await asyncio.create_subprocess_exec(
            "codex", "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            limit=1024 * 1024,
        )

        async def send(message: dict) -> None:
            assert process.stdin is not None
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()

        async def receive(request_id: int) -> dict[str, Any]:
            assert process.stdout is not None
            while line := await process.stdout.readline():
                message = json.loads(line)
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    code = message["error"].get("code", "unknown")
                    raise RuntimeError(f"Codex app-server 请求失败（错误码 {code}）")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ValueError("Codex app-server 响应格式异常")
                return result
            raise RuntimeError("Codex app-server 已退出")

        try:
            async with asyncio.timeout(self._timeout):
                await send({"id": 1, "method": "initialize", "params": {
                    "clientInfo": {"name": "ai_limit_monitor", "version": "0.1.0"},
                }})
                await receive(1)
                await send({"method": "initialized"})
                request: dict[str, Any] = {"id": 2, "method": method}
                if params is not None:
                    request["params"] = params
                await send(request)
                return await receive(2)
        finally:
            await _terminate_process(process)
