# Progress log

One entry per completed phase, newest last.

## Phase 0 - 2026-10-16 - Project bootstrap
Built:
- uv project (Python 3.12, pyproject.toml, uv.lock) with the section 2 stack except crawlers and croniter.
- Dockerfile (python:3.12-slim, ffmpeg, uv from its official image, non-root user `median`, venv at /opt/venv), docker-compose.yml (db, web, worker; volumes pgdata and media-data), docker-compose.override.yml (bind mount, uvicorn --reload), docker/initdb/10-create-test-db.sh, .env.example, Makefile.
- App skeleton: app/config.py (pydantic-settings), app/db/ (async engine, session dependency, declarative Base, migrate entry point), app/migrations/ (async Alembic env, empty revision 0001), app/jobs/ (Procrastinate app, `ping` task, open_jobs/defer_ping/job_counts facade, idempotent schema install).
- Web: base layout with navigation to every section 13 page, `?` shortcut dialog, dashboard (database status, job counts), "Coming in phase N" placeholders, vendored htmx 2.0.11, GET /healthz, POST /api/debug/ping.
- Tests (16): healthz, unreachable database, dashboard and nav, placeholders, vendored htmx, ping deferral (row in procrastinate_jobs), ping task, migrate idempotency.

Verify:
- `make up`, then open http://localhost:8000 (dashboard with navigation).
- `curl localhost:8000/healthz` -> `{"status":"ok","database":"ok"}`.
- `curl -X POST localhost:8000/api/debug/ping`, then `make logs` shows the worker line `Job ping[N](...) ended with status: Success ... Result: pong`.
- Run `make migrate` twice (the second prints "procrastinate schema: already present"). Then `make lint` and `make test`.

Known gaps:
- The cloud sandbox cannot build the image (blocked Debian mirrors and ghcr.io). The Docker build and full-stack checks run in GitHub Actions CI instead (.github/workflows/ci.yml with scripts/smoke.sh); see the CI run on the phase 0 commits.
- The dashboard shows no file counts yet (no files table until phase 1).
- APP_PASSWORD is read but unused until phase 8.

## Phase 1 - 2026-10-16 - Data model and ingest pipeline
Built:
- Migration 0002: enums media_kind, file_status, ingest_outcome, tag_origin; tables files, file_sources (source_id and job_run_id without foreign keys until phase 5), ingest_events (plus batch_id), folders (built-in top-level "Inbox", sibling-unique names), tags, file_tags. Models in app/db/models_files.py and app/db/models_library.py.
- app/storage: DataPaths for every section 5 location, sha-based archive/thumb/trash paths, atomic_move/atomic_copy/atomic_write_bytes (temp name in the destination directory, fsync, os.replace), run_after_commit (session hook; dropped on rollback), trash move/restore.
- app/library/tags.py: normalize_tag (section 9), get_or_create_tags, crawler merge and user add rules.
- app/ingest: IngestOrigin; ingest(path, origin) moves (or copies) into staging/<batch_id>/ and defers process_ingest; process_ingest implements section 7 steps 1-5 (known remote before hashing, streamed SHA-256 in 1 MiB chunks, duplicate_exact with source and crawler-tag merge, forced_reingest of discarded files, probe with Pillow then ffprobe then extension, WebP thumbnails, compute_perceptual hook for phase 2, status error on unexpected exceptions); concurrency-safe through the sha256 unique constraint.
- Jobs process_ingest and retry_file. API: POST /api/uploads (streaming multipart), GET /api/uploads/<batch_id> (JSON or htmx fragment), POST /api/files/<id>/retry. Media: GET /media/<id>/original (Range, 206) and /media/<id>/thumb.
- Pages: /upload (drag and drop, tags, force checkbox, progress, polling batch status), /review (temporary grid of pending files and failed files with Retry), /files/<id> (img/video/audio, metadata, tags, sources, status), dashboard counts per status.
- CLI: python -m app.cli import <directory> [--tags a,b] [--force] [--recursive]; optional read-only ./imports mount at /imports in the dev override.
- Tests: 86 in total (70 new): tag normalization, storage, every pipeline case of the phase brief (new, duplicate, discarded duplicate, force, known remote, tag merge, video, audio, corrupt jpeg, error and retry, concurrent identical content), upload API end to end, htmx fragment, Range 206, retry API, pages, ingest() and the CLI.
- scripts/smoke.sh phase 1 checks (media generated in the web container by scripts/smoke_media.py); SMOKE_LOCAL=1 runs them against a native stack.

Verify:
- `make up`, open http://localhost:8000/upload, drop 3 images and 1 video: the batch card ends with new 4; /review shows them with thumbnails; the dashboard's pending review count grows by 4.
- Upload the same files again: the batch card shows duplicate exact 4 and the dashboard totals do not change.
- Open the video at /files/<id>; it plays and seeks (`curl -H 'Range: bytes=0-99' -i localhost:8000/media/<id>/original` returns 206).
- Put files in ./imports, then `docker compose exec web python -m app.cli import /imports --recursive`; follow GET /api/uploads/<batch_id>.
- `make lint`, `make test`, `scripts/smoke.sh`.

Known gaps:
- Cloud session: Docker Hub answered 429 for postgres:18 and the image cannot be built here, so local checks ran natively against PostgreSQL 16 from the sandbox (migrations, 86 tests, uvicorn + worker + full smoke.sh with SMOKE_LOCAL=1, Chromium upload and video seek). CI runs the real image and postgres:18.
- Playwright's Chromium has no H.264 decoder, so the in-browser seek check used a VP9 WebM; MP4 serving is checked by Range requests (smoke.sh, tests).
- No perceptual hashes or similar-file flags yet (phase 2); no archive/discard flow (phase 3), so trash and archive paths are only exercised by tests.
- A file whose processing job is lost (worker killed mid-job) stays in status processing; there is no sweeper yet.
- An ingest that fails before a files row exists (for example an unreadable staging file) records an error event only; the UI cannot retry it.
