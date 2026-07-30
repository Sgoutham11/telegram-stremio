# Telegram Cloud Uploader

A production-oriented, Dockerized Python 3.12 service in which users submit
files privately to a Telegram bot, connect their own Telegram account and
Google Drive, and upload into isolated per-user destinations. The service uses
Telethon/MTProto for media transfer and rclone for verified cloud uploads.

## Architecture

`Telegram bot -> SQLite FIFO queue -> owner Telethon session -> download worker -> user's selected rclone remote -> remote verification`. Each Telegram user has an isolated session, download directory, storage configuration, and persistent destination preferences. The onboarding web service is bound to host loopback and is intended to be exposed through the existing HTTPS reverse proxy.

## Prerequisites

- Docker Engine with Compose v2
- A Telegram API ID/hash from [my.telegram.org](https://my.telegram.org)
- Google OAuth client credentials with the Drive API enabled
- Enough local space for the largest file plus `MIN_FREE_DISK_GB`

## Configure

```bash
cp .env.example .env
mkdir -p data/users data/pending-telegram data/logs config/users
```

PowerShell:

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force data/users,data/pending-telegram,data/logs,config/users
```

Put the API credentials from my.telegram.org in `.env`, set `WATCH_MODE=chat`, and set `WATCH_CHAT_ID` to the private group's numeric ID (commonly `-100...`). Configure trusted Telegram IDs and their cloud directory names as ordered lists:

```env
WATCH_MODE=chat
WATCH_CHAT_ID=-1001234567890
ALLOWED_USER_IDS=111111111,222222222
ALLOWED_USER_NAME=GOUTHAM,GALAXY
```

The lists map by position: `111111111 -> GOUTHAM` and `222222222 -> GALAXY`. They must have equal, non-zero lengths; IDs and names must be unique. Startup fails on an invalid mapping. Messages from other chats are silently ignored; unknown users in the watched group are directed to `@sgoutham11`, but their content is not processed. Obtain IDs from trusted tooling or Telegram logs; never give an untrusted bot sensitive forwarded content.

### Discover Telegram IDs during setup

If the private group ID or user IDs are not yet known, temporarily enable ID debugging. Debug mode permits startup without `WATCH_CHAT_ID` or an allowlist. While either is missing, discovery-only mode disables new uploads and interrupted-job recovery; only identifier logging remains active.

```env
WATCH_MODE=chat
WATCH_CHAT_ID=
DEBUG_TELEGRAM_IDS=true
ALLOWED_USER_IDS=
ALLOWED_USER_NAME=
```

Start the service and send one message in the desired private group:

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate
docker compose -f docker-compose.prod.yml logs -f telegram-uploader
```

The log block reports the chat ID for `WATCH_CHAT_ID`, sender ID for `ALLOWED_USER_IDS`, sender display name, username, chat type, and message type. After collecting every trusted user's ID, configure the two ordered allowlist variables, set `DEBUG_TELEGRAM_IDS=false`, and restart. Debugging logs setup metadata and message text from every received Telegram message before filtering; keep it disabled outside this short setup window.

Messages from users who are not listed in `ALLOWED_USER_IDS` are not processed. In the watched group, the service tells them to DM `@sgoutham11`. The service never sends this notice in unrelated chats or while Telegram ID discovery-only mode is active.

## Configure Google Drive storage

Set the Google OAuth client credentials and callback URL in `.env`. Users add
their own Google Drives from the private `/connect` setup page; the service
creates and maintains a separate rclone configuration for every Telegram user.

```env
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret
GOOGLE_REDIRECT_URI=https://playbuddy.zapto.org/uploader/api/storage/google/callback
DEFAULT_RCLONE_REMOTE=gdrive
MULTY_RCLONE_COUNT=2
```

`MULTY_RCLONE_COUNT` is the maximum number of Google Drive connections each
Telegram user may add. It defaults to `2` and accepts values from `1` through
`10`. Every slot must use a different verified Google account. The Google
account chooser is shown for every new connection, and the server rejects an
account already connected by that Telegram user.

The first Drive is named `gdrive`, the second `gdrive_02`, then `gdrive_03`,
and so on. A newly connected Drive becomes the selected destination. Tokens
and rclone configuration remain private under
`/config/users/<telegram-user-id>/rclone.conf`.

Connections created by an older application version are upgraded
automatically: the service reads the authenticated user's ID and email from
Google Drive before opening the next account chooser. The existing Drive does
not need to be disconnected.

## Local development

The default [docker-compose.yml](docker-compose.yml) builds the current checkout and is intended for local development:

```bash
cp .env.example .env
docker compose run --rm telegram-uploader python -m app.auth
docker compose up --build
docker compose logs -f telegram-uploader
```

Authentication requests the Telegram code and, if enabled, the 2FA password. Normal startup is non-interactive and fails with instructions if `/data/session/telegram.session` is absent. On bind-mounted Linux folders, ensure UID 10001 can write to `data/`.

## Prebuilt Docker image

Users who do not want to build the image can download a prebuilt archive from the repository's **Releases** page. Choose `telegram-uploader-linux-amd64.tar.gz` for normal Intel/AMD servers or `telegram-uploader-linux-arm64.tar.gz` for ARM64 servers such as Oracle Ampere. Check a Linux server with `uname -m`: `x86_64` means `amd64`, while `aarch64` or `arm64` means `arm64`.

Download the matching image plus `docker-compose.prod.yml` and `env.example` from one release, then run:

```bash
mkdir -p ~/telegram-uploader/{data/users,data/pending-telegram,data/logs,config/users}
cd ~/telegram-uploader
mv env.example .env
# Edit .env before continuing.
docker load -i telegram-uploader-linux-amd64.tar.gz  # Use the arm64 file on ARM64.
```

A first-time installation starts the service and then completes each user's
Telegram and Google Drive connection through `/connect`:

```bash
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f telegram-uploader
```

For an update, preserve `.env`, `data/`, and `config/users/`; download and load the new image archive, replace `docker-compose.prod.yml`, and run `docker compose -f docker-compose.prod.yml up -d --force-recreate`.

To generate the standard Docker archive locally:

```bash
docker build --pull -t telegram-uploader:latest .
docker save --output telegram-uploader.tar telegram-uploader:latest
# Optional on Linux, WSL, or Git Bash:
gzip -9 telegram-uploader.tar
```

Both `.tar` and `.tar.gz` are valid inputs to `docker load`; `.tar.gz` is preferred for downloading because it is smaller. Do not commit either archive to normal Git history. To publish downloadable images, open **Actions -> Publish Prebuilt Docker Images -> Run workflow**, enter a version such as `v1.0.0`, and run it. The workflow builds both supported architectures and attaches the compressed images, production Compose file, environment template, and checksums to a GitHub Release.

## Production deployment with GitHub Actions

Production uses [docker-compose.prod.yml](docker-compose.prod.yml). Commits to `main` that change application or deployment files run the tests, build `telegram-uploader:latest` on the GitHub runner, copy the saved image and production Compose file to Oracle, and recreate the service over SSH. The workflow can also be started manually from the GitHub Actions page.

Prepare the Oracle server once:

```bash
mkdir -p ~/telegram-uploader/{data/users,data/pending-telegram,data/logs,config/users}
cd ~/telegram-uploader
# Create the private runtime files once. GitHub Actions does not replace them:
cp .env.example .env
# Users create their Telegram and Drive connections through /connect.
```

The server only needs the production Compose file and these persistent private assets:

- `.env`
- `data/app.db`
- per-user Telegram sessions and downloads under `data/users/`
- per-user rclone configurations under `config/users/`
- the mounted `data/` directories for pending logins and logs

Configure these GitHub repository settings under **Settings -> Secrets and variables -> Actions**:

- Secret `SERVER_HOST`: Oracle hostname or IP address
- Secret `SSH_PRIVATE_KEY`: private key text used to connect to Oracle
- Variable `SERVER_USER`: Oracle SSH user, such as `ubuntu`

The workflow deploys only inside `~/telegram-uploader`. It replaces `telegram-uploader.tar` and `docker-compose.prod.yml`, loads the image, recreates the container, waits for it to become healthy, and removes the transferred archive. The server's `.env`, SQLite database, per-user Telegram sessions, per-user rclone configurations, downloads, and logs remain in their bind-mounted paths. Restrict the deploy key to this server and repository workflow.

### Sharing an existing HTTPS domain

The onboarding UI can run below a path prefix while another application keeps
the domain root. For example:

```text
https://playbuddy.zapto.org/           -> existing application
https://playbuddy.zapto.org/uploader/  -> Telegram uploader
```

Set both public URLs with the same prefix:

```env
PUBLIC_BASE_URL=https://playbuddy.zapto.org/uploader
GOOGLE_REDIRECT_URI=https://playbuddy.zapto.org/uploader/api/storage/google/callback
```

Add the locations from `deploy/nginx/uploader-path.conf.example` inside the
existing HTTPS `server` block. Keep the trailing slash in
`proxy_pass http://127.0.0.1:8080/;` so Nginx removes `/uploader` before
forwarding to FastAPI. Register the complete prefixed callback URL in Google
Cloud Console. Redirects, cookies, frontend assets, and API requests remain
under `/uploader/`; the existing application continues to own `/`.

For a manual production update when troubleshooting:

```bash
cd ~/telegram-uploader
# Copy telegram-uploader.tar and docker-compose.prod.yml into this directory first.
docker load -i telegram-uploader.tar
docker compose -f docker-compose.prod.yml up -d --force-recreate --remove-orphans
```

## Per-user root and upload directories

Every user has an independent root and child directory. The default root is
their Telegram first name plus last name, converted to uppercase and sanitised.
The default child directory remains `DOWNLOADS`.

```text
Telegram name: Goutham S
Forward file
-> GOUTHAMS/DOWNLOADS/file.mkv
```

Use `/dirroot` privately with the bot to show or override the root:

```text
/dirroot GOUTHAM
/dir Movies
Forward file
-> GOUTHAM/Movies/file.mkv
```

Use `/dirroot default` to return to the sanitised Telegram-name root. Both
preferences persist in SQLite and are captured when a job is accepted, so
later changes affect only future jobs. `RCLONE_BASE_PATH` is retained as a
deprecated environment compatibility setting and is not used for new jobs.

### Legacy directory behavior

With `RCLONE_BASE_PATH=UPLOADS`, `DEFAULT_UPLOAD_DIRECTORY=DOWNLOADS`, and the mapping `111111111 -> GOUTHAM`, that user initially uploads to:

```text
Forward file
→ UPLOADS/GOUTHAM/DOWNLOADS/file.mkv
```

That user can select a nested directory for subsequently forwarded files:

```text
/dir Series/Friends
Forward file
→ UPLOADS/GOUTHAM/Series/Friends/file.mkv
```

Another configured user, such as `GALAXY`, has an independent selection under `UPLOADS/GALAXY/...`; one user's `/dir` command never affects another user. Use `/dir` to show your current directory and `/dir default` or `/dir reset` to restore your own default. Each path segment may contain letters, numbers, spaces, hyphens, and underscores; use `/` between nested folders. Selections survive container and server restarts and are captured when each job is queued, so later changes never alter queued or active jobs.

## Per-user persistent rclone remotes

Use `/remotes` to list connected Drives and their Google account emails,
`/remote` to show the current destination, and `/remote gdrive_02` to select a
different Drive:

```text
/remotes
/remote gdrive_02
/dir Movies
Forward file
-> gdrive_02:GOUTHAM/Movies/file.mkv
```

Each user has an independent set of connections and an independent selection.
The selected remote is captured when a job is queued, so switching storage
affects only future files; queued and active jobs keep their original
destination. Connections and selections survive restarts through the mounted
SQLite database and per-user rclone configuration. The setup page disconnects
only the currently selected Drive and automatically selects another connected
Drive when available.

## Commands

All interaction occurs privately with the bot. Use `/start`, `/connect`,
`/tutorial`, `/status`, `/cancel [job-id]`, `/help`, `/ls`,
`/dirroot [name|default]`, `/dir [path]`, `/remote [name]`, and `/remotes`.
`/tutorial` sends a compact illustrated setup and playback guide for
iPhone/iPad, Android, and Android TV. `/ls` lists only the commands available
to the requesting user.

Set `ADMIN_TELEGRAM_USER_ID` to the administrator's numeric Telegram user ID
to enable read-only operational commands for that account:

- `/db user` (or `/db users`) — all users, connection state, destinations,
  and job totals
- `/db user <user-id>` — one user's operational details
- `/db activeworks` — all queued, downloading, and uploading jobs
- `/db stats` — aggregate user and job counts
- `/db failed [limit]` — recent failures (1–50, default 10)

These commands never display Telegram session paths, web/onboarding/OAuth
tokens, or rclone credentials. Leaving `ADMIN_TELEGRAM_USER_ID` blank disables
the admin command set.

New jobs do not use a shared processing group. The bot replies directly to the
submitted media with a random job reference. The owner's Telethon session finds
that private reply, follows its `reply_to_msg_id` to the owner's original media
message, and downloads it. No external user is added to a common group and no
user session can see another user's bot conversation.

The `/connect` page lets the user choose either QR login or phone-number login.
Phone login accepts an international number such as `+919876543210`, sends a
Telegram login code, and then requests the Telegram two-step-verification
password when that account has one. The service never writes the phone number,
login code, or password to SQLite.

Telegram QR tokens are short-lived even though the overall onboarding window
is longer. While the connection page remains open, the service automatically
recreates expired QR tokens and replaces the displayed image. Always scan the
currently visible QR; an older photograph cannot be accepted after its
embedded token expires. On a single mobile device, use the phone-number option
instead.

## Configuration reference

`.env.example` is the authoritative full reference.
`DEFAULT_RCLONE_REMOTE` defines the base name for automatically created Drive
remotes, while `MULTY_RCLONE_COUNT` limits how many different Google accounts
each Telegram user can connect. Important controls also include
`DEFAULT_UPLOAD_DIRECTORY`, queue/concurrency limits, disk reserve and optional
size ceiling, progress interval, rclone retry/checker/transfer parameters,
collision policy (`rename`, `overwrite`, `skip`), rotating logs, and local
cleanup after successful uploads.

`PHONE_LOGIN_TTL_SECONDS=300` controls how long a phone-code login remains
open, and `MAX_TELEGRAM_CODE_ATTEMPTS=5` limits incorrect code submissions.
`TELEGRAM_2FA_TTL_SECONDS` and `MAX_TELEGRAM_2FA_ATTEMPTS` independently
control the optional two-step-verification stage. `MAX_PENDING_QR_LOGINS`
remains the compatibility name for the global number of simultaneous
Telegram onboarding flows; it limits both QR and phone logins.

`MAX_CONCURRENT_USER_WORKERS=2` allows two distinct users to process one file
each in parallel, from Telegram download through cloud upload. A single user
can occupy only one worker. The dispatcher selects the oldest queued file for
each available user and orders candidates by creation time and job ID. Work
from a third user remains queued until a worker is free, and the Telegram
status explains that the workers are busy. This preserves submission-time
priority: earlier user work is selected before later work, including when the
same user submits another group of files later.

Google Drive uploads use `RCLONE_DRIVE_CHUNK_SIZE=64Mi` by default. Each active
worker may run one rclone upload, so memory and network usage increase with
`MAX_CONCURRENT_USER_WORKERS`. On a 1 GB server, begin with `2` and monitor
container memory and I/O before increasing it. `RCLONE_UPLOAD_TIMEOUT_MINUTES`
stops a genuinely wedged cloud process; progress remains below completion
until rclone exits and remote verification succeeds.

If a host repeatedly sends the complete file but loses Google Drive's final response, use `RCLONE_RETRIES=1` and `RCLONE_LOW_LEVEL_RETRIES=1`. The service checks the expected remote path and size up to six times over 30 seconds after a non-zero rclone exit. A committed object is treated as successful; a missing or wrong-sized object remains failed and retained locally for `.retry`.

## Large files, recovery, and cleanup

Files are never loaded wholly into memory. Telethon writes incrementally; rclone handles cloud-side retry/resumability according to the backend. A job begins only when free space covers its Telegram size plus the configured reserve. On restart, active JSON states become recoverable and are queued when `RETRY_INTERRUPTED_JOBS=true`; source messages must still exist. Successful local files are removed only after rclone exit and remote size verification. Failed files remain for `FAILED_FILE_RETENTION_HOURS` unless immediate partial deletion is enabled.

The image includes `cryptg`, Telethon's native MTProto encryption accelerator. Actual download speed still depends on the route to the Telegram media data center; repeated connection resets or refused connections in the logs indicate a network/DC-path bottleneck rather than an application rate limit.

Large Telegram files use four parallel aligned download lanes by default (`TELEGRAM_DOWNLOAD_CONNECTIONS=4`) once they reach `PARALLEL_DOWNLOAD_MIN_SIZE_MB=64`. Each lane has a 120-second inactivity watchdog (`TELEGRAM_DOWNLOAD_STALL_TIMEOUT_SECONDS=120`). If a lane stops returning data, all parallel lanes are cancelled before the file is restarted with Telethon's sequential downloader. Set the connection count to `1` to always use the sequential downloader. More connections are not always faster and may worsen an unstable media-DC route; increase gradually and do not exceed the validated maximum of 16.

Downloaded files are deleted after verified uploads when `DELETE_LOCAL_AFTER_SUCCESS=true`, and rclone's process memory is returned automatically when it exits. Docker's `NET I/O` and `BLOCK I/O` values are lifetime counters, not retained buffers; resetting them would require recreating the container and would not free RAM or disk space. Do not run host-wide Linux cache-dropping commands after jobs because they affect every service and generally reduce performance.

## Security

The container runs as non-root with all Linux capabilities dropped, binds its
web port only to host loopback, uses subprocess argument arrays (never a
shell), and sanitizes filenames. Never commit `.env`, SQLite data, sessions,
downloads, logs, or rclone configuration. Telegram sessions and Google OAuth
tokens grant account access: restrict their filesystem permissions, back them
up encrypted, and revoke exposed credentials immediately.

`config/users` is intentionally mounted writable. Google Drive refresh tokens
are stored in each user's rclone configuration, and rclone persists refreshed
tokens by atomically replacing that file. A read-only mount can make an upload
retry after its data was committed. Restrict the host directory to the service
account rather than mounting it read-only.

On Linux, prepare the mounted rclone configuration for container UID/GID `10001:10001`:

```bash
sudo chown -R 10001:10001 config/users data
sudo chmod 700 config/users data/users data/pending-telegram
```

The application never changes these host permissions automatically.

Repository and Docker context rules exclude `.env` variants, Telegram sessions, rclone configuration, downloads, state, logs, image archives, private keys, IDE metadata, and caches. `.env.example` contains placeholders only. Never add production credentials to workflow YAML or Compose files; keep them in GitHub Actions secrets and server-mounted runtime files.

## Testing and updates

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall app tests
docker compose build --pull
docker compose up -d
```

## Troubleshooting

- **Telegram session missing/expired:** open `/connect` and reconnect that user's Telegram account.
- **Remote invalid/quota/permission:** use `/remotes` to confirm the selected remote, then inspect the service logs.
- **Duplicate Google account:** choose a different Google account; one account cannot occupy two Drive slots for the same Telegram user.
- **Older Drive has no displayed email:** open `/connect` and select **Add Google Drive**; the service attempts to recover the existing account identity automatically.
- **Rclone config read-only:** make `config/users` writable by container UID 10001. OAuth token refresh cannot work on a read-only mount.
- **Unhealthy container:** inspect `/data/state/health.json`, `docker compose ps`, and logs.
- **Message ignored:** confirm `WATCH_MODE=chat`, the private `WATCH_CHAT_ID`, and that the sender has a position-matched entry in both allowed-user lists.
- **Startup mapping error:** ensure `ALLOWED_USER_IDS` and `ALLOWED_USER_NAME` have the same number of unique comma-separated entries.
- **Unknown chat ID:** temporarily set `DEBUG_TELEGRAM_IDS=true`, send a group message, copy the logged chat ID, then disable debugging.
- **Disk rejection:** free space or lower `MIN_FREE_DISK_GB` cautiously.
- **Flood waits:** transfers continue; progress edits resume later.
- **File reference expired:** use `.retry`; if Telegram no longer serves it, forward the source again.
