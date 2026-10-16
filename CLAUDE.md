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
6. Run the stack with make up and check every acceptance criterion of the phase yourself (curl, the test client, or rendering the pages). Report each criterion as PASS or FAIL with evidence.
7. Append to docs/PROGRESS.md: phase number, date, what was built, how to verify it, known gaps.
8. Commit with the message "phase N: <summary>".

## Guardrails
- Never delete or rewrite real data under DATA_DIR from a dev command. Tests use a temporary DATA_DIR and the separate test database.
- No dependencies outside the stack in ARCHITECTURE.md section 2 without a DECISIONS.md entry.
- Never call a shell with interpolated strings. Subprocesses take argument lists.
- Keep this file under 200 lines; put detail in docs/.
