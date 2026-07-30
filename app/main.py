from __future__ import annotations

import asyncio
import logging

import uvicorn
from telethon import TelegramClient

from .bot_service import BotService
from .client_manager import TelegramClientManager
from .config import Settings
from .database import Database
from .dispatcher import JobDispatcher
from .logging_config import configure_logging
from .maintenance import MaintenanceService
from .oauth_service import GoogleOAuthService
from .rclone_service import RcloneService
from .security import RateLimiter
from .telegram_onboarding import TelegramOnboardingManager
from .web import create_web_app

LOG = logging.getLogger(__name__)


async def run() -> None:
    settings = Settings()
    settings.prepare_directories()
    configure_logging(settings)
    database = Database(settings.database_path)
    bot: TelegramClient | None = None
    clients: TelegramClientManager | None = None
    onboarding: TelegramOnboardingManager | None = None
    dispatcher: JobDispatcher | None = None
    rclone: RcloneService | None = None
    bot_service: BotService | None = None
    server: uvicorn.Server | None = None
    maintenance: MaintenanceService | None = None

    try:
        await database.connect()
        await database.cleanup_expired()

        bot = TelegramClient(
            str(settings.bot_session_path),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        await bot.start(bot_token=settings.telegram_bot_token)
        bot_identity = await bot.get_me()

        clients = TelegramClientManager(settings, database)
        await clients.start_all_users()
        rclone = RcloneService(settings)
        for user in await database.connected_users():
            user_id = int(user["telegram_user_id"])
            if user["storage_connected"]:
                try:
                    remotes = await rclone.list_remotes(user_id)
                    if not remotes:
                        raise RuntimeError("No remotes configured")
                except Exception:
                    LOG.exception("Storage validation failed for user %s", user_id)
                    await database.set_user_fields(user_id, storage_connected=0)

        limiter = RateLimiter()
        onboarding = TelegramOnboardingManager(settings, database, clients)
        await onboarding.cleanup_orphans()
        oauth = GoogleOAuthService(settings, database, rclone)
        dispatcher = JobDispatcher(
            settings, database, clients, rclone, bot, int(bot_identity.id)
        )
        bot_service = BotService(
            bot, settings, database, clients, rclone, dispatcher, limiter
        )
        bot_service.register()
        await dispatcher.start()
        maintenance = MaintenanceService(database, clients, onboarding)
        maintenance.start()

        application = create_web_app(
            settings, database, clients, onboarding, rclone, oauth, limiter
        )
        server = uvicorn.Server(
            uvicorn.Config(
                application,
                host=settings.web_host,
                port=settings.web_port,
                log_level=settings.log_level.lower(),
                proxy_headers=True,
                forwarded_allow_ips="127.0.0.1",
            )
        )
        LOG.info(
            "Multi-user uploader started; web=%s:%s active_users=%s",
            settings.web_host,
            settings.web_port,
            len(clients.list_active_clients()),
        )
        await server.serve()
    finally:
        if bot_service:
            bot_service.accepting = False
        if maintenance:
            await maintenance.stop()
        if onboarding:
            await onboarding.shutdown()
        if dispatcher:
            await dispatcher.shutdown()
        if rclone:
            await rclone.shutdown()
        if clients:
            await clients.shutdown()
        if bot:
            await bot.disconnect()
        await database.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.critical("Startup failed", exc_info=True)
        raise


if __name__ == "__main__":
    main()
