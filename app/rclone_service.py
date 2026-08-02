from __future__ import annotations

import asyncio
import configparser
import json
import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable

import httpx

from .config import Settings
from .exceptions import UploadError
from .models import RcloneResult, UploadJob

LOG = logging.getLogger(__name__)
ProgressCallback = Callable[[int, float, float | None], Awaitable[None]]
GOOGLE_DRIVE_RCLONE_SCOPE = "drive.file"
GOOGLE_DRIVE_RECONNECT_MESSAGE = (
    "Google Drive permissions have changed. Please disconnect and reconnect "
    "Google Drive."
)


def parse_rclone_progress(line: str) -> tuple[int, float, float | None] | None:
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    stats = data.get("stats", data)
    if not isinstance(stats, dict) or "bytes" not in stats:
        return None
    transfers = stats.get("transferring")
    current = (
        transfers[0]
        if isinstance(transfers, list)
        and transfers
        and isinstance(transfers[0], dict)
        else stats
    )
    transferred = int(current.get("bytes", 0))
    speed = float(current.get("speed", stats.get("speed", 0)))
    eta = current.get("eta", stats.get("eta"))
    return transferred, speed, float(eta) if eta is not None else None


class RcloneService:
    """Runs rclone with the job owner's configuration on every invocation."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.processes: dict[int, asyncio.subprocess.Process] = {}

    def config_path(self, user_id: int) -> Path:
        return self.settings.user_rclone_config(int(user_id))

    async def _run(self, *args: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return (
            process.returncode or 0,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    async def list_remotes(self, user_id: int) -> list[str]:
        config = self.config_path(user_id)
        if not config.is_file():
            return []
        code, output, error = await self._run(
            "rclone", "listremotes", "--config", str(config)
        )
        if code:
            raise UploadError(f"Unable to read storage remotes: {error.strip()[:300]}")
        return [
            line.strip()[:-1]
            for line in output.splitlines()
            if line.strip().endswith(":")
        ]

    async def validate_remote(self, user_id: int, remote: str) -> str:
        remotes = await self.list_remotes(user_id)
        selected = next(
            (candidate for candidate in remotes if candidate.casefold() == remote.casefold()),
            None,
        )
        if not selected:
            raise ValueError("Storage remote does not exist in your rclone configuration")
        return selected

    def uses_required_google_drive_scope(self, user_id: int, remote: str) -> bool:
        """Return whether a configured Drive remote uses the app-file scope."""
        config = self.config_path(user_id)
        parser = configparser.RawConfigParser(interpolation=None)
        try:
            with config.open("r", encoding="utf-8") as handle:
                parser.read_file(handle)
            return (
                parser.get(remote, "type").strip() == "drive"
                and parser.get(remote, "scope", fallback="").strip()
                == GOOGLE_DRIVE_RCLONE_SCOPE
            )
        except (configparser.Error, OSError):
            return False

    async def verify_connection(
        self, user_id: int, remote: str, timeout_seconds: float = 30
    ) -> bool:
        """Verify that the user's remote is both configured and accessible."""
        async def check() -> tuple[int, str, str, str]:
            selected = await self.validate_remote(user_id, remote)
            if not self.uses_required_google_drive_scope(user_id, selected):
                LOG.warning(
                    "%s User %s remote %s does not use scope %s.",
                    GOOGLE_DRIVE_RECONNECT_MESSAGE,
                    user_id,
                    selected,
                    GOOGLE_DRIVE_RCLONE_SCOPE,
                )
                return 1, "", GOOGLE_DRIVE_RECONNECT_MESSAGE, selected
            code, output, error = await self._run(
                "rclone",
                "lsd",
                f"{selected}:",
                "--max-depth",
                "1",
                "--config",
                str(self.config_path(user_id)),
            )
            return code, output, error, selected

        try:
            code, _, error, selected = await asyncio.wait_for(
                check(),
                timeout=timeout_seconds,
            )
            if code:
                LOG.warning(
                    "Storage verification failed for user %s remote %s: %s",
                    user_id,
                    selected,
                    error.strip()[:300],
                )
            return code == 0
        except (ValueError, UploadError, asyncio.TimeoutError):
            LOG.warning(
                "Storage verification failed for user %s remote %s",
                user_id,
                remote,
            )
            return False

    async def google_drive_identity(
        self, user_id: int, remote: str
    ) -> tuple[str, str] | None:
        """Recover the account identity for a pre-identity Drive connection."""
        selected = await self.validate_remote(user_id, remote)
        if not await self.verify_connection(user_id, selected):
            return None
        config = self.config_path(user_id)
        parser = configparser.RawConfigParser(interpolation=None)
        try:
            with config.open("r", encoding="utf-8") as handle:
                parser.read_file(handle)
            token_value = parser.get(selected, "token")
            token = json.loads(token_value)
            access_token = str(token.get("access_token") or "")
            if not access_token:
                return None
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    "https://www.googleapis.com/drive/v3/about",
                    params={"fields": "user(permissionId,emailAddress)"},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
            user = response.json().get("user") or {}
            account_id = str(user.get("permissionId") or "").strip()
            email = str(user.get("emailAddress") or "").strip().casefold()
            return (account_id, email) if account_id and email else None
        except (
            configparser.Error,
            json.JSONDecodeError,
            OSError,
            httpx.HTTPError,
            TypeError,
            AttributeError,
        ):
            LOG.warning(
                "Unable to recover Google Drive identity for user %s remote %s",
                user_id,
                selected,
            )
            return None

    async def create_google_drive(
        self,
        user_id: int,
        remote: str,
        client_id: str,
        client_secret: str,
        token: dict[str, object],
    ) -> str:
        config = self.config_path(user_id)
        config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", remote):
            raise ValueError("Invalid storage remote name")
        if remote in await self.list_remotes(user_id):
            raise ValueError("Storage remote already exists")
        code, _, error = await self._run(
            "rclone",
            "config",
            "create",
            remote,
            "drive",
            "scope",
            GOOGLE_DRIVE_RCLONE_SCOPE,
            "client_id",
            client_id,
            "client_secret",
            client_secret,
            "token",
            json.dumps(token, separators=(",", ":")),
            "--config",
            str(config),
            "--non-interactive",
            "--obscure",
        )
        if code:
            raise UploadError(f"Unable to create Google Drive connection: {error[:300]}")
        os.chmod(config.parent, 0o700)
        os.chmod(config, 0o600)
        await self.validate_remote(user_id, remote)
        return remote

    async def disconnect_storage(self, user_id: int, remote: str) -> None:
        config = self.config_path(user_id)
        if not config.is_file():
            return
        try:
            remote = await self.validate_remote(user_id, remote)
        except ValueError:
            return
        code, _, error = await self._run(
            "rclone",
            "config",
            "delete",
            remote,
            "--config",
            str(config),
        )
        if code:
            raise UploadError(f"Unable to remove Google Drive connection: {error[:300]}")
        if not await self.list_remotes(user_id):
            config.unlink(missing_ok=True)

    def build_remote_path(self, job: UploadJob) -> str:
        relative = (
            PurePosixPath(job.selected_root_directory)
            / job.selected_directory
            / job.file_name
        )
        return f"{job.selected_remote}:{relative.as_posix()}"

    async def remote_exists(self, job: UploadJob, remote_path: str) -> bool:
        code, output, _ = await self._run(
            "rclone",
            "lsjson",
            remote_path,
            "--stat",
            "--config",
            str(self.config_path(job.owner_user_id)),
        )
        return code == 0 and bool(output.strip())

    async def resolve_collision(self, job: UploadJob, remote_path: str) -> str:
        if not await self.remote_exists(job, remote_path):
            return remote_path
        if self.settings.remote_collision_policy == "overwrite":
            return remote_path
        if self.settings.remote_collision_policy == "skip":
            raise FileExistsError("A file already exists at the destination")
        prefix, name = remote_path.rsplit("/", 1)
        path = Path(name)
        for index in range(1, 10_000):
            candidate = f"{prefix}/{path.stem}_{index}{path.suffix}"
            if not await self.remote_exists(job, candidate):
                return candidate
        raise UploadError("Unable to choose an unused destination name")

    async def upload_file(
        self, job: UploadJob, callback: ProgressCallback
    ) -> RcloneResult:
        if job.id is None or not job.local_path or not job.remote_path:
            raise ValueError("Incomplete upload job")
        config = self.config_path(job.owner_user_id)
        if not config.is_file():
            raise UploadError("Storage connection is missing")
        args = [
            "rclone",
            "copyto",
            job.local_path,
            job.remote_path,
            "--config",
            str(config),
            "--stats",
            f"{self.settings.rclone_stats_interval_seconds}s",
            "--use-json-log",
            "--log-level",
            "INFO",
            "--stats-log-level",
            "NOTICE",
            "--retries",
            str(self.settings.rclone_retries),
            "--retries-sleep",
            f"{self.settings.rclone_retries_sleep_seconds}s",
            "--low-level-retries",
            str(self.settings.rclone_low_level_retries),
            "--transfers",
            str(self.settings.rclone_transfers),
            "--checkers",
            str(self.settings.rclone_checkers),
            "--drive-chunk-size",
            self.settings.rclone_drive_chunk_size,
        ]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.processes[job.id] = process
        errors: list[str] = []

        async def consume(stream: asyncio.StreamReader | None) -> None:
            if not stream:
                return
            async for raw in stream:
                line = raw.decode(errors="replace").strip()
                progress = parse_rclone_progress(line)
                if progress:
                    await callback(*progress)
                    continue
                try:
                    record = json.loads(line)
                    if str(record.get("level", "")).lower() in {"error", "critical"}:
                        message = str(record.get("msg", "Upload failed"))
                        errors.append(message)
                        LOG.warning("rclone job %s: %s", job.id, message)
                except (json.JSONDecodeError, TypeError):
                    if line and re.search(r"fatal|error|failed", line, re.I):
                        errors.append(line)

        try:
            await asyncio.wait_for(
                asyncio.gather(consume(process.stdout), consume(process.stderr)),
                timeout=self.settings.rclone_upload_timeout_minutes * 60,
            )
            code = await process.wait()
        except asyncio.TimeoutError as exc:
            await self._terminate(process)
            raise UploadError("Cloud upload timed out") from exc
        finally:
            self.processes.pop(job.id, None)

        verified = await self.verify_upload(job)
        if code != 0 and not verified:
            raise UploadError((errors[-1] if errors else "Cloud upload failed")[:500])
        if not verified:
            raise UploadError("Cloud upload could not be verified")
        return RcloneResult(remote_path=job.remote_path, verified=True)

    async def verify_upload(self, job: UploadJob) -> bool:
        if not job.remote_path:
            return False
        code, output, _ = await self._run(
            "rclone",
            "lsjson",
            job.remote_path,
            "--stat",
            "--config",
            str(self.config_path(job.owner_user_id)),
        )
        if code:
            return False
        try:
            return int(json.loads(output).get("Size", -1)) == job.file_size
        except (json.JSONDecodeError, AttributeError, ValueError):
            return False

    async def cancel(self, job_id: int) -> None:
        process = self.processes.get(job_id)
        if process and process.returncode is None:
            await self._terminate(process)

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(self._terminate(process) for process in list(self.processes.values())),
            return_exceptions=True,
        )

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
