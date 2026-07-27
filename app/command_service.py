from __future__ import annotations

import shutil

from .config import Settings
from .directory_service import DirectoryService
from .models import JobStatus
from .queue_manager import QueueManager
from .remote_service import RemoteService
from .state_store import StateStore
from .utils import format_bytes, format_duration


class CommandService:
    def __init__(self, settings: Settings, queue: QueueManager, state: StateStore, directories: DirectoryService, remotes: RemoteService):
        self.settings, self.queue, self.state, self.directories, self.remotes = settings, queue, state, directories, remotes

    async def handle(self, event: object) -> None:
        text = event.raw_text.strip()
        command, *args = text.split(maxsplit=1)
        user_id = event.sender_id or 0
        if command == ".help":
            response = ".status - active transfer\n.queue - pending jobs\n.dir <name> - Set your upload directory (nested paths supported)\n.dir - Show your current upload directory\n.dir default - Reset your directory to the default\n.remote - Show your selected storage\n.remote <storage-name> - Select storage for future uploads\n.remotes - List available storage remotes\n.cancel [message_id] - cancel a job\n.retry <message_id> - retry failed job\n.config - safe configuration\n.help - this help"
        elif command == ".dir":
            current = await self.directories.get_user_current_directory(user_id)
            destination = self.directories.build_destination_directory(user_id, current)
            if not args:
                response = f"Current upload directory: {current}\nDestination: {destination}"
            elif args[0].strip().lower() in {"default", "reset"}:
                current = await self.directories.reset_user_current_directory(user_id)
                response = f"Upload directory reset\n\nCurrent directory: {current}\nDestination: {self.directories.build_destination_directory(user_id, current)}"
            else:
                try:
                    current = await self.directories.set_user_current_directory(user_id, args[0])
                    response = f"Upload directory changed\n\nCurrent directory: {current}\nDestination: {self.directories.build_destination_directory(user_id, current)}"
                except ValueError:
                    response = "Invalid directory name.\nUse letters, numbers, spaces, hyphens, and underscores, with / between nested folders."
        elif command == ".remote":
            current_remote = await self.remotes.get_selected_remote(user_id)
            available = ", ".join(self.remotes.list_allowed_remotes())
            if not args:
                response = (
                    f"Current storage: {current_remote}\n"
                    f"Available storage: {available}\n\n"
                    "Usage:\n.remote <storage-name>"
                )
            else:
                requested = args[0].strip()
                try:
                    selected = await self.remotes.set_selected_remote(user_id, requested)
                    response = f"Storage changed to: {selected}"
                except ValueError:
                    response = f"Unknown storage: {requested}\n\nAvailable storage:\n{available}"
        elif command == ".remotes":
            current_remote = await self.remotes.get_selected_remote(user_id)
            rows = "\n".join(
                f"{index}. {remote}"
                for index, remote in enumerate(self.remotes.list_allowed_remotes(), 1)
            )
            response = f"Available storage:\n{rows}\n\nCurrent storage: {current_remote}"
        elif command == ".status":
            active = next(iter(self.queue.active.values()), None)
            free = shutil.disk_usage(self.settings.download_dir).free
            current = await self.directories.get_user_current_directory(user_id)
            selected_remote = await self.remotes.get_selected_remote(user_id)
            current_line = f"Your current directory: {current}"
            active_destination = self.directories.build_snapshot_destination_directory(active.upload_username, active.upload_directory) if active else ""
            active_remote = (
                active.rclone_remote or self.settings.default_rclone_remote
                if active
                else selected_remote
            )
            response = f"Active: {active.filename}\nPhase: {active.status}\nStorage: {active_remote}\nProgress: {active.progress_percent:.1f}%\nSpeed: {format_bytes(active.speed_bytes_per_second)}/s\nETA: {format_duration(active.eta_seconds)}\n{current_line}\nActive job directory: {active.upload_directory}\nDestination: {active.remote_path or active_remote + ':' + active_destination + '/' + active.filename}\nQueue: {self.queue.queue.qsize()}\nDisk free: {format_bytes(free)}" if active else f"Active: none\n{current_line}\nStorage: {selected_remote}\nDestination: {selected_remote}:{self.directories.build_destination_directory(user_id, current)}\nQueue: {self.queue.queue.qsize()}\nDisk free: {format_bytes(free)}"
        elif command == ".queue":
            rows = [f"{i}. {j.filename}\n   Size: {format_bytes(j.file_size)}\n   Storage: {j.rclone_remote or self.settings.default_rclone_remote}\n   Directory: {j.upload_directory}\n   Message ID: {j.message_id}" for i, j in enumerate(self.queue.snapshot(), 1)]
            response = "Pending jobs:\n" + ("\n".join(rows) if rows else "none")
        elif command == ".cancel":
            target = args[0] if args else None
            jobs = list(self.queue.active.values()) + self.queue.snapshot()
            job = next((j for j in jobs if target is None or str(j.message_id) == target), None)
            response = "Cancellation requested." if job and self.queue.request_cancel(job.job_key) else "Job not found."
        elif command == ".retry" and args:
            jobs = await self.state.load_all()
            job = next((j for j in jobs.values() if str(j.message_id) == args[0] and j.status in {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.RECOVERABLE}), None)
            if job:
                job.status, job.error_message = JobStatus.QUEUED, None
                await self.state.save(job)
                await self.queue.add(job)
                response = "Job queued for retry."
            else:
                response = "Retryable job not found."
        elif command == ".config":
            current = await self.directories.get_user_current_directory(user_id)
            selected_remote = await self.remotes.get_selected_remote(user_id)
            response = f"Configuration:\nRoot path: {self.settings.rclone_base_path}\nConfigured username: {self.directories.get_allowed_username(user_id)}\nDefault directory: {self.settings.default_upload_directory}\nYour current directory: {current}\nCurrent storage: {selected_remote}\nDefault storage: {self.settings.default_rclone_remote}\nAvailable storage: {', '.join(self.remotes.list_allowed_remotes())}\nCollision policy: {self.settings.remote_collision_policy}"
        else:
            return
        await event.reply(response[:4000])
