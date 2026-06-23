from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass

from domain.auth import LoginCompleteResult, LoginStartResult, Provider

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s\x1b]+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CODE_RE = re.compile(r"\b[A-Z0-9]{4,8}-[A-Z0-9]{4,8}\b")


@dataclass
class _LoginProcess:
    provider: Provider
    process: asyncio.subprocess.Process
    output: str
    created_at: float
    url: str | None = None
    code: str | None = None
    requires_code: bool = False


class CliLoginManager:
    """Starts Claude/Codex CLI login flows inside the container.

    Codex uses device-code login and completes after the user enters the code in a browser.
    Claude opens an OAuth URL and expects a returned code to be submitted back to stdin.
    """

    def __init__(self, initial_read_timeout_seconds: float = 8.0) -> None:
        self._sessions: dict[Provider, _LoginProcess] = {}
        self._initial_read_timeout_seconds = initial_read_timeout_seconds
        self._lock = asyncio.Lock()

    async def start(self, provider: Provider) -> LoginStartResult:
        async with self._lock:
            return await self._start_locked(provider)

    async def _start_locked(self, provider: Provider) -> LoginStartResult:
        logger.info("starting cli login provider=%s", provider)
        existing = self._sessions.get(provider)
        if existing and existing.process.returncode is None:
            await self._refresh_session(existing, timeout_seconds=1.0)
            if existing.url or existing.code:
                return LoginStartResult(
                    provider=provider,
                    ok=True,
                    message="登录流程已经在等待用户操作。",
                    url=existing.url,
                    code=existing.code,
                    requires_code=existing.requires_code,
                    already_running=True,
                )
            logger.warning("restarting stale login process provider=%s", provider)
            _terminate_process(existing.process)
            self._sessions.pop(provider, None)

        command = _command_for_provider(provider)
        executable = shutil.which(command[0])
        if not executable:
            logger.error("cli command not found provider=%s command=%s", provider, command[0])
            return LoginStartResult(
                provider=provider,
                ok=False,
                message=f"容器内没有找到命令: {command[0]}",
            )

        env = os.environ.copy()
        env.update(
            {
                "NO_COLOR": "1",
                "FORCE_COLOR": "0",
                "TERM": "dumb",
            }
        )

        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *command[1:],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except Exception as exc:
            logger.exception("failed to start cli login provider=%s", provider)
            return LoginStartResult(
                provider=provider,
                ok=False,
                message=f"启动登录命令失败: {exc}",
            )

        output = await _read_available_output(process, self._initial_read_timeout_seconds)
        clean_output = _clean_output(output)
        url = _first_url(clean_output)
        code = _first_code(clean_output)
        requires_code = provider == "claude"

        session = _LoginProcess(
            provider=provider,
            process=process,
            output=clean_output,
            created_at=time.time(),
            url=url,
            code=code,
            requires_code=requires_code,
        )
        self._sessions[provider] = session

        logger.info(
            "cli login initialized provider=%s has_url=%s has_code=%s requires_code=%s",
            provider,
            bool(url),
            bool(code),
            requires_code,
        )

        if not url and process.returncode is not None:
            return LoginStartResult(
                provider=provider,
                ok=False,
                message=clean_output.strip() or "登录命令已退出，但没有输出登录链接。",
            )

        return LoginStartResult(
            provider=provider,
            ok=True,
            message="登录流程已启动。",
            url=url,
            code=code,
            requires_code=requires_code,
        )

    async def submit_code(self, provider: Provider, code: str) -> LoginCompleteResult:
        async with self._lock:
            return await self._submit_code_locked(provider, code)

    async def _submit_code_locked(self, provider: Provider, code: str) -> LoginCompleteResult:
        logger.info("submitting cli login code provider=%s", provider)
        session = self._sessions.get(provider)
        if not session or session.process.returncode is not None:
            return LoginCompleteResult(
                provider=provider,
                ok=False,
                message="没有正在等待 code 的登录流程。",
            )
        if provider != "claude":
            return LoginCompleteResult(
                provider=provider,
                ok=False,
                message="Codex 不需要把 code 发回 Bot。",
            )
        if not session.process.stdin:
            return LoginCompleteResult(
                provider=provider,
                ok=False,
                message="登录流程 stdin 不可用。",
            )

        session.process.stdin.write((code.strip() + "\n").encode("utf-8"))
        await session.process.stdin.drain()
        try:
            await asyncio.wait_for(session.process.wait(), timeout=60)
        except TimeoutError:
            return LoginCompleteResult(
                provider=provider,
                ok=False,
                message="已提交 code，但登录命令仍未结束。",
            )

        output = session.output + _clean_output(
            await _read_available_output(session.process, timeout_seconds=1.0)
        )
        self._sessions.pop(provider, None)

        if session.process.returncode == 0:
            logger.info("cli login completed provider=%s", provider)
            return LoginCompleteResult(provider=provider, ok=True, message="Claude 登录已完成。")
        logger.warning(
            "cli login failed provider=%s returncode=%s",
            provider,
            session.process.returncode,
        )
        return LoginCompleteResult(
            provider=provider,
            ok=False,
            message=output.strip() or f"Claude 登录失败，退出码 {session.process.returncode}。",
        )

    async def poll_completed(self) -> list[LoginCompleteResult]:
        async with self._lock:
            return await self._poll_completed_locked()

    async def _poll_completed_locked(self) -> list[LoginCompleteResult]:
        results: list[LoginCompleteResult] = []
        for provider, session in list(self._sessions.items()):
            if session.process.returncode is None:
                continue
            output = session.output + _clean_output(
                await _read_available_output(session.process, timeout_seconds=0.2)
            )
            self._sessions.pop(provider, None)
            if session.process.returncode == 0:
                results.append(
                    LoginCompleteResult(provider=provider, ok=True, message="登录已完成。")
                )
            else:
                results.append(
                    LoginCompleteResult(
                        provider=provider,
                        ok=False,
                        message=(
                            output.strip()
                            or f"登录失败，退出码 {session.process.returncode}。"
                        ),
                    )
                )
        return results

    async def _refresh_session(self, session: _LoginProcess, timeout_seconds: float) -> None:
        output = _clean_output(await _read_available_output(session.process, timeout_seconds))
        if not output:
            return
        session.output += output
        session.url = session.url or _first_url(session.output)
        session.code = session.code or _first_code(session.output)


