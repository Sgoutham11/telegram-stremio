from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable

from .config import Settings
from .exceptions import JobCancelled, UploadError
from .models import RcloneResult, UploadJob

ProgressCallback = Callable[[int, float, float | None], Awaitable[None]]
UploadEventCallback = Callable[[str], Awaitable[None]]
LOG = logging.getLogger(__name__)


def parse_rclone_progress(line: str) -> tuple[int, float, float | None] | None:
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    # With --use-json-log, accounting snapshots are nested under "stats".
    # Accept flat objects too so the parser remains compatible with older
    # rclone wrappers and recorded state fixtures.
    stats = data.get("stats", data)
    if not isinstance(stats, dict) or "bytes" not in stats:
        return None
    # Global accounting.bytes includes bytes sent again by retries. Since
    # copyto handles exactly one file, its active transfer entry is the
    # accurate unique-file position and must take precedence.
    transferring = stats.get("transferring")
    current = transferring[0] if isinstance(transferring, list) and transferring and isinstance(transferring[0], dict) else stats
    transferred = int(current.get("bytes", 0))
    speed = float(current.get("speed", stats.get("speed", 0)))
    eta = current.get("eta", stats.get("eta"))
    return transferred, speed, float(eta) if eta is not None else None


class RcloneService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.processes: dict[str, asyncio.subprocess.Process] = {}

    async def _run(self, *args: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        return process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def list_configured_remotes(self) -> list[str]:
        code, out, err = await self._run(
            "rclone",
            "listremotes",
            "--config",
            str(self.settings.rclone_config_path),
        )
        if code:
            raise UploadError(f"Unable to read rclone remote names: {err.strip()[:300]}")
        return [line.strip()[:-1] for line in out.splitlines() if line.strip().endswith(":")]

    async def validate_configured_remotes(self) -> None:
        configured = await self.list_configured_remotes()
        configured_by_name = {name.casefold(): name for name in configured}
        missing = [
            remote
            for remote in self.settings.allowed_rclone_remotes
            if remote.casefold() not in configured_by_name
        ]
        if missing:
            raise UploadError(
                "Configured rclone remote name(s) missing from rclone.conf: "
                + ", ".join(missing)
            )
        # Use the exact section spelling emitted by rclone after performing
        # case-insensitive matching against Telegram/config input.
        canonical_allowed = [
            configured_by_name[remote.casefold()]
            for remote in self.settings.allowed_rclone_remotes
        ]
        default = configured_by_name[
            (self.settings.default_rclone_remote or self.settings.rclone_remote).casefold()
        ]
        self.settings.allowed_rclone_remotes = canonical_allowed
        self.settings.default_rclone_remote = default
        self.settings.rclone_remote = default
        LOG.info("Configured rclone remotes: %s", ", ".join(self.settings.allowed_rclone_remotes))

    async def validate_remote(self) -> None:
        """Backward-compatible startup validator without cloud connectivity."""
        await self.validate_configured_remotes()

    def build_remote_path(self, filename: str, upload_directory: str, remote_name: str | None = None) -> str:
        requested = remote_name or self.settings.default_rclone_remote or self.settings.rclone_remote
        remote = self.settings.resolve_rclone_remote(requested)
        if remote is None:
            raise ValueError(f"rclone remote is not allowed: {requested}")
        relative = PurePosixPath(self.settings.rclone_base_path.strip("/")) / upload_directory / filename
        return f"{remote}:{relative.as_posix()}"

    async def remote_exists(self, remote_path: str) -> bool:
        code, out, _ = await self._run("rclone", "lsjson", remote_path, "--stat", "--config", str(self.settings.rclone_config_path))
        return code == 0 and bool(out.strip())

    async def resolve_collision(self, remote_path: str) -> str:
        if not await self.remote_exists(remote_path):
            return remote_path
        policy = self.settings.remote_collision_policy
        if policy == "overwrite":
            return remote_path
        if policy == "skip":
            raise FileExistsError("Destination already exists and collision policy is skip")
        prefix, name = remote_path.rsplit("/", 1)
        path = Path(name)
        for index in range(1, 10000):
            candidate = f"{prefix}/{path.stem}_{index}{path.suffix}"
            if not await self.remote_exists(candidate):
                return candidate
        raise UploadError("Unable to find an unused remote filename")

    async def upload_file(self, job: UploadJob, callback: ProgressCallback, event_callback: UploadEventCallback | None = None) -> RcloneResult:
        assert job.local_path and job.remote_path
        args = ["rclone", "copyto", job.local_path, job.remote_path, "--config", str(self.settings.rclone_config_path), "--stats", f"{self.settings.rclone_stats_interval_seconds}s", "--use-json-log", "--log-level", "INFO", "--stats-log-level", "NOTICE", "--retries", str(self.settings.rclone_retries), "--retries-sleep", f"{self.settings.rclone_retries_sleep_seconds}s", "--low-level-retries", str(self.settings.rclone_low_level_retries), "--transfers", str(self.settings.rclone_transfers), "--checkers", str(self.settings.rclone_checkers), "--drive-chunk-size", self.settings.rclone_drive_chunk_size]
        args.extend(shlex.split(self.settings.rclone_extra_args))
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.processes[job.job_key] = process
        errors: list[str] = []
        observed_error_count = 0
        diagnostics: deque[str] = deque(maxlen=30)
        last_event_at = 0.0
        diagnostic_pattern = re.compile(
            r"failed|retry|timeout|timed out|reset|broken pipe|unexpected eof|"
            r"(?:http(?: status)?[ /:]|status[ =])(?:403|429|5\d\d)\b",
            re.IGNORECASE,
        )

        async def consume(stream: asyncio.StreamReader | None) -> None:
            nonlocal observed_error_count, last_event_at
            if not stream:
                return
            async for raw in stream:
                line = raw.decode(errors="replace").strip()
                parsed = parse_rclone_progress(line)
                if parsed:
                    await callback(*parsed)
                if line:
                    try:
                        record = json.loads(line)
                        message = str(record.get("msg", line))
                        stats_record = record.get("stats", {})
                        error_count = int(stats_record.get("errors", 0)) if isinstance(stats_record, dict) else 0
                        if message and not stats_record:
                            diagnostics.append(message)
                        # Stats messages contain arbitrary byte counts which
                        # must never be interpreted as HTTP status codes.
                        important = parsed is None and diagnostic_pattern.search(message) is not None
                        severe = str(record.get("level", "")).lower() in {"error", "critical", "emergency", "alert"} or "fatal error" in message.lower()
                        if severe:
                            errors.append(message)
                        if severe or important:
                            LOG.warning("rclone upload message for %s: %s", job.job_key, message)
                            now = time.monotonic()
                            if event_callback and now - last_event_at >= 10:
                                last_event_at = now
                                await event_callback(message[:300])
                        if error_count > observed_error_count:
                            observed_error_count = error_count
                            detail = next((item for item in reversed(diagnostics) if diagnostic_pattern.search(item)), "")
                            summary = f"rclone reported {error_count} transfer error(s)"
                            if detail:
                                summary += f": {detail}"
                            LOG.warning("%s for %s", summary, job.job_key)
                            now = time.monotonic()
                            if event_callback and now - last_event_at >= 10:
                                last_event_at = now
                                await event_callback(summary[:300])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        diagnostics.append(line)

        try:
            await asyncio.wait_for(
                asyncio.gather(consume(process.stdout), consume(process.stderr)),
                timeout=self.settings.rclone_upload_timeout_minutes * 60,
            )
        except asyncio.TimeoutError as exc:
            await self._terminate_process(process)
            self.processes.pop(job.job_key, None)
            raise UploadError(
                f"Cloud upload exceeded {self.settings.rclone_upload_timeout_minutes} minutes and was stopped"
            ) from exc
        code = await process.wait()
        self.processes.pop(job.job_key, None)
        if code != 0:
            # A cloud may commit the object but its final HTTP response can be
            # lost. Verify before declaring failure so the file is not sent
            # again merely because rclone missed that acknowledgement.
            verified = await self.verify_upload_eventually(job)
            if not verified:
                raise UploadError((errors[-1] if errors else f"rclone exited with code {code}")[:500])
            LOG.warning("rclone exited with code %s for %s, but remote size verification succeeded", code, job.job_key)
        else:
            verified = await self.verify_upload_eventually(job)
        if not verified:
            raise UploadError("Upload completed but destination size verification failed")
        link = await self.public_link(job.remote_path) if self.settings.generate_public_link else None
        return RcloneResult(remote_path=job.remote_path, verified=True, public_link=link)

    async def verify_upload(self, job: UploadJob) -> bool:
        assert job.remote_path
        code, out, _ = await self._run("rclone", "lsjson", job.remote_path, "--stat", "--config", str(self.settings.rclone_config_path))
        if code:
            return False
        try:
            return int(json.loads(out).get("Size", -1)) == job.file_size
        except (json.JSONDecodeError, AttributeError, ValueError):
            return False

    async def verify_upload_eventually(self, job: UploadJob, attempts: int = 6, delay_seconds: float = 5) -> bool:
        """Allow time for a just-committed remote object to become visible."""
        for attempt in range(attempts):
            if await self.verify_upload(job):
                return True
            if attempt + 1 < attempts:
                await asyncio.sleep(delay_seconds)
        return False

    async def public_link(self, path: str) -> str | None:
        code, out, _ = await self._run("rclone", "link", path, "--config", str(self.settings.rclone_config_path))
        return out.strip() if code == 0 else None

    async def cancel_upload(self, job_key: str) -> None:
        process = self.processes.get(job_key)
        if not process or process.returncode is not None:
            return
        await self._terminate_process(process)
        raise JobCancelled("Upload cancelled")

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
