from __future__ import annotations

import asyncio
import logging

from .client_manager import TelegramClientManager
from .database import Database
from .telegram_onboarding import TelegramOnboardingManager

LOG = logging.getLogger(__name__)


class MaintenanceService:
    """Expires setup state and retries transiently unavailable user sessions."""

    def __init__(
        self,
        database: Database,
        clients: TelegramClientManager,
        onboarding: TelegramOnboardingManager,
        interval_seconds: float = 60,
    ):
        self.database = database
        self.clients = clients
        self.onboarding = onboarding
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self.database.cleanup_expired()
                await self.onboarding.expire_pending()
                for user in await self.database.connected_users():
                    user_id = int(user["telegram_user_id"])
                    if self.clients.get_client(user_id) is None:
                        try:
                            await self.clients.start_user(user_id)
                        except Exception:
                            LOG.warning(
                                "User session %s is still unavailable", user_id
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Periodic maintenance failed")
            await asyncio.sleep(self.interval_seconds)
