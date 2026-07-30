from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone

from app.bot_service import BotService
from app.dispatcher import JobDispatcher
from app.models import JobStatus, UploadJob
from app.security import RateLimiter
from conftest import telegram_user


class OwnerClient:
    def __init__(self, reference: str):
        self.reference = reference
        self.requested_ids = []

    async def get_input_entity(self, bot_user_id):
        assert bot_user_id == 500
        return "private-bot-dialog"

    async def iter_messages(self, peer, **kwargs):
        assert peer == "private-bot-dialog"
        yield SimpleNamespace(
            raw_text=f"Queued\nReference: {self.reference}",
            sender_id=500,
            reply_to_msg_id=777,
        )

    async def get_messages(self, peer, ids):
        assert peer == "private-bot-dialog"
        self.requested_ids.append(ids)
        return SimpleNamespace(id=ids, media=object())


class NeverUsedClient:
    async def get_input_entity(self, _bot_user_id):
        raise AssertionError("non-owner client was used")


class Clients:
    def __init__(self, owner):
        self.owner = owner
        self.non_owner = NeverUsedClient()

    def get_client(self, user_id):
        return self.owner if user_id == 10 else self.non_owner


async def test_private_reference_resolves_owner_original_message(
    settings, database
):
    await database.upsert_user(telegram_user(10), 10)
    reference = "JOB-private-reference"
    job = await database.create_job(
        UploadJob(
            owner_user_id=10,
            bot_chat_id=10,
            bot_message_id=20,
            source_reference=reference,
            file_name="file.bin",
            file_size=100,
            selected_remote="gdrive",
            selected_root_directory="TESTUSER",
            selected_directory="DOWNLOADS",
            status=JobStatus.QUEUED,
        )
    )
    owner = OwnerClient(reference)
    dispatcher = JobDispatcher(
        settings,
        database,
        Clients(owner),
        SimpleNamespace(),
        SimpleNamespace(),
        bot_user_id=500,
    )

    message = await dispatcher._resolve_private_source(owner, job)

    assert message.id == 777
    assert owner.requested_ids == [777]
    assert (await database.get_job(job.id)).source_message_id == 777


async def test_resolved_source_id_is_reused_after_restart(settings, database):
    await database.upsert_user(telegram_user(10), 10)
    job = await database.create_job(
        UploadJob(
            owner_user_id=10,
            bot_chat_id=10,
            bot_message_id=21,
            source_reference="JOB-persisted",
            source_message_id=888,
            file_name="file.bin",
            selected_remote="gdrive",
            selected_root_directory="TESTUSER",
            selected_directory="DOWNLOADS",
        )
    )
    owner = OwnerClient("unused")
    dispatcher = JobDispatcher(
        settings,
        database,
        Clients(owner),
        SimpleNamespace(),
        SimpleNamespace(),
        bot_user_id=500,
    )

    message = await dispatcher._resolve_private_source(owner, job)

    assert message.id == 888
    assert owner.requested_ids == [888]


async def test_bot_submission_stays_in_private_dialog(settings, database):
    user = telegram_user(10, "Private User")
    await database.upsert_user(user, 10)
    await database.set_user_fields(
        10,
        telegram_connected=1,
        storage_connected=1,
        selected_remote="gdrive",
    )

    class Storage:
        async def list_remotes(self, user_id):
            assert user_id == 10
            return ["gdrive"]

        async def verify_connection(self, user_id, remote):
            return user_id == 10 and remote == "gdrive"

    class Event:
        chat_id = 10

        def __init__(self):
            self.replies = []
            self.message = SimpleNamespace(
                id=30,
                file=SimpleNamespace(
                    name="private.bin",
                    size=5 * 1024**3,
                    mime_type="application/octet-stream",
                ),
                media=SimpleNamespace(),
                date=datetime.now(timezone.utc),
            )

        async def reply(self, text, **_kwargs):
            self.replies.append(text)
            return SimpleNamespace(id=900)

    event = Event()
    service = BotService(
        # This object deliberately has no forward_messages method.
        SimpleNamespace(),
        settings,
        database,
        SimpleNamespace(get_client=lambda _user_id: object()),
        Storage(),
        SimpleNamespace(),
        RateLimiter(),
    )

    await service._submit_file(event, user)

    rows = await database.fetchall("SELECT * FROM upload_jobs")
    assert len(rows) == 1
    assert rows[0]["status"] == JobStatus.QUEUED.value
    assert rows[0]["internal_chat_id"] is None
    assert rows[0]["internal_message_id"] is None
    assert rows[0]["source_reference"].startswith("JOB-")
    assert rows[0]["source_reference"] in event.replies[0]
