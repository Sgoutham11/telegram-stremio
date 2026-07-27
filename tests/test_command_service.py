import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from app.command_service import CommandService
from app.config import Settings
from app.directory_service import DirectoryService
from app.handlers import register_handlers
from app.queue_manager import QueueManager
from app.remote_service import RemoteService
from app.state_store import StateStore


class Event:
    def __init__(self, text, sender_id=123):
        self.raw_text = text
        self.sender_id = sender_id
        self.replies = []

    async def reply(self, text):
        self.replies.append(text)


class FakeClient:
    def __init__(self):
        self.handler = None

    def on(self, _event_type):
        def decorator(callback):
            self.handler = callback
            return callback
        return decorator


def settings(tmp_path):
    return Settings(
        _env_file=None,
        telegram_api_id=1,
        telegram_api_hash="x",
        watch_mode="chat",
        watch_chat_id=-100123,
        allowed_user_ids=[123, 456],
        allowed_user_names=["GOUTHAM", "GALAXY"],
        download_dir=tmp_path,
        state_dir=tmp_path,
        log_dir=tmp_path,
        log_file=tmp_path / "x.log",
    )


async def service(tmp_path):
    config = settings(tmp_path)
    directories = DirectoryService(tmp_path, config.rclone_base_path, config.default_upload_directory, config.allowed_users)
    await directories.load()
    remotes = RemoteService(config)
    await remotes.load()
    return CommandService(config, QueueManager(5), StateStore(tmp_path), directories, remotes), directories


async def test_dir_show_set_nested_reset_and_user_isolation(tmp_path):
    commands, directories = await service(tmp_path)
    show = Event(".dir", sender_id=123)
    await commands.handle(show)
    assert "UPLOADS/GOUTHAM/DOWNLOADS" in show.replies[0]

    change = Event(".dir Series/Friends", sender_id=123)
    await commands.handle(change)
    assert "UPLOADS/GOUTHAM/Series/Friends" in change.replies[0]
    assert await directories.get_user_current_directory(123) == "Series/Friends"
    assert await directories.get_user_current_directory(456) == "DOWNLOADS"

    galaxy = Event(".dir Anime", sender_id=456)
    await commands.handle(galaxy)
    assert "UPLOADS/GALAXY/Anime" in galaxy.replies[0]
    assert await directories.get_user_current_directory(123) == "Series/Friends"

    reset = Event(".dir reset", sender_id=123)
    await commands.handle(reset)
    assert "Upload directory reset" in reset.replies[0]
    assert await directories.get_user_current_directory(123) == "DOWNLOADS"
    assert await directories.get_user_current_directory(456) == "Anime"


async def test_dir_rejects_unsafe_name(tmp_path):
    commands, directories = await service(tmp_path)
    event = Event(".dir ../secret")
    await commands.handle(event)
    assert event.replies == ["Invalid directory name.\nUse letters, numbers, spaces, hyphens, and underscores, with / between nested folders."]
    assert await directories.get_user_current_directory(123) == "DOWNLOADS"


async def test_remote_commands_and_per_user_isolation(tmp_path):
    commands, _ = await service(tmp_path)
    commands.settings.allowed_rclone_remotes = ["gdrive", "mega"]

    show = Event(".remote", sender_id=123)
    await commands.handle(show)
    assert show.replies == [
        "Current storage: gdrive\nAvailable storage: gdrive, mega\n\nUsage:\n.remote <storage-name>"
    ]

    select = Event(".remote MEGA", sender_id=123)
    await commands.handle(select)
    assert select.replies == ["Storage changed to: mega"]
    assert await commands.remotes.get_selected_remote(123) == "mega"
    assert await commands.remotes.get_selected_remote(456) == "gdrive"

    listing = Event(".remotes", sender_id=123)
    await commands.handle(listing)
    assert listing.replies == [
        "Available storage:\n1. gdrive\n2. mega\n\nCurrent storage: mega"
    ]

    invalid = Event(".remote unknown", sender_id=123)
    await commands.handle(invalid)
    assert invalid.replies == [
        "Unknown storage: unknown\n\nAvailable storage:\ngdrive, mega"
    ]


async def test_status_with_no_active_job_uses_selected_remote(tmp_path):
    commands, _ = await service(tmp_path)
    status = Event(".status", sender_id=123)
    await commands.handle(status)
    assert "Active: none" in status.replies[0]
    assert "Storage: gdrive" in status.replies[0]
    assert "Destination: gdrive:UPLOADS/GOUTHAM/DOWNLOADS" in status.replies[0]


