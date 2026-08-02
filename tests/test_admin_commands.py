from __future__ import annotations

from types import SimpleNamespace

from app.bot_service import BotService
from app.models import JobStatus, UploadJob
from app.security import RateLimiter
from conftest import telegram_user


class Event:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.replies: list[str] = []

    async def reply(self, text: str, **_kwargs):
        self.replies.append(text)


class NoopDispatcher:
    async def cancel(self, *_args):
        return False


def service(settings, database) -> BotService:
    return BotService(
        SimpleNamespace(),
        settings,
        database,
        SimpleNamespace(),
        SimpleNamespace(),
        NoopDispatcher(),
        RateLimiter(),
    )


async def test_ls_is_permission_aware(settings, database):
    settings.admin_telegram_user_id = 10
    admin = telegram_user(10, "Admin User")
    regular = telegram_user(20, "Regular User")

    admin_event = Event(10)
    regular_event = Event(20)
    bot = service(settings, database)

    await bot._handle_command(admin_event, admin, "/ls")
    await bot._handle_command(regular_event, regular, "/ls")

    assert "/db users" in admin_event.replies[0]
    assert "/block <user-id>" in admin_event.replies[0]
    assert "Administrator commands" not in regular_event.replies[0]
    assert "/clear -" in regular_event.replies[0]
    assert "/dirroot" in regular_event.replies[0]


async def test_db_commands_are_admin_only(settings, database):
    settings.admin_telegram_user_id = 10
    admin = telegram_user(10, "Admin User")
    regular = telegram_user(20, "Regular User")
    await database.upsert_user(admin, 10)
    await database.upsert_user(regular, 20)

    event = Event(20)
    await service(settings, database)._handle_command(event, regular, "/db users")

    assert event.replies == [
        "This command is available only to the administrator."
    ]
    assert "Admin User" not in event.replies[0]


async def test_admin_user_report_shows_operations_but_not_secret_paths(
    settings, database
):
    settings.admin_telegram_user_id = 10
    admin = telegram_user(10, "Admin User")
    regular = telegram_user(20, "Regular User")
    await database.upsert_user(admin, 10)
    await database.upsert_user(regular, 20)
    await database.set_user_fields(
        20,
        telegram_connected=1,
        storage_connected=1,
        session_path="/private/telegram.session",
        rclone_config_path="/private/rclone.conf",
        selected_remote="gdrive",
    )
    await database.create_job(
        UploadJob(
            owner_user_id=20,
            bot_chat_id=20,
            bot_message_id=100,
            file_name="video.mkv",
            file_size=1024,
            selected_remote="gdrive",
            selected_root_directory="REGULARUSER",
            selected_directory="DOWNLOADS",
            status=JobStatus.QUEUED,
        )
    )

    event = Event(10)
    await service(settings, database)._handle_command(event, admin, "/db users")
    output = "\n".join(event.replies)

    assert "User 20 | Regular User" in output
    assert "Telegram: connected" in output
    assert "Storage: connected" in output
    assert "queued=1" in output
    assert "/private/telegram.session" not in output
    assert "/private/rclone.conf" not in output
    assert "token_hash" not in output


async def test_admin_activeworks_stats_and_failures(settings, database):
    settings.admin_telegram_user_id = 10
    admin = telegram_user(10, "Admin User")
    await database.upsert_user(admin, 10)

    queued = await database.create_job(
        UploadJob(
            owner_user_id=10,
            bot_chat_id=10,
            bot_message_id=101,
            file_name="queued.bin",
            file_size=2048,
            selected_remote="gdrive",
            selected_root_directory="ADMINUSER",
            selected_directory="DOWNLOADS",
            status=JobStatus.QUEUED,
        )
    )
    failed = await database.create_job(
        UploadJob(
            owner_user_id=10,
            bot_chat_id=10,
            bot_message_id=102,
            file_name="failed.bin",
            file_size=4096,
            selected_remote="gdrive",
            selected_root_directory="ADMINUSER",
            selected_directory="DOWNLOADS",
            status=JobStatus.FAILED,
        )
    )
    assert queued.id is not None
    assert failed.id is not None
    await database.update_job(
        failed.id,
        error_code="UPLOAD_FAILED",
        error_message="provider unavailable",
    )

    bot = service(settings, database)
    active_event = Event(10)
    stats_event = Event(10)
    failed_event = Event(10)

    await bot._handle_command(active_event, admin, "/db activeworks")
    await bot._handle_command(stats_event, admin, "/db stats")
    await bot._handle_command(failed_event, admin, "/db failed 5")

    assert "queued.bin" in "\n".join(active_event.replies)
    assert "failed.bin" not in "\n".join(active_event.replies)
    assert "QUEUED: 1" in stats_event.replies[0]
    assert "FAILED: 1" in stats_event.replies[0]
    assert "UPLOAD_FAILED - provider unavailable" in failed_event.replies[0]
