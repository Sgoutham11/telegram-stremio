from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from .cleanup_service import CleanupService
from .command_service import CommandService
from .config import Settings
from .directory_service import DirectoryService
from .file_service import FileService
from .handlers import register_handlers
from .logging_config import configure_logging
from .progress_service import ProgressService
from .queue_manager import QueueManager
from .rclone_service import RcloneService
from .remote_service import RemoteService
from .state_store import StateStore
from .telegram_client import create_client
from .worker import Worker

LOG = logging.getLogger(__name__)


async def write_health(path: Path, client: object, workers: list[asyncio.Task]) -> None:
    while True:
        payload = {"pid": os.getpid(), "telegram_connected": client.is_connected(), "workers_alive": all(not t.done() for t in workers), "queue_manager_alive": True}
        temporary = path.with_suffix(".tmp")
        await asyncio.to_thread(temporary.write_text, json.dumps(payload), "utf-8")
        await asyncio.to_thread(os.replace, temporary, path)
        await asyncio.sleep(10)


async def run() -> None:
    settings = Settings()
    settings.prepare_directories()
    configure_logging(settings)
    settings.validate_runtime()
    state = StateStore(settings.state_dir)
    directories = DirectoryService(settings.state_dir, settings.rclone_base_path, settings.default_upload_directory, settings.allowed_users)
    await directories.load()
    queue = QueueManager(settings.queue_max_size)
    rclone = RcloneService(settings)
    await rclone.validate_configured_remotes()
    remotes = RemoteService(settings)
    await remotes.load()
    client = create_client(settings)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session is expired or unauthorized; run: python -m app.auth")
    me = await client.get_me()
    commands = CommandService(settings, queue, state, directories, remotes)
    register_handlers(client, settings, queue, state, commands, directories, me.id)
    progress = ProgressService(client, settings.progress_update_interval_seconds)
    worker_objects = [Worker(client, settings, queue, state, FileService(settings), rclone, progress) for _ in range(settings.max_concurrent_jobs)]
    worker_tasks = [asyncio.create_task(w.run(), name=f"worker-{i}") for i, w in enumerate(worker_objects)]
    if settings.telegram_id_discovery_only:
        LOG.warning("Telegram ID discovery-only mode is active; uploads and interrupted-job recovery are disabled")
    else:
        for job in await state.recover(settings.retry_interrupted_jobs):
            await queue.add(job)
    cleanup_task = asyncio.create_task(CleanupService(settings, state, queue).run(), name="cleanup")
    health_task = asyncio.create_task(write_health(settings.state_dir / "health.json", client, worker_tasks), name="health")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    LOG.info("Uploader started as Telegram user %s; watch mode=%s", me.id, settings.watch_mode)
    client_task = asyncio.create_task(client.run_until_disconnected())
    await asyncio.wait([asyncio.create_task(stop.wait()), client_task], return_when=asyncio.FIRST_COMPLETED)
    LOG.info("Graceful shutdown started")
    for task in worker_tasks + [cleanup_task, health_task, client_task]:
        task.cancel()
    await asyncio.gather(*worker_tasks, cleanup_task, health_task, client_task, return_exceptions=True)
    await client.disconnect()


def main() -> None:
    try:
        asyncio.run(run())
    except Exception as exc:
        logging.critical("Startup failed: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
