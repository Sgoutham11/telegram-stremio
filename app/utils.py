from __future__ import annotations

import mimetypes
import re
from datetime import datetime
from pathlib import Path, PurePath

INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, fallback: str = "file", max_bytes: int = 240) -> str:
    name = PurePath(name or "").name
    name = INVALID.sub("_", name).strip(" .")
    if name in {"", ".", ".."}:
        name = fallback
    stem, suffix = Path(name).stem, Path(name).suffix[:20]
    while len((stem + suffix).encode("utf-8")) > max_bytes and stem:
        stem = stem[:-1]
    return (stem or fallback) + suffix


def default_root_directory(
    first_name: str | None, last_name: str | None, user_id: int
) -> str:
    """Build the default cloud root from the Telegram first and last names."""
    combined = f"{first_name or ''}{last_name or ''}".upper()
    sanitized = re.sub(r"[^A-Z0-9_-]+", "_", combined).strip("._-")
    sanitized = sanitized[:100].strip("._-")
    return sanitized or f"USER_{int(user_id)}"


def validate_root_directory(value: str) -> str:
    value = value.strip().upper()
    if (
        value in {"", ".", ".."}
        or not re.fullmatch(r"[A-Z0-9 _-]{1,100}", value)
    ):
        raise ValueError("invalid root directory")
    return value


def fallback_filename(message_id: int, media_type: str, timestamp: datetime, mime_type: str | None = None) -> str:
    extension = mimetypes.guess_extension(mime_type or "") or ""
    return sanitize_filename(f"{message_id}_{media_type}_{timestamp:%Y%m%d_%H%M%S}{extension}")


def format_bytes(value: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {sec}s" if hours else f"{minutes}m {sec}s"
