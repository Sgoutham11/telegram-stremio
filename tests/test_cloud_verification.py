from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

from app.oauth_service import GoogleOAuthService
from app.rclone_service import RcloneService
from app.security import RateLimiter, expires_at, token_hash
from app.web import create_web_app
from conftest import telegram_user


async def test_rclone_connection_requires_live_remote_access(settings):
    service = RcloneService(settings)
    config = settings.user_rclone_config(10)
    config.parent.mkdir(parents=True)
    config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
    calls = []

    async def run(*args):
        calls.append(args)
        if args[1] == "listremotes":
            return 0, "gdrive:\n", ""
        return 0, "", ""

    service._run = run
    assert await service.verify_connection(10, "gdrive")
    assert calls[1][0:3] == ("rclone", "lsd", "gdrive:")
    assert str(config) in calls[1]


async def test_rclone_connection_rejects_authentication_failure(settings):
    service = RcloneService(settings)
    config = settings.user_rclone_config(10)
    config.parent.mkdir(parents=True)
    config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")

    async def run(*args):
        if args[1] == "listremotes":
            return 0, "gdrive:\n", ""
        return 1, "", "invalid_grant"

    service._run = run
    assert not await service.verify_connection(10, "gdrive")


async def test_rclone_connection_timeout_is_not_connected(settings):
    service = RcloneService(settings)
    config = settings.user_rclone_config(10)
    config.parent.mkdir(parents=True)
    config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")

    async def run(*_args):
        await asyncio.sleep(0.05)
        return 0, "gdrive:\n", ""

    service._run = run
    assert not await service.verify_connection(10, "gdrive", timeout_seconds=0.01)


async def test_starting_oauth_does_not_mark_storage_connected(settings, database):
    await database.upsert_user(telegram_user(10), 10)
    service = GoogleOAuthService(settings, database, SimpleNamespace())

    url = await service.authorization_url(10, "browser-session-hash")

    assert "accounts.google.com" in url
    assert not (await database.get_user(10))["storage_connected"]


async def test_web_status_uses_live_storage_verification(settings, database):
    await database.upsert_user(telegram_user(10), 10)
    await database.set_user_fields(
        10,
        telegram_connected=1,
        storage_connected=1,
        selected_remote="gdrive",
    )
    raw_cookie = "browser-session"
    await database.execute(
        """
        INSERT INTO web_sessions
        (token_hash, telegram_user_id, created_at, expires_at, revoked)
        VALUES (?, ?, ?, ?, 0)
        """,
        (
            token_hash(raw_cookie),
            10,
            datetime.now(timezone.utc).isoformat(),
            expires_at(60),
        ),
    )

    class Storage:
        async def verify_connection(self, *_args):
            return False

    app = create_web_app(
        settings,
        database,
        SimpleNamespace(),
        SimpleNamespace(),
        Storage(),
        SimpleNamespace(),
        RateLimiter(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://example.test"
    ) as client:
        response = await client.get(
            "/api/status", cookies={"uploader_session": raw_cookie}
        )

    assert response.status_code == 200
    assert response.json()["storage"]["connected"] is False
    assert response.json()["active"] is False
