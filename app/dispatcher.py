from __future__ import annotations

import asyncio
import logging
import math
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from .client_manager import TelegramClientManager
from .config import Settings
from .database import Database
from .models import JobStatus, UploadJob, utcnow_text
from .rclone_service import RcloneService
from .utils import format_bytes, format_duration, sanitize_filename

LOG = logging.getLogger(__name__)


class JobDispatcher:
    """Central owner router; it never broadcasts hidden-chat events to clients."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        clients: TelegramClientManager,
        rclone: RcloneService,
        bot: Any,
        bot_user_id: int,
    ):
        self.settings = settings
        self.database = database
        self.clients = clients
        self.rclone = rclone
        self.bot = bot
        self.bot_user_id = bot_user_id
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._jobs: dict[int, asyncio.Task[None]] = {}
        self._active_users: dict[int, int] = {}
        self._last_progress: dict[int, float] = {}

    async def start(self) -> None:
        self._running = True
        await self.database.recover_jobs()
        self._task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        for task in self._jobs.values():
            task.cancel()
        await asyncio.gather(
            *(list(self._jobs.values()) + ([self._task] if self._task else [])),
            return_exceptions=True,
        )

    async def cancel(self, user_id: int, job_id: int | None = None) -> bool:
        candidates = (
            [job_id]
            if job_id is not None
            else [
                int(row["id"])
                for row in await self.database.fetchall(
                    """
                    SELECT id FROM upload_jobs
                    WHERE owner_user_id=? AND status IN (?, ?, ?, ?)
                    ORDER BY created_at LIMIT 1
                    """,
                    (
                        user_id,
                        JobStatus.RECEIVED.value,
                        JobStatus.QUEUED.value,
                        JobStatus.DOWNLOADING.value,
                        JobStatus.UPLOADING.value,
                    ),
                )
            ]
        )
        for candidate in candidates:
            if candidate is None:
                continue
            job = await self.database.get_job(candidate)
            if not job or job.owner_user_id != user_id:
                continue
            await self.database.update_job(candidate, status=JobStatus.CANCELLED)
            await self.rclone.cancel(candidate)
            task = self._jobs.get(candidate)
            if task:
                task.cancel()
            return True
        return False

    async def cancel_all(self, user_id: int) -> int:
        rows = await self.database.fetchall(
            """
            SELECT id FROM upload_jobs
            WHERE owner_user_id=? AND status IN (?, ?, ?, ?, ?, ?)
            ORDER BY created_at, id
            """,
            (
                user_id,
                JobStatus.RECEIVED.value,
                JobStatus.FORWARDING.value,
                JobStatus.QUEUED.value,
                JobStatus.DOWNLOADING.value,
                JobStatus.DOWNLOADED.value,
                JobStatus.UPLOADING.value,
            ),
        )
        tasks = [
            self._jobs[int(row["id"])]
            for row in rows
            if int(row["id"]) in self._jobs
        ]
        cancelled = 0
        for row in rows:
            cancelled += int(await self.cancel(user_id, int(row["id"])))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return cancelled

    async def _loop(self) -> None:
        while self._running:
            try:
                available = max(
                    0,
                    self.settings.max_concurrent_user_workers - len(self._jobs),
                )
                if available:
                    for job in await self.database.next_queued_user_jobs(
                        self.settings.max_connected_users, self._active_users
                    ):
                        if len(self._jobs) >= self.settings.max_concurrent_user_workers:
                            break
                        assert job.id is not None
                        if (
                            job.id in self._jobs
                            or job.owner_user_id in self._active_users
                        ):
                            continue
                        owner_client = self.clients.get_client(job.owner_user_id)
                        if not owner_client or not owner_client.is_connected():
                            # A transiently unavailable session must not turn a
                            # durable queued job into a permanent failure.
                            continue
                        await self.database.update_job(
                            job.id,
                            status=JobStatus.DOWNLOADING,
                            started_at=utcnow_text(),
                        )
                        task = asyncio.create_task(self._process(job))
                        self._jobs[job.id] = task
                        self._active_users[job.owner_user_id] = job.id
                        task.add_done_callback(
                            lambda _task,
                            job_id=job.id,
                            user_id=job.owner_user_id: self._release_worker(
                                job_id, user_id
                            )
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Job dispatcher polling failed")
            await asyncio.sleep(self.settings.queue_poll_interval_seconds)

    async def _process(self, job: UploadJob) -> None:
        assert job.id is not None
        try:
            client = self.clients.get_client(job.owner_user_id)
            if not client or not client.is_connected():
                raise RuntimeError("Your Telegram session is not connected")
            job.status = JobStatus.DOWNLOADING
            job.started_at = utcnow_text()

            target_dir = self.settings.user_download_dir(job.owner_user_id)
            target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            filename = sanitize_filename(job.file_name)
            target = target_dir / f"{job.id}_{filename}"
            if not target.resolve().is_relative_to(target_dir.resolve()):
                raise ValueError("Unsafe download path")
            free = shutil.disk_usage(target_dir).free
            required = job.file_size + self.settings.min_free_disk_bytes
            if free < required:
                raise OSError("Insufficient disk space for this file")

            message = await self._resolve_private_source(client, job)
            if not message or not getattr(message, "media", None):
                LOG.error(
                    "Owner private-dialog lookup returned no media: "
                    "job=%s owner=%s source_message_id=%s result_type=%s",
                    job.id,
                    job.owner_user_id,
                    job.source_message_id,
                    type(message).__name__ if message is not None else "None",
                )
                raise FileNotFoundError(
                    "The submitted media is unavailable in your private bot chat"
                )
            await self._progress(job, "Downloading from Telegram", 0, 0, None, True)
            await self._download(client, message, target, job)

            if not target.is_file() or (
                job.file_size and target.stat().st_size != job.file_size
            ):
                raise OSError("Downloaded file size does not match Telegram metadata")
            job.local_path = str(target)
            job.status = JobStatus.DOWNLOADED
            await self.database.update_job(
                job.id,
                status=job.status,
                local_path=job.local_path,
            )

            job.remote_path = self.rclone.build_remote_path(job)
            job.remote_path = await self.rclone.resolve_collision(job, job.remote_path)
            job.status = JobStatus.UPLOADING
            await self.database.update_job(
                job.id, status=job.status, remote_path=job.remote_path
            )
            upload_started = time.monotonic()

            async def upload_progress(
                current: int, speed: float, eta: float | None
            ) -> None:
                await self._progress(
                    job,
                    f"Uploading to {job.selected_remote}",
                    min(current, job.file_size),
                    speed,
                    eta,
                )

            await self.rclone.upload_file(job, upload_progress)

            job.status = JobStatus.COMPLETED
            job.completed_at = utcnow_text()
            await self.database.update_job(
                job.id,
                status=job.status,
                completed_at=job.completed_at,
            )
            await self._edit(
                job,
                "Upload completed\n\n"
                f"File: {job.file_name}\n"
                f"Size: {format_bytes(job.file_size)}\n"
                f"Storage: {job.selected_remote}\n"
                f"Directory: {job.selected_root_directory}/{job.selected_directory}\n"
                f"Upload time: {format_duration(time.monotonic() - upload_started)}",
            )
            if self.settings.delete_local_after_success:
                target.unlink(missing_ok=True)
        except asyncio.CancelledError:
            current = await self.database.get_job(job.id)
            if current and current.status != JobStatus.CANCELLED:
                await self.database.update_job(job.id, status=JobStatus.QUEUED)
            raise
        except Exception as exc:
            LOG.exception("Upload job %s failed", job.id)
            job.status = JobStatus.FAILED
            await self.database.update_job(
                job.id,
                status=job.status,
                error_code=type(exc).__name__.upper(),
                error_message=str(exc)[:500],
                completed_at=utcnow_text(),
            )
            await self._edit(
                job,
                f"Upload failed\n\nFile: {job.file_name}\nReason: {str(exc)[:300]}",
            )

    def _release_worker(self, job_id: int, user_id: int) -> None:
        self._jobs.pop(job_id, None)
        if self._active_users.get(user_id) == job_id:
            self._active_users.pop(user_id, None)

    async def _download(
        self, client: Any, message: Any, target: Path, job: UploadJob
    ) -> None:
        started = time.monotonic()
        last_current = 0

        def callback(current: int, total: int) -> None:
            nonlocal last_current
            last_current = current
            elapsed = max(time.monotonic() - started, 0.001)
            speed = current / elapsed
            eta = (total - current) / speed if total and speed else None
            asyncio.create_task(
                self._progress(job, "Downloading from Telegram", current, speed, eta)
            )

        use_parallel = (
            self.settings.telegram_download_connections > 1
            and job.file_size
            >= self.settings.parallel_download_min_size_mb * 1024**2
        )
        if not use_parallel:
            await client.download_media(
                message, file=str(target), progress_callback=callback
            )
            return
        try:
            await self._download_parallel(client, message, target, job, callback)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception(
                "Parallel Telegram download failed for job %s; using sequential fallback",
                job.id,
            )
            target.unlink(missing_ok=True)
            await client.download_media(
                message, file=str(target), progress_callback=callback
            )

    async def _resolve_private_source(self, client: Any, job: UploadJob) -> Any:
        """Resolve the owner's original media through the bot's private reply."""
        if not job.source_reference:
            raise RuntimeError(
                "This is a legacy shared-group job. Submit the file again privately."
            )
        peer = await client.get_input_entity(self.bot_user_id)
        if job.source_message_id:
            return await client.get_messages(peer, ids=job.source_message_id)

        # Telegram search indexing can lag slightly behind the bot reply, so
        # search by the durable reference and fall back to recent messages
        # while indexing catches up. This never inspects another user's dialog
        # because ``client`` belongs to this job owner.
        for attempt in range(10):
            candidates = []
            async for candidate in client.iter_messages(
                peer, search=job.source_reference, limit=10
            ):
                candidates.append(candidate)
            if not candidates:
                async for candidate in client.iter_messages(peer, limit=100):
                    candidates.append(candidate)
            for candidate in candidates:
                text = (
                    getattr(candidate, "raw_text", None)
                    or getattr(candidate, "message", None)
                    or ""
                )
                if job.source_reference not in text:
                    continue
                if getattr(candidate, "sender_id", None) != self.bot_user_id:
                    continue
                reply_id = getattr(candidate, "reply_to_msg_id", None)
                if not reply_id:
                    continue
                source = await client.get_messages(peer, ids=reply_id)
                if source and getattr(source, "media", None):
                    job.source_message_id = int(reply_id)
                    assert job.id is not None
                    await self.database.update_job(
                        job.id, source_message_id=job.source_message_id
                    )
                    return source
            if attempt < 9:
                await asyncio.sleep(1)
        return None

    async def _download_parallel(
        self,
        client: Any,
        message: Any,
        target: Path,
        job: UploadJob,
        callback: Callable[[int, int], None],
    ) -> None:
        chunk_size = 512 * 1024
        connections = min(
            self.settings.telegram_download_connections,
            max(1, math.ceil(job.file_size / chunk_size)),
        )
        stride = chunk_size * connections
        with target.open("wb") as handle:
            handle.truncate(job.file_size)
        transferred = 0
        transfer_lock = asyncio.Lock()

        async def lane(index: int) -> None:
            nonlocal transferred
            offset = index * chunk_size
            if offset >= job.file_size:
                return
            limit = math.ceil((job.file_size - offset) / stride)
            position = offset
            iterator = client.iter_download(
                message.media,
                offset=offset,
                stride=stride,
                limit=limit,
                chunk_size=chunk_size,
                request_size=chunk_size,
                file_size=job.file_size,
            )
            try:
                with target.open("r+b", buffering=0) as handle:
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                iterator.__anext__(),
                                timeout=self.settings.telegram_download_stall_timeout_seconds,
                            )
                        except StopAsyncIteration:
                            break
                        data = bytes(chunk)
                        handle.seek(position)
                        handle.write(data)
                        position += stride
                        async with transfer_lock:
                            transferred += len(data)
                            callback(min(transferred, job.file_size), job.file_size)
            finally:
                await iterator.close()

        tasks = [asyncio.create_task(lane(index)) for index in range(connections)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _progress(
        self,
        job: UploadJob,
        phase: str,
        current: int,
        speed: float,
        eta: float | None,
        force: bool = False,
    ) -> None:
        assert job.id is not None
        now = time.monotonic()
        if (
            not force
            and now - self._last_progress.get(job.id, 0)
            < self.settings.progress_update_interval_seconds
        ):
            return
        self._last_progress[job.id] = now
        percent = (
            min(99.9, current * 100 / job.file_size) if job.file_size else 0
        )
        await self._edit(
            job,
            f"{phase}\n\n"
            f"File: {job.file_name}\n"
            f"Storage: {job.selected_remote}\n"
            f"Progress: {percent:.1f}%\n"
            f"Processed: {format_bytes(current)} / {format_bytes(job.file_size)}\n"
            f"Speed: {format_bytes(speed)}/s\n"
            f"ETA: {format_duration(eta)}",
        )

    async def _edit(self, job: UploadJob, text: str) -> None:
        if not job.status_message_id:
            return
        try:
            reference = (
                f"\n\nReference: {job.source_reference}"
                if job.source_reference
                else ""
            )
            await self.bot.edit_message(
                job.bot_chat_id,
                job.status_message_id,
                (text[: 4000 - len(reference)] + reference),
            )
        except Exception:
            LOG.warning("Unable to edit private status for job %s", job.id)
