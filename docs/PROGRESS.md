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
- Tests: 87 in total (71 new): tag normalization, storage, every pipeline case of the phase brief (new, duplicate, discarded duplicate, force, known remote, tag merge, video, audio with and without cover art, corrupt jpeg, error and retry, concurrent identical content), upload API end to end, htmx fragment, Range 206, retry API, pages, ingest() and the CLI.
- scripts/smoke.sh phase 1 checks (media generated in the web container by scripts/smoke_media.py); SMOKE_LOCAL=1 runs them against a native stack.

Verify:
- `make up`, open http://localhost:8000/upload, drop 3 images and 1 video: the batch card ends with new 4; /review shows them with thumbnails; the dashboard's pending review count grows by 4.
- Upload the same files again: the batch card shows duplicate exact 4 and the dashboard totals do not change.
- Open the video at /files/<id>; it plays and seeks (`curl -H 'Range: bytes=0-99' -i localhost:8000/media/<id>/original` returns 206).
- Put files in ./imports, then `docker compose exec web python -m app.cli import /imports --recursive`; follow GET /api/uploads/<batch_id>.
- `make lint`, `make test`, `scripts/smoke.sh`.

Known gaps:
- Cloud session: Docker Hub answered 429 for postgres:18 and the image cannot be built here, so local checks ran natively against PostgreSQL 16 from the sandbox (migrations, 87 tests, uvicorn + worker + full smoke.sh with SMOKE_LOCAL=1, Chromium upload and video seek). CI runs the real image and postgres:18.
- Playwright's Chromium has no H.264 decoder, so the in-browser seek check used a VP9 WebM; MP4 serving is checked by Range requests (smoke.sh, tests).
- No perceptual hashes or similar-file flags yet (phase 2); no archive/discard flow (phase 3), so trash and archive paths are only exercised by tests.
- A file whose processing job is lost (worker killed mid-job) stays in status processing; there is no sweeper yet.
- An ingest that fails before a files row exists (for example an unreadable staging file) records an error event only; the UI cannot retry it.

## Phase 2 - 2026-10-17 - Duplicate detection
Built:
- Migration 0003: enum similar_method; tables video_frame_hashes (file_id, frame_index 0-4, phash) and similar_pairs (file_a_id < file_b_id check, method, distance, dismissed, created_at); index on similar_pairs(file_b_id); partial index on files(duration_ms) for videos. Models in app/db/models_dedup.py.
- app/dedup/hashing.py: ImageHash pHash (hash_size 8) of the EXIF-transposed RGB first frame; to_signed/to_unsigned for bigint storage; video frame hashes at 10/30/50/70/90 percent via ffmpeg (seek before input, one PNG frame piped into Pillow); clips under 1 s hash one frame (frame_index 0).
- app/dedup/index.py: SimilarityIndex protocol and BruteForceIndex over numpy uint64 arrays (numpy.bitwise_count popcount); query(hash, threshold, exclude_id) returns (file_id, distance), nearest first.
- app/dedup/service.py: compute_perceptual (the phase 1 hook now delegates to it) hashes, matches against files in every status and inserts pairs without touching existing ones; video candidates selected in SQL by the duration rule, then frame-by-frame comparison (3 of 5, median distance); rehash and recompute_similar keep dismissed flags of pairs that still match. app/dedup/settings.py: get_dedup_settings().
- CLI: python -m app.cli rehash [--missing-only], python -m app.cli recompute-similar. Procrastinate tasks rehash and recompute_similar (queue maintenance).
- API: GET /api/files/<id>/similar, POST /api/similar/<a>/<b>/<method>/dismiss. /files/<id> has a "Similar files" section (thumbnails, status labels, distance, Dismiss). The temporary /review grid shows "similar to archived or pending (n)" and a red dashed "similar to discarded (n)" badge.
- Tests: 117 in total (30 new): signed conversion and popcount against pure-Python references (random values, high bit), hash bits vs ImageHash hex, EXIF transpose, animation first frame, resized/JPEG q60/WebP variants similar, 5 unrelated structured images not similar, testsrc2 re-encode similar, testsrc2 vs mandelbrot not, 12 s vs 4 s video never a candidate, short clips, discarded variant flagged, exact copy still duplicate_exact, dismissed pairs kept through retry (ingest), rehash and recompute-similar, threshold changes, missing originals, hashing failures, API, badges, file page, CLI and tasks, 100,000-hash query under 1 s (marked slow; deselect with -m "not slow"). Test media are generated (seeded shapes, gradients and text; lavfi testsrc2 and mandelbrot).
- scripts/smoke.sh phase 2 checks: 50 percent copy gets a badge linking to the original, discarding the original via psql then uploading a JPEG q60 copy gets "similar to discarded (1)", exact copies are duplicate_exact, rehash --missing-only and recompute-similar succeed.

