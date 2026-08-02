from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .models import JobStatus, UploadJob, utcnow_text
from .utils import default_root_directory


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS users (
    telegram_user_id INTEGER PRIMARY KEY,
    bot_chat_id INTEGER NOT NULL,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    display_name TEXT,
    telegram_connected INTEGER NOT NULL DEFAULT 0,
    storage_connected INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    session_path TEXT,
    rclone_config_path TEXT,
    selected_remote TEXT,
    selected_root_directory TEXT,
    selected_directory TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_connected_at TEXT
);

CREATE TABLE IF NOT EXISTS telegram_login_sessions (
    connection_id TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL,
    temporary_session_path TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    error_code TEXT,
    FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
);

CREATE TABLE IF NOT EXISTS onboarding_tokens (
    token_hash TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
);

CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    web_session_hash TEXT NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
);

CREATE TABLE IF NOT EXISTS storage_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    remote_name TEXT NOT NULL,
    provider_account_id TEXT,
    account_email TEXT,
    config_path TEXT NOT NULL,
    connected INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(telegram_user_id, remote_name),
    FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
);

CREATE TABLE IF NOT EXISTS upload_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    bot_chat_id INTEGER NOT NULL,
    bot_message_id INTEGER NOT NULL,
    internal_chat_id INTEGER,
    internal_message_id INTEGER,
    source_reference TEXT UNIQUE,
    source_message_id INTEGER,
    status_message_id INTEGER,
    file_name TEXT NOT NULL,
    file_size INTEGER NOT NULL DEFAULT 0,
    mime_type TEXT,
    selected_remote TEXT NOT NULL,
    selected_root_directory TEXT NOT NULL,
    selected_directory TEXT NOT NULL,
    local_path TEXT,
    remote_path TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(owner_user_id, bot_chat_id, bot_message_id),
    UNIQUE(internal_chat_id, internal_message_id),
    FOREIGN KEY (owner_user_id) REFERENCES users(telegram_user_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON upload_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_owner ON upload_jobs(owner_user_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_internal ON upload_jobs(internal_chat_id, internal_message_id);
CREATE INDEX IF NOT EXISTS idx_login_expiry ON telegram_login_sessions(expires_at, status);
CREATE INDEX IF NOT EXISTS idx_oauth_expiry ON oauth_states(expires_at, used);
CREATE INDEX IF NOT EXISTS idx_web_expiry ON web_sessions(expires_at, revoked);
CREATE INDEX IF NOT EXISTS idx_onboarding_expiry ON onboarding_tokens(expires_at, used);
"""


class Database:
    """Small serialized aiosqlite repository.

    A single connection avoids surprising lock contention while WAL mode still
    permits external read-only inspection and safe crash recovery.
    """

    def __init__(self, path: Path):
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.executescript(SCHEMA)
        await self._migrate_schema()
        await self.connection.commit()

    async def _migrate_schema(self) -> None:
        """Add new preference snapshots without replacing an existing app.db."""
        user_columns = {
            row["name"]
            for row in await (
                await self._db().execute("PRAGMA table_info(users)")
            ).fetchall()
        }
        if "selected_root_directory" not in user_columns:
            await self._db().execute(
                "ALTER TABLE users ADD COLUMN selected_root_directory TEXT"
            )
        job_columns = {
            row["name"]
            for row in await (
                await self._db().execute("PRAGMA table_info(upload_jobs)")
            ).fetchall()
        }
        if "selected_root_directory" not in job_columns:
            # Existing jobs retain their already-captured legacy UPLOADS root.
            await self._db().execute(
                "ALTER TABLE upload_jobs ADD COLUMN "
                "selected_root_directory TEXT NOT NULL DEFAULT 'UPLOADS'"
            )
        if "source_reference" not in job_columns:
            await self._db().execute(
                "ALTER TABLE upload_jobs ADD COLUMN source_reference TEXT"
            )
            await self._db().execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_jobs_source_reference ON upload_jobs(source_reference)"
            )
        if "source_message_id" not in job_columns:
            await self._db().execute(
                "ALTER TABLE upload_jobs ADD COLUMN source_message_id INTEGER"
            )
        storage_columns = {
            row["name"]
            for row in await (
                await self._db().execute("PRAGMA table_info(storage_connections)")
            ).fetchall()
        }
        if "provider_account_id" not in storage_columns:
            await self._db().execute(
                "ALTER TABLE storage_connections "
                "ADD COLUMN provider_account_id TEXT"
            )
        if "account_email" not in storage_columns:
            await self._db().execute(
                "ALTER TABLE storage_connections ADD COLUMN account_email TEXT"
            )
        await self._db().execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_storage_provider_account
            ON storage_connections(
                telegram_user_id, provider, provider_account_id
            )
            WHERE provider_account_id IS NOT NULL
            """
        )
        await self._db().execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_storage_account_email
            ON storage_connections(
                telegram_user_id, provider, account_email
            )
            WHERE account_email IS NOT NULL
            """
        )

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    def _db(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("database is not connected")
        return self.connection

    async def execute(self, sql: str, values: Iterable[Any] = ()) -> int:
        async with self._write_lock:
            cursor = await self._db().execute(sql, tuple(values))
            await self._db().commit()
            if sql.lstrip().upper().startswith("INSERT"):
                return int(cursor.lastrowid or 0)
            return max(int(cursor.rowcount or 0), 0)

    async def fetchone(
        self, sql: str, values: Iterable[Any] = ()
    ) -> dict[str, Any] | None:
        cursor = await self._db().execute(sql, tuple(values))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def fetchall(
        self, sql: str, values: Iterable[Any] = ()
    ) -> list[dict[str, Any]]:
        cursor = await self._db().execute(sql, tuple(values))
        return [dict(row) for row in await cursor.fetchall()]

    async def upsert_user(
        self, user: object, bot_chat_id: int, default_directory: str = "DOWNLOADS"
    ) -> dict[str, Any]:
        user_id = int(getattr(user, "id"))
        first = getattr(user, "first_name", None)
        last = getattr(user, "last_name", None)
        display = " ".join(part for part in (first, last) if part).strip()
        display = display or getattr(user, "username", None) or str(user_id)
        root = default_root_directory(first, last, user_id)
        now = utcnow_text()
        await self.execute(
            """
            INSERT INTO users (
                telegram_user_id, bot_chat_id, username, first_name, last_name,
                display_name, selected_root_directory, selected_directory,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                bot_chat_id=excluded.bot_chat_id,
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                display_name=excluded.display_name,
                selected_root_directory=COALESCE(
                    users.selected_root_directory,
                    excluded.selected_root_directory
                ),
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                bot_chat_id,
                getattr(user, "username", None),
                first,
                last,
                display,
                root,
                default_directory,
                now,
                now,
            ),
        )
        result = await self.get_user(user_id)
        assert result
        return result

    async def ensure_user(
        self, user_id: int, bot_chat_id: int, default_directory: str
    ) -> None:
        now = utcnow_text()
        await self.execute(
            """
            INSERT INTO users (
                telegram_user_id, bot_chat_id, display_name,
                selected_root_directory, selected_directory, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO NOTHING
            """,
            (
                user_id,
                bot_chat_id,
                str(user_id),
                default_root_directory(None, None, user_id),
                default_directory,
                now,
                now,
            ),
        )

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        return await self.fetchone(
            "SELECT * FROM users WHERE telegram_user_id=?", (user_id,)
        )

    async def connected_users(self) -> list[dict[str, Any]]:
        return await self.fetchall(
            "SELECT * FROM users WHERE telegram_connected=1 AND active=1"
        )

    async def storage_connections(
        self, user_id: int, connected_only: bool = True
    ) -> list[dict[str, Any]]:
        connected = "AND connected=1" if connected_only else ""
        return await self.fetchall(
            f"""
            SELECT
                id, telegram_user_id, provider, remote_name,
                provider_account_id, account_email, connected,
                created_at, updated_at
            FROM storage_connections
            WHERE telegram_user_id=? {connected}
            ORDER BY created_at, id
            """,
            (user_id,),
        )

    async def admin_user_report(self, user_id: int | None = None) -> list[dict[str, Any]]:
        where = "WHERE u.telegram_user_id=?" if user_id is not None else ""
        values: tuple[int, ...] = (user_id,) if user_id is not None else ()
        return await self.fetchall(
            f"""
            SELECT
                u.telegram_user_id,
                u.username,
                u.first_name,
                u.last_name,
                u.display_name,
                u.telegram_connected,
                u.storage_connected,
                u.active,
                u.selected_remote,
                u.selected_root_directory,
                u.selected_directory,
                u.created_at,
                u.updated_at,
                u.last_connected_at,
                COUNT(j.id) AS total_jobs,
                SUM(CASE WHEN j.status='QUEUED' THEN 1 ELSE 0 END) AS queued_jobs,
                SUM(CASE WHEN j.status IN (
                    'RECEIVED', 'FORWARDING', 'DOWNLOADING',
                    'DOWNLOADED', 'UPLOADING'
                ) THEN 1 ELSE 0 END) AS active_jobs,
                SUM(CASE WHEN j.status='COMPLETED' THEN 1 ELSE 0 END) AS completed_jobs,
                SUM(CASE WHEN j.status='FAILED' THEN 1 ELSE 0 END) AS failed_jobs,
                SUM(CASE WHEN j.status='CANCELLED' THEN 1 ELSE 0 END) AS cancelled_jobs
            FROM users AS u
            LEFT JOIN upload_jobs AS j ON j.owner_user_id=u.telegram_user_id
            {where}
            GROUP BY u.telegram_user_id
            ORDER BY u.created_at, u.telegram_user_id
            """,
            values,
        )

    async def admin_active_jobs(self) -> list[dict[str, Any]]:
        return await self.fetchall(
            """
            SELECT
                j.id,
                j.owner_user_id,
                u.display_name,
                u.username,
                j.file_name,
                j.file_size,
                j.status,
                j.selected_remote,
                j.selected_root_directory,
                j.selected_directory,
                j.created_at,
                j.started_at,
                j.updated_at
            FROM upload_jobs AS j
            LEFT JOIN users AS u ON u.telegram_user_id=j.owner_user_id
            WHERE j.status IN (
                'RECEIVED', 'FORWARDING', 'QUEUED', 'DOWNLOADING',
                'DOWNLOADED', 'UPLOADING'
            )
            ORDER BY j.created_at, j.id
            """
        )

    async def admin_job_statistics(self) -> list[dict[str, Any]]:
        return await self.fetchall(
            """
            SELECT status, COUNT(*) AS count
            FROM upload_jobs
            GROUP BY status
            ORDER BY status
            """
        )

    async def admin_recent_failures(self, limit: int = 10) -> list[dict[str, Any]]:
        return await self.fetchall(
            """
            SELECT
                j.id,
                j.owner_user_id,
                u.display_name,
                j.file_name,
                j.error_code,
                j.error_message,
                j.updated_at
            FROM upload_jobs AS j
            LEFT JOIN users AS u ON u.telegram_user_id=j.owner_user_id
            WHERE j.status='FAILED'
            ORDER BY j.updated_at DESC, j.id DESC
            LIMIT ?
            """,
            (limit,),
        )

    async def set_user_fields(self, user_id: int, **fields: Any) -> None:
        allowed = {
            "telegram_connected",
            "storage_connected",
            "active",
            "session_path",
            "rclone_config_path",
            "selected_remote",
            "selected_root_directory",
            "selected_directory",
            "display_name",
            "username",
            "last_connected_at",
        }
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("invalid user field update")
        fields["updated_at"] = utcnow_text()
        columns = ", ".join(f"{name}=?" for name in fields)
        await self.execute(
            f"UPDATE users SET {columns} WHERE telegram_user_id=?",
            (*fields.values(), user_id),
        )

    async def clear_user_access(
        self,
        user_id: int,
        default_directory: str,
        active: bool | None = None,
    ) -> bool:
        """Remove connection state while retaining the user and job history."""
        async with self._write_lock:
            await self._db().execute("BEGIN IMMEDIATE")
            cursor = await self._db().execute(
                "SELECT 1 FROM users WHERE telegram_user_id=?", (user_id,)
            )
            if not await cursor.fetchone():
                await self._db().rollback()
                return False
            await self._db().execute(
                """
                UPDATE upload_jobs
                SET status=?, updated_at=?
                WHERE owner_user_id=? AND status IN (?, ?, ?, ?, ?, ?)
                """,
                (
                    JobStatus.CANCELLED.value,
                    utcnow_text(),
                    user_id,
                    JobStatus.RECEIVED.value,
                    JobStatus.FORWARDING.value,
                    JobStatus.QUEUED.value,
                    JobStatus.DOWNLOADING.value,
                    JobStatus.DOWNLOADED.value,
                    JobStatus.UPLOADING.value,
                ),
            )
            for table in (
                "telegram_login_sessions",
                "onboarding_tokens",
                "web_sessions",
                "oauth_states",
                "storage_connections",
            ):
                await self._db().execute(
                    f"DELETE FROM {table} WHERE telegram_user_id=?", (user_id,)
                )
            await self._db().execute(
                """
                UPDATE users
                SET telegram_connected=0,
                    storage_connected=0,
                    active=CASE WHEN ? IS NULL THEN active ELSE ? END,
                    session_path=NULL,
                    rclone_config_path=NULL,
                    selected_remote=NULL,
                    selected_root_directory=NULL,
                    selected_directory=?,
                    last_connected_at=NULL,
                    updated_at=?
                WHERE telegram_user_id=?
                """,
                (
                    active,
                    int(active) if active is not None else None,
                    default_directory,
                    utcnow_text(),
                    user_id,
                ),
            )
            await self._db().commit()
            return True

    async def create_job(self, job: UploadJob) -> UploadJob:
        job_id = await self.execute(
            """
            INSERT INTO upload_jobs (
                owner_user_id, bot_chat_id, bot_message_id, status_message_id,
                source_reference, source_message_id,
                file_name, file_size, mime_type, selected_remote,
                selected_root_directory, selected_directory, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.owner_user_id,
                job.bot_chat_id,
                job.bot_message_id,
                job.status_message_id,
                job.source_reference,
                job.source_message_id,
                job.file_name,
                job.file_size,
                job.mime_type,
                job.selected_remote,
                job.selected_root_directory,
                job.selected_directory,
                job.status.value,
                job.created_at,
                job.updated_at,
            ),
        )
        job.id = job_id
        return job

    async def get_job(self, job_id: int) -> UploadJob | None:
        row = await self.fetchone("SELECT * FROM upload_jobs WHERE id=?", (job_id,))
        return UploadJob.model_validate(row) if row else None

    async def next_queued_jobs(self, limit: int) -> list[UploadJob]:
        rows = await self.fetchall(
            """
            SELECT * FROM upload_jobs
            WHERE status=?
            ORDER BY created_at, id
            LIMIT ?
            """,
            (JobStatus.QUEUED.value, limit),
        )
        return [UploadJob.model_validate(row) for row in rows]

    async def next_queued_user_jobs(
        self, limit: int, excluded_user_ids: Iterable[int] = ()
    ) -> list[UploadJob]:
        """Return the oldest queued file for each currently available user.

        Results are globally ordered by submission time. Selecting at most one
        row per owner prevents one account from occupying multiple user
        workers while retaining FIFO priority between different users.
        """
        excluded = tuple(int(value) for value in excluded_user_ids)
        exclusion_sql = ""
        values: list[Any] = [JobStatus.QUEUED.value]
        if excluded:
            placeholders = ", ".join("?" for _ in excluded)
            exclusion_sql = f"AND j.owner_user_id NOT IN ({placeholders})"
            values.extend(excluded)
        values.extend((JobStatus.QUEUED.value, limit))
        rows = await self.fetchall(
            f"""
            SELECT j.*
            FROM upload_jobs AS j
            WHERE j.status=?
              {exclusion_sql}
              AND NOT EXISTS (
                  SELECT 1
                  FROM upload_jobs AS earlier
                  WHERE earlier.owner_user_id=j.owner_user_id
                    AND earlier.status=?
                    AND (
                        earlier.created_at < j.created_at
                        OR (
                            earlier.created_at = j.created_at
                            AND earlier.id < j.id
                        )
                    )
              )
            ORDER BY j.created_at, j.id
            LIMIT ?
            """,
            values,
        )
        return [UploadJob.model_validate(row) for row in rows]

    async def user_worker_queue_position(self, user_id: int) -> int | None:
        """Return a user's FIFO position among owners with unfinished work."""
        rows = await self.fetchall(
            """
            SELECT owner_user_id, MIN(created_at) AS first_created, MIN(id) AS first_id
            FROM upload_jobs
            WHERE status IN (
                'RECEIVED', 'FORWARDING', 'QUEUED', 'DOWNLOADING',
                'DOWNLOADED', 'UPLOADING'
            )
            GROUP BY owner_user_id
            ORDER BY first_created, first_id
            """
        )
        for position, row in enumerate(rows, 1):
            if int(row["owner_user_id"]) == int(user_id):
                return position
        return None

    async def update_job(self, job_id: int, **fields: Any) -> None:
        allowed = {
            "internal_chat_id",
            "internal_message_id",
            "source_message_id",
            "status_message_id",
            "local_path",
            "remote_path",
            "status",
            "error_code",
            "error_message",
            "started_at",
            "completed_at",
        }
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("invalid job field update")
        normalized = {
            name: value.value if isinstance(value, JobStatus) else value
            for name, value in fields.items()
        }
        normalized["updated_at"] = utcnow_text()
        columns = ", ".join(f"{name}=?" for name in normalized)
        await self.execute(
            f"UPDATE upload_jobs SET {columns} WHERE id=?",
            (*normalized.values(), job_id),
        )

    async def user_job_counts(self, user_id: int) -> tuple[int, int]:
        queued = await self.fetchone(
            "SELECT COUNT(*) AS count FROM upload_jobs WHERE owner_user_id=? AND status=?",
            (user_id, JobStatus.QUEUED.value),
        )
        active = await self.fetchone(
            """
            SELECT COUNT(*) AS count FROM upload_jobs
            WHERE owner_user_id=? AND status IN (?, ?, ?)
            """,
            (
                user_id,
                JobStatus.DOWNLOADING.value,
                JobStatus.DOWNLOADED.value,
                JobStatus.UPLOADING.value,
            ),
        )
        return int((queued or {})["count"]), int((active or {})["count"])

    async def cancel_user_jobs(self, user_id: int, job_id: int | None = None) -> int:
        where = "owner_user_id=? AND status IN (?, ?, ?, ?)"
        values: list[Any] = [
            user_id,
            JobStatus.RECEIVED.value,
            JobStatus.FORWARDING.value,
            JobStatus.QUEUED.value,
            JobStatus.DOWNLOADING.value,
        ]
        if job_id is not None:
            where += " AND id=?"
            values.append(job_id)
        return await self.execute(
            f"UPDATE upload_jobs SET status=?, updated_at=? WHERE {where}",
            (JobStatus.CANCELLED.value, utcnow_text(), *values),
        )

    async def recover_jobs(self) -> None:
        await self.execute(
            """
            UPDATE upload_jobs
            SET status=?, error_code=NULL, error_message=NULL, updated_at=?
            WHERE status IN (?, ?, ?)
            """,
            (
                JobStatus.QUEUED.value,
                utcnow_text(),
                JobStatus.DOWNLOADING.value,
                JobStatus.DOWNLOADED.value,
                JobStatus.UPLOADING.value,
            ),
        )

    async def cleanup_expired(self) -> None:
        now = utcnow_text()
        await self.execute(
            "UPDATE web_sessions SET revoked=1 WHERE expires_at<=?", (now,)
        )
        await self.execute(
            "UPDATE onboarding_tokens SET used=1 WHERE expires_at<=?", (now,)
        )
        await self.execute(
            "UPDATE oauth_states SET used=1 WHERE expires_at<=?", (now,)
        )
        await self.execute(
            """
            UPDATE telegram_login_sessions SET status='EXPIRED'
            WHERE expires_at<=? AND status NOT IN ('CONNECTED', 'FAILED', 'EXPIRED')
            """,
            (now,),
        )

    async def claim_oauth_state(
        self, state_hash: str, web_session_hash: str
    ) -> dict[str, Any] | None:
        now = utcnow_text()
        async with self._write_lock:
            await self._db().execute("BEGIN IMMEDIATE")
            cursor = await self._db().execute(
                """
                SELECT * FROM oauth_states
                WHERE state_hash=? AND web_session_hash=? AND used=0 AND expires_at>?
                """,
                (state_hash, web_session_hash, now),
            )
            row = await cursor.fetchone()
            if row:
                await self._db().execute(
                    "UPDATE oauth_states SET used=1 WHERE state_hash=?",
                    (state_hash,),
                )
            await self._db().commit()
            return dict(row) if row else None

    async def claim_onboarding_token(
        self, digest: str
    ) -> dict[str, Any] | None:
        now = utcnow_text()
        async with self._write_lock:
            await self._db().execute("BEGIN IMMEDIATE")
            cursor = await self._db().execute(
                """
                SELECT * FROM onboarding_tokens
                WHERE token_hash=? AND used=0 AND expires_at>?
                """,
                (digest, now),
            )
            row = await cursor.fetchone()
            if row:
                await self._db().execute(
                    "UPDATE onboarding_tokens SET used=1 WHERE token_hash=?",
                    (digest,),
                )
            await self._db().commit()
            return dict(row) if row else None
