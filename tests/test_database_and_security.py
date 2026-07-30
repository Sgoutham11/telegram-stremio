from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.models import JobStatus, UploadJob
from app.security import RateLimiter, expires_at, random_token, token_hash
from conftest import telegram_user


async def test_user_registration_and_isolated_preferences(database):
    await database.upsert_user(telegram_user(10, "Alice A"), 10, "INBOX")
    await database.upsert_user(telegram_user(20, "Bob B"), 20, "INBOX")
    await database.set_user_fields(10, selected_directory="Movies", selected_remote="a")
    await database.set_user_fields(20, selected_directory="Series", selected_remote="b")

    alice = await database.get_user(10)
    bob = await database.get_user(20)
    assert (alice["selected_directory"], alice["selected_remote"]) == ("Movies", "a")
    assert (bob["selected_directory"], bob["selected_remote"]) == ("Series", "b")


async def test_duplicate_job_is_rejected(database):
    await database.upsert_user(telegram_user(10), 10)
    job = UploadJob(
        owner_user_id=10,
        bot_chat_id=10,
        bot_message_id=5,
        file_name="file.bin",
        selected_remote="gdrive",
        selected_root_directory="TESTUSER",
        selected_directory="DOWNLOADS",
    )
    await database.create_job(job)
    with pytest.raises(Exception):
        await database.create_job(job.model_copy(update={"id": None}))


async def test_jobs_and_cancellation_are_owner_scoped(database):
    for user_id in (10, 20):
        await database.upsert_user(telegram_user(user_id), user_id)
    alice = await database.create_job(
        UploadJob(
            owner_user_id=10,
            bot_chat_id=10,
            bot_message_id=1,
            file_name="a.bin",
            selected_remote="gdrive",
            selected_root_directory="ALICE",
            selected_directory="A",
            status=JobStatus.QUEUED,
        )
    )
    bob = await database.create_job(
        UploadJob(
            owner_user_id=20,
            bot_chat_id=20,
            bot_message_id=1,
            file_name="b.bin",
            selected_remote="gdrive",
            selected_root_directory="BOB",
            selected_directory="B",
            status=JobStatus.QUEUED,
        )
    )
    assert await database.cancel_user_jobs(10, bob.id) == 0
    assert (await database.get_job(bob.id)).status == JobStatus.QUEUED
    assert await database.cancel_user_jobs(10, alice.id) == 1
    assert (await database.get_job(alice.id)).status == JobStatus.CANCELLED


async def test_one_time_onboarding_token_is_single_use(database):
    await database.upsert_user(telegram_user(10), 10)
    raw = random_token()
    await database.execute(
        """
        INSERT INTO onboarding_tokens
        (token_hash, telegram_user_id, created_at, expires_at, used)
        VALUES (?, ?, ?, ?, 0)
        """,
        (
            token_hash(raw),
            10,
            datetime.now(timezone.utc).isoformat(),
            expires_at(60),
        ),
    )
    assert await database.claim_onboarding_token(token_hash(raw))
    assert await database.claim_onboarding_token(token_hash(raw)) is None


async def test_expired_token_is_rejected(database):
    await database.upsert_user(telegram_user(10), 10)
    raw = random_token()
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    await database.execute(
        """
        INSERT INTO onboarding_tokens
        (token_hash, telegram_user_id, created_at, expires_at, used)
        VALUES (?, ?, ?, ?, 0)
        """,
        (token_hash(raw), 10, past, past),
    )
    assert await database.claim_onboarding_token(token_hash(raw)) is None


def test_tokens_are_high_entropy_and_only_hash_is_stable():
    first, second = random_token(), random_token()
    assert first != second
    assert len(first) >= 40
    assert token_hash(first) == token_hash(first)
    assert first not in token_hash(first)


def test_rate_limit_reports_retry_time():
    limiter = RateLimiter()
    limiter.check("connect", "10", 1, 60)
    with pytest.raises(HTTPException) as raised:
        limiter.check("connect", "10", 1, 60)
    assert "seconds" in raised.value.detail
    assert raised.value.headers["Retry-After"]


def test_per_user_paths_cannot_be_selected_from_message_text(settings):
    assert settings.user_session_path(123).as_posix().endswith("/users/123/telegram.session")
    assert settings.user_rclone_config(123).as_posix().endswith(
        "/rclone-users/123/rclone.conf"
    )


def test_default_root_uses_sanitized_uppercase_name():
    from app.utils import default_root_directory

    assert default_root_directory("Gou@tham", " S!", 10) == "GOU_THAM_S"
    assert default_root_directory(None, None, 10) == "USER_10"
