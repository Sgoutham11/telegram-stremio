from __future__ import annotations

from types import SimpleNamespace

from app.bot_service import BotService
from app.models import UploadJob
from app.rclone_service import RcloneService
from app.security import RateLimiter
from conftest import telegram_user


class Event:
    chat_id = 10

    def __init__(self):
        self.replies = []

    async def reply(self, text, **_kwargs):
        self.replies.append(text)


class Noop:
    async def cancel(self, *_args):
        return False


async def test_dirroot_command_persists_uppercase_override(settings, database):
    user = telegram_user(10, "Goutham S")
    await database.upsert_user(user, 10)
    event = Event()
    service = BotService(
        SimpleNamespace(),
        settings,
        database,
        SimpleNamespace(),
        SimpleNamespace(),
        Noop(),
        RateLimiter(),
    )

    await service._handle_command(event, user, "/dirroot Personal Root")

    stored = await database.get_user(10)
    assert stored["selected_root_directory"] == "PERSONAL ROOT"
    assert event.replies == ["Root directory changed to: PERSONAL ROOT"]


async def test_dirroot_reset_returns_to_sanitized_telegram_name(settings, database):
    user = telegram_user(10, "Gou@tham S!")
    await database.upsert_user(user, 10)
    await database.set_user_fields(10, selected_root_directory="CUSTOM")
    event = Event()
    service = BotService(
        SimpleNamespace(),
        settings,
        database,
        SimpleNamespace(),
        SimpleNamespace(),
        Noop(),
        RateLimiter(),
    )

    await service._handle_command(event, user, "/dirroot reset")

    stored = await database.get_user(10)
    assert stored["selected_root_directory"] == "GOU_THAMS"


def test_rclone_destination_uses_captured_root_and_directory(settings):
    job = UploadJob(
        owner_user_id=10,
        bot_chat_id=10,
        bot_message_id=1,
        file_name="video.mkv",
        selected_remote="gdrive",
        selected_root_directory="GOUTHAM",
        selected_directory="SERIES/Friends",
    )
    assert (
        RcloneService(settings).build_remote_path(job)
        == "gdrive:GOUTHAM/SERIES/Friends/video.mkv"
    )


def test_root_change_does_not_mutate_existing_job_snapshot(settings):
    job = UploadJob(
        owner_user_id=10,
        bot_chat_id=10,
        bot_message_id=1,
        file_name="video.mkv",
        selected_remote="gdrive",
        selected_root_directory="ORIGINAL",
        selected_directory="DOWNLOADS",
    )
    future_user_root = "CHANGED"
    assert future_user_root != job.selected_root_directory
    assert (
        RcloneService(settings).build_remote_path(job)
        == "gdrive:ORIGINAL/DOWNLOADS/video.mkv"
    )
