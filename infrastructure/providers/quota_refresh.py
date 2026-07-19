from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import tempfile
from dataclasses import dataclass

from domain.models import ProviderName

logger = logging.getLogger(__name__)

# CLI error output is only used for diagnostics; keep the tail small enough for
# Telegram messages while still carrying the actual failure reason.
_OUTPUT_TAIL_CHARS = 300
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# Codex mandates a kernel sandbox (Landlock/seccomp on Linux) which is often
# unavailable inside containers; these markers identify that failure mode.
_SANDBOX_FAILURE_MARKERS = ("landlock", "seccomp", "sandbox")
# CLI output markers that mean the provider login itself is broken; retrying the
# request without a re-login cannot succeed.
_AUTH_FAILURE_MARKERS = (
    "401",
    "unauthorized",
    "failed to authenticate",
    "invalid authentication",
    "invalid_grant",
    "refresh token expired",
    "access token has expired",
    "re-authenticate",
    "please run /login",
    "not logged in",
)


def is_auth_failure(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return any(marker in lowered for marker in _AUTH_FAILURE_MARKERS)


@dataclass(frozen=True)
class QuotaRefreshResult:
    """Result of one lightweight provider request."""

    provider: ProviderName
    ok: bool
    error: str | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    gave_up: bool = False
    auth_blocked: bool = False


class QuotaRefreshClient:
    """Starts a new quota window through the provider's authenticated CLI."""

    def __init__(self, *, timeout_seconds: float = 120.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def request(self, provider: ProviderName) -> QuotaRefreshResult:
        result = await self._run(provider, _command_for(provider))
        if (
            provider == "codex"
            and not result.ok
            and result.error
            and _is_sandbox_failure(result.error)
        ):
            # The container itself is the isolation boundary; retry once without
            # the kernel sandbox so the request can still go through.
            logger.warning("codex kernel sandbox unavailable, retrying with danger-full-access")
            fallback = await self._run(provider, _codex_command(sandbox="danger-full-access"))
            if fallback.ok:
                return fallback
            return QuotaRefreshResult(
                provider=provider,
                ok=False,
                error=f"{result.error} | no-sandbox retry: {fallback.error}",
            )
        return result

    async def _run(
        self,
        provider: ProviderName,
        command: tuple[str, ...],
    ) -> QuotaRefreshResult:
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            output_bytes, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            await _terminate_process(process)
            return QuotaRefreshResult(
                provider=provider,
                ok=False,
                error=f"timeout_{int(self._timeout_seconds)}s",
            )
        except asyncio.CancelledError:
            await _terminate_process(process)
            raise

        if process.returncode != 0:
            output_tail = _output_tail(output_bytes)
            logger.warning(
                "quota refresh command failed provider=%s exit_code=%s output=%s",
                provider,
                process.returncode,
                output_tail or "<empty>",
            )
            error = f"exit_code_{process.returncode}"
            if output_tail:
                error = f"{error}: {output_tail}"
            return QuotaRefreshResult(provider=provider, ok=False, error=error)
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
    return _codex_command(sandbox="read-only")


def _codex_command(*, sandbox: str) -> tuple[str, ...]:
    return (
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "--color",
        "never",
        "--config",
        'model_reasoning_effort="low"',
        "Reply with exactly OK. Do not inspect files or use tools.",
    )


def _is_sandbox_failure(error: str) -> bool:
    lowered = error.lower()
    return any(marker in lowered for marker in _SANDBOX_FAILURE_MARKERS)


def _output_tail(output_bytes: bytes | None) -> str:
    if not output_bytes:
        return ""
    text = output_bytes.decode("utf-8", errors="replace")
    text = _ANSI_ESCAPE_PATTERN.sub("", text)
    text = " ".join(text.split())
    return text[-_OUTPUT_TAIL_CHARS:]


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
