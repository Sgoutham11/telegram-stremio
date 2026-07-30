from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.bot_service import BotService
from app.dispatcher import JobDispatcher
from app.models import JobStatus, UploadJob
from app.security import RateLimiter
from conftest import telegram_user


async def create_queued_job(
    database,
    user_id: int,
    message_id: int,
    file_name: str,
    created_at: datetime,
) -> UploadJob:
    return await database.create_job(
        UploadJob(
            owner_user_id=user_id,
            bot_chat_id=user_id,
            bot_message_id=message_id,
            file_name=file_name,
            selected_remote="gdrive",
            selected_root_directory=f"USER_{user_id}",
            selected_directory="DOWNLOADS",
            status=JobStatus.QUEUED,
            created_at=created_at.isoformat(),
            updated_at=created_at.isoformat(),
        )
    )


async def test_worker_candidates_are_distinct_users_and_fifo(database):
    for user_id in (1, 2, 3):
        await database.upsert_user(telegram_user(user_id), user_id)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    user1_file1 = await create_queued_job(
        database, 1, 1, "file1", start
    )
    user1_file2 = await create_queued_job(
        database, 1, 2, "file2", start + timedelta(seconds=1)
    )
    user2_file3 = await create_queued_job(
        database, 2, 3, "file3", start + timedelta(seconds=2)
    )
    await create_queued_job(
        database, 2, 4, "file4", start + timedelta(seconds=3)
    )
    user3_file5 = await create_queued_job(
        database, 3, 5, "file5", start + timedelta(seconds=4)
    )
    user2_file6 = await create_queued_job(
        database, 2, 6, "file6", start + timedelta(seconds=5)
    )
    await create_queued_job(
        database, 2, 7, "file7", start + timedelta(seconds=6)
    )

    first_workers = await database.next_queued_user_jobs(2)
    assert [(job.owner_user_id, job.file_name) for job in first_workers] == [
        (1, "file1"),
        (2, "file3"),
    ]

    # With users 1 and 2 assigned, the dispatcher has zero free worker slots
    # and does not request another candidate. If a slot becomes available,
    # the selector correctly identifies user 3 as the next distinct owner.
    waiting_distinct_user = await database.next_queued_user_jobs(1, {1, 2})
    assert [job.id for job in waiting_distinct_user] == [user3_file5.id]

    # When user 1's slot is available, its older file remains ahead of user 3.
    next_for_free_slot = await database.next_queued_user_jobs(1, {2})
    assert [job.id for job in next_for_free_slot] == [user1_file1.id]
    await database.update_job(user1_file1.id, status=JobStatus.COMPLETED)
    next_for_free_slot = await database.next_queued_user_jobs(1, {2})
    assert [job.id for job in next_for_free_slot] == [user1_file2.id]

    # User 3's earlier submission stays ahead of user 2's later work once
    # user 2 becomes eligible again.
    for job in await database.fetchall(
        "SELECT id FROM upload_jobs WHERE owner_user_id IN (1, 2) AND id < ?",
        (user3_file5.id,),
    ):
        await database.update_job(int(job["id"]), status=JobStatus.COMPLETED)
    next_after_earlier_work = await database.next_queued_user_jobs(2)
    assert [job.id for job in next_after_earlier_work] == [
        user3_file5.id,
        user2_file6.id,
    ]


async def test_third_user_submission_reports_busy_workers(settings, database):
    settings.max_concurrent_user_workers = 2
    for user_id in (10, 20, 30):
        await database.upsert_user(telegram_user(user_id), user_id)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await create_queued_job(database, 10, 1, "first.bin", start)
    await create_queued_job(
        database, 20, 2, "second.bin", start + timedelta(seconds=1)
    )
    await database.set_user_fields(
        30,
        telegram_connected=1,
        storage_connected=1,
        selected_remote="gdrive",
    )

    class Storage:
        async def list_remotes(self, _user_id):
            return ["gdrive"]

        async def verify_connection(self, _user_id, _remote):
            return True

    class Event:
        chat_id = 30

        def __init__(self):
            self.replies: list[str] = []
            self.message = SimpleNamespace(
                id=3,
                file=SimpleNamespace(
                    name="third.bin",
                    size=1024,
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
        SimpleNamespace(),
        settings,
        database,
        SimpleNamespace(get_client=lambda _user_id: object()),
        Storage(),
        SimpleNamespace(),
        RateLimiter(),
    )

    await service._submit_file(event, telegram_user(30))

    assert "2 workers are busy." in event.replies[0]
    assert "queued in submission-time order" in event.replies[0]
    assert await database.user_worker_queue_position(30) == 3


async def test_dispatcher_assigns_at_most_one_worker_per_user(settings, database):
    settings.max_concurrent_user_workers = 2
    settings.queue_poll_interval_seconds = 0.01
    for user_id in (1, 2, 3):
        await database.upsert_user(telegram_user(user_id), user_id)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await create_queued_job(database, 1, 1, "user1-first", start)
    await create_queued_job(
        database, 1, 2, "user1-second", start + timedelta(milliseconds=1)
    )
    await create_queued_job(
        database, 2, 3, "user2-first", start + timedelta(milliseconds=2)
    )
    await create_queued_job(
        database, 3, 4, "user3-first", start + timedelta(milliseconds=3)
    )

    class ConnectedClient:
        @staticmethod
        def is_connected():
            return True

    clients = SimpleNamespace(get_client=lambda _user_id: ConnectedClient())
    dispatcher = JobDispatcher(
        settings,
        database,
        clients,
        SimpleNamespace(),
        SimpleNamespace(),
        bot_user_id=500,
    )
    started: list[tuple[int, str]] = []
    hold_workers = asyncio.Event()

    async def hold(job):
        started.append((job.owner_user_id, job.file_name))
        await hold_workers.wait()

    dispatcher._process = hold
    await dispatcher.start()
    try:
        for _ in range(50):
            if len(started) == 2:
                break
            await asyncio.sleep(0.01)
        assert started == [(1, "user1-first"), (2, "user2-first")]
        assert len(dispatcher._active_users) == 2
        assert set(dispatcher._active_users) == {1, 2}
    finally:
        await dispatcher.shutdown()
