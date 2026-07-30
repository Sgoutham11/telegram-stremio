from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from .config import Settings
from .database import Database
from .models import utcnow_text
from .rclone_service import RcloneService
from .security import expires_at, random_token, token_hash


class GoogleOAuthService:
    AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
    REVOCATION_ENDPOINT = "https://oauth2.googleapis.com/revoke"
    SCOPE = "https://www.googleapis.com/auth/drive"

    def __init__(
        self, settings: Settings, database: Database, rclone: RcloneService
    ):
        self.settings = settings
        self.database = database
        self.rclone = rclone

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
                "scope": self.SCOPE,
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
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
            received = response.json()
        if "access_token" not in received:
            raise RuntimeError("Google did not return an access token")
        token = {
            "access_token": received["access_token"],
            "token_type": received.get("token_type", "Bearer"),
            "refresh_token": received.get("refresh_token", ""),
            "expiry": (
                datetime.now(timezone.utc)
                + timedelta(seconds=int(received.get("expires_in", 3600)))
            ).isoformat(),
        }
        remote = await self.rclone.create_google_drive(
            user_id,
            self.settings.google_client_id,
            self.settings.google_client_secret,
            token,
        )
        if not await self.rclone.verify_connection(user_id, remote):
            raise RuntimeError(
                "Google authorization completed, but Drive access could not be verified"
            )
        config = self.settings.user_rclone_config(user_id)
        now = utcnow_text()
        await self.database.execute(
            """
            INSERT INTO storage_connections (
                telegram_user_id, provider, remote_name, config_path,
                connected, created_at, updated_at
            ) VALUES (?, 'google', ?, ?, 1, ?, ?)
            ON CONFLICT(telegram_user_id, remote_name) DO UPDATE SET
                provider='google',
                config_path=excluded.config_path,
                connected=1,
                updated_at=excluded.updated_at
            """,
            (user_id, remote, str(config), now, now),
        )
        await self.database.set_user_fields(
            user_id,
            storage_connected=1,
            rclone_config_path=str(config),
            selected_remote=remote,
        )
        return user_id
