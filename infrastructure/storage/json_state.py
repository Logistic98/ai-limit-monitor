from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MonitorState:
    """Persistent state used to deduplicate threshold alerts."""

    alert_levels: dict[str, float] = field(default_factory=dict)
    provider_errors: dict[str, bool] = field(default_factory=dict)
    provider_error_kinds: dict[str, str] = field(default_factory=dict)
    last_report_at: float = 0.0

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
            last_report_at=float(data.get("last_report_at") or 0.0),
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
                    "last_report_at": self.last_report_at,
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


def _dict_bool(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {str(key): bool(item) for key, item in value.items()}


def _dict_str(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}
