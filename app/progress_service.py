from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from telethon.errors import FloodWaitError, MessageNotModifiedError

from .models import UploadJob
from .utils import format_bytes, format_duration

LOG = logging.getLogger(__name__)


class ProgressService:
    def __init__(self, client: object, interval: float):
        self.client = client
        self.interval = interval
        self._last: dict[str, float] = {}
        self._last_text: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def update(self, job: UploadJob, phase: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last.get(job.job_key, 0) < self.interval:
            return
        if not job.status_message_id:
            return
        lock = self._locks.setdefault(job.job_key, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            storage = f"\nStorage: {job.rclone_remote}" if job.rclone_remote else ""
            text = f"{phase}\n\nFile: {job.filename}{storage}\nProgress: {job.progress_percent:.1f}%\nProcessed: {format_bytes(job.bytes_processed)} / {format_bytes(job.file_size)}\nSpeed: {format_bytes(job.speed_bytes_per_second)}/s\nETA: {format_duration(job.eta_seconds)}"
            if text == self._last_text.get(job.job_key):
                self._last[job.job_key] = time.monotonic()
                return
            # Rate-limit temporary failures too. Otherwise a failed edit is
            # retried for every rclone stats line instead of at the configured
            # Telegram update interval.
            self._last[job.job_key] = time.monotonic()
            try:
                await self.client.edit_message(job.chat_id, job.status_message_id, text)
                self._last_text[job.job_key] = text
            except MessageNotModifiedError:
                self._last_text[job.job_key] = text
            except FloodWaitError as exc:
                LOG.warning("Progress edit flood wait: %ss", exc.seconds)
            except Exception:
                LOG.exception("Temporary progress edit failure for %s", job.job_key)
