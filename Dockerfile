# One image for the web and worker services.
FROM ghcr.io/astral-sh/uv:0.11.32 AS uv

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/

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