def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()


def _command_for_provider(provider: Provider) -> list[str]:
    if provider == "claude":
        return ["claude", "auth", "login"]
    return ["codex", "login", "--device-auth"]


async def _read_available_output(
    process: asyncio.subprocess.Process,
    timeout_seconds: float,
) -> str:
    streams = [stream for stream in (process.stdout, process.stderr) if stream is not None]
    if not streams:
        return ""

    output = bytearray()
    deadline = time.monotonic() + timeout_seconds
    pending = {asyncio.create_task(stream.read(1024)): stream for stream in streams}
    try:
        while pending and time.monotonic() < deadline:
            timeout = max(0.05, deadline - time.monotonic())
            done, _ = await asyncio.wait(
                pending.keys(),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break
            for task in done:
                stream = pending.pop(task)
                chunk = task.result()
                if chunk:
                    output.extend(chunk)
                    pending[asyncio.create_task(stream.read(1024))] = stream
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending.keys(), return_exceptions=True)
    return output.decode("utf-8", errors="replace")


def _clean_output(output: str) -> str:
    return _ANSI_RE.sub("", output).replace("\r", "")


def _first_url(output: str) -> str | None:
    match = _URL_RE.search(output)
    return match.group(0).rstrip(".,)") if match else None


def _first_code(output: str) -> str | None:
    match = _CODE_RE.search(output)
    return match.group(0) if match else None
