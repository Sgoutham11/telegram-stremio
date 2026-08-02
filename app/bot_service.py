from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException
from telethon import Button, events

from .client_manager import TelegramClientManager
from .config import Settings
from .database import Database
from .dispatcher import JobDispatcher
from .models import JobStatus, UploadJob
from .rclone_service import RcloneService
from .security import RateLimiter, expires_at, random_token, token_hash
from .utils import (
    default_root_directory,
    fallback_filename,
    format_bytes,
    sanitize_filename,
    validate_root_directory,
)

LOG = logging.getLogger(__name__)


class BotService:
    """Private control-bot handlers; the bot never downloads submitted media."""

    def __init__(
        self,
        bot: Any,
        settings: Settings,
        database: Database,
        clients: TelegramClientManager,
        rclone: RcloneService,
        dispatcher: JobDispatcher,
        limiter: RateLimiter,
        onboarding: Any | None = None,
    ):
        self.bot = bot
        self.settings = settings
        self.database = database
        self.clients = clients
        self.rclone = rclone
        self.dispatcher = dispatcher
        self.limiter = limiter
        self.onboarding = onboarding
        self.accepting = True

    def register(self) -> None:
        @self.bot.on(events.NewMessage(incoming=True))
        async def private_message(event: Any) -> None:
            if not getattr(event, "is_private", False):
                return
            sender = await event.get_sender()
            if not sender or getattr(sender, "bot", False):
                return
            user_id = int(sender.id)
            text = (event.raw_text or "").strip()
            try:
                user = await self.database.get_user(user_id)
                if user and not user["active"] and not self._is_admin(user_id):
                    await event.reply(await self._blocked_message())
                    return
                if text.startswith("/"):
                    await self._handle_command(event, sender, text)
                elif getattr(event.message, "media", None):
                    await self._submit_file(event, sender)
            except HTTPException as exc:
                await event.reply(str(exc.detail))
            except Exception:
                LOG.exception("Private bot handler failed for user %s", user_id)
                await event.reply("The request could not be completed. Please try again.")

    async def _handle_command(self, event: Any, sender: Any, text: str) -> None:
        command, *arguments = text.split(maxsplit=1)
        command = command.split("@", 1)[0].lower()
        argument = arguments[0].strip() if arguments else ""
        user_id = int(sender.id)
        self.limiter.check(f"bot:{command}", str(user_id), 12, 60)

        existing_user = await self.database.get_user(user_id)
        if (
            existing_user
            and not existing_user["active"]
            and not self._is_admin(user_id)
        ):
            await event.reply(await self._blocked_message())
            return

        if command == "/ls":
            await event.reply(self._command_list(self._is_admin(user_id)))
            return

        if command == "/tutorial":
            await self._send_tutorial(event)
            return

        if command == "/start":
            await self.database.upsert_user(
                sender,
                int(event.chat_id),
                self.settings.default_upload_directory,
            )
            user = await self.database.get_user(user_id)
            if user and not user["selected_directory"]:
                await self.database.set_user_fields(
                    user_id, selected_directory=self.settings.default_upload_directory
                )
            await event.reply(
                "Welcome. This private bot routes your files through your own "
                "Telegram account and cloud connection.\n\n"
                "Use /connect to set up or manage your connections."
            )
            return

        user = await self.database.get_user(user_id)
        if not user:
            await event.reply("Send /start before using this command.")
            return
        if command == "/clear":
            target_id = user_id
            if argument:
                if not self._is_admin(user_id):
                    await event.reply(
                        "Only the administrator can clear another user."
                    )
                    return
                if not argument.isdigit() or int(argument) <= 0:
                    await event.reply("Usage: /clear <user-id>")
                    return
                target_id = int(argument)
            if not await self._clear_user_access(target_id):
                await event.reply("User not found.")
                return
            if target_id == user_id:
                await event.reply(
                    "Your Telegram and Google Drive connections were cleared. "
                    "Use /connect when you want to connect again."
                )
            else:
                await event.reply(
                    f"User {target_id} was cleared and may connect again."
                )
            return
        if command in {"/block", "/unblock"}:
            if not self._is_admin(user_id):
                await event.reply(
                    "This command is available only to the administrator."
                )
                return
            if not argument.isdigit() or int(argument) <= 0:
                await event.reply(f"Usage: {command} <user-id>")
                return
            target_id = int(argument)
            if target_id == user_id and command == "/block":
                await event.reply("The administrator account cannot block itself.")
                return
            target = await self.database.get_user(target_id)
            if command == "/block":
                if not target:
                    await self.database.ensure_user(
                        target_id,
                        target_id,
                        self.settings.default_upload_directory,
                    )
                await self._clear_user_access(target_id, active=False)
                await event.reply(
                    f"User {target_id} was cleared and blocked."
                )
            elif not target:
                await event.reply("User not found.")
            else:
                await self.database.set_user_fields(target_id, active=1)
                await event.reply(
                    f"User {target_id} was unblocked and may use /start and "
                    "/connect again."
                )
            return
        selected_root = user["selected_root_directory"] or default_root_directory(
            user["first_name"], user["last_name"], user_id
        )
        if not user["selected_root_directory"]:
            await self.database.set_user_fields(
                user_id, selected_root_directory=selected_root
            )
            user["selected_root_directory"] = selected_root
        if command == "/db":
            if not self._is_admin(user_id):
                await event.reply(
                    "This command is available only to the administrator."
                )
                return
            await self._handle_admin_database_command(event, argument)
        elif command == "/connect":
            self.limiter.check("connect", str(user_id), 5, 300)
            raw = random_token()
            now = datetime.now(timezone.utc).isoformat()
            # Only the newest unused setup link should remain valid. This
            # prevents a user from accumulating several live bearer tokens.
            await self.database.execute(
                """
                UPDATE onboarding_tokens
                SET used=1
                WHERE telegram_user_id=? AND used=0
                """,
                (user_id,),
            )
            await self.database.execute(
                """
                INSERT INTO onboarding_tokens (
                    token_hash, telegram_user_id, created_at, expires_at, used
                ) VALUES (?, ?, ?, ?, 0)
                """,
                (
                    token_hash(raw),
                    user_id,
                    now,
                    expires_at(self.settings.onboarding_token_ttl_seconds),
                ),
            )
            base = self.settings.public_base_url.rstrip("/")
            setup_url = f"{base}/connect?token={raw}"
            connected_note = (
                "Your Telegram and cloud connections are already active. "
                "Use this link to view or manage them.\n\n"
                if user["telegram_connected"] and user["storage_connected"]
                else ""
            )
            host = (urlsplit(setup_url).hostname or "").casefold()
            is_local = host in {"localhost", "127.0.0.1", "::1"}
            buttons = None if is_local else Button.url("Open setup", setup_url)
            local_note = (
                "\n\nTelegram does not activate localhost links. Copy the full "
                "URL and paste it into a browser on the computer running Docker."
                if is_local
                else ""
            )
            await event.reply(
                connected_note
                + "This single-use setup link expires shortly:\n"
                + setup_url
                + local_note,
                buttons=buttons,
                link_preview=False,
            )
        elif command == "/status":
            queued, active = await self.database.user_job_counts(user_id)
            telegram = "Connected" if user["telegram_connected"] else "Not connected"
            storage_verified = bool(
                user["storage_connected"]
                and user["selected_remote"]
                and await self.rclone.verify_connection(
                    user_id, str(user["selected_remote"])
                )
            )
            storage = (
                "Google Drive connected"
                if storage_verified
                else "Not connected"
            )
            await event.reply(
                f"Telegram session: {telegram}\n"
                f"Storage: {storage}\n"
                f"Remote: {user['selected_remote'] or 'N/A'}\n"
                f"Root directory: {selected_root}\n"
                f"Directory: {user['selected_directory'] or self.settings.default_upload_directory}\n"
                f"Queued jobs: {queued}\n"
                f"Active jobs: {active}"
            )
        elif command == "/cancel":
            self.limiter.check("cancel", str(user_id), 10, 60)
            job_id = int(argument) if argument.isdigit() else None
            cancelled = await self.dispatcher.cancel(user_id, job_id)
            await event.reply(
                "Cancellation requested." if cancelled else "No matching active job."
            )
        elif command == "/help":
            await event.reply(self._command_list(self._is_admin(user_id)))
        elif command == "/dirroot":
            if not argument:
                await event.reply(f"Current root directory: {selected_root}")
            elif argument.lower() in {"default", "reset"}:
                selected_root = default_root_directory(
                    user["first_name"], user["last_name"], user_id
                )
                await self.database.set_user_fields(
                    user_id, selected_root_directory=selected_root
                )
                await event.reply(f"Root directory reset to: {selected_root}")
            else:
                try:
                    selected_root = validate_root_directory(argument)
                    await self.database.set_user_fields(
                        user_id, selected_root_directory=selected_root
                    )
                    await event.reply(f"Root directory changed to: {selected_root}")
                except ValueError:
                    await event.reply(
                        "Invalid root directory. Use letters, numbers, spaces, "
                        "hyphens, and underscores."
                    )
        elif command == "/dir":
            if not argument:
                selected_directory = (
                    user["selected_directory"]
                    or self.settings.default_upload_directory
                )
                await event.reply(
                    f"Current directory: {selected_directory}\n"
                    f"Destination: {selected_root}/{selected_directory}"
                )
            else:
                try:
                    normalized = self._safe_directory(argument)
                    await self.database.set_user_fields(
                        user_id, selected_directory=normalized
                    )
                    await event.reply(f"Directory changed to: {normalized}")
                except ValueError:
                    await event.reply(
                        "Invalid directory. Use letters, numbers, spaces, hyphens, "
                        "underscores, and / for nested folders."
                    )
        elif command in {"/remote", "/remotes"}:
            remotes = await self.rclone.list_remotes(user_id)
            connections = await self.database.storage_connections(user_id)
            accounts = {
                row["remote_name"]: row["account_email"] or "unknown account"
                for row in connections
            }
            if command == "/remotes":
                selected = user["selected_remote"] or "N/A"
                rows = "\n".join(
                    f"{index}. {name} — {accounts.get(name, 'external config')}"
                    for index, name in enumerate(remotes, 1)
                )
                await event.reply(
                    f"Available remotes:\n{rows or 'none'}\n\nCurrent remote: {selected}"
                )
            elif not argument:
                await event.reply(
                    f"Current remote: {user['selected_remote'] or 'N/A'}\n"
                    f"Available remotes: {', '.join(remotes) or 'none'}"
                )
            else:
                try:
                    selected = await self.rclone.validate_remote(user_id, argument)
                    await self.database.set_user_fields(
                        user_id, selected_remote=selected
                    )
                    await event.reply(f"Storage changed to: {selected}")
                except ValueError:
                    await event.reply(
                        f"Unknown remote. Available: {', '.join(remotes) or 'none'}"
                    )

    def _is_admin(self, user_id: int) -> bool:
        return (
            self.settings.admin_telegram_user_id is not None
            and user_id == self.settings.admin_telegram_user_id
        )

    @staticmethod
    async def _send_tutorial(event: Any) -> None:
        image_path = Path(__file__).with_name("static") / "tutorial-guide.png"
        text = (
            "Telegram Stremio - quick tutorial\n\n"
            "1. Send /connect, connect your Telegram account, then add Google Drive.\n"
            "2. Optional: choose a Drive with /remote and a folder with /dir.\n"
            "3. Send or forward a file you are authorized to access and wait for "
            "Upload completed.\n\n"
            "Watch from your Google Drive:\n"
            "- iPhone/iPad: VLC > Network > Cloud Services > Google Drive.\n"
            "- Android: RS File Manager > Google Drive > open the file with VLC.\n"
            "- Android TV: install RS File Manager and VLC, open Google Drive in "
            "RS File Manager, then play the file with VLC.\n\n"
            "Use /status to check your connections and jobs."
        )
        if image_path.is_file():
            await event.reply(text, file=str(image_path))
        else:
            LOG.warning("Tutorial image is missing: %s", image_path)
            await event.reply(text)

    @staticmethod
    def _command_list(is_admin: bool) -> str:
        commands = [
            "Commands available to you:",
            "/start - register this private chat",
            "/connect - open or manage secure onboarding",
            "/clear - remove your Telegram and Google Drive connections",
            "/tutorial - show setup and playback guide",
            "/status - show your connections and jobs",
            "/cancel [job-id] - cancel your job",
            "/dirroot [name|default] - show or select your root directory",
            "/dir [directory] - show or select your upload directory",
            "/remote [name] - show or select your storage remote",
            "/remotes - list configured storage remotes",
            "/ls - list commands available to you",
            "/help - show this command list",
        ]
        if is_admin:
            commands.extend(
                [
                    "",
                    "Administrator commands:",
                    "/db user or /db users - show all user details and job totals",
                    "/db user <user-id> - show one user's details",
                    "/db activeworks - show queued and processing jobs",
                    "/db stats - show user and job totals by status",
                    "/db failed [limit] - show recent failed jobs (default 10)",
                    "/clear <user-id> - clear a user's connections without blocking",
                    "/block <user-id> - clear and block a user",
                    "/unblock <user-id> - allow a blocked user to connect again",
                ]
            )
        return "\n".join(commands)

    async def _blocked_message(self) -> str:
        contact = self.settings.admin_contact.strip()
        if not contact and self.settings.admin_telegram_user_id is not None:
            admin = await self.database.get_user(
                self.settings.admin_telegram_user_id
            )
            if admin and admin["username"]:
                contact = f"@{admin['username']}"
            else:
                contact = str(self.settings.admin_telegram_user_id)
        if contact:
            return f"You are blocked. Contact admin {contact} for more details."
        return "You are blocked. Contact the administrator for more details."

    async def _clear_user_access(
        self, user_id: int, active: bool | None = None
    ) -> bool:
        if not await self.database.get_user(user_id):
            return False
        pending = await self.database.fetchall(
            """
            SELECT temporary_session_path FROM telegram_login_sessions
            WHERE telegram_user_id=?
            """,
            (user_id,),
        )
        await self.dispatcher.cancel_all(user_id)
        if self.onboarding is not None:
            await self.onboarding.clear_user(user_id)
        await self.clients.stop_user(user_id)
        shutil.rmtree(self.settings.user_data_dir(user_id), ignore_errors=True)
        shutil.rmtree(
            self.settings.user_rclone_config(user_id).parent, ignore_errors=True
        )
        pending_root = self.settings.pending_telegram_root.resolve()
        for row in pending:
            path = Path(row["temporary_session_path"]).parent
            try:
                resolved = path.resolve()
                if resolved.is_relative_to(pending_root):
                    shutil.rmtree(resolved, ignore_errors=True)
            except OSError:
                LOG.warning(
                    "Unable to clear pending Telegram path for user %s", user_id
                )
        return await self.database.clear_user_access(
            user_id,
            self.settings.default_upload_directory,
            active=active,
        )

    async def _handle_admin_database_command(
        self, event: Any, argument: str
    ) -> None:
        parts = argument.split()
        subcommand = parts[0].lower() if parts else ""

        if subcommand in {"user", "users"}:
            user_id: int | None = None
            if len(parts) > 1:
                if subcommand != "user" or not parts[1].isdigit():
                    await event.reply("Usage: /db users or /db user <user-id>")
                    return
                user_id = int(parts[1])
            rows = await self.database.admin_user_report(user_id)
            if not rows:
                await event.reply("No matching users found.")
                return
            blocks = [self._format_admin_user(row) for row in rows]
            await self._reply_chunks(event, "User database", blocks)
            return

        if subcommand == "activeworks":
            rows = await self.database.admin_active_jobs()
            if not rows:
                await event.reply("No queued or active work.")
                return
            blocks = [self._format_admin_job(row) for row in rows]
            await self._reply_chunks(event, "Queued and active work", blocks)
            return

        if subcommand == "stats":
            users = await self.database.fetchone(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN telegram_connected=1 THEN 1 ELSE 0 END)
                        AS telegram_connected,
                    SUM(CASE WHEN storage_connected=1 THEN 1 ELSE 0 END)
                        AS storage_connected
                FROM users
                """
            )
            statuses = await self.database.admin_job_statistics()
            user_stats = users or {}
            lines = [
                "Database statistics",
                "",
                f"Users: {int(user_stats.get('total') or 0)}",
                f"Enabled users: {int(user_stats.get('active') or 0)}",
                "Telegram connected: "
                f"{int(user_stats.get('telegram_connected') or 0)}",
                "Storage connected: "
                f"{int(user_stats.get('storage_connected') or 0)}",
                "",
                "Jobs:",
            ]
            lines.extend(
                f"{row['status']}: {row['count']}" for row in statuses
            )
            if not statuses:
                lines.append("none")
            await event.reply("\n".join(lines))
            return

        if subcommand == "failed":
            if len(parts) > 1 and not parts[1].isdigit():
                await event.reply("Usage: /db failed [limit]")
                return
            limit = min(max(int(parts[1]), 1), 50) if len(parts) > 1 else 10
            rows = await self.database.admin_recent_failures(limit)
            if not rows:
                await event.reply("No failed jobs.")
                return
            blocks = [
                (
                    f"Job {row['id']} | User {row['owner_user_id']} "
                    f"({row['display_name'] or 'N/A'})\n"
                    f"File: {row['file_name']}\n"
                    f"Error: {row['error_code'] or 'N/A'} - "
                    f"{row['error_message'] or 'N/A'}\n"
                    f"Updated: {row['updated_at']}"
                )
                for row in rows
            ]
            await self._reply_chunks(event, "Recent failed jobs", blocks)
            return

        await event.reply(
            "Admin database commands:\n"
            "/db user (or /db users)\n"
            "/db user <user-id>\n"
            "/db activeworks\n"
            "/db stats\n"
            "/db failed [limit]"
        )

    @staticmethod
    def _format_admin_user(row: dict[str, Any]) -> str:
        username = f"@{row['username']}" if row["username"] else "N/A"
        return (
            f"User {row['telegram_user_id']} | {row['display_name'] or 'N/A'}\n"
            f"Username: {username}\n"
            f"Name: {row['first_name'] or 'N/A'} {row['last_name'] or ''}".rstrip()
            + "\n"
            f"Enabled: {'yes' if row['active'] else 'no'}\n"
            f"Blocked: {'no' if row['active'] else 'yes'}\n"
            f"Telegram: {'connected' if row['telegram_connected'] else 'not connected'}\n"
            f"Storage: {'connected' if row['storage_connected'] else 'not connected'}\n"
            f"Remote: {row['selected_remote'] or 'N/A'}\n"
            f"Destination: {row['selected_root_directory'] or 'N/A'}/"
            f"{row['selected_directory'] or 'N/A'}\n"
            f"Jobs: total={row['total_jobs']} queued={row['queued_jobs'] or 0} "
            f"active={row['active_jobs'] or 0} "
            f"completed={row['completed_jobs'] or 0} "
            f"failed={row['failed_jobs'] or 0} "
            f"cancelled={row['cancelled_jobs'] or 0}\n"
            f"Created: {row['created_at']}\n"
            f"Updated: {row['updated_at']}\n"
            f"Last connected: {row['last_connected_at'] or 'N/A'}"
        )

    @staticmethod
    def _format_admin_job(row: dict[str, Any]) -> str:
        username = f"@{row['username']}" if row["username"] else "N/A"
        return (
            f"Job {row['id']} | {row['status']}\n"
            f"User: {row['owner_user_id']} | "
            f"{row['display_name'] or 'N/A'} | {username}\n"
            f"File: {row['file_name']} ({format_bytes(row['file_size'])})\n"
            f"Destination: {row['selected_remote']}:"
            f"{row['selected_root_directory']}/{row['selected_directory']}\n"
            f"Created: {row['created_at']}\n"
            f"Started: {row['started_at'] or 'N/A'}\n"
            f"Updated: {row['updated_at']}"
        )

    @staticmethod
    async def _reply_chunks(
        event: Any, heading: str, blocks: list[str], limit: int = 3800
    ) -> None:
        chunk = heading
        for block in blocks:
            candidate = f"{chunk}\n\n{block}"
            if len(candidate) > limit and chunk != heading:
                await event.reply(chunk)
                chunk = f"{heading} (continued)\n\n{block}"
            else:
                chunk = candidate
        await event.reply(chunk)

    async def _submit_file(self, event: Any, sender: Any) -> None:
        user_id = int(sender.id)
        self.limiter.check("file", str(user_id), 10, 60)
        if not self.accepting:
            await event.reply("The uploader is shutting down. Try again shortly.")
            return
        user = await self.database.get_user(user_id)
        if not user:
            await event.reply("Send /start before submitting a file.")
            return
        if not user["telegram_connected"] or not self.clients.get_client(user_id):
            await event.reply("Connect your Telegram account with /connect first.")
            return
        if not user["storage_connected"]:
            await event.reply("Connect Google Drive with /connect first.")
            return
        selected_root = user["selected_root_directory"] or default_root_directory(
            user["first_name"], user["last_name"], user_id
        )
        if not user["selected_root_directory"]:
            await self.database.set_user_fields(
                user_id, selected_root_directory=selected_root
            )
        remotes = await self.rclone.list_remotes(user_id)
        selected_remote = user["selected_remote"]
        if not selected_remote or selected_remote not in remotes:
            selected_remote = next(
                (
                    name
                    for name in remotes
                    if name.casefold() == self.settings.default_rclone_remote.casefold()
                ),
                remotes[0] if remotes else None,
            )
        if not selected_remote:
            await event.reply("Your storage configuration has no usable remotes.")
            return
        if not await self.rclone.verify_connection(user_id, selected_remote):
            await event.reply(
                "Google Drive permissions have changed. Please disconnect and "
                "reconnect Google Drive."
            )
            return
        file = getattr(event.message, "file", None)
        if not file:
            await event.reply("This message does not contain downloadable media.")
            return
        size = int(getattr(file, "size", 0) or 0)
        if self.settings.max_file_size_bytes and size > self.settings.max_file_size_bytes:
            await event.reply("This file exceeds the configured application limit.")
            return
        filename = sanitize_filename(
            getattr(file, "name", None)
            or fallback_filename(
                event.message.id,
                event.message.media.__class__.__name__.lower(),
                event.message.date,
                getattr(file, "mime_type", None),
            )
        )
        source_reference = "JOB-" + random_token(12)
        job = UploadJob(
            owner_user_id=user_id,
            bot_chat_id=int(event.chat_id),
            bot_message_id=int(event.message.id),
            source_reference=source_reference,
            file_name=filename,
            file_size=size,
            mime_type=getattr(file, "mime_type", None),
            selected_remote=selected_remote,
            selected_root_directory=selected_root,
            selected_directory=user["selected_directory"]
            or self.settings.default_upload_directory,
        )
        try:
            job = await self.database.create_job(job)
        except sqlite3.IntegrityError:
            await event.reply("This message was already submitted.")
            return
        assert job.id is not None
        try:
            worker_position = await self.database.user_worker_queue_position(user_id)
            worker_notice = ""
            if (
                worker_position is not None
                and worker_position > self.settings.max_concurrent_user_workers
            ):
                workers = self.settings.max_concurrent_user_workers
                noun = "worker is" if workers == 1 else "workers are"
                worker_notice = (
                    f"\n\n{workers} {noun} busy. Your work is queued in "
                    "submission-time order."
                )
            status = await event.reply(
                "Queued\n\n"
                f"Job: {job.id}\n"
                f"File: {filename}\n"
                f"Size: {format_bytes(size)}\n"
                f"Storage: {selected_remote}\n"
                f"Directory: {job.selected_root_directory}/{job.selected_directory}"
                f"{worker_notice}\n\n"
                f"Reference: {source_reference}"
            )
            job.status_message_id = int(status.id)
            await self.database.update_job(
                job.id,
                status_message_id=job.status_message_id,
                status=JobStatus.QUEUED,
            )
        except Exception:
            LOG.exception("Unable to create private status reply for job %s", job.id)
            await self.database.update_job(
                job.id,
                status=JobStatus.FAILED,
                error_code="PRIVATE_REPLY_FAILED",
                error_message="The bot could not create the private job reply",
            )
            await event.reply("File submission failed. Please try again.")

    @staticmethod
    def _safe_directory(value: str) -> str:
        parts = [part.strip() for part in value.split("/")]
        if (
            not parts
            or len(value) > 500
            or len(parts) > 10
            or any(
                part in {"", ".", ".."}
                or not re.fullmatch(r"[A-Za-z0-9 _-]{1,100}", part)
                for part in parts
            )
        ):
            raise ValueError("invalid directory")
        return "/".join(parts)
