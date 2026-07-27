from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False, populate_by_name=True)

    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str = ""
    telegram_session_path: Path = Path("/data/session/telegram.session")
    watch_mode: Literal["saved_messages", "chat"] = "saved_messages"
    watch_chat_id: int | None = None
    debug_telegram_ids: bool = False
    allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    allowed_user_names: Annotated[list[str], NoDecode] = Field(default_factory=list, validation_alias="ALLOWED_USER_NAME")
    # RCLONE_REMOTE is retained for backward compatibility. At validation
    # time it is normalized to the effective DEFAULT_RCLONE_REMOTE.
    rclone_remote: str = "gdrive"
    default_rclone_remote: str | None = None
    allowed_rclone_remotes: Annotated[list[str], NoDecode] = Field(default_factory=list)
    rclone_base_path: str = "UPLOADS"
    default_upload_directory: str = "DOWNLOADS"
    remote_folder_pattern: str = ""  # Deprecated; retained only for env compatibility.
    remote_collision_policy: Literal["rename", "overwrite", "skip"] = "rename"
    download_dir: Path = Path("/data/downloads")
    state_dir: Path = Path("/data/state")
    log_dir: Path = Path("/data/logs")
    rclone_config_path: Path = Path("/config/rclone/rclone.conf")
    max_concurrent_jobs: int = Field(1, ge=1, le=16)
    queue_max_size: int = Field(20, ge=1)
    max_file_size_gb: float = Field(0, ge=0)
    min_free_disk_gb: float = Field(5, ge=0)
    progress_update_interval_seconds: float = Field(5, ge=1)
    telegram_download_connections: int = Field(4, ge=1, le=16)
    telegram_download_stall_timeout_seconds: float = Field(120, gt=0)
    parallel_download_min_size_mb: int = Field(64, ge=1)
    rclone_transfers: int = Field(1, ge=1)
    rclone_checkers: int = Field(2, ge=1)
    rclone_retries: int = Field(5, ge=0)
    rclone_low_level_retries: int = Field(10, ge=0)
    rclone_retries_sleep_seconds: int = Field(10, ge=0)
    rclone_stats_interval_seconds: int = Field(2, ge=1)
    rclone_drive_chunk_size: str = "64Mi"
    rclone_upload_timeout_minutes: int = Field(180, ge=1)
    rclone_extra_args: str = ""
    delete_local_after_success: bool = True
    delete_partial_on_failure: bool = False
    failed_file_retention_hours: float = Field(24, ge=0)
    cleanup_interval_minutes: float = Field(30, gt=0)
    retry_interrupted_jobs: bool = True
    generate_public_link: bool = False
    log_level: str = "INFO"
    log_file: Path = Path("/data/logs/uploader.log")
    log_max_bytes: int = Field(10_485_760, ge=1024)
    log_backup_count: int = Field(5, ge=0)

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def parse_ids(cls, value: object) -> object:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [int(x.strip()) for x in value.split(",") if x.strip()]
        return value

    @field_validator("watch_chat_id", mode="before")
    @classmethod
    def parse_optional_chat_id(cls, value: object) -> object:
        if value in (None, ""):
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("allowed_user_names", mode="before")
    @classmethod
    def parse_user_names(cls, value: object) -> object:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("allowed_rclone_remotes", mode="before")
    @classmethod
    def parse_rclone_remotes(cls, value: object) -> object:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("allowed_user_names")
    @classmethod
    def user_names_are_safe(cls, value: list[str]) -> list[str]:
        for name in value:
            if not re.fullmatch(r"[A-Za-z0-9 _-]{1,100}", name) or name in {".", ".."}:
                raise ValueError("ALLOWED_USER_NAME entries may contain only letters, numbers, spaces, hyphens, and underscores")
        return value

    @field_validator("rclone_remote")
    @classmethod
    def remote_name_is_safe(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("RCLONE_REMOTE contains invalid characters")
        return value

    @field_validator("default_rclone_remote", mode="before")
    @classmethod
    def optional_default_remote_is_safe(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            raise ValueError("DEFAULT_RCLONE_REMOTE must be text")
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("DEFAULT_RCLONE_REMOTE contains invalid characters")
        return value

    @field_validator("default_upload_directory")
    @classmethod
    def default_directory_is_safe(cls, value: str) -> str:
        value = value.strip()
        if value in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9 _-]{1,100}", value):
            raise ValueError("DEFAULT_UPLOAD_DIRECTORY contains invalid characters")
        return value

    @field_validator("rclone_drive_chunk_size")
    @classmethod
    def drive_chunk_size_is_safe(cls, value: str) -> str:
        if not re.fullmatch(r"[1-9][0-9]*(?:Ki|Mi|Gi|K|M|G)?", value):
            raise ValueError("RCLONE_DRIVE_CHUNK_SIZE must be a positive rclone size such as 64Mi")
        return value

    @model_validator(mode="after")
    def validate_mode(self) -> "Settings":
        if self.watch_mode == "chat" and self.watch_chat_id is None and not self.debug_telegram_ids:
            raise ValueError("WATCH_CHAT_ID is required when WATCH_MODE=chat")
        if len(self.allowed_user_ids) != len(self.allowed_user_names):
            raise ValueError("ALLOWED_USER_IDS and ALLOWED_USER_NAME must contain the same number of entries")
        if len(set(self.allowed_user_ids)) != len(self.allowed_user_ids):
            raise ValueError("ALLOWED_USER_IDS must not contain duplicates")
        normalized_names = [name.casefold() for name in self.allowed_user_names]
        if len(set(normalized_names)) != len(normalized_names):
            raise ValueError("ALLOWED_USER_NAME must not contain duplicate names")
        if self.watch_mode == "chat" and not self.allowed_user_ids and not self.debug_telegram_ids:
            raise ValueError("ALLOWED_USER_IDS and ALLOWED_USER_NAME require at least one entry when WATCH_MODE=chat")
        default_remote = self.default_rclone_remote or self.rclone_remote
        allowed_remotes = [remote.strip() for remote in self.allowed_rclone_remotes] or [default_remote]
        for remote in allowed_remotes:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", remote):
                raise ValueError(f"ALLOWED_RCLONE_REMOTES contains an invalid remote name: {remote!r}")
        folded = [remote.casefold() for remote in allowed_remotes]
        if len(set(folded)) != len(folded):
            raise ValueError("ALLOWED_RCLONE_REMOTES must not contain duplicate names")
        canonical_default = next(
            (remote for remote in allowed_remotes if remote.casefold() == default_remote.casefold()),
            None,
        )
        if canonical_default is None:
            raise ValueError("DEFAULT_RCLONE_REMOTE must be present in ALLOWED_RCLONE_REMOTES")
        self.allowed_rclone_remotes = allowed_remotes
        self.default_rclone_remote = canonical_default
        self.rclone_remote = canonical_default
        return self

    def prepare_directories(self) -> None:
        for directory in (self.download_dir, self.state_dir, self.log_dir, self.telegram_session_path.parent):
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()

    def validate_runtime(self, require_session: bool = True) -> None:
        self.prepare_directories()
        if shutil.which("rclone") is None:
            raise ValueError("rclone executable was not found")
        if not self.rclone_config_path.is_file():
            raise ValueError(f"rclone config not found: {self.rclone_config_path}")
        # OAuth providers such as Google Drive periodically refresh their
        # token. rclone saves that token by creating a sibling temporary file
        # and atomically replacing rclone.conf, so checking only file write
        # access is insufficient.
        config_probe = self.rclone_config_path.parent / ".rclone-write-test"
        try:
            config_probe.write_text("ok", encoding="utf-8")
            config_probe.unlink()
        except OSError as exc:
            raise ValueError(
                f"rclone config directory is not writable: {self.rclone_config_path.parent}; "
                "OAuth remotes require a writable config to persist refreshed tokens"
            ) from exc
        if require_session and not session_exists(self.telegram_session_path):
            raise ValueError("Telegram session missing; run: python -m app.auth")

    @property
    def allowed_users(self) -> dict[int, str]:
        return dict(zip(self.allowed_user_ids, self.allowed_user_names, strict=True))

    @property
    def telegram_id_discovery_only(self) -> bool:
        return self.debug_telegram_ids and self.watch_mode == "chat" and (
            self.watch_chat_id is None or not self.allowed_user_ids
        )

    def get_allowed_username(self, user_id: int) -> str:
        try:
            return self.allowed_users[user_id]
        except KeyError as exc:
            raise PermissionError(f"Telegram user {user_id} is not allowed") from exc

    def resolve_rclone_remote(self, remote_name: str) -> str | None:
        candidate = remote_name.strip().casefold()
        return next((remote for remote in self.allowed_rclone_remotes if remote.casefold() == candidate), None)

    def is_authorized(self, sender_id: int, self_id: int | None = None) -> bool:
        return sender_id in self.allowed_users

    def public_dict(self) -> dict[str, object]:
        hidden = {"telegram_api_hash", "telegram_phone"}
        return {k: str(v) if isinstance(v, Path) else v for k, v in self.model_dump().items() if k not in hidden}


def session_exists(path: Path) -> bool:
    return path.is_file() or path.with_suffix(path.suffix + ".session").is_file()
