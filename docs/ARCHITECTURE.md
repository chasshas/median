# Median - Architecture and decisions

## 1. Purpose and pipeline
Collect -> Check -> Review -> Classify -> Search.
- Collect: crawler add-ons (server-side) and manual upload put files into staging through ONE ingest service.
- Check: exact duplicates are dropped automatically, including duplicates of discarded files. Visually similar files are flagged, never dropped automatically.
- Review: the user archives or discards each pending file. Discarded files keep their hashes so they keep counting as duplicates.
- Classify: each archived file sits in exactly one folder (folders nest) and has any number of tags. Tags supplied by crawlers are added by default; the user can add or remove any tag.
- Search: by tags, folder, dates, type, source, text.

## 2. Stack (change only with a DECISIONS.md entry)
- Python 3.12+, managed with uv. pyproject.toml and uv.lock are committed; Docker installs with uv sync --frozen.
- FastAPI (latest stable) on Uvicorn. Pydantic v2, pydantic-settings for configuration.
- PostgreSQL 18 (Docker image postgres:18).
- SQLAlchemy 2.x async ORM with the psycopg 3 driver. Alembic for migrations.
- Procrastinate (PostgreSQL-backed job queue) for all background work, wrapped in app/jobs/ so it can be replaced.
- UI: server-rendered Jinja2 templates, htmx 2.0.x vendored as a static file (no CDN), small vanilla JS modules, plain CSS. No SPA framework, no Node build step.
- Media: Pillow, ImageHash (pHash), numpy; ffmpeg and ffprobe binaries installed in the image.
- Crawlers: yt-dlp and gallery-dl, installed in a separate persistent venv at DATA_DIR/tools/venv and run as subprocesses (Phases 6 and 8). Never imported into the app process.
- Scheduling helper: croniter (Phase 5).
- Tests: pytest, pytest-asyncio, httpx. Lint and format: ruff.

## 3. Runtime
Docker Compose services sharing one image: db (postgres:18, named volume), web (uvicorn app.main:app on port 8000), worker (procrastinate worker). A dev override bind-mounts the code and enables reload. The same PostgreSQL server hosts the app database and a test database (created by an init script).
Configuration through environment variables (see .env.example): DATABASE_URL, TEST_DATABASE_URL, DATA_DIR (default /data), APP_TIMEZONE (default Asia/Seoul), APP_PASSWORD (Phase 8), IMAGE_PHASH_THRESHOLD (default 6), VIDEO_FRAME_THRESHOLD (default 8), TRASH_RETENTION_DAYS (default 7).

## 4. Repository layout
app/main.py (app factory), app/config.py, app/db/ (engine, session, models per area), app/migrations/ (Alembic), app/storage/ (paths, atomic moves, trash), app/ingest/ (ingest service, probing, hashing, thumbnails), app/dedup/, app/review/, app/library/ (folders, tags, classification), app/crawl/ (add-on interface, registry, runner, scheduling), app/addons/ (built-in add-ons), app/search/, app/ops/ (auth, backup, integrity, settings, tool updates), app/jobs/ (Procrastinate app and task registration), app/web/ (page and htmx fragment routers, templates/, static/), app/api/ (JSON API under /api).
tests/ mirrors app/. docs/ holds ARCHITECTURE.md, PROGRESS.md, DECISIONS.md and later ADDONS.md.
Layering: routers call services; services own transactions; models hold no business logic; only app/storage touches the filesystem layout.

## 5. Storage layout (under DATA_DIR)
- staging/<batch_id>/<file>: ingested files awaiting review (batch_id = upload batch or job run).
- archive/<sha[0:2]>/<sha[2:4]>/<sha>.<ext>: archived originals (sha = hex SHA-256).
- thumbs/<sha[0:2]>/<sha>.webp: thumbnails, max edge 320 px, kept for archived AND discarded files.
- trash/<sha>.<ext>: originals of discarded files, purged after TRASH_RETENTION_DAYS (allows undo).
- work/<job_run_id>/: crawler working directories, removed when a run ends.
- addon_state/<source_id>/: persistent per-source state for add-ons (native download archives).
- secrets/: cookie files and credentials for add-ons, mode 0600.
- tools/venv/: crawler tool venv. logs/jobs/<job_run_id>.log: job logs. backups/: Phase 8.
Rules: moves are atomic (temp file in the same filesystem, then os.replace). Files are deleted or moved only after the database transaction that records the change commits. ext is lowercase and comes from the detected type, falling back to the original extension.

