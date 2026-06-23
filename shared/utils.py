from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def load_json_file(path: Path) -> dict[str, Any]:
    with path.expanduser().open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def first_existing_path(paths: Iterable[Path | None]) -> Path | None:
    for path in paths:
        if path and path.expanduser().exists():
            return path.expanduser()
    return None


def nested_get(data: dict[str, Any], *path: str) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def parse_epoch_seconds(value: int | float | str | None) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=UTC)


def jwt_payload(token: str | None) -> dict[str, Any]:
    if not token or token.count(".") < 2:
        return {}
    payload = token.split(".", 2)[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def local_time(value: datetime | None, timezone: str) -> str:
    if value is None:
        return "未知"
    zone = ZoneInfo(timezone)
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def window_label_from_minutes(minutes: int | None, *, secondary: bool = False) -> str:
    if minutes == 300:
        return "5h limit"
    if minutes == 10080:
        return "7d limit"
    if minutes is None or minutes <= 0:
        return "Secondary usage limit" if secondary else "Usage limit"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days}d limit"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours}h limit"
    return f"{minutes}m limit"


def html_escape(value: object) -> str:
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
