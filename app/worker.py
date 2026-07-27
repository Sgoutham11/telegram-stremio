from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from pathlib import Path

from .config import Settings
from .exceptions import JobCancelled
from .file_service import FileService
from .models import JobStatus, UploadJob, utcnow
from .progress_service import ProgressService
from .queue_manager import QueueManager
from .rclone_service import RcloneService
from .state_store import StateStore
from .utils import format_bytes, format_duration

LOG = logging.getLogger(__name__)


class Worker:
    def __init__(self, client: object, settings: Settings, queue: QueueManager, state: StateStore, files: FileService, rclone: RcloneService, progress: ProgressService):
        self.client, self.settings, self.queue, self.state = client, settings, queue, state
        self.files, self.rclone, self.progress = files, rclone, progress
        self.alive = True

    async def run(self) -> None:
        while self.alive:
            job = await self.queue.get()
            try:
                await self.process(job)
            except asyncio.CancelledError:
                raise
            except JobCancelled as exc:
                LOG.info("Job %s cancelled", job.job_key)
                job.status, job.error_message, job.completed_at = JobStatus.CANCELLED, str(exc), utcnow()
                await self.state.save(job)
                if self.settings.delete_partial_on_failure:
                    await self.files.remove_job_directory(job)
                await self._edit(job, f"Upload cancelled\n\nFile: {job.filename}\nStorage: {job.rclone_remote or self.settings.default_rclone_remote}\nMessage ID: {job.message_id}")
            except Exception as exc:
                LOG.exception("Job %s failed", job.job_key)
                job.status, job.error_message, job.completed_at = JobStatus.FAILED, str(exc)[:500], utcnow()
                await self.state.save(job)
                if self.settings.delete_partial_on_failure:
                    await self.files.remove_job_directory(job)
                await self._edit(job, f"Upload failed\n\nFile: {job.filename}\nStorage: {job.rclone_remote or self.settings.default_rclone_remote}\nReason: {job.error_message}\nLocal file {'deleted' if self.settings.delete_partial_on_failure else 'retained for retry'}.\nMessage ID: {job.message_id}")
            finally:
                self.queue.finish(job)

    async def process(self, job: UploadJob) -> None:
        if self.queue.is_cancelled(job.job_key):
            raise JobCancelled("Job cancelled while queued")
        # Jobs snapshot their per-user selection at admission. Old state files
        # and snapshots whose remote was later removed fall back safely.
        job.rclone_remote = (
            self.settings.resolve_rclone_remote(job.rclone_remote)
            or self.settings.default_rclone_remote
            or self.settings.rclone_remote
        )
        job.started_at, job.status = utcnow(), JobStatus.DOWNLOADING
        path = await self.files.prepare(job)
        await self.state.save(job)
        await self.progress.update(job, "Downloading from Telegram", force=True)
        message = await self.client.get_messages(job.chat_id, ids=job.message_id)
        if not message or not message.media:
            raise FileNotFoundError("Source Telegram message or media is unavailable")
        started, last_progress_request = time.monotonic(), 0.0

        def download_progress(current: int, total: int) -> None:
            nonlocal last_progress_request
            if self.queue.is_cancelled(job.job_key):
                raise JobCancelled("Download cancelled")
            elapsed = max(time.monotonic() - started, .001)
            job.bytes_processed, job.file_size = current, total or job.file_size
            job.progress_percent = current * 100 / total if total else 0
            job.speed_bytes_per_second = current / elapsed
            job.eta_seconds = (total - current) / job.speed_bytes_per_second if total and job.speed_bytes_per_second else None
            now = time.monotonic()
            if now - last_progress_request >= self.settings.progress_update_interval_seconds:
                last_progress_request = now
                asyncio.create_task(self.progress.update(job, "Downloading from Telegram"))

        use_parallel = self.settings.telegram_download_connections > 1 and job.file_size >= self.settings.parallel_download_min_size_mb * 1024**2
        if use_parallel:
            try:
                await self._download_parallel(message, path, job, download_progress)
            except JobCancelled:
                raise
            except Exception as exc:
                LOG.exception("Parallel Telegram download failed for %s; restarting sequentially", job.job_key)
                job.bytes_processed, job.progress_percent = 0, 0
                job.speed_bytes_per_second, job.eta_seconds = 0, None
                await self.progress.update(
                    job,
                    f"Parallel Telegram download interrupted; restarting sequentially\nReason: {str(exc)[:300]}",
                    force=True,
                )
                await self.client.download_media(message, file=str(path), progress_callback=download_progress)
        else:
            await self.client.download_media(message, file=str(path), progress_callback=download_progress)
        if not path.is_file() or path.stat().st_size != job.file_size:
            raise IOError("Downloaded file size does not match Telegram metadata")
        job.status, job.bytes_processed, job.progress_percent = JobStatus.DOWNLOADED, 0, 0
        job.remote_path = self.rclone.build_remote_path(job.filename, job.remote_directory, job.rclone_remote)
        job.remote_path = await self.rclone.resolve_collision(job.remote_path)
        job.status = JobStatus.UPLOADING
        await self.state.save(job)

        last_rclone_bytes = 0
        upload_attempt = 1
        retry_reason: str | None = None

        async def upload_event(message: str) -> None:
            nonlocal retry_reason
            retry_reason = message
            # Make the retry visible immediately even if it occurs between
            # normal five-second progress update windows.
            await self.progress.update(
                job,
                f"Cloud upload error; rclone will retry\nAttempt: {upload_attempt + 1}\nReason: {message[:300]}",
                force=True,
            )

        async def upload_progress(current: int, speed: float, eta: float | None) -> None:
            nonlocal last_rclone_bytes, upload_attempt, retry_reason
            if self.queue.is_cancelled(job.job_key):
                await self.rclone.cancel_upload(job.job_key)
            regression_threshold = max(1024**2, int(job.file_size * 0.01))
            restarted = False
            if current < last_rclone_bytes - regression_threshold:
                upload_attempt += 1
                restarted = True
                LOG.warning("rclone restarted upload for %s (attempt %s)", job.job_key, upload_attempt)
            last_rclone_bytes = current
            # rclone's aggregate counter can exceed the source size after a
            # retry. Display the current attempt's real position; the attempt
            # label makes an intentional reset unambiguous.
            reported_bytes = min(max(current, 0), job.file_size) if job.file_size else max(current, 0)
            unique_bytes = reported_bytes
            job.bytes_processed, job.speed_bytes_per_second, job.eta_seconds = unique_bytes, max(speed, 0), eta
            # Reserve 100% for a successful rclone exit and remote size check.
            job.progress_percent = min(99.9, unique_bytes * 100 / job.file_size) if job.file_size else 0
            if job.file_size and current >= job.file_size:
                phase = f"Finalizing upload on {job.rclone_remote}\nAttempt: {upload_attempt}"
            elif upload_attempt > 1:
                reason = f"\nLast error: {retry_reason[:300]}" if retry_reason else ""
                phase = f"Retrying upload to {job.rclone_remote}\nAttempt: {upload_attempt}{reason}"
            else:
                phase = f"Uploading to {job.rclone_remote}"
            await self.progress.update(job, phase, force=restarted)

        result = await self.rclone.upload_file(job, upload_progress, upload_event)
        job.status, job.progress_percent, job.bytes_processed = JobStatus.COMPLETED, 100, job.file_size
        job.completed_at = utcnow()
        await self.state.save(job)
        elapsed = (job.completed_at - job.started_at).total_seconds() if job.started_at else 0
        remote_relative_path = result.remote_path.split(":", 1)[1] if ":" in result.remote_path else result.remote_path
        public_link = f"\nLink: {result.public_link}" if result.public_link else ""
        await self._edit(job, f"Upload completed\n\nFile: {job.filename}\nSize: {format_bytes(job.file_size)}\nStorage: {job.rclone_remote}\nPath: {remote_relative_path}{public_link}\nTotal time: {format_duration(elapsed)}\nMessage ID: {job.message_id}")
        if self.settings.delete_local_after_success:
            await self.files.remove_job_directory(job)

    async def _download_parallel(self, message: object, path: Path, job: UploadJob, progress_callback: Callable[[int, int], None]) -> None:
        """Download aligned Telegram chunks concurrently into disjoint offsets."""
        chunk_size = 512 * 1024
        connections = min(self.settings.telegram_download_connections, max(1, math.ceil(job.file_size / chunk_size)))
        stride = chunk_size * connections
        await asyncio.to_thread(self._preallocate, path, job.file_size)
        transferred = 0

        async def lane(index: int) -> None:
            nonlocal transferred
            offset = index * chunk_size
            if offset >= job.file_size:
                return
            limit = math.ceil((job.file_size - offset) / stride)
            position = offset
            iterator = self.client.iter_download(
                message.media,
                offset=offset,
                stride=stride,
                limit=limit,
                chunk_size=chunk_size,
                request_size=chunk_size,
                file_size=job.file_size,
            )
            try:
                with path.open("r+b", buffering=0) as handle:
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                iterator.__anext__(),
                                timeout=self.settings.telegram_download_stall_timeout_seconds,
                            )
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError as exc:
                            raise TimeoutError(
                                f"Telegram download lane {index + 1} received no data for "
                                f"{self.settings.telegram_download_stall_timeout_seconds:g} seconds"
                            ) from exc
                        if self.queue.is_cancelled(job.job_key):
                            raise JobCancelled("Download cancelled")
                        data = bytes(chunk)
                        handle.seek(position)
                        handle.write(data)
                        position += stride
                        transferred += len(data)
                        progress_callback(min(transferred, job.file_size), job.file_size)
            finally:
                await iterator.close()

        LOG.info("Starting parallel Telegram download for %s with %s connections", job.job_key, connections)
        tasks = [asyncio.create_task(lane(index)) for index in range(connections)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # asyncio.gather propagates the first exception without cancelling
            # its other children. Stop every lane before sequential fallback
            # rewrites the same destination file.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    @staticmethod
    def _preallocate(path: Path, size: int) -> None:
        with path.open("wb") as handle:
            handle.truncate(size)

    async def _edit(self, job: UploadJob, text: str) -> None:
        if job.status_message_id:
            try:
                await self.client.edit_message(job.chat_id, job.status_message_id, text)
            except Exception:
                LOG.exception("Unable to edit final status for %s", job.job_key)
