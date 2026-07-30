from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .client_manager import TelegramClientManager
from .config import Settings
from .database import Database
from .oauth_service import GoogleOAuthService
from .rclone_service import RcloneService
from .security import RateLimiter, expires_at, random_token, resolve_web_user, token_hash
from .telegram_onboarding import TelegramOnboardingManager


class TwoFactorRequest(BaseModel):
    connectionId: str = Field(min_length=20, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class TelegramDisconnectRequest(BaseModel):
    action: Literal["local", "revoke"] = "local"


def create_web_app(
    settings: Settings,
    database: Database,
    clients: TelegramClientManager,
    onboarding: TelegramOnboardingManager,
    rclone: RcloneService,
    oauth: GoogleOAuthService,
    limiter: RateLimiter,
) -> FastAPI:
    app = FastAPI(
        title="Telegram Stremio Onboarding",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.middleware("http")
    async def private_cache_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/connect" or request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    async def session(request: Request) -> tuple[dict[str, object], str]:
        user, digest = await resolve_web_user(request, database)
        limiter.check(
            "web",
            f"{user['telegram_user_id']}:{request.url.path}",
            120,
            60,
        )
        return user, digest

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static / "index.html")

    @app.get("/connect")
    async def exchange_onboarding_token(token: str) -> RedirectResponse:
        record = await database.claim_onboarding_token(token_hash(token))
        if not record:
            raise HTTPException(status_code=401, detail="This setup link is invalid or expired.")
        raw_session = random_token()
        now = datetime.now(timezone.utc).isoformat()
        await database.execute(
            """
            INSERT INTO web_sessions (
                token_hash, telegram_user_id, created_at, expires_at, revoked
            ) VALUES (?, ?, ?, ?, 0)
            """,
            (
                token_hash(raw_session),
                int(record["telegram_user_id"]),
                now,
                expires_at(settings.web_session_ttl_seconds),
            ),
        )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "uploader_session",
            raw_session,
            max_age=settings.web_session_ttl_seconds,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/api/status")
    async def status(request: Request) -> dict[str, object]:
        user, _ = await session(request)
        user_id = int(user["telegram_user_id"])
        selected_remote = user["selected_remote"]
        storage_verified = bool(
            user["storage_connected"]
            and selected_remote
            and await rclone.verify_connection(user_id, str(selected_remote))
        )
        return {
            "telegram": {
                "connected": bool(user["telegram_connected"]),
                "displayName": user["display_name"] if user["telegram_connected"] else None,
            },
            "storage": {
                "connected": storage_verified,
                "provider": "Google Drive" if storage_verified else None,
                "remote": selected_remote if storage_verified else None,
            },
            "active": bool(
                user["active"]
                and user["telegram_connected"]
                and storage_verified
            ),
        }

    @app.post("/api/telegram/connect/start")
    async def start_telegram(request: Request) -> dict[str, str]:
        user, _ = await session(request)
        user_id = int(user["telegram_user_id"])
        limiter.check("qr-start", str(user_id), 3, 600)
        try:
            return await onboarding.start(user_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.get("/api/telegram/connect/status/{connection_id}")
    async def telegram_status(
        connection_id: str, request: Request
    ) -> dict[str, str]:
        user, _ = await session(request)
        user_id = int(user["telegram_user_id"])
        limiter.check("qr-status", str(user_id), 90, 300)
        try:
            return await onboarding.status(connection_id, user_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Connection not found") from None

    @app.post("/api/telegram/connect/2fa")
    async def telegram_2fa(
        payload: TwoFactorRequest, request: Request
    ) -> dict[str, str]:
        user, _ = await session(request)
        user_id = int(user["telegram_user_id"])
        limiter.check("2fa", str(user_id), settings.max_telegram_2fa_attempts, 600)
        try:
            return await onboarding.submit_2fa(
                payload.connectionId, user_id, payload.password
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Connection not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.post("/api/telegram/disconnect")
    async def disconnect_telegram(
        payload: TelegramDisconnectRequest, request: Request
    ) -> dict[str, str]:
        user, _ = await session(request)
        user_id = int(user["telegram_user_id"])
        limiter.check("telegram-disconnect", str(user_id), 3, 600)
        client = clients.get_client(user_id)
        if payload.action == "revoke" and client:
            await client.log_out()
        await clients.stop_user(user_id)
        session_path = settings.user_session_path(user_id)
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(session_path) + suffix)
            if not path.exists():
                continue
            if payload.action == "local":
                archived = path.with_name(
                    path.name
                    + ".disconnected-"
                    + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                )
                os.replace(path, archived)
            else:
                path.unlink(missing_ok=True)
        await database.set_user_fields(
            user_id, telegram_connected=0, session_path=None
        )
        return {"status": "DISCONNECTED"}

    @app.get("/api/storage/google/connect")
    async def connect_google(request: Request) -> RedirectResponse:
        user, web_digest = await session(request)
        user_id = int(user["telegram_user_id"])
        limiter.check("oauth-start", str(user_id), 5, 600)
        try:
            url = await oauth.authorization_url(user_id, web_digest)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        return RedirectResponse(url)

    @app.get("/api/storage/google/callback")
    async def google_callback(
        code: str, state: str, request: Request
    ) -> RedirectResponse:
        user, web_digest = await session(request)
        try:
            owner = await oauth.complete(code, state, web_digest)
        except PermissionError:
            raise HTTPException(status_code=403, detail="Invalid OAuth state") from None
        except Exception:
            return RedirectResponse("/?storage=failed", status_code=303)
        if owner != int(user["telegram_user_id"]):
            raise HTTPException(status_code=403, detail="OAuth ownership mismatch")
        return RedirectResponse("/?storage=connected", status_code=303)

    @app.post("/api/storage/google/disconnect")
    async def disconnect_google(request: Request) -> dict[str, str]:
        user, _ = await session(request)
        user_id = int(user["telegram_user_id"])
        limiter.check("storage-disconnect", str(user_id), 3, 600)
        await rclone.disconnect_storage(user_id)
        await database.execute(
            """
            UPDATE storage_connections
            SET connected=0, updated_at=?
            WHERE telegram_user_id=? AND provider='google'
            """,
            (datetime.now(timezone.utc).isoformat(), user_id),
        )
        await database.set_user_fields(
            user_id,
            storage_connected=0,
            rclone_config_path=None,
            selected_remote=None,
        )
        return {"status": "DISCONNECTED"}

    @app.get("/healthz")
    async def health() -> JSONResponse:
        database_ok = database.connection is not None
        return JSONResponse(
            {"status": "ok" if database_ok else "unhealthy"},
            status_code=200 if database_ok else 503,
        )

    return app
