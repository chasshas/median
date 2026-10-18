# One image for the web and worker services.
FROM ghcr.io/astral-sh/uv:0.11.32 AS uv
# JavaScript runtime for yt-dlp (YouTube needs it); only the binary is copied.
FROM denoland/deno:bin-2.9.7 AS deno

FROM python:3.12-slim

# ffmpeg for media, rsync for make backup-files, and the PostgreSQL 18 client tools (pg_dump,
# pg_restore) from the PGDG repository so backups match the postgres:18 server.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg rsync ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && pg_dump --version \
    && pg_dump --version | grep -Eq '^pg_dump \(PostgreSQL\) 18\.' \
    && pg_restore --version | grep -Eq '^pg_restore \(PostgreSQL\) 18\.'

COPY --from=uv /uv /uvx /bin/
COPY --from=deno /deno /usr/local/bin/deno

# The venv lives outside /app so the dev bind mount of the code does not hide it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    RUFF_CACHE_DIR=/tmp/ruff-cache \
    DATA_DIR=/data

RUN groupadd --system --gid 1000 median \
    && useradd --system --uid 1000 --gid median --home-dir /app --shell /usr/sbin/nologin median

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project

COPY --chown=median:median . .
RUN uv sync --frozen \
    && mkdir -p /data \
    && chown median:median /data

USER median
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