Verify:
- `make up`, upload an image at /upload, then a copy resized to 50 percent: /review shows "similar to archived or pending (1)" on the copy, linking to the first file; /files/<copy> lists it under "Similar files".
- `make psql`, `UPDATE files SET status = 'discarded' WHERE id = <first>;`, upload a re-encoded copy: its tile shows "similar to discarded (1)".
- Upload an exact copy: the batch card shows duplicate exact.
- `docker compose exec web python -m app.cli rehash --missing-only` and `docker compose exec web python -m app.cli recompute-similar`.
- `make lint`, `make test` (`make test ARGS='-m "not slow"'` skips the timing test), `scripts/smoke.sh`.

Known gaps:
- Cloud session: Docker has no daemon here, so checks ran natively against PostgreSQL 16 (migrations, 117 tests, uvicorn + worker + full smoke.sh with SMOKE_LOCAL=1, headless Chromium screenshots of /review and /files/<id>). CI runs the image and postgres:18.
- Ingest queries the image index by loading every stored pHash per new file (fine for single-user collections; a cached or BK-tree index can replace BruteForceIndex behind the same interface).
- No compare view or keyboard flow yet (phase 3); a dismissed pair cannot be undismissed.
- rehash/recompute-similar have no UI trigger and no job_runs record until phases 5 and 8.

