import json

import pytest

from app.config import Settings
from app.remote_service import RemoteService


def settings(tmp_path, allowed=None):
    return Settings(
        _env_file=None,
        telegram_api_id=1,
        telegram_api_hash="x",
        allowed_user_ids=[111, 222],
        allowed_user_names=["GOUTHAM", "GALAXY"],
        default_rclone_remote="gdrive",
        allowed_rclone_remotes=allowed or ["gdrive", "mega"],
        state_dir=tmp_path,
        download_dir=tmp_path,
        log_dir=tmp_path,
        log_file=tmp_path / "x.log",
    )


async def test_default_independent_selection_and_persistence(tmp_path):
    config = settings(tmp_path)
    service = RemoteService(config)
    await service.load()
    assert await service.get_selected_remote(111) == "gdrive"
    assert await service.get_selected_remote(222) == "gdrive"

    assert await service.set_selected_remote(111, "MEGA") == "mega"
    assert await service.get_selected_remote(111) == "mega"
    assert await service.get_selected_remote(222) == "gdrive"

    payload = json.loads((tmp_path / "user_remotes.json").read_text())
    assert payload["111"]["selected_remote"] == "mega"
    assert payload["111"]["updated_at"].endswith("Z")

    restarted = RemoteService(config)
    await restarted.load()
    assert await restarted.get_selected_remote(111) == "mega"


async def test_removed_stored_remote_falls_back_to_default(tmp_path):
    (tmp_path / "user_remotes.json").write_text(
        json.dumps({"111": {"selected_remote": "mega", "updated_at": "now"}})
    )
    service = RemoteService(settings(tmp_path, allowed=["gdrive"]))
    await service.load()
    assert await service.get_selected_remote(111) == "gdrive"


async def test_unavailable_or_corrupt_state_falls_back_without_crashing(tmp_path):
    (tmp_path / "user_remotes.json").write_text("{broken")
    service = RemoteService(settings(tmp_path))
    await service.load()
    assert await service.get_selected_remote(111) == "gdrive"


async def test_unknown_remote_and_user_are_rejected(tmp_path):
    service = RemoteService(settings(tmp_path))
    await service.load()
    with pytest.raises(ValueError):
        await service.set_selected_remote(111, "mega;rm")
    with pytest.raises(PermissionError):
        await service.get_selected_remote(999)
