from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import TelegramLoginStatus
from app.telegram_onboarding import PendingLogin, TelegramOnboardingManager


class RecordingDatabase:
    def __init__(self):
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql, values=()):
        self.calls.append((sql, tuple(values)))
        return 1


class RotatingQrLogin:
    def __init__(self):
        self.url = "tg://login?token=old"
        self.wait_calls = 0
        self.recreate_calls = 0
        self.recreated = asyncio.Event()
        self.hold = asyncio.Event()

    async def wait(self):
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise asyncio.TimeoutError
        await self.hold.wait()

    async def recreate(self):
        self.recreate_calls += 1
        self.url = "tg://login?token=new"
        self.recreated.set()


async def test_expired_telegram_token_rotates_inside_overall_login_window(
    settings, tmp_path: Path
):
    database = RecordingDatabase()
    manager = TelegramOnboardingManager(
        settings,
        database,
        SimpleNamespace(),
    )
    pending = PendingLogin(
        connection_id="connection-id",
        user_id=10,
        session_path=tmp_path / "telegram.session",
        client=SimpleNamespace(),
        expires_at=(
            datetime.now(timezone.utc) + timedelta(seconds=60)
        ).isoformat(),
        status=TelegramLoginStatus.WAITING_FOR_SCAN,
        qr_image=manager._qr_data_uri("tg://login?token=old"),
    )
    old_image = pending.qr_image
    qr_login = RotatingQrLogin()

    task = asyncio.create_task(manager._wait_for_qr(pending, qr_login))
    try:
        await asyncio.wait_for(qr_login.recreated.wait(), timeout=1)
        assert qr_login.recreate_calls == 1
        assert qr_login.wait_calls >= 2
        assert pending.status == TelegramLoginStatus.WAITING_FOR_SCAN
        assert pending.qr_image != old_image
        assert "refreshed automatically" in (pending.message or "")
        assert database.calls
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
