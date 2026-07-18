from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MonitorState:
    """Persistent state for alert deduplication and post-reset request scheduling."""

    alert_levels: dict[str, float] = field(default_factory=dict)
    provider_errors: dict[str, bool] = field(default_factory=dict)
    provider_error_kinds: dict[str, str] = field(default_factory=dict)
    proactive_quota_refresh_enabled: bool = True
    quota_refresh_scheduled_at: dict[str, float] = field(default_factory=dict)
    quota_refresh_last_attempt_at: dict[str, float] = field(default_factory=dict)
    quota_refresh_attempt_counts: dict[str, int] = field(default_factory=dict)
    quota_refresh_last_success_at: dict[str, float] = field(default_factory=dict)
    quota_refresh_last_errors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> MonitorState:
        expanded = path.expanduser()
        if not expanded.exists():
            return cls()
        try:
            data = json.loads(expanded.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        return cls(
            alert_levels=_dict_float(data.get("alert_levels")),
            provider_errors=_dict_bool(data.get("provider_errors")),
            provider_error_kinds=_dict_str(data.get("provider_error_kinds")),
            proactive_quota_refresh_enabled=_strict_bool(
                data.get("proactive_quota_refresh_enabled"),
                default=True,
            ),
            quota_refresh_scheduled_at=_dict_float(data.get("quota_refresh_scheduled_at")),
            quota_refresh_last_attempt_at=_dict_float(data.get("quota_refresh_last_attempt_at")),
            quota_refresh_attempt_counts=_dict_int(data.get("quota_refresh_attempt_counts")),
            quota_refresh_last_success_at=_dict_float(data.get("quota_refresh_last_success_at")),
            quota_refresh_last_errors=_dict_str(data.get("quota_refresh_last_errors")),
        )

    def save(self, path: Path) -> None:
        expanded = path.expanduser()
        expanded.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = expanded.with_suffix(expanded.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(
                {
                    "alert_levels": self.alert_levels,
                    "provider_errors": self.provider_errors,
                    "provider_error_kinds": self.provider_error_kinds,
                    "proactive_quota_refresh_enabled": (self.proactive_quota_refresh_enabled),
                    "quota_refresh_scheduled_at": self.quota_refresh_scheduled_at,
                    "quota_refresh_last_attempt_at": self.quota_refresh_last_attempt_at,
                    "quota_refresh_attempt_counts": self.quota_refresh_attempt_counts,
                    "quota_refresh_last_success_at": self.quota_refresh_last_success_at,
                    "quota_refresh_last_errors": self.quota_refresh_last_errors,
                    "updated_at": time.time(),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        tmp_path.replace(expanded)


def _dict_float(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, item in value.items():
        try:
            result[str(key)] = float(item)
        except (TypeError, ValueError):
            continue
    return result


def _dict_int(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, item in value.items():
        try:
            result[str(key)] = int(item)
        except (TypeError, ValueError):
            continue
    return result


def _dict_bool(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {str(key): bool(item) for key, item in value.items()}


def _dict_str(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def _strict_bool(value: Any, *, default: bool) -> bool:
    return value if isinstance(value, bool) else default
