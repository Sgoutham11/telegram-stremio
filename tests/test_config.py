import pytest
from pydantic import ValidationError

from app.config import Settings


BASE = {"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "hash"}


def test_environment_parsing(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    monkeypatch.setenv("ALLOWED_USER_IDS", "1, 2")
    monkeypatch.setenv("ALLOWED_USER_NAME", "GOUTHAM, GALAXY")
    monkeypatch.setenv("DELETE_LOCAL_AFTER_SUCCESS", "false")
    monkeypatch.setenv("TELEGRAM_DOWNLOAD_CONNECTIONS", "6")
    monkeypatch.setenv("DEFAULT_RCLONE_REMOTE", " gdrive ")
    monkeypatch.setenv("ALLOWED_RCLONE_REMOTES", " gdrive, Mega ")
    settings = Settings(_env_file=None)
    assert settings.allowed_user_ids == [1, 2]
    assert settings.allowed_user_names == ["GOUTHAM", "GALAXY"]
    assert settings.get_allowed_username(2) == "GALAXY"
    assert settings.delete_local_after_success is False
    assert settings.telegram_download_connections == 6
    assert settings.default_upload_directory == "DOWNLOADS"
    assert settings.remote_folder_pattern == ""
    assert settings.default_rclone_remote == "gdrive"
    assert settings.allowed_rclone_remotes == ["gdrive", "Mega"]
    assert settings.resolve_rclone_remote("mega") == "Mega"


def test_legacy_rclone_remote_is_default_and_only_allowed_remote(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    settings = Settings(
        _env_file=None,
        rclone_remote="legacy_drive",
        default_rclone_remote=None,
        allowed_rclone_remotes=[],
    )
    assert settings.default_rclone_remote == "legacy_drive"
    assert settings.rclone_remote == "legacy_drive"
    assert settings.allowed_rclone_remotes == ["legacy_drive"]


def test_default_remote_must_be_allowed(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError, match="must be present"):
        Settings(
            _env_file=None,
            default_rclone_remote="gdrive",
            allowed_rclone_remotes=["mega"],
        )


def test_remote_names_are_case_insensitive_and_duplicates_are_rejected(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    settings = Settings(
        _env_file=None,
        default_rclone_remote="GDRIVE",
        allowed_rclone_remotes=["gdrive", "Mega"],
    )
    assert settings.default_rclone_remote == "gdrive"
    assert settings.resolve_rclone_remote("mEgA") == "Mega"
    with pytest.raises(ValidationError, match="duplicate"):
        Settings(
            _env_file=None,
            default_rclone_remote="gdrive",
            allowed_rclone_remotes=["gdrive", "GDRIVE"],
        )


def test_chat_mode_requires_id(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, watch_mode="chat")


def test_allowed_user_lists_must_have_equal_lengths(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError, match="same number of entries"):
        Settings(_env_file=None, allowed_user_ids=[1, 2], allowed_user_names=["GOUTHAM"])


def test_chat_mode_requires_nonempty_allowlist(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError, match="at least one entry"):
        Settings(_env_file=None, watch_mode="chat", watch_chat_id=-100123)


def test_debug_id_mode_allows_initial_chat_discovery(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None, watch_mode="chat", debug_telegram_ids=True)
    assert settings.watch_chat_id is None
    assert settings.allowed_users == {}
    assert settings.telegram_id_discovery_only is True


def test_debug_id_mode_accepts_empty_chat_id_from_docker_env(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None, watch_mode="chat", watch_chat_id="", debug_telegram_ids=True)
    assert settings.watch_chat_id is None


def test_empty_chat_id_still_fails_when_debugging_is_disabled(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError, match="WATCH_CHAT_ID is required"):
        Settings(
            _env_file=None,
            watch_mode="chat",
            watch_chat_id="",
            debug_telegram_ids=False,
            allowed_user_ids=[1],
            allowed_user_names=["GOUTHAM"],
        )


def test_debugging_with_complete_configuration_is_not_discovery_only(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    settings = Settings(
        _env_file=None,
        watch_mode="chat",
        watch_chat_id=-100123,
        debug_telegram_ids=True,
        allowed_user_ids=[1],
        allowed_user_names=["GOUTHAM"],
    )
    assert settings.telegram_id_discovery_only is False


def test_invalid_drive_chunk_size(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rclone_drive_chunk_size="--bad")


def test_upload_timeout_must_be_positive(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rclone_upload_timeout_minutes=0)


def test_retry_sleep_must_not_be_negative(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rclone_retries_sleep_seconds=-1)


def test_authorization(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None, allowed_user_ids=[9], allowed_user_names=["GOUTHAM"])
    assert settings.is_authorized(9, 9)
    assert not settings.is_authorized(8, 9)
    with pytest.raises(PermissionError):
        settings.get_allowed_username(8)


def test_low_memory_defaults(monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.max_concurrent_jobs == 1
    assert settings.telegram_download_connections == 4
    assert settings.telegram_download_stall_timeout_seconds == 120
    assert settings.rclone_transfers == 1
    assert settings.rclone_checkers == 2
    assert settings.rclone_drive_chunk_size == "64Mi"


def test_runtime_requires_writable_rclone_directory(tmp_path, monkeypatch):
    for key, value in BASE.items(): monkeypatch.setenv(key, value)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "rclone.conf"
    config.write_text("[gdrive]\ntype = drive\n")
    session = tmp_path / "telegram.session"
    session.write_text("session")
    monkeypatch.setattr("app.config.shutil.which", lambda _: "/usr/bin/rclone")
    settings = Settings(
        _env_file=None,
        rclone_config_path=config,
        telegram_session_path=session,
        download_dir=tmp_path / "downloads",
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
        log_file=tmp_path / "logs" / "uploader.log",
    )
    settings.validate_runtime()
    assert not (config_dir / ".rclone-write-test").exists()