async def test_authorization_and_monitored_group_are_enforced(tmp_path):
    commands, directories = await service(tmp_path)
    client = FakeClient()
    config = commands.settings
    register_handlers(client, config, commands.queue, commands.state, commands, directories, self_id=123)

    unauthorized = Event(".dir Movies", sender_id=999)
    unauthorized.chat_id = -100123
    await client.handler(unauthorized)
    assert unauthorized.replies == ["DM admin @sgoutham11 to access the streaming platform."]

    unrelated = Event(".dir Movies", sender_id=123)
    unrelated.chat_id = -100999
    await client.handler(unrelated)
    assert unrelated.replies == []
    assert await directories.get_user_current_directory(123) == "DOWNLOADS"


async def test_unauthorized_notice_never_replies_to_self(tmp_path):
    commands, directories = await service(tmp_path)
    client = FakeClient()
    config = commands.settings
    register_handlers(client, config, commands.queue, commands.state, commands, directories, self_id=777)

    own_message = Event("Hello", sender_id=777)
    own_message.chat_id = -100123
    await client.handler(own_message)
    assert own_message.replies == []


async def test_debug_ids_are_logged_before_chat_and_sender_filters(tmp_path, caplog):
    commands, directories = await service(tmp_path)
    commands.settings.debug_telegram_ids = True
    client = FakeClient()
    register_handlers(client, commands.settings, commands.queue, commands.state, commands, directories, self_id=123)

    class DebugEvent(Event):
        chat_id = -100777
        is_channel = True
        is_group = True
        is_private = False
        message = SimpleNamespace(document=object(), photo=None, media=object(), message="Hello")

        async def get_chat(self):
            return SimpleNamespace(title="Uploader Group")

        async def get_sender(self):
            return SimpleNamespace(id=999, first_name="Test", last_name="User", username="testuser")

    event = DebugEvent("Hello", sender_id=999)
    with caplog.at_level(logging.INFO, logger="app.handlers"):
        await client.handler(event)

    output = caplog.text
    assert "Telegram Debug Information" in output
    assert "Chat ID      : -100777" in output
    assert "Chat Name    : Uploader Group" in output
    assert "Chat Type    : supergroup" in output
    assert "Sender ID    : 999" in output
    assert "Sender Name  : Test User" in output
    assert "Username     : @testuser" in output
    assert "Message Type : document" in output
    assert event.replies == []


async def test_debug_disabled_does_not_fetch_entities(tmp_path):
    commands, directories = await service(tmp_path)
    client = FakeClient()
    register_handlers(client, commands.settings, commands.queue, commands.state, commands, directories, self_id=123)

    class NoFetchEvent(Event):
        chat_id = -100999

        async def get_chat(self):
            raise AssertionError("chat lookup must not run")

        async def get_sender(self):
            raise AssertionError("sender lookup must not run")

    await client.handler(NoFetchEvent("Hello", sender_id=999))


async def test_concurrent_files_receive_distinct_queue_positions(tmp_path):
    commands, directories = await service(tmp_path)
    commands.settings.allowed_rclone_remotes = ["gdrive", "mega"]
    await commands.remotes.set_selected_remote(123, "mega")
    client = FakeClient()
    register_handlers(client, commands.settings, commands.queue, commands.state, commands, directories, self_id=123)

    class StatusReply:
        def __init__(self, message_id):
            self.id = message_id

    class MediaEvent(Event):
        chat_id = -100123

        def __init__(self, message_id):
            super().__init__("", sender_id=123)
            self.message = SimpleNamespace(
                id=message_id,
                media=SimpleNamespace(),
                file=SimpleNamespace(size=1024, name=f"file-{message_id}.bin", mime_type="application/octet-stream"),
                date=datetime.now(timezone.utc),
                grouped_id=None,
            )

        async def reply(self, text):
            # Ensure all concurrent handlers reach the old race window.
            await asyncio.sleep(0.01)
            self.replies.append(text)
            return StatusReply(self.message.id + 1000)

    events = [MediaEvent(message_id) for message_id in (1, 2, 3)]
    await asyncio.gather(*(client.handler(event) for event in events))

    assert [event.replies[0].rsplit("Position: ", 1)[1] for event in events] == ["1", "2", "3"]
    await commands.remotes.set_selected_remote(123, "gdrive")
    assert [job.rclone_remote for job in commands.queue.snapshot()] == ["mega", "mega", "mega"]
