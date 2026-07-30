from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-only application configuration.

    API credentials are deliberately not included in ``public_dict`` and are
    never sent to either the bot or the onboarding frontend.
    """

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False
    )

    telegram_api_id: int
    telegram_api_hash: str
    telegram_bot_token: str
    # Deprecated compatibility input. New jobs are resolved inside each
    # user's private bot dialog and do not use a shared processing group.
    internal_upload_chat_id: int | None = None

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    public_base_url: str = "http://localhost:8080"

    database_path: Path = Path("/data/app.db")
    user_data_root: Path = Path("/data/users")
    pending_telegram_root: Path = Path("/data/pending-telegram")
    user_rclone_root: Path = Path("/config/users")
    bot_session_path: Path = Path("/data/bot.session")

    web_host: str = "0.0.0.0"
    web_port: int = Field(8080, ge=1, le=65535)
    qr_login_ttl_seconds: int = Field(120, ge=30, le=600)
    telegram_2fa_ttl_seconds: int = Field(300, ge=30, le=1800)
    max_telegram_2fa_attempts: int = Field(5, ge=1, le=10)
    web_session_ttl_seconds: int = Field(1800, ge=60, le=86400)
    onboarding_token_ttl_seconds: int = Field(600, ge=60, le=3600)
    oauth_state_ttl_seconds: int = Field(600, ge=60, le=3600)

    default_rclone_remote: str = "gdrive"
    default_upload_directory: str = "DOWNLOADS"
    rclone_base_path: str = "UPLOADS"
    remote_collision_policy: str = "rename"
    rclone_drive_chunk_size: str = "64Mi"
    rclone_retries: int = Field(5, ge=0, le=50)
    rclone_low_level_retries: int = Field(10, ge=0, le=100)
    rclone_retries_sleep_seconds: int = Field(10, ge=0, le=300)
    rclone_stats_interval_seconds: int = Field(2, ge=1, le=60)
    rclone_upload_timeout_minutes: int = Field(180, ge=1)
    rclone_transfers: int = Field(1, ge=1, le=16)
    rclone_checkers: int = Field(2, ge=1, le=32)

    max_connected_users: int = Field(100, ge=1)
    max_pending_qr_logins: int = Field(10, ge=1)
    multy_rclone_count: int = Field(2, ge=1, le=10)
    max_file_size_bytes: int = Field(0, ge=0)
    min_free_disk_bytes: int = Field(5 * 1024**3, ge=0)
    max_concurrent_user_workers: int = Field(2, ge=1, le=32)
    queue_poll_interval_seconds: float = Field(1.0, ge=0.1, le=30)
    progress_update_interval_seconds: float = Field(5.0, ge=1, le=60)
    telegram_download_connections: int = Field(4, ge=1, le=16)
    telegram_download_stall_timeout_seconds: float = Field(120, ge=15)
    parallel_download_min_size_mb: int = Field(64, ge=1)
    delete_local_after_success: bool = True

    admin_telegram_user_id: int | None = None
    admin_contact: str = ""

    log_level: str = "INFO"
    log_file: Path = Path("/data/logs/uploader.log")
    log_max_bytes: int = Field(10_485_760, ge=1024)
    log_backup_count: int = Field(5, ge=0)

    @field_validator("default_rclone_remote")
    @classmethod
    def remote_name_is_safe(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
            raise ValueError("DEFAULT_RCLONE_REMOTE contains invalid characters")
        return value

    @field_validator("public_base_url")
    @classmethod
    def public_url_is_safe(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or ".." in parsed.path.split("/")
        ):
            raise ValueError("PUBLIC_BASE_URL must be an HTTP(S) origin and path")
        if parsed.path and not re.fullmatch(
            r"(?:/[A-Za-z0-9._~-]+)+", parsed.path
        ):
            raise ValueError("PUBLIC_BASE_URL contains an invalid path prefix")
        return value

    @field_validator("default_upload_directory")
    @classmethod
    def directory_is_safe(cls, value: str) -> str:
        value = value.strip()
        if (
            value in {"", ".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9 _-]{1,100}", value)
        ):
            raise ValueError("DEFAULT_UPLOAD_DIRECTORY contains invalid characters")
        return value

    @field_validator("rclone_drive_chunk_size")
    @classmethod
    def chunk_size_is_safe(cls, value: str) -> str:
        if not re.fullmatch(r"[1-9][0-9]*(?:Ki|Mi|Gi|K|M|G)?", value):
            raise ValueError("RCLONE_DRIVE_CHUNK_SIZE must be a valid rclone size")
        return value

    @field_validator("admin_telegram_user_id", mode="before")
    @classmethod
    def empty_admin_id_is_disabled(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_limits(self) -> "Settings":
        if self.remote_collision_policy not in {"rename", "overwrite", "skip"}:
            raise ValueError("REMOTE_COLLISION_POLICY must be rename, overwrite or skip")
        return self

    def prepare_directories(self) -> None:
        directories = (
            self.database_path.parent,
            self.user_data_root,
            self.pending_telegram_root,
            self.user_rclone_root,
            self.bot_session_path.parent,
            self.log_file.parent,
        )
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    def user_data_dir(self, telegram_user_id: int) -> Path:
        return self.user_data_root / str(int(telegram_user_id))

    def user_session_path(self, telegram_user_id: int) -> Path:
        return self.user_data_dir(telegram_user_id) / "telegram.session"

    def user_download_dir(self, telegram_user_id: int) -> Path:
        return self.user_data_dir(telegram_user_id) / "downloads"

    def user_rclone_config(self, telegram_user_id: int) -> Path:
        return self.user_rclone_root / str(int(telegram_user_id)) / "rclone.conf"

    @property
    def public_base_path(self) -> str:
        """Path prefix exposed by the reverse proxy, or empty at domain root."""
        path = urlsplit(self.public_base_url).path.rstrip("/")
        return "" if path in {"", "/"} else path

    def public_dict(self) -> dict[str, object]:
        hidden = {
            "telegram_api_hash",
            "telegram_bot_token",
            "google_client_secret",
            "admin_telegram_user_id",
        }
        return {
            key: str(value) if isinstance(value, Path) else value
            for key, value in self.model_dump().items()
            if key not in hidden
        }
