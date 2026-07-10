from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
from dataclasses import dataclass

from domain.models import ProviderName


@dataclass(frozen=True)
class QuotaRefreshResult:
    """Result of one lightweight provider request."""

    provider: ProviderName
    ok: bool
    error: str | None = None


class QuotaRefreshClient:
    """Starts a new quota window through the provider's authenticated CLI."""

    def __init__(self, *, timeout_seconds: float = 120.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def request(self, provider: ProviderName) -> QuotaRefreshResult:
        command = _command_for(provider)
        executable = shutil.which(command[0])
        if not executable:
            return QuotaRefreshResult(
                provider=provider,
                ok=False,
                error="executable_not_found",
            )

        process = await asyncio.create_subprocess_exec(
            executable,
            *command[1:],
            cwd=tempfile.gettempdir(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            return_code = await asyncio.wait_for(
                process.wait(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            await _terminate_process(process)
            return QuotaRefreshResult(provider=provider, ok=False, error="timeout")
        except asyncio.CancelledError:
            await _terminate_process(process)
            raise

        if return_code != 0:
            return QuotaRefreshResult(
                provider=provider,
                ok=False,
                error=f"exit_code_{return_code}",
            )
        return QuotaRefreshResult(provider=provider, ok=True)


def _command_for(provider: ProviderName) -> tuple[str, ...]:
    if provider == "claude":
        return (
            "claude",
            "--print",
            "--no-session-persistence",
            "--max-turns",
            "1",
            "--tools",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-chrome",
            "--effort",
            "low",
            "--system-prompt",
            "Reply with exactly OK. Do not use tools.",
            "OK",
        )
    return (
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--config",
        'model_reasoning_effort="low"',
        "Reply with exactly OK. Do not inspect files or use tools.",
    )


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=5)
        return
    except TimeoutError:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        await process.wait()
        return
    await process.wait()
