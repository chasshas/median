# Median

Median is a single-user, self-hosted service for collecting and archiving media. Files come in through crawler add-ons (yt-dlp, gallery-dl and others, run server-side) or manual upload. Exact duplicates are dropped automatically, even when they match a file you discarded long ago, and visually similar files are flagged for you. You review every new file and archive it or discard it. Archived files go into nested folders and get tags, and you can search everything.

The full specification is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Progress is logged in [docs/PROGRESS.md](docs/PROGRESS.md) and design decisions in [docs/DECISIONS.md](docs/DECISIONS.md).

## Install in one step

On a Linux machine or a Mac with Docker (Docker Engine with the Compose plugin, or Docker Desktop) and git:

```sh
curl -fsSL https://raw.githubusercontent.com/chasshas/median/main/install.sh | sh
```

or, from a clone, `./install.sh`. The installer checks Docker, clones Median into `~/median` (`--dir` to change it), writes `.env` on the first run (random `SECRET_KEY` and database password, a login password you type or one it generates and prints, your machine's time zone, port 8000 or the next free one, and `COMPOSE_FILE=docker-compose.yml` so the stack runs without the development override), builds the image, applies the migrations, starts db, web and worker and waits until the service answers. At the end it prints the address and the login.

Run it again to update: it pulls the code (fast-forward only), backs up the database if the stack is running, rebuilds and migrates. It never overwrites an existing `.env`. Options (`./install.sh --help`): `--yes` (no questions), `--port`, `--timezone`, `--password` or `--no-password`, `--branch`, `--install-docker` (Linux: install Docker with get.docker.com first), `--skip-backup`, `--configure-only` (write `.env` and stop). Windows users run it inside WSL 2 with Docker Desktop.

## Setup by hand

Requirements: a Linux machine (or any host with Docker) with Docker Engine and the Compose plugin, make, and disk space for your collection. Everything else (Python, PostgreSQL 18, ffmpeg, the crawler tools) runs in containers.

```sh
git clone <this repository> median && cd median
cp .env.example .env      # passwords, APP_TIMEZONE, WEB_PORT, APP_PASSWORD, SECRET_KEY
make up                   # build the image, start db, migrate, start web + worker
```

`make up` uses `docker-compose.override.yml`, which bind-mounts the source and reloads on changes (development). For a deployment, leave it out: `make up COMPOSE="docker compose -f docker-compose.yml"` (and the same `COMPOSE=...` for every other make target), or delete the override file.

Media and state live in the `media-data` volume (mounted at `DATA_DIR`, default `/data`) and the database in `pgdata`. Set `APP_PASSWORD` and `SECRET_KEY` before the service is reachable from anywhere but your own machine (see Exposing the service).

| Command | What it does |
| --- | --- |
| `make up` / `make down` / `make logs` | Start, stop and follow the stack (db, web, worker) |
| `make migrate` | Apply Alembic migrations and the Procrastinate schema (safe to repeat) |
| `make test` | Run pytest in the web container against the test database |
| `make lint` / `make format` | Check, or apply, ruff formatting and lint |
| `make e2e` | End-to-end acceptance test on its own throwaway stacks (see Development) |
| `make shell` / `make psql` | Open a shell in web, or psql on the app database |
| `make backup` / `make restore FILE=...` | Dump the database now, or replace it with a dump (see Backups) |
| `make backup-files DEST=...` | Copy archive/ and thumbs/ to a directory with rsync |

## First run

1. Open http://localhost:8000 (or your `WEB_PORT`). With `APP_PASSWORD` set you are asked to sign in first. The dashboard shows file counts, disk usage and job counts.
2. Upload a few files at /upload (drag and drop or "choose files"; optional tags; "force" re-ingests a file you discarded before). The batch card shows how many were new and how many were exact duplicates.
3. Review them at /review. Press `?` for the keys: J/K move, A archive (opens the archive panel to choose a folder and tags), D discard, S skip, C compare with similar files, X select, Shift+A/Shift+D for the selection, U undo, G grid. Discarded originals stay in `trash/` for `TRASH_RETENTION_DAYS` (default 7) so a discard can be undone.
4. Organize at /library (folder tree, moves, bulk tags) and /tags (rename, merge, delete), and open any file at /files/<id>.
5. Search at /search with the syntax of [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) section 12, for example `cat -dog folder:"Animals" collected:2026-01-01..2026-03-31 type:image`; the filter panel, autocomplete and saved searches help. The navigation bar's search box works on every page.

To import an existing collection, put it in `./imports` (or set `IMPORTS_DIR`) and run `docker compose exec web python -m app.cli import /imports --recursive [--tags a,b] [--force]`. Files are copied; the originals stay where they are. Progress: `GET /api/uploads/<batch_id>` or /review.

Perceptual duplicates: `python -m app.cli rehash [--missing-only]` recomputes image and video hashes from the originals and rebuilds their similar pairs; `python -m app.cli recompute-similar` rebuilds all pairs from stored hashes with the current thresholds (dismissed pairs stay dismissed). Run them with `docker compose exec web ...`, or from /settings.

## Adding sources

/sources lists your crawl sources. "New source" offers every installed add-on:

| Add-on | For | Main settings |
| --- | --- | --- |
| yt-dlp (video sites) | channels, playlists, single videos | URLs, format, max items, date after, uploader and category tags, cookies file, extra options |
| gallery-dl (image galleries) | galleries, artist pages, boorus | URLs, which metadata fields become tags (`tags`, `artist=artist`), range, cookies file, extra options |
| Direct links | plain file URLs | URLs, tags, size limit |
| Demo images | trying Median without the network | count, seed, tags |

Each source can have a cron schedule (in `APP_TIMEZONE`, for example `0 */6 * * *`), default tags, a default folder (preselected when you archive its files) and a maximum runtime. "Run now" starts a run; /jobs and /jobs/<id> show its live log, its counts (new, duplicate, already known) and a Cancel button. Files a source already delivered are skipped before download, even if you discarded them, and tags you removed never come back. Only download what you are allowed to download; cookies files are stored under `DATA_DIR/secrets` with mode 0600.

yt-dlp and gallery-dl live in `DATA_DIR/tools/venv`, outside the app. The worker installs or upgrades them each time it starts and the update_tools job keeps them current (see Updating). Extra options that run commands, write elsewhere or load other configuration are refused. To write your own add-on, see [docs/ADDONS.md](docs/ADDONS.md).

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

## Updating

- Median itself: `./install.sh` (it backs up the database, pulls, rebuilds and migrates), or by hand: back up first (`make backup`, and copy the dump off the machine), then `git pull` and `make up`. `make up` rebuilds the image, applies new migrations before web and worker start, and recreates the containers; data in the volumes stays. Read docs/PROGRESS.md for what changed.
- Crawler tools: yt-dlp and gallery-dl are upgraded when the worker starts and by the update_tools job (default Mondays 05:00, setting "Crawler tool update schedule"); /settings shows the installed versions and the last change (also `DATA_DIR/tools/versions.json`). Run it now from /settings, or `docker compose exec web python -m app.cli update-tools`. Restarting the worker (`docker compose restart worker`) also upgrades them.
- Deno (the JavaScript runtime yt-dlp uses) and ffmpeg are part of the image: change the version in the `Dockerfile` if needed and run `make up` to rebuild.
- PostgreSQL stays on major version 18 (`postgres:18`); minor updates come with `docker compose pull db && make up`. A future major upgrade needs a dump and restore, not just a new image.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `make up` fails at the image build | The build needs Docker Hub, ghcr.io, deb.debian.org and apt.postgresql.org. Check the network or proxy; the build also fails on purpose if `pg_dump` is not version 18. |
| The page does not load, port already in use | Another service uses 8000: set `WEB_PORT` in `.env` and `make up` again. |
| A file stays in "processing" | The worker was stopped while it worked on it. Restart the worker (`docker compose restart worker`): files stuck for more than an hour are queued again at startup and by the daily housekeeping. |
| A file is in the Failed tab of /review | Its processing raised an error (shown on the tile and at /files/<id>). Retry it there; files that cannot be decoded are still stored, as media kind "other". |
| A crawl run failed | Open it on /jobs: the error and the log show the tool's output. yt-dlp and gallery-dl often need a newer version (Run now on "Crawler tool update" in /settings) or a cookies file. |
| "No such file" for yt-dlp or gallery-dl | The worker could not install the tools (no network at startup); `DATA_DIR/tools/versions.json` has the error. Fix the network, then restart the worker. |
| A run stays "running" after a crash | Restarting the worker marks it failed with "interrupted"; the next run works. |
| A script gets 403 on POST | Unsafe requests need an `Origin` header naming the server (`curl -H "Origin: http://median.lan:8000" ...`). Behind a reverse proxy, pass the Host header through. |
| The login does not stick behind a proxy | The cookie is Secure when uvicorn sees https: run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>` when the proxy speaks http to it, and keep the Host header. |
| Forgot the password | Change `APP_PASSWORD` in `.env` and `make down && make up`; every session ends. |
| The dashboard warns about free space | Below 10 percent free on the data filesystem. Purge trash (/settings), delete old backups in `DATA_DIR/backups`, or grow the disk. Uploads and moves fail when the disk is full; nothing is deleted. |
| The integrity report lists problems | See Integrity check. Missing originals need the media backup (`make backup-files`) copied back. |
| Similar files are flagged too often or too rarely | Change the thresholds on /settings, then "Recompute similar pairs now". |

Logs: `make logs` (web, worker, db) and the per-run job logs on /jobs/<id> (files in `DATA_DIR/logs/jobs/`).

## Development

Read [CLAUDE.md](CLAUDE.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Tests run with `make test` (`make test ARGS='-m "not slow"'` skips the timing tests); lint with `make lint`. `scripts/smoke.sh` checks the acceptance criteria of every phase against a running stack. `make e2e` runs the end-to-end acceptance test (`scripts/e2e.py`): it starts two throwaway Compose projects (`median-e2e-a`, `median-e2e-b`, fresh volumes, ports 18000 and 18001, a random password), drives the whole pipeline through the HTTP API, prints PASS or FAIL per step and removes the projects again; `E2E_KEEP=1` keeps them for inspection. [docs/MANUAL_CHECKLIST.md](docs/MANUAL_CHECKLIST.md) lists the checks to do in a browser. CI (`.github/workflows/ci.yml`) runs all of this on every push.

## Phases

The project was built in ten phases, 0 to 9.

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
| 9 | End-to-end acceptance, fixes and documentation (done) |
