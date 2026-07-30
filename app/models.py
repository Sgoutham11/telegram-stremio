from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
import re

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_text() -> str:
    return utcnow().isoformat()


class JobStatus(StrEnum):
    RECEIVED = "RECEIVED"
    FORWARDING = "FORWARDING"
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TelegramLoginStatus(StrEnum):
    NOT_CONNECTED = "NOT_CONNECTED"
    QR_LOADING = "QR_LOADING"
    WAITING_FOR_SCAN = "WAITING_FOR_SCAN"
    PHONE_CODE_LOADING = "PHONE_CODE_LOADING"
    WAITING_FOR_CODE = "WAITING_FOR_CODE"
    TWO_FACTOR_REQUIRED = "TWO_FACTOR_REQUIRED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class UploadJob(BaseModel):
    id: int | None = None
    owner_user_id: int
    bot_chat_id: int
    bot_message_id: int
    internal_chat_id: int | None = None
    internal_message_id: int | None = None
    source_reference: str | None = None
    source_message_id: int | None = None
    status_message_id: int | None = None
    file_name: str
    file_size: int = 0
    mime_type: str | None = None
    selected_remote: str
    selected_root_directory: str
    selected_directory: str
    local_path: str | None = None
    remote_path: str | None = None
    status: JobStatus = JobStatus.RECEIVED
    error_code: str | None = None
    error_message: str | None = None
    created_at: str = Field(default_factory=utcnow_text)
    updated_at: str = Field(default_factory=utcnow_text)
    started_at: str | None = None
    completed_at: str | None = None

    @field_validator("file_name")
    @classmethod
    def file_name_is_safe(cls, value: str) -> str:
        if not value or Path(value).name != value or "\x00" in value:
            raise ValueError("invalid file name")
        return value

    @field_validator("selected_remote")
    @classmethod
    def remote_is_safe(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value.strip()):
            raise ValueError("invalid rclone remote")
        return value.strip()

    @field_validator("selected_directory")
    @classmethod
    def directory_is_safe(cls, value: str) -> str:
        parts = [part.strip() for part in value.split("/")]
        if (
            not parts
            or len(value) > 500
            or len(parts) > 10
            or any(
                part in {"", ".", ".."}
                or not re.fullmatch(r"[A-Za-z0-9 _-]{1,100}", part)
                for part in parts
            )
        ):
            raise ValueError("invalid upload directory")
        return "/".join(parts)

    @field_validator("selected_root_directory")
    @classmethod
    def root_directory_is_safe(cls, value: str) -> str:
        value = value.strip().upper()
        if (
            value in {"", ".", ".."}
            or not re.fullmatch(r"[A-Z0-9 _-]{1,100}", value)
        ):
            raise ValueError("invalid root directory")
        return value

    @property
    def job_key(self) -> str:
        return str(self.id or f"{self.owner_user_id}:{self.bot_message_id}")


class RcloneResult(BaseModel):
    remote_path: str
    verified: bool
    public_link: str | None = None
