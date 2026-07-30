from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

from app.oauth_service import GoogleOAuthService
from app.rclone_service import RcloneService
from app.security import RateLimiter, expires_at, random_token, token_hash
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


async def test_rclone_recovers_google_identity_from_existing_drive(
    settings, monkeypatch
):
    service = RcloneService(settings)
    config = settings.user_rclone_config(10)
    config.parent.mkdir(parents=True)
    config.write_text(
        '[gdrive]\ntype = drive\ntoken = {"access_token":"existing-token"}\n',
        encoding="utf-8",
    )

    async def run(*args):
        if args[1] == "listremotes":
            return 0, "gdrive:\n", ""
        return 0, "", ""

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "user": {
                    "permissionId": "drive-account-id",
                    "emailAddress": "Existing@Gmail.com",
                }
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    service._run = run
    monkeypatch.setattr(
        "app.rclone_service.httpx.AsyncClient", lambda **_kwargs: Client()
    )

    assert await service.google_drive_identity(10, "gdrive") == (
        "drive-account-id",
        "existing@gmail.com",
    )


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


async def test_disconnect_accepts_previous_no_body_browser_request(
    settings, database
):
    await database.upsert_user(telegram_user(10), 10)
    await database.set_user_fields(
        10,
        storage_connected=1,
        selected_remote="gdrive",
        rclone_config_path=str(settings.user_rclone_config(10)),
    )
    now = datetime.now(timezone.utc).isoformat()
    await database.execute(
        """
        INSERT INTO storage_connections (
            telegram_user_id, provider, remote_name, config_path,
            connected, created_at, updated_at
        ) VALUES (10, 'google', 'gdrive', ?, 1, ?, ?)
        """,
        (str(settings.user_rclone_config(10)), now, now),
    )
    raw_cookie = "browser-session"
    await database.execute(
        """
        INSERT INTO web_sessions
        (token_hash, telegram_user_id, created_at, expires_at, revoked)
        VALUES (?, 10, ?, ?, 0)
        """,
        (token_hash(raw_cookie), now, expires_at(60)),
    )

    class Storage:
        def __init__(self):
            self.deleted = []

        async def disconnect_storage(self, user_id, remote):
            self.deleted.append((user_id, remote))

    storage = Storage()
    app = create_web_app(
        settings,
        database,
        SimpleNamespace(),
        SimpleNamespace(),
        storage,
        SimpleNamespace(),
        RateLimiter(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://example.test"
    ) as client:
        response = await client.post(
            "/api/storage/google/disconnect",
            cookies={"uploader_session": raw_cookie},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "DISCONNECTED", "remote": "gdrive"}
    assert storage.deleted == [(10, "gdrive")]
    user = await database.get_user(10)
    assert user["storage_connected"] == 0
    assert user["selected_remote"] is None


async def test_prefixed_connect_redirect_and_cookie_stay_under_uploader(
    settings, database
):
    settings.public_base_url = "https://playbuddy.zapto.org/uploader"
    await database.upsert_user(telegram_user(10), 10)
    raw_token = random_token()
    await database.execute(
        """
        INSERT INTO onboarding_tokens
        (token_hash, telegram_user_id, created_at, expires_at, used)
        VALUES (?, ?, ?, ?, 0)
        """,
        (
            token_hash(raw_token),
            10,
            datetime.now(timezone.utc).isoformat(),
            expires_at(60),
        ),
    )
    app = create_web_app(
        settings,
        database,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        RateLimiter(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://playbuddy.zapto.org",
        follow_redirects=False,
    ) as client:
        response = await client.get("/connect", params={"token": raw_token})
        index = await client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/uploader/"
    cookie = response.headers["set-cookie"]
    assert "Path=/uploader/" in cookie
    assert "Secure" in cookie
    assert 'href="static/style.css"' in index.text
    assert 'src="static/app.js"' in index.text


def test_public_base_path_supports_root_and_prefixed_deployments(settings):
    assert settings.public_base_path == ""
    settings.public_base_url = "https://playbuddy.zapto.org/uploader"
    assert settings.public_base_path == "/uploader"
