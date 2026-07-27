# Telegram Cloud Uploader

A production-oriented, Dockerized Python 3.12 service that uses a normal Telegram account through Telethon/MTProto. Trusted users forward media into one configured private Telegram group; the service streams it to disk and transfers it to per-user directories on any rclone-supported cloud. Albums are handled consistently as one independent job per Telegram message while preserving `media_group_id` in state.

## Architecture

`Telethon event handler -> sender allowlist -> bounded asyncio queue -> download worker -> rclone copyto -> remote verification`. Each message has an atomic JSON state file in `/data/state`; downloads use isolated `/data/downloads/{chat_id}_{message_id}` directories. Per-user directory selections are atomically persisted in `/data/state/user_directories.json`, and per-user rclone selections in `/data/state/user_remotes.json`. No database or public port is used.

## Prerequisites

- Docker Engine with Compose v2
- A Telegram API ID/hash from [my.telegram.org](https://my.telegram.org)
- An rclone remote
- Enough local space for the largest file plus `MIN_FREE_DISK_GB`

## Configure

```bash
cp .env.example .env
mkdir -p data/downloads data/state data/session data/logs config/rclone
```

PowerShell:

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force data/downloads,data/state,data/session,data/logs,config/rclone
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

## Configure rclone

Configure outside the service (Google Drive: choose `drive`, complete OAuth, select the appropriate scope):

```bash
docker run --rm -it -v "$(pwd)/config/rclone:/config/rclone" rclone/rclone config --config /config/rclone/rclone.conf
```

PowerShell:

```powershell
docker run --rm -it -v "${PWD}/config/rclone:/config/rclone" rclone/rclone config --config /config/rclone/rclone.conf
```

The same `rclone.conf` may contain multiple sections, for example `[gdrive]`, `[mega]`, and `[onedrive]`. Configure the default and Telegram-selectable allowlist:

```env
DEFAULT_RCLONE_REMOTE=gdrive
ALLOWED_RCLONE_REMOTES=gdrive,mega,onedrive
```

`DEFAULT_RCLONE_REMOTE` falls back to the legacy `RCLONE_REMOTE` when omitted. If `ALLOWED_RCLONE_REMOTES` is omitted, only the default is selectable. Matching is case-insensitive, but the spelling from `ALLOWED_RCLONE_REMOTES` is retained in state and paths. At startup the service uses `rclone listremotes` to ensure every allowed name exists in the mounted config; it does not contact the cloud merely to validate names.

Verify the mounted configuration:

```bash
docker compose -f docker-compose.prod.yml exec telegram-uploader \
  rclone listremotes --config /config/rclone/rclone.conf
```

Google Drive, OneDrive, Dropbox, S3, B2, WebDAV, and other rclone backends work without provider-specific application code.

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
mkdir -p ~/telegram-uploader/{data/downloads,data/state,data/session,data/logs,config/rclone}
cd ~/telegram-uploader
mv env.example .env
# Edit .env before continuing.
docker load -i telegram-uploader-linux-amd64.tar.gz  # Use the arm64 file on ARM64.
```

A first-time installation also needs an rclone configuration and Telegram login session:

```bash
docker run --rm -it \
  -v "$PWD/config/rclone:/config/rclone" \
  rclone/rclone config --config /config/rclone/rclone.conf

docker compose -f docker-compose.prod.yml run --rm telegram-uploader python -m app.auth
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f telegram-uploader
```

For an update, preserve `.env`, `data/`, and `config/rclone/`; download and load the new image archive, replace `docker-compose.prod.yml`, and run `docker compose -f docker-compose.prod.yml up -d --force-recreate`.

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
mkdir -p ~/telegram-uploader/{data/downloads,data/state,data/session,data/logs,config/rclone}
cd ~/telegram-uploader
# Create the private runtime files once. GitHub Actions does not replace them:
cp .env.example .env
# Authenticate once so data/session/telegram.session exists.
# Configure rclone so config/rclone/rclone.conf exists.
```

The server only needs the production Compose file and these persistent private assets:

- `.env`
- `data/session/telegram.session`
- `config/rclone/rclone.conf`
- the mounted `data/` directories for downloads, state, and logs

Configure these GitHub repository settings under **Settings -> Secrets and variables -> Actions**:

- Secret `SERVER_HOST`: Oracle hostname or IP address
- Secret `SSH_PRIVATE_KEY`: private key text used to connect to Oracle
- Variable `SERVER_USER`: Oracle SSH user, such as `ubuntu`

The workflow deploys only inside `~/telegram-uploader`. It replaces `telegram-uploader.tar` and `docker-compose.prod.yml`, loads the image, recreates the container, waits for it to become healthy, and removes the transferred archive. The server's `.env`, Telegram session, rclone configuration, downloads, state, and logs remain in their existing bind-mounted paths. Restrict the deploy key to this server and repository workflow.

For a manual production update when troubleshooting:

```bash
cd ~/telegram-uploader
# Copy telegram-uploader.tar and docker-compose.prod.yml into this directory first.
docker load -i telegram-uploader.tar
docker compose -f docker-compose.prod.yml up -d --force-recreate --remove-orphans
```

## Per-user persistent upload directories

With `RCLONE_BASE_PATH=UPLOADS`, `DEFAULT_UPLOAD_DIRECTORY=DOWNLOADS`, and the mapping `111111111 -> GOUTHAM`, that user initially uploads to:

```text
Forward file
→ UPLOADS/GOUTHAM/DOWNLOADS/file.mkv
```

That user can select a nested directory for subsequently forwarded files:

```text
.dir Series/Friends
Forward file
→ UPLOADS/GOUTHAM/Series/Friends/file.mkv
```

Another configured user, such as `GALAXY`, has an independent selection under `UPLOADS/GALAXY/...`; one user's `.dir` command never affects another user. Use `.dir` to show your current directory and `.dir default` or `.dir reset` to restore your own default. Each path segment may contain letters, numbers, spaces, hyphens, and underscores; use `/` between nested folders. Selections survive container and server restarts and are captured when each job is queued, so later changes never alter queued or active jobs.

## Per-user persistent rclone remotes

Use `.remote` to show your current and available storage, `.remotes` to list the allowlist, and `.remote mega` to select another configured remote. Remote and directory selections are independent:

```text
.remote mega
.dir Movies
Forward file
-> mega:UPLOADS/GOUTHAM/Movies/file.mkv
```

Each allowed Telegram user has an independent selection. The remote is captured when a job is queued, so switching storage affects only later files; queued and active jobs keep their original destination. Selections survive restarts and redeployments through the mounted `/data/state/user_remotes.json`. A stored selection removed from `ALLOWED_RCLONE_REMOTES` safely falls back to `DEFAULT_RCLONE_REMOTE`.

## Commands

Type `.status`, `.queue`, `.dir [path|default|reset]`, `.remote [storage-name]`, `.remotes`, `.cancel`, `.cancel <message_id>`, `.retry <message_id>`, `.config`, or `.help` in the monitored private group. Chat and sender authorization are applied before command or media processing. `.config` omits secrets.

## Configuration reference

`.env.example` is the authoritative full reference. `ALLOWED_USER_IDS` and `ALLOWED_USER_NAME` are ordered lists that define authorization and each user's top-level cloud directory. `DEFAULT_RCLONE_REMOTE` defines the fallback storage and `ALLOWED_RCLONE_REMOTES` defines the selectable names. `DEBUG_TELEGRAM_IDS=false` is the production-safe default and should be enabled only while discovering initial setup identifiers. Important controls also include `RCLONE_BASE_PATH`, `DEFAULT_UPLOAD_DIRECTORY`, queue/concurrency limits, disk reserve and optional size ceiling, progress interval, rclone retry/checker/transfer parameters, collision policy (`rename`, `overwrite`, `skip`), local cleanup/failed retention, interrupted-job retry, rotating logs, and optional public links. `REMOTE_FOLDER_PATTERN` is deprecated, retained only for environment compatibility, and has no effect; date folders are disabled. `MAX_FILE_SIZE_GB=0` disables the application ceiling.

Google Drive uploads use `RCLONE_DRIVE_CHUNK_SIZE=64Mi` by default. The observed peak on a 1 GB deployment remained far below the production container's 700 MiB hard limit, leaving room for this larger upload buffer. Larger chunks can improve resumable-upload throughput but consume that much memory per active transfer. Production Compose reserves 256 MiB and limits the service to 128 processes. Keep `MAX_CONCURRENT_JOBS=1`, `RCLONE_TRANSFERS=1`, and `RCLONE_CHECKERS=2` on a 1 GB instance. Google Drive does not support rclone's multi-thread single-file upload interface, so `RCLONE_TRANSFERS` only helps when separate files are uploading concurrently; it does not split one Drive file across parallel streams. `RCLONE_UPLOAD_TIMEOUT_MINUTES=180` stops a genuinely wedged cloud process; active progress is capped at 99.9% until rclone exits and remote size verification succeeds. When rclone retries, Telegram shows the attempt number, current-attempt progress, and the latest available rclone error instead of holding at the previous attempt's 99.9%. INFO-level rclone diagnostics are inspected for Drive/API failures, and `RCLONE_RETRIES_SLEEP_SECONDS=10` pauses between whole-file attempts.

If a host repeatedly sends the complete file but loses Google Drive's final response, use `RCLONE_RETRIES=1` and `RCLONE_LOW_LEVEL_RETRIES=1`. The service checks the expected remote path and size up to six times over 30 seconds after a non-zero rclone exit. A committed object is treated as successful; a missing or wrong-sized object remains failed and retained locally for `.retry`.

## Large files, recovery, and cleanup

Files are never loaded wholly into memory. Telethon writes incrementally; rclone handles cloud-side retry/resumability according to the backend. A job begins only when free space covers its Telegram size plus the configured reserve. On restart, active JSON states become recoverable and are queued when `RETRY_INTERRUPTED_JOBS=true`; source messages must still exist. Successful local files are removed only after rclone exit and remote size verification. Failed files remain for `FAILED_FILE_RETENTION_HOURS` unless immediate partial deletion is enabled.

The image includes `cryptg`, Telethon's native MTProto encryption accelerator. Actual download speed still depends on the route to the Telegram media data center; repeated connection resets or refused connections in the logs indicate a network/DC-path bottleneck rather than an application rate limit.

Large Telegram files use four parallel aligned download lanes by default (`TELEGRAM_DOWNLOAD_CONNECTIONS=4`) once they reach `PARALLEL_DOWNLOAD_MIN_SIZE_MB=64`. Each lane has a 120-second inactivity watchdog (`TELEGRAM_DOWNLOAD_STALL_TIMEOUT_SECONDS=120`). If a lane stops returning data, all parallel lanes are cancelled before the file is restarted with Telethon's sequential downloader. Set the connection count to `1` to always use the sequential downloader. More connections are not always faster and may worsen an unstable media-DC route; increase gradually and do not exceed the validated maximum of 16.

Downloaded files are deleted after verified uploads when `DELETE_LOCAL_AFTER_SUCCESS=true`, and rclone's process memory is returned automatically when it exits. Docker's `NET I/O` and `BLOCK I/O` values are lifetime counters, not retained buffers; resetting them would require recreating the container and would not free RAM or disk space. Do not run host-wide Linux cache-dropping commands after jobs because they affect every service and generally reduce performance.

## Security

The container runs as non-root with all Linux capabilities dropped, exposes no ports, uses subprocess argument arrays (never a shell), and sanitizes filenames. Never commit `.env`, sessions, downloads, state, logs, or `rclone.conf`. The Telegram session grants account access: restrict its filesystem permissions, back it up encrypted, and revoke it from Telegram Active Sessions if exposed. Back up `data/session` and `config/rclone/rclone.conf` securely.

`config/rclone` is intentionally mounted writable. OAuth remotes such as Google Drive refresh tokens and rclone persists them by creating a temporary file beside `rclone.conf` and atomically replacing the configuration. A read-only mount can make rclone retry an otherwise completed upload and create duplicate objects on providers that allow duplicate names. Restrict the host directory to the service account rather than mounting it read-only.

On Linux, prepare the mounted rclone configuration for container UID/GID `10001:10001`:

```bash
sudo chown -R 10001:10001 config/rclone
sudo chmod 700 config/rclone
sudo chmod 600 config/rclone/rclone.conf
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

- **Session missing/expired:** rerun the one-shot authentication command.
- **Remote invalid/quota/permission:** run `rclone about REMOTE: --config config/rclone/rclone.conf` and inspect service logs.
- **Allowed remote missing at startup:** compare `ALLOWED_RCLONE_REMOTES` with `rclone listremotes --config config/rclone/rclone.conf`; names must exist in the same mounted config.
- **Rclone config read-only:** make `config/rclone` writable by container UID 10001. OAuth token refresh cannot work on a read-only mount.
- **Unhealthy container:** inspect `/data/state/health.json`, `docker compose ps`, and logs.
- **Message ignored:** confirm `WATCH_MODE=chat`, the private `WATCH_CHAT_ID`, and that the sender has a position-matched entry in both allowed-user lists.
- **Startup mapping error:** ensure `ALLOWED_USER_IDS` and `ALLOWED_USER_NAME` have the same number of unique comma-separated entries.
- **Unknown chat ID:** temporarily set `DEBUG_TELEGRAM_IDS=true`, send a group message, copy the logged chat ID, then disable debugging.
- **Disk rejection:** free space or lower `MIN_FREE_DISK_GB` cautiously.
- **Flood waits:** transfers continue; progress edits resume later.
- **File reference expired:** use `.retry`; if Telegram no longer serves it, forward the source again.
