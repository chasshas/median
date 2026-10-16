# Median

Single-user, self-hosted service that collects media through crawler add-ons and manual upload, detects duplicates (including against previously discarded files), lets the user review each file (archive or discard), classifies archived files into nested folders and tags, and searches them. The full specification is imported below; it is the source of truth.

@docs/ARCHITECTURE.md
@docs/PROGRESS.md
@docs/DECISIONS.md

## Commands
- make up / make down / make logs: run the Docker Compose stack (db, web, worker)
- make migrate: apply Alembic migrations and the Procrastinate schema
- make test: run pytest inside the web container against the test database
- make lint: ruff format --check and ruff check
- make shell / make psql: open a shell in web / psql on the app database

## Phase protocol (follow for every phase)
1. Read docs/ARCHITECTURE.md, docs/PROGRESS.md and docs/DECISIONS.md. Before coding, write a short plan: files to add or change, migrations, jobs, routes, tests.
2. Implement only the current phase's scope. Never start the next phase.
3. If the spec is ambiguous, choose the simplest option consistent with it and record it in docs/DECISIONS.md (date, phase, decision, reason). If the spec is wrong or blocks you, stop and ask the user.
4. Schema changes only through new Alembic migrations. Never edit a migration that has been applied.
5. Write tests together with the code. Run make lint and make test until both pass.
6. Check every acceptance criterion of the phase yourself (curl, the test client, or rendering the pages) and report each as PASS or FAIL with evidence. Add the checks to scripts/smoke.sh. Run the stack with make up where Docker can build the image; otherwise follow "Verification in cloud sessions" below.
7. Append to docs/PROGRESS.md: phase number, date, what was built, how to verify it, known gaps.
8. Commit with the message "phase N: <summary>" and push. The phase is done only when CI (.github/workflows/ci.yml) is green on that commit; if it fails, fix it and push again.

## Verification in cloud sessions
Cloud sessions cannot build the image (Debian mirrors and ghcr.io are blocked) and the user cannot run commands locally. So:
- CI on GitHub is the source of truth for the image and the full stack. On every push it builds the image, runs make up, runs make migrate twice, then scripts/smoke.sh, make lint and make test. Read the run and job logs with the GitHub tools and quote them as evidence.
- Before pushing, check natively: docker compose up -d --wait db; set DATABASE_URL and TEST_DATABASE_URL to the db container IP (docker inspect); then uv run python -m app.db.migrate, uv run ruff format --check ., uv run ruff check ., uv run pytest; run uvicorn app.main:app and procrastinate --app=app.jobs.app.job_app worker from .venv/bin with PYTHONPATH=. for manual checks.
- Never work around the sandbox network policy. Report blocked hosts to the user.

## Guardrails
- Never delete or rewrite real data under DATA_DIR from a dev command. Tests use a temporary DATA_DIR and the separate test database.
- No dependencies outside the stack in ARCHITECTURE.md section 2 without a DECISIONS.md entry.
- Never call a shell with interpolated strings. Subprocesses take argument lists.
- Keep this file under 200 lines; put detail in docs/.
