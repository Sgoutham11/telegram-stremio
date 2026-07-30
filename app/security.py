from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status

from .database import Database


def random_token(bytes_count: int = 32) -> str:
    return secrets.token_urlsafe(bytes_count)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def expires_at(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class RateLimiter:
    """Process-local sliding-window limiter for abuse-sensitive entry points."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(self, action: str, subject: str, limit: int, window: float) -> None:
        now = time.monotonic()
        events = self._events[(action, subject)]
        while events and events[0] <= now - window:
            events.popleft()
        if len(events) >= limit:
            retry_after = max(1, int(window - (now - events[0])) + 1)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Too many requests. Please try again in "
                    f"{retry_after} seconds."
                ),
                headers={"Retry-After": str(retry_after)},
            )
        events.append(now)


async def resolve_web_user(
    request: Request, database: Database
) -> tuple[dict[str, object], str]:
    raw = request.cookies.get("uploader_session")
    if not raw:
        raise HTTPException(status_code=401, detail="Open a fresh /connect link from the bot.")
    digest = token_hash(raw)
    row = await database.fetchone(
        """
        SELECT * FROM web_sessions
        WHERE token_hash=? AND revoked=0 AND expires_at>?
        """,
        (digest, datetime.now(timezone.utc).isoformat()),
    )
    if not row:
        raise HTTPException(status_code=401, detail="Your onboarding session has expired.")
    user = await database.get_user(int(row["telegram_user_id"]))
    if not user or not user["active"]:
        raise HTTPException(status_code=403, detail="This account is inactive.")
    return user, digest
