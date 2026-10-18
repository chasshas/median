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
| `make backup` / `make restore FILE=...` | Dump the database now, or replace it with a dump (see Backups) |
| `make backup-files DEST=...` | Copy archive/ and thumbs/ to a directory with rsync |

To import an existing collection, put it in `./imports` (or set `IMPORTS_DIR`) and run `docker compose exec web python -m app.cli import /imports --recursive [--tags a,b] [--force]`. Files are copied; the originals stay where they are.

Perceptual duplicates: `python -m app.cli rehash [--missing-only]` recomputes image and video hashes from the originals and rebuilds their similar pairs; `python -m app.cli recompute-similar` rebuilds all pairs from stored hashes with the current thresholds (dismissed pairs stay dismissed). Run them with `docker compose exec web ...`.

Review: open http://localhost:8000/review and press `?` for the keyboard map (J/K move, A archive, D discard, S skip, C compare, X select, Shift+A/Shift+D for the selection, U undo, G grid). Discarded originals stay in `trash/` for `TRASH_RETENTION_DAYS` (default 7) so a discard can be undone; the worker purges older ones daily, and `python -m app.cli purge-trash` does it on demand.

Crawlers: /sources creates sources for the built-in add-ons (yt-dlp for video sites, gallery-dl for image galleries, direct links, demo) or your own (see [docs/ADDONS.md](docs/ADDONS.md)). The worker installs or upgrades yt-dlp and gallery-dl in `DATA_DIR/tools/venv` each time it starts (versions in `DATA_DIR/tools/versions.json`); `python -m app.cli tools` does it on demand.

Search: /search takes the query syntax of [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) section 12 (`cat -dog folder:"Animals" collected:2026-01-01..2026-03-31 type:image`), with a filter panel, autocomplete and saved searches.

Media and state live in the `media-data` volume (mounted at `DATA_DIR`, default `/data`), and the database lives in `pgdata`. `docker-compose.override.yml` is for development: it bind-mounts the source and reloads on changes. For a deployment, leave it out: `docker compose -f docker-compose.yml up -d`.

## Settings

/settings changes the similarity thresholds, the trash retention, the time zone, the default review order, how many database backups to keep and when the crawler tools are updated. Saved values are stored in the database (`app_settings`) and override the environment variables of the same name (see `.env.example`); setting a value back to its default removes the override. After a threshold change the page offers to recompute the similar pairs. The same page runs every maintenance job on demand and shows when each one runs next.

| Job | Schedule (APP_TIMEZONE) | What it does |
| --- | --- | --- |
| purge_trash | daily 03:30 | deletes trash originals older than the retention |
| backup | daily 04:00 | `pg_dump` of the database to `DATA_DIR/backups/db-<timestamp>.dump`, keeps the newest N (default 14) |
| housekeeping | daily 04:30 | deletes job runs and job logs older than 90 days (ingest events stay), measures disk usage per storage area |
| integrity | Sundays 05:00 | compares the database with `DATA_DIR` (see below) |
| update_tools | setting, default Mondays 05:00 | `pip install --upgrade "yt-dlp[default]" gallery-dl` in the tool venv |

Every run is listed on /jobs (filter by kind and status) with its log. The same jobs exist on the command line: `docker compose exec web python -m app.cli backup | integrity [--verify-hashes] | housekeeping | update-tools`.

When the worker starts, runs that a stopped worker left running are marked failed with the error "interrupted", and files stuck in processing for more than an hour are queued again.

## Exposing the service

Median is a single-user application. Keep it on your LAN, or reach it through a VPN (WireGuard, Tailscale) or a reverse proxy that terminates TLS (Caddy, nginx, Traefik). Never publish port 8000 directly to the internet.

- Set `APP_PASSWORD` (and `SECRET_KEY`, a long random string such as the output of `openssl rand -hex 32`) in `.env`, then `make down && make up`. Every page then asks for the password; the session cookie is HttpOnly, SameSite=Lax and lasts 30 days. Changing the password signs every session out. Without a password the UI shows a "No password set" banner and the web service logs a warning at startup.
- Failed logins are slowed down per client IP (1 s, 2 s, 4 s, ... up to 30 s). Behind a reverse proxy every request comes from the proxy's address, so the slowdown applies to all clients together; that is fine for one user.
- Requests that change something (POST, PUT, PATCH, DELETE) must come from a page of the same site: their Origin or Referer header must name the host the request was sent to, or the answer is 403. Browsers send these headers on their own. Scripts that call the API must add one, for example `curl -H "Origin: http://median.lan:8000" -X POST ...`. A reverse proxy must pass the original Host header through (`proxy_set_header Host $host;` in nginx; Caddy does it by default).
- Behind TLS the session cookie is marked Secure when uvicorn sees https; run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy address>` (in `docker-compose.yml`) if the proxy speaks plain http to it.

## Backups

The database and the media are backed up separately.

- Database: the backup job writes a custom-format dump to `DATA_DIR/backups/` every day; run it now from /settings or with `make backup`. The image has the PostgreSQL 18 client tools, matching the `postgres:18` server (the build fails if `pg_dump` is not version 18). Copy dumps off the machine yourself, for example `docker compose cp worker:/data/backups ./db-backups`.
- Restore: `make restore FILE=db-20261018T040000Z.dump` (a dump name in `DATA_DIR/backups`) or `make restore FILE=./path/on/this/machine.dump`. It asks for confirmation (or pass `CONFIRM=yes`), stops web and worker, checks that the dump is readable, replaces everything in the app database with it, applies the migrations on top and starts web and worker again. On a fresh machine: `make up` once, then `make restore ...`.
- Media: the dump holds no files. Copy `archive/` (the originals) and `thumbs/` with `make backup-files DEST=/path/to/backup/disk`, which runs rsync in a container (as your user) and only adds or updates files, so it can run as often as you like. To restore media, copy those directories back into the `media-data` volume (`docker compose cp /path/to/backup/disk/archive worker:/data/`). Staging and trash are short-lived and not part of this backup.

## Integrity check

The integrity job (weekly, or from /settings) reports archived files whose original is missing, pending or failed files without their staging file, discarded files whose trash copy is gone before the retention ends, files in `archive/` or `staging/` without a matching row, leftover `work/` directories and missing thumbnails. With "verify hashes" (or `--verify-hashes`) it also re-hashes every archive file and reports mismatches. The report is on the run's page, with buttons for the safe fixes only: regenerate thumbnails, ingest orphan staging files, delete leftover work directories. Missing originals, orphan archive files and hash mismatches are never changed automatically.

## Crawler tools

yt-dlp and gallery-dl live in `DATA_DIR/tools/venv`. The worker upgrades them when it starts, and the update_tools job does it on its schedule or from /settings; /settings shows the installed versions and the result of the last update (also in `DATA_DIR/tools/versions.json`). Deno, the JavaScript runtime yt-dlp uses, is part of the image: it is updated by rebuilding the image (`make build`, after changing the version in the `Dockerfile`).

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
| 7 | Search and saved searches (done) |
| 8 | Operations: auth, backup, integrity checks, settings, tool updates (done) |
| 9 | Hardening and polish |
