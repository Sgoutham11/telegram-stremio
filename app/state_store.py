from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from .models import JobStatus, UploadJob

LOG = logging.getLogger(__name__)


class StateStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, job_key: str) -> Path:
        return self.directory / f"{job_key.replace(':', '_')}.json"

    async def save(self, job: UploadJob) -> None:
        lock = self._locks.setdefault(job.job_key, asyncio.Lock())
        async with lock:
            await asyncio.to_thread(self._save_sync, job)

    def _save_sync(self, job: UploadJob) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self._path(job.job_key)
        temporary = destination.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(job.model_dump(mode="json"), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)

    async def load_all(self) -> dict[str, UploadJob]:
        return await asyncio.to_thread(self._load_all_sync)

    def _load_all_sync(self) -> dict[str, UploadJob]:
        jobs: dict[str, UploadJob] = {}
        self.directory.mkdir(parents=True, exist_ok=True)
        for path in self.directory.glob("*.json"):
            if path.name in {"health.json", "current_directory.json", "user_directories.json", "user_remotes.json"}:
                continue
            try:
                job = UploadJob.model_validate_json(path.read_text(encoding="utf-8"))
                jobs[job.job_key] = job
            except Exception:
                LOG.exception("Ignoring corrupted state file %s", path)
        return jobs

    async def recover(self, retry: bool) -> list[UploadJob]:
        jobs = await self.load_all()
        recovered = []
        active = {JobStatus.QUEUED, JobStatus.DOWNLOADING, JobStatus.DOWNLOADED, JobStatus.UPLOADING, JobStatus.RECOVERABLE}
        for job in jobs.values():
            if job.status in active:
                job.status = JobStatus.RECOVERABLE if retry else JobStatus.FAILED
                job.error_message = "Interrupted by application restart"
                await self.save(job)
                if retry:
                    recovered.append(job)
        return recovered
