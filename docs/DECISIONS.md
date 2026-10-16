# Decision log

Format: YYYY-MM-DD | Phase N | Decision | Reason

2026-10-16 | Phase 0 | DATABASE_URL and TEST_DATABASE_URL are plain libpq URLs (postgresql://...); app.config converts them to postgresql+psycopg:// for SQLAlchemy. | Procrastinate and psql take libpq URLs; one format for every consumer.
2026-10-16 | Phase 0 | htmx is vendored at app/web/static/vendor/htmx-2.0.11.min.js, served at /static. | ARCHITECTURE.md section 4 puts static/ under app/web/; the Phase 0 brief said app/static. The architecture is the source of truth.
2026-10-16 | Phase 0 | make migrate runs python -m app.db.migrate: alembic upgrade head, then the Procrastinate schema.sql only when procrastinate_jobs does not exist yet. | procrastinate schema --apply fails on an existing schema; this keeps migrate idempotent. A future Procrastinate upgrade that ships schema migrations must add an Alembic migration that runs them.
2026-10-16 | Phase 0 | make up starts db, runs make migrate, then starts web and worker. No separate migrate service. | Keeps the three services from the spec, and web and worker always start on a migrated database.
2026-10-16 | Phase 0 | The image installs the dev dependency group (pytest, ruff) too. | make test and make lint run inside the web container; one image for everything is simpler for a single-user app.
2026-10-16 | Phase 0 | The venv lives at /opt/venv (UV_PROJECT_ENVIRONMENT), PYTHONPATH=/app. | The dev override bind-mounts the code over /app without hiding the installed packages.
2026-10-16 | Phase 0 | The uv image is pinned to ghcr.io/astral-sh/uv:0.11.32. | Reproducible builds; same uv version that produced uv.lock.
2026-10-16 | Phase 0 | Placeholder phase numbers in the nav: upload 1, review 3, library, tags and /files/<id> 4, sources and jobs 5, search 7, settings 8. | Inferred from the phase hints in ARCHITECTURE.md (2 = perceptual hashes, 5-6 = crawlers, 7 = search, 8 = ops). Adjust app/web/pages.py NAV if the phase plan differs.
2026-10-16 | Phase 0 | Tests reset the test database (DROP SCHEMA public CASCADE) once per session and run every migration from scratch; conftest refuses to run if TEST_DATABASE_URL and DATABASE_URL name the same database. | Exercises the migrations on every run and keeps the app database safe.
