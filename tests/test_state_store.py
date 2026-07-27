import json

from app.models import JobStatus, UploadJob
from app.state_store import StateStore


async def test_atomic_state_and_duplicate_key(tmp_path):
    store = StateStore(tmp_path)
    job = UploadJob(job_key="1:2", chat_id=1, message_id=2, sender_id=3, filename="a", status=JobStatus.COMPLETED)
    await store.save(job)
    loaded = await store.load_all()
    assert loaded["1:2"].status == JobStatus.COMPLETED
    assert not list(tmp_path.glob("*.tmp"))


async def test_corrupt_state_is_ignored(tmp_path):
    (tmp_path / "bad.json").write_text("{")
    assert await StateStore(tmp_path).load_all() == {}


async def test_old_job_without_upload_directory_defaults_to_downloads(tmp_path):
    payload = {"job_key": "1:2", "chat_id": 1, "message_id": 2, "sender_id": 3, "filename": "old.mkv"}
    (tmp_path / "1_2.json").write_text(json.dumps(payload))
    loaded = await StateStore(tmp_path).load_all()
    assert loaded["1:2"].upload_directory == "DOWNLOADS"
    assert loaded["1:2"].upload_username == ""
    assert loaded["1:2"].rclone_remote == ""
    assert loaded["1:2"].remote_directory == "DOWNLOADS"


async def test_directory_state_is_not_treated_as_job_state(tmp_path):
    (tmp_path / "current_directory.json").write_text(json.dumps({"directory": "Movies"}))
    (tmp_path / "user_directories.json").write_text(json.dumps({"123": {"username": "GOUTHAM", "current_directory": "Movies"}}))
    (tmp_path / "user_remotes.json").write_text(json.dumps({"123": {"selected_remote": "mega"}}))
    assert await StateStore(tmp_path).load_all() == {}
