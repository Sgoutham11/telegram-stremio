from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.database import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        telegram_api_id=123,
        telegram_api_hash="api-hash-secret",
        telegram_bot_token="123:bot-secret",
        internal_upload_chat_id=-100123456,
        google_client_id="google-client",
        google_client_secret="google-secret",
        google_redirect_uri="https://example.test/api/storage/google/callback",
        public_base_url="https://example.test",
        database_path=tmp_path / "app.db",
        user_data_root=tmp_path / "users",
        pending_telegram_root=tmp_path / "pending",
        user_rclone_root=tmp_path / "rclone-users",
        bot_session_path=tmp_path / "bot.session",
        log_file=tmp_path / "logs" / "app.log",
        min_free_disk_bytes=0,
        telegram_download_connections=1,
        queue_poll_interval_seconds=0.1,
    )


@pytest.fixture
async def database(settings: Settings):
    database = Database(settings.database_path)
    await database.connect()
    yield database
    await database.close()


def telegram_user(user_id: int, name: str = "Test User"):
    first, *last = name.split(maxsplit=1)
    return SimpleNamespace(
        id=user_id,
        username=f"user{user_id}",
        first_name=first,
        last_name=last[0] if last else None,
        bot=False,
    )
