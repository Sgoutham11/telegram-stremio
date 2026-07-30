from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import qrcode
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyError,
    FloodWaitError,
    PasswordHashInvalidError,
    RPCError,
    SessionPasswordNeededError,
)

from .client_manager import ClientFactory, TelegramClientManager, default_client_factory
from .config import Settings
from .database import Database
from .models import TelegramLoginStatus, utcnow_text
from .security import expires_at, random_token

LOG = logging.getLogger(__name__)


@dataclass
class PendingLogin:
    connection_id: str
    user_id: int
    session_path: Path
    client: Any
    expires_at: str
    status: TelegramLoginStatus = TelegramLoginStatus.QR_LOADING
    qr_image: str | None = None
    message: str | None = None
    attempts: int = 0
    task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TelegramOnboardingManager:
    """Maintains temporary QR clients and atomically promotes valid sessions."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        clients: TelegramClientManager,
        client_factory: ClientFactory = default_client_factory,
    ):
        self.settings = settings
        self.database = database
        self.clients = clients
        self.client_factory = client_factory
        self._pending: dict[str, PendingLogin] = {}
        self._lock = asyncio.Lock()
        self.accepting = True

    async def start(self, user_id: int) -> dict[str, str]:
        if not self.accepting:
            raise RuntimeError("Application is shutting down")
        async with self._lock:
            active = next(
                (
                    login
                    for login in self._pending.values()
                    if login.user_id == user_id
                    and login.status
                    not in {
                        TelegramLoginStatus.CONNECTED,
                        TelegramLoginStatus.EXPIRED,
                        TelegramLoginStatus.FAILED,
                    }
                ),
                None,
            )
            if active:
                return self._response(active)
            active_count = sum(
                login.status
                not in {
                    TelegramLoginStatus.CONNECTED,
                    TelegramLoginStatus.EXPIRED,
                    TelegramLoginStatus.FAILED,
                }
                for login in self._pending.values()
            )
            if active_count >= self.settings.max_pending_qr_logins:
                raise RuntimeError("Too many pending Telegram connections")

            connection_id = random_token(24)
            directory = self.settings.pending_telegram_root / connection_id
            directory.mkdir(parents=True, mode=0o700)
            session_path = directory / "telegram.session"
            client = self.client_factory(
                session_path,
                self.settings.telegram_api_id,
                self.settings.telegram_api_hash,
            )
            pending = PendingLogin(
                connection_id=connection_id,
                user_id=user_id,
                session_path=session_path,
                client=client,
                expires_at=expires_at(self.settings.qr_login_ttl_seconds),
            )
            self._pending[connection_id] = pending

        await self.database.execute(
            """
            INSERT INTO telegram_login_sessions (
                connection_id, telegram_user_id, temporary_session_path,
                status, attempt_count, created_at, expires_at
            ) VALUES (?, ?, ?, ?, 0, ?, ?)
            """,
            (
                connection_id,
                user_id,
                str(session_path),
                TelegramLoginStatus.QR_LOADING.value,
                utcnow_text(),
                pending.expires_at,
            ),
        )
        try:
            await client.connect()
            qr_login = await client.qr_login()
            pending.qr_image = self._qr_data_uri(qr_login.url)
            pending.status = TelegramLoginStatus.WAITING_FOR_SCAN
            await self._persist(pending)
            pending.task = asyncio.create_task(self._wait_for_qr(pending, qr_login))
            return self._response(pending)
        except Exception:
            await self._fail(pending, "QR_START_FAILED")
            raise RuntimeError("Unable to start Telegram QR login")

    async def status(self, connection_id: str, user_id: int) -> dict[str, str]:
        pending = self._pending.get(connection_id)
        if not pending or pending.user_id != user_id:
            raise KeyError("Telegram connection not found")
        if self._expired(pending):
            await self._expire(pending)
        return self._response(pending)

    async def submit_2fa(
        self, connection_id: str, user_id: int, password: str
    ) -> dict[str, str]:
        pending = self._pending.get(connection_id)
        if not pending or pending.user_id != user_id:
            raise KeyError("Telegram connection not found")
        if self._expired(pending):
            await self._expire(pending)
            return self._response(pending)
        async with pending.lock:
            if pending.status != TelegramLoginStatus.TWO_FACTOR_REQUIRED:
                raise ValueError("This connection is not waiting for a password")
            pending.attempts += 1
            try:
                await pending.client.sign_in(password=password)
                pending.status = TelegramLoginStatus.CONNECTING
                await self._persist(pending)
                await self._finalize(pending)
            except PasswordHashInvalidError:
                if pending.attempts >= self.settings.max_telegram_2fa_attempts:
                    await self._fail(pending, "MAX_2FA_ATTEMPTS")
                else:
                    pending.message = "Incorrect password. Please try again."
                    await self._persist(pending)
            except FloodWaitError as exc:
                pending.message = (
                    f"Telegram rate limited this login. Try again in {exc.seconds} seconds."
                )
                await self._persist(pending)
            except (AuthKeyError, RPCError, TimeoutError):
                await self._fail(pending, "TELEGRAM_AUTH_FAILED")
        return self._response(pending)

    async def _wait_for_qr(self, pending: PendingLogin, qr_login: Any) -> None:
        try:
            await asyncio.wait_for(
                qr_login.wait(), timeout=self.settings.qr_login_ttl_seconds
            )
            pending.status = TelegramLoginStatus.CONNECTING
            await self._persist(pending)
            await self._finalize(pending)
        except SessionPasswordNeededError:
            pending.status = TelegramLoginStatus.TWO_FACTOR_REQUIRED
            pending.expires_at = expires_at(self.settings.telegram_2fa_ttl_seconds)
            pending.message = "Enter your Telegram two-step verification password."
            await self._persist(pending)
        except (asyncio.TimeoutError, TimeoutError):
            await self._expire(pending)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Telegram QR login failed for connection %s", pending.connection_id)
            await self._fail(pending, "TELEGRAM_AUTH_FAILED")

    async def _finalize(self, pending: PendingLogin) -> None:
        me = await pending.client.get_me()
        if not me or int(me.id) != pending.user_id:
            pending.message = (
                "Please connect the same Telegram account that opened this "
                "onboarding link."
            )
            await self._fail(pending, "TELEGRAM_ID_MISMATCH", keep_message=True)
            return

        await pending.client.disconnect()
        destination_dir = self.settings.user_data_dir(pending.user_id)
        destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.settings.user_session_path(pending.user_id)
        await self.clients.stop_user(pending.user_id)
        self._move_session_family(pending.session_path, destination)
        os.chmod(destination_dir, 0o700)
        os.chmod(destination, 0o600)

        display = " ".join(
            part
            for part in (getattr(me, "first_name", None), getattr(me, "last_name", None))
            if part
        ).strip()
        await self.database.set_user_fields(
            pending.user_id,
            telegram_connected=1,
            session_path=str(destination),
            display_name=display or getattr(me, "username", None) or str(me.id),
            username=getattr(me, "username", None),
            last_connected_at=utcnow_text(),
        )
        await self.clients.start_user(pending.user_id)
        pending.status = TelegramLoginStatus.CONNECTED
        pending.qr_image = None
        pending.message = (
            "Telegram connected. Files sent privately to the bot will be "
            "resolved through this account."
        )
        await self._persist(pending)
        shutil.rmtree(pending.session_path.parent, ignore_errors=True)

    async def _persist(self, pending: PendingLogin) -> None:
        await self.database.execute(
            """
            UPDATE telegram_login_sessions
            SET status=?, attempt_count=?, expires_at=?, error_code=?
            WHERE connection_id=?
            """,
            (
                pending.status.value,
                pending.attempts,
                pending.expires_at,
                None,
                pending.connection_id,
            ),
        )

    async def _fail(
        self, pending: PendingLogin, error_code: str, keep_message: bool = False
    ) -> None:
        pending.status = TelegramLoginStatus.FAILED
        pending.qr_image = None
        if not keep_message:
            pending.message = "Telegram connection failed. Start a new connection."
        if pending.task and pending.task is not asyncio.current_task():
            pending.task.cancel()
        try:
            await pending.client.disconnect()
        except Exception:
            LOG.debug("Unable to disconnect pending Telegram client", exc_info=True)
        await self.database.execute(
            """
            UPDATE telegram_login_sessions
            SET status=?, attempt_count=?, error_code=?
            WHERE connection_id=?
            """,
            (
                pending.status.value,
                pending.attempts,
                error_code,
                pending.connection_id,
            ),
        )
        shutil.rmtree(pending.session_path.parent, ignore_errors=True)

    async def _expire(self, pending: PendingLogin) -> None:
        pending.status = TelegramLoginStatus.EXPIRED
        pending.message = "This QR login expired. Start a new connection."
        pending.qr_image = None
        if pending.task and pending.task is not asyncio.current_task():
            pending.task.cancel()
        try:
            await pending.client.disconnect()
        except Exception:
            LOG.debug("Unable to disconnect expired Telegram client", exc_info=True)
        await self._persist(pending)
        shutil.rmtree(pending.session_path.parent, ignore_errors=True)

    async def cleanup_orphans(self) -> None:
        known = {
            Path(row["temporary_session_path"]).parent
            for row in await self.database.fetchall(
                """
                SELECT temporary_session_path FROM telegram_login_sessions
                WHERE status NOT IN ('CONNECTED', 'FAILED', 'EXPIRED')
                AND expires_at>?
                """,
                (utcnow_text(),),
            )
        }
        self.settings.pending_telegram_root.mkdir(parents=True, exist_ok=True)
        for path in self.settings.pending_telegram_root.iterdir():
            if path.is_dir() and path not in known:
                shutil.rmtree(path, ignore_errors=True)

    async def expire_pending(self) -> None:
        for pending in list(self._pending.values()):
            if (
                pending.status
                not in {
                    TelegramLoginStatus.CONNECTED,
                    TelegramLoginStatus.EXPIRED,
                    TelegramLoginStatus.FAILED,
                }
                and self._expired(pending)
            ):
                await self._expire(pending)

    async def shutdown(self) -> None:
        self.accepting = False
        pending = list(self._pending.values())
        for item in pending:
            if item.task:
                item.task.cancel()
        await asyncio.gather(
            *(item.client.disconnect() for item in pending), return_exceptions=True
        )

    @staticmethod
    def _qr_data_uri(url: str) -> str:
        image = qrcode.make(url)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()

    @staticmethod
    def _move_session_family(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            Path(str(destination) + suffix).unlink(missing_ok=True)
        for suffix in ("", "-wal", "-shm"):
            source_file = Path(str(source) + suffix)
            destination_file = Path(str(destination) + suffix)
            if source_file.exists():
                os.replace(source_file, destination_file)
                os.chmod(destination_file, 0o600)
        if not destination.exists():
            raise FileNotFoundError("Telegram did not create a session file")

    @staticmethod
    def _expired(pending: PendingLogin) -> bool:
        return datetime.fromisoformat(pending.expires_at) <= datetime.now(timezone.utc)

    @staticmethod
    def _response(pending: PendingLogin) -> dict[str, str]:
        response = {
            "connectionId": pending.connection_id,
            "status": pending.status.value,
            "expiresAt": pending.expires_at,
        }
        if pending.qr_image and pending.status == TelegramLoginStatus.WAITING_FOR_SCAN:
            response["qrImage"] = pending.qr_image
        if pending.message:
            response["message"] = pending.message
        return response
