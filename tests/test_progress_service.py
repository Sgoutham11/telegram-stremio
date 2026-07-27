from app.models import UploadJob
from app.progress_service import ProgressService


class Client:
    def __init__(self):
        self.edits = []

    async def edit_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))


async def test_progress_message_shows_snapshotted_storage():
    client = Client()
    progress = ProgressService(client, interval=5)
    job = UploadJob(
        job_key="1:2",
        chat_id=1,
        message_id=2,
        sender_id=3,
        filename="file.bin",
        file_size=100,
        status_message_id=9,
        rclone_remote="mega",
    )
    await progress.update(job, "Uploading to mega", force=True)
    assert "Storage: mega" in client.edits[0][2]