## Phase 3 - 2026-10-17 - Review queue
Built:
- Migration 0004: enum review_action_kind; table review_actions (file_id, action, previous_status, previous_folder_id, at, undone, plus group_id for replace and detail jsonb for exact undo); model in app/db/models_review.py.
- app/review/service.py: archive (default Inbox, user tags), discard, discard_archived, replace (new file archived into the old one's folder with the union of active tags, old one discarded), compare_decision (keep left, keep right, keep both, replace; all or nothing), undo (newest non-undone action per file only, replace undone as a pair, refused with a message once the trash original is purged). All take lists, lock rows in id order, return per-file results and move originals after commit.
- app/review/trash.py and the purge_trash Procrastinate periodic task (daily 03:17 UTC) plus python -m app.cli purge-trash. app/library/folders.py: Inbox lookup and folder display paths.
- app/review/queue.py and GET /api/review/queue (media_kind, source_key, batch, has_similar, similar_to_discarded, status pending_review|error, order oldest|newest, keyset cursor). POST /api/review/archive, /discard, /discard-archived, /replace, /compare, /undo.
- /review page: pending and failed tabs (failed: Retry buttons), filter form, archive folder and tags, counters (remaining, decided this session, selected); single view (image/video/audio/file card, side panel with name, size, dimensions, duration, collected date, sources with links, tags by origin, similar strip with statuses; next item preloaded), grid view (thumbnails with badges, click or X to select, Shift+click ranges), compare view (both files with dimensions, size, format and dates, larger resolution highlighted; keep left, keep right, keep both, replace). Keyboard map of section 13 plus G, Enter and the compare keys; shortcuts ignored while typing; toasts with Undo; failures keep the item and show the server's message. JS in app/web/static/js/review.js; fragments under /review/fragments/.
- Tests: 155 in total (38 new): every decision and its undo with file locations on disk, tags restored, rollback moves nothing, reused staging names, undo after purge, batches with invalid files, compare choices and refusals, forced re-ingest blocking undo, discarded files still duplicates, purge retention (service, task and CLI), queue order, filters, cursor pages and bad parameters, API results, page shell, failed tab, single/grid/compare fragment rendering.
- scripts/smoke.sh phase 3 checks (20 files decided through the API the keys call, files under archive/ and trash/, undo back to staging, compare fragment and keep left, exact copy of a discarded file dropped, variant shows the discarded match in the strip, purge and refused undo). Phase 1 and 2 checks now read the grid fragment, and the phase 2 discard uses the API.

Verify:
- `make up`, upload 20 images at /upload, open http://localhost:8000/review. Press A and D (keyboard only) until "Remaining" is 0; `make psql`: `SELECT status, folder_id, count(*) FROM files GROUP BY 1, 2;` shows the archived files in Inbox (folder 1); `docker compose exec web ls /data/archive /data/trash`.
- Press U: the last file comes back on screen and its original is back under /data/staging.
- Upload an image and then a 50 percent copy; with the copy on screen press C, then 1 (keep left, discard right).
- Upload the discarded original again (dropped as an exact duplicate), then a re-encoded copy: its similar strip shows the original as "discarded".
- `docker compose exec web python -m app.cli purge-trash`. `make lint`, `make test`, `scripts/smoke.sh`.

Known gaps:
- Cloud session: no Docker daemon, so checks ran natively against PostgreSQL 16 (migrations, 155 tests, uvicorn + worker + full smoke.sh with SMOKE_LOCAL=1). The keyboard flow ran in headless Chromium (Playwright from the sandbox's global Node install, not part of the repo): 20 files decided with A/D only (10 in Inbox under archive/, 10 under trash/), U twice moved files back to staging, typing "dada" in the tags field decided nothing, C then 1 on a resized copy, Shift+click and X selection with Shift+D and U, a rejected decision kept the item with the server's message, no console errors. CI runs smoke.sh (API level) but no browser.
- The undo history and the toast Undo buttons live in the page; after a reload, decisions can be undone only through POST /api/review/undo with the action ids. There is no list of past decisions in the UI.
- A file move that fails after commit (for example a full disk) is only logged; the row then points at a location without the file. An integrity check is phase 8.
- Empty staging/<batch>/ directories stay after their files are decided.
- The Pending and Failed tab counts are from page load; the "Remaining" counter is live.
- purge_trash has no job_runs record or UI trigger until phase 5.

## Phase 4 - 2026-10-17 - Classification and library
Built:
- No migration (phase 1 tables suffice).
- app/library/folders.py: create, rename, move (cycles and case-insensitive sibling clashes rejected), delete (empty directly; non-empty with move_contents_to_parent, merging clashing subfolders recursively), get_tree() with direct and subtree counts of archived files, path_of, resolve_path("A/B"), subtree_ids (recursive CTE), recent_folders; the Inbox cannot be renamed, moved or deleted.
- app/library/tags.py: list_tags with active counts, autocomplete (usage then name), rename_tag with merge, delete_tag (force when in use), add_user_tag, remove_tag, restore_tag, file_tags. app/library/classify.py: move_files (database only), bulk add/remove tags, update_details. app/library/browse.py: library grid query and prev/next neighbours.
- review.service.archive(file_ids, folder_id, add_tags, remove_tags_per_file), undoable.
- API: /api/folders (tree, recent, resolve, get, create, PATCH rename/move, DELETE), /api/tags (list, autocomplete, rename with 409 merge_into, delete), /api/files/<id>/tags (get, add, remove, restore), PATCH /api/files/<id>, /api/library (list, move, tags/add, tags/remove).
- Review: A / Shift+A open the archive panel (searchable folder tree, 5 recent folders, last used folder preselected, inline subfolder, crawler tag chips for a single file, user tags with autocomplete, keep-tags toggle, Enter confirms, Esc cancels). JS in app/web/static/js/archive_panel.js and common.js.
- Pages: /library (folder tree with counts, create/rename/move/delete, grid with include subfolders, sort, order, pagination, multi-select move, add tag, remove tag, discard), /files/<id> (title, description, folder, tag chips with origin, removed tags with restore, prev/next and J/K in the library context), /tags (counts, instant filter, rename with merge confirmation, delete). Each page lists its keys behind ?.
- Tests: 186 in total (32 new): folder cycles, clashes, Inbox protection, delete with merge (including a subfolder named like its parent and a top-level delete into the Inbox), tree counts, recent folders; tag rename merge over every row combination, autocomplete order and escaping, delete with force, per-file add/remove/restore; removed crawler tag surviving a duplicate ingest from the same add-on through the API; archive with folder and tag edits in one call and undo; bulk archive; moving archived files leaves DATA_DIR untouched (inode, size, mtime); API and page rendering.
- scripts/smoke.sh phase 4 checks; scripts/smoke_crawler_ingest.py.

Verify:
- `make up`, ingest files, open http://localhost:8000/review, press A, type part of a nested folder's name, click x on a crawler tag, type a tag and Enter, Enter again: /files/<id> shows the folder and tags, the removed one under "Removed tags".
- /library: the tree shows "direct / with subfolders" counts; select 10 files, Move: the counts change on the reload. Delete a non-empty folder with "Move files and subfolders" ticked: `make psql`, `SELECT count(*) FROM files WHERE status = 'archived' AND folder_id IS NULL;` is 0.
- /tags: rename "cats" to "cat" and confirm the merge.
- `make lint`, `make test`, `scripts/smoke.sh`.

Known gaps:
- Cloud session: no Docker daemon, so checks ran natively against PostgreSQL 16 (migrations, 186 tests, uvicorn + worker + full smoke.sh with SMOKE_LOCAL=1 on a fresh database and again on top of it). Headless Chromium (Playwright from the sandbox's global Node install) drove the acceptance flow: A, search "coast", remove crawler tag "beach", add "Favourite", Enter archived the file into Travel / Coast with exactly those tags; Esc archived nothing; Shift+A on two files with an inline new subfolder; library move of 10 files updated the tree counts; deleting a non-empty folder left no file without a folder; J moved to the next file; tag remove and restore; renaming cats to cat confirmed the merge. No page errors. CI runs smoke.sh (API level) but no browser.
- Library actions (moves, bulk tags) have no undo; the selection is per page.
- The archive panel loads the whole folder tree each time it opens (fine for hundreds of folders).
- Undoing a review action whose tag was later merged into another tag skips that tag's rows.

## Phase 5 - 2026-10-17 - Crawler add-on framework
Built:
- Migration 0005: enums job_kind and job_status; tables crawl_sources (plus max_runtime_minutes) and job_runs (plus queued_at, cancel_requested, queue_job_id) with a partial unique index allowing one queued or running run per source; foreign keys from file_sources.source_id, file_sources.job_run_id and ingest_events.job_run_id (ON DELETE SET NULL). Models in app/db/models_crawl.py.
- app/crawl/base.py: Addon (key, name, version, description, ConfigModel, async run), CrawledItem, CrawlContext (work_dir, state_dir, secrets_dir, log, emit, is_known, known_remote_ids, is_cancelled, sleep, run_subprocess), SecretFile. app/crawl/procs.py: run_subprocess (argument list, own process group, PATH/LANG/HOME plus explicit variables, output streamed to the job log, group killed on timeout, cancel or task cancellation, last 50 lines returned).
- app/crawl/registry.py: discovery of the media_archive.addons entry points and DATA_DIR/addons/*.py (module-level ADDON) at web and worker startup; broken add-ons and duplicate keys are recorded and shown on /sources.
- app/crawl/runner.py and task run_crawl (queue crawl): queued -> running -> succeeded, failed or cancelled; job log at logs/jobs/<id>.log; emit() through the ingest service with source defaults (crawler tags, default folder in files.metadata); cancel flag polled every second; per-source runtime limit; work_dir always removed. app/crawl/service.py: sources CRUD with config validation and secret files (mode 0600), queue_run, cancel_run, lost-run reaping, run stats from ingest_events. app/crawl/scheduling.py and the periodic task schedule_crawls (every minute, croniter, APP_TIMEZONE). app/crawl/forms.py: forms generated from the add-on's JSON schema.
- Built-in demo add-on (app/addons/demo.py, entry point demo): count, seed, tags, delay_seconds; structured Pillow images with remote ids demo-<seed>-<n>.
- Archive panel: preselects the crawl source's default folder (queue items carry default_folder_id).
- API: /api/sources (list with add-ons, fields and add-on errors; create; get; put; patch enabled; delete; run) and /api/jobs (list with stats; detail; log; cancel). Pages: /sources (list, add-on errors, new/edit forms, toggle, Run now, delete), /jobs (runs with status, source, duration, stats) and /jobs/<id> (stats, live log tail polling every 2 s, Cancel). docs/ADDONS.md for add-on authors.
- Packaging: uv_build backend so the project and its entry points are installed; croniter dependency; worker --concurrency=4.
- Tests: 230 in total (45 new crawl tests, plus schema checks): discovery (entry point, single file, seven kinds of broken files, duplicate keys, errors on /sources and in the API), run_subprocess (output and tail, literal "; rm -rf" argument, string refused, limited environment, cwd, timeout and cancel killing a sleeping child of the group, task cancellation), demo run end to end (10 files with file_sources linked to source and run, demo plus default tags as crawler tags, default folder in the review queue, stats, log, work_dir removed) and a second run with only skipped_known_remote, known remote lookups across statuses, cancelling a running demo run (under 3 s), cancel killing a subprocess group, a stubborn add-on, the runtime limit, failing add-on, missing add-on and invalid config, cancelling a queued run, lost-run reaping, scheduler (APP_TIMEZONE, due/not due, coalescing, disabled and manual sources skipped, no overlap, the periodic task), every form field kind rendered and parsed, form create/edit with secret upload (0600) and validation errors, JSON API and pages.
- scripts/smoke.sh phase 5 checks: demo source with count 10 created through the form, Run now, 10 pending files with the right tags and default folder, second run with 10 skipped and no new files, a source scheduled every minute producing two runs about 60 s apart without overlap, cancelling a long run.

Verify:
- `make up`, open http://localhost:8000/sources, choose "Demo images", Create, set count 10 and a tag, Create source, then Run now: the run page shows the log and ends with new 10; /review shows the 10 images with the tags. Run now again: the run shows skipped known remote 10 and the dashboard totals do not change.
- Create a demo source with schedule `* * * * *`: /jobs?source_id=<id> gains one run per minute; disable it with the Enabled button.
- Create a demo source with count 1000 and delay seconds 1, Run now, press Cancel on the run page: it turns cancelled within a few seconds.
- Put a single-file add-on in DATA_DIR/addons/ (see docs/ADDONS.md) and restart web and worker (`make down && make up`); a broken one appears under "Add-on errors" on /sources.
- `make lint`, `make test`, `scripts/smoke.sh`.

Known gaps:
- Cloud session: no Docker daemon, so checks ran natively against PostgreSQL 16 (migrations twice, 230 tests, uvicorn + worker with --concurrency=4, full smoke.sh with SMOKE_LOCAL=1 including the scheduled and cancel checks). Headless Chromium (Playwright from the sandbox's global Node install) checked /sources, the generated form, N and Esc, the live run page (emitted count rising, Cancel turned the run cancelled in about 2 s, polling stopped) and the archive panel preselecting the source's default folder. CI builds the image (now also installing the project with uv_build) and runs postgres:18.
- New or changed add-ons need a restart of web and worker; there is no reload button.
- Runs of other kinds (rehash, purge_trash, ...) still do not write job_runs rows; that comes with the settings and maintenance work in phase 8.
- Cancelling or the runtime limit cannot interrupt blocking (non-async) code inside an add-on; such an add-on stops at its next await.
- The jobs page shows the newest 100 runs without paging; old job logs are never deleted.

## Phase 6 - 2026-10-17 - Built-in crawler add-ons
Built:
- Crawler tool venv: app/crawl/tools.py (tool_path, ensure_tools, versions.json), app/worker.py (the worker's entry point: install or upgrade yt-dlp[default] and gallery-dl in DATA_DIR/tools/venv, then exec the procrastinate worker), python -m app.cli tools. Dockerfile copies Deno 2.9.7 from denoland/deno:bin-2.9.7. run_subprocess passes proxy and CA bundle variables.
- app/crawl/safety.py: extra_args denylists for yt-dlp and gallery-dl that also catch abbreviations, "=value" forms and short option clusters; URL checks. app/crawl/watch.py: run_while_polling, LineTail, StableFiles.
- Add-ons (entry points): ytdlp (app/addons/ytdlp.py: archive rebuilt from the database before each run, emits from done.tsv every 2 s while yt-dlp runs, tags + categories + uploader:<name>, exit 101 and "error after files" count as success), gallerydl (app/addons/gallerydl.py: emits files whose sidecar exists and whose size was stable for 2 s, remote ids with filename fallback, tag_fields with namespaces and nested fields), direct (app/addons/direct.py: httpx streaming, Content-Disposition, size limit, known URLs skipped without a request).
- docs/ADDONS.md: built-in add-ons, remote_id/tags/metadata rules, running programs safely, testing with fake executables, a complete single-file example (checked by loading it and ingesting two files).
- Tests: 416 in total (186 new): fake yt-dlp and gallery-dl executables with slow, grow, partial, no-sidecar and failing modes; ytdlp (archive from the database after losing the state dir, emission while running, tags and uploader namespace, warning after a non-zero exit, failure without files, max_items exit 101, switches); gallerydl (stable size, partial files, remote id fallback, tag_fields lists/strings/nested, emission while running, missing sidecar, recrawl skip); direct (local HTTP server: two images, Content-Disposition, size limit by header and by stream, known URL without a request); every denied option alone, with a value and in "=value" form, plus abbreviations and clusters; tools venv and worker entry point; network tests (skipped by default).
- scripts/smoke.sh phase 6: tool versions (and not importable in the app venv), Deno in the image, a ytdlp source ingesting a video and downloading nothing on the second run, "[debug] JS runtimes: deno-..." in the job and worker logs, a gallerydl source ingesting 2 images with tags from tag_fields, a direct source ingesting 2 images, --exec refused (400).

Verify:
- `make up`; `make logs` shows "crawler tools: {...}" from the worker; `docker compose exec worker cat /data/tools/versions.json`.
- /sources: create "yt-dlp (video sites)" with one short video URL you may download and extra args `--verbose`; Run now. The run shows new 1, the file in /review has the video's tags and `uploader:<name>`, the run log has `[debug] JS runtimes: deno-2.9.7`. Run now again: emitted 0, the log says "has already been recorded in the archive".
- A "gallery-dl (image galleries)" source with one gallery URL: its images arrive with tags from tag_fields. A "Direct links" source with two image URLs: both arrive.
- With real URLs: `make test ARGS="-m network tests/crawl/test_network.py"` with MEDIAN_NET_YTDLP_URL, MEDIAN_NET_GALLERYDL_URL and MEDIAN_NET_DIRECT_URLS set (add them to the web service environment or pass `-e` through `docker compose run`).
- `make lint`, `make test`, `scripts/smoke.sh`.

Known gaps:
- The acceptance checks with real sites need URLs from the user; this session verified the add-ons with the real tools against local media only (smoke.sh locally and in CI). The network tests ran against that local server: gallery-dl and direct passed, ytdlp passed up to its uploader-tag assertion, which needs a real site (yt-dlp's generic extractor has no uploader).
- Cloud session: no Docker daemon. Checks ran natively against PostgreSQL 16 with the tools installed by python -m app.cli tools into a local DATA_DIR and Deno 2.9.7 from PyPI; CI builds the image with the Deno stage.
- The worker needs network at startup to upgrade the tools; without it, it starts with the installed versions (or none, and tool runs fail with "No such file").
- gallery-dl items without an id fall back to "<category>:<filename>", so two such files with the same stem from one category (for example two direct links named image.jpg on different hosts) share a remote id and the second is skipped.
- Cookie files are passed to the tools as given; gallery-dl and yt-dlp may rewrite them (session cookies) inside DATA_DIR/secrets.

## Phase 7 - 2026-10-17 - Search
Built:
- Migration 0006: table saved_searches; indexes files (status, archived_at DESC, id) and files (status, collected_at DESC, id). Model in app/db/models_search.py.
- app/search/filters.py: SearchFilter (every section 12 filter and sort) and search(): tag conditions as EXISTS/NOT EXISTS on active rows by tag id, folder exact or subtree (subtree_ids), local-day date ranges in APP_TIMEZONE, case-insensitive text on title/file name/description, media kinds, source keys, statuses; sorts archived/collected/size asc and desc and random (md5 of seed and id); keyset pagination on (sort key, id) with an opaque cursor and the total count.
- app/search/query.py: tokenizer (double quotes, escapes, positions), parse() into SearchFilter with errors at character positions and warnings for unknown tags, format_query() (canonical, round trip). app/search/params.py (q or filter fields), app/search/suggest.py (autocomplete), app/search/saved.py (saved searches), app/search/views.py (result items with folder paths, active tags, original deleted).
- API: GET /api/search, /api/search/parse, /api/search/suggest; /api/saved-searches CRUD.
- /search page: query box with autocomplete (keys, tags also after - ~ +, folder paths after folder: and folder=:, type/status/source values), filter panel synced with the query box both ways, results with the library grid component (fragments/file_grid.html, js/file_grid.js, now also used by /library) and its bulk move/tag/untag/discard, status badges and "original deleted", sort select, Next/First pages, saved searches sidebar (save, rename, delete, open), syntax errors and warnings. Search box in the navigation on every page. The URL holds q, sort and seed.
- Tests: 470 in total (51 new; 3 network tests skipped by default): parser (every token type, quotes and escapes, negation, any-of, both folder forms, open/closed/single-day ranges, Korean and namespaced tags, 13 invalid tokens with positions, unknown-tag warnings, round trips, canonical form); builder (each filter alone and combined, folder subtree, removed tags never match, Asia/Seoul midnight boundaries); pagination over every sort with page sizes 1, 4 and 7 (ties and NULL archived_at included, random with a fixed seed), bad cursors; API (q, fields, warnings, pages, parse, suggest, saved searches CRUD and reopening); pages (panel shows the query's conditions, panel submit redirect, random seed in the URL, errors, pagination links, discarded tiles, sidebar, nav box); performance (slow): 50,000 files, 500 tags, two tags_all, one tags_none, folder subtree and a date range in about 50 ms locally (limit 300 ms).
- scripts/smoke.sh phase 7: 7 uploaded files archived into Animals, a subfolder and another folder with cat/dog tags; the acceptance query returns exactly what an independent SQL query finds; /search shows the same conditions in the panel; a panel change redirects to the updated query; a saved search reopens with the same results; the results are bulk-moved and found in the new folder.

Verify:
- `make up`, archive a few images into a folder "Animals" (and a subfolder) with tags cat and dog, then open http://localhost:8000/search?q=cat%20-dog%20folder:%22Animals%22%20collected:2026-01-01..2026-12-31%20type:image: only cat images in Animals and its subfolders without dog appear, and the Filters panel shows cat / dog / Animals / the dates / image.
- Tick "video" in the panel: the query box and the URL gain type:video. Type "-d" in the query box: tag suggestions appear; "folder:" lists folder paths.
- Save the search in the sidebar, open another page, click it: the same results. Select results (X or the checkboxes), choose a folder, Move.
- Type the query into the navigation search box on any page.
- `make lint`, `make test` (`make test ARGS='-m "not slow"'` skips the timing tests), `scripts/smoke.sh`.

Known gaps:
- Cloud session: no Docker daemon, so checks ran natively against PostgreSQL 16 (migrations, 470 tests with 467 passed and 3 network skips, uvicorn + worker, full smoke.sh with SMOKE_LOCAL=1). Headless Chromium (Playwright from the sandbox's global Node install) checked the nav box, live query-to-panel sync and error display, panel-to-query/URL update, autocomplete (tags after - and ~, folder paths, keys, values; arrow keys, Enter, Tab), save/rename/open/delete of a saved search with identical results, / focus, J/X selection and a bulk move from the search grid. CI runs smoke.sh (API and HTML level) but no browser.
- Keyset pages have Next and First links only; there is no "previous page" except the browser's back button.
- The text filter is a substring match (ILIKE) without a trigram index; it scans the rows the other filters leave.
- Result items load every folder path per page (fine for hundreds of folders).
- Tags are matched by exact name; there is no namespace wildcard (for example every artist:*).
