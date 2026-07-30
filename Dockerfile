FROM python:3.12.11-slim-bookworm
ARG RCLONE_VERSION=1.74.4
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64) rarch=amd64;; arm64) rarch=arm64;; *) exit 1;; esac \
    && curl -fsSLo /tmp/rclone.zip "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${rarch}.zip" \
    && unzip /tmp/rclone.zip -d /tmp \
    && install -m 0755 /tmp/rclone-*/rclone /usr/local/bin/rclone \
    && rm -rf /var/lib/apt/lists/* /tmp/rclone* \
    && groupadd --system --gid 10001 uploader && useradd --system --uid 10001 --gid uploader --home /app uploader
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
RUN chmod 0555 scripts/*.sh \
    && mkdir -p /data/users /data/pending-telegram /data/logs /config/users \
    && chown -R uploader:uploader /app /data /config/users \
    && chmod 0700 /data/users /data/pending-telegram /config/users
USER uploader
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["python", "-m", "app.main"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 CMD ["curl", "-fsS", "http://127.0.0.1:8080/healthz"]
