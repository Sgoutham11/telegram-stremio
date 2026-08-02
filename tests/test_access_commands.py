from __future__ import annotations

from types import SimpleNamespace

from app.bot_service import BotService
from app.models import JobStatus, UploadJob
from app.security import RateLimiter
from conftest import telegram_user


class Event:
    def __init__(self, chat_id: int, sender=None, text: str = ""):
        self.chat_id = chat_id
        self.sender = sender
        self.raw_text = text
        self.is_private = True
        self.message = SimpleNamespace(media=None)
        self.replies: list[str] = []

    async def get_sender(self):
        return self.sender

    async def reply(self, text: str, **_kwargs):
        self.replies.append(text)


class Bot:
    def on(self, _event):
        def decorator(callback):
            self.handler = callback
            return callback

        return decorator


class Clients:
    def __init__(self):
        self.stopped = []

    async def stop_user(self, user_id):
        self.stopped.append(user_id)


class Dispatcher:
    def __init__(self):
        self.cancelled = []

    async def cancel_all(self, user_id):
        self.cancelled.append(user_id)
        return 0

    async def cancel(self, *_args):
        return False


class Onboarding:
    def __init__(self):
        self.cleared = []

    async def clear_user(self, user_id):
        self.cleared.append(user_id)


def service(settings, database):
    bot = Bot()
    clients = Clients()
    dispatcher = Dispatcher()
    onboarding = Onboarding()
    result = BotService(
        bot,
        settings,
        database,
        clients,
        SimpleNamespace(),
        dispatcher,
        RateLimiter(),
        onboarding=onboarding,
    )
    return result, bot, clients, dispatcher, onboarding


async def connected_user(database, settings, user_id):
    user = telegram_user(user_id, f"User {user_id}")
    await database.upsert_user(user, user_id)
    session = settings.user_session_path(user_id)
    session.parent.mkdir(parents=True)
    session.write_bytes(b"session")
    config = settings.user_rclone_config(user_id)
    config.parent.mkdir(parents=True)
    config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
    await database.set_user_fields(
        user_id,
        telegram_connected=1,
        storage_connected=1,
        session_path=str(session),
        rclone_config_path=str(config),
        selected_remote="gdrive",
    )
    await database.execute(
        """
        INSERT INTO storage_connections (
            telegram_user_id, provider, remote_name, config_path,
            connected, created_at, updated_at
        ) VALUES (?, 'google', 'gdrive', ?, 1, 'now', 'now')
        """,
        (user_id, str(config)),
    )
    return user


async def test_user_clear_removes_connections_and_allows_reconnect(
    settings, database
):
    user = await connected_user(database, settings, 20)
    await database.execute(
        """
        INSERT INTO onboarding_tokens
        (token_hash, telegram_user_id, created_at, expires_at, used)
        VALUES ('onboarding', 20, 'now', 'later', 0)
        """
    )
    await database.execute(
        """
        INSERT INTO web_sessions
        (token_hash, telegram_user_id, created_at, expires_at, revoked)
        VALUES ('web', 20, 'now', 'later', 0)
        """
    )
    await database.execute(
        """
        INSERT INTO oauth_states
        (state_hash, web_session_hash, telegram_user_id, provider,
         created_at, expires_at, used)
        VALUES ('oauth', 'web', 20, 'google', 'now', 'later', 0)
        """
    )
    job = await database.create_job(
        UploadJob(
            owner_user_id=20,
            bot_chat_id=20,
            bot_message_id=100,
            file_name="queued.bin",
            selected_remote="gdrive",
            selected_root_directory="USER20",
            selected_directory="DOWNLOADS",
            status=JobStatus.QUEUED,
        )
    )
    uploader, _, clients, dispatcher, onboarding = service(settings, database)
    event = Event(20)

    await uploader._handle_command(event, user, "/clear")

    record = await database.get_user(20)
    assert record["active"] == 1
    assert record["telegram_connected"] == 0
    assert record["storage_connected"] == 0
    assert record["session_path"] is None
    assert record["rclone_config_path"] is None
    assert record["selected_remote"] is None
    assert await database.storage_connections(20) == []
    assert await database.fetchall(
        "SELECT * FROM onboarding_tokens WHERE telegram_user_id=20"
    ) == []
    assert await database.fetchall(
        "SELECT * FROM web_sessions WHERE telegram_user_id=20"
    ) == []
    assert await database.fetchall(
        "SELECT * FROM oauth_states WHERE telegram_user_id=20"
    ) == []
    assert (await database.get_job(job.id)).status == JobStatus.CANCELLED
    assert not settings.user_data_dir(20).exists()
    assert not settings.user_rclone_config(20).parent.exists()
    assert clients.stopped == [20]
    assert dispatcher.cancelled == [20]
    assert onboarding.cleared == [20]
    assert "Use /connect" in event.replies[0]


async def test_admin_clear_does_not_block_user(settings, database):
    settings.admin_telegram_user_id = 10
    admin = await connected_user(database, settings, 10)
    await connected_user(database, settings, 20)
    uploader, *_ = service(settings, database)
    event = Event(10)

    await uploader._handle_command(event, admin, "/clear 20")

    assert (await database.get_user(20))["active"] == 1
    assert event.replies == ["User 20 was cleared and may connect again."]


async def test_admin_block_rejects_messages_until_unblocked(settings, database):
    settings.admin_telegram_user_id = 10
    settings.admin_contact = "@adminusername"
    admin = await connected_user(database, settings, 10)
    target = await connected_user(database, settings, 20)
    uploader, bot, *_ = service(settings, database)

    block_event = Event(10)
    await uploader._handle_command(block_event, admin, "/block 20")
    assert (await database.get_user(20))["active"] == 0
    assert block_event.replies == ["User 20 was cleared and blocked."]

    uploader.register()
    blocked_event = Event(20, target, "hello")
    await bot.handler(blocked_event)
    assert blocked_event.replies == [
        "You are blocked. Contact admin @adminusername for more details."
    ]

    unblock_event = Event(10)
    await uploader._handle_command(unblock_event, admin, "/unblock 20")
    assert (await database.get_user(20))["active"] == 1
    assert "may use /start and /connect again" in unblock_event.replies[0]


async def test_start_does_not_bypass_admin_block(settings, database):
    user = telegram_user(20, "Blocked User")
    await database.upsert_user(user, 20)
    await database.set_user_fields(20, active=0)
    uploader, *_ = service(settings, database)
    event = Event(20)

    await uploader._handle_command(event, user, "/start")

    assert (await database.get_user(20))["active"] == 0
    assert "You are blocked" in event.replies[0]


async def test_blocked_message_uses_stored_admin_username_by_default(
    settings, database
):
    settings.admin_telegram_user_id = 10
    await database.upsert_user(telegram_user(10, "Admin User"), 10)
    uploader, *_ = service(settings, database)

    assert await uploader._blocked_message() == (
        "You are blocked. Contact admin @user10 for more details."
    )
