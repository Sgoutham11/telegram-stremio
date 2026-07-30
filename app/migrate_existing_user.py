from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

from telethon import TelegramClient

from .config import Settings
from .database import Database
from .rclone_service import RcloneService


async def migrate(user_id: int) -> None:
    settings = Settings()
    settings.prepare_directories()
    source_session = Path("/data/session/telegram.session")
    source_rclone = Path("/config/rclone/rclone.conf")
    if not source_session.is_file():
        raise FileNotFoundError(f"Existing session not found: {source_session}")
    if not source_rclone.is_file():
        raise FileNotFoundError(f"Existing rclone config not found: {source_rclone}")

    destination_session = settings.user_session_path(user_id)
    destination_rclone = settings.user_rclone_config(user_id)
    destination_session.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination_rclone.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination_session.exists() or destination_rclone.exists():
        raise FileExistsError("A per-user session or rclone config already exists")

    for suffix in ("", "-wal", "-shm"):
        source = Path(str(source_session) + suffix)
        if source.exists():
            shutil.copy2(source, Path(str(destination_session) + suffix))
    shutil.copy2(source_rclone, destination_rclone)
    os.chmod(destination_session, 0o600)
    os.chmod(destination_rclone, 0o600)

    client = TelegramClient(
        str(destination_session),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    database = Database(settings.database_path)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise PermissionError("Copied Telegram session is not authorized")
        me = await client.get_me()
        if not me or int(me.id) != user_id:
            raise PermissionError("Copied Telegram session belongs to another user")

        await database.connect()
        rclone = RcloneService(settings)
        remotes = await rclone.list_remotes(user_id)
        if not remotes:
            raise RuntimeError("Copied rclone configuration has no remotes")
        await database.upsert_user(
            me, user_id, settings.default_upload_directory
        )
        selected = next(
            (
                remote
                for remote in remotes
                if remote.casefold() == settings.default_rclone_remote.casefold()
            ),
            remotes[0],
        )
        await database.set_user_fields(
            user_id,
            telegram_connected=1,
            storage_connected=1,
            session_path=str(destination_session),
            rclone_config_path=str(destination_rclone),
            selected_remote=selected,
            selected_directory=settings.default_upload_directory,
        )
    except Exception:
        for suffix in ("", "-wal", "-shm"):
            Path(str(destination_session) + suffix).unlink(missing_ok=True)
        destination_rclone.unlink(missing_ok=True)
        raise
    finally:
        await client.disconnect()
        await database.close()

    print(
        f"Migrated user {user_id}. The original session and rclone config were preserved."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy the legacy single-user session and rclone config"
    )
    parser.add_argument("--telegram-user-id", type=int, required=True)
    arguments = parser.parse_args()
    asyncio.run(migrate(arguments.telegram_user_id))


if __name__ == "__main__":
    main()
