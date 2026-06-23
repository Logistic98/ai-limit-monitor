from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

ProviderName = Literal["claude", "codex"]
ErrorKind = Literal["auth_required", "request_failed", "parse_failed"]


@dataclass(frozen=True)
class UsageWindow:
    """A normalized usage/rate-limit window."""

    key: str
    label: str
    used_percent: float
    resets_at: datetime | None = None
    window_minutes: int | None = None
    limit_reached: bool | None = None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)


@dataclass(frozen=True)
class CreditInfo:
    """Optional provider credit/overage information."""

    has_credits: bool | None = None
    unlimited: bool | None = None
    balance: str | None = None
    used: str | None = None
    limit: str | None = None
    remaining_percent: float | None = None
    resets_at: datetime | None = None


@dataclass(frozen=True)
class ProviderUsage:
    """Normalized usage result for one provider."""

    provider: ProviderName
    ok: bool
    captured_at: datetime
    windows: list[UsageWindow] = field(default_factory=list)
    credits: CreditInfo | None = None
    plan_type: str | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None
    raw: dict[str, Any] | None = None

    @property
    def max_used_percent(self) -> float:
        if not self.windows:
            return 0.0
        return max(window.used_percent for window in self.windows)

    @property
    def has_limit_reached(self) -> bool:
        return any(window.limit_reached for window in self.windows)


@dataclass(frozen=True)
class CheckResult:
    """Aggregated result for a monitor check."""

    captured_at: datetime
    providers: list[ProviderUsage]

    @property
    def ok(self) -> bool:
        return all(provider.ok for provider in self.providers)