## 6. Data model (target schema; each phase creates only the tables it needs)
All ids are bigint identity; all times timestamptz in UTC.
- files: id; sha256 bytea(32) unique not null; size_bytes; mime; ext; media_kind enum(image, video, audio, other); width, height, duration_ms (nullable); status enum(processing, pending_review, archived, discarded, error); error_message; staging_path (relative, set while processing or pending_review); has_thumb bool; phash bigint (images; 64-bit pHash stored as signed int64); original_filename; title; description; folder_id fk folders (set when archived); collected_at (first ingest of this content); reviewed_at; archived_at; discarded_at; metadata jsonb. Indexes on status, folder_id, collected_at, archived_at, media_kind.
- video_frame_hashes: file_id fk, frame_index smallint 0-4, phash bigint; primary key (file_id, frame_index).
- file_sources: id; file_id fk; source_key text ('upload' or an add-on key); remote_id text null; url text null; source_id fk crawl_sources null; job_run_id fk null; fetched_at; raw_metadata jsonb. Unique (source_key, remote_id) where remote_id is not null.
- ingest_events: id; at; outcome enum(new, duplicate_exact, skipped_known_remote, forced_reingest, error); file_id null; source_key; remote_id; url; job_run_id null; detail text.
- similar_pairs: file_a_id < file_b_id; method enum(image_phash, video_frames); distance smallint; dismissed bool default false; created_at; primary key (file_a_id, file_b_id, method).
- folders: id; parent_id fk folders null (null = top level); name; created_at. Names unique among siblings (unique index on (coalesce(parent_id, 0), lower(name))). A top-level folder "Inbox" is created by migration and cannot be deleted or renamed.
- tags: id; name unique (already normalized); created_at.
- file_tags: file_id, tag_id, origin enum(crawler, user), removed bool default false, created_at; primary key (file_id, tag_id); index (tag_id, file_id) where removed = false.
- crawl_sources: id; name; addon_key; config jsonb; schedule text null (cron); enabled; default_folder_id null; default_tags text[]; last_run_at; created_at.
- job_runs: id; source_id null; kind enum(crawl, rehash, integrity, backup, update_tools, purge_trash); status enum(queued, running, succeeded, failed, cancelled); started_at; finished_at; stats jsonb; log_path; error.
- saved_searches: id; name unique; query text; created_at.
- app_settings: key text primary key; value jsonb.

## 7. File lifecycle
The ingest service is the only way files enter: ingest(path, origin) where origin has source_key, remote_id, url, source_id, job_run_id, tags, title, description, metadata, force (bool). It moves the incoming file into staging and defers the job process_ingest(file path, origin). That job:
1. If remote_id is set and (source_key, remote_id) exists in file_sources and not force: event skipped_known_remote, delete the incoming file, stop.
2. Compute SHA-256 by streaming.
3. If the sha exists: if force and the existing file is discarded, restore it to pending_review with the new copy as its staging file (event forced_reingest). Otherwise event duplicate_exact, add a file_sources row if this source is new, merge crawler tags (insert origin=crawler unless a file_tags row for that tag already exists, removed or not), delete the incoming file, stop.
4. Otherwise create files with status processing, probe type and dimensions (Pillow first, then ffprobe, then the extension), create the thumbnail, compute perceptual hashes (Phase 2), attach file_sources and crawler tags, set status pending_review, event new.
5. On any exception: status error with error_message; the file stays in staging; the review UI can retry.
Review decisions: archive(file, folder, tags) moves staging -> archive path and sets folder_id, status archived, archived_at. discard(file) moves the original to trash/ and sets status discarded, discarded_at; hashes, thumbnail, sources and tags stay. Undo of a discard restores from trash while the file is still there. An archived file can later be discarded from the library (same rules). A purge_trash job deletes trash files older than TRASH_RETENTION_DAYS.

