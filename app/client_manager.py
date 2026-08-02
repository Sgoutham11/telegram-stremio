from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from telethon import TelegramClient

from .config import Settings
from .database import Database

LOG = logging.getLogger(__name__)
ClientFactory = Callable[[Path, int, str], Any]


def default_client_factory(path: Path, api_id: int, api_hash: str) -> TelegramClient:
    return TelegramClient(str(path), api_id, api_hash)


class TelegramClientManager:
    """Owns exactly one long-running Telethon client per connected user."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        client_factory: ClientFactory = default_client_factory,
    ):
        self.settings = settings
        self.database = database
        self.client_factory = client_factory
        self._clients: dict[int, Any] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._map_lock = asyncio.Lock()

    async def start_user(self, user_id: int) -> Any:
        user_id = int(user_id)
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            existing = self._clients.get(user_id)
            if existing and existing.is_connected():
                return existing
            if existing:
                await existing.disconnect()
                self._clients.pop(user_id, None)
            if len(self._clients) >= self.settings.max_connected_users:
                raise RuntimeError("Maximum connected user limit reached")

            path = self.settings.user_session_path(user_id)
            if not path.is_file():
                raise FileNotFoundError("Telegram session is missing")
            client = self.client_factory(
                path, self.settings.telegram_api_id, self.settings.telegram_api_hash
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise PermissionError("Telegram session is no longer authorized")
                me = await client.get_me()
                if not me or int(me.id) != user_id:
                    raise PermissionError("Telegram session identity does not match its owner")
                self._clients[user_id] = client
                return client
            except Exception:
                try:
                    await client.disconnect()
                except Exception:
                    LOG.debug("Unable to disconnect failed client", exc_info=True)
                raise

    async def stop_user(self, user_id: int) -> None:
        user_id = int(user_id)
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            client = self._clients.pop(user_id, None)
            if client:
                await client.disconnect()

    async def restart_user(self, user_id: int) -> Any:
        await self.stop_user(user_id)
        return await self.start_user(user_id)

    def get_client(self, user_id: int) -> Any | None:
        return self._clients.get(int(user_id))

    def list_active_clients(self) -> list[int]:
        return list(self._clients)

    async def start_all_users(self) -> None:
        users = await self.database.connected_users()
        for user in users:
            user_id = int(user["telegram_user_id"])
            try:
                await self.start_user(user_id)
            except (FileNotFoundError, PermissionError):
                LOG.exception(
                    "Telegram session requires reconnection for user %s", user_id
                )
                await self.database.set_user_fields(
                    user_id, telegram_connected=0, session_path=None
                )
            except Exception:
                LOG.exception("Unable to start Telegram session for user %s", user_id)

    async def shutdown(self) -> None:
        async with self._map_lock:
            clients = list(self._clients.items())
            self._clients.clear()
        results = await asyncio.gather(
            *(client.disconnect() for _, client in clients), return_exceptions=True
        )
        for (user_id, _), result in zip(clients, results, strict=True):
            if isinstance(result, Exception):
                LOG.warning("Unable to disconnect user %s: %s", user_id, result)
