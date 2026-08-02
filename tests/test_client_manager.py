from __future__ import annotations

from pathlib import Path

import pytest

from app.client_manager import TelegramClientManager
from conftest import telegram_user


class FakeClient:
    def __init__(self, path: Path, *_args, identity: int | None = None, corrupt=False):
        self.path = path
        self.identity = identity or int(path.parent.name)
        self.corrupt = corrupt
        self.connected = False
        self.disconnect_count = 0

    async def connect(self):
        if self.corrupt:
            raise OSError("corrupt")
        self.connected = True

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return not self.corrupt

    async def get_me(self):
        return telegram_user(self.identity)

    async def disconnect(self):
        self.connected = False
        self.disconnect_count += 1


async def connected_user(database, settings, user_id):
    await database.upsert_user(telegram_user(user_id), user_id)
    path = settings.user_session_path(user_id)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"session")
    await database.set_user_fields(
        user_id, telegram_connected=1, session_path=str(path)
    )


async def test_start_get_stop_user(settings, database):
    await connected_user(database, settings, 10)
    manager = TelegramClientManager(settings, database, FakeClient)
    client = await manager.start_user(10)
    assert manager.get_client(10) is client
    await manager.stop_user(10)
    assert manager.get_client(10) is None


async def test_duplicate_start_reuses_client(settings, database):
    await connected_user(database, settings, 10)
    created = []

    def factory(*args):
        created.append(FakeClient(*args))
        return created[-1]

    manager = TelegramClientManager(settings, database, factory)
    assert await manager.start_user(10) is await manager.start_user(10)
    assert len(created) == 1


async def test_identity_mismatch_is_rejected(settings, database):
    await connected_user(database, settings, 10)
    manager = TelegramClientManager(
        settings, database, lambda *args: FakeClient(*args, identity=999)
    )
    with pytest.raises(PermissionError):
        await manager.start_user(10)
    assert manager.get_client(10) is None


async def test_corrupt_user_does_not_block_other_users(settings, database):
    await connected_user(database, settings, 10)
    await connected_user(database, settings, 20)

    def factory(path, *args):
        return FakeClient(path, *args, corrupt=int(path.parent.name) == 10)

    manager = TelegramClientManager(settings, database, factory)
    await manager.start_all_users()
    assert manager.get_client(10) is None
    assert manager.get_client(20) is not None


async def test_unauthorized_session_is_marked_for_reconnection(settings, database):
    await connected_user(database, settings, 10)

    class UnauthorizedClient(FakeClient):
        async def is_user_authorized(self):
            return False

    manager = TelegramClientManager(settings, database, UnauthorizedClient)
    await manager.start_all_users()

    user = await database.get_user(10)
    assert user["telegram_connected"] == 0
    assert user["session_path"] is None
    assert settings.user_session_path(10).exists()


async def test_shutdown_disconnects_all(settings, database):
    for user_id in (10, 20):
        await connected_user(database, settings, user_id)
    manager = TelegramClientManager(settings, database, FakeClient)
    clients = [await manager.start_user(user_id) for user_id in (10, 20)]
    await manager.shutdown()
    assert manager.list_active_clients() == []
    assert all(client.disconnect_count == 1 for client in clients)