## 8. Duplicate rules
- Exact: SHA-256 equality against files in every status. Automatic.
- Known remote: (source_key, remote_id). Automatic, checked before hashing.
- Perceptual (never automatic, shown in review): images use ImageHash phash with hash_size 8 on the EXIF-transposed RGB image (first frame of animations); similar when Hamming distance <= IMAGE_PHASH_THRESHOLD. Videos hash 5 frames at 10, 30, 50, 70 and 90 percent of the duration; similar when durations differ by at most max(3 percent, 1 s) and at least 3 of the 5 frame pairs are <= VIDEO_FRAME_THRESHOLD. Audio and other files have no perceptual hash.
- New files are compared against all hashed files in any status. Matches are stored in similar_pairs. Dismissed pairs stay hidden.
- Implementation: brute force over a numpy uint64 array behind a SimilarityIndex interface, so a BK-tree can replace it later.

## 9. Tags
Normalization: Unicode NFKC, trim, lowercase, collapse internal whitespace to one underscore, trim underscores, max 100 characters. Optional namespace "ns:value" (ns matches [a-z0-9_]+). Empty results are dropped. Non-Latin text (for example Korean) is kept as is.
Origins: tags from add-ons are inserted with origin crawler. When the user removes a tag the row stays with removed = true, so recrawls and duplicates never re-add it. User-added tags have origin user; re-adding a removed tag sets removed = false and origin = user.
Tag management: rename (merges into an existing tag if the new name exists), delete (with confirmation when in use), list with usage counts.

## 10. Folders
One folder per archived file. Folders can be created, renamed, moved (cycles rejected) and deleted. Deleting a non-empty folder asks whether to move its files and subfolders to the parent. Display paths as "A / B / C". Subtree queries use recursive CTEs.

## 11. Crawler add-ons (Phases 5 and 6)
Add-ons are Python classes registered through the entry-point group media_archive.addons; built-in add-ons register the same way in pyproject.toml. Each add-on has key, name, version, a Pydantic config model, and async run(ctx, config). The CrawlContext gives: work_dir (empty, per run), state_dir (persistent, per source), secrets_dir, a logger writing to the job log, is_known(remote_id), known_remote_ids(), emit(item) (item = path inside work_dir, remote_id, url, title, description, tags, metadata), is_cancelled(), and run_subprocess(args, timeout) (argument list, no shell, output streamed to the job log). emit() hands the file to the ingest service with the run's source_id and job_run_id.
A run_crawl(job_run_id) job takes a queued job_runs row for a source, runs the add-on, records stats (emitted, new, duplicate_exact, skipped_known_remote, errors) from ingest_events, and removes work_dir. A periodic task each minute queues runs whose cron schedule is due, never two runs of one source at once.

## 12. Search (Phase 7)
Filters: folder (with or without subfolders), tags_all, tags_any, tags_none, date range on collected_at or archived_at, media_kind list, source_key list, status (default archived), text (case-insensitive match on title, original_filename, description). Sorts: archived_at desc (default), collected_at, size, random. Keyset pagination.
Query syntax (parsed into the same filters): bare tag = must have; -tag = must not have; ~tag = any of; folder:"A/B" (includes subfolders); folder=:"A/B" (exact folder); collected:2026-01-01..2026-03-31 and archived:..2026-05-01 (open ranges allowed); type:image|video|audio|other; source:<key>; status:pending|archived|discarded; "quoted words" = text search.

## 13. UI
Pages: / (dashboard counts), /upload, /review, /library (folder tree and grid), /files/<id>, /search, /tags, /sources, /jobs, /settings. One base layout with navigation. Media is streamed by a route that supports HTTP Range so videos can seek. Each page lists its keyboard shortcuts behind the ? key.
Review keys: J/K or arrow keys move, A archive, D discard, S skip, C compare with similar files, X select, Shift+A and Shift+D apply to the selection, U undo the last decision.

## 14. Conventions
- Type hints everywhere; ruff clean.
- Services receive an AsyncSession; one transaction per request or job.
- Times stored in UTC and displayed in APP_TIMEZONE.
- Logs go to stdout; job logs also go to logs/jobs/.
- Test media is generated in fixtures (Pillow images; ffmpeg lavfi testsrc videos). No large binary fixtures.
- Tests never use the network. Real-network smoke tests are marked network and skipped by default.
