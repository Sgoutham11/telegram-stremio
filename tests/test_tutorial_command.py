from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from app.bot_service import BotService
from app.security import RateLimiter
from conftest import telegram_user


class Event:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.replies: list[tuple[str, dict]] = []

    async def reply(self, text: str, **kwargs):
        self.replies.append((text, kwargs))


async def test_tutorial_is_available_before_registration(settings, database):
    bot = BotService(
        SimpleNamespace(),
        settings,
        database,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        RateLimiter(),
    )
    event = Event(20)

    await bot._handle_command(event, telegram_user(20, "New User"), "/tutorial")

    text, kwargs = event.replies[0]
    assert "/connect" in text
    assert "iPhone/iPad" in text
    assert "RS File Manager" in text
    assert "Android TV" in text
    image_path = Path(kwargs["file"])
    assert image_path.name == "tutorial-guide.png"
    assert image_path.is_file()
    with Image.open(image_path) as image:
        assert image.size == (1200, 1600)


def test_tutorial_is_listed_for_all_users():
    assert "/tutorial - show setup and playback guide" in BotService._command_list(
        False
    )
