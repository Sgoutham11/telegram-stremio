from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.oauth_service import GoogleOAuthService, StorageConnectionError
from conftest import telegram_user


class FakeRclone:
    def __init__(self):
        self.remotes: dict[int, list[str]] = {}
        self.identities: dict[tuple[int, str], tuple[str, str]] = {}
        self.created: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, str]] = []

    async def list_remotes(self, user_id):
        return list(self.remotes.get(user_id, []))

    async def create_google_drive(
        self, user_id, remote, _client_id, _client_secret, _token
    ):
        self.remotes.setdefault(user_id, []).append(remote)
        self.created.append((user_id, remote))
        return remote

    async def verify_connection(self, user_id, remote):
        return remote in self.remotes.get(user_id, [])

    async def google_drive_identity(self, user_id, remote):
        return self.identities.get((user_id, remote))

    async def disconnect_storage(self, user_id, remote):
        self.remotes.setdefault(user_id, []).remove(remote)
        self.deleted.append((user_id, remote))


async def oauth_state(service, user_id=10, web_digest="browser-session"):
    url = await service.authorization_url(user_id, web_digest)
    return url, parse_qs(urlsplit(url).query)["state"][0]


async def test_two_distinct_google_accounts_create_two_selectable_remotes(
    settings, database
):
    settings.multy_rclone_count = 2
    await database.upsert_user(telegram_user(10), 10)
    rclone = FakeRclone()
    service = GoogleOAuthService(settings, database, rclone)

    async def exchange(_code):
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }

    identities = [
        {"sub": "google-account-1", "email": "FIRST@GMAIL.COM", "email_verified": True},
        {"sub": "google-account-2", "email": "second@gmail.com", "email_verified": True},
    ]

    async def identity(_access_token):
        return identities.pop(0)

    service._exchange_code = exchange
    service._fetch_identity = identity

    first_url, first_state = await oauth_state(service)
    first_query = parse_qs(urlsplit(first_url).query)
    assert "openid" in first_query["scope"][0].split()
    assert "email" in first_query["scope"][0].split()
    assert first_query["prompt"] == ["select_account consent"]
    assert await service.complete("code-1", first_state, "browser-session") == 10

    _, second_state = await oauth_state(service)
    assert await service.complete("code-2", second_state, "browser-session") == 10

    rows = await database.storage_connections(10)
    assert [(row["remote_name"], row["account_email"]) for row in rows] == [
        ("gdrive", "first@gmail.com"),
        ("gdrive_02", "second@gmail.com"),
    ]
    assert rclone.created == [(10, "gdrive"), (10, "gdrive_02")]
    assert (await database.get_user(10))["selected_remote"] == "gdrive_02"

    with pytest.raises(StorageConnectionError) as raised:
        await service.authorization_url(10, "browser-session")
    assert raised.value.code == "limit"


async def test_same_google_account_cannot_be_connected_twice(settings, database):
    settings.multy_rclone_count = 2
    await database.upsert_user(telegram_user(10), 10)
    rclone = FakeRclone()
    service = GoogleOAuthService(settings, database, rclone)

    async def exchange(_code):
        return {"access_token": "token", "refresh_token": "refresh"}

    async def same_identity(_access_token):
        return {
            "sub": "same-google-account",
            "email": "same@gmail.com",
            "email_verified": True,
        }

    service._exchange_code = exchange
    service._fetch_identity = same_identity

    _, first_state = await oauth_state(service)
    await service.complete("first", first_state, "browser-session")
    _, duplicate_state = await oauth_state(service)
    with pytest.raises(StorageConnectionError) as raised:
        await service.complete("duplicate", duplicate_state, "browser-session")

    assert raised.value.code == "duplicate"
    assert rclone.created == [(10, "gdrive")]
    assert len(await database.storage_connections(10)) == 1


async def test_unverified_google_email_is_rejected_before_rclone_creation(
    settings, database
):
    await database.upsert_user(telegram_user(10), 10)
    rclone = FakeRclone()
    service = GoogleOAuthService(settings, database, rclone)

    async def exchange(_code):
        return {"access_token": "token"}

    async def identity(_access_token):
        return {
            "sub": "account",
            "email": "unverified@gmail.com",
            "email_verified": False,
        }

    service._exchange_code = exchange
    service._fetch_identity = identity
    _, state = await oauth_state(service)

    with pytest.raises(StorageConnectionError) as raised:
        await service.complete("code", state, "browser-session")
    assert raised.value.code == "identity"
    assert rclone.created == []


async def test_legacy_connection_identity_is_recovered_without_reconnecting(
    settings, database
):
    settings.multy_rclone_count = 2
    await database.upsert_user(telegram_user(10), 10)
    await database.execute(
        """
        INSERT INTO storage_connections (
            telegram_user_id, provider, remote_name, config_path,
            connected, created_at, updated_at
        ) VALUES (10, 'google', 'gdrive', '/legacy/rclone.conf', 1, ?, ?)
        """,
        ("2026-07-30T00:00:00Z", "2026-07-30T00:00:00Z"),
    )
    rclone = FakeRclone()
    rclone.remotes[10] = ["gdrive"]
    rclone.identities[(10, "gdrive")] = (
        "legacy-google-account",
        "legacy@gmail.com",
    )
    service = GoogleOAuthService(settings, database, rclone)

    url = await service.authorization_url(10, "browser-session")

    assert url.startswith(service.AUTHORIZATION_ENDPOINT)
    connection = (await database.storage_connections(10))[0]
    assert connection["provider_account_id"] == "legacy-google-account"
    assert connection["account_email"] == "legacy@gmail.com"
