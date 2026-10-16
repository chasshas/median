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
