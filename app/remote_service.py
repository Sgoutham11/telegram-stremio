from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from .config import Settings
from .models import utcnow

LOG = logging.getLogger(__name__)


class RemoteService:
    """Persist one allowed rclone remote selection per Telegram user."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.state_path = settings.state_dir / "user_remotes.json"
        self._selected: dict[int, str] = {}
        self._updated_at: dict[int, str] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        async with self._lock:
            try:
                payload = await asyncio.to_thread(self._read_sync)
                self._load_payload(payload)
            except FileNotFoundError:
                self._selected = {}
                self._updated_at = {}
            except Exception:
                self._selected = {}
                self._updated_at = {}
                LOG.exception("Invalid per-user remote state %s; using defaults", self.state_path)

    def _read_sync(self) -> dict[str, object]:
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("per-user remote state must be an object")
        return value

    def _load_payload(self, payload: dict[str, object]) -> None:
        selected: dict[int, str] = {}
        updated: dict[int, str] = {}
        for user_id in self.settings.allowed_user_ids:
            record = payload.get(str(user_id))
            if not isinstance(record, dict):
                continue
            stored = record.get("selected_remote")
            canonical = self.settings.resolve_rclone_remote(stored) if isinstance(stored, str) else None
            if canonical:
                selected[user_id] = canonical
            elif stored:
                LOG.warning(
                    "Stored rclone remote %r for Telegram user %s is no longer allowed; using %s",
                    stored,
                    user_id,
                    self.settings.default_rclone_remote,
                )
            timestamp = record.get("updated_at")
            if isinstance(timestamp, str):
                updated[user_id] = timestamp
        self._selected = selected
        self._updated_at = updated

    def _require_allowed_user(self, user_id: int) -> None:
        if user_id not in self.settings.allowed_users:
            raise PermissionError(f"Telegram user {user_id} is not allowed")

    def list_allowed_remotes(self) -> list[str]:
        return list(self.settings.allowed_rclone_remotes)

    async def get_selected_remote(self, user_id: int) -> str:
        self._require_allowed_user(user_id)
        async with self._lock:
            selected = self._selected.get(user_id)
            canonical = self.settings.resolve_rclone_remote(selected) if selected else None
            return canonical or self.settings.default_rclone_remote or self.settings.rclone_remote

    async def set_selected_remote(self, user_id: int, remote_name: str) -> str:
        self._require_allowed_user(user_id)
        canonical = self.settings.resolve_rclone_remote(remote_name)
        if canonical is None:
            raise ValueError("unknown rclone remote")
        async with self._lock:
            self._selected[user_id] = canonical
            self._updated_at[user_id] = utcnow().isoformat().replace("+00:00", "Z")
            await asyncio.to_thread(self._write_sync)
        return canonical

    def _write_sync(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        payload = {
            str(user_id): {
                "selected_remote": self._selected.get(user_id, self.settings.default_rclone_remote),
                "updated_at": self._updated_at.get(user_id),
            }
            for user_id in self.settings.allowed_user_ids
        }
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)
