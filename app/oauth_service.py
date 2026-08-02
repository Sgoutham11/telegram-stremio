from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings
from .database import Database
from .models import utcnow_text
from .rclone_service import (
    GOOGLE_DRIVE_RECONNECT_MESSAGE,
    RcloneService,
)
from .security import expires_at, random_token, token_hash


class StorageConnectionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GoogleOAuthService:
    AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
    REVOCATION_ENDPOINT = "https://oauth2.googleapis.com/revoke"
    USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
    SCOPES = (
        "openid",
        "email",
        "https://www.googleapis.com/auth/drive.file",
    )

    def __init__(
        self, settings: Settings, database: Database, rclone: RcloneService
    ):
        self.settings = settings
        self.database = database
        self.rclone = rclone
        self._user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def authorization_url(
        self, user_id: int, web_session_hash: str
    ) -> str:
        if not all(
            (
                self.settings.google_client_id,
                self.settings.google_client_secret,
                self.settings.google_redirect_uri,
            )
        ):
            raise RuntimeError("Google OAuth is not configured")
        connections = await self._google_connections(user_id)
        self._require_current_drive_permissions(user_id, connections)
        if len(connections) >= self.settings.multy_rclone_count:
            raise StorageConnectionError(
                "limit",
                "You have reached the configured Google Drive connection limit.",
            )
        state = random_token()
        await self.database.execute(
            """
            INSERT INTO oauth_states (
                state_hash, web_session_hash, telegram_user_id, provider,
                created_at, expires_at, used
            ) VALUES (?, ?, ?, 'google', ?, ?, 0)
            """,
            (
                token_hash(state),
                web_session_hash,
                user_id,
                utcnow_text(),
                expires_at(self.settings.oauth_state_ttl_seconds),
            ),
        )
        query = urlencode(
            {
                "client_id": self.settings.google_client_id,
                "redirect_uri": self.settings.google_redirect_uri,
                "response_type": "code",
                "scope": " ".join(self.SCOPES),
                "access_type": "offline",
                "include_granted_scopes": "false",
                "prompt": "select_account consent",
                "state": state,
            }
        )
        return f"{self.AUTHORIZATION_ENDPOINT}?{query}"

    async def complete(
        self, code: str, state: str, web_session_hash: str
    ) -> int:
        record = await self.database.claim_oauth_state(
            token_hash(state), web_session_hash
        )
        if not record:
            raise PermissionError("Invalid or expired OAuth state")
        user_id = int(record["telegram_user_id"])
        received = await self._exchange_code(code)
        if "access_token" not in received:
            raise RuntimeError("Google did not return an access token")
        identity = await self._fetch_identity(str(received["access_token"]))
        subject = str(identity.get("sub") or "").strip()
        email = str(identity.get("email") or "").strip().casefold()
        if (
            not subject
            or not email
            or identity.get("email_verified") is not True
        ):
            raise StorageConnectionError(
                "identity",
                "Google did not return a verified account email.",
            )
        async with self._user_locks[user_id]:
            connections = await self._google_connections(user_id)
            self._require_current_drive_permissions(user_id, connections)
            if any(
                row["provider"] == "google"
                and (
                    row["provider_account_id"] == subject
                    or str(row["account_email"] or "").casefold() == email
                )
                for row in connections
            ):
                raise StorageConnectionError(
                    "duplicate",
                    "This Google account is already connected. Choose a different account.",
                )
            if len(connections) >= self.settings.multy_rclone_count:
                raise StorageConnectionError(
                    "limit",
                    "You have reached the configured Google Drive connection limit.",
                )
            remote = await self._next_remote_name(user_id)
            token = {
                "access_token": received["access_token"],
                "token_type": received.get("token_type", "Bearer"),
                "refresh_token": received.get("refresh_token", ""),
                "expiry": (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=int(received.get("expires_in", 3600)))
                ).isoformat(),
            }
            created = False
            try:
                await self.rclone.create_google_drive(
                    user_id,
                    remote,
                    self.settings.google_client_id,
                    self.settings.google_client_secret,
                    token,
                )
                created = True
                if not await self.rclone.verify_connection(user_id, remote):
                    raise RuntimeError(
                        "Google authorization completed, but Drive access "
                        "could not be verified"
                    )
                config = self.settings.user_rclone_config(user_id)
                now = utcnow_text()
                await self.database.execute(
                    """
                    INSERT INTO storage_connections (
                        telegram_user_id, provider, remote_name,
                        provider_account_id, account_email, config_path,
                        connected, created_at, updated_at
                    ) VALUES (?, 'google', ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        user_id,
                        remote,
                        subject,
                        email,
                        str(config),
                        now,
                        now,
                    ),
                )
            except Exception:
                if created:
                    await self.rclone.disconnect_storage(user_id, remote)
                raise
            await self.database.set_user_fields(
                user_id,
                storage_connected=1,
                rclone_config_path=str(self.settings.user_rclone_config(user_id)),
                selected_remote=remote,
            )
        return user_id

    async def _exchange_code(self, code: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self.settings.google_client_id,
                    "client_secret": self.settings.google_client_secret,
                    "redirect_uri": self.settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
            return dict(response.json())

    async def _fetch_identity(self, access_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                self.USERINFO_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            return dict(response.json())

    async def _next_remote_name(self, user_id: int) -> str:
        existing = {name.casefold() for name in await self.rclone.list_remotes(user_id)}
        base = self.settings.default_rclone_remote
        if base.casefold() not in existing:
            return base
        for index in range(2, self.settings.multy_rclone_count + 2):
            candidate = f"{base}_{index:02d}"
            if candidate.casefold() not in existing:
                return candidate
        raise StorageConnectionError(
            "limit", "No storage remote slot is available."
        )

    async def _google_connections(self, user_id: int) -> list[dict[str, Any]]:
        connections = [
            row
            for row in await self.database.storage_connections(user_id)
            if row["provider"] == "google"
        ]
        changed = False
        for connection in connections:
            if connection["provider_account_id"] and connection["account_email"]:
                continue
            identity = await self.rclone.google_drive_identity(
                user_id, str(connection["remote_name"])
            )
            if not identity:
                continue
            account_id, email = identity
            await self.database.execute(
                """
                UPDATE storage_connections
                SET provider_account_id=?, account_email=?, updated_at=?
                WHERE id=?
                """,
                (account_id, email, utcnow_text(), connection["id"]),
            )
            changed = True
        if changed:
            connections = [
                row
                for row in await self.database.storage_connections(user_id)
                if row["provider"] == "google"
            ]
        return connections

    def _require_current_drive_permissions(
        self, user_id: int, connections: list[dict[str, Any]]
    ) -> None:
        if any(
            not self.rclone.uses_required_google_drive_scope(
                user_id, str(connection["remote_name"])
            )
            for connection in connections
        ):
            raise StorageConnectionError(
                "permissions", GOOGLE_DRIVE_RECONNECT_MESSAGE
            )
