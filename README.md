# Median

Median is a single-user, self-hosted service for collecting and archiving media. Files come in through crawler add-ons (yt-dlp, gallery-dl and others, run server-side) or manual upload. Exact duplicates are dropped automatically, even when they match a file you discarded long ago, and visually similar files are flagged for you. You review every new file and archive it or discard it. Archived files go into nested folders and get tags, and you can search everything.

The full specification is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Progress is logged in [docs/PROGRESS.md](docs/PROGRESS.md) and design decisions in [docs/DECISIONS.md](docs/DECISIONS.md).

## Getting started

Requirements: Docker with the Compose plugin, and make.

```sh
cp .env.example .env      # adjust passwords, timezone, ports
make up                   # build, start db, migrate, start web + worker
```

Open http://localhost:8000. `make logs` follows the logs and `make down` stops the stack.

| Command | What it does |
| --- | --- |
| `make up` / `make down` / `make logs` | Start, stop and follow the stack (db, web, worker) |
| `make migrate` | Apply Alembic migrations and the Procrastinate schema (safe to repeat) |
| `make test` | Run pytest in the web container against the test database |
| `make lint` / `make format` | Check, or apply, ruff formatting and lint |
| `make shell` / `make psql` | Open a shell in web, or psql on the app database |

To import an existing collection, put it in `./imports` (or set `IMPORTS_DIR`) and run `docker compose exec web python -m app.cli import /imports --recursive [--tags a,b] [--force]`. Files are copied; the originals stay where they are.

Perceptual duplicates: `python -m app.cli rehash [--missing-only]` recomputes image and video hashes from the originals and rebuilds their similar pairs; `python -m app.cli recompute-similar` rebuilds all pairs from stored hashes with the current thresholds (dismissed pairs stay dismissed). Run them with `docker compose exec web ...`.

Review: open http://localhost:8000/review and press `?` for the keyboard map (J/K move, A archive, D discard, S skip, C compare, X select, Shift+A/Shift+D for the selection, U undo, G grid). Discarded originals stay in `trash/` for `TRASH_RETENTION_DAYS` (default 7) so a discard can be undone; the worker purges older ones daily, and `python -m app.cli purge-trash` does it on demand.

Crawlers: /sources creates sources for the built-in add-ons (yt-dlp for video sites, gallery-dl for image galleries, direct links, demo) or your own (see [docs/ADDONS.md](docs/ADDONS.md)). The worker installs or upgrades yt-dlp and gallery-dl in `DATA_DIR/tools/venv` each time it starts (versions in `DATA_DIR/tools/versions.json`); `python -m app.cli tools` does it on demand.

Media and state live in the `media-data` volume (mounted at `DATA_DIR`, default `/data`), and the database lives in `pgdata`. `docker-compose.override.yml` is for development: it bind-mounts the source and reloads on changes. For a deployment, leave it out: `docker compose -f docker-compose.yml up -d`.

## Phases

The project is built in ten phases, 0 to 9. The scope of each phase after 0 is provisional and will be confirmed as it starts.

| Phase | Scope |
| --- | --- |
| 0 | Bootstrap: Docker stack, app skeleton, migrations, job queue, base UI (done) |
| 1 | Ingest service, storage layout, upload page, exact duplicates (done) |
| 2 | Perceptual hashes and similar-file detection (done) |
| 3 | Review: archive, discard, undo, trash (done) |
| 4 | Library: folders, tags, file page (done) |
| 5 | Crawler add-on framework, sources, jobs, scheduling (done) |
| 6 | Built-in add-ons (yt-dlp, gallery-dl, direct links) (done) |
| 7 | Search and saved searches |
| 8 | Operations: auth, backup, integrity checks, settings, tool updates |
| 9 | Hardening and polish |
