from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Provider = Literal["claude", "codex"]


@dataclass(frozen=True)
class LoginStartResult:
    provider: Provider
    ok: bool
    message: str
    url: str | None = None
    code: str | None = None
    requires_code: bool = False
    already_running: bool = False


@dataclass(frozen=True)
class LoginCompleteResult:
    provider: Provider
    ok: bool
    message: str
