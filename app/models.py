from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
import re

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECOVERABLE = "RECOVERABLE"


class UploadJob(BaseModel):
    job_key: str
    chat_id: int
    message_id: int
    sender_id: int
    filename: str
    upload_username: str = ""
    upload_directory: str = "DOWNLOADS"
    rclone_remote: str = ""
    file_size: int = 0
    media_group_id: str | None = None
    local_path: str | None = None
    remote_path: str | None = None
    status: JobStatus = JobStatus.QUEUED
    progress_percent: float = 0.0
    bytes_processed: int = 0
    speed_bytes_per_second: float = 0.0
    eta_seconds: float | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status_message_id: int | None = None

    @field_validator("upload_directory")
    @classmethod
    def upload_directory_is_safe(cls, value: str) -> str:
        value = value.strip()
        parts = [part.strip() for part in value.split("/")]
        if not parts or len(parts) > 10 or len(value) > 500 or any(
            part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9 _-]{1,100}", part)
            for part in parts
        ):
            raise ValueError("invalid upload directory")
        return "/".join(parts)

    @field_validator("upload_username")
    @classmethod
    def upload_username_is_safe(cls, value: str) -> str:
        value = value.strip()
        if value and (value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9 _-]{1,100}", value)):
            raise ValueError("invalid upload username")
        return value

    @field_validator("rclone_remote")
    @classmethod
    def rclone_remote_is_safe(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("invalid rclone remote")
        return value

    @property
    def remote_directory(self) -> str:
        return f"{self.upload_username}/{self.upload_directory}" if self.upload_username else self.upload_directory

    @property
    def directory(self) -> Path | None:
        return Path(self.local_path).parent if self.local_path else None


class RcloneResult(BaseModel):
    remote_path: str
    verified: bool
    public_link: str | None = None
