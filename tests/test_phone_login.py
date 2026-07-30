from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError

from app.telegram_onboarding import TelegramOnboardingManager


class RecordingDatabase:
    def __init__(self):
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.user_fields: list[tuple[int, dict[str, object]]] = []

    async def execute(self, sql, values=()):
        self.executions.append((sql, tuple(values)))
        return 1

    async def set_user_fields(self, user_id, **fields):
        self.user_fields.append((user_id, fields))


class RecordingClients:
    def __init__(self):
        self.stopped: list[int] = []
        self.started: list[int] = []

    async def stop_user(self, user_id):
        self.stopped.append(user_id)

    async def start_user(self, user_id):
        self.started.append(user_id)


class PhoneClient:
    def __init__(self, session_path, user_id=10, code_error=None, needs_2fa=False):
        self.session_path = session_path
        self.user_id = user_id
        self.code_error = code_error
        self.needs_2fa = needs_2fa
        self.phone: str | None = None
        self.sign_in_calls: list[dict[str, object]] = []
        self.disconnected = False

    async def connect(self):
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_path.write_bytes(b"temporary-session")

    async def send_code_request(self, phone):
        self.phone = phone
        return SimpleNamespace(phone_code_hash="phone-code-hash")

    async def sign_in(self, **values):
        self.sign_in_calls.append(values)
        if "code" in values and self.code_error:
            raise self.code_error(request=None)
        if "code" in values and self.needs_2fa:
            raise SessionPasswordNeededError(request=None)
        return SimpleNamespace(id=self.user_id)

    async def get_me(self):
        return SimpleNamespace(
            id=self.user_id,
            first_name="Test",
            last_name="User",
            username="testuser",
        )

    async def disconnect(self):
        self.disconnected = True


def manager_with_client(settings, client_builder):
    database = RecordingDatabase()
    clients = RecordingClients()
    created: list[PhoneClient] = []

    def factory(path, _api_id, _api_hash):
        client = client_builder(path)
        created.append(client)
        return client

    manager = TelegramOnboardingManager(
        settings, database, clients, client_factory=factory
    )
    return manager, database, clients, created


async def test_phone_code_login_promotes_the_matching_telegram_session(settings):
    manager, database, clients, created = manager_with_client(
        settings, lambda path: PhoneClient(path)
    )

    started = await manager.start_phone(10, "+91 98765 43210")

    assert started["status"] == "WAITING_FOR_CODE"
    assert started["loginMethod"] == "phone"
    assert "phoneNumber" not in started
    assert created[0].phone == "+919876543210"
    assert all(
        "+919876543210" not in str(values)
        for _sql, values in database.executions
    )

    result = await manager.submit_code(
        started["connectionId"], 10, "12345"
    )

    assert result["status"] == "CONNECTED"
    assert created[0].sign_in_calls[0] == {
        "phone": "+919876543210",
        "code": "12345",
        "phone_code_hash": "phone-code-hash",
    }
    assert settings.user_session_path(10).read_bytes() == b"temporary-session"
    assert clients.stopped == [10]
    assert clients.started == [10]
    assert database.user_fields[-1][1]["telegram_connected"] == 1


async def test_invalid_phone_code_can_be_retried_without_restarting(settings):
    manager, _database, _clients, _created = manager_with_client(
        settings,
        lambda path: PhoneClient(path, code_error=PhoneCodeInvalidError),
    )
    started = await manager.start_phone(10, "+919876543210")

    result = await manager.submit_code(
        started["connectionId"], 10, "11111"
    )

    assert result["status"] == "WAITING_FOR_CODE"
    assert "Incorrect login code" in result["message"]


async def test_phone_login_continues_through_telegram_two_factor_auth(settings):
    manager, _database, clients, created = manager_with_client(
        settings, lambda path: PhoneClient(path, needs_2fa=True)
    )
    started = await manager.start_phone(10, "+919876543210")

    code_result = await manager.submit_code(
        started["connectionId"], 10, "12345"
    )

    assert code_result["status"] == "TWO_FACTOR_REQUIRED"
    password_result = await manager.submit_2fa(
        started["connectionId"], 10, "telegram-password"
    )
    assert password_result["status"] == "CONNECTED"
    assert created[0].sign_in_calls[-1] == {"password": "telegram-password"}
    assert clients.started == [10]


def test_phone_number_requires_international_format(settings):
    manager, _database, _clients, _created = manager_with_client(
        settings, lambda path: PhoneClient(path)
    )

    with pytest.raises(ValueError, match="international format"):
        manager._normalize_phone("9876543210")
